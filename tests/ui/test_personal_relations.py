"""Direct relation management stays readable for long project identifiers."""

from test_frontend_browser import site as site  # pytest fixture registration
from test_frontend_browser import chromium_path
from test_product_navigation_design_alignment import sign_in
from playwright.sync_api import expect, sync_playwright
from vks_api.documents import DocumentService


def test_long_relation_names_do_not_overlap_detail_or_overflow_at_four_widths(site):
    service = site.service
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(
        actor, {"key": "personal-relations-ui", "name": "Personal relations"}, "personal-relations-project"
    )
    job = service.demo_import(actor, project["id"], "demo")
    service.process_job(job["id"])
    workspace = service.workspace(actor, project["id"])
    aid = workspace["selected_analysis_set"]["id"]
    document = workspace["documents"][0]
    docs = DocumentService(service)
    current = docs.read_revision(actor, document["entity_id"], document["entity_revision_id"])
    docs.save_draft(
        actor,
        document["entity_id"],
        {
            "analysis_set_id": aid,
            "base_revision_id": current["entity_revision_id"],
            "markdown": current["markdown"].replace("## 查询缴费记录", "## " + "long.section.identifier." * 20),
            "locked_section_ids": [],
        },
        current["row_version"],
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            sign_in(page, site[0], project["id"], "relations")
            listing = page.get_by_label("知识关联列表", exact=True)
            expect(listing.locator("button")).not_to_have_count(0)
            expect(page.get_by_role("button", name="移除关联", exact=True)).to_be_visible()
            for width in (1440, 1024, 768, 375):
                page.set_viewport_size({"width": width, "height": 1000})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert listing.evaluate(
                    "el => { const box=el.getBoundingClientRect(); return [...el.querySelectorAll('button')].every(button => button.getBoundingClientRect().right <= box.right + 1) }"
                )
                assert page.locator(".knowledge-relations-layout").evaluate(
                    "el => {const [list,detail]=el.children; const a=list.getBoundingClientRect(), b=detail.getBoundingClientRect(); return b.left >= a.right - 1 || b.top >= a.bottom - 1}"
                )
        finally:
            browser.close()
