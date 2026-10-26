"""Repeat bounded source analysis against the draft's actual fixed snapshots."""

from sqlalchemy import select
from .models import Analysis, Artifact
from .knowledge import SourceSnapshot, persist_bundle
from packages.analyzers.java_spring import analyze_sources
from packages.analyzers.vks_ingest import ParsedBundle


def prepare(service, job_id):
    from .models import Job
    from .jobs import current_actor
    from .service import DomainError

    with service.db() as s:
        job = s.get(Job, job_id)
        actor = current_actor(service, job.created_by, job.project_id, s, job=job)
        service.require(s, actor, job.project_id, "source:read")
        analysis = s.get(Analysis, job.input_json["analysis_set_id"])
        if not analysis or analysis.state == "frozen":
            raise DomainError("VKS_STATE_CONFLICT", "已发布基线不可重新索引", 409)
        profiles = {"java-spring-mybatis-v1", "frontend-web-v1", "explicit-binding-v1", None}
        if job.input_json.get("analyzer_profile") not in profiles:
            raise DomainError("VKS_INVALID_INPUT", "不支持此分析器配置")
        from .project_profile import ProjectProfile

        profile = s.get(ProjectProfile, job.project_id)
        selected_profile = job.input_json.get("analyzer_profile") or (
            "explicit-binding-v1"
            if profile and profile.default_analysis_language == "source_anchors"
            else "java-spring-mybatis-v1"
        )
        selected_version = analysis.selection_version
        latest = {}
        for source in analysis.sources:
            latest[source.get("source_id", source["source_snapshot_id"])] = source["source_snapshot_id"]
        artifacts = []
        sources = []
        paths = set()
        for snapshot_id in latest.values():
            snapshot = s.get(SourceSnapshot, snapshot_id)
            if not snapshot:
                continue
            rows = list(
                s.scalars(select(Artifact).where(Artifact.snapshot_id == snapshot.id, Artifact.kind == "source"))
            )
            if not rows:
                continue
            sources.append(
                {
                    "source_id": snapshot.source_key,
                    "provider": snapshot.provider,
                    "revision": snapshot.provider_revision,
                    "content_sha256": snapshot.content_sha256,
                    "role": snapshot.role,
                    "service_key": snapshot.service_key,
                }
            )
            for a in rows:
                if a.relative_path in paths:
                    raise DomainError("VKS_INVALID_INPUT", "固定来源存在同路径歧义；请加仓库目录前缀")
                paths.add(a.relative_path)
                artifacts.append(
                    {
                        "path": a.relative_path,
                        "source_id": snapshot.source_key,
                        "kind": a.kind,
                        "sha256": a.sha256,
                        "media_type": a.media_type,
                        "sensitivity": a.sensitivity,
                        "detected_encoding": a.metadata_json.get("encoding", "utf-8"),
                        "content": (service.data_dir / "objects" / a.object_key).read_bytes(),
                    }
                )
        metadata = {
            "schema_version": "1.0",
            "baseline_key": analysis.key,
            "import_key": "reindex-" + job_id,
            "allow_platform_llm": False,
            "_preserve_material_policy": True,
        }
    if selected_profile == "explicit-binding-v1":
        from packages.analyzers.java_spring import AnalysisResult

        parsed = AnalysisResult()
        parsed.metrics = {"parser_version": "explicit-binding-v1", "source_count": len(artifacts)}
        parsed.issues.append(
            {
                "code": "VKS_EXPLICIT_ANCHORS_PROFILE",
                "severity": "info",
                "message": "按项目默认配置仅保留显式源码锚点，未追加框架静态候选。",
            }
        )
    else:
        parsed = analyze_sources(artifacts, sources)
    bundle = ParsedBundle(
        manifest=metadata,
        sources=sources,
        artifacts=artifacts,
        entities=parsed.entities,
        evidence=parsed.evidence,
        relations=parsed.relations,
        issues=parsed.issues,
        analysis_metrics=parsed.metrics,
    )
    if job.input_json.get("analyzer_profile") == "frontend-web-v1":
        bundle.issues.append(
            {
                "code": "VKS_FRONTEND_EXPLICIT_ANCHORS_ONLY",
                "severity": "info",
                "message": "前端仅支持显式构建/热点源码锚点；没有推测动态React/Vue调用。已有显式绑定和人工审核保持不变。",
            }
        )
    return bundle, selected_version


def finalize(service, s, job, prepared):
    from .service import DomainError

    analysis = s.get(Analysis, job.input_json["analysis_set_id"])
    bundle, version = prepared
    if analysis.selection_version != version:
        raise DomainError("VKS_VERSION_CONFLICT", "索引期间草稿已变化，请重试", 412)
    result = persist_bundle(service, s, job.project_id, job.created_by, bundle, analysis.id)
    result["analysis_metrics"] = bundle.analysis_metrics
    result["message"] = "已重新解析固定源码；现有人工确认、拒绝与锁定保持不变。"
    return result
