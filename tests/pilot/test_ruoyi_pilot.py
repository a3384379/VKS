"""Pinned-source bytes and passive preview boundaries for the RuoYi adapter."""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess

from bs4 import BeautifulSoup
import pytest

from packages.analyzers.vks_ingest import parse_bundle
from packages.collector.snapshots import Snapshot
from scripts import build_ruoyi_pilot as pilot


def source_files():
    files = {"README.md": b"# Original\r\n\r\nPinned documentation.\r\n"}
    for module in pilot.MODULES:
        files[f"ruoyi-admin/src/main/resources/templates/system/{module}/{module}.html"] = (
            '<html><body><script>fetch("https://forbidden.invalid");</script>'
            f'<a onclick="$.operate.add()" shiro:hasPermission="system:{module}:add">新增 &amp; 检查</a>'
            "</body></html>"
        ).encode()
        files[f"ruoyi-admin/src/main/java/Sys{module.title()}Controller.java"] = (
            "import org.springframework.web.bind.annotation.*;\n"
            f'@RestController @RequestMapping("/system/{module}")\n'
            f"public class Sys{module.title()}Controller {{\n"
            '@GetMapping("/add") public String add() {return "ok";}\n}\n'
        ).encode()
    files["ruoyi-admin/src/main/resources/application.yml"] = b"password: do-not-import"
    files["ruoyi-admin/target/Arbitrary.java"] = b"must not enter source scope"
    return files


def test_preview_uses_source_actions_and_escaped_text_with_exact_original_bytes(monkeypatch):
    originals = source_files()
    monkeypatch.setattr(pilot, "collect_git", lambda *_: Snapshot("git", "a" * 40, originals))
    files, provenance = pilot.build("unused")
    parsed = parse_bundle(pilot.pack(files), "ruoyi")
    assert sum(row["type"] == "action" for row in parsed.entities) == 6
    assert len(provenance["link_candidates"]) == 6
    assert files["README.md"] == originals["README.md"]
    for path, digest in provenance["original_sha256"].items():
        assert files[path] == originals[path]
        assert sha256(files[path]).hexdigest() == digest
    assert not any("application.yml" in path or "/target/" in path for path in files)
    for path, raw in files.items():
        if path.startswith("preview/"):
            soup = BeautifulSoup(raw, "html.parser")
            assert not soup.find_all(["script", "iframe", "form", "a"])
            assert soup.button.get_text() == "新增 & 检查"
            assert not soup.find_all(attrs={"onclick": True})
    assert all(row["causal_basis"] != "correlated_runtime" for row in parsed.relations)
    assert json.loads(files["manifest.json"])["allow_platform_llm"] is False


def test_missing_template_requires_explicit_scope_review(monkeypatch):
    files = source_files()
    del files["ruoyi-admin/src/main/resources/templates/system/user/user.html"]
    monkeypatch.setattr(pilot, "collect_git", lambda *_: Snapshot("git", "a" * 40, files))
    with pytest.raises(ValueError, match="Required RuoYi template missing"):
        pilot.build("unused")


@pytest.mark.skipif(not os.getenv("VKS_RUOYI_SOURCE_ROOT"), reason="未指定真实 RuoYi 只读工作区")
def test_actual_ruoyi_git_bytes_and_workspace_are_unchanged():
    root = Path(os.environ["VKS_RUOYI_SOURCE_ROOT"])
    before = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain=v1", "-z"])
    files, provenance = pilot.build(root, os.environ.get("VKS_RUOYI_PILOT_REVISION", "HEAD"))
    parsed = parse_bundle(pilot.pack(files), "ruoyi")
    assert len(provenance["link_candidates"]) >= 10
    assert sum(row["type"] == "action" for row in parsed.entities) >= 20
    assert {row["type"] for row in parsed.entities} >= {"api", "symbol", "sql", "table", "action", "page"}
    for path in provenance["original_sha256"]:
        assert files[path] == subprocess.check_output(
            ["git", "-C", str(root), "show", provenance["source_commit"] + ":" + path]
        )
    assert subprocess.check_output(["git", "-C", str(root), "status", "--porcelain=v1", "-z"]) == before
