"""Actual pinned SVN reads, isolated from host clients and credentials."""

import os
import secrets
import subprocess
import pytest
from packages.collector.snapshots import collect_svn, bundle_bytes
from packages.analyzers.vks_ingest import parse_bundle


@pytest.mark.skipif(os.getenv("VKS_TEST_SVN_DOCKER") != "1", reason="未启用真实 SVN 容器验收")
def test_real_svn_pinned_read_preserves_bytes_filters_private_and_does_not_follow_links():
    name = "vks-svn-verify-" + secrets.token_hex(5)

    def docker(*args, timeout=180):
        result = subprocess.run(["docker", *args], capture_output=True, timeout=timeout, check=False)
        assert result.returncode == 0, "隔离 SVN fixture 操作失败（未输出材料或凭据）"
        return result.stdout

    try:
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            "vks.verification=svn",
            "--tmpfs",
            "/svn",
            "--user",
            "0",
            "python:3.12-slim-bookworm",
            "sleep",
            "360",
        )
        docker(
            "exec",
            name,
            "sh",
            "-c",
            "apt-get update -qq && apt-get install --no-install-recommends -y -qq subversion",
            timeout=200,
        )
        fixture = """
from pathlib import Path
import subprocess
root=Path('/svn/import');root.mkdir()
(root/'rules.md').write_bytes(b'\\xef\\xbb\\xbf# Original\\r\\nrule\\r\\n')
(root/'ignored.sh').write_text('touch /svn/EXECUTED')
(root/'package.json').write_text('{"scripts":{"postinstall":"touch /svn/EXECUTED"}}')
(root/'.ENV.local').write_text('PRIVATE_SECRET=excluded')
(root/'node_modules').mkdir();(root/'node_modules/ignored.js').write_text('private')
(root/'linked.md').symlink_to('/etc/passwd')
subprocess.run(['svnadmin','create','/svn/repo'],check=True)
subprocess.run(['svn','import','/svn/import','file:///svn/repo','-m','r1','--non-interactive'],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['svn','checkout','file:///svn/repo','/svn/work','--non-interactive'],check=True,stdout=subprocess.DEVNULL)
Path('/svn/work/rules.md').write_bytes(b'# Changed\\nnew rule\\n')
subprocess.run(['svn','commit','/svn/work','-m','r2','--non-interactive'],check=True,stdout=subprocess.DEVNULL)
"""
        docker("exec", name, "python", "-c", fixture)
        command = ("docker", "exec", name, "svn")
        original = collect_svn("file:///svn/repo", "1", command=command, allowed_roots=("file:///svn/repo",))
        assert original.revision == "r1"
        assert original.files["rules.md"] == b"\xef\xbb\xbf# Original\r\nrule\r\n"
        assert set(original.files) == {"rules.md", "package.json"}
        changed = collect_svn("file:///svn/repo", "2", command=command, allowed_roots=("file:///svn/repo",))
        assert changed.files["rules.md"] == b"# Changed\nnew rule\n"
        assert docker("exec", name, "sh", "-c", "test ! -e /svn/EXECUTED && svnlook youngest /svn/repo").strip() == b"2"
        parsed = parse_bundle(bundle_bytes(original, "demo", "r1"), "demo")
        assert parsed.sources[0]["provider"] == "svn" and parsed.sources[0]["revision"] == "r1"
        assert next(a for a in parsed.artifacts if a["path"] == "rules.md")["content"] == original.files["rules.md"]
    finally:
        subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True, timeout=20, check=False)
