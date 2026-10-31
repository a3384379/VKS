"""Exact guide URLs, passive source returns and authorized inline attachments."""

from test_frontend_browser import site as site  # pytest fixture registration

from copy import deepcopy
from io import BytesIO
import re
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select

from test_frontend_browser import chromium_path
from packages.collector.snapshots import Snapshot, bundle_bytes
from vks_api.journeys import JourneyService
from vks_api.models import Evidence, Relation, uid


@pytest.fixture(scope="module")
def navigation_site(site):
    service = site.service
    _, owner = service.login(service.bootstrap_token)
    pid = service.create_project(owner, dict(key="journey-nav", name="Journey navigation fixture"), "nav-project")["id"]
    job = service.demo_import(owner, pid, "nav-demo")
    service.process_job(job["id"])
    aid = service.workspace(owner, pid)["selected_analysis_set"]["id"]
    image = BytesIO()
    Image.new("RGB", (32, 32), "#eef4ff").save(image, format="PNG")
    raw = bundle_bytes(
        Snapshot(
            "local",
            "nav-fixture",
            {
                "manual.md": "# 附件导航手册\n\n## 具体业务说明\n\n![准确图片](images/map.png)\n\n[下载准确附件](images/map.png)\n".encode(),
                "images/map.png": image.getvalue(),
            },
        ),
        "journey-nav",
        "nav-attachments",
        source_key="guide-docs",
        role="documentation",
    )
    with service.db() as db:
        service._commit_bundle(db, pid, owner.id, service._parse(raw, "journey-nav"), aid)
    w = service.workspace(owner, pid, aid)
    document = next(row for row in w["documents"] if row["name"] == "附件导航手册")
    section = next(row for row in w["entities"] if row["type"] == "section" and row["name"] == "具体业务说明")
    symbol = next(row for row in w["entities"] if row["type"] == "symbol")
    action = w["actions"][0]
    with service.db() as db:
        evidence = db.scalar(select(Evidence).where(Evidence.analysis_set_id == aid))
        for target in (document, section):
            db.add(
                Relation(
                    id=uid(),
                    project_id=pid,
                    analysis_set_id=aid,
                    from_revision_id=action["entity_revision_id"],
                    to_revision_id=target["entity_revision_id"],
                    relation_type="documents",
                    evidence_ids=[evidence.id],
                    reason="Synthetic exact attachment context",
                    excluded=False,
                    validity_status="valid",
                    causal_basis="not_established",
                )
            )
    graph = dict(
        schema_version="2.0",
        key="nav-a",
        title="导航A",
        entry="action",
        semantics=dict(
            kind="business_process",
            basis="declared",
            purpose="准确上下文回归",
            outcome="保留步骤与资料来源",
            preconditions=["合成数据"],
            source_refs=[
                dict(entity_id=row["entity_id"], revision_id=row["entity_revision_id"])
                for row in (document, section, symbol)
            ],
        ),
        nodes=[
            dict(
                key="action",
                kind="action",
                title="核对动作",
                x=0,
                y=0,
                binding=dict(entity_id=action["entity_id"], revision_id=action["entity_revision_id"]),
            ),
            dict(key="declare", kind="step", title="阅读资料声明", description="依据文档核对条件", x=120, y=0),
            dict(key="check", kind="decision", title="核对分支", x=240, y=0),
            dict(key="yes", kind="end", title="符合声明条件", x=360, y=0),
            dict(key="no", kind="end", title="不符合声明条件", x=360, y=120),
        ],
        edges=[
            dict(key="next", source="action", target="declare", label=""),
            dict(key="reviewed", source="declare", target="check", label=""),
            dict(key="accept", source="check", target="yes", label="条件满足"),
            dict(key="decline", source="check", target="no", label="条件缺失"),
        ],
    )
    journeys = JourneyService(service)
    first = journeys.create(owner, pid, dict(analysis_set_id=aid, dsl=graph), "nav-a")
    second_dsl = deepcopy(graph)
    second_dsl.update(key="nav-b", title="导航B")
    second = journeys.create(owner, pid, dict(analysis_set_id=aid, dsl=second_dsl), "nav-b")
    return dict(
        url=site[0],
        service=service,
        owner=owner,
        pid=pid,
        aid=aid,
        first=first,
        second=second,
        document=document,
        section=section,
        symbol=symbol,
    )


