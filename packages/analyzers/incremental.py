"""Dependency-bound semantic shards over authorized, immutable source bytes.

The index is conservative: type references, shared declarations, mapper includes,
table ownership and adapter conflicts invalidate their readers. Cache entries
never supply permissions or old source identities. A changed dependency vector
is a miss even when the root file itself has not changed.
"""

from collections import defaultdict
import base64
from hashlib import sha256
import json
import re
import zlib

from defusedxml import ElementTree as SafeXML
from defusedxml.common import DefusedXmlException

from .cancellation import checkpoint
from .syntax_cache import active_cache, canonical, MAX_PACKET_BYTES, processor_version, unit

JAVA_TOKENS = re.compile(r'//[^\r\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[\w$]+|[^\s]')
WORD = re.compile(r"[\w$]+\Z")
SQL_WORD = re.compile(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*")
TABLE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([A-Za-z_][\w.]*)", re.I)
MAPPINGS = {"RequestMapping", "GetMapping", "PostMapping", "PutMapping", "PatchMapping", "DeleteMapping"}


def summarize(text, suffix):
    from .java_spring import MAX_SOURCE_BYTES

    result = dict(names=[], exports=[], groups=[], namespace="", includes=[], tables=[], broad=False)
    if len(text.encode()) > MAX_SOURCE_BYTES:
        return result
    if suffix == ".java":
        tokens = []
        for index, match in enumerate(JAVA_TOKENS.finditer(text)):
            if index % 256 == 0:
                checkpoint()
            token = match.group()
            if WORD.fullmatch(token):
                tokens.append(token)
            if len(tokens) > 250_000:
                return dict(result, broad=True)
        names = set(tokens)
        result["names"] = sorted(names)
        result["exports"] = sorted(
            {tokens[i + 1] for i, token in enumerate(tokens[:-1]) if token in {"class", "interface", "enum", "record"}}
        )
        result["groups"] = sorted(
            ({"mapping"} if names & MAPPINGS else set())
            | ({"feign"} if "FeignClient" in names else set())
            | ({"job"} if "XxlJob" in names else set())
        )
        return result
    try:
        root = SafeXML.fromstring(text, forbid_dtd=False, forbid_entities=True, forbid_external=True)
    except (SafeXML.ParseError, DefusedXmlException):
        return result  # The authoritative analyzer also excludes unsafe XML.
    if root.tag != "mapper":
        return result
    namespace = root.attrib.get("namespace", "")
    if not re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+", namespace):
        return result
    result["namespace"] = namespace
    result["names"] = [namespace.rsplit(".", 1)[-1]]
    result["includes"] = sorted(
        {
            ref.rsplit(".", 1)[0] if "." in ref else namespace
            for node in root.iter("include")
            if (ref := node.attrib.get("refid", "")) and "${" not in ref
        }
    )
    # Includes may supply a table after FROM in another file. Propagate these
    # potential identifiers through include readers before table-owner grouping.
    text_content = " ".join(root.itertext())
    tables = {match.group(1).casefold() for match in TABLE.finditer(text_content)}
    for fragment in root.findall("sql"):
        tables.update(token.casefold() for token in SQL_WORD.findall(" ".join(fragment.itertext())))
    result["tables"] = sorted(tables)
    return result


def decode_summary(value, _):
    if set(value) != {"names", "exports", "groups", "namespace", "includes", "tables", "broad"}:
        raise ValueError("Invalid dependency summary")
    for name in ("names", "exports", "groups", "includes", "tables"):
        if (
            not isinstance(value[name], list)
            or len(value[name]) > 250_000
            or any(not isinstance(item, str) for item in value[name])
        ):
            raise ValueError("Invalid dependency names")
    if not isinstance(value["namespace"], str) or type(value["broad"]) is not bool:
        raise ValueError("Invalid dependency scope")
    return value


def dependencies(paths, summaries, services):
    exports, namespaces, groups = defaultdict(set), defaultdict(set), defaultdict(set)
    by_service = defaultdict(set)
    for path in paths:
        summary, service = summaries[path], services[path]
        by_service[service].add(path)
        for name in summary["exports"]:
            exports[service, name].add(path)
        if summary["namespace"]:
            namespaces[service, summary["namespace"]].add(path)
        for group in summary["groups"]:
            groups[group, None if group == "feign" else service].add(path)
    graph = {path: {path} for path in paths}
    for path in paths:
        summary, service = summaries[path], services[path]
        for name in summary["names"]:
            graph[path].update(exports[service, name])
        for namespace in [summary["namespace"], *summary["includes"]]:
            graph[path].update(namespaces[service, namespace])
        for group in summary["groups"]:
            graph[path].update(groups[group, None if group == "feign" else service])
        if summary["broad"]:
            graph[path].update(by_service[service])
            for other in by_service[service]:
                graph[other].add(path)
    # SQL ownership is global within a service. Includes can supply literal table
    # names indirectly; all potential owners must see the same first declaration.
    tables = {path: set(summaries[path]["tables"]) for path in paths}
    changed = True
    while changed:
        checkpoint()
        changed = False
        for path in paths:
            if not summaries[path]["namespace"]:
                continue
            expanded = tables[path].union(*(tables[other] for other in graph[path]))
            if expanded != tables[path]:
                tables[path] = expanded
                changed = True
    table_owners = defaultdict(set)
    for path in paths:
        for table in tables[path]:
            table_owners[services[path], table].add(path)
    for path in paths:
        for table in tables[path]:
            graph[path].update(table_owners[services[path], table])
    return graph


def closure(graph, root):
    seen, pending = set(), [root]
    while pending:
        checkpoint()
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(graph[current] - seen)
    return seen


def project(analyzer, path):
    result = analyzer.result
    relations = [
        row for row in result.relations if analyzer.relation_owners[row["from_key"], row["to_key"], row["type"]] == path
    ]
    evidence_keys = {key for row in relations for key in row["evidence_keys"]}
    return dict(
        path=path,
        entities=[row for row in result.entities if analyzer.entity_owners[row["stable_key"]] == path],
        evidence=[
            row
            for row in result.evidence
            if analyzer.evidence_owners[row["stable_key"]] == path or row["stable_key"] in evidence_keys
        ],
        relations=relations,
        issues=[row for row in result.issues if row["artifact_path"] == path],
        metrics=analyzer.file_metrics.get(path, {}),
        supported=path in result.supported_paths,
    )


def load(backend, key, processor, path, allowed):
    checkpoint()
    packet = backend.load("semantic", key, processor)
    try:
        raw = canonical(packet["payload"])
        if (
            packet["source_sha256"] != key
            or packet["processor"] != processor
            or len(raw) > MAX_PACKET_BYTES
            or sha256(raw).hexdigest() != packet["payload_sha256"]
        ):
            return None
        encoded = packet["payload"]
        if (
            encoded["encoding"] != "json-zlib-v1"
            or type(encoded["decoded_bytes"]) is not int
            or not 0 <= encoded["decoded_bytes"] <= MAX_PACKET_BYTES
        ):
            return None
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(base64.b64decode(encoded["data"], validate=True), MAX_PACKET_BYTES + 1)
        if (
            len(decoded) != encoded["decoded_bytes"]
            or len(decoded) > MAX_PACKET_BYTES
            or not decoder.eof
            or decoder.unconsumed_tail
            or decoder.unused_data
        ):
            return None
        data = json.loads(decoded)
        if data["path"] != path or type(data["supported"]) is not bool:
            return None
        for kind in ("entities", "evidence", "relations", "issues"):
            if (
                not isinstance(data[kind], list)
                or len(data[kind]) > 100_000
                or any(not isinstance(row, dict) for row in data[kind])
            ):
                return None
        if any(type(n) is not int or n < 0 for n in data["metrics"].values()):
            return None
        if any(
            row["artifact_path"] not in allowed or not isinstance(row["stable_key"], str)
            for row in [*data["entities"], *data["evidence"]]
        ):
            return None
        if any(row["artifact_path"] != path for row in data["issues"]):
            return None
        for row in data["relations"]:
            if (
                any(not isinstance(row[name], str) for name in ("from_key", "to_key", "type", "reason", "causal_basis"))
                or not isinstance(row["evidence_keys"], list)
                or any(not isinstance(k, str) for k in row["evidence_keys"])
            ):
                return None
        return data
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, zlib.error):
        return None


