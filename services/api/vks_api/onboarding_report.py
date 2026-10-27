"""Permission-filtered onboarding denominators and concrete follow-up actions."""

from fastapi import Depends, Request
from .v2_contracts import Envelope
from .service import canonical, sha


def report(service, actor, pid, aid=None, rid=None):
    w = service.workspace(actor, pid, aid, rid)
    trusted = [r for r in w["relations"] if r["review_status"] == "confirmed" and r["validity_status"] == "valid"]
    linked = {rid for row in trusted for rid in (row["from_revision_id"], row["to_revision_id"])}
    actions = {row["entity_revision_id"] for row in w["actions"]}
    unanchored = [row for row in w["actions"] if not row.get("artifact_id") or not row.get("locator")]
    candidates = [row for row in w["relations"] if row["review_status"] == "candidate"]
    needs_review = [row for row in w["relations"] if row["validity_status"] != "valid"]
    kinds = {row["type"] for row in w["entities"]}
    gaps = []

    def gap(code, count, denominator, message, action):
        if count:
            gaps.append(dict(code=code, count=count, denominator=denominator, message=message, next_action=action))

    gap("NO_MATERIAL", int(not w["sources"]), 1, "尚无当前可见资料，上传并安全预检后重试。", "upload_material")
    gap("UNREVIEWED_RELATIONS", len(candidates), len(w["relations"]), "候选尚未计入已确认覆盖。", "review_candidates")
    gap(
        "STALE_RELATIONS",
        len(needs_review),
        len(w["relations"]),
        "变更后的失效关联需要核对准确修订。",
        "review_candidates",
    )
    gap(
        "ACTION_EVIDENCE_GAP",
        len(actions - linked),
        len(actions),
        "动作尚无已确认且有效的关联。",
        "create_knowledge_task",
    )
    gap("UNANCHORED_ACTION", len(unanchored), len(actions), "动作缺少准确来源锚点。", "upload_material")
    gap(
        "NO_SOURCE_SYMBOLS",
        int("symbol" not in kinds),
        1,
        "当前范围没有源码符号，源码链路能力受限。",
        "upload_material",
    )
    jobs = [
        dict(id=row["id"], kind=row["kind"], state=row["state"], retry_allowed=row["state"] in ("failed", "cancelled"))
        for row in w["jobs"][:20]
    ]
    result = dict(
        project_id=pid,
        analysis_set_id=(w["selected_analysis_set"] or {}).get("id"),
        release_id=(w["selected_release"] or {}).get("id"),
        source_vector=w["sources"],
        scope="当前身份可见固定基线；未解析和未知不代表零，候选不计作人工确认",
        counts=dict(
            pages=len(w["pages"]),
            actions=len(actions),
            scenarios=len(w["scenarios"]),
            documents=len(w["documents"]),
            relations=len(w["relations"]),
            candidate_relations=len(candidates),
            confirmed_valid_relations=len(trusted),
            actions_with_confirmed_valid_links=len(actions & linked),
            action_denominator=len(actions),
        ),
        gaps=gaps,
        jobs=jobs,
        job_scope="最近可见任务，最多 20 项；详细解析问题在对应任务内按权限查看",
        unknowns=w["unknowns"],
        candidate_only=True,
    )
    result["sha256"] = sha(canonical(result))
    return result


def install(app, service, actor, envelope):
    @app.get("/api/v2/projects/{pid}/onboarding/report", response_model=Envelope[dict])
    def get(
        pid: str, request: Request, analysis_set_id: str | None = None, release_id: str | None = None, a=Depends(actor)
    ):
        return envelope(request, report(service, a, pid, analysis_set_id, release_id))
