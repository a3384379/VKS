"""Conservative projections when an input identifies actions but proves no order."""

from packages.journey_dsl import validate_journey
from .service import DomainError


def planned_business_journey(structure, catalog, key, title, unknowns):
    """Compile bounded model data using only server-provided fixed identities."""
    rows = {row["revision_id"]: row for row in catalog}
    referenced = set(structure["source_revision_ids"]) | {
        node["target_revision_id"] for node in structure["nodes"] if node["target_revision_id"] is not None
    }
    if referenced - set(rows):
        raise DomainError("VKS_INVALID_EVIDENCE", "业务导览引用了未提供或无权的准确修订")
    if any(rows[rid]["type"] == "journey" for rid in structure["source_revision_ids"]):
        raise DomainError("VKS_INVALID_INPUT", "业务声明来源不能引用其他流程")
    allowed = {
        "page": {"page"},
        "action": {"action"},
        "reference": {"document", "section", "api", "symbol", "sql", "table", "scenario"},
    }
    nodes = []
    for index, value in enumerate(structure["nodes"]):
        node = {name: value[name] for name in ("key", "kind", "title")}
        node.update(x=40 + (index % 4) * 240, y=40 + (index // 4) * 120)
        if value["description"] is not None:
            node["description"] = value["description"]
        rid = value["target_revision_id"]
        if rid is not None:
            row = rows[rid]
            if row["type"] not in allowed.get(value["kind"], set()):
                raise DomainError("VKS_INVALID_INPUT", "业务导览节点与固定引用类型不一致")
            node["binding"] = dict(entity_id=row["entity_id"], revision_id=rid)
        nodes.append(node)
    dsl = dict(
        schema_version="2.0",
        key=key,
        title=title,
        entry=structure["entry"],
        nodes=nodes,
        edges=[dict(edge, key="edge-" + str(index)) for index, edge in enumerate(structure["edges"])],
        semantics=dict(
            kind="business_process",
            basis="inferred",
            purpose=structure["purpose"],
            preconditions=structure["preconditions"],
            outcome=structure["outcome"],
            source_refs=[
                dict(entity_id=rows[rid]["entity_id"], revision_id=rid) for rid in structure["source_revision_ids"]
            ],
            unknowns=list(
                dict.fromkeys(
                    [
                        *(item for item in unknowns if item.strip()),
                        "业务目标、步骤、分支与结果为依据固定材料生成的推断，尚未经过独立业务验证",
                    ]
                )
            ),
        ),
    )
    try:
        return validate_journey(dsl)
    except ValueError as error:
        raise DomainError("VKS_INVALID_INPUT", str(error)) from error


def action_evidence_navigation(key, title, actions, *, basis):
    """A fan-out denotes independent evidence choices, never an execution chain."""
    unknowns = ["所选动作之间的业务先后、前置条件和结果尚未建立；排列位置不代表执行顺序"]
    return dict(
        schema_version="2.0",
        key=key,
        title=title,
        semantics=dict(
            kind="evidence_navigation",
            basis=basis,
            purpose="逐个查看所选动作在固定知识版本中的依据",
            outcome="定位动作关联知识与待补证内容",
            unknowns=unknowns,
        ),
        entry="choose",
        nodes=[dict(key="choose", kind="branch", title="选择一个动作查看证据", x=40, y=40)]
        + [
            dict(
                key="step-" + str(i),
                title=row["name"],
                description="独立的证据入口；不表示此动作先于或后于其他动作执行。",
                kind="action",
                x=280 + (i % 3) * 240,
                y=40 + (i // 3) * 140,
                binding=dict(entity_id=row["entity_id"], revision_id=row["entity_revision_id"]),
            )
            for i, row in enumerate(actions)
        ]
        + [dict(key="done", kind="end", title="本次证据查看结束", x=40, y=200 + ((len(actions) - 1) // 3) * 140)],
        edges=[
            edge
            for i in range(len(actions))
            for edge in (
                dict(key="choose-" + str(i), source="choose", target="step-" + str(i), label="查看该动作证据"),
                dict(key="return-" + str(i), source="step-" + str(i), target="done", label="结束查看"),
            )
        ],
    )