def store(backend, key, processor, data):
    raw = canonical(data)
    if len(raw) <= MAX_PACKET_BYTES:
        encoded = dict(
            encoding="json-zlib-v1",
            decoded_bytes=len(raw),
            data=base64.b64encode(zlib.compress(raw, level=3)).decode("ascii"),
        )
        payload = canonical(encoded)
        backend.store(
            "semantic",
            key,
            processor,
            dict(source_sha256=key, processor=processor, payload_sha256=sha256(payload).hexdigest(), payload=encoded),
        )


def normalize(result):
    for name in ("entities", "evidence", "relations", "issues"):
        getattr(result, name).sort(key=canonical)
    return result


def merge(shards):
    from .java_spring import AnalysisResult, MAX_DECLARATIONS

    result = AnalysisResult()
    entities, evidence, relations = {}, {}, {}
    for path, shard in shards.items():
        checkpoint()
        for name, values in (("entities", entities), ("evidence", evidence), ("relations", relations)):
            for row in shard[name]:
                key = row["stable_key"] if name != "relations" else (row["from_key"], row["to_key"], row["type"])
                if key in values and values[key] != row:
                    raise ValueError("Conflicting dependency cache projections")
                values[key] = row
        result.issues.extend(shard["issues"])
        if shard["supported"]:
            result.supported_paths.add(path)
        for name, count in shard["metrics"].items():
            result.metrics[name] = result.metrics.get(name, 0) + count
    if len(entities) > MAX_DECLARATIONS:
        raise ValueError("Java declaration limit exceeded")
    result.entities, result.evidence, result.relations = (
        list(entities.values()),
        list(evidence.values()),
        list(relations.values()),
    )
    if any(
        row["from_key"] not in entities
        or row["to_key"] not in entities
        or not set(row["evidence_keys"]) <= evidence.keys()
        for row in relations.values()
    ):
        raise ValueError("Incomplete dependency cache projection")
    return normalize(result)


