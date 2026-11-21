"""Project tools shared by Studio conversations and the official MCP gateway."""

from typing import Annotated, Literal

from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .interactive import InteractiveBindingOutput
from .interactive_ai import AssistanceInput, InteractiveAssistant
from .knowledge_locator import DocumentSectionLocator, PrototypeBindingLocator, binding_reference, section_reference
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import Input, Ref
from .service import Actor, DomainError, canonical, sha

MAX_DOCUMENT_PAGE_BYTES = 75 * 1024


class DocumentRead(Input):
    analysis_set_id: Ref | None = None
    release_id: Ref | None = None
    document_id: Ref
    revision_id: Ref
    section_revision_ids: list[Ref] | None = Field(default=None, min_length=1, max_length=32)
    max_chars: int | None = Field(default=None, ge=100, le=30000)
    offset: int = Field(default=0, ge=0)


class DocumentSlice(BaseModel):
    section_revision_id: str
    name: str
    content: str
    start_offset: int
    end_offset: int
    locator: DocumentSectionLocator | None = None
    studio_url: str | None = None


class DocumentPage(BaseModel):
    document_id: str
    entity_revision_id: str
    row_version: int
    sections: list[DocumentSlice]
    offset: int
    next_offset: int | None
    truncated: bool


class LegacyDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    document_id: str
    entity_revision_id: str
    content_sha256: str
    markdown: str
    sections: list[dict]
    row_version: int


class ContextBinding(InteractiveBindingOutput):
    locator: PrototypeBindingLocator | None = None
    studio_url: str | None = None


class ToolContext(BaseModel):
    catalog: dict
    binding: ContextBinding | None
    related: dict


class DocumentWrite(Input):
    analysis_set_id: Ref
    document_id: Ref
    base_revision_id: Ref
    row_version: int = Field(ge=1)
    markdown: str = Field(max_length=1_000_000)


class WorkspaceExport(Input):
    analysis_set_id: Ref | None = None
    release_id: Ref | None = None
    format: Literal["markdown_bundle", "pdf", "vksbundle", "readonly_html"] = "readonly_html"
    include_source: bool = False
    document_ids: list[Ref] = Field(default_factory=list, max_length=20)
    document_mode: Literal["project", "original"] = "project"

    @model_validator(mode="after")
    def baseline(self):
        if bool(self.analysis_set_id) == bool(self.release_id):
            raise ValueError("请选择一个草稿或保存版本")
        return self


