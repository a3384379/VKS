"""Immutable, passive UI annotations; no imported JavaScript or network executes."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag
from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .documents import DocumentService, canonical, digest, fail
from .models import Analysis, Artifact, Entity, Evidence, Relation, Revision, Selection, uid

SELECTOR = re.compile(
    r"""^(?P<tag>[A-Za-z][A-Za-z0-9-]*)?(?:\#(?P<id>[A-Za-z_][A-Za-z0-9_-]*))?"""
    r"""(?:\.(?P<class>[A-Za-z_][A-Za-z0-9_-]*))?(?:\[(?P<attr>[A-Za-z_][A-Za-z0-9_.:-]*)="""
    r"""(?P<quote>["'])(?P<value>[^"'\\\]\r\n]{1,256})(?P=quote)\])?$"""
)
PASSIVE_TARGETS = frozenset(
    [
        "main",
        "article",
        "section",
        "div",
        "span",
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "header",
        "footer",
        "nav",
        "aside",
        "strong",
        "b",
        "em",
        "i",
        "small",
        "code",
        "pre",
        "br",
        "hr",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "caption",
        "button",
        "label",
        "details",
        "summary",
        "blockquote",
        "dl",
        "dt",
        "dd",
    ]
)
OMITTED = frozenset(
    [
        "script",
        "style",
        "link",
        "iframe",
        "object",
        "embed",
        "svg",
        "math",
        "img",
        "video",
        "audio",
        "source",
        "meta",
        "base",
        "head",
        "title",
        "template",
        "noscript",
    ]
)
HIDDEN_STYLE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important)?\s*(?:;|$)", re.IGNORECASE
)


def parse_selector(selector):
    value = selector.strip()
    match = SELECTOR.fullmatch(value)
    if not match or not any(match.group(key) for key in ("tag", "id", "class", "attr")):
        fail("VKS_LOCATOR_CONFLICT", '仅支持单个 tag/#id/.class/[attr="value"] 组合；复杂定位请使用区域热点', 409)
    return match.groupdict()


def matches(element, parsed):
    return (
        (not parsed["tag"] or element.name == parsed["tag"].lower())
        and (not parsed["id"] or element.get("id") == parsed["id"])
        and (not parsed["class"] or parsed["class"] in element.get("class", []))
        and (not parsed["attr"] or element.get(parsed["attr"].lower()) == parsed["value"])
    )


def hidden(element):
    for node in [element, *element.parents]:
        if not isinstance(node, Tag):
            continue
        if (
            node.name in OMITTED
            or node.has_attr("hidden")
            or node.has_attr("inert")
            or str(node.get("aria-hidden", "")).lower() == "true"
            or HIDDEN_STYLE.search(node.get("style", ""))
        ):
            return True
    return False


def overlaps(a, b):
    return bool(
        a
        and b
        and a["x"] < b["x"] + b["width"]
        and b["x"] < a["x"] + a["width"]
        and a["y"] < b["y"] + b["height"]
        and b["y"] < a["y"] + a["height"]
    )