def analyze_incremental(artifacts, sources, texts=None):
    from .java_spring import Analyzer, VERSION

    backend = active_cache()
    if backend is None:
        return normalize(Analyzer(artifacts, sources, texts).run())
    files = {
        row["path"]: row
        for row in artifacts
        if row["kind"] == "source" and row["path"].lower().endswith((".java", ".xml"))
    }
    paths = list(files)
    summaries, fingerprints, services = {}, {}, {}
    reader = Analyzer(artifacts, sources, texts)
    for path, artifact in files.items():
        checkpoint()
        text = reader.text(artifact)
        suffix = ".java" if path.lower().endswith(".java") else ".xml"
        summaries[path], _ = unit(
            "dependency_summary",
            (suffix + "\n" + text).encode(),
            lambda: summarize(text, suffix),
            lambda value: value,
            decode_summary,
        )
        services[path] = sources[artifact["source_id"]].get("service_key", artifact["source_id"])
        fingerprints[path] = dict(
            path=path,
            source_id=artifact["source_id"],
            service=services[path],
            raw_sha256=artifact["sha256"],
            text_sha256=sha256(text.encode()).hexdigest(),
        )
    graph = dependencies(paths, summaries, services)
    positions = {path: index for index, path in enumerate(paths)}
    processor = processor_version()
    shards, keys, misses, work = {}, {}, set(), set()
    for path in paths:
        scope = closure(graph, path)
        keys[path] = sha256(
            canonical(dict(root=path, dependencies=[fingerprints[p] for p in sorted(scope, key=positions.__getitem__)]))
        ).hexdigest()
        cached = load(backend, keys[path], processor, path, scope)
        if cached is None:
            misses.add(path)
            work.update(scope)
        else:
            shards[path] = cached
    analyzer = Analyzer([files[p] for p in paths if p in work], sources, texts)
    analyzer.run()
    for path in paths:
        if path in misses:
            shards[path] = project(analyzer, path)
            store(backend, keys[path], processor, shards[path])
    fallback = False
    try:
        result = merge({p: shards[p] for p in paths})
    except (KeyError, TypeError, ValueError):
        # Disposable cache cannot omit or select a competing logical fact.
        analyzer = Analyzer(list(files.values()), sources, texts)
        analyzer.run()
        work, misses, fallback = set(paths), set(paths), True
        shards = {p: project(analyzer, p) for p in paths}
        for path in paths:
            store(backend, keys[path], processor, shards[path])
        result = merge(shards)
    for name in ("syntax_parsed_files", "syntax_reused_files"):
        result.metrics[name] = analyzer.metrics[name]
    for path in set(paths) - work:
        result.metrics["syntax_reused_files"] += shards[path]["metrics"].get("syntax_parsed_files", 0) + shards[path][
            "metrics"
        ].get("syntax_reused_files", 0)
    result.metrics.update(
        entity_count=len(result.entities),
        relation_count=len(result.relations),
        unsupported_or_unresolved=len(result.issues),
        parser_version=VERSION,
        dependency_indexed_files=len(paths),
        dependency_fingerprint_misses=len(misses),
        dependency_recomputed_files=len(work),
        dependency_reused_files=len(paths) - len(work),
        dependency_calls_recomputed=analyzer.metrics["calls"],
        dependency_cache_fallbacks=int(fallback),
    )
    return result
