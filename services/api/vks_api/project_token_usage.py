"""Project ledger totals, by usage period or visible prototype page."""

import json
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .ai_gateway import ModelUsage
from .exports import allow_artifact, read_original
from .models import Artifact, Audit, Idempotency, Relation, Revision
from .prototype_edit import Input
from .service import DomainError


class TokenCounts(Input):
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, inputs, outputs):
        self.input_tokens += inputs
        self.output_tokens += outputs


class FunctionTokenUsage(TokenCounts):
    package_id: str
    route: str
    page_title: str


class ProjectTokenUsage(Input):
    model: TokenCounts
    embedding_tokens: int
    unattributed: TokenCounts
    functions: list[FunctionTokenUsage]


class TokenPeriodUsage(Input):
    period: Literal["day", "month"]
    start_date: str
    total_tokens: int
    answer_tokens: int
    association_tokens: int
    embedding_tokens: int
    other_tokens: int
    unclassified_tokens: int


def project_period_usage(assistant, actor, pid, period, selected_date=None):
    zone = ZoneInfo("Asia/Shanghai")
    start = selected_date or datetime.now(zone).date()
    try:
        if period == "month":
            start = start.replace(day=1)
            end = (start + timedelta(days=32)).replace(day=1)
        else:
            end = start + timedelta(days=1)
        utc_start, utc_end = (
            datetime.combine(day, time.min, zone).astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            for day in (start, end)
        )
    except (OverflowError, ValueError) as exc:
        raise DomainError("VKS_INVALID_INPUT", "日期超出可查询范围", 422) from exc
    with assistant.links.reader.reading.db() as s:
        assistant.service.require(s, actor, pid, "project:manage")
        scope = (
            ModelUsage.project_id == pid,
            ModelUsage.created_at >= utc_start,
            ModelUsage.created_at < utc_end,
            ModelUsage.state.in_(("succeeded", "failed")),
        )
        records = s.execute(
            select(ModelUsage.id, ModelUsage.input_tokens + ModelUsage.output_tokens).where(*scope)
        ).all()
        totals = dict.fromkeys(("answer", "association", "other", "unclassified"), 0)
        purposes = dict(s.execute(
            select(ModelUsage.id, Audit.metadata_json["usage_purpose"].as_string())
            .join(Audit, (Audit.resource_id == ModelUsage.id) & (Audit.actor_id == ModelUsage.actor_id))
            .where(*scope, Audit.project_id == pid, Audit.action == "model.reserve",
                   Audit.metadata_json["usage_purpose"].as_string().in_(("answer", "association", "other")))
        ).all())
        if len(purposes) < len(records):
            # Older receipts can prove these were associations even before
            # reservations recorded a purpose. A page hint alone cannot, since
            # questions and explanations also carry a prototype target.
            saved = (
                select(ModelUsage.id)
                .join(Idempotency, ("assistant." + ModelUsage.id == Idempotency.key)
                      & (Idempotency.actor_id == ModelUsage.actor_id))
                .where(*scope, Idempotency.project_id == pid, Idempotency.operation == "interactive.binding.save")
            )
            previews = (
                select(ModelUsage.id)
                .join(Artifact, (Artifact.metadata_json["model_usage_id"].as_string() == ModelUsage.id)
                      & (Artifact.metadata_json["created_by"].as_string() == ModelUsage.actor_id))
                .where(*scope, Artifact.project_id == pid, Artifact.kind == "generated",
                       Artifact.metadata_json["reassociation_preview"].as_boolean().is_(True))
            )
            for usage_id in s.scalars(saved.union(previews)):
                purposes.setdefault(usage_id, "association")
        for usage_id, tokens in records:
            totals[purposes.get(usage_id, "unclassified")] += tokens
    embeddings = assistant.rag.store.used_tokens(pid, days=(start.isoformat(), end.isoformat()))
    return TokenPeriodUsage(
        period=period,
        start_date=start.isoformat(),
        total_tokens=sum(totals.values()) + embeddings,
        embedding_tokens=embeddings,
        **{f"{purpose}_tokens": tokens for purpose, tokens in totals.items()},
    )


def prototype_target(form):
    if not form.package_id:
        return None
    return _target(form.package_id, form.target.model_dump(), form.context.page_title)


def _target(package_id, target, page_title=""):
    return {
        "package_id": package_id,
        "route": target.get("route", ""),
        "page_title": target.get("label", "") if target.get("selector") == "body" else page_title,
    }


