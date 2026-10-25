from hashlib import sha256
from pathlib import Path
import json

import pytest

from packages.analyzers.java_spring import analyze_sources, canonical
from packages.analyzers.vks_ingest import _read_zip, BundleError

ROOT = Path(__file__).resolve().parents[2]


def analyze(files, service="billing"):
    artifacts = []
    for path, content in files.items():
        data = content.encode() if isinstance(content, str) else content
        artifacts.append(
            {"path": path, "kind": "source", "source_id": service, "content": data, "sha256": sha256(data).hexdigest()}
        )
    return analyze_sources(artifacts, [{"source_id": service, "service_key": service}])


def fixture(version="v1"):
    root = ROOT / "fixtures/java-gold" / version
    artifacts = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            service = path.relative_to(root).parts[0]
            artifacts.append(
                {
                    "path": str(path.relative_to(root)),
                    "kind": "source",
                    "source_id": service,
                    "content": data,
                    "sha256": sha256(data).hexdigest(),
                }
            )
    return analyze_sources(artifacts, [{"source_id": s, "service_key": s} for s in ("billing", "support")])


def edges(result, kind):
    entities = {e["stable_key"]: e for e in result.entities}
    return [(entities[r["from_key"]], entities[r["to_key"]]) for r in result.relations if r["type"] == kind]


def test_real_ast_spring_arrays_condition_fingerprints_and_service_isolation():
    result = fixture()
    apis = [e for e in result.entities if e["type"] == "api"]
    assert len(apis) == 11  # 2 query + 4 submit + 2 pending + 2 reflect + 1 independent service
    paid = [e for e in apis if e["metadata"]["conditions"].get("params") == ["!deleted", "state=paid"]]
    assert {e["metadata"]["service_key"] for e in paid} == {"billing", "support"}
    assert len({e["stable_key"] for e in paid}) == 3
    for api in paid:
        conditions = api["metadata"]["conditions"]
        assert conditions["headers"] == ["X-Tenant"]
        assert conditions["consumes"] == ["application/json"]
        assert api["metadata"]["condition_fingerprint"] == sha256(canonical(conditions)).hexdigest()
    posts = [e for e in apis if e["metadata"]["method"] == "POST"]
    assert len(posts) == 4
    assert all(e["metadata"]["conditions"]["headers"] == ["!X-Debug", "X-Tenant"] for e in posts)
    assert all(e["metadata"]["conditions"]["consumes"] == ["application/xml"] for e in posts)
    assert all(r["causal_basis"] != "correlated_runtime" and "review_status" not in r for r in result.relations)


def test_calls_keep_overloads_and_unknown_library_return_as_explicit_gap():
    result = fixture()
    calls = [(a["display_name"], b["display_name"]) for a, b in edges(result, "may_call")]
    assert ("fixture.billing.InvoiceController.submit(double)", "fixture.billing.InvoiceService.save(double)") in calls
    assert (
        "fixture.billing.InvoiceController.submit(double)",
        "fixture.billing.InvoiceService.save(String)",
    ) not in calls
    assert ("fixture.billing.InvoiceController.records(String)", "fixture.billing.InvoiceService.find(String)") in calls
    assert not any("ambiguous(" in a for a, b in calls)
    gaps = [i for i in result.issues if i["code"] == "VKS_JAVA_UNRESOLVED_CALL"]
    assert any(i["resolution_state"] == "ambiguous" for i in gaps)
    assert any(i["resolution_state"] == "dynamic" for i in gaps)
    assert result.metrics["unresolved_calls"] > 0
    assert result.metrics["resolved_calls"] > 0


def test_mybatis_include_result_map_namespace_sql_and_dynamic_boundaries():
    result = fixture()
    statements = {e["display_name"]: e for e in result.entities if e["type"] == "sql"}
    static = statements["fixture.billing.InvoiceMapper.find"]
    assert static["content"].startswith("SELECT id, amount FROM demo_invoice")
    assert static["metadata"]["include_refs"] == ["fixture.billing.InvoiceMapper.columns"]
    assert static["metadata"]["result_maps"]["invoice"]["mappings"][1]["column"] == "amount"
    dynamic = statements["fixture.billing.InvoiceMapper.dynamic"]
    assert dynamic["metadata"]["resolution_state"] == "dynamic"
    assert dynamic["metadata"]["dynamic_tags"] == ["if", "where"]
    assert not any(a["stable_key"] == dynamic["stable_key"] for a, _ in edges(result, "reads_table"))
    assert {b["display_name"] for _, b in edges(result, "reads_table")} == {"demo_invoice"}
    assert len(edges(result, "maps_to_sql")) == 4
    assert len(edges(result, "writes_table")) == 2
    xml = (ROOT / "fixtures/java-gold/v1/billing/InvoiceMapper.xml").read_text().splitlines()
    for statement in statements.values():
        loc = statement["locator"]
        assert f'id="{statement["display_name"].rsplit(".", 1)[1]}"' in xml[loc["startLine"] - 1]
        assert xml[loc["endLine"] - 1].strip().startswith("</")


