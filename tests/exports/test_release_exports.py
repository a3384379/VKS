import zipfile
import json
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader
from vks_api.models import Artifact, Revision
from vks_api.service import Service

from packages.analyzers.vks_ingest import parse_bundle

ROOT = Path(__file__).resolve().parents[2]


def publish(service, owner, project):
    job = service.demo_import(owner, project["id"], "demo")
    service.process_job(job["id"])
    work = service.workspace(owner, project["id"])
    for relation in work["relations"]:
        service.review_relation(
            owner,
            relation["id"],
            {"decision": "confirm", "reason": "核对固定材料", "evidence_ids": relation["evidence_ids"]},
            relation["row_version"],
        )
    candidate = service.request_release(
        owner, project["id"], {"analysis_set_id": work["selected_analysis_set"]["id"]}, "precheck"
    )
    confirmation = service.confirm_release(owner, candidate["candidate_id"])
    release = service.publish(
        owner,
        candidate["candidate_id"],
        {k: confirmation[k] for k in ("expected_manifest_sha256", "human_confirmation_ref")}
        | {"change_note": "导出验证"},
        "publish",
    )
    return work, release


@pytest.fixture
def context(tmp_path):
    service = Service(tmp_path / "instance")
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "charge-demo", "name": "导出验证"}, "project")
    work, release = publish(service, owner, project)
    yield service, owner, project, work, release
    service.engine.dispose()


def exported(context, format, include_source=False, actor=None):
    service, owner, project, _, release = context
    actor = actor or owner
    job = service.export(
        actor,
        project["id"],
        {"release_id": release["id"], "format": format, "include_source": include_source},
        f"export-{format}-{include_source}",
    )
    service.process_job(job["id"])
    result = service.get_job(actor, job["id"])
    assert result["state"] == "succeeded", result
    return service.download_export(actor, project["id"], job["id"]), job


@pytest.mark.parametrize("include_source", [False, True])
def test_bundle_reimports_with_original_bytes_permissions_and_new_candidate_reviews(context, include_source):
    service, owner, project, work, _ = context
    (raw, filename), _ = exported(context, "vksbundle", include_source)
    assert filename.endswith(".vksbundle.zip")
    bundle = parse_bundle(raw, project["key"])
    assert bundle.manifest["allow_platform_llm"] is False
    assert any(
        a["content"] == (ROOT / "fixtures/charge-demo/docs/invoice-rules.md").read_bytes() for a in bundle.artifacts
    )
    assert any(e["type"] == "action" for e in bundle.entities)
    assert (
        any(e["type"] == "section" and e["stable_key"] == work["sections"][0]["stable_key"] for e in bundle.entities)
        if work.get("sections")
        else True
    )
    assert not any(e["kind"] == "observed" for e in bundle.evidence)
    assert any(a["kind"] == "source" for a in bundle.artifacts) is include_source
    with service.db() as s:
        value = service._commit_bundle(s, project["id"], owner.id, bundle)
    restored = service.workspace(owner, project["id"], value["analysis_set_id"])
    assert restored["relations"]
    assert all(r["review_status"] == "candidate" for r in restored["relations"])
    assert all(r["causal_basis"] != "correlated_runtime" for r in restored["relations"])


def test_pdf_is_chinese_readable_paginated_and_release_pinned(context, tmp_path):
    (_, _, _, _, release) = context
    (raw, filename), _ = exported(context, "pdf")
    assert raw.startswith(b"%PDF-") and filename.endswith(".pdf")
    reader = PdfReader(BytesIO(raw))
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert "知识发布报告" in text and "申请金额必须大于零" in text
    assert release["manifest_sha256"] in text
    assert not reader.trailer["/Root"].get("/OpenAction")
    output = tmp_path / "report.pdf"
    output.write_bytes(raw)


def test_export_filters_derived_permission_flags_and_redacts_sensitive_text(context):
    service, owner, project, work, _ = context
    document = work["documents"][0]
    with service.db() as s:
        revision = s.get(Revision, document["entity_revision_id"])
        artifact = s.get(Artifact, revision.artifact_id)
        artifact.metadata_json = dict(artifact.metadata_json, requires_source_read=True)
        revision.content += '\n客户 13800138000；access_token="fake_secret_12345"；/Users/private/data.md\n'
    token = service.create_token(
        owner, project["id"], {"name": "business-export", "scopes": ["knowledge:read", "knowledge:export"]}
    )
    actor = service.authenticate(token["token"], "token")
    (raw, _), _ = exported(context, "markdown_bundle", actor=actor)
    with zipfile.ZipFile(BytesIO(raw)) as z:
        text = "\n".join(z.read(x).decode() for x in z.namelist())
        assert "申请金额必须大于零" not in text and document["entity_revision_id"] not in text
    # A separate key permits source reviewer export, but every derived body is
    # scanned again even when the original import was safe.
    (raw, _), _ = exported(context, "markdown_bundle", include_source=True)
    with zipfile.ZipFile(BytesIO(raw)) as z:
        text = "\n".join(z.read(x).decode() for x in z.namelist())
        assert "13800138000" not in text and "fake_secret_12345" not in text and "/Users/private" not in text
        assert "[REDACTED]" in text