class ProjectTools:
    def __init__(self, service, assistant=None):
        self.service = service
        self.assistant = assistant or InteractiveAssistant(service)
        self.links = self.assistant.links

    def context(self, actor, pid, form):
        result = self.links.catalog(actor, pid, form.analysis_set_id, form.release_id)
        binding = self.assistant._binding(actor, pid, form)
        if form.release_id and binding and not binding["excluded"]:
            package = next((p for p in result["packages"] if p["id"] == binding["package_id"]), None)
            if package:
                binding = {
                    **binding,
                    **binding_reference(self.service.studio_base_url, pid, form.release_id, package, binding),
                }
        return {
            "catalog": result,
            "binding": binding,
            "related": self.assistant.related(actor, pid, form),
        }

    def document(self, actor, pid, form):
        from .prototype_read import missing, selected_id

        with self.links.reader.reading.db() as s:
            _, release, choices, _ = self.links.reader.scope(s, actor, pid, form.analysis_set_id, form.release_id)
            entity, document = selected_id(s, self.service, actor, pid, choices, form.revision_id, "document")
            if entity.id != form.document_id:
                raise missing()
            chapters = self.links.documents._sections(s, entity, document)
            visible = [
                row
                for row in chapters
                if choices.get(row.entity_id) == row.id and self.service._visible_revision(s, actor, pid, row)
            ]
            if form.max_chars is None and form.section_revision_ids is None and not form.offset:
                legacy = self.links.documents._dto(s, entity, document, sections=visible)
                if len(visible) != len(chapters):
                    legacy.update(
                        markdown="\n\n".join(row.content for row in visible), identity_ambiguities=[], partial=True
                    )
                return legacy
            if form.section_revision_ids is not None:
                requested = set(form.section_revision_ids)
                if len(requested) != len(form.section_revision_ids):
                    raise DomainError("VKS_INVALID_INPUT", "章节修订不能重复")
                visible = [row for row in visible if row.id in requested]
                if len(visible) != len(requested):
                    raise missing()
            return self._page_document(pid, release, document, visible, form.offset, form.max_chars or 8000)

    def _page_document(self, pid, release, document, sections, offset, budget):
        total = sum(len(row.content) for row in sections)
        if offset > total:
            raise DomainError("VKS_INVALID_INPUT", "续读偏移超出当前可读原文")
        end = min(offset + budget, total)
        # MCP includes the JSON twice and escapes one copy as text. Reserve
        # space for that encoding and for page fields outside the sections.
        remaining = MAX_DOCUMENT_PAGE_BYTES - 1024
        parts, position, page_end = [], 0, offset
        for row in sections:
            if position >= end:
                break
            section_start = position
            start_offset = max(0, offset - position)
            end_offset = min(len(row.content), end - position)
            position += len(row.content)
            if start_offset >= end_offset:
                continue
            location = (
                section_reference(self.service.studio_base_url, pid, release.id, document, row) if release else {}
            )
            part = DocumentSlice(
                section_revision_id=row.id,
                name=row.name[:1000],
                content="",
                start_offset=start_offset,
                end_offset=end_offset,
                **location,
            )
            overhead = len(part.model_dump_json().encode()) + 1
            content_budget = remaining - overhead
            content = row.content[start_offset:end_offset]
            content_bytes = len(canonical(content).encode()) - 2
            if content_bytes > content_budget:
                if parts:
                    break
                # A single section can exhaust the byte budget before the
                # character ceiling. Find its longest fitting Unicode prefix.
                low, high = 0, len(content)
                while low + 1 < high:
                    middle = (low + high) // 2
                    if len(canonical(content[:middle]).encode()) - 2 <= content_budget:
                        low = middle
                    else:
                        high = middle
                if not low:
                    raise DomainError("VKS_BUDGET_EXCEEDED", "章节定位信息超过原文读取预算")
                content = content[:low]
                content_bytes = len(canonical(content).encode()) - 2
                part.end_offset = start_offset + low
            part.content = content
            parts.append(part)
            page_end = section_start + part.end_offset
            remaining -= overhead + content_bytes
            if part.end_offset < end_offset:
                break
        return DocumentPage(
            document_id=document.entity_id,
            entity_revision_id=document.id,
            row_version=document.metadata_json.get("row_version", 1),
            sections=parts,
            offset=offset,
            next_offset=page_end if page_end < total else None,
            truncated=page_end < total,
        ).model_dump(mode="json")

    def save_document(self, actor, pid, form, key, *, validate=None, generation=None):
        from .documents import DocumentService
        from .models import Entity, Revision

        service = self.service
        with service.db() as s:
            service.require(s, actor, pid, "knowledge:edit")
            entity = s.get(Entity, form.document_id)
            if not entity or entity.project_id != pid or entity.type != "document":
                raise DomainError("VKS_NOT_FOUND", "未找到文档", 404)

            def write():
                _document, _, _, current = DocumentService(service)._selected_document(
                    s, actor, form.document_id, form.analysis_set_id
                )
                locks = current.metadata_json.get("locked_section_ids", [])
                return DocumentService(service).save_draft(
                    actor,
                    form.document_id,
                    {
                        "analysis_set_id": form.analysis_set_id,
                        "base_revision_id": form.base_revision_id,
                        "markdown": form.markdown,
                        "locked_section_ids": locks,
                    },
                    form.row_version,
                    session=s,
                    validate=validate,
                    generation=generation,
                )

            result = service.idem(s, actor, pid, "workspace.document.save", key, form.model_dump(), write)
            revision = s.get(Revision, result["entity_revision_id"])
            if not revision or not service._visible_revision(s, actor, pid, revision):
                raise DomainError("VKS_NOT_FOUND", "保存结果的当前来源不可访问", 404)
            return result

    def document_snapshot(self, actor, pid, form, key):
        """Pin the baseline; the export projection filters exact document IDs."""
        from .documents import DocumentService
        from .materials import MaterialService
        from .models import Entity, Revision

        with self.service.db() as s:
            analysis, release, choices, _ = self.links.reader.scope(
                s, actor, pid, form.analysis_set_id, form.release_id
            )
            document_ids = set(form.document_ids)
            for eid in document_ids:
                row = s.get(Revision, choices.get(eid)) if choices.get(eid) else None
                entity = s.get(Entity, eid)
                if (
                    not row
                    or not entity
                    or entity.type != "document"
                    or not self.service._visible_revision(s, actor, pid, row)
                ):
                    raise DomainError("VKS_NOT_FOUND", "所选版本中没有可导出的文档", 404)
                for section in DocumentService(self.service)._sections(s, entity, row, analysis.id):
                    if not self.service._visible_revision(s, actor, pid, section):
                        raise DomainError("VKS_NOT_FOUND", "文档章节不可访问", 404)
            if release:
                return release.id
            aid = analysis.id
        return MaterialService(self.service).checkpoint(actor, pid, aid, "指定文档导出", key)["id"]

    def export(self, actor, pid, form, key):
        from .materials import MaterialService

        if form.document_ids and form.format == "vksbundle":
            raise DomainError("VKS_INVALID_INPUT", "指定文档导出支持 Markdown、PDF 或 HTML；可回导资料包请导出整个项目")
        # Stable derivation fits the existing checkpoint's key length limits.
        operation_key = "chat-export." + sha(key)[:48]
        with self.service.db() as s:
            self.service.require(s, actor, pid, "knowledge:export")
            # Validate key reuse before creating any clone/checkpoint.
            self.service.idem(
                s, actor, pid, "workspace.export.request", key, form.model_dump(), lambda: {"accepted": True}
            )
        with self.service.db() as s:
            self.service.require(s, actor, pid, "knowledge:export")
        release = form.release_id
        if form.document_ids:
            release = self.document_snapshot(actor, pid, form, operation_key)
        elif not release:
            release = MaterialService(self.service).checkpoint(
                actor, pid, form.analysis_set_id, "会话 / MCP 导出", operation_key
            )["id"]
        return self.service.export(
            actor,
            pid,
            {
                "release_id": release,
                "format": form.format,
                "include_source": form.include_source,
                "document_mode": form.document_mode,
                **({"document_ids": list(form.document_ids)} if form.document_ids else {}),
                **({"documents_only": True} if form.document_ids else {}),
            },
            operation_key,
        )