def _historical_targets(s, service, actor, pid, missing, visible_packages):
    # A successful direct association stores the exact usage ID in its
    # idempotency receipt. A preview instead keeps it on its immutable artifact.
    saved = s.execute(
        select(ModelUsage.id, Revision.metadata_json)
        .select_from(Idempotency)
        .join(
            ModelUsage,
            ("assistant." + ModelUsage.id == Idempotency.key) & (ModelUsage.actor_id == Idempotency.actor_id),
        )
        .join(Relation, Relation.id == Idempotency.response_json["binding_id"].as_string())
        .join(Revision, Revision.id == Relation.from_revision_id)
        .where(
            Idempotency.project_id == pid,
            ModelUsage.project_id == pid,
            Relation.project_id == pid,
            Revision.project_id == pid,
            Relation.relation_type == "has_business_point",
            Idempotency.operation == "interactive.binding.save",
            Idempotency.key.like("assistant.%"),
        )
    )
    targets = {}
    for usage_id, metadata in saved:
        anchor = metadata.get("interactive_anchor", {})
        if usage_id in missing and anchor.get("package_id") in visible_packages and anchor.get("target"):
            targets[usage_id] = _target(anchor["package_id"], anchor["target"])
    previews = s.scalars(
        select(Artifact)
        .join(ModelUsage, ModelUsage.id == Artifact.metadata_json["model_usage_id"].as_string())
        .where(
            Artifact.project_id == pid,
            Artifact.kind == "generated",
            Artifact.metadata_json["reassociation_preview"].as_boolean().is_(True),
            ModelUsage.project_id == pid,
            ModelUsage.actor_id == Artifact.metadata_json["created_by"].as_string(),
        )
    )
    source_read = service._can(s, actor, pid, "source:read")
    caps = {
        "allowed_source": source_read,
        "allowed_confidential": source_read,
        "allowed_trace": service._can(s, actor, pid, "trace:read"),
    }
    for artifact in previews:
        meta = artifact.metadata_json
        usage_id = meta.get("model_usage_id")
        package_id = meta.get("original_artifact_id")
        refs = meta.get("input_revision_provenance", [])
        if (
            usage_id not in missing
            or package_id not in visible_packages
            or not allow_artifact(artifact, caps, s)
            or not refs
            or any(not service._visible_revision(s, actor, pid, s.get(Revision, ref["revision_id"])) for ref in refs)
        ):
            continue
        try:
            form = json.loads(read_original(service, artifact))["form"]
            if form["package_id"] == package_id:
                targets[usage_id] = _target(package_id, form["target"], form["context"].get("page_title", ""))
        except (DomainError, FileNotFoundError, ValueError, KeyError):
            # Retain settled usage even when a legacy artifact is inaccessible.
            continue
    return targets


def project_token_usage(assistant, actor, pid, aid, release_id):
    service, links = assistant.service, assistant.links
    with links.reader.reading.db() as s:
        service.require(s, actor, pid, "project:manage")
        _, _, choices, _ = links.reader.scope(s, actor, pid, aid, release_id)
        visible_packages = {item["id"] for item in links._packages(s, actor, pid, choices, kind="prototype")}
        records = s.execute(
            select(ModelUsage.id, ModelUsage.input_tokens, ModelUsage.output_tokens)
            .where(ModelUsage.project_id == pid, ModelUsage.state.in_(("succeeded", "failed")))
            .order_by(ModelUsage.created_at, ModelUsage.id)
        ).all()
        targets = {
            uid: meta["prototype_target"]
            for uid, meta in s.execute(
                select(Audit.resource_id, Audit.metadata_json).where(
                    Audit.project_id == pid, Audit.action == "model.reserve"
                )
            )
            if (meta or {}).get("prototype_target")
        }
        missing = {row.id for row in records} - targets.keys()
        if visible_packages and missing:
            targets.update(_historical_targets(s, service, actor, pid, missing, visible_packages))
        model, unattributed, functions = TokenCounts(), TokenCounts(), {}
        for usage_id, inputs, outputs in records:
            model.add(inputs, outputs)
            if not (inputs or outputs):
                continue
            target = targets.get(usage_id, {})
            package_id, route = target.get("package_id"), target.get("route", "")
            if package_id not in visible_packages or not route.startswith("/"):
                unattributed.add(inputs, outputs)
                continue
            key = (package_id, route)
            if key not in functions:
                functions[key] = FunctionTokenUsage(package_id=package_id, route=route, page_title="")
            group = functions[key]
            group.page_title = target.get("page_title") or group.page_title
            group.add(inputs, outputs)
    return ProjectTokenUsage(
        model=model,
        embedding_tokens=assistant.rag.store.used_tokens(pid),
        unattributed=unattributed,
        functions=list(functions.values()),
    )
