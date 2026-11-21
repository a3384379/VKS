"""Keyword and optional embedding retrieval over authorized, exact selections."""

import re
import socket
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Lock, Timer
from time import monotonic

import anyio

from .interactive import InteractiveKnowledgeService
from .interactive_ai import ChapterIndex, tokens
from .knowledge_locator import saved_scope
from .models import Credential, Entity, now
from .reading import chunks
from .service import DomainError, canonical, sha
from .vector_rag import fusion_scores

SEARCH_SECONDS = 15
EMBEDDING_SECONDS = 5
MAX_QUERY = 1000
MAX_RESULTS = 20
MAX_EXCERPT = 1200
MAX_TOTAL = 6000
MAX_DATA_BYTES = 56 * 1024


class SearchControl:
    def __init__(self, *, reauthenticate=None):
        self.deadline = monotonic() + SEARCH_SECONDS
        self.cancelled = Event()
        self.lock = Lock()
        self.provider = None
        self.reauthenticate = reauthenticate

    def check(self):
        if self.cancelled.is_set():
            raise DomainError("VKS_CANCELLED", "知识检索已停止", 499)
        if monotonic() >= self.deadline:
            raise DomainError("VKS_SEARCH_TIMEOUT", "知识检索超过等待上限", 503, retryable=True)

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            if self.provider:
                self.provider.interrupt()

    @contextmanager
    def embedding(self):
        provider = EmbeddingCall(self)
        with self.lock:
            self.check()
            self.provider = provider
        timer = Timer(max(0, provider.deadline - monotonic()), provider.interrupt)
        timer.daemon = True
        timer.start()
        try:
            yield provider
        finally:
            timer.cancel()
            with self.lock:
                self.provider = None


class EmbeddingCall:
    def __init__(self, control):
        self.control = control
        self.deadline = min(control.deadline, monotonic() + EMBEDDING_SECONDS)
        self.lock = Lock()
        self.socket = None
        self.interrupted = False

    def check(self):
        self.control.check()
        if self.interrupted or monotonic() >= self.deadline:
            raise TimeoutError("embedding request timed out")

    def timeout(self):
        self.check()
        return max(0.01, self.deadline - monotonic())

    def attach(self, connection):
        with self.lock:
            self.check()
            self.socket = connection.sock

    def detach(self):
        with self.lock:
            self.socket = None

    def interrupt(self):
        with self.lock:
            self.interrupted = True
            if self.socket is not None:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


async def run_search_request(request, search, *, reauthenticate=None):
    """Pass disconnects to the worker and its pinned embedding connection."""
    control = SearchControl(reauthenticate=reauthenticate)
    error = result = None

    async def watch():
        while (await request.receive())["type"] != "http.disconnect":
            pass
        control.cancel()
        tasks.cancel_scope.cancel()

    with anyio.move_on_after(SEARCH_SECONDS) as timeout:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(watch)
            try:
                result = await anyio.to_thread.run_sync(search, control, abandon_on_cancel=True)
            except DomainError as exc:
                error = exc
            finally:
                control.cancel()
                tasks.cancel_scope.cancel()
    if timeout.cancel_called:
        raise DomainError("VKS_SEARCH_TIMEOUT", "知识检索超过等待上限", 503, retryable=True)
    if error is not None:
        raise error
    if result is None:
        raise DomainError("VKS_CANCELLED", "知识检索已停止", 499)
    return result


@dataclass
class SearchRow:
    entity: dict
    content: str
    direct: str | None
    document: str | None = None
    source_kind: str | None = None
    source_sections: list[str] | None = None
    document_result: bool = False


def _authorized_view(reader, session, actor, pid, payload):
    # A human actor's project grants revalidate membership, while its session
    # credential must also be checked after a potentially slow provider call.
    if actor.human and actor.credential_id and not actor.credential_id.startswith("oauth:"):
        credential = session.get(Credential, actor.credential_id)
        if not credential or credential.revoked or credential.expires_at <= now():
            raise DomainError("VKS_UNAUTHENTICATED", "会话或令牌已失效", 401)
    return reader._search_context(session, actor, pid, payload)


