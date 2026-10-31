"""Explainable overlapping groups; no fuzzy identity merges or auto acceptance."""

from collections import defaultdict
from pathlib import PurePosixPath
from .service import canonical, sha


def candidates(workspace):
    output = []

    def emit(key, title, rows, basis, reason):
        rows = list({r["entity_id"]: r for r in rows}.values())
        output.append(
            dict(
                key="module-" + sha(canonical(key))[:24],
                title=title[:160],
                entity_ids=[r["entity_id"] for r in rows],
                revision_ids=[r["entity_revision_id"] for r in rows],
                basis=basis,
                excluded=False,
                reason=reason,
                member_count=len(rows),
                scope="有界任务输入；候选可重叠，采用后可在模块管理调整",
            )
        )

    for page in workspace["pages"][:50]:
        actions = [
            a
            for a in workspace["actions"]
            if a["metadata"].get("page_key", a["metadata"].get("pageKey")) == page["stable_key"]
        ]
        emit(
            ["page", page["stable_key"]],
            page["name"],
            [page, *actions],
            "explicit_page_action_membership",
            "按准确页面/动作标识分组；业务模块边界仍需人员核对",
        )
    groups = defaultdict(list)
    for row in workspace["entities"]:
        metadata = row.get("metadata", {})
        service = metadata.get("service_key") or row.get("source_key", "")
        if row["type"] == "api":
            route = metadata.get("normalized_route") or metadata.get("path") or metadata.get("route")
            if isinstance(route, str) and route.startswith("/") and route.strip("/"):
                groups[("route_prefix", service, "/" + route.strip("/").split("/")[0])].append(row)
        path = row.get("source_path")
        if path and row["type"] in ("document", "section", "symbol"):
            parent = str(PurePosixPath(path).parent)
            groups[
                (
                    "document_directory" if row["type"] in ("document", "section") else "source_directory",
                    service,
                    parent,
                )
            ].append(row)
    for (basis, service, prefix), rows in sorted(groups.items()):
        if len(output) >= 50:
            break
        emit(
            [basis, service, prefix],
            (service + " · " if service else "") + prefix,
            rows,
            basis,
            "按显式服务范围内的路由前缀或来源目录提出分组；目录/类名/文档仅是线索，不代表已确认业务边界",
        )
    return output
