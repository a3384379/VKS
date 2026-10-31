from io import BytesIO
import hashlib
import json
import zipfile
import pytest
from sqlalchemy import select
from vks_api.models import Relation
from vks_api.scenarios import ScenarioService
from vks_api.service import Service
from packages.analyzers.vks_ingest import parse_bundle, source_digest
from test_browser_capture import browser_path, mock_site, capture_dsl, env


def imported_graph(service, owner, project, conditional=False):
    frontend = b'<html><body data-vks-page="capture.page"><button data-vks-action="capture.submit">Inspect</button></body></html>'
    specification = {
        "openapi": "3.1.0",
        "info": {"title": "Fixture", "version": "1"},
        "paths": {"/write": {"post": {"responses": {"200": {"description": "ok"}}}}},
    }
    api = json.dumps(specification).encode()
    java = b'package fixture; import org.springframework.web.bind.annotation.RestController; import org.springframework.web.bind.annotation.PostMapping; @RestController class Payments { @PostMapping(value="/write", params="mode=full") public String write(){return "ok";} }'
    files = {"preview/index.html": frontend, "api/other.json": api}
    records = []

    def add(path, source, kind, raw, media):
        files[path] = raw
        records.append(
            {
                "source_id": source,
                "kind": kind,
                "path": path,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "media_type": media,
                "sensitivity": "internal",
            }
        )

    add("preview/index.html", "frontend", "prototype", frontend, "text/html")
    add("api/other.json", "other", "openapi", api, "application/json")
    add(
        "backend/Payments.java" if conditional else "api/billing.json",
        "billing",
        "source" if conditional else "openapi",
        java if conditional else api,
        "text/plain" if conditional else "application/json",
    )
    ui = {
        "schemaVersion": "1.0",
        "projectKey": project["key"],
        "baselineKey": "capture-v1",
        "pages": [
            {
                "logicalKey": "capture.page",
                "name": "受控页面",
                "artifactPath": "preview/index.html",
                "sha256": hashlib.sha256(frontend).hexdigest(),
            }
        ],
        "actions": [{"logicalKey": "capture.submit", "pageKey": "capture.page", "label": "提交", "event": "click"}],
        "documentSections": [],
    }
    add("knowledge/ui.json", "metadata", "ui_manifest", json.dumps(ui).encode(), "application/json")
    manifest = {
        "schema_version": "1.0",
        "project_key": project["key"],
        "baseline_key": "capture-v1",
        "import_key": "fixture",
        "allow_platform_llm": False,
        "artifacts": records,
        "uiManifest": {"path": "knowledge/ui.json", "sha256": records[-1]["sha256"]},
        "sources": [],
    }
    for source, role in (
        ("frontend", "prototype"),
        ("other", "api"),
        ("billing", "backend" if conditional else "api"),
        ("metadata", "documentation"),
    ):
        manifest["sources"].append(
            {
                "source_id": source,
                "service_key": source,
                "role": role,
                "provider": "local",
                "root": ".",
                "content_sha256": source_digest([a for a in records if a["source_id"] == source]),
            }
        )
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for path, raw in files.items():
            archive.writestr(path, raw)
    bundle = parse_bundle(stream.getvalue(), project["key"])
    with service.db() as session:
        result = service._commit_bundle(session, project["id"], owner.id, bundle)
    return service.workspace(owner, project["id"], result["analysis_set_id"])


@pytest.mark.parametrize("mode", ["unique", "ambiguous", "conditional"])
def test_actual_mock_request_produces_only_unique_temporal_candidates_and_preserves_review(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("VKS_RUNNER_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("VKS_RUNNER_ENABLED", "1")
    monkeypatch.setenv("VKS_CHROMIUM_EXECUTABLE", browser_path())
    with mock_site() as (base, counts):
        service = Service(tmp_path / "instance")
        _, owner = service.login(service.bootstrap_token)
        project = service.create_project(owner, {"key": "runtime-test", "name": "运行摘要候选"}, "project")
        workspace = imported_graph(service, owner, project, mode == "conditional")
        aid = workspace["selected_analysis_set"]["id"]
        scenarios = ScenarioService(service)
        policy = {"key": "mock-test", "name": "Mock", **env(base)}
        if mode != "ambiguous":
            policy["service_key"] = "billing"
        scenarios.create_environment(owner, project["id"], policy)
        scenarios.create_dataset(
            owner, project["id"], {"key": "synthetic", "name": "合成", "synthetic": True, "data": {}}
        )
        dsl = capture_dsl(True)
        dsl["steps"][1]["action_entity_id"] = workspace["actions"][0]["entity_id"]
        saved = scenarios.create(owner, project["id"], {"analysis_set_id": aid, "dsl": dsl}, "scenario")

        def capture(revision, key):
            approval = {
                "analysis_set_id": aid,
                "scenario_revision_id": revision,
                "reason": "批准独立合成写入，不是生产操作",
            }
            value = scenarios.request_capture(
                owner,
                saved["scenario_id"],
                {
                    "scenario_revision_id": approval["scenario_revision_id"],
                    "environment_ref": "mock-test",
                    "analysis_set_id": approval["analysis_set_id"],
                },
                key,
            )
            service.process_job(value["job"]["id"])
            result = service.get_job(owner, value["job"]["id"])
            assert result["state"] == "succeeded", result
            return approval["scenario_revision_id"]

        revision = capture(saved["scenario_revision_id"], "capture-1")
        assert counts["writes"] == 1
        with service.db() as session:
            observed = list(
                session.scalars(
                    select(Relation).where(Relation.analysis_set_id == aid, Relation.relation_type == "observed_during")
                )
            )
        if mode == "unique":
            assert len(observed) == 1
            relation = observed[0]
            assert relation.excluded is False and relation.causal_basis == "temporal_association"
            evidence = service.get_evidence(owner, project["id"], relation.evidence_ids)[0]
            assert evidence["evidence_kind"] == "observed" and evidence["locator"]["source_kind"] == "runtime_capture"
            assert evidence["locator"]["request_id"] and evidence["locator"]["step_key"] == "click"
            reviewed = service.set_relation_state(
                owner, relation.id, {"excluded": "confirm" == "reject"}, relation.row_version
            )["relation"]
            capture(revision, "capture-2")
            with service.db() as session:
                unchanged = session.get(Relation, relation.id)
                assert unchanged.manual_lock and unchanged.row_version == reviewed["row_version"]
                assert (
                    unchanged.evidence_ids == relation.evidence_ids and unchanged.causal_basis == "temporal_association"
                )
            assert counts["writes"] == 2
        else:
            assert observed == []
            capture_result = scenarios.captures(owner, project["id"])[0]["result"]
            post = next((request for request in capture_result["requests"] if request["method"] == "POST"))
            resolution = next(
                (row for row in capture_result["network_resolution"] if row["request_id"] == post["request_id"])
            )
            assert resolution["state"] == "unresolved"
            assert resolution["reason_code"] == (
                "REQUEST_CONDITIONS_UNVERIFIED" if mode == "conditional" else "ACTION_OR_API_NOT_UNIQUE"
            )
        service.engine.dispose()
