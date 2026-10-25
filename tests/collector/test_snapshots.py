import subprocess
import pytest
from packages.collector.snapshots import collect_directory, collect_git, collect_svn, bundle_bytes, CollectionError
from packages.analyzers.vks_ingest import parse_bundle


def git(root, *args):
    return (
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        .stdout.decode()
        .strip()
    )


def test_directory_preserves_bytes_skips_private_symlinks_and_never_runs_scripts(tmp_path):
    (tmp_path / "docs").mkdir()
    raw = b"\xef\xbb\xbf# \xe8\xa7\x84\xe5\x88\x99\r\n\r\n## One\r\ntext\r\n"
    (tmp_path / "docs/rules.md").write_bytes(raw)
    (tmp_path / ".env").write_text("PRIVATE=secret")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/ignored.js").write_text("bad")
    (tmp_path / "package.json").write_text('{"scripts":{"postinstall":"touch EXECUTED"}}')
    (tmp_path / "linked.md").symlink_to("/etc/passwd")
    snapshot = collect_directory(tmp_path)
    assert snapshot.files["docs/rules.md"] == raw
    assert ".env" not in snapshot.files and "linked.md" not in snapshot.files
    assert not any("node_modules" in p for p in snapshot.files)
    assert not (tmp_path / "EXECUTED").exists()
    bundle = bundle_bytes(snapshot, "demo", "v1")
    parsed = parse_bundle(bundle, "demo")
    assert next(a for a in parsed.artifacts if a["path"] == "docs/rules.md")["content"] == raw
    assert parsed.sources[0]["provider"] == "local"
    assert bundle == bundle_bytes(snapshot, "demo", "v1")


def test_git_pins_revision_without_touching_dirty_checkout_or_executing_hooks(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    (tmp_path / "rules.md").write_text("# Original\nrule\n")
    git(tmp_path, "add", "rules.md")
    git(tmp_path, "commit", "-m", "v1")
    revision = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "rules.md").write_text("# Dirty\nnew\n")
    hook = tmp_path / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\ntouch EXECUTED\n")
    hook.chmod(0o700)
    before = git(tmp_path, "status", "--porcelain")
    snapshot = collect_git(tmp_path, revision)
    assert snapshot.files["rules.md"] == b"# Original\nrule\n"
    assert snapshot.revision == revision and git(tmp_path, "status", "--porcelain") == before
    assert not (tmp_path / "EXECUTED").exists()
    parsed = parse_bundle(bundle_bytes(snapshot, "demo", "v1"), "demo")
    assert parsed.sources[0]["revision"] == revision
    # Same bytes at another source revision remain distinct version vectors.
    git(tmp_path, "commit", "--allow-empty", "-m", "v2")
    second = collect_git(tmp_path, "HEAD")
    assert second.revision != revision and second.files == snapshot.files
    assert parse_bundle(bundle_bytes(second, "demo", "v2"), "demo").sources[0]["revision"] != revision


@pytest.mark.parametrize("revision", ["--upload-pack=bad", "HEAD\nother", ""])
def test_git_rejects_option_injection(tmp_path, revision):
    with pytest.raises(CollectionError):
        collect_git(tmp_path, revision)


def test_svn_requires_explicit_client_root_and_pinned_numeric_revision():
    with pytest.raises(CollectionError):
        collect_svn("https://repo.invalid/project", "HEAD", allowed_roots=("https://repo.invalid",))
    with pytest.raises(CollectionError):
        collect_svn("https://repo.invalid/project", "123")
    with pytest.raises(CollectionError):
        collect_svn("https://repo.invalid.evil/project", "123", allowed_roots=("https://repo.invalid",))


def test_svn_readonly_commands_keep_revision_and_reject_links(monkeypatch):
    import packages.collector.snapshots as module

    commands = []

    def read(args, **kw):
        commands.append(args)
        if "list" in args:
            return b'<lists><list><entry kind="file"><name>rules.md</name><size>9</size></entry><entry kind="file"><name>link.md</name><size>4</size></entry></list></lists>'
        if "proplist" in args:
            return b"<properties>svn:special</properties>" if args[-1].endswith("link.md@123") else b"<properties/>"
        return b"# R\nrule\n"

    monkeypatch.setattr(module, "_run", read)
    result = collect_svn("https://repo.invalid/project", "123", allowed_roots=("https://repo.invalid/project",))
    assert result.provider == "svn" and result.revision == "r123"
    assert result.files == {"rules.md": b"# R\nrule\n"}
    assert all("-r" in args and "123" in args and "--non-interactive" in args for args in commands)
    assert not any(word in args for args in commands for word in ("checkout", "commit", "update", "export"))
    assert parse_bundle(bundle_bytes(result, "demo", "v1"), "demo").sources[0]["revision"] == "r123"


@pytest.mark.parametrize(
    "path",
    [
        "file:///allowed/../private",
        "file:///allowed/%2e%2e/private",
        "file:///allowed/%2E/private",
        "https://repo.invalid/project/%5c..%5cprivate",
    ],
)
def test_svn_rejects_encoded_path_escape_before_client_invocation(path, monkeypatch):
    import packages.collector.snapshots as module

    def forbidden(*args, **kwargs):
        raise AssertionError("SVN 客户端不得读取越界地址")

    monkeypatch.setattr(module, "_run", forbidden)
    with pytest.raises(CollectionError):
        collect_svn(path, "1", allowed_roots=("file:///allowed", "https://repo.invalid/project"))


def test_private_source_filters_apply_case_insensitively(tmp_path):
    (tmp_path / ".ENV.production.md").write_text("private")
    (tmp_path / "NODE_MODULES").mkdir()
    (tmp_path / "NODE_MODULES/ignored.js").write_text("private")
    (tmp_path / "readme.md").write_text("# Allowed")
    assert collect_directory(tmp_path).files == {"readme.md": b"# Allowed"}
