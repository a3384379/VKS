"""Real Studio Runtime resolves identical targets by package and restores locator bookmarks."""

from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import expect, sync_playwright
from vks_api import identity
from vks_api.interactive import InteractiveBinding, InteractiveKnowledgeService
from vks_api.interactive_ai import AssistanceInput
from vks_api.models import Artifact, uid
from vks_api.project_tools import DocumentRead, ProjectTools
from vks_api.prototype_previews import PrototypePreviews

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
                            "<button id='show-a' onclick=\"modeA.hidden=false; modeB.hidden=true\">Show state A</button>"
                            "<button id='show-b' onclick=\"modeA.hidden=true; modeB.hidden=false\">Show state B</button>"
                            "<div role='dialog' id='modeA' hidden>Mode A content</div>"
                            "<div role='dialog' id='modeB' hidden>Mode B content</div>"
                            f"<p>{name} details</p>"
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

    def save(pack, title, selector, label, steps=(), state=""):
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
                    "state": state,
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
    dialog_id = save(packs[1], "Exact popup", "#field", "Exact dialog field", ("#open",), "#dialog")
    state_a = save(packs[1], "State A", "#add", "Add", ("#show-a",), "#modeA")
    state_b = save(packs[1], "State B", "#add", "Add", ("#show-b",), "#modeB")
    unknown_state = save(packs[1], "Unknown state", "#add", "Add", state="#missing")
    before = {row["id"]: row for row in links.bindings(owner, pid, aid)}

    def frame(page):
        return (
            page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material").frame_locator("#actualPage")
        )

    def bookmark(bind, release_id="", **extra):
        query = {"project": pid, "view": "prototype", "binding": bind}
        query.update({"release": release_id} if release_id else {"analysis": aid})
        query.update(extra)
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
            expect(frame(page).get_by_text("Package B details", exact=True)).to_be_visible()
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
            expect(page.get_by_text("已核验指定状态并定位到 Exact dialog field")).to_be_visible()
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
            with another.expect_response(lambda response: "/interactive/catalog?" in response.url):
                another.goto(url + bookmark(frozen_id, release["id"]))
            expect(another.locator('iframe[title="原 HTML 原型"]')).to_have_count(0)
            assert len(delayed) == 1
            delayed[0].continue_()
            assert_dialog(another)

            # Follow the exact URLs returned by the same handlers used by
            # MCP. A clean browser must retain the document link across login.
            service.studio_base_url = url + "/"
            tools = ProjectTools(service)
            frozen_document = tools.document(
                owner, pid,
                DocumentRead(
                    release_id=release["id"], document_id=catalog["documents"][0]["id"],
                    revision_id=revision, section_revision_ids=[section], max_chars=800,
                ),
            )
            browser_context = browser.new_context(viewport={"width": 1440, "height": 950})
            browser_context.add_init_script("localStorage.setItem('vks.studio.auto-associate', 'off')")
            deep_link = browser_context.new_page()
            deep_link.goto(frozen_document["sections"][0]["studio_url"])
            deep_link.get_by_label("密码", exact=True).fill("owner")
            deep_link.get_by_role("button", name="打开交互工作台", exact=True).click()
            expect(deep_link.locator("#section-" + section)).to_be_in_viewport()
            expect(deep_link.get_by_text("已定位到指定章节", exact=True)).to_be_visible()
            frozen_binding = tools.context(
                owner, pid,
                AssistanceInput(
                    release_id=release["id"], package_id=packs[1]["artifact_id"],
                    target=before[dialog_id]["target"],
                ),
            )["binding"]
            assert frozen_binding["id"] == frozen_id
            # Cold binding login is independent from the document session.
            binding_context = browser.new_context(viewport={"width": 1440, "height": 950})
            binding_context.add_init_script("localStorage.setItem('vks.studio.auto-associate', 'off')")
            binding_page = binding_context.new_page()
            binding_page.goto(frozen_binding["studio_url"])
            binding_page.get_by_label("密码", exact=True).fill("owner")
            binding_page.get_by_role("button", name="打开交互工作台", exact=True).click()
            assert_dialog(binding_page)
            runtime_origin = next(urlsplit(frame.url) for frame in binding_page.frames
                                  if urlsplit(frame.url).hostname and urlsplit(frame.url).hostname.endswith(".localhost"))
            assert runtime_origin.port != urlsplit(url).port
            binding_context.grant_permissions(["clipboard-read", "clipboard-write"], origin=url)
            binding_page.get_by_role("button", name="复制准确位置", exact=True).click()
            expect(binding_page.get_by_text("已复制准确位置", exact=True)).to_be_visible()
            copied = binding_page.evaluate("navigator.clipboard.readText()")
            assert parse_qs(urlsplit(copied).fragment) == parse_qs(urlsplit(frozen_binding["studio_url"]).fragment)
            binding_page.screenshot(path=tmp_path / "exact-binding.png")
            deep_link.screenshot(path=tmp_path / "exact-section.png")

            # Same route, frame, selector and label. The state's recorded
            # steps are still needed when another visible state is active.
            state_page = context.new_page()
            state_page.goto(url + bookmark(state_a))
            expect(frame(state_page).get_by_text("Mode A content")).to_be_visible()
            expect(state_page.get_by_text("已核验指定状态并定位到 Add")).to_be_visible()
            state_page.goto(url + bookmark(state_b))
            expect(frame(state_page).get_by_text("Mode B content")).to_be_visible()
            expect(frame(state_page).get_by_text("Mode A content")).not_to_be_visible()
            expect(state_page.get_by_text("已核验指定状态并定位到 Add")).to_be_visible()
            state_page.evaluate("history.back()")
            expect(state_page).to_have_url(url + bookmark(state_a))
            expect(frame(state_page).get_by_text("Mode A content")).to_be_visible()
            state_page.goto(url + bookmark(unknown_state))
            expect(frame(state_page).locator("#add")).to_be_visible()
            expect(state_page.get_by_text("控件可见，无法核验指定状态")).to_be_visible()
            state_page.get_by_role("button", name="仅打开页面", exact=True).click()
            expect(state_page.get_by_text("已打开原型页面", exact=True)).to_be_visible()
            frozen_unknown_state = next(
                row["id"] for row in links.bindings(owner, pid, release_id=release["id"])
                if row["title"] == "Unknown state"
            )
            state_page.goto(url + bookmark(frozen_unknown_state, release["id"]))
            expect(state_page.get_by_text("控件可见，无法核验指定状态")).to_be_visible()
            expect(state_page.get_by_role("button", name="复制准确位置", exact=True)).to_have_count(0)
            expect(state_page.get_by_role("button", name="更新当前页预览", exact=True)).to_have_attribute(
                "title", "在隔离环境中生成当前页面的默认状态预览"
            )

            # Block package B's actual content fetch and navigate to A while B's
            # runtime is waiting. Release the pending response once A is active.
            race = context.new_page()
            held = []
            content_b = f"**/interactive/packages/{packs[1]['artifact_id']}/content?**"
            race.route(content_b, lambda request: held.append(request))
            with race.expect_request(lambda request: f"/{packs[1]['artifact_id']}/content?" in request.url):
                race.goto(url + bookmark(dialog_id))
            expect(race.get_by_text("正在等待原型运行环境…", exact=True)).to_be_visible()
            race.goto(url + bookmark(a_id))
            expect(frame(race).get_by_text("Package A details", exact=True)).to_be_visible()
            expect(frame(race).locator("#add")).to_be_focused()
            assert len(held) == 1
            held[0].continue_()
            race.unroute(content_b)
            expect(race.get_by_text("已定位到 Add", exact=True)).to_be_visible()
            race.evaluate("history.back()")
            expect(race).to_have_url(url + bookmark(dialog_id))
            assert_dialog(race)

            # An explicit wrong package never renders either package for the
            # binding. Unknown IDs and malformed requests retain their hash.
            bad = url + bookmark(b_id, package=packs[0]["package_id"])
            state_page.goto(bad)
            expect(state_page.get_by_text("当前版本中无法读取该位置")).to_be_visible()
            expect(state_page.locator('iframe[title="原 HTML 原型"]')).to_have_count(0)
            assert state_page.url == bad
            state_page.goto(url + bookmark(b_id) + "&analysis=conflict")
            expect(state_page.get_by_text("链接参数不完整或格式无效。")).to_be_visible()

            wrong_document = frozen_document["sections"][0]["studio_url"].replace(revision, uid())
            deep_link.goto(wrong_document)
            expect(deep_link.get_by_text("当前版本中无法读取该位置")).to_be_visible()
            assert deep_link.url == wrong_document
            expect(deep_link.locator("#section-" + section)).to_have_count(0)
            deep_link.get_by_role("button", name="打开版本目录", exact=True).click()
            expect(deep_link.locator("article.document-reader").get_by_role("heading", name="选择一篇文档开始阅读")).to_be_visible()
        finally:
            browser.close()


