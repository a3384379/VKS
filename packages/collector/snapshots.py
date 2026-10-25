"""Read-only collectors. A client path is never accepted by the server API.

Git uses immutable blobs, not checkout/build/hooks. SVN uses a pinned revision
through the user's local client. Commands never use a shell or pass secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit
import re
import stat
import subprocess
import zipfile
from defusedxml import ElementTree
from packages.analyzers.vks_ingest import safe_path, source_digest, MAX_FILES, MAX_FILE_BYTES, MAX_BUNDLE_BYTES

IGNORED = {
    ".git",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "target",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".vks",
}
TEXT_SOURCE = {".java", ".xml", ".kt", ".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".sql", ".css", ".properties"}
ALLOWED = TEXT_SOURCE | {
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".json",
    ".yaml",
    ".yml",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".txt",
}
SECRET_NAMES = {"credentials", "id_rsa", "id_ed25519", ".npmrc", ".pypirc", ".netrc"}


class CollectionError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass
class Snapshot:
    provider: str
    revision: str
    files: dict[str, bytes]
    issues: list[dict] = field(default_factory=list)


def included(path):
    p = Path(path)
    return (
        not any(part.casefold() in IGNORED for part in p.parts)
        and not any(part.casefold().startswith(".env") or part.casefold() in SECRET_NAMES for part in p.parts)
        and p.suffix.lower() in ALLOWED
    )


def _limits(files):
    if (
        len(files) > MAX_FILES
        or sum(map(len, files.values())) > MAX_BUNDLE_BYTES
        or any(len(v) > MAX_FILE_BYTES for v in files.values())
    ):
        raise CollectionError("VKS_LIMIT_EXCEEDED", "来源快照超过文件数/字节配额")


def _run(args, *, input=None, timeout=60):
    try:
        result = subprocess.run(
            args,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
        )
    except FileNotFoundError:
        raise CollectionError("VKS_DEPENDENCY_UNAVAILABLE", "来源客户端未安装") from None
    except subprocess.TimeoutExpired:
        raise CollectionError("VKS_SOURCE_TIMEOUT", "只读来源操作超时") from None
    if result.returncode:
        # stderr may contain credentials/URLs; never reflect it.
        raise CollectionError("VKS_SOURCE_READ_FAILED", "无法读取所选来源版本")
    return result.stdout


def collect_directory(root: Path) -> Snapshot:
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise CollectionError("VKS_INVALID_INPUT", "来源必须是目录")
    files, issues = {}, []
    visited_dirs = 0

    def visit(directory_fd, prefix="", depth=0):
        nonlocal visited_dirs
        visited_dirs += 1
        if depth > 64 or visited_dirs > 10000:
            raise CollectionError("VKS_LIMIT_EXCEEDED", "来源目录层级或数量超限")
        for name in sorted(os.listdir(directory_fd)):
            relative = (prefix + "/" + name).lstrip("/")
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                if included(relative):
                    issues.append({"relative_path": relative, "reason_code": "POLICY_EXCLUDED"})
                continue
            if stat.S_ISDIR(info.st_mode):
                if name in IGNORED or name.startswith(".env") or name in SECRET_NAMES:
                    continue
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                except OSError:
                    raise CollectionError("VKS_SOURCE_READ_FAILED", "读取期间目录发生变化，请重新采集") from None
                try:
                    visit(child, relative, depth + 1)
                finally:
                    os.close(child)
                continue
            if not included(relative):
                continue
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                with os.fdopen(fd, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                        raise CollectionError("VKS_LIMIT_EXCEEDED", "来源含特殊文件或超大文件")
                    files[safe_path(relative)] = handle.read(MAX_FILE_BYTES + 1)
            except OSError:
                raise CollectionError("VKS_SOURCE_READ_FAILED", "读取期间文件发生变化，请重新采集") from None
            _limits(files)

    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(root_fd)
    finally:
        os.close(root_fd)
    records = [{"path": p, "sha256": sha256(b).hexdigest(), "size_bytes": len(b)} for p, b in files.items()]
    return Snapshot("local", source_digest(records), files, issues)


def collect_git(root: Path, revision: str) -> Snapshot:
    if not revision or revision.startswith("-") or any(c in revision for c in "\n\r\x00"):
        raise CollectionError("VKS_INVALID_INPUT", "Git revision 无效")
    root = Path(root).resolve(strict=True)
    base = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root)]
    commit = _run([*base, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"]).decode().strip()
    if not re.fullmatch("[a-f0-9]{40}|[a-f0-9]{64}", commit):
        raise CollectionError("VKS_INVALID_INPUT", "无法固定 Git commit")
    entries = _run([*base, "ls-tree", "-r", "-l", "-z", commit]).split(b"\0")
    wanted, issues = [], []
    for record in entries:
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        path = raw_path.decode("utf-8", errors="strict")
        if not included(path):
            continue
        mode, kind, oid, blob_size = meta.decode("ascii").split()
        if mode not in ("100644", "100755") or kind != "blob":
            issues.append({"relative_path": path, "reason_code": "POLICY_EXCLUDED"})
            continue
        if int(blob_size) > MAX_FILE_BYTES:
            raise CollectionError("VKS_LIMIT_EXCEEDED", "Git blob 过大")
        wanted.append((safe_path(path), oid, int(blob_size)))
    if len(wanted) > MAX_FILES or sum(n for _, _, n in wanted) > MAX_BUNDLE_BYTES:
        raise CollectionError("VKS_LIMIT_EXCEEDED", "来源文件过多")
    output = _run(
        [*base, "cat-file", "--batch"], input=("".join(oid + "\n" for _, oid, _ in wanted)).encode(), timeout=120
    )
    files, offset = {}, 0
    for path, oid, expected_size in wanted:
        end = output.find(b"\n", offset)
        header = output[offset:end].split()
        if len(header) != 3 or header[0].decode() != oid or header[1] != b"blob":
            raise CollectionError("VKS_SOURCE_READ_FAILED", "Git blob 响应不一致")
        size = int(header[2])
        offset = end + 1
        if size != expected_size or size > MAX_FILE_BYTES:
            raise CollectionError("VKS_LIMIT_EXCEEDED", "Git blob 过大")
        files[path] = output[offset : offset + size]
        offset += size + 1
        _limits(files)
    return Snapshot("git", commit, files, issues)


def collect_svn(locator: str, revision: str, *, command=("svn",), allowed_roots: tuple[str, ...] = ()) -> Snapshot:
    # The CLI caller explicitly registers roots; no caller-controlled server path.
    if not re.fullmatch(r"[0-9]{1,16}", revision):
        raise CollectionError("VKS_INVALID_INPUT", "SVN 必须固定数字 revision")
    if not allowed_roots or not any(
        locator == p.rstrip("/") or locator.startswith(p.rstrip("/") + "/") for p in allowed_roots
    ):
        raise CollectionError("VKS_FORBIDDEN", "SVN 地址不在客户端显式允许范围")
    if (
        any(c in locator for c in "\r\n\x00")
        or locator.startswith("-")
        or "@" in locator.split("://", 1)[-1].split("/", 1)[0]
    ):
        raise CollectionError("VKS_INVALID_INPUT", "SVN 地址不得包含凭据或选项")
    parsed = urlsplit(locator)
    decoded_path = unquote(parsed.path)
    if (
        parsed.scheme not in ("file", "http", "https", "svn", "svn+ssh")
        or any(part in (".", "..") for part in decoded_path.split("/"))
        or "\\" in decoded_path
        or parsed.query
        or parsed.fragment
        or any(ord(c) < 32 or ord(c) == 127 for c in decoded_path)
    ):
        raise CollectionError("VKS_FORBIDDEN", "SVN 地址包含越界或歧义路径")
    target = locator.rstrip("/") + "@" + revision
    listing = _run([*command, "list", "--xml", "--recursive", "--non-interactive", "-r", revision, "--", target])
    try:
        tree = ElementTree.fromstring(listing)
    except Exception:
        raise CollectionError("VKS_INVALID_INPUT", "SVN 清单无效") from None
    files = {}
    for entry in tree.findall(".//entry"):
        if entry.get("kind") != "file":
            continue
        path = entry.findtext("name") or ""
        if not included(path):
            continue
        path = safe_path(path)
        if int(entry.findtext("size") or 0) > MAX_FILE_BYTES:
            raise CollectionError("VKS_LIMIT_EXCEEDED", "SVN 文件过大")
        # svn:special/symlink is not followed by cat; reject the link artifact.
        props = _run(
            [
                *command,
                "proplist",
                "--xml",
                "--non-interactive",
                "-r",
                revision,
                "--",
                locator.rstrip("/") + "/" + path + "@" + revision,
            ]
        )
        if b"svn:special" in props:
            continue
        files[path] = _run(
            [
                *command,
                "cat",
                "--non-interactive",
                "-r",
                revision,
                "--",
                locator.rstrip("/") + "/" + path + "@" + revision,
            ]
        )
        _limits(files)
    return Snapshot("svn", "r" + revision, files)


def bundle_bytes(
    snapshot: Snapshot, project_key: str, baseline_key: str, *, source_key="repository", role="backend"
) -> bytes:
    if not re.fullmatch(r"[a-z][a-z0-9._-]{0,95}", project_key) or not re.fullmatch(
        r"[a-z][a-z0-9._-]{0,95}", baseline_key
    ):
        raise CollectionError("VKS_INVALID_INPUT", "项目/基线 key 无效")
    artifacts = []
    for path, raw in sorted(snapshot.files.items()):
        suffix = Path(path).suffix.lower()
        kind = "source"
        media = "text/plain"
        encoding = "utf-8"
        if suffix in (".md", ".markdown"):
            kind = "markdown"
            media = "text/markdown"
        elif suffix == ".html":
            kind = "prototype"
            media = "text/html"
        elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            kind = "screenshot"
            media = "image/" + ("jpeg" if suffix in (".jpg", ".jpeg") else suffix[1:])
            encoding = None
        elif suffix in (".json", ".yaml", ".yml"):
            try:
                import yaml

                value = json.loads(raw) if suffix == ".json" else yaml.safe_load(raw)
                if isinstance(value, dict) and ("openapi" in value or "swagger" in value):
                    kind = "openapi"
            except Exception:
                pass
            media = "application/json" if suffix == ".json" else "application/yaml"
        item = {
            "source_id": source_key,
            "kind": kind,
            "path": safe_path(path),
            "sha256": sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "media_type": media,
            "sensitivity": "internal",
        }
        if encoding:
            item["encoding"] = encoding
        artifacts.append(item)
    if not artifacts:
        raise CollectionError("VKS_EMPTY_SOURCE", "来源没有可支持的材料")
    source = {
        "source_id": source_key,
        "role": role,
        "provider": snapshot.provider,
        "root": ".",
        "content_sha256": source_digest(artifacts),
    }
    if snapshot.provider in ("git", "svn"):
        source["revision"] = snapshot.revision
    manifest = {
        "schema_version": "1.0",
        "project_key": project_key,
        "baseline_key": baseline_key,
        "import_key": baseline_key + "-snapshot",
        "sources": [source],
        "artifacts": artifacts,
        "analysis_origin": "manual",
        "allow_platform_llm": False,
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, raw in sorted(
            {
                **snapshot.files,
                "manifest.json": (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
            }.items()
        ):
            info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    return output.getvalue()
