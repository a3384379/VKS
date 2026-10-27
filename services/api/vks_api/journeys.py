"""V2 journeys on the existing immutable entity/revision/release model."""

from .v2_contracts import Envelope, JourneyOutput, JourneyValidation, JourneyDecision

from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from sqlalchemy import select

from packages.journey_dsl import Journey, validate_journey
from .models import Analysis, Artifact, Audit, Entity, Release, Revision, Selection, uid
from .reading import ReadingService, artifact_policy
from .service import DomainError, canonical, sha


class JourneyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str
    dsl: Journey


class JourneyReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str
    revision_id: str
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=2000)


class JourneyService:
    def __init__(self, service):
        self.service = service

    def _analysis(self, s, actor, pid, aid):
        self.service.require(s, actor, pid, "knowledge:edit")
        analysis = s.get(Analysis, aid)
        if not analysis or analysis.project_id != pid:
            raise DomainError("VKS_NOT_FOUND", "未找到草稿", 404)
        if analysis.state == "frozen":
            raise DomainError("VKS_STATE_CONFLICT", "发布版不可修改，请克隆草稿", 409)
        return analysis

    def _validate(self, s, actor, analysis, dsl):
        try:
            dsl = validate_journey(dsl)
        except ValueError as error:
            raise DomainError("VKS_INVALID_INPUT", str(error)) from error
        revisions = {row.entity_id: row for row in self.service._revisions(s, analysis.id)}
        references = []
        for node in dsl["nodes"]:
            binding = node.get("binding")
            if not binding:
                continue
            row = revisions.get(binding["entity_id"])
            if (
                not row
                or row.id != binding["revision_id"]
                or actor is not None
                and not self.service._visible_revision(s, actor, analysis.project_id, row)
            ):
                raise DomainError("VKS_NOT_FOUND", "流程引用不在当前可见版本", 404)
            kind = s.get(Entity, row.entity_id).type
            allowed = {
                "page": {"page"},
                "action": {"action"},
                "reference": {"document", "section", "api", "symbol", "sql", "table", "scenario"},
            }
            if kind not in allowed[node["kind"]]:
                raise DomainError("VKS_INVALID_INPUT", "流程节点与引用类型不一致")
            references.append(row)
        from .journey_replay import replay_references

        replay_revisions, _ = replay_references(self.service, s, actor, analysis, dsl)
        return dsl, list({row.id: row for row in [*references, *replay_revisions]}.values())

    def _dto(self, s, row):
        return dict(
            journey_id=row.entity_id,
            revision_id=row.id,
            content_sha256=row.content_sha256,
            created_at=row.created_at,
            dsl=row.metadata_json["journey_dsl"],
        )

    def _save(self, s, actor, analysis, entity, dsl, references):
        data = canonical(dsl)
        raw = data.encode()
        policies = []
        from .journey_replay import replay_references

        _, capture_artifacts = replay_references(self.service, s, actor, analysis, dsl)
        for row in references:
            art = s.get(Artifact, row.artifact_id) if row.artifact_id else None
            policy = artifact_policy(s, art) if art else {}
            policies.append(
                (
                    s.get(Entity, row.entity_id).type in ("symbol", "sql", "table")
                    or art is not None
                    and (art.kind == "source" or art.sensitivity == "confidential")
                    or policy.get("requires_source_read", False),
                    art is not None and art.kind in ("trace", "capture") or policy.get("requires_trace_read", False),
                )
            )
        art = Artifact(
            id=uid(),
            project_id=analysis.project_id,
            snapshot_id=uid(),
            relative_path="journeys/" + uid() + ".json",
            sha256=sha(raw),
            size_bytes=len(raw),
            kind="knowledge",
            media_type="application/json",
            object_key=self.service._put_object(raw),
            metadata_json={
                "journey_binding_revisions": [row.id for row in references],
                "journey_capture_artifacts": [art.id for art in capture_artifacts],
                "requires_source_read": any(p[0] for p in policies)
                or any(art.sensitivity == "confidential" for art in capture_artifacts),
                "requires_trace_read": any(p[1] for p in policies) or bool(capture_artifacts),
            },
        )
        s.add(art)
        s.flush()
        metadata = {"journey_dsl": dsl, "evidence_origin": "declared", "review_status": "candidate"}
        digest = sha(
            canonical(
                dict(
                    content=data,
                    metadata=metadata,
                    artifact_id=art.id,
                    locator={},
                    schema_version="1.0",
                    name=dsl["title"],
                )
            )
        )
        row = Revision(
            id=uid(),
            entity_id=entity.id,
            project_id=analysis.project_id,
            artifact_id=art.id,
            name=dsl["title"],
            content=data,
            content_sha256=digest,
            semantic_content_sha256=sha(data),
            metadata_json=metadata,
        )
        s.add(row)
        s.flush()
        choice = s.get(Selection, (analysis.id, entity.id))
        if choice:
            choice.entity_revision_id = row.id
        else:
            s.add(Selection(analysis_set_id=analysis.id, entity_id=entity.id, entity_revision_id=row.id))
        analysis.selection_version += 1
        self.service.audit(s, actor, analysis.project_id, "journey.save", row.id)
        return self._dto(s, row)

    def create(self, actor, pid, payload, key):
        with self.service.db() as s:
            analysis = self._analysis(s, actor, pid, payload["analysis_set_id"])

            def save():
                dsl, refs = self._validate(s, actor, analysis, payload["dsl"])
                entity = s.scalar(
                    select(Entity).where(
                        Entity.project_id == pid, Entity.type == "journey", Entity.stable_key == dsl["key"]
                    )
                )
                if entity and s.get(Selection, (analysis.id, entity.id)):
                    raise DomainError("VKS_STATE_CONFLICT", "流程已存在，请编辑当前修订", 409)
                if not entity:
                    entity = Entity(id=uid(), project_id=pid, type="journey", stable_key=dsl["key"])
                    s.add(entity)
                    s.flush()
                return self._save(s, actor, analysis, entity, dsl, refs)

            return self.service.idem(s, actor, pid, "journey.create", key, payload, save)

    def update(self, actor, jid, payload, version, key):
        with self.service.db() as s:
            entity = s.get(Entity, jid)
            if not entity or entity.type != "journey":
                raise DomainError("VKS_NOT_FOUND", "未找到流程", 404)
            analysis = self._analysis(s, actor, entity.project_id, payload["analysis_set_id"])

            def save():
                choice = s.get(Selection, (analysis.id, jid))
                if not choice:
                    raise DomainError("VKS_NOT_FOUND", "流程不在当前版本", 404)
                previous = s.get(Revision, choice.entity_revision_id)
                if not self.service._visible_revision(s, actor, entity.project_id, previous):
                    raise DomainError("VKS_NOT_FOUND", "未找到流程", 404)
                self.service._version(choice.entity_revision_id, version)
                dsl, refs = self._validate(s, actor, analysis, payload["dsl"])
                if dsl["key"] != entity.stable_key:
                    raise DomainError("VKS_INVALID_INPUT", "流程逻辑键不可改变")
                return self._save(s, actor, analysis, entity, dsl, refs)

            return self.service.idem(
                s, actor, entity.project_id, "journey.update", key, dict(payload, base_revision_id=version), save
            )

    def list(self, actor, pid, analysis_set_id=None, release_id=None):
        reading = ReadingService(self.service)
        with reading.db() as s:
            analysis, release = reading.baseline(s, actor, pid, analysis_set_id, release_id)
            choices, _ = reading.selections(s, analysis, release)
            visible = reading.visible_ids(s, actor, pid, choices)
            return [
                self._dto(s, row)
                for row in reading.revision_rows(s, visible.values())
                if s.get(Entity, row.entity_id).type == "journey"
            ]

    def clone_for_analysis(self, s, actor, target, changed):
        from copy import deepcopy

        replacements = {}
        for row in list(self.service._revisions(s, target.id)):
            entity = s.get(Entity, row.entity_id)
            if entity.type != "journey":
                continue
            original = row.metadata_json.get("journey_dsl", {})
            dsl = deepcopy(original)
            for node in dsl.get("nodes", []):
                binding = node.get("binding")
                if binding and binding["revision_id"] in changed:
                    binding["revision_id"] = changed[binding["revision_id"]]
                for field in ("state_ref", "capture_ref"):
                    ref = node.get(field)
                    if ref and ref["scenario_revision_id"] in changed:
                        ref["scenario_revision_id"] = changed[ref["scenario_revision_id"]]
            if dsl != original:
                dsl, refs = self._validate(s, actor, target, dsl)
                saved = self._save(s, actor, target, entity, dsl, refs)
                replacements[row.id] = saved["revision_id"]
        if replacements:
            from .models import Relation

            for relation in s.scalars(select(Relation).where(Relation.analysis_set_id == target.id)):
                if relation.from_revision_id in replacements or relation.to_revision_id in replacements:
                    relation.from_revision_id = replacements.get(relation.from_revision_id, relation.from_revision_id)
                    relation.to_revision_id = replacements.get(relation.to_revision_id, relation.to_revision_id)
                    relation.validity_status = "stale"
                    relation.row_version += 1
        return replacements

    def get(self, actor, pid, jid, rid):
        with self.service.db() as s:
            self.service.require(s, actor, pid)
            row = s.get(Revision, rid)
            if (
                not row
                or row.project_id != pid
                or row.entity_id != jid
                or s.get(Entity, jid).type != "journey"
                or not self.service._visible_revision(s, actor, pid, row)
            ):
                raise DomainError("VKS_NOT_FOUND", "未找到流程修订", 404)
            if not self.service._can(s, actor, pid, "knowledge:edit") and not any(
                any(item["entity_revision_id"] == rid for item in release.manifest_json["selections"])
                for release in s.scalars(select(Release).where(Release.project_id == pid))
            ):
                raise DomainError("VKS_NOT_FOUND", "未找到流程修订", 404)
            return self._dto(s, row)

    def review(self, actor, jid, payload, version, key):
        with self.service.db() as s:
            entity = s.get(Entity, jid)
            if not entity or entity.type != "journey":
                raise DomainError("VKS_NOT_FOUND", "未找到流程", 404)
            analysis = self._analysis(s, actor, entity.project_id, payload["analysis_set_id"])
            self.service.require(s, actor, entity.project_id, "links:review")
            if not actor.human:
                raise DomainError("VKS_FORBIDDEN", "流程审核需要人员身份", 403)

            def decide():
                choice = s.get(Selection, (analysis.id, jid))
                if not choice:
                    raise DomainError("VKS_NOT_FOUND", "未找到流程", 404)
                self.service._version(choice.entity_revision_id, version)
                if payload["revision_id"] != choice.entity_revision_id:
                    raise DomainError("VKS_VERSION_CONFLICT", "审核目标已变化", 412)
                row = s.get(Revision, choice.entity_revision_id)
                self._validate(s, actor, analysis, row.metadata_json["journey_dsl"])
                self.service.audit(
                    s,
                    actor,
                    entity.project_id,
                    "journey." + payload["decision"],
                    row.id,
                    metadata={
                        "analysis_set_id": analysis.id,
                        "sha256": row.content_sha256,
                        "reason": payload["reason"],
                    },
                )
                analysis.selection_version += 1
                return {"revision_id": row.id, "decision": payload["decision"]}

            return self.service.idem(
                s, actor, entity.project_id, "journey.review", key, dict(payload, version=version), decide
            )

    def assert_publishable(self, s, analysis):
        blockers = []
        for row in self.service._revisions(s, analysis.id):
            if s.get(Entity, row.entity_id).type != "journey":
                continue
            try:
                self._validate(s, None, analysis, row.metadata_json["journey_dsl"])
                latest = s.scalar(
                    select(Audit)
                    .where(
                        Audit.project_id == analysis.project_id,
                        Audit.resource_id == row.id,
                        Audit.action.in_(["journey.approve", "journey.reject"]),
                    )
                    .order_by(Audit.created_at.desc(), Audit.id.desc())
                )
                if (
                    not latest
                    or latest.action != "journey.approve"
                    or latest.metadata_json.get("sha256") != row.content_sha256
                ):
                    blockers.append("流程修订尚未人工审核：" + row.entity_id)
            except (DomainError, KeyError):
                blockers.append("流程引用版本需要复核：" + row.entity_id)
        return blockers