def install_project_tools(app, service, actor, envelope, assistant):
    tools = ProjectTools(service, assistant)
    prefix = "/api/v3/projects/{pid}/interactive/tools"
    ActorDep = Annotated[Actor, Depends(actor)]

    @app.post(prefix + "/context", response_model=PrototypeEnvelope[ToolContext])
    def context(pid: str, payload: AssistanceInput, request: Request, a: ActorDep):
        return envelope(request, tools.context(a, pid, payload))

    @app.post(prefix + "/document", response_model=PrototypeEnvelope[DocumentPage | LegacyDocument])
    def document(pid: str, payload: DocumentRead, request: Request, a: ActorDep):
        return envelope(request, tools.document(a, pid, payload))

    @app.post(prefix + "/document/save", response_model=PrototypeEnvelope[dict])
    def save_document(
        pid: str,
        payload: DocumentWrite,
        request: Request,
        a: ActorDep,
        idempotency_key: str = Header(..., min_length=1, max_length=128),
    ):
        return envelope(request, tools.save_document(a, pid, payload, idempotency_key))

    @app.post(prefix + "/export", response_model=PrototypeEnvelope[dict], status_code=202)
    def export(
        pid: str,
        payload: WorkspaceExport,
        request: Request,
        a: ActorDep,
        idempotency_key: str = Header(..., min_length=1, max_length=128),
    ):
        return envelope(request, tools.export(a, pid, payload, idempotency_key))
