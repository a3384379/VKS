"""Business reading stays passive, branches explicitly, and preserves exact source context."""

from copy import deepcopy
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from test_frontend_browser import site as site, chromium_path  # pytest fixture registration
from vks_api.journeys import JourneyService

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def guide_site(site):
    service = site.service
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(owner, {"key": "journey-reading", "name": "业务导览回归"}, "reading-project")["id"]
    job = service.demo_import(owner, pid, "reading-material")
    service.process_job(job["id"])
    workspace = service.workspace(owner, pid)
    aid = workspace["selected_analysis_set"]["id"]
    action = workspace["actions"][0]
    document = workspace["documents"][0]
    graph = dict(
        schema_version="2.0", key="reading-business", title="办理一项申请", entry="apply",
        semantics=dict(kind="business_process", basis="declared", purpose="理解申请从提交到结果的处理", preconditions=["使用合成数据", "当前角色具备申请权限"], outcome="获得明确的处理结果", unknowns=["完整业务成功路径尚未验收"], source_refs=[dict(entity_id=document["entity_id"], revision_id=document["entity_revision_id"])]),
        nodes=[
            dict(key="apply", kind="action", title="了解申请操作", description="先核对输入条件，再理解提交动作的接口和证据。", x=0, y=0, binding=dict(entity_id=action["entity_id"], revision_id=action["entity_revision_id"])),
            dict(key="explain", kind="step", title="核对资料中的业务说明", description="这是材料中声明的步骤，没有独立动作绑定。", x=160, y=0),
            dict(key="check", kind="decision", title="检查申请条件", description="下面的条件来自测试资料声明。", x=250, y=0),
            dict(key="accepted", kind="end", title="申请被受理", description="声明结果；尚未执行真实申请。", x=500, y=0),
            dict(key="rejected", kind="end", title="提示补充资料", description="声明失败结果，保留待验证范围。", x=500, y=150),
        ],
        edges=[dict(key="next", source="apply", target="explain", label="已理解输入"), dict(key="understood", source="explain", target="check", label=""), dict(key="yes", source="check", target="accepted", label="资料完整"), dict(key="no", source="check", target="rejected", label="缺少必填资料")],
    )
    journeys = JourneyService(service)
    flow = journeys.create(owner, pid, dict(analysis_set_id=aid, dsl=graph), "reading-business")
    legacy = deepcopy(graph)
    legacy.pop("semantics")
    legacy.update(key="reading-legacy", title="旧业务流程候选")
    journeys.create(owner, pid, dict(analysis_set_id=aid, dsl=legacy), "reading-legacy")
    evidence = deepcopy(graph)
    evidence.update(key="reading-evidence", title="独立动作证据")
    evidence["semantics"] = dict(kind="evidence_navigation", basis="inferred", purpose="独立核对动作来源")
    journeys.create(owner, pid, dict(analysis_set_id=aid, dsl=evidence), "reading-evidence")
    comparison = deepcopy(graph)
    comparison.update(key="reading-comparison", title="测试观察对比")
    comparison["semantics"] = dict(kind="observation_comparison", basis="observed", purpose="比较保存状态的差异")
    journeys.create(owner, pid, dict(analysis_set_id=aid, dsl=comparison), "reading-comparison")
    return dict(url=site[0], pid=pid, aid=aid, flow=flow, action=action, document=document)


def login(page, s):
    page.goto(s["url"] + f"/#view=journeys&project={s['pid']}&entity={s['flow']['journey_id']}")
    page.get_by_label("登录名", exact=True).fill("owner")
    page.get_by_label("密码", exact=True).fill("owner")
    page.get_by_role("button", name="打开工作空间").click()
    expect(page.get_by_label("当前导览", exact=True)).to_be_visible()