def test_method_file_move_preserves_stable_identity_but_locator_and_source_change():
    before, after = fixture("v1"), fixture("v2")
    select = lambda result: next(
        e for e in result.entities if e["display_name"] == "fixture.billing.InvoiceService.find(String)"
    )
    old, new = select(before), select(after)
    assert old["stable_key"] == new["stable_key"]
    assert old["artifact_path"] != new["artifact_path"]
    assert old["locator"] != new["locator"]
    assert old["content"] == new["content"]
    old_routes = {e["metadata"]["normalized_route"] for e in before.entities if e["type"] == "api"}
    new_routes = {e["metadata"]["normalized_route"] for e in after.entities if e["type"] == "api"}
    assert "/pay/invoice" in old_routes - new_routes
    assert "/pay/invoice-v2" in new_routes - old_routes
    sql = lambda result: next(e for e in result.entities if e["type"] == "sql" and e["display_name"].endswith(".find"))
    assert sql(before)["stable_key"] == sql(after)["stable_key"]
    assert sql(before)["content"] != sql(after)["content"]


def test_class_and_method_http_conditions_combine_and_media_type_overrides():
    result = analyze(
        {
            "C.java": """import org.springframework.web.bind.annotation.*;
    @RequestMapping(path={"/a","/b"}, method=RequestMethod.GET, params="tenant=demo", consumes="a/type")
    class C { @RequestMapping(path={"/x","/y"}, method=RequestMethod.POST, params="!missing", consumes="b/type") void m() {} }"""
        }
    )
    apis = [e for e in result.entities if e["type"] == "api"]
    assert len(apis) == 8
    assert {e["metadata"]["method"] for e in apis} == {"GET", "POST"}
    assert all(
        e["metadata"]["conditions"] == {"params": ["!missing", "tenant=demo"], "consumes": ["b/type"]} for e in apis
    )


def test_dynamic_paths_custom_annotations_and_same_condition_handler_conflict_are_not_guessed():
    result = analyze(
        {
            "C.java": """import org.springframework.web.bind.annotation.*;
    class C {
      @GetMapping("${runtime}/a") void dynamic(){}
      @GetMapping("/x") void a(){}
      @GetMapping("/x") void b(){}
    }""",
            "Own.java": """class Own { @RequestMapping("/fake") void fake(){} }""",
        }
    )
    apis = [e for e in result.entities if e["type"] == "api"]
    assert len(apis) == 1  # One canonical endpoint; both conflicting source declarations remain.
    assert len(apis[0]["metadata"]["source_declarations"]) == 2
    assert len(edges(result, "handler_of")) == 2
    assert all(e["metadata"]["resolution_state"] == "ambiguous" for e in apis)
    assert all(e["metadata"]["conflicting_handlers"] for e in apis)
    assert any(i["code"] == "VKS_SPRING_DYNAMIC_MAPPING" for i in result.issues)
    assert any(i["code"] == "VKS_SPRING_HANDLER_CONFLICT" for i in result.issues)


def test_lexical_scope_does_not_resolve_a_shadow_variable_outside_its_block():
    result = analyze(
        {
            "C.java": """class A {void ping(){}} class B {void ping(){}}
    class C { A service; void run(){service.ping(); { B service=new B(); service.ping(); this.service.ping(); } service.ping();} }"""
        }
    )
    calls = [(a["display_name"], b["display_name"]) for a, b in edges(result, "may_call")]
    assert set(calls) == {("C.run()", "A.ping()"), ("C.run()", "B.ping()")}
    # Four invocation locations are resolved even though repeated graph edges dedupe.
    assert result.metrics["resolved_calls"] == 4


def test_overloaded_array_parameters_and_constructor_identity_do_not_collapse():
    result = analyze(
        {"C.java": "class C { C(){} void C(){} void m(String a){} void m(String a[]){} void m(int... xs){} }"}
    )
    methods = [e for e in result.entities if e["metadata"].get("kind") in ("method", "constructor")]
    assert len(methods) == 5
    assert len({e["stable_key"] for e in methods}) == 5
    assert {e["metadata"]["signature"] for e in methods} == {"<init>()", "C()", "m(String)", "m(String[])", "m(int...)"}


@pytest.mark.parametrize(
    "xml",
    [
        '<!DOCTYPE mapper [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><mapper namespace="p.M"><select id="x">&xxe;</select></mapper>',
        '<!DOCTYPE mapper [<!ENTITY a "x"><!ENTITY b "&a;&a;">]><mapper namespace="p.M"><select id="x">&b;</select></mapper>',
    ],
)
def test_xxe_and_entity_expansion_are_rejected_without_reading_or_fetching(xml, monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: pytest.fail("No XML network access"))
    result = analyze({"M.xml": xml})
    assert not result.entities
    assert any(i["code"] == "VKS_MYBATIS_UNSAFE_XML" for i in result.issues)


