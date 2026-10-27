"""Passive business journeys. This DSL contains references, never executable steps."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Binding(Strict):
    entity_id: str = Field(min_length=1, max_length=36)
    revision_id: str = Field(min_length=1, max_length=36)


class StateReference(Strict):
    scenario_id: str = Field(min_length=1, max_length=36)
    scenario_revision_id: str = Field(min_length=1, max_length=36)
    state_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,96}$")


class CaptureReference(Strict):
    scenario_id: str = Field(min_length=1, max_length=36)
    scenario_revision_id: str = Field(min_length=1, max_length=36)
    capture_id: str = Field(min_length=1, max_length=36)
    snapshot_index: int = Field(ge=0, le=19)


class Node(Strict):
    key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,96}$")
    kind: Literal["page", "action", "decision", "branch", "end", "reference"]
    title: str = Field(min_length=1, max_length=200)
    x: int = Field(ge=0, le=4000)
    y: int = Field(ge=0, le=4000)
    binding: Binding | None = None
    state_ref: StateReference | None = None
    capture_ref: CaptureReference | None = None


class Edge(Strict):
    key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,96}$")
    source: str = Field(min_length=1, max_length=96)
    target: str = Field(min_length=1, max_length=96)
    label: str = Field(max_length=160)


class Journey(Strict):
    schema_version: Literal["2.0"]
    key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,96}$")
    title: str = Field(min_length=1, max_length=200)
    entry: str = Field(min_length=1, max_length=96)
    nodes: list[Node] = Field(min_length=1, max_length=120)
    edges: list[Edge] = Field(max_length=240)


def validate_journey(value):
    try:
        model = Journey.model_validate(value)
    except ValidationError as error:
        raise ValueError("流程字段不符合 2.0 契约") from error
    nodes = {node.key: node for node in model.nodes}
    if len(nodes) != len(model.nodes) or len({edge.key for edge in model.edges}) != len(model.edges):
        raise ValueError("节点与连接 key 必须唯一")
    if model.entry not in nodes:
        raise ValueError("入口节点不存在")
    outgoing = {key: [] for key in nodes}
    pairs = set()
    for edge in model.edges:
        if edge.source not in nodes or edge.target not in nodes or (edge.source, edge.target) in pairs:
            raise ValueError("连接悬空或重复")
        pairs.add((edge.source, edge.target))
        outgoing[edge.source].append(edge)
    for node in nodes.values():
        edges = outgoing[node.key]
        if node.kind == "end" and edges:
            raise ValueError("终止节点不能有出边")
        if node.kind != "end" and not edges:
            raise ValueError("非终止节点需要后继")
        if node.kind == "decision" and (
            len(edges) < 2
            or any(not edge.label.strip() for edge in edges)
            or len({edge.label.strip() for edge in edges}) != len(edges)
        ):
            raise ValueError("判断需要至少两个不同名称的分支")
        if node.kind in ("page", "action", "reference") and not node.binding:
            raise ValueError("页面、动作和知识引用需要准确修订")
        if node.kind in ("decision", "branch", "end") and node.binding:
            raise ValueError("控制节点不能携带知识引用")
        if (
            node.state_ref
            and node.capture_ref
            or node.kind in ("decision", "branch", "end")
            and (node.state_ref or node.capture_ref)
        ):
            raise ValueError("节点只能引用一种被动状态，控制节点不能携带回放引用")
    active, visited = set(), set()

    def walk(key):
        if key in active:
            raise ValueError("流程不能包含循环")
        if key in visited:
            return
        active.add(key)
        for edge in outgoing[key]:
            walk(edge.target)
        active.remove(key)
        visited.add(key)

    walk(model.entry)
    if visited != set(nodes):
        raise ValueError("流程含不可达节点")
    # Omit absent optional bindings for stable JSON round trips.
    return model.model_dump(exclude_none=True)