def test_registered_prototype_page_survives_new_snapshot_and_respects_revocation(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    original = test_materials.stage(
        service, owner, manager, pid, aid, "prototype",
        zip_resources(**{
            "index.html": '<a href="terms.html">Open terms</a>',
            "terms.html": '<h1>Frozen terms</h1><button onclick="this.textContent = \'Edited\'">Original form</button>',
        }),
        name="Restricted prototype", filename="preview.zip",
    )
    fallback = test_materials.stage(
        service, owner, manager, pid, aid, "prototype",
        "<h1>Other visible prototype</h1>", name="Other package",
    )
    doc = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "# Terms\n\nFrozen original rule.",
    )
    test_materials.activate(service, owner, manager, pid, aid, [original, fallback, doc])
    with service.db() as db:
        artifact = db.get(Artifact, original["artifact_id"])
        artifact.metadata_json = {**artifact.metadata_json, "requires_source_read": True}
    release = manager.checkpoint(owner, pid, aid, "Frozen release", uid())
    replacement = test_materials.stage(
        service, owner, manager, pid, aid, "prototype",
        zip_resources(**{
            "index.html": '<a href="terms.html">Open new terms</a>',
            "terms.html": '<h1>Updated terms</h1><button>Updated form</button>',
        }),
        package_id=original["package_id"], filename="preview.zip",
    )
    updated_doc = test_materials.stage(
        service, owner, manager, pid, aid, "documents",
        "# Terms\n\nUpdated rule.", package_id=doc["package_id"],
    )
    test_materials.activate(service, owner, manager, pid, aid, [replacement, updated_doc])
    latest = manager.checkpoint(owner, pid, aid, "New release", uid())
    reader = identity.create_user(
        service, owner, {"username": "link-reader", "display_name": "Link Reader", "password": "Synthetic read password"},
    )
    service.set_member(owner, pid, {"user_id": reader["id"], "role": "viewer", "status": "active"},
                       service.get_project(owner, pid)["row_version"])

    def grant(permissions):
        identity.set_member_permissions(
            service, owner, pid, {"user_id": reader["id"], "permissions": permissions},
            service.get_project(owner, pid)["row_version"],
        )

    grant(["source:read"])
    previews = PrototypePreviews(service)
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        service.studio_base_url = url + "/"
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 950})
        context.add_init_script("localStorage.setItem('vks.studio.auto-associate', 'off')")
        page = context.new_page()
        try:
            frozen_page = next(item for item in previews.studio_pages(
                owner, pid, None, release["id"], original["package_id"], None, None, None, 0, 20,
            )["items"] if item["route"] == "/terms.html")
            updated_page = next(item for item in previews.studio_pages(
                owner, pid, None, latest["id"], original["package_id"], None, None, None, 0, 20,
            )["items"] if item["route"] == "/terms.html")
            assert frozen_page["page_ref"] != updated_page["page_ref"]
            page.goto(frozen_page["studio_url"])
            page.get_by_label("登录名", exact=True).fill("link-reader")
            page.get_by_label("密码", exact=True).fill("Synthetic read password")
            page.get_by_role("button", name="打开交互工作台", exact=True).click()
            material = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material").frame_locator("#actualPage")
            expect(material.get_by_role("heading", name="Frozen terms")).to_be_visible()
            expect(page.get_by_text("已打开原型页面", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="更新当前页预览", exact=True)).to_be_visible()
            material.get_by_role("button", name="Original form").click()
            expect(material.get_by_role("button", name="Edited")).to_be_visible()

            page.goto(updated_page["studio_url"])
            expect(material.get_by_role("heading", name="Updated terms")).to_be_visible()
            page.evaluate("history.back()")
            expect(page).to_have_url(frozen_page["studio_url"])
            expect(material.get_by_role("button", name="Original form")).to_be_visible()
            expect(page.get_by_text("已打开原型页面", exact=True)).to_be_visible()
            page.screenshot(path=tmp_path / "exact-unbound-page.png")

            links = InteractiveKnowledgeService(service)
            original_catalog = links.catalog(owner, pid, release_id=release["id"])["documents"][0]
            original_detail = links.document(owner, pid, original_catalog["revision_id"], release_id=release["id"])
            old_chapter = original_detail["sections"][0]["revision_id"]
            doc_link = ProjectTools(service).document(
                owner, pid, DocumentRead(
                    release_id=release["id"],
                    document_id=original_catalog["id"],
                    revision_id=original_catalog["revision_id"],
                    section_revision_ids=[old_chapter],
                    max_chars=800,
                ),
            )["sections"][0]["studio_url"]
            page.goto(doc_link)
            expect(page.locator("#section-" + old_chapter)).to_be_in_viewport()
            expect(page.get_by_text("Frozen original rule.", exact=True)).to_be_visible()

            unknown = frozen_page["studio_url"].replace(frozen_page["page_ref"], "f" * 64)
            page.goto(unknown)
            expect(page.get_by_text("当前版本中无法读取该位置", exact=True)).to_be_visible()
            expect(page.locator('iframe[title="原 HTML 原型"]')).to_have_count(0)
            assert page.url == unknown

            grant([])
            page.goto(frozen_page["studio_url"])
            expect(page.get_by_text("当前版本中无法读取该位置", exact=True)).to_be_visible()
            expect(page.locator('iframe[title="原 HTML 原型"]')).to_have_count(0)
            assert page.url == frozen_page["studio_url"]
        finally:
            browser.close()


