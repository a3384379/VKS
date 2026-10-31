"""Permission-filtered onboarding denominators and concrete follow-up actions."""

from fastapi import Depends, Request
from .v2_contracts import Envelope
from .service import canonical, sha
from .knowledge_coverage import action_knowledge_revisions


def report(service, actor, pid, aid=None, rid=None):
    w = service.workspace(actor, pid, aid, rid)
    trusted = [r for r in w["relations"] if (r["excluded"] is False) and r["validity_status"] == "valid"]
    linked = action_knowledge_revisions(w["entities"], trusted)
    actions = {row["entity_revision_id"] for row in w["actions"]}
    unanchored = [row for row in w["actions"] if not row.get("artifact_id") or not row.get("locator")]
    excluded = [row for row in w["relations"] if row["excluded"]]
    outdated = [row for row in w["relations"] if row["validity_status"] != "valid"]
    kinds = {row["type"] for row in w["entities"]}
    gaps = []

    def gap(code, count, denominator, message, action):
        if count:
            gaps.append(dict(code=code, count=count, denominator=denominator, message=message, next_action=action))

    gap("NO_MATERIAL", int(not w["sources"]), 1, "尚无当前可见资料，上传并格式预检后重试。", "upload_material")
    gap(
        "STALE_RELATIONS",
        len(outdated),
        len(w["relations"]),
        "变更后的失效关联需要核对准确修订。",
        "manage_relations",
    )
    gap(
        "ACTION_EVIDENCE_GAP",
        len(actions - linked),
        len(actions),
        "动作尚无有效的接口、源码、数据或文档知识关联；页面成员关系不计入。",
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
        scope="当前身份可见固定基线；关联知识覆盖排除结构关系，未解析和未知不代表零",
        counts=dict(
            pages=len(w["pages"]),
            actions=len(actions),
            scenarios=len(w["scenarios"]),
            documents=len(w["documents"]),
            relations=len(w["relations"]),
            excluded_relations=len(excluded),
            valid_relations=len(trusted),
            actions_with_valid_links=len(actions & linked),
            action_denominator=len(actions),
        ),
        gaps=gaps,
        jobs=jobs,
        job_scope="最近可见任务，最多 20 项；详细解析问题在对应任务内按权限查看",
        unknowns=w["unknowns"],
        saved_directly=True,
    )
    result["sha256"] = sha(canonical(result))
    return result


def install(app, service, actor, envelope):
    @app.get("/api/v2/projects/{pid}/onboarding/report", response_model=Envelope[dict])
    def get(
        pid: str, request: Request, analysis_set_id: str | None = None, release_id: str | None = None, a=Depends(actor)
    ):
        return envelope(request, report(service, a, pid, analysis_set_id, release_id))
