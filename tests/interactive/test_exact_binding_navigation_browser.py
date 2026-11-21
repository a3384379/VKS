"""Real Studio Runtime resolves identical targets by package and restores locator bookmarks."""

from urllib.parse import urlencode

from playwright.sync_api import expect, sync_playwright
from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.models import uid

from tests.interactive import test_materials
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.interactive.test_studio_bundle import zip_resources
from tests.ui.test_frontend_browser import chromium_path

materials = test_materials.materials


def test_exact_package_edit_and_binding_bookmark_reveal(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    packs = [
        test_materials.stage(
            service,
            owner,
            manager,
            pid,
            aid,
            "prototype",
            zip_resources(
                **{
                    "index.html": f"<title>起始页 {name}</title><a href='detail.html'>打开明细</a>",
                    "detail.html": (
                        "<title>明细</title><button id='add'>Add</button>"
                        "<button id='open' onclick=\"dialog.showModal(); count.textContent=Number(count.textContent)+1\">Open</button>"
                        "<p id='count'>0</p><dialog id='dialog'><input aria-label='Exact dialog field' id='field'></dialog>"
                    ),
                }
            ),
            name=name,
            filename="site.zip",
        )
        for name in ("Package A", "Package B")
    ]
    doc = test_materials.stage(service, owner, manager, pid, aid, "documents", "# 规则\n\n先校验名称。")
    test_materials.activate(service, owner, manager, pid, aid, [*packs, doc])
    links = InteractiveKnowledgeService(service)
    catalog = links.catalog(owner, pid, aid)
    revision = catalog["documents"][0]["revision_id"]
    section = links.document(owner, pid, revision, aid)["sections"][0]["revision_id"]

    def save(pack, title, selector, label, steps=()):
        result = links.save(
            owner,
            pid,
            InteractiveBinding(
                analysis_set_id=aid,
                package_id=pack["artifact_id"],
                target={
                    "route": "/detail.html",
                    "frame_path": ["actualPage"],
                    "selector": selector,
                    "label": label,
                    "open_steps": list(steps),
                },
                point={"title": title, "summary": "独立说明 " + title},
                section_revision_ids=[section],
            ),
            links.catalog(owner, pid, aid)["selection_version"],
            uid(),
        )
        return result["binding_id"]

    a_id = save(packs[0], "Rule A", "#add", "Add")
    b_id = save(packs[1], "Rule B", "#add", "Add")
    dialog_id = save(packs[1], "Exact popup", "#field", "Exact dialog field", ("#open",))
    before = {row["id"]: row for row in links.bindings(owner, pid, aid)}

    def frame(page):
        return (
            page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material").frame_locator("#actualPage")
        )

    def bookmark(bind, release_id=""):
        query = {"project": pid, "view": "prototype", "binding": bind}
        query.update({"release": release_id} if release_id else {"analysis": aid})
        return "/#" + urlencode(query)

    def assert_dialog(page):
        expect(page.get_by_label("当前原型包")).to_have_value(packs[1]["package_id"])
        expect(frame(page).get_by_role("textbox", name="Exact dialog field")).to_be_focused()
        expect(frame(page).locator("#count")).to_have_text("1")

    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 950})
        context.add_init_script("localStorage.setItem('vks.studio.auto-associate', 'off')")
        page = context.new_page()
        try:
            login(page, url, pid, aid)
            page.goto(url + bookmark(b_id))
            add = frame(page).get_by_role("button", name="Add", exact=True)
            expect(add).to_be_focused()
            add.click(button="right")
            page.get_by_role("menuitem", name="手动补充关联", exact=True).click()
            expect(page.get_by_label("业务说明")).to_have_value("独立说明 Rule B")
            page.get_by_label("业务说明").fill("只修改 B")
            assert "package=" not in page.url
            runtime = page.locator('iframe[title="原 HTML 原型"]')
            runtime.evaluate("(frame) => { window.auditRuntime = frame.contentWindow; }")
            delayed_reload = []
            binding_requests = "**/interactive/bindings?**"
            page.route(binding_requests, lambda request: delayed_reload.append(request))
            with page.expect_response(lambda r: "/interactive/catalog?" in r.url):
                page.get_by_role("button", name="保存关联", exact=True).click()
            page.wait_for_function(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))"
            )
            assert len(delayed_reload) == 1
            expect(page.get_by_label("当前原型包")).to_have_value(packs[1]["package_id"])
            delayed_reload[0].continue_()
            page.unroute(binding_requests)
            expect(page.get_by_text("关联已保存，刷新后仍可查看", exact=True)).to_be_visible()
            expect(add).to_be_visible()
            assert runtime.evaluate("(frame) => window.auditRuntime === frame.contentWindow")
            after = {row["id"]: row for row in links.bindings(owner, pid, aid)}
            assert after[a_id] == before[a_id]
            assert after[b_id]["summary"] == "只修改 B"

            page.goto(url + bookmark(dialog_id))
            assert_dialog(page)
            page.reload()
            assert_dialog(page)
            release = manager.checkpoint(owner, pid, aid, "固定深链验证", uid())
            frozen_id = next(
                row["id"]
                for row in links.bindings(owner, pid, release_id=release["id"])
                if row["title"] == "Exact popup"
            )
            another = context.new_page()
            delayed = []
            another.route("**/interactive/bindings?**", lambda route: delayed.append(route))
            another.goto(url + bookmark(frozen_id, release["id"]))
            # Deliberately let the default Runtime load before binding discovery.
            expect(frame(another).get_by_role("link", name="打开明细")).to_be_visible()
            assert len(delayed) == 1
            delayed[0].continue_()
            assert_dialog(another)
        finally:
            browser.close()
