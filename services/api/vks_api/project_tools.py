"""Project tools shared by Studio conversations and the official MCP gateway."""

from typing import Annotated, Literal

from fastapi import Depends, Header, Request
from pydantic import Field, model_validator

from .interactive_ai import AssistanceInput, InteractiveAssistant
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import Input, Ref
from .service import Actor, DomainError, sha


class DocumentRead(Input):
    analysis_set_id: Ref | None = None
    release_id: Ref | None = None
    document_id: Ref
    revision_id: Ref


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
        return {
            "catalog": result,
            "binding": self.assistant._binding(actor, pid, form),
            "related": self.assistant.related(actor, pid, form),
        }

    def document(self, actor, pid, form):
        with self.service.db() as s:
            _, _, choices, _ = self.links.reader.scope(s, actor, pid, form.analysis_set_id, form.release_id)
            if choices.get(form.document_id) != form.revision_id:
                raise DomainError("VKS_VERSION_CONFLICT", "文档不属于所选版本", 412)
            # Scope filtering occurs before any names, text or counts leave here.
            from .prototype_read import selected_id

            selected_id(s, self.service, actor, pid, choices, form.revision_id, "document")
        return self.service.get_document(actor, form.document_id, form.revision_id)

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

    @app.post(prefix + "/context", response_model=PrototypeEnvelope[dict])
    def context(pid: str, payload: AssistanceInput, request: Request, a: ActorDep):
        return envelope(request, tools.context(a, pid, payload))

    @app.post(prefix + "/document", response_model=PrototypeEnvelope[dict])
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
