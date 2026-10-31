from hashlib import sha256
from packages.analyzers.vks_ingest import parse_bundle
from packages.collector.snapshots import Snapshot, bundle_bytes


def parse(files):
    return parse_bundle(bundle_bytes(Snapshot("local", "fixed", files), "test", "ui", source_key="ui"), "test")


def test_unmarked_html_uses_source_path_and_unique_ids_without_fabricating_dom_attributes():
    raw = b'<!doctype html>\r\n<title>Payments</title>\r\n<main><button id="pay">Pay</button><button>Unknown</button></main>'
    result = parse({"ui.html": raw})
    page = next(e for e in result.entities if e["type"] == "page")
    actions = [e for e in result.entities if e["type"] == "action"]
    assert page["display_name"] == "Payments" and page["metadata"]["discovery_basis"] == "source_path"
    assert len(actions) == 1 and actions[0]["metadata"]["selector"] == "#pay"
    assert actions[0]["locator"] == {"kind": "file_lines", "startLine": 3, "endLine": 3}
    assert result.evidence[0]["sha256"] == sha256(raw).hexdigest()
    assert result.relations[0]["causal_basis"] == "not_established"
    assert any(i["code"] == "VKS_UI_DISCOVERY_GAP" for i in result.issues)
    assert result.artifacts[0]["content"] == raw
    again = parse({"ui.html": raw.replace(b">Pay<", b">Next label<")})
    assert {e["stable_key"] for e in result.entities} == {e["stable_key"] for e in again.entities}


def test_explicit_keys_precede_labels_ambiguous_anchors_are_not_merged_and_hidden_content_ignored():
    content = b"""<main data-vks-page="ui.page.a"><button data-vks-action="ui.action.a">Pay</button>
    <button id="dupe">Pay</button><button id="dupe">Pay</button>
    <div hidden><button data-vks-action="ui.action.hidden">Secret</button></div>
    <script><button data-vks-action="ui.action.script">Script</button></script></main>"""
    result = parse({"a.html": content})
    assert {e["stable_key"] for e in result.entities} == {"ui.page.a", "ui.action.a"}
    repeated = parse({"a.html": content, "b.html": content})
    assert not [e for e in repeated.entities if e["type"] in ("page", "action")]
    assert any(i["code"] == "VKS_UI_DISCOVERY_GAP" for i in repeated.issues)