def login(page, s):
    page.goto(
        s["url"] + f"/#view=journeys&project={s['pid']}&baseline=draft:{s['aid']}&entity={s['first']['journey_id']}"
    )
    page.get_by_label("登录名", exact=True).fill("owner")
    page.get_by_label("密码", exact=True).fill("owner")
    page.get_by_role("button", name="打开工作空间").click()
    expect(page.get_by_label("当前导览", exact=True)).to_be_visible()


def parameters(page):
    return parse_qs(urlparse(page.url).fragment)


def test_library_step_branch_history_and_source_return_are_shareable(navigation_site):
    s = navigation_site
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport=dict(width=1600, height=1100))
        errors, writes = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login(page, s)
            page.on(
                "request",
                lambda request: (
                    writes.append(request.url) if request.method in ("POST", "PUT", "PATCH", "DELETE") else None
                ),
            )
            reader = page.get_by_label("当前导览", exact=True)
            page.get_by_label("导览目录").get_by_role("button", name="导航B", exact=False).click()
            expect(reader.get_by_role("heading", name="导航B", exact=True)).to_be_visible()
            expect(page).to_have_url(re.compile("entity=" + s["second"]["journey_id"]))
            reader.get_by_role("button", name="阅读资料声明", exact=True).click()
            declaration_url = page.url
            expect(reader.get_by_role("button", name="阅读资料声明", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            reader.get_by_label("选择阅读分支").get_by_role("button", name="条件满足 符合声明条件", exact=True).click()
            expect(reader.get_by_role("button", name="符合声明条件", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            assert parameters(page)["journey_revision"] == [s["second"]["revision_id"]]
            branch_url = page.url
            page.go_back()
            expect(reader.get_by_role("button", name="阅读资料声明", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            assert page.url == declaration_url
            page.go_forward()
            expect(reader.get_by_role("button", name="符合声明条件", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            assert page.url == branch_url
            page.reload()
            expect(reader.get_by_role("button", name="符合声明条件", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            reader.get_by_role("button", name="阅读资料声明", exact=True).click()
            declaration_url = page.url
            context = reader.get_by_label("当前步骤的声明依据", exact=True)
            details = context.locator("details").filter(
                has=page.locator("summary").get_by_text(s["document"]["name"], exact=True)
            )
            details.locator("summary").click()
            details.get_by_role("button", name="打开准确来源", exact=True).click()
            expect(page.locator(".document-reader")).to_contain_text("附件导航手册")
            expect(page.get_by_role("button", name="返回业务导览 · 导航B", exact=True)).to_be_visible()
            page.go_back()
            expect(reader.get_by_role("button", name="阅读资料声明", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            assert page.url == declaration_url
            reader.locator(".journey-reading-support").get_by_role(
                "button", name=s["symbol"]["name"], exact=True
            ).click()
            expect(page.locator(".graph-detail")).to_contain_text(s["symbol"]["name"])
            page.reload()
            page.get_by_role("button", name="返回业务导览 · 导航B", exact=True).click()
            expect(reader.get_by_role("button", name="阅读资料声明", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            assert parameters(page)["entity"] == [s["second"]["journey_id"]]
            assert parameters(page)["journey_choices"] == ['{"check":"accept"}']
            # A standalone document deep link has no remembered guide. Its
            # reverse projection still opens the exact selected Journey.
            page.goto(
                s["url"]
                + f"/#view=documents&project={s['pid']}&baseline=draft:{s['aid']}&entity={s['document']['entity_id']}"
            )
            references = page.locator(".chapter-relations .v2-business-references")
            references.get_by_role("button", name="打开导览 · 导航B →", exact=True).click()
            expect(reader.get_by_role("heading", name="导航B", exact=True)).to_be_visible()
            expect(reader.get_by_role("button", name="阅读资料声明", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            assert parameters(page)["entity"] == [s["second"]["journey_id"]]
            assert parameters(page)["journey_revision"] == [s["second"]["revision_id"]]
            assert not writes and not errors
        finally:
            browser.close()


def test_declared_document_section_and_related_cards_load_exact_authorized_attachments(navigation_site):
    s = navigation_site
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport=dict(width=1600, height=1100))
        requests = []
        try:
            login(page, s)
            page.on("request", lambda request: requests.append(request.url) if "/attachments?" in request.url else None)
            reader = page.get_by_label("当前导览", exact=True)
            reader.get_by_role("button", name="阅读资料声明", exact=True).click()
            declaration = reader.get_by_label("当前步骤的声明依据", exact=True)
            for source in (s["document"], s["section"]):
                details = declaration.locator("details").filter(
                    has=page.locator("summary").get_by_text(source["name"], exact=True)
                )
                details.locator("summary").click()
                expect(details.get_by_role("img", name="准确图片", exact=True)).to_be_visible()
                expect(details.get_by_role("img", name="准确图片", exact=True)).to_have_js_property("naturalWidth", 32)
                expect(details.get_by_role("link", name="下载准确附件", exact=True)).to_have_attribute(
                    "href",
                    f"/api/v1/documents/{s['document']['entity_id']}/revisions/{s['document']['entity_revision_id']}/attachments?path=images%2Fmap.png",
                )
            reader.get_by_role("button", name="核对动作", exact=True).click()
            knowledge = reader.get_by_label("当前步骤知识", exact=True)
            knowledge.get_by_role("button", name="文档", exact=True).click()
            expect(knowledge.get_by_role("img", name="准确图片", exact=True)).to_have_count(2)
            for image in knowledge.get_by_role("img", name="准确图片", exact=True).all():
                expect(image).to_be_visible()
                expect(image).to_have_js_property("naturalWidth", 32)
            expected = (
                f"/documents/{s['document']['entity_id']}/revisions/{s['document']['entity_revision_id']}/attachments?"
            )
            assert len(requests) >= 4 and all(expected in url for url in requests)
        finally:
            browser.close()


def test_steps_branches_and_same_guide_history_do_not_refetch_journey_library(navigation_site):
    s = navigation_site
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport=dict(width=1600, height=1100))
        try:
            login(page, s)
            reader = page.get_by_label("当前导览", exact=True)
            list_requests = []
            page.on(
                "request",
                lambda request: (
                    list_requests.append(request.url) if f"/projects/{s['pid']}/journeys?" in request.url else None
                ),
            )
            reader.get_by_role("button", name="阅读资料声明", exact=True).click()
            expect(reader.get_by_role("button", name="阅读资料声明", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            reader.get_by_label("选择阅读分支").get_by_role("button", name="条件满足 符合声明条件", exact=True).click()
            expect(reader.get_by_role("button", name="符合声明条件", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            reader.get_by_label("已选阅读分支").get_by_role("button").click()
            expect(reader.get_by_role("button", name="核对分支", exact=True)).to_have_attribute("aria-pressed", "true")
            reader.get_by_label("选择阅读分支").get_by_role(
                "button", name="条件缺失 不符合声明条件", exact=True
            ).click()
            expect(reader.get_by_role("button", name="不符合声明条件", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            page.go_back()
            expect(reader.get_by_role("button", name="核对分支", exact=True)).to_have_attribute("aria-pressed", "true")
            page.go_forward()
            expect(reader.get_by_role("button", name="不符合声明条件", exact=True)).to_have_attribute(
                "aria-pressed", "true"
            )
            assert not list_requests
        finally:
            browser.close()
