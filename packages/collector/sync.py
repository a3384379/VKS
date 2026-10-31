"""Explicit, local-only snapshot sync. No background schedule or remote shell."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from .snapshots import CollectionError, Snapshot, bundle_bytes, collect_directory, collect_git, collect_svn


def inventory(snapshot: Snapshot):
    return {path: sha256(raw).hexdigest() for path, raw in sorted(snapshot.files.items())}


def snapshot_delta(before, after, *, scope_before, scope_after):
    """Changes are observations about bytes, never automatic knowledge deletions."""
    if scope_before != scope_after:
        raise CollectionError("VKS_SYNC_SCOPE_CHANGED", "授权范围变化，请重新建立完整基线")
    added, removed = set(after) - set(before), set(before) - set(after)
    changed = sorted(path for path in set(before) & set(after) if before[path] != after[path])
    moves = []
    for old in sorted(removed):
        targets = [new for new in added if after[new] == before[old]]
        origins = [path for path in removed if before[path] == before[old]]
        if len(targets) == len(origins) == 1:
            moves.append({"from": old, "to": targets[0], "sha256": before[old]})
    moved_old, moved_new = {m["from"] for m in moves}, {m["to"] for m in moves}
    shrink = len(before) >= 10 and len(removed) / len(before) >= 0.25
    return dict(
        added=sorted(added - moved_new),
        removed=sorted(removed - moved_old),
        modified=changed,
        moved=moves,
        unchanged=sum(before[p] == after[p] for p in set(before) & set(after)),
        previous_file_count=len(before),
        current_file_count=len(after),
        scope_anomaly=shrink,
        has_changes=bool(added or removed or changed),
        unknowns=["字节变化未必表示业务规则变化；大规模缺失需要人工核对采集范围"] if shrink else [],
    )


def collect_authorized(root, consent_root, provider="directory", revision=None):
    if provider == "svn":
        return collect_svn(root, revision or "", allowed_roots=(consent_root,))
    requested, authorized = Path(root).absolute(), Path(consent_root).resolve(strict=True)
    if os.name == "nt" and any(
        getattr(part.lstat(), "st_file_attributes", 0) & 0x400 for part in (requested, *requested.parents)
    ):
        raise CollectionError("VKS_FORBIDDEN", "授权目录不能经由 Windows 重解析点或目录联接跳转")
    if requested.is_symlink() or requested.resolve(strict=True) != authorized:
        raise CollectionError("VKS_FORBIDDEN", "采集根目录与本次显式授权不一致")
    if provider == "directory":
        return collect_directory(authorized)
    if provider == "git":
        if not revision:
            raise CollectionError("VKS_INVALID_INPUT", "同步 Git 必须显式提供固定 ref")
        return collect_git(authorized, revision)
    raise CollectionError("VKS_INVALID_INPUT", "不支持的来源类型")


def atomic_private(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".vks-sync-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="VKS 本机显式授权同步，始终只读来源；不执行项目代码")
    for name in ("root", "consent-root", "project", "baseline", "output", "state"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--provider", choices=["directory", "git", "svn"], default="directory")
    parser.add_argument("--revision")
    parser.add_argument("--source-key", default="repository")
    args = parser.parse_args()
    state_path = Path(args.state).absolute()
    if ".vks" not in state_path.parts:
        parser.error("同步状态含本机授权信息，必须保存于已忽略的 .vks/ 目录")
    scope = sha256(
        json.dumps([args.provider, str(args.consent_root), args.project, args.source_key]).encode()
    ).hexdigest()
    previous = json.loads(state_path.read_bytes()) if state_path.exists() else {"files": {}, "scope": scope}
    snapshot = collect_authorized(args.root, args.consent_root, args.provider, args.revision)
    current = inventory(snapshot)
    delta = snapshot_delta(previous["files"], current, scope_before=previous["scope"], scope_after=scope)
    report_path = Path(args.output).with_suffix(".delta.json")
    atomic_private(report_path, (json.dumps(delta, ensure_ascii=False, indent=2) + "\n").encode())
    if delta["scope_anomaly"]:
        raise CollectionError("VKS_SYNC_SCOPE_ANOMALY", "大量来源文件缺失；未生成新包或推进游标，查看本机 delta 报告")
    raw = bundle_bytes(snapshot, args.project, args.baseline, source_key=args.source_key)
    atomic_private(Path(args.output), raw)
    atomic_private(
        state_path,
        json.dumps({"scope": scope, "revision": snapshot.revision, "files": current}, ensure_ascii=False).encode(),
    )
    print(
        json.dumps({"provider": snapshot.provider, "revision": snapshot.revision, "delta": delta}, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