def _scope_key(reader, session, actor, pid, view):
    analysis, release, choices, _, visible, endpoints = view
    baseline = (
        (release.id, release.manifest_sha256)
        if release
        else (analysis.id, analysis.selection_version) if analysis else None
    )
    return baseline, choices, visible, endpoints, reader.permissions(session, actor, pid)


def _collect(reader, s, actor, pid, payload, query, view, control):
    analysis, _, choices, index, visible, endpoints = view
    kinds = payload.get("kinds", ())
    project_documents = "document" in kinds and "section" not in kinds
    rows, sections, points = {}, set(), set()
    for item in reader._search_rows(s, pid, visible, payload, check=control.check):
        rid, eid, name, content, digest, semantic, schema, metadata, locator, stable, kind, path = item
        match = reader._search_match(query, name, stable, path, content, metadata, locator, canonical)
        if kind != "section" and not match and not (project_documents and kind == "document"):
            continue
        rows[rid] = SearchRow(
            {
                "entity_id": eid,
                "entity_revision_id": rid,
                "type": kind,
                "name": name,
                "content_sha256": digest,
                "semantic_content_sha256": semantic,
                "schema_version": schema,
            },
            content,
            match,
        )
        if kind == "section":
            sections.add(rid)
        elif kind == "document":
            rows[rid].source_kind = "authored_knowledge"
            rows[rid].document_result = project_documents
        elif kind == "business_point":
            points.add(rid)
            rows[rid].source_kind = (
                "generated_binding" if metadata.get("origin") == "model" else "authored_knowledge"
            )
    links = InteractiveKnowledgeService(reader.service)
    if points:
        active, citations = set(), defaultdict(set)
        for batch in chunks(points):
            for edge in reader.edge_rows(s, analysis.id, index, frontier=batch):
                control.check()
                start, end = edge["from_revision_id"], edge["to_revision_id"]
                if (
                    edge["excluded"]
                    or edge["validity_status"] != "valid"
                    or start not in endpoints
                    or end not in endpoints
                    or not reader.service._visible_relation(s, actor, pid, edge)
                ):
                    continue
                if edge["relation_type"] == "has_business_point" and end in points:
                    active.add(end)
                elif edge["relation_type"] == "supported_by_section" and start in points:
                    citations[start].add(end)
        source_sections = set().union(*citations.values()) if citations else set()
        sources = links._selected_citation_documents(
            s, actor, pid, choices, reader.revision_rows(s, source_sections)
        )
        for rid in points:
            permitted = citations[rid] & sources.keys()
            if rid not in active or not permitted or permitted != citations[rid]:
                del rows[rid]
            else:
                rows[rid].source_sections = sorted(permitted)[:3]
    if project_documents:
        from .documents import DocumentService

        documents = reader.revision_rows(s, [rid for rid, row in rows.items() if row.entity["type"] == "document"])
        for document in documents:
            control.check()
            try:
                chapters = DocumentService._sections(s, s.get(Entity, document.entity_id), document)
            except DomainError as exc:
                if exc.code != "VKS_DOCUMENT_SECTIONS_UNRESOLVED":
                    raise
                continue
            for chapter in chapters:
                if chapter.id not in endpoints:
                    continue
                sections.add(chapter.id)
                match = query in chapter.content.casefold() or query in chapter.name.casefold()
                rows[chapter.id] = SearchRow(
                    rows[document.id].entity, chapter.content, "fulltext" if match else None, document_result=True
                )
    if not sections:
        return rows, []
    chapters = reader.revision_rows(s, sections)
    documents = links._selected_citation_documents(s, actor, pid, choices, chapters)
    corpus = []
    for chapter in sorted(chapters, key=lambda row: row.id):
        control.check()
        rid = chapter.id
        if rid not in documents:
            del rows[rid]
            continue
        document = documents[rid][1]
        rows[rid].document = document.id
        rows[rid].source_kind = "original_section"
        corpus.append({
            "revision_id": rid,
            "revision_sha256": chapter.content_sha256,
            "name": chapter.name,
            "content": chapter.content,
            "document_revision_id": document.id,
            "document_name": document.name,
        })
    return rows, corpus


