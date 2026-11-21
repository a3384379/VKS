"""Chapter entry navigation keeps the reader context and exact prototype locators."""

from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright
from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.models import Relation, uid

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


@pytest.mark.parametrize("reader_mode", ["standard", "handbook"])
def test_chapter_entries_fold_other_pages_and_restore_reading(materials, tmp_path, monkeypatch, reader_mode):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    main = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "prototype",
        '<title>收费标准设置</title><button id="formula">计费公式</button><button id="formula-edit">计费公式</button>',
        name="收费配置",
    )
    other = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "prototype",
        '<title>收费台</title><button id="payment">收款</button>',
        name="缴费工作台",
    )
    docs = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        "# 收费手册\n\n## 前置说明\n\n"
        + "用于测试阅读滚动位置。\n\n" * 100
        + "## 收费标准设置\n\n"
        + "先核对项目和收费项。\n\n" * 35
        + "## 无关联章节\n\n这一节仅用于阅读。\n\n",
    )
    test_materials.activate(service, owner, manager, pid, aid, [main, other, docs])
    links = InteractiveKnowledgeService(service)
    catalog = links.catalog(owner, pid, aid)
    revision = catalog["documents"][0]["revision_id"]
    doc = links.document(owner, pid, revision, aid)
    chapter = next(s for s in doc["sections"] if s["name"] == "收费标准设置")
    unrelated = next(s for s in doc["sections"] if s["name"] == "无关联章节")
    version = catalog["selection_version"]
    for pack, selector, label, state, title in (
        (main, "body", "收费标准设置", "", "收费标准文档"),
        (main, "#formula", "计费公式", "新增", "新增时选择公式"),
        (main, "#formula-edit", "计费公式", "编辑", "编辑时选择公式"),
        (other, "body", "收费台", "", "其他引用"),
        (other, "#payment", "收款", "", "本次排除的引用"),
    ):
        form = InteractiveBinding(
            analysis_set_id=aid,
            package_id=pack["artifact_id"],
            target={"route": "/", "frame_path": [], "selector": selector, "label": label, "state": state},
            point={"title": title, "summary": "章节业务说明"},
            section_revision_ids=[chapter["revision_id"]],
        )
        binding = links.save(owner, pid, form, str(version), uid())
        version = binding["selection_version"]
        if selector == "#payment":
            with service.db() as s:
                s.get(Relation, binding["binding_id"]).excluded = True
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium_path(p), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 950})
        page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
        errors, ai = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda req: ai.append(req.url) if req.url.endswith("/assist") else None)
        try:
            login(page, url, pid, aid, f"&doc={revision}&reader={reader_mode}")
            article = page.locator(".document-reader")
            selected = article.locator("#section-" + chapter["revision_id"])
            launch = selected.get_by_role("button", name="查看原型入口", exact=True)
            expect(launch).to_be_visible()
            expect(selected.locator(".chapter-heading")).to_contain_text("收费标准设置")
            expect(page.locator(".related-sidebar")).to_have_count(0)
            expect(
                article.locator("#section-" + unrelated["revision_id"]).get_by_role(
                    "button", name="查看原型入口", exact=True
                )
            ).to_have_count(0)
            launch.scroll_into_view_if_needed()
            scroll = "e => (e.closest('.hb-main') || e).scrollTop"
            position = article.evaluate(scroll)
            assert position > 300
            source_hash = parse_qs(urlsplit(page.url).fragment)
            launch.click()
            dialog = page.get_by_role("dialog", name="原型入口", exact=True)
            expect(dialog.get_by_role("heading", name="收费配置 / 收费标准设置", exact=True)).to_be_visible()
            expect(dialog.get_by_text("缴费工作台 / 收费台", exact=True)).not_to_be_visible()
            expect(dialog.get_by_text("新增时选择公式", exact=True)).not_to_be_visible()
            assert parse_qs(urlsplit(page.url).fragment) == source_hash
            dialog.get_by_text("关联控件（2）", exact=True).click()
            expect(dialog.get_by_text("新增时选择公式", exact=True)).to_be_visible()
            expect(dialog.get_by_text("编辑时选择公式", exact=True)).to_be_visible()
            dialog.get_by_text("其他引用本章的页面（1）", exact=True).click()
            expect(dialog.get_by_text("缴费工作台 / 收费台", exact=True)).to_be_visible()
            expect(dialog.get_by_text("收款", exact=True)).to_have_count(0)
            page.clock.install()
            dialog.get_by_role("article", name="收费配置 / 收费标准设置", exact=True).locator("header").get_by_role(
                "button", name="打开原型", exact=True
            ).click()
            expect(page.get_by_text("已定位到 收费标准设置", exact=True)).to_be_visible()
            expect(page.get_by_role("complementary", name="知识助手")).not_to_be_visible()
            page.clock.fast_forward(6100)
            expect(page.locator(".toast")).to_have_count(0)

            def check_return():
                expect(launch).to_be_in_viewport()
                page.wait_for_function(
                    "(top) => { const r=document.querySelector('.document-reader'); return r && Math.abs((r.closest('.hb-main') || r).scrollTop - top) <= 2; }",
                    arg=position,
                )
                assert parse_qs(urlsplit(page.url).fragment) == source_hash

            page.locator(".contextbar").get_by_role("button", name="返回文档", exact=True).click()
            check_return()
            launch.click()
            dialog.get_by_text("关联控件（2）", exact=True).click()
            dialog.get_by_role("listitem").filter(has_text="编辑时选择公式").get_by_role(
                "button", name="打开原型", exact=True
            ).click()
            expect(page.locator('.navigation-feedback[data-phase="degraded"]')).to_contain_text(
                "控件可见，无法核验指定状态"
            )
            source = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            expect(source.locator("#formula-edit")).to_be_focused()
            expect(page.get_by_role("complementary", name="知识助手")).not_to_be_visible()
            page.go_back(wait_until="commit")
            check_return()
            assert not ai and not errors
        finally:
            browser.close()
