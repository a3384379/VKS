"""Permission-first, immutable section comparisons. Never applies a document edit."""

import difflib
from typing import Any
from fastapi import Depends, Request
from pydantic import BaseModel
from .models import Analysis, Entity, Revision
from .documents import DocumentService
from .versions import VersionService
from .reading import ReadingService
from .service import DomainError, canonical, sha
from .v2_contracts import Envelope


def patch(before, after):
    # Bound both SequenceMatcher inputs and rendered output, preserving counts.
    left, right = before.splitlines(), after.splitlines()
    truncated = len(before) > 100000 or len(after) > 100000 or max(len(left), len(right)) > 3000
    if truncated:
        return dict(
            text="差异超过交互配额，请查看准确修订的原文。",
            is_truncated=True,
            before_lines=len(left),
            after_lines=len(right),
        )
    lines = list(difflib.unified_diff(left, right, fromfile="before", tofile="after", lineterm=""))
    text = "\n".join(lines[:200])
    truncated = len(lines) > 200 or len(text) > 16000
    return dict(text=text[:16000], is_truncated=truncated, before_lines=len(left), after_lines=len(right))


class DocumentDiffOutput(BaseModel):
    project_id: str
    document_id: str
    from_release_id: str
    to_analysis_set_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    sections: list[dict[str, Any]]
    denominator: dict[str, int]
    is_truncated: bool
    report_sha256: str
    requires_document_review: bool = True


class DocumentDiffService:
    def __init__(self, service):
        self.service = service

    def get(self, actor, pid, eid, release_id, aid, expected_revision_id=None):
        service = self.service
        with service.db() as s:
            service.require(s, actor, pid)
            old, target = VersionService(service)._baselines(s, pid, release_id, aid)
            source = s.get(Analysis, old.analysis_set_id)
            old_choices, _ = ReadingService(service).selections(s, source, old)
            entity = s.get(Entity, eid)
            if not entity or entity.project_id != pid or entity.type != "document":
                raise DomainError("VKS_NOT_FOUND", "未找到可见文档", 404)
            before = s.get(Revision, old_choices.get(eid)) if old_choices.get(eid) else None
            after = next((r for r in service._revisions(s, aid) if r.entity_id == eid), None)
            if expected_revision_id is not None and (after.id if after else "-") != expected_revision_id:
                raise DomainError("VKS_VERSION_CONFLICT", "文档修订已改变，请重新检测并核对提案", 412)
            if not before and not after:
                raise DomainError("VKS_NOT_FOUND", "文档不属于比较的版本", 404)
            pairs = []
            for parent in (before, after):
                if parent and not service._visible_revision(s, actor, pid, parent):
                    raise DomainError("VKS_NOT_FOUND", "未找到可见文档", 404)
                sections = DocumentService(service)._sections(s, entity, parent) if parent else []
                if any(row.project_id != pid or not service._visible_revision(s, actor, pid, row) for row in sections):
                    raise DomainError("VKS_NOT_FOUND", "当前权限不能比较完整文档修订", 404)
                if len({r.entity_id for r in sections}) != len(sections):
                    raise DomainError("VKS_DOCUMENT_SECTIONS_UNRESOLVED", "章节映射重复，需复核准确历史映射", 409)
                pairs.append(sections)
            maps = [{row.entity_id: (index, row) for index, row in enumerate(rows)} for rows in pairs]
            order = list(dict.fromkeys([r.entity_id for rows in pairs for r in rows]))
            changes = []
            unchanged = 0

            def frame(row):
                return (
                    dict(revision_id=row.id, content_sha256=row.content_sha256, name=row.name, locator=row.locator)
                    if row
                    else None
                )

            for section in order:
                old_index, a = maps[0].get(section, (None, None))
                new_index, b = maps[1].get(section, (None, None))
                if a and b and a.id == b.id and old_index == new_index:
                    unchanged += 1
                    continue
                kind = (
                    "added"
                    if not a
                    else "removed"
                    if not b
                    else "modified"
                    if a.content != b.content
                    else "renamed"
                    if a.name != b.name
                    else "moved"
                    if old_index != new_index or a.locator != b.locator
                    else "metadata_only"
                )
                changes.append(
                    dict(
                        section_id=section,
                        section_key=s.get(Entity, section).stable_key,
                        change_kind=kind,
                        before=frame(a),
                        after=frame(b),
                        old_index=old_index,
                        new_index=new_index,
                        locked_before=bool(before and section in before.metadata_json.get("locked_section_ids", [])),
                        locked_after=bool(after and section in after.metadata_json.get("locked_section_ids", [])),
                        patch=patch(a.content if a else "", b.content if b else ""),
                    )
                )
            result = dict(
                project_id=pid,
                document_id=eid,
                from_release_id=release_id,
                to_analysis_set_id=aid,
                before=frame(before),
                after=frame(after),
                sections=changes[:200],
                denominator=dict(
                    before_sections=len(pairs[0]),
                    after_sections=len(pairs[1]),
                    changed_sections=len(changes),
                    unchanged_sections=unchanged,
                ),
                is_truncated=len(changes) > 200,
                requires_document_review=True,
            )
            result["report_sha256"] = sha(canonical(result))
            return result


def install_document_diff(app, service, actor, envelope):
    diff = DocumentDiffService(service)

    @app.get("/api/v2/projects/{pid}/documents/{eid}/section-diff", response_model=Envelope[DocumentDiffOutput])
    def get(
        pid: str,
        eid: str,
        from_release_id: str,
        to_analysis_set_id: str,
        request: Request,
        expected_revision_id: str | None = None,
        a=Depends(actor),
    ):
        return envelope(request, diff.get(a, pid, eid, from_release_id, to_analysis_set_id, expected_revision_id))
