import argparse
from pathlib import Path
from .snapshots import collect_directory, collect_git, collect_svn, bundle_bytes


def main():
    p = argparse.ArgumentParser(description="VKS 只读客户端采集；不安装/构建/修改来源项目")
    p.add_argument("--provider", choices=["directory", "git", "svn"], required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--revision")
    p.add_argument("--allow-svn-root", action="append", default=[])
    p.add_argument("--project", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--service", default="repository")
    p.add_argument("--output", required=True)
    args = p.parse_args()
    snapshot = (
        collect_directory(Path(args.root))
        if args.provider == "directory"
        else collect_git(Path(args.root), args.revision or "HEAD")
        if args.provider == "git"
        else collect_svn(args.root, args.revision or "", allowed_roots=tuple(args.allow_svn_root))
    )
    output = Path(args.output)
    output.write_bytes(bundle_bytes(snapshot, args.project, args.baseline, source_key=args.service))
    print(f"已生成只读快照包：{len(snapshot.files)} 个文件；{len(snapshot.issues)} 项忽略；来源版本已固定。")


if __name__ == "__main__":
    main()
