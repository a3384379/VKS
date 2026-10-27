from dataclasses import replace
from io import BytesIO
import json, zipfile
from pathlib import Path
import pytest
from vks_api.app import create_app
from vks_api.source_alignment import compare_sources, diagnostics
from vks_api.service import DomainError


def test_labels_are_scoped_declared_and_never_prove_runtime_causality():
    base = dict(provider="svn", role="backend", provider_revision="r12", service_key="billing", active=True)
    sources = [
        dict(base, source_id="code", source_snapshot_id="code", version_labels={"build_id": "b1"}),
        dict(
            base,
            provider="local",
            role="api",
            source_id="api",
            source_snapshot_id="api",
            version_labels={"code_revision": "12", "build_id": "b1"},
        ),
    ]
    result, status = compare_sources(sources)
    assert status == "matched" and all(x["basis"] == "declared_labels_only" for x in result)
    sources[1]["version_labels"]["code_revision"] = "r13"
    assert compare_sources(sources)[1] == "mismatched"
    sources[1]["active"] = False
    assert compare_sources(sources)[1] == "unknown"
    sources[1]["active"] = True
    sources[1]["service_key"] = "other"
    assert compare_sources(sources)[1] == "unknown"


def test_import_labels_are_immutable_visible_and_dont_merge_api_or_runtime(tmp_path):
    app = create_app(tmp_path / "labels", enable_worker=False)
    service = app.state.service
    _, actor = service.login(service.bootstrap_token)
    pid = service.create_project(actor, {"key": "charge-demo", "name": "Source label test"}, "p")["id"]
    with zipfile.ZipFile(Path("fixtures/charge-demo.vksbundle.zip")) as z:
        files = {i.filename: z.read(i) for i in z.infolist()}
    manifest = json.loads(files["manifest.json"])
    for source in manifest["sources"]:
        if source["source_id"] in ("api", "backend"):
            source["version_labels"] = {
                "code_revision": "r12" if source["source_id"] == "backend" else "r13",
                "build_id": "fixture-build",
            }

    def packed():
        raw = BytesIO()
        with zipfile.ZipFile(raw, "w") as z:
            for name, data in files.items():
                z.writestr(name, json.dumps(manifest).encode() if name == "manifest.json" else data)
        return service._parse(raw.getvalue(), "charge-demo")

    try:
        with service.db() as db:
            saved = service._commit_bundle(db, pid, actor.id, packed())
        aid = saved["analysis_set_id"]
        row = diagnostics(service, actor, pid, aid)
        assert row["declared_alignment"] == "mismatched" and row["runtime_alignment"] == "unknown"
        assert (
            next(s for s in row["source_vector"] if s["source_id"] == "api")["version_labels"]["code_revision"] == "r13"
        )
        limited = replace(actor, scopes=actor.scopes - {"source:read"})
        restricted = diagnostics(service, limited, pid, aid)
        assert all(s["source_id"] != "backend" for s in restricted["source_vector"])
        assert all(item["source_id"] != "backend" for c in restricted["comparisons"] for item in c["values"])
        assert service.workspace(actor, pid, aid)["selected_analysis_set"]["runtime_alignment"] == "unknown"
        next(s for s in manifest["sources"] if s["source_id"] == "api")["version_labels"]["code_revision"] = "r12"
        with pytest.raises(DomainError) as error:
            with service.db() as db:
                service._commit_bundle(db, pid, actor.id, packed(), aid)
        assert error.value.code == "VKS_VERSION_CONFLICT"
        assert diagnostics(service, actor, pid, aid) == row
    finally:
        service.engine.dispose()
