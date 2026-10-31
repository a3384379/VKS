"""Build a private, read-only RuoYi pilot from pinned Git blobs.

Only six system administration modules are selected. Original templates are
source evidence; their scripts are never run by this adapter. The generated
preview contains passive buttons and all links remain reviewable candidates.
"""

from hashlib import sha256
import argparse
import html
import json
from pathlib import Path

from bs4 import BeautifulSoup
from packages.collector.snapshots import collect_git
from packages.analyzers.vks_ingest import canonical_json, parse_bundle, source_digest
from scripts.build_flowtest_pilot import pack

MODULES = {"user": "用户", "role": "角色", "menu": "菜单", "dept": "部门", "post": "岗位", "config": "参数"}


def digest(raw):
    return sha256(raw).hexdigest()


def build(root, revision="HEAD"):
    snapshot = collect_git(Path(root), revision)
    baseline = "git-" + snapshot.revision[:12] + "-pilot-v2"
    stems = {"Sys" + name.title() for name in MODULES}
    files = {
        path: raw
        for path, raw in snapshot.files.items()
        if path == "README.md"
        or path.startswith("ruoyi-")
        and (
            Path(path).suffix in (".java", ".xml")
            and any(
                Path(path).stem
                in {stem, stem + "Controller", "I" + stem + "Service", stem + "ServiceImpl", stem + "Mapper"}
                for stem in stems
            )
            or path in {f"ruoyi-admin/src/main/resources/templates/system/{name}/{name}.html" for name in MODULES}
        )
    }
    originals = {path: digest(raw) for path, raw in files.items()}
    sources = [
        dict(source_id=key, root=key, role="backend", provider="git", revision=snapshot.revision, service_key="ruoyi")
        for key in sorted({path.split("/")[0] for path in files if "/" in path})
    ]
    # README keeps its original path and belongs to its own documentation source.
    sources.append(
        dict(
            source_id="readme",
            root="README.md",
            role="documentation",
            provider="git",
            revision=snapshot.revision,
            service_key="ruoyi-docs",
        )
    )
    sources.extend(
        [
            dict(source_id="preview", root="preview", role="prototype", provider="local", service_key="ruoyi-preview"),
            dict(
                source_id="knowledge",
                root="knowledge",
                role="documentation",
                provider="local",
                service_key="ruoyi-pilot",
            ),
        ]
    )

    def manifest():
        artifacts = []
        for path, raw in sorted(files.items()):
            sid = "readme" if path == "README.md" else path.split("/")[0]
            kind = (
                "markdown"
                if path.endswith(".md")
                else "prototype"
                if sid == "preview"
                else "ui_manifest"
                if path.endswith("ui-manifest.json")
                else "binding_manifest"
                if path.endswith("bindings.json")
                else "fixture"
                if sid == "knowledge"
                else "source"
            )
            media = (
                "text/markdown"
                if kind == "markdown"
                else "text/html"
                if kind == "prototype"
                else "application/json"
                if sid == "knowledge"
                else "text/plain"
            )
            artifacts.append(
                dict(
                    source_id=sid,
                    path=path,
                    kind=kind,
                    sha256=digest(raw),
                    size_bytes=len(raw),
                    media_type=media,
                    encoding="utf-8",
                    sensitivity="internal",
                )
            )
        active_sources = [
            dict(s, content_sha256=source_digest([a for a in artifacts if a["source_id"] == s["source_id"]]))
            for s in sources
            if any(a["source_id"] == s["source_id"] for a in artifacts)
        ]
        result = dict(
            schema_version="1.0",
            project_key="ruoyi",
            baseline_key=baseline,
            import_key=baseline,
            sources=active_sources,
            artifacts=artifacts,
            analysis_origin="manual",
            allow_platform_llm=False,
        )
        for key, path in (("uiManifest", "knowledge/ui-manifest.json"), ("bindings", "knowledge/bindings.json")):
            if path in files:
                result[key] = dict(path=path, sha256=digest(files[path]))
        return canonical_json(result)

    files["manifest.json"] = manifest()
    parsed = parse_bundle(pack(files), "ruoyi")
    files.pop("manifest.json")
    apis = [row for row in parsed.entities if row["type"] == "api"]
    pages, actions, evidence, bindings, link_candidates = [], [], [], [], []
    for name, label in MODULES.items():
        path = f"ruoyi-admin/src/main/resources/templates/system/{name}/{name}.html"
        if path not in files:
            raise ValueError("Required RuoYi template missing: " + path)
        text = files[path].decode("utf-8")
        soup = BeautifulSoup(text, "html.parser")
        buttons = [("search", "搜索", soup.find("a", onclick="$.table.search()"))]
        for anchor in soup.find_all("a"):
            permission = anchor.get("shiro:haspermission", "")
            if permission.startswith(f"system:{name}:"):
                buttons.append((permission.rsplit(":", 1)[1], anchor.get_text(" ", strip=True), anchor))
        passive = f'<!doctype html><html lang="zh-CN"><body><main data-vks-page="ruoyi.page.{name}"><h1>{label}管理 · 固定源码候选</h1><p>只选择知识，不执行若依脚本或业务请求。</p>'
        seen = set()
        for operation, title, anchor in buttons:
            if anchor is None or operation in seen:
                continue
            seen.add(operation)
            key = f"ruoyi.action.{name}.{operation}"
            actions.append(dict(logicalKey=key, pageKey="ruoyi.page." + name, label=title, event="click"))
            passive += f'<button type="button" data-vks-action="{key}">{html.escape(title)}</button>'
            needle = anchor.get("shiro:haspermission") or anchor["onclick"]
            line = next(i for i, value in enumerate(text.splitlines(), 1) if needle in value)
            eid = "ruoyi.ev." + name + "." + operation
            evidence.append(
                dict(
                    evidenceKey=eid,
                    evidenceKind="manual",
                    artifactPath=path,
                    sha256=digest(files[path]),
                    locator=dict(kind="file_lines", startLine=line, endLine=line),
                    claim="固定 Git 模板中的准确权限/事件声明；不是运行因果",
                )
            )
            bindings.append(
                dict(
                    fromLogicalKey="ruoyi.page." + name,
                    toLogicalKey=key,
                    relationType="contains",
                    evidenceRefs=[eid],
                    reason="固定模板中的模块操作候选；由派生被动页展示，不执行源脚本",
                )
            )
            route_operation = {"search": "list", "import": "importData"}.get(operation, operation)
            # Only literal, uniquely resolved endpoints. Dynamic edit IDs stay unknown.
            route = f"/system/{name}/{route_operation}"
            targets = [
                row
                for row in apis
                if row["metadata"].get("normalized_route") == route
                and row["metadata"].get("method") == ("GET" if operation == "add" else "POST")
            ]
            if len(targets) == 1:
                link_candidates.append(
                    dict(
                        fromLogicalKey=key,
                        toLogicalKey=targets[0]["stable_key"],
                        relationType="may_call",
                        evidenceRefs=[eid],
                        reason="固定模板操作与同版唯一字面路由的人工候选；业务含义需核对，不建立运行因果",
                    )
                )
        passive += "</main></body></html>"
        preview = f"preview/{name}.html"
        files[preview] = passive.encode()
        pages.append(
            dict(
                logicalKey="ruoyi.page." + name,
                name=label + "管理 · 派生被动页",
                artifactPath=preview,
                sha256=digest(files[preview]),
            )
        )
    if not bindings:
        raise ValueError("No RuoYi actions; review adapter scope")
    files["knowledge/ui-manifest.json"] = canonical_json(
        dict(
            schemaVersion="1.0",
            projectKey="ruoyi",
            baselineKey=baseline,
            pages=pages,
            actions=actions,
            documentSections=[],
        )
    )
    files["knowledge/bindings.json"] = canonical_json(
        dict(
            schemaVersion="1.0",
            projectKey="ruoyi",
            baselineKey=baseline,
            producer=dict(kind="manual", name="VKS RuoYi read-only pilot adapter", version="1"),
            evidence=evidence,
            bindings=bindings,
        )
    )
    provenance = dict(
        source_commit=snapshot.revision,
        original_sha256=originals,
        runtime_source_alignment="unknown",
        allow_platform_llm=False,
        independently_human_reviewed=False,
        actions=len(actions),
        link_candidates=link_candidates,
    )
    files["knowledge/provenance.json"] = canonical_json(provenance)
    files["manifest.json"] = manifest()
    return files, provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    files, provenance = build(args.root, args.revision)
    raw = pack(files)
    parsed = parse_bundle(raw, "ruoyi")
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    out.chmod(0o600)
    print(
        json.dumps(
            dict(
                source_commit=provenance["source_commit"],
                bundle_sha256=digest(raw),
                artifacts=len(parsed.artifacts),
                actions=provenance["actions"],
                relations=len(parsed.relations),
                issues=len(parsed.issues),
            ),
            ensure_ascii=False,
        )
    )
