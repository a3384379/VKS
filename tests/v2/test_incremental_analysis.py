from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from packages.analyzers.java_spring import analyze_sources
from packages.analyzers.syntax_cache import MemoryUnitCache, cache_scope
from test_journeys import store

ROOT = Path(__file__).resolve().parents[2]


def materials(version="v1"):
    root = ROOT / "fixtures/java-gold" / version
    artifacts = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            raw = path.read_bytes()
            artifacts.append(
                dict(
                    path=str(path.relative_to(root)),
                    kind="source",
                    source_id=path.relative_to(root).parts[0],
                    content=raw,
                    sha256=sha256(raw).hexdigest(),
                )
            )
    return artifacts, [dict(source_id=s, service_key=s) for s in ("billing", "support")]


def facts(result):
    return deepcopy((result.entities, result.evidence, result.relations, result.issues, result.supported_paths))


def test_incremental_cache_matches_cold_gold_and_reparses_only_changed_files():
    artifacts, sources = materials()
    cache = MemoryUnitCache()
    with cache_scope(cache):
        cold = analyze_sources(artifacts, sources)
        warm = analyze_sources(artifacts, sources)
    assert facts(warm) == facts(cold)
    assert warm.metrics["syntax_parsed_files"] == 0
    assert warm.metrics["syntax_reused_files"] == len(artifacts)
    changed = deepcopy(artifacts)
    mapper = next(a for a in changed if a["path"] == "billing/InvoiceMapper.xml")
    mapper["content"] = mapper["content"].replace(b"demo_invoice", b"changed_invoice")
    mapper["sha256"] = sha256(mapper["content"]).hexdigest()
    with cache_scope(cache):
        incremental = analyze_sources(changed, sources)
    reference = analyze_sources(changed, sources)
    assert facts(incremental) == facts(reference)
    assert incremental.metrics["syntax_parsed_files"] == 1
    assert incremental.metrics["syntax_reused_files"] == len(artifacts) - 1
    assert any(e["metadata"].get("table_name") == "changed_invoice" for e in incremental.entities)
    assert not any(e["metadata"].get("table_name") == "demo_invoice" for e in incremental.entities)


def test_cached_java_does_not_freeze_cross_file_resolution_or_service_identity():
    artifacts, sources = materials()
    cache = MemoryUnitCache()
    with cache_scope(cache):
        analyze_sources(artifacts, sources)
    changed = [deepcopy(a) for a in artifacts if a["path"] != "billing/InvoiceService.java"]
    moved = next(a for a in changed if a["path"] == "billing/InvoiceController.java")
    moved["path"] = "billing/moved/InvoiceController.java"
    with cache_scope(cache):
        actual = analyze_sources(changed, sources)
    expected = analyze_sources(changed, sources)
    assert facts(actual) == facts(expected)
    assert actual.metrics["syntax_parsed_files"] == 0
    assert actual.metrics["unresolved_calls"] > 0
    with cache_scope(cache):
        other_service = analyze_sources(artifacts, [dict(s, service_key="other-" + s["source_id"]) for s in sources])
    assert not ({e["stable_key"] for e in actual.entities} & {e["stable_key"] for e in other_service.entities})


def test_corrupted_cached_unit_is_reparsed_and_never_deserialized_as_code():
    artifacts, sources = materials()
    cache = MemoryUnitCache()
    with cache_scope(cache):
        expected = analyze_sources(artifacts, sources)
    for key in cache.values:
        cache.values[key] = {"payload": "not executable and not a syntax tree"}
    with cache_scope(cache):
        actual = analyze_sources(artifacts, sources)
    assert facts(actual) == facts(expected)
    assert actual.metrics["syntax_parsed_files"] == len(artifacts)


def test_one_percent_source_change_reparses_one_file_with_identical_cold_facts():
    artifacts = []
    for i in range(100):
        raw = f"package demo; public class Source{i} {{ public int value() {{ return {i}; }} }}".encode()
        artifacts.append(
            dict(
                path=f"billing/Source{i}.java",
                kind="source",
                source_id="billing",
                content=raw,
                sha256=sha256(raw).hexdigest(),
            )
        )
    sources = [dict(source_id="billing", service_key="billing")]
    cache = MemoryUnitCache()
    with cache_scope(cache):
        analyze_sources(artifacts, sources)
    artifacts[0] = dict(artifacts[0], content=artifacts[0]["content"].replace(b"return 0", b"return 1"))
    artifacts[0]["sha256"] = sha256(artifacts[0]["content"]).hexdigest()
    with cache_scope(cache):
        actual = analyze_sources(artifacts, sources)
    assert actual.metrics["syntax_parsed_files"] == 1
    assert actual.metrics["syntax_reused_files"] == 99
    assert actual.metrics["dependency_recomputed_files"] == 100
    assert facts(actual) == facts(analyze_sources(artifacts, sources))


def test_job_reuses_project_private_units_preserves_decisions_and_recovers_corruption(store):
    from sqlalchemy import select
    from vks_api.jobs import Checkpoint
    from vks_api.models import Job

    service, actor, pid, w, _, _ = store
    aid = w["selected_analysis_set"]["id"]
    with service.db() as db:
        imported = db.scalar(select(Job).where(Job.project_id == pid, Job.kind == "import"))
        assert imported.result_json["analysis_metrics"]["syntax_parsed_files"] > 0
        point = db.scalar(select(Checkpoint).where(Checkpoint.job_id == imported.id, Checkpoint.stage == "PARSE"))
        import json

        packet = json.loads((service.data_dir / "objects" / point.result_json["object_key"]).read_bytes())
        assert packet["analysis_metrics"] == imported.result_json["analysis_metrics"]
    relation = next(r for r in w["relations"] if r["relation_type"] == "contains")
    service.review_relation(
        actor,
        relation["id"],
        dict(decision="reject", reason="Synthetic rejected fact", evidence_ids=relation["evidence_ids"]),
        relation["row_version"],
    )
    job = service.create_analysis_job(actor, aid, {"kind": "index"}, "warm")
    service.process_job(job["id"])
    result = service.get_job(actor, job["id"])
    assert result["state"] == "succeeded", result
    assert result["result"]["analysis_metrics"]["syntax_parsed_files"] == 0
    assert result["result"]["analysis_metrics"]["syntax_reused_files"] > 0
    assert (
        next(r for r in service.workspace(actor, pid, aid)["relations"] if r["id"] == relation["id"])["review_status"]
        == "rejected"
    )
    with service.db() as s:
        rows = list(
            s.scalars(
                select(Checkpoint)
                .join(Job, Checkpoint.job_id == Job.id)
                .where(Job.project_id == pid, Checkpoint.stage.like("SOURCE_%"))
            )
        )
        assert rows
        for row in rows:
            row.result_json = {"payload": "corrupted syntax data"}
    job = service.create_analysis_job(actor, aid, {"kind": "index"}, "repair-cache")
    service.process_job(job["id"])
    result = service.get_job(actor, job["id"])
    assert result["state"] == "succeeded", result
    assert result["result"]["analysis_metrics"]["syntax_parsed_files"] > 0
