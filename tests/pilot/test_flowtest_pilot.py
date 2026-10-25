"""Opt-in actual FlowTest snapshot acceptance. No FlowTest import or business calls."""

from pathlib import Path
import json
import os
import subprocess
import pytest
from packages.analyzers.vks_ingest import parse_bundle
from scripts.build_flowtest_pilot import build, pack
from scripts.seed_flowtest_pilot import seed


@pytest.mark.skipif(not os.getenv("VKS_FLOWTEST_SOURCE_ROOT"), reason="未指定真实FlowTest只读试点工作区")
def test_real_flowtest_git_bytes_anchors_import_and_no_source_changes(tmp_path):
    root = Path(os.environ["VKS_FLOWTEST_SOURCE_ROOT"])
    before = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain=v1", "-z"])
    revision = os.environ.get("VKS_FLOWTEST_PILOT_REVISION", "HEAD")
    files, provenance = build(root, revision)
    raw = pack(files)
    parsed = parse_bundle(raw, "flowtest")
    assert sum(e["type"] == "action" for e in parsed.entities) == 30
    assert sum(e["type"] == "api" for e in parsed.entities) >= 15
    assert sum(e["type"] == "section" for e in parsed.entities) >= 30
    assert all(r["causal_basis"] != "correlated_runtime" for r in parsed.relations)
    manifest = json.loads(files["manifest.json"])
    assert manifest["allow_platform_llm"] is False
    for artifact in manifest["artifacts"]:
        if artifact["source_id"] in ("frontend", "backend", "docs"):
            original = subprocess.check_output(
                ["git", "-C", str(root), "show", provenance["source_commit"] + ":" + artifact["path"]]
            )
            assert original == files[artifact["path"]]
    bundle = tmp_path / "flowtest.zip"
    bundle.write_bytes(raw)
    report = seed(tmp_path / "vks-private", bundle)
    assert report["actions"] == 30 and report["scenarios"] == 3 and report["runtime_source_alignment"] == "unknown"
    assert report["published"] is False and report["platform_model_calls"] == 0
    assert subprocess.check_output(["git", "-C", str(root), "status", "--porcelain=v1", "-z"]) == before