def test_reading_embeds_same_version_page_and_knowledge_with_explicit_passive_branches(guide_site):
    s = guide_site
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login(page, s)
            reader = page.get_by_label("当前导览", exact=True)
            expect(page.get_by_label("流程名称", exact=True)).to_have_count(0)
            expect(page.get_by_label("连接起点", exact=True)).to_have_count(0)
            expect(page.get_by_role("button", name="导出 DSL", exact=True)).to_have_count(0)
            expect(reader).to_contain_text("理解申请从提交到结果的处理")
            expect(reader).to_contain_text("当前角色具备申请权限")
            expect(reader).to_contain_text("完整业务成功路径尚未验收")
            expect(reader.get_by_label("步骤对应页面")).to_be_visible()
            expect(reader.locator(".safe-prototype")).to_be_visible()
            expect(reader.get_by_label("当前步骤知识")).to_contain_text("先核对输入条件")
            assert "view=journeys" in page.url
            requests = []
            page.on("request", lambda request: requests.append((request.method, request.url)))
            knowledge = reader.get_by_label("当前步骤知识")
            knowledge.get_by_role("button", name="接口", exact=True).click()
            knowledge.get_by_role("button", name="证据", exact=True).click()
            reader.get_by_role("button", name="核对资料中的业务说明", exact=True).click()
            declaration = reader.get_by_label("当前步骤的声明依据", exact=True)
            expect(declaration).to_contain_text("没有独立动作绑定")
            declaration.locator("summary").filter(has_text=s["document"]["name"]).click()
            expect(declaration.locator(".markdown")).to_be_visible()
            reader.get_by_role("button", name="检查申请条件", exact=True).click()
            expect(reader.get_by_role("button", name="申请被受理", exact=True)).to_have_count(0)
            reader.get_by_label("选择阅读分支").get_by_role("button", name="资料完整 申请被受理", exact=True).click()
            expect(reader.get_by_role("button", name="申请被受理", exact=True)).to_have_attribute("aria-pressed", "true")
            expect(reader.get_by_role("button", name="提示补充资料", exact=True)).to_have_count(0)
            reader.get_by_label("已选阅读分支").get_by_role("button").click()
            expect(reader.get_by_role("button", name="申请被受理", exact=True)).to_have_count(0)
            reader.get_by_label("选择阅读分支").get_by_role("button", name="缺少必填资料 提示补充资料", exact=True).click()
            expect(reader.get_by_role("button", name="提示补充资料", exact=True)).to_have_attribute("aria-pressed", "true")
            reader.get_by_role("button", name="了解申请操作", exact=True).click()
            expect(reader.get_by_label("步骤对应页面")).to_be_visible()
            expect(reader.get_by_label("当前步骤知识")).to_contain_text("先核对输入条件")
            library = page.get_by_label("导览目录")
            library.get_by_role("button", name="独立动作证据", exact=False).click()
            expect(reader.locator("ol.journey-reading-steps")).to_have_count(0)
            expect(reader).to_contain_text("条目之间没有业务先后含义")
            library.get_by_role("button", name="旧业务流程候选", exact=False).click()
            expect(reader).to_contain_text("现有材料不足以作为业务流程导览")
            expect(reader.locator("ol.journey-reading-steps")).to_have_count(0)
            library.get_by_role("button", name="测试观察对比", exact=False).click()
            expect(reader).to_contain_text("状态差异不证明业务条件或执行因果")
            assert "view=journeys" in page.url
            assert not [(method, url) for method, url in requests if method in ("POST", "PUT", "PATCH", "DELETE")]
            assert not errors
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            output = ROOT / "output/playwright"
            output.mkdir(parents=True, exist_ok=True)
            library.get_by_role("button", name="办理一项申请", exact=False).click()
            page.screenshot(path=str(output / "journey-business-reading.png"), full_page=True)
        finally:
            browser.close()


def test_meaning_is_maintained_with_ordinary_forms_and_returns_to_reading(guide_site):
    s = guide_site
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        try:
            login(page, s)
            page.get_by_role("button", name="维护导览与结构", exact=True).click()
            page.get_by_label("业务目标或材料用途", exact=True).fill("理解申请的处理边界")
            page.get_by_label("前置条件", exact=True).fill("使用合成数据\n\n具备申请权限\n")
            page.get_by_label("步骤说明", exact=True).fill("只阅读已有知识，核对资料来源。")
            page.get_by_label("查找依据资料", exact=True).fill(s["action"]["name"])
            page.get_by_role("checkbox", name=f"依据资料：动作 · {s['action']['name']}", exact=True).check()
            with page.expect_response(lambda response: "/revisions" in response.url and response.request.method == "POST") as saved:
                page.get_by_role("button", name="保存流程", exact=True).click()
            assert saved.value.ok, saved.value.text()
            data = saved.value.json()["data"]
            assert data["dsl"]["semantics"]["preconditions"] == ["使用合成数据", "具备申请权限"]
            assert s["flow"]["dsl"]["semantics"]["source_refs"][0] in data["dsl"]["semantics"]["source_refs"]
            assert dict(entity_id=s["action"]["entity_id"], revision_id=s["action"]["entity_revision_id"]) in data["dsl"]["semantics"]["source_refs"]
            page.get_by_role("button", name="返回业务导览", exact=True).click()
            reader = page.get_by_label("当前导览", exact=True)
            expect(reader).to_contain_text("理解申请的处理边界")
            expect(reader.get_by_label("当前步骤知识")).to_contain_text("只阅读已有知识，核对资料来源。")
            expect(page.get_by_label("节点类型", exact=True)).to_have_count(0)
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1440, 1024, 768, 375])
def test_business_guide_remains_readable_without_page_overflow(guide_site, width):
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": width, "height": 1000})
        try:
            login(page, guide_site)
            expect(page.get_by_label("当前步骤的页面与知识")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        finally:
            browser.close()