def install_journeys(app, service, actor, envelope):
    journeys = JourneyService(service)
    api = "/api/v2"

    @app.get(
        api + "/projects/{pid}/journeys", response_model=Envelope[list[JourneyOutput]], response_model_exclude_none=True
    )
    def listing(
        pid: str, request: Request, analysis_set_id: str | None = None, release_id: str | None = None, a=Depends(actor)
    ):
        return envelope(request, journeys.list(a, pid, analysis_set_id, release_id))

    @app.post(
        api + "/projects/{pid}/journeys",
        status_code=201,
        response_model=Envelope[JourneyOutput],
        response_model_exclude_none=True,
    )
    def create(pid: str, payload: JourneyInput, request: Request, a=Depends(actor), idempotency_key: str = Header(...)):
        return envelope(request, journeys.create(a, pid, payload.model_dump(exclude_none=True), idempotency_key))

    @app.post(
        api + "/projects/{pid}/journeys/validate",
        response_model=Envelope[JourneyValidation],
        response_model_exclude_none=True,
    )
    def validate(pid: str, payload: JourneyInput, request: Request, a=Depends(actor)):
        with service.db() as s:
            analysis = journeys._analysis(s, a, pid, payload.analysis_set_id)
            dsl, _ = journeys._validate(s, a, analysis, payload.dsl.model_dump(exclude_none=True))
            return envelope(request, {"dsl": dsl, "sha256": sha(canonical(dsl)), "requires_review": True})

    @app.post(
        api + "/journeys/{jid}/revisions", response_model=Envelope[JourneyOutput], response_model_exclude_none=True
    )
    def update(
        jid: str,
        payload: JourneyInput,
        request: Request,
        a=Depends(actor),
        if_match: str = Header(...),
        idempotency_key: str = Header(...),
    ):
        return envelope(
            request,
            journeys.update(a, jid, payload.model_dump(exclude_none=True), if_match.strip('"'), idempotency_key),
        )

    @app.get(
        api + "/journeys/{jid}/revisions/{rid}",
        response_model=Envelope[JourneyOutput],
        response_model_exclude_none=True,
    )
    def revision(jid: str, rid: str, project_id: str, request: Request, a=Depends(actor)):
        return envelope(request, journeys.get(a, project_id, jid, rid))

    @app.post(api + "/journeys/{jid}/review", response_model=Envelope[JourneyDecision])
    def review(
        jid: str,
        payload: JourneyReviewInput,
        request: Request,
        a=Depends(actor),
        if_match: str = Header(...),
        idempotency_key: str = Header(...),
    ):
        return envelope(request, journeys.review(a, jid, payload.model_dump(), if_match.strip('"'), idempotency_key))
