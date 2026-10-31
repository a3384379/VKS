"""Compare declared build labels on exact visible source snapshots.

Label agreement is distinct from verified runtime/source alignment. A declaration
never joins API identities, establishes Trace causality or grants new access.
"""

from collections import defaultdict
from typing import Any, Literal
from fastapi import Depends, Request
from pydantic import BaseModel
from .models import Artifact
from .v2_contracts import Envelope


class SourceDiagnostics(BaseModel):
    project_id: str
    analysis_set_id: str | None
    release_id: str | None
    source_vector: list[dict[str, Any]]
    comparisons: list[dict[str, Any]]
    declared_alignment: Literal["matched", "mismatched", "unknown"]
    runtime_alignment: Literal["matched", "mismatched", "unknown"] = "unknown"
    scope: str = "visible_current_selected_materials"
    unknowns: list[str]


def revision(value):
    if value is None:
        return None
    value = str(value)
    if value.isdecimal():
        return "r" + value
    return value.lower() if len(value) in (40, 64) else value


def compare_sources(sources):
    groups = defaultdict(list)
    for row in sources:
        if row.get("active"):
            groups[row.get("service_key") or row["source_id"]].append(row)
    comparisons = []
    for service, rows in sorted(groups.items()):
        for axis in ("code_revision", "build_id"):
            values = []
            for row in rows:
                labels = row.get("version_labels", {})
                value = (
                    row.get("provider_revision")
                    if axis == "code_revision" and row["role"] == "backend" and row["provider"] in ("git", "svn")
                    else labels.get(axis)
                )
                values.append(
                    dict(
                        source_snapshot_id=row["source_snapshot_id"],
                        source_id=row["source_id"],
                        value=revision(value) if axis == "code_revision" else value,
                    )
                )
            known = {item["value"] for item in values if item["value"] is not None}
            status = (
                "mismatched"
                if len(known) > 1
                else "matched"
                if len(values) >= 2 and len(known) == 1 and all(item["value"] for item in values)
                else "unknown"
            )
            comparisons.append(
                dict(service_key=service, axis=axis, status=status, basis="declared_labels_only", values=values)
            )
    statuses = {row["status"] for row in comparisons}
    overall = "mismatched" if "mismatched" in statuses else "matched" if statuses == {"matched"} else "unknown"
    return comparisons, overall


def diagnostics(service, actor, pid, aid=None, rid=None):
    # workspace applies current permissions before even returning names or counts.
    w = service.workspace(actor, pid, aid, rid)
    active = set()
    with service.db() as s:
        if w["selected_analysis_set"]:
            for row in service._revisions(s, w["selected_analysis_set"]["id"]):
                if not service._visible_revision(s, actor, pid, row):
                    continue
                for artid in (row.artifact_id, row.metadata_json.get("original_artifact_id")):
                    art = s.get(Artifact, artid) if artid else None
                    if art:
                        active.add(art.snapshot_id)
        vector = [dict(row, active=row["source_snapshot_id"] in active) for row in w["sources"]]
    comparisons, status = compare_sources(vector)
    return dict(
        project_id=pid,
        analysis_set_id=(w["selected_analysis_set"] or {}).get("id"),
        release_id=(w["selected_release"] or {}).get("id"),
        source_vector=vector,
        comparisons=comparisons,
        declared_alignment=status,
        runtime_alignment="unknown",
        scope="visible_current_selected_materials",
        unknowns=[
            "来源标签由材料声明；一致标签不证明实际部署或运行因果",
            "缺少对应代码修订、构建或运行部署标识时保留 unknown，不按接口同名合并",
        ],
    )


def install_source_alignment(app, service, actor, envelope):
    @app.get("/api/v2/projects/{pid}/source-diagnostics", response_model=Envelope[SourceDiagnostics])
    def get(
        pid: str, request: Request, analysis_set_id: str | None = None, release_id: str | None = None, a=Depends(actor)
    ):
        return envelope(request, diagnostics(service, a, pid, analysis_set_id, release_id))
