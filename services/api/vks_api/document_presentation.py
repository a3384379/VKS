"""Versioned document reader choice, separate from corpus and source bytes."""

from typing import Annotated, Literal

from fastapi import Depends, Header, Request
from pydantic import Field
from sqlalchemy import JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, Release
from .prototype_contracts import PrototypeEnvelope
from .prototype_edit import Input

KEY = "document_presentation"


class DocumentPresentation(Base):
    __tablename__ = "document_presentation"
    analysis_set_id: Mapped[str] = mapped_column(ForeignKey("analysis_set.id"), primary_key=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)


class PresentationSettings(Input):
    mode: Literal["auto", "standard", "handbook", "original"] = "auto"
    template_id: Literal["handbook"] = "handbook"
    template_version: Literal["1.0.0"] = "1.0.0"


class PresentationInput(PresentationSettings):
    analysis_set_id: str = Field(min_length=1, max_length=96)


class PresentationOutput(Input):
    analysis_set_id: str
    selection_version: int
    editable: bool
    settings: PresentationSettings


class PresentationSearchInput(Input):
    analysis_set_id: str | None = None
    release_id: str | None = None
    query: str = Field(min_length=2, max_length=2000)


class PresentationSearchResult(Input):
    document: str
    section: str | None = None
    title: str
    snippet: str


def presentation_for_release(s, release):
    # Immutable snapshots carry their own choice. Legacy releases use auto,
    # without consulting mutable preferences from a newer working copy.
    return PresentationSettings.model_validate(release.manifest_json.get(KEY, {})).model_dump()


def presentation_for_analysis(s, analysis):
    if analysis.state == "frozen":
        from sqlalchemy import select

        release = s.scalar(select(Release).where(Release.analysis_set_id == analysis.id))
        if release:
            return presentation_for_release(s, release)
    row = s.get(DocumentPresentation, analysis.id)
    return PresentationSettings.model_validate(row.settings if row else {}).model_dump()


def clone_presentation(s, source, target):
    from sqlalchemy import select

    row = s.get(DocumentPresentation, source.id)
    release = (
        s.scalar(select(Release).where(Release.analysis_set_id == source.id)) if source.state == "frozen" else None
    )
    if row or release and KEY in release.manifest_json:
        s.add(DocumentPresentation(analysis_set_id=target.id, settings=presentation_for_analysis(s, source)))


class PresentationService:
    def __init__(self, service):
        self.service = service

    def get(self, actor, pid, aid=None, release_id=None):
        from .prototype_read import PrototypeReader

        reader = PrototypeReader(self.service)
        with reader.reading.db() as s:
            analysis, release, _, _ = reader.scope(s, actor, pid, aid, release_id)
            return {
                "analysis_set_id": analysis.id,
                "selection_version": analysis.selection_version,
                "editable": not release
                and analysis.state != "frozen"
                and self.service._can(s, actor, pid, "knowledge:edit"),
                "settings": presentation_for_release(s, release) if release else presentation_for_analysis(s, analysis),
            }

    def update(self, actor, pid, form, expected, key):
        from .materials import MaterialService, version

        with self.service.db() as s:
            analysis = MaterialService(self.service)._draft(s, actor, pid, form.analysis_set_id)

            def apply():
                version(analysis.selection_version, expected)
                settings = PresentationSettings.model_validate(
                    form.model_dump(exclude={"analysis_set_id"})
                ).model_dump()
                row = s.get(DocumentPresentation, analysis.id)
                if not row:
                    row = DocumentPresentation(analysis_set_id=analysis.id, settings=settings)
                    s.add(row)
                elif row.settings == settings:
                    return {
                        "analysis_set_id": analysis.id,
                        "selection_version": analysis.selection_version,
                        "editable": True,
                        "settings": settings,
                    }
                else:
                    row.settings = settings
                analysis.selection_version += 1
                s.flush()
                self.service.audit(s, actor, pid, "document.presentation.update", analysis.id)
                return {
                    "analysis_set_id": analysis.id,
                    "selection_version": analysis.selection_version,
                    "editable": True,
                    "settings": settings,
                }

            return self.service.idem(
                s, actor, pid, "document.presentation", key, {**form.model_dump(), "expected": expected}, apply
            )

    def search(self, actor, pid, form):
        from .interactive import InteractiveKnowledgeService
        from .service import DomainError

        links = InteractiveKnowledgeService(self.service)
        reader = links.reader.reading
        query = form.query.strip().casefold()
        if len(query) < 2:
            raise DomainError("VKS_INVALID_INPUT", "请输入至少两个字符", 422)
        # Match only the current authorized chapter projection. Searching the
        # original document body would disclose removed chapters through a
        # document-level match even when every returned reference was visible.
        with reader.db() as s:
            _analysis, _release, choices, _ = links.reader.scope(s, actor, pid, form.analysis_set_id, form.release_id)
            rows = links._rows(s, actor, pid, choices, types={"document", "section"})
            visible_chapters = {row.id: row for entity, row in rows if entity.type == "section"}
            result = []
            for entity, document in rows:
                if entity.type != "document":
                    continue
                chapters = [
                    visible_chapters[row.id]
                    for row in links.documents._sections(s, entity, document)
                    if row.id in visible_chapters
                ]
                if query in document.name.casefold():
                    result.append(
                        {
                            "document": document.id,
                            "section": None,
                            "title": document.name,
                            "snippet": "\n\n".join(row.content for row in chapters)[:280],
                        }
                    )
                for chapter in chapters:
                    offset = chapter.content.casefold().find(query)
                    if offset < 0 and query not in chapter.name.casefold():
                        continue
                    start = max(0, offset - 70)
                    result.append(
                        {
                            "document": document.id,
                            "section": chapter.id,
                            "title": chapter.name,
                            "snippet": chapter.content[start : start + 280],
                        }
                    )
                    if len(result) >= 50:
                        return result[:50]
            return result


def install_document_presentation(app, service, actor, envelope):
    shared = PresentationService(service)
    ActorDep = Annotated[object, Depends(actor)]
    prefix = "/api/v3/projects/{pid}/document-presentation"

    @app.get(prefix, response_model=PrototypeEnvelope[PresentationOutput])
    def get(pid: str, request: Request, a: ActorDep, analysis_set_id: str | None = None, release_id: str | None = None):
        return envelope(request, shared.get(a, pid, analysis_set_id, release_id))

    @app.put(prefix, response_model=PrototypeEnvelope[PresentationOutput])
    def update(
        pid: str,
        request: Request,
        form: PresentationInput,
        a: ActorDep,
        if_match: str = Header(...),
        idempotency_key: str = Header(..., max_length=96),
    ):
        return envelope(request, shared.update(a, pid, form, if_match, idempotency_key))

    @app.post(prefix + "/search", response_model=PrototypeEnvelope[list[PresentationSearchResult]])
    def search(pid: str, request: Request, form: PresentationSearchInput, a: ActorDep):
        return envelope(request, shared.search(a, pid, form))
