"""Selections retain the surrounding graph and full-document reading context."""

from playwright.sync_api import expect

from test_audit_navigation import audit_site as audit_site, signed_page  # pytest fixture registration
from test_frontend_browser import site as site  # pytest fixture registration
from packages.collector.snapshots import Snapshot, bundle_bytes
from vks_api.documents import DocumentService
from vks_api.knowledge import KnowledgeService


def test_graph_node_selection_preserves_nodes_positions_zoom_and_scroll(audit_site):
    s = audit_site
    service, owner, pid = s["service"], s["owner"], s["pid"]
    clone = KnowledgeService(service).clone(owner, pid, s["a"], "Selection map", "selection-map")
    aid = clone["id"]
    html = (
        '<main data-vks-page="ui.page.selection"><h1>Selection map</h1>'
        + "".join(f'<button data-vks-action="ui.action.selection-{i}">Selection {i}</button>' for i in range(55))
        + "</main>"
    ).encode()
    raw = bundle_bytes(
        Snapshot("local", "selection-map", {"selection.html": html}),
        "charge-demo",
        "selection-map",
        source_key="selection-ui",
        role="prototype",
    )
    with service.db() as db:
        service._commit_bundle(db, pid, owner.id, service._parse(raw, "charge-demo"), aid)
    workspace = service.workspace(owner, pid, aid)
    root = next(e for e in workspace["pages"] if e["stable_key"] == "ui.page.selection")
    requests = []

    def prepare(page):
        page.on("request", lambda r: requests.append(r.url) if "/knowledge-map?" in r.url else None)

    with signed_page(
        s, f"/#view=graph&project={pid}&baseline=draft:{aid}&entity={root['id']}", prepare=prepare
    ) as page:
        expect(page.locator(".graph-node")).to_have_count(56)
        page.get_by_role("button", name="放大图谱", exact=True).click()
        count = len(requests)

        def frame():
            return page.locator(".v2-graph-scroll").evaluate("""el => ({
                top: el.scrollTop, left: el.scrollLeft,
                transform: el.querySelector('.graph-inner').style.transform,
                nodes: [...el.querySelectorAll('.graph-node')].map(node => ({
                    key: node.querySelector('small').textContent,
                    left: node.style.left, top: node.style.top,
                })),
            })""")

        for key in ("ui.action.selection-25", "ui.action.selection-26", "ui.action.selection-25"):
            node = page.locator(".graph-node").filter(has=page.get_by_text(key, exact=True))
            node.scroll_into_view_if_needed()
            before = frame()
            node.click()
            expect(page.locator(".graph-detail .logical-key")).to_have_text(key)
            after = frame()
            assert after["nodes"] == before["nodes"]
            assert after["transform"] == before["transform"]
            assert abs(after["top"] - before["top"]) <= 1
            assert abs(after["left"] - before["left"]) <= 1
            assert len(requests) == count
            expect(node).to_have_attribute("aria-pressed", "true")

        with page.expect_response(lambda r: "/knowledge-map?" in r.url):
            page.get_by_role("button", name="以此节点展开", exact=True).click()
        expect(page.locator(".graph-node")).to_have_count(2)
        assert len(requests) == count + 1


def test_document_directory_keeps_full_body_and_scrolls_to_exact_heading(site):
    url, pid, _, _ = site
    service = site.service
    _, owner = service.login(service.bootstrap_token)
    workspace = service.workspace(owner, pid)
    doc = workspace["documents"][0]
    aid = workspace["selected_analysis_set"]["id"]
    docs = DocumentService(service)
    old = docs.read_revision(owner, doc["entity_id"], doc["entity_revision_id"])
    markdown = (
        "# Directory fixture\n\nFull document introduction.\n\n## Parent without body\n\n"
        "### Child with body\n\nChild body must remain visible in the full document.\n\n"
        + "\n\n".join(f"Paragraph {i}: full surrounding context." for i in range(35))
        + "\n\n## Repeated heading\n\nFirst repeated section.\n\n"
        + "\n\n".join(f"Middle paragraph {i}." for i in range(20))
        + "\n\n## Repeated heading\n\nSecond repeated section.\n\n## Final section\n\nFinal body marker.\n\n"
        + "\n\n".join(f"## Appendix {i}\n\nAppendix body {i}." for i in range(45))
    )
    saved = docs.save_draft(
        owner,
        doc["entity_id"],
        dict(analysis_set_id=aid, base_revision_id=old["entity_revision_id"], markdown=markdown, locked_section_ids=[]),
        old["row_version"],
    )
    sections = saved["sections"]
    parent = next(e for e in sections if e["name"] == "Parent without body")
    repeated = [e for e in sections if e["name"] == "Repeated heading"]
    path = f"/#view=documents&project={pid}&baseline=draft:{aid}&entity={doc['entity_id']}"
    with signed_page(dict(url=url), path) as page:
        body = page.locator(".document-reader > .markdown")
        expect(body).to_contain_text("Final body marker.")
        before = body.inner_text()
        for section in (parent, *repeated):
            index = next(i for i, e in enumerate(sections) if e["entity_id"] == section["entity_id"])
            page.locator(".section-tree button").nth(index).click()
            expect(page.locator(".section-tree button.selected")).to_contain_text(section["name"])
            assert body.inner_text() == before
            heading = body.locator(f'[data-source-line="{section["locator"]["startLine"]}"]')
            expect(heading).to_be_visible()
            position = heading.bounding_box()
            assert position and 0 <= position["y"] < 180
            expect(heading).to_be_focused()
            tree = page.locator(".document-tree")
            tree_position = tree.bounding_box()
            assert tree_position and 0 <= tree_position["y"] <= 32
            assert "entity=" + section["entity_id"] in page.url
        page.reload()
        expect(body).to_contain_text("Child body must remain visible")
        heading = body.locator(f'[data-source-line="{repeated[-1]["locator"]["startLine"]}"]')
        expect(heading).to_be_visible()
        assert heading.bounding_box()["y"] < 180
        directory = page.locator(".document-tree-content")
        assert directory.evaluate("el => el.scrollHeight > el.clientHeight")
        scroll = page.evaluate("window.scrollY")
        directory.evaluate("el => { el.scrollTop = el.scrollHeight; }")
        expect(page.locator(".section-tree button").last).to_be_in_viewport()
        assert page.evaluate("window.scrollY") == scroll
        page.set_viewport_size(dict(width=375, height=900))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert directory.bounding_box()["height"] <= 900 * 0.35 + 1
        assert docs.read_revision(owner, doc["entity_id"], old["entity_revision_id"]) == old