class HotspotService:
    def __init__(self, service):
        self.service = service
        self.documents = DocumentService(service)

    def _load(self, s, actor, pid, page_id, payload, version):
        self.service.require(s, actor, pid, "knowledge:edit")
        analysis = s.get(Analysis, payload["analysis_set_id"])
        page = s.get(Entity, page_id)
        if not analysis or analysis.project_id != pid or not page or page.project_id != pid or page.type != "page":
            fail("VKS_NOT_FOUND", "未找到页面或分析集合", 404)
        s.refresh(analysis, with_for_update=True)
        if analysis.state == "frozen":
            fail("VKS_STATE_CONFLICT", "发布版热点不可修改，请先创建草稿", 409)
        choice = s.get(Selection, (analysis.id, page.id))
        revision = s.get(Revision, choice.entity_revision_id) if choice else None
        if not revision or not self.service._visible_revision(s, actor, pid, revision):
            fail("VKS_NOT_FOUND", "未找到可访问页面", 404)
        if revision.metadata_json.get("prototype_manifest"):
            fail("VKS_STATE_CONFLICT", "请通过原型清单维护该页面的状态与热区", 409)
        if revision.id != payload["base_page_revision_id"]:
            fail("VKS_VERSION_CONFLICT", "页面修订已变化，请重新定位", 412)
        self.service._version(revision.metadata_json.get("row_version", 1), version)
        actions = []
        for row in self.service._revisions(s, analysis.id):
            entity = s.get(Entity, row.entity_id)
            if (
                entity.type == "action"
                and (row.metadata_json.get("page_key") or row.locator.get("pageKey")) == page.stable_key
            ):
                if not self.service._visible_revision(s, actor, pid, row):
                    fail("VKS_FORBIDDEN", "编辑页面热点需要读取该页面的全部动作", 403)
                actions.append(row)
        return analysis, page, choice, revision, actions

    def _target_state(self, s, actor, analysis, key):
        if not key:
            return
        candidates = set()
        from .scenarios import ScenarioRevision

        for revision in self.service._revisions(s, analysis.id):
            if not self.service._visible_revision(s, actor, analysis.project_id, revision):
                continue
            entity = s.get(Entity, revision.entity_id)
            if entity.type == "page" and revision.metadata_json.get("state_key") == key:
                candidates.add(revision.id)
            elif entity.type == "scenario":
                scenario = s.scalar(select(ScenarioRevision).where(ScenarioRevision.entity_revision_id == revision.id))
                if scenario:
                    graph = scenario.dsl.get("explanation_graph", {})
                    for state in graph.get("states", []):
                        if state.get("stateKey") == key:
                            candidates.add(
                                state.get("pageRevisionId")
                                or state.get("page_revision_id")
                                or "unlocated:" + scenario.id
                            )
        if not candidates:
            fail("VKS_NOT_FOUND", "目标状态不属于当前基线", 404)
        if len(candidates) > 1:
            fail("VKS_LOCATOR_CONFLICT", "目标状态在当前基线存在多个位置，请明确场景后再标注", 409)

    def _validate_locator(self, s, page, revision, actions, payload):
        rect = payload["rect"]
        if (
            any(not 0 <= rect[key] <= 1 for key in ("x", "y", "width", "height"))
            or rect["width"] <= 0
            or rect["height"] <= 0
            or rect["x"] + rect["width"] > 1 + 1e-9
            or rect["y"] + rect["height"] > 1 + 1e-9
        ):
            fail("VKS_LOCATOR_CONFLICT", "区域热点必须在0..1页面范围内且具有正面积", 409)
        editing = payload.get("action_id")
        records = revision.metadata_json.get("hotspots", [])
        for record in records:
            if (
                record["action_id"] != editing
                and record.get("status") != "removed"
                and overlaps(rect, record.get("rect"))
            ):
                fail("VKS_LOCATOR_CONFLICT", "热点区域与另一动作重叠，请缩小或移动区域", 409)
        selector = payload.get("selector")
        if not selector:
            return ["区域定位由编辑者声明；未执行元素点击、状态跳转或业务请求"]
        artifact = s.get(Artifact, revision.artifact_id) if revision.artifact_id else None
        if not artifact or not (
            "html" in artifact.media_type or artifact.relative_path.lower().endswith((".html", ".htm"))
        ):
            fail("VKS_LOCATOR_CONFLICT", "截图热点支持区域定位，不能声明DOM选择器", 409)
        if len(revision.content.encode()) > 2_000_000:
            fail("VKS_LIMIT_EXCEEDED", "DOM快照超过热点定位大小限制", 413)
        parsed = parse_selector(selector)
        soup = BeautifulSoup(revision.content, "html.parser")
        scopes = soup.find_all(attrs={"data-vks-page": page.stable_key})
        if len(scopes) > 1:
            fail("VKS_LOCATOR_CONFLICT", "页面逻辑锚点重复，不能猜测热点所属页面", 409)
        scope = scopes[0] if scopes else soup
        targets = [node for node in scope.find_all(True) if matches(node, parsed)]
        if isinstance(scope, Tag) and scope.name != "[document]" and matches(scope, parsed):
            targets.insert(0, scope)
        if len(targets) != 1:
            fail("VKS_LOCATOR_CONFLICT", f"固定DOM选择器匹配{len(targets)}个元素，必须唯一", 409)
        target = targets[0]
        if hidden(target) or target.name not in PASSIVE_TARGETS:
            fail("VKS_LOCATOR_CONFLICT", "目标隐藏、不可被动渲染或位于隔离内容中，请使用可见区域", 409)
        for action in actions:
            if action.entity_id == editing or action.metadata_json.get("status") == "removed":
                continue
            existing = action.metadata_json.get("selector")
            action_key = s.get(Entity, action.entity_id).stable_key
            if target.get("data-vks-action") == action_key or existing and matches(target, parse_selector(existing)):
                fail("VKS_LOCATOR_CONFLICT", "该元素已有其他动作热点，请明确编辑原动作", 409)
        return ["DOM定位仅验证固定被动快照；未执行脚本、样式计算或真实业务请求"]

    def save(self, actor, pid, page_id, payload, version, remove=False):
        with self.service.db() as s:
            analysis, page, choice, old_page, actions = self._load(s, actor, pid, page_id, payload, version)
            action_id = payload.get("action_id")
            prior = next((row for row in actions if row.entity_id == action_id), None) if action_id else None
            if action_id and not prior:
                fail("VKS_NOT_FOUND", "待编辑动作不属于当前页面", 404)
            if prior and prior.metadata_json.get("status") == "removed":
                fail("VKS_STATE_CONFLICT", "动作已移除，请新建热点身份", 409)
            if remove and not prior:
                fail("VKS_NOT_FOUND", "未找到待移除热点", 404)
            if prior:
                action = s.get(Entity, prior.entity_id)
                if payload.get("action_key", action.stable_key) != action.stable_key:
                    fail("VKS_STATE_CONFLICT", "编辑定位不能改变动作长期身份", 409)
            else:
                if s.scalar(
                    select(Entity).where(
                        Entity.project_id == pid, Entity.type == "action", Entity.stable_key == payload["action_key"]
                    )
                ):
                    fail("VKS_STATE_CONFLICT", "动作逻辑key已存在，请使用action_id明确编辑", 409)
                action = Entity(id=uid(), project_id=pid, type="action", stable_key=payload["action_key"])
                s.add(action)
                s.flush()
            warnings = []
            if remove:
                record = {
                    "action_id": action.id,
                    "action_key": action.stable_key,
                    "label": prior.name,
                    "rect": prior.metadata_json.get("rect"),
                    "selector": prior.metadata_json.get("selector"),
                    "target_state_key": prior.metadata_json.get("target_state_key"),
                    "status": "removed",
                    "evidence_origin": "manual",
                }
            else:
                warnings = self._validate_locator(s, page, old_page, actions, payload)
                self._target_state(s, actor, analysis, payload.get("target_state_key"))
                record = {
                    "action_id": action.id,
                    "action_key": action.stable_key,
                    "label": payload["label"],
                    "rect": payload["rect"],
                    "selector": payload.get("selector") or None,
                    "target_state_key": payload.get("target_state_key") or None,
                    "status": "active",
                    "evidence_origin": "manual",
                }
            records = [row for row in old_page.metadata_json.get("hotspots", []) if row["action_id"] != action.id] + [
                record
            ]
            artifact = s.get(Artifact, old_page.artifact_id)
            if not artifact:
                fail("VKS_NOT_FOUND", "页面原始制品缺失，不能保存定位", 404)
            new_page = self.documents._revision(
                s,
                page,
                artifact,
                old_page.name,
                old_page.content,
                old_page.locator,
                dict(
                    old_page.metadata_json,
                    hotspots=records,
                    row_version=old_page.metadata_json.get("row_version", 1) + 1,
                    preview_mode="snapshot",
                    source_kind="manual_hotspots",
                    runtime_source_alignment="unknown",
                ),
            )
            choice.entity_revision_id = new_page.id
            action_meta = dict(
                prior.metadata_json if prior else {},
                **{k: v for k, v in record.items() if k not in ("action_id", "action_key", "label")},
                page_key=page.stable_key,
                page_revision_id=new_page.id,
                row_version=(prior.metadata_json.get("row_version", 1) if prior else 0) + 1,
                event="click",
                runtime_source_alignment="unknown",
            )
            locator = {
                "kind": "ui_hotspot",
                "pageKey": page.stable_key,
                "actionKey": action.stable_key,
                "rect": record["rect"],
                "selector": record["selector"],
            }
            new_action = self.documents._revision(
                s, action, artifact, record["label"], record["label"], locator, action_meta
            )
            selected = s.get(Selection, (analysis.id, action.id))
            if selected:
                selected.entity_revision_id = new_action.id
            else:
                s.add(Selection(analysis_set_id=analysis.id, entity_id=action.id, entity_revision_id=new_action.id))
            evidence = Evidence(
                id=uid(),
                project_id=pid,
                analysis_set_id=analysis.id,
                artifact_id=artifact.id,
                stable_key="hotspot." + new_page.id + "." + action.id,
                evidence_kind="manual",
                content_sha256=artifact.sha256,
                locator=dict(locator, annotation_sha256=digest(canonical(record))),
                excerpt=canonical(record),
                claim="授权编辑者标注的被动页面热点；不证明真实点击、状态跳转或接口因果",
            )
            s.add(evidence)
            s.flush()
            replaced = {old_page.id: new_page.id}
            if prior:
                replaced[prior.id] = new_action.id
            for relation in s.scalars(select(Relation).where(Relation.analysis_set_id == analysis.id)):
                if relation.from_revision_id not in replaced and relation.to_revision_id not in replaced:
                    continue
                target = s.get(Revision, relation.to_revision_id)
                static_membership = (
                    old_page.artifact_id == new_page.artifact_id
                    and old_page.content == new_page.content
                    and old_page.locator == new_page.locator
                    and relation.from_revision_id == old_page.id
                    and relation.relation_type == "contains"
                    and relation.to_revision_id not in replaced
                    and relation.validity_status == "valid"
                    and target is not None
                    and target.artifact_id == artifact.id
                    and target.metadata_json.get("evidence_origin") == "static"
                    and target.metadata_json.get("discovery_basis") in ("explicit_dom_attribute", "unique_dom_id")
                    and bool(relation.evidence_ids)
                    and all(
                        (ev := s.get(Evidence, eid)) is not None
                        and ev.artifact_id == artifact.id
                        and ev.content_sha256 == artifact.sha256
                        and ev.evidence_kind == "static"
                        for eid in relation.evidence_ids
                    )
                )
                relation.from_revision_id = replaced.get(relation.from_revision_id, relation.from_revision_id)
                relation.to_revision_id = replaced.get(relation.to_revision_id, relation.to_revision_id)
                relation.validity_status = (
                    "broken"
                    if remove and new_action.id in (relation.from_revision_id, relation.to_revision_id)
                    else "valid"
                    if static_membership
                    else "stale"
                )
                if relation.causal_basis == "correlated_runtime":
                    relation.causal_basis = "not_established"
                if not static_membership:
                    relation.evidence_ids = list(dict.fromkeys([*relation.evidence_ids, evidence.id]))
                relation.row_version += 1
            if not remove:
                for relation_type in ("contains", "renders"):
                    if not s.scalar(
                        select(Relation).where(
                            Relation.analysis_set_id == analysis.id,
                            Relation.from_revision_id == new_page.id,
                            Relation.to_revision_id == new_action.id,
                            Relation.relation_type == relation_type,
                        )
                    ):
                        relation = Relation(
                            id=uid(),
                            project_id=pid,
                            analysis_set_id=analysis.id,
                            from_revision_id=new_page.id,
                            to_revision_id=new_action.id,
                            relation_type=relation_type,
                            excluded=False,
                            validity_status="valid",
                            causal_basis="not_established",
                            evidence_ids=[evidence.id],
                            reason="人工被动热点标注，保存后可用",
                            manual_lock=False,
                        )
                        s.add(relation)
                        s.flush()
                        self.service._verify_relation(s, relation)
            analysis.selection_version += 1
            self.service.audit(
                s,
                actor,
                pid,
                "hotspot.remove" if remove else "hotspot.save",
                new_action.id,
                metadata={"page_revision_id": new_page.id, "action_id": action.id},
            )
            s.flush()
            return {
                "page": self.service.entity_dto(s, new_page, True),
                "action": self.service.entity_dto(s, new_action, True),
                "hotspots": records,
                "warnings": warnings,
            }


class RectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    x: float
    y: float
    width: float
    height: float


class HotspotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str
    base_page_revision_id: str
    action_id: str | None = None
    action_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
    label: str = Field(min_length=1, max_length=160)
    rect: RectInput
    selector: str | None = Field(default=None, max_length=512)
    target_state_key: str | None = Field(default=None, max_length=240)


class RemoveHotspotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_set_id: str
    base_page_revision_id: str


def install_hotspots(app, service, actor, envelope, API="/api/v1"):
    hotspots = HotspotService(service)
    actor_dependency = Depends(actor)

    @app.post(API + "/projects/{pid}/pages/{page_id}/hotspots")
    def save(
        pid: str, page_id: str, payload: HotspotInput, request: Request, a=actor_dependency, if_match: str = Header(...)
    ):
        return envelope(request, hotspots.save(a, pid, page_id, payload.model_dump(), if_match))

    @app.delete(API + "/projects/{pid}/pages/{page_id}/hotspots/{action_id}")
    def remove(
        pid: str,
        page_id: str,
        action_id: str,
        payload: RemoveHotspotInput,
        request: Request,
        a=actor_dependency,
        if_match: str = Header(...),
    ):
        return envelope(
            request,
            hotspots.save(a, pid, page_id, dict(payload.model_dump(), action_id=action_id), if_match, remove=True),
        )

    return hotspots