def test_bundle_cannot_launder_source_or_trace_requirements_and_download_rechecks(context):
    service, owner, project, work, _ = context
    document = work["documents"][0]
    with service.db() as s:
        artifact = s.get(Artifact, document["artifact_id"])
        artifact.metadata_json = dict(artifact.metadata_json, requires_source_read=True, allow_platform_llm=False)
    (raw, _), _ = exported(context, "vksbundle", include_source=True)
    bundle = parse_bundle(raw, project["key"])
    markdown = next(a for a in bundle.artifacts if a["kind"] == "markdown")
    assert markdown["sensitivity"] == "confidential"
    assert bundle.manifest["allow_platform_llm"] is False
    with service.db() as s:
        artifact = s.get(Artifact, document["artifact_id"])
        artifact.metadata_json = dict(artifact.metadata_json, requires_trace_read=True)
    job = service.export(
        owner,
        project["id"],
        {"release_id": context[4]["id"], "format": "vksbundle", "include_source": True},
        "export-trace-omitted",
    )
    service.process_job(job["id"])
    raw, _ = service.download_export(owner, project["id"], job["id"])
    bundle = parse_bundle(raw, project["key"])
    assert not any(a["kind"] == "markdown" for a in bundle.artifacts)


def test_authored_hotspots_and_scenario_definitions_export_as_explicit_derivatives(tmp_path):
    import hashlib
    from vks_api.hotspots import HotspotService
    from vks_api.scenarios import ScenarioService

    service = Service(tmp_path / "hotspots")
    _, owner = service.login(service.bootstrap_token)
    p = service.create_project(owner, {"key": "prototype-export", "name": "热点导出"}, "project")
    baseline = service.create_analysis(owner, p["id"], {"name": "原型资料", "sources": []}, "analysis")
    raw = b"<!doctype html><html><body><h1>Original</h1><button id='submit'>Continue</button></body></html>"
    upload = service.create_upload(
        owner,
        p["id"],
        {
            "filename": "preview.html",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "media_type": "text/html",
        },
    )
    service.put_upload(owner, p["id"], upload["upload_session_id"], raw)
    job = service.import_bundle(
        owner,
        p["id"],
        {
            "kind": "prototype",
            "artifact_ids": [upload["artifact_id"]],
            "prototype_mode": "axure_html",
            "analysis_set_id": baseline["id"],
        },
        "prototype",
    )
    service.process_job(job["id"])
    assert service.get_job(owner, job["id"])["state"] == "succeeded"
    w = service.workspace(owner, p["id"])
    analysis = w["selected_analysis_set"]
    page = w["pages"][0]
    HotspotService(service).save(
        owner,
        p["id"],
        page["entity_id"],
        {
            "analysis_set_id": analysis["id"],
            "base_page_revision_id": page["entity_revision_id"],
            "action_key": "manual.exported",
            "label": "人工标注",
            "rect": {"x": 0.2, "y": 0.2, "width": 0.2, "height": 0.1},
        },
        page["row_version"],
    )
    scenario = ScenarioService(service).create(
        owner,
        p["id"],
        {
            "analysis_set_id": analysis["id"],
            "dsl": {
                "schema_version": "1.0",
                "scenario_key": "scenario.exported",
                "name": "只读定义",
                "environment_ref": "mock",
                "dataset_ref": "synthetic",
                "risk_level": "read_only",
                "timeout_ms": 2000,
                "steps": [{"step_key": "open", "kind": "goto", "path": "/", "timeout_ms": 1000, "risk": "read_only"}],
            },
        },
        "scenario",
    )
    for relation in service.workspace(owner, p["id"])["relations"]:
        service.review_relation(
            owner,
            relation["id"],
            {"decision": "confirm", "reason": "人工审核注释", "evidence_ids": relation["evidence_ids"]},
            relation["row_version"],
        )
    candidate = service.request_release(owner, p["id"], {"analysis_set_id": analysis["id"]}, "precheck")
    confirmation = service.confirm_release(owner, candidate["candidate_id"])
    release = service.publish(
        owner,
        candidate["candidate_id"],
        {k: confirmation[k] for k in ("human_confirmation_ref", "expected_manifest_sha256")}
        | {"change_note": "发布被动热点"},
        "publish",
    )
    job = service.export(
        owner, p["id"], {"release_id": release["id"], "format": "vksbundle", "include_source": False}, "bundle"
    )
    service.process_job(job["id"])
    assert service.get_job(owner, job["id"])["state"] == "succeeded", service.get_job(owner, job["id"])
    exported, _ = service.download_export(owner, p["id"], job["id"])
    parsed = parse_bundle(exported, p["key"])
    assert any(e["stable_key"] == "manual.exported" and e["type"] == "action" for e in parsed.entities)
    assert any(a["content"] == raw for a in parsed.artifacts)
    evidence = {item["stable_key"]: item for item in parsed.evidence}
    manual_relations = [row for row in parsed.relations if row["to_key"] == "manual.exported"]
    assert manual_relations and all(
        evidence[eid]["kind"] == "manual" for row in manual_relations for eid in row["evidence_keys"]
    )
    # The new unique-ID discovery stays static across a page metadata annotation
    # and export; a person's separate hotspot must not relabel that provenance.
    assert any(item["kind"] == "static" for item in parsed.evidence)
    assert all(row["causal_basis"] != "correlated_runtime" for row in parsed.relations)
    definition = next(
        json.loads(a["content"])
        for a in parsed.artifacts
        if a["path"] == "export/scenarios/" + scenario["scenario_id"] + ".json"
    )
    assert definition["review_required"] is True and definition["approval_transferred"] is False
    assert definition["definition"]["scenario_key"] == "scenario.exported"
    service.engine.dispose()