def test_expired_studio_session_hides_old_workspace_and_restores_exact_target(materials, tmp_path, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _ = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype)
    release = manager.checkpoint(owner, pid, aid, "Session recovery", uid())
    links = InteractiveKnowledgeService(service)
    catalog = links.catalog(owner, pid, release_id=release["id"])
    doc = catalog["documents"][0]
    detail = links.document(owner, pid, doc["revision_id"], release_id=release["id"])
    chapters = [section["revision_id"] for section in detail["sections"]]
    binding = links.bindings(owner, pid, release_id=release["id"])[0]
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as pw:
        service.studio_base_url = url + "/"
        browser = pw.chromium.launch(executable_path=chromium_path(pw), headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 950})
        context.add_init_script("localStorage.setItem('vks.studio.auto-associate', 'off')")
        page = context.new_page()
        try:
            tools = ProjectTools(service)
            document_links = tools.document(
                owner, pid, DocumentRead(
                    release_id=release["id"], document_id=doc["id"], revision_id=doc["revision_id"],
                    section_revision_ids=chapters, max_chars=2000,
                ),
            )["sections"]
            prototype_link = tools.context(
                owner, pid, AssistanceInput(
                    release_id=release["id"], package_id=prototype["artifact_id"], target=binding["target"],
                ),
            )["binding"]["studio_url"]

            page.goto(document_links[0]["studio_url"])
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开交互工作台", exact=True).click()
            expect(page.locator("#section-" + chapters[0])).to_be_in_viewport()
            expect(page.get_by_text("新增收费项需要填写名称。", exact=True)).to_be_visible()

            # A request from the first authenticated workspace receives its
            # response only after the authenticated workspace has been rebuilt.
            catalog_requests = "**/interactive/catalog?**"
            stale = []

            def hold_first_catalog(route):
                if stale:
                    route.continue_()
                else:
                    stale.append(route)

            page.route(catalog_requests, hold_first_catalog)
            with page.expect_request(lambda request: "/interactive/catalog?" in request.url):
                page.goto(document_links[1]["studio_url"])

            context.clear_cookies()
            session_requests = "**/api/v1/auth/session"
            inspection = []

            def hold_session_inspection(route):
                if route.request.method == "GET":
                    inspection.append(route)
                else:
                    route.continue_()

            page.route(session_requests, hold_session_inspection)
            with page.expect_request(lambda request: request.url.endswith("/auth/session")):
                page.goto(prototype_link)
            expect(page.get_by_text("正在确认登录状态…", exact=True)).to_be_visible()
            expect(page.locator("article.document-reader")).to_have_count(0)
            expect(page.locator('iframe[title="原 HTML 原型"]')).to_have_count(0)
            expect(page).to_have_url(prototype_link)
            assert len(stale) == len(inspection) == 1
            inspection[0].continue_()
            page.unroute(session_requests)

            expect(page.get_by_text("会话已失效，请重新登录。", exact=True)).to_be_visible()
            expect(page.locator("article.document-reader")).to_have_count(0)
            expect(page.locator('iframe[title="原 HTML 原型"]')).to_have_count(0)
            expect(page).to_have_url(prototype_link)
            page.get_by_label("密码", exact=True).fill("owner")
            page.get_by_role("button", name="打开交互工作台", exact=True).click()
            material = page.frame_locator('iframe[title="原 HTML 原型"]').frame_locator("#material")
            expect(material.get_by_role("button", name="新增收费项")).to_be_focused()
            expect(page.get_by_text("已定位到 新增收费项", exact=True)).to_be_visible()

            late_probes = []

            def record_session_probe(request):
                if request.method == "GET" and request.url.endswith("/auth/session"):
                    late_probes.append(request)

            page.on("request", record_session_probe)
            with page.expect_response(lambda response: "/interactive/catalog?" in response.url and response.status == 401):
                stale[0].fulfill(status=401, json={"error": {"code": "VKS_UNAUTHENTICATED", "message": "Old session"}})
            page.unroute(catalog_requests)
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            assert not late_probes
            page.remove_listener("request", record_session_probe)
            expect(material.get_by_role("button", name="新增收费项")).to_be_focused()
            expect(page.get_by_label("密码", exact=True)).to_have_count(0)
            expect(page).to_have_url(prototype_link)

            # After the route's session check and catalog succeed, expire the
            # server session while the runtime's binary request is starting.
            page.goto(document_links[0]["studio_url"])
            expect(page.locator("#section-" + chapters[0])).to_be_in_viewport()
            session_cookie = next(row for row in context.cookies(url) if row["name"] == "vks_session")
            browser_actor = service.authenticate(session_cookie["value"], "session")
            asset_request = f"**/interactive/packages/{prototype['artifact_id']}/content?**"

            def expire_before_content(route):
                service.logout(browser_actor)
                route.continue_()

            page.route(asset_request, expire_before_content, times=1)
            with page.expect_response(lambda response: "/content?" in response.url and response.status == 401):
                page.goto(prototype_link)
            expect(page.get_by_text("会话已失效，请重新登录。", exact=True)).to_be_visible()
            expect(page.locator('iframe[title="原 HTML 原型"]')).to_have_count(0)
            expect(page).to_have_url(prototype_link)
        finally:
            browser.close()