def _term_positions(content, terms):
    # Match the word and CJK bigram boundaries of the chapter index while
    # preserving character offsets into the unchanged source.
    for match in re.finditer(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]+", content):
        word = match.group()
        if word[0] < "\u3400":
            if (term := word.lower()) in terms:
                yield match.start(), term
            continue
        for index in range(len(word) - 1):
            if (term := word[index:index + 2]) in terms:
                yield match.start() + index, term


def _excerpt(content, query, terms, semantic_start, limit):
    if not content:
        return "", False
    if not limit:
        return "", True
    if semantic_start is not None:
        end = semantic_start + min(limit, 900)
        return content[semantic_start:end], semantic_start > 0 or end < len(content)
    literal = re.search(re.escape(query), content, flags=re.IGNORECASE)
    if literal:
        offset = literal.start()
    elif not terms:
        return "", True
    else:
        window, counts = deque(), Counter()
        span = limit - min(100, limit // 4)
        best_count, offset = 0, None
        for position, term in _term_positions(content, terms):
            end = position + min(len(term), span)
            while window and end - window[0][0] > span:
                _, expired = window.popleft()
                counts[expired] -= 1
                if counts[expired] == 0:
                    del counts[expired]
            window.append((position, term))
            counts[term] += 1
            if len(counts) > best_count:
                best_count, offset = len(counts), window[0][0]
                if best_count == len(terms):
                    break
        if offset is None:
            return "", True
    leading = min(100, limit // 4)
    if literal:
        leading = min(leading, max(0, limit - len(literal.group())))
    start = max(0, offset - leading)
    return content[start : start + limit], start > 0 or start + limit < len(content)


def _budget(rows, ranked, query, terms, semantic, payload):
    limit = payload.get("limit", 8)
    per_hit = payload.get("max_excerpt_chars", MAX_EXCERPT)
    remaining = payload.get("max_total_chars", MAX_TOTAL)
    found, documents, collapsed = [], Counter(), set()
    truncated = False
    for rid, match in ranked:
        row = rows[rid]
        if row.document_result and row.entity["entity_revision_id"] in collapsed:
            continue
        if len(found) >= limit:
            truncated = True
            break
        if row.document and documents[row.document] >= 3:
            truncated = True
            continue
        excerpt, partial = _excerpt(
            row.content, query, terms, semantic.get(rid) if match in {"hybrid", "semantic"} else None,
            min(per_hit, remaining),
        )
        hit = {
            "entity": row.entity,
            "excerpt": excerpt,
            "evidence_ids": [],
            "match_kind": match,
            "excerpt_truncated": partial,
        }
        if row.source_kind:
            hit["source_kind"] = row.source_kind
        if row.source_sections:
            hit["source_section_revision_ids"] = row.source_sections
        if row.document:
            hit["document_revision_id"] = row.document
            documents[row.document] += 1
        if row.entity["type"] == "document":
            collapsed.add(row.entity["entity_revision_id"])
        found.append((rid, hit))
        remaining -= len(excerpt)
        truncated |= partial
    return found, truncated


def _cap_serialized_hits(hits):
    """Enforce a second budget on the JSON actually returned to the caller."""
    result, size, truncated = [], 2, False
    for hit in hits:
        remaining = MAX_DATA_BYTES - size - 1
        encoded = canonical(hit).encode()
        if len(encoded) > remaining and hit["evidence_ids"]:
            # Keep the immutable chapter locator when a highly connected node
            # would otherwise exhaust the whole response with evidence IDs.
            hit["evidence_ids"] = []
            truncated = True
            encoded = canonical(hit).encode()
        size += len(encoded) + 1
        if size > MAX_DATA_BYTES:
            break
        result.append(hit)
    return result, truncated or len(result) < len(hits)


def search(reader, actor, pid, payload, *, metadata=None, control=None):
    mode = payload["retrieval_mode"]
    query = payload["query"].strip()
    limit = payload.get("limit", 8)
    if (
        mode not in {"keyword", "hybrid", "auto"}
        or not query
        or len(payload["query"]) > MAX_QUERY
        or not 1 <= limit <= MAX_RESULTS
        or not 1 <= payload.get("max_excerpt_chars", MAX_EXCERPT) <= MAX_EXCERPT
        or not 1 <= payload.get("max_total_chars", MAX_TOTAL) <= MAX_TOTAL
    ):
        raise DomainError("VKS_INVALID_INPUT", "请输入 1–1000 字符的检索词并使用有效的结果和摘要预算", 422)
    control = control or SearchControl()
    terms = tokens(query)
    service = reader.service
    assistant = getattr(service, "_search_assistant", None)
    with reader.db() as s:
        view = _authorized_view(reader, s, actor, pid, payload)
        baseline = _scope_key(reader, s, actor, pid, view)
        rows, chapters = _collect(reader, s, actor, pid, payload, query.casefold(), view, control)
        corpus = sha(canonical([pid, [
            (c["revision_id"], c["document_revision_id"], c["document_name"], c["revision_sha256"])
            for c in chapters
        ]]))

    def check_baseline(s, fresh):
        control.check()
        if _scope_key(reader, s, actor, pid, fresh) != baseline:
            raise DomainError("VKS_VERSION_CONFLICT", "知识检索期间资料范围或权限已变化，请重新检索", 412)

    def check_authentication():
        control.check()
        if control.reauthenticate and control.reauthenticate() != actor:
            raise DomainError("VKS_VERSION_CONFLICT", "知识检索期间认证身份或权限已变化，请重新检索", 412)

    def validate_scope():
        check_authentication()
        with reader.db() as s:
            check_baseline(s, _authorized_view(reader, s, actor, pid, payload))

    index = assistant._index(corpus, chapters, check=control.check) if assistant else ChapterIndex(
        chapters, check=control.check
    )
    lexical = [row["revision_id"] for row in index.rank(terms, label=query, check=control.check)]
    exact = [(rid, "exact") for rid, row in rows.items() if row.direct == "exact"]
    exact.sort(key=lambda pair: (rows[pair[0]].entity["name"], pair[0]))
    semantic, degraded, used = {}, None, "keyword"
    if mode in {"hybrid", "auto"}:
        if assistant:
            semantic, degraded, embedded = assistant.rag.search_cached(
                actor, pid, query, chapters, control, validate_scope
            )
            if embedded:
                used = "hybrid"
        else:
            degraded = "embedding_disabled"
    scores = fusion_scores(lexical, semantic)
    lexical_ids = set(lexical)
    ranked = exact + [
        (rid, "hybrid" if rid in semantic and rid in lexical_ids else "semantic" if rid in semantic else "fulltext")
        for rid in sorted(scores, key=lambda rid: (-scores[rid], rid))
        if rows[rid].direct != "exact"
    ]
    ranked.extend(
        (rid, "fulltext")
        for rid, row in sorted(rows.items(), key=lambda pair: (pair[1].entity["name"], pair[0]))
        if row.direct == "fulltext" and rid not in scores
    )
    found, truncated = _budget(rows, ranked, query, terms, semantic, payload)
    check_authentication()
    with reader.db() as s:
        fresh = _authorized_view(reader, s, actor, pid, payload)
        check_baseline(s, fresh)
        analysis, release, choices, index, _, endpoints = fresh
        hits = reader._search_references(s, actor, pid, analysis, release, choices, index, endpoints, found)
        hits, capped = _cap_serialized_hits(hits)
        control.check()
        if metadata is not None:
            retrieval = {"requested_mode": mode, "used_mode": used, "truncated": truncated or capped}
            if degraded:
                retrieval["degraded_reason"] = degraded
            metadata["retrieval"] = retrieval
            if release:
                metadata["scope"] = saved_scope(service.release_dto(release))
    return hits