def test_missing_cycle_duplicate_include_and_sql_string_do_not_create_table_facts():
    result = analyze(
        {
            "M.java": "package p; interface M {String a();String b();String c();String d();}",
            "M.xml": """<mapper namespace="p.M">
      <sql id="loop"><include refid="loop"/></sql>
      <select id="a">SELECT <include refid="missing"/> FROM secret_table</select>
      <select id="b">SELECT <include refid="loop"/> FROM secret_table</select>
      <select id="c">SELECT 'data FROM fabricated_table' as label</select>
      <select id="d">SELECT * FROM a, b</select>
    </mapper>""",
        }
    )
    assert not [e for e in result.entities if e["type"] == "table"]
    assert any(i["code"] == "VKS_MYBATIS_INCLUDE" for i in result.issues)


def test_duplicate_namespace_statement_does_not_pick_first_mapper():
    xml = '<mapper namespace="p.M"><select id="a">SELECT * FROM sample_table</select></mapper>'
    result = analyze({"M.java": "package p; interface M {String a();}", "one.xml": xml, "two.xml": xml})
    assert len([e for e in result.entities if e["type"] == "sql"]) == 2
    assert not edges(result, "maps_to_sql")
    assert not edges(result, "reads_table")


def test_non_source_files_cannot_be_parsed_and_syntax_errors_keep_explicit_gap():
    result = analyze({"C.java": "class C { void unfinished("})
    assert not result.entities
    assert any(i["code"] == "VKS_JAVA_SYNTAX" for i in result.issues)
    result = analyze_sources(
        [{"path": "evil.java", "kind": "fixture", "source_id": "b", "content": b"class C {}", "sha256": "0" * 64}],
        [{"source_id": "b"}],
    )
    assert not result.entities


def test_safe_preview_zip_still_enforces_paths_when_manifest_is_optional():
    from io import BytesIO
    from zipfile import ZipFile

    out = BytesIO()
    with ZipFile(out, "w") as archive:
        archive.writestr("index.html", "safe")
    assert _read_zip(out.getvalue(), require_manifest=False) == {"index.html": b"safe"}
    with pytest.raises(BundleError):
        _read_zip(out.getvalue())


def test_route_pattern_and_trailing_slash_are_not_silently_merged():
    result = analyze(
        {
            "C.java": """import org.springframework.web.bind.annotation.*;
    @RequestMapping("/a/") class C { @GetMapping(path={"/x","/x/","/x?"}) void m(){} }"""
        }
    )
    apis = [e for e in result.entities if e["type"] == "api"]
    assert {e["metadata"]["normalized_route"] for e in apis} == {"/a/x", "/a/x/", "/a/x?"}
    assert all(e["metadata"]["resolution_state"] == "resolved" for e in apis)


def test_feign_target_service_and_context_path_are_external_candidates_not_handlers():
    result = analyze(
        {
            "Client.java": """package p;
    import org.springframework.cloud.openfeign.FeignClient;
    import org.springframework.web.bind.annotation.*;
    @FeignClient(name="invoice-service",path="/billing")
    interface Client { @PostMapping("/open") void open(double amount); }"""
        },
        service="gateway",
    )
    api = next(e for e in result.entities if e["type"] == "api")
    assert api["metadata"]["service_key"] == "invoice-service"
    assert api["metadata"]["normalized_route"] == "/billing/open"
    assert api["metadata"]["resolution_state"] == "external"
    assert api["metadata"]["api_origin"] == "feign"
    assert len(edges(result, "may_call")) == 1
    assert not edges(result, "handler_of")


def test_frozen_gold_reports_real_misses_coverage_and_zero_confirmed_relations():
    from packages.analyzers.evaluate_java import evaluate

    report = evaluate()
    assert report["relation_denominator"] == 40
    assert (report["tp"], report["fp"], report["fn"]) == (39, 0, 1)
    assert report["by_relation_type"]["may_call"]["recall"] == 6 / 7
    assert report["call_resolution_coverage"] == 6 / 11
    assert report["confirmed_count"] == 0
    assert not report["independently_human_reviewed"]


def test_endpoint_identity_survives_handler_method_rename():
    original = 'import org.springframework.web.bind.annotation.*; class C {@GetMapping("/x") void oldName(){}}'
    before = analyze({"C.java": original})
    after = analyze({"C.java": original.replace("oldName", "newName")})
    a = next(e for e in before.entities if e["type"] == "api")
    b = next(e for e in after.entities if e["type"] == "api")
    assert a["stable_key"] == b["stable_key"]
    assert a["metadata"]["operation_id"] != b["metadata"]["operation_id"]
