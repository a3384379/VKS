"""Read the actual checkout into a passive VKS knowledge bundle; execute no source material."""

from hashlib import sha256
from pathlib import Path
import ast
import json
import tempfile
from scripts.build_demo import build as demo, zip_files
from packages.analyzers.vks_ingest import canonical_json, source_digest, parse_bundle

ROOT = Path(__file__).resolve().parents[1]
FEATURES = [
    (
        "project",
        "项目与模块",
        "创建项目及业务模块，成员权限决定可见资料。",
        "service.py",
        "Service",
        "create_project",
        "/api/v1/projects",
        "POST",
    ),
    (
        "imports",
        "资料接入",
        "只读采集并导入固定资料，检查摘要和定位，不执行仓库脚本。",
        "service.py",
        "Service",
        "import_bundle",
        "/api/v1/projects/{pid}/imports",
        "POST",
    ),
    (
        "scenarios",
        "场景与采集",
        "场景修订与有限分支独立保存；采集必须固定环境、合成数据和审批。",
        "scenarios.py",
        "ScenarioService",
        "create",
        "/api/v1/projects/{pid}/scenarios",
        "POST",
    ),
    (
        "documents",
        "文档与审核",
        "编辑生成新修订，章节锁和文档人员审核参与发布检查。",
        "service.py",
        "Service",
        "save_document",
        "/api/v1/documents/{eid}/draft",
        "PUT",
    ),
    (
        "reviews",
        "关系审核",
        "人工审核和证据有效性分别保存，机器只可提议候选。",
        "service.py",
        "Service",
        "review_relation",
        "/api/v1/relations/{rid}/review",
        "POST",
    ),
    (
        "graph",
        "知识探索",
        "按同一知识基线和当前权限读取局部图谱。",
        "service.py",
        "Service",
        "graph",
        "/api/v1/entities/{eid}/relations",
        "GET",
    ),
    (
        "releases",
        "版本发布",
        "发布需要预检、人员会话确认和固定manifest；旧修订保持可读。",
        "service.py",
        "Service",
        "publish",
        "/api/v1/release-candidates/{cid}/publish",
        "POST",
    ),
    (
        "clients",
        "MCP客户端",
        "专用客户端凭证按项目与scope授权，服务器逐次重新校验。",
        "service.py",
        "Service",
        "create_token",
        "/api/v1/projects/{pid}/tokens",
        "POST",
    ),
]


def build():
    from vks_api.app import create_app

    with tempfile.TemporaryDirectory(prefix="vks-openapi-") as directory:
        app = create_app(directory, enable_worker=False)
        spec = app.openapi()
        app.state.service.engine.dispose()
    project = "vks-selfhosted"
    baseline = "selfhosted-v1"
    original = json.loads(demo()["manifest.json"])
    files = {}
    evidence = []
    bindings = []
    sections = []
    code = []
    page = "self.workspace"
    html = (
        '<!doctype html><html lang="zh-CN"><body><main data-vks-page="'
        + page
        + '"><h1>知境 VKS · 源码自举导航</h1><p>静态导航快照；点击仅选择知识，不执行API。</p>'
    )
    markdown = "# 知境 VKS 功能与来源\n\n此资料由当前工作区的实际源码与OpenAPI生成。关系为人工编排的候选，不代表真实运行观察；所有功能细节以引用修订为准。\n\n"
    for key, title, description, filename, classname, method, route, verb in FEATURES:
        assert verb.lower() in spec["paths"][route], route
        html += f'<button type="button" data-vks-action="self.{key}">{title}</button>'
        start = len(markdown.splitlines()) + 1
        markdown += (
            "## " + title + "\n\n" + description + f" 服务方法 `{classname}.{method}`；接口声明 `{verb} {route}`。\n\n"
        )
        sections.append(
            {
                "sectionKey": "self.chapter." + key,
                "artifactPath": "docs/vks-workflow.md",
                "headingPath": ["知境 VKS 功能与来源", title],
                "startLine": start,
                "endLine": start + 2,
            }
        )
        path = "backend/" + filename
        raw = (ROOT / "services/api/vks_api" / filename).read_bytes()
        files[path] = raw
        tree = ast.parse(raw)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == classname)
        node = next(n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method)
        code.append(
            {
                "logicalKey": "self.symbol." + key,
                "entityType": "symbol",
                "artifactPath": path,
                "sha256": sha256(raw).hexdigest(),
                "qualifiedName": "vks_api." + filename[:-3] + "." + classname + "." + method,
                "startLine": node.lineno,
                "endLine": node.end_lineno,
            }
        )
    html += "</main></body></html>"
    files["preview/index.html"] = html.encode()
    files["docs/vks-workflow.md"] = markdown.encode()
    files["api/openapi.json"] = json.dumps(spec, ensure_ascii=False, indent=2).encode()
    for section in sections:
        section["sha256"] = sha256(files[section["artifactPath"]]).hexdigest()
    ui = {
        "schemaVersion": "1.0",
        "projectKey": project,
        "baselineKey": baseline,
        "pages": [
            {
                "logicalKey": page,
                "name": "VKS 真实源码导航 · 静态自举",
                "artifactPath": "preview/index.html",
                "sha256": sha256(files["preview/index.html"]).hexdigest(),
            }
        ],
        "actions": [
            {"logicalKey": "self." + key, "pageKey": page, "label": title, "event": "click"}
            for key, title, *_ in FEATURES
        ],
        "documentSections": sections,
        "codeEntities": code,
    }
    for index, (key, title, _, filename, classname, method, route, verb) in enumerate(FEATURES):
        action = "self." + key
        chapter = sections[index]
        symbol = code[index]
        for eid, path, kind, loc, claim in (
            (
                "self.ev.action." + key,
                "preview/index.html",
                "static",
                {"kind": "dom_attribute", "pageKey": page, "actionKey": action},
                "静态导航中的动作锚点",
            ),
            (
                "self.ev.chapter." + key,
                chapter["artifactPath"],
                "static",
                {
                    "kind": "document_section",
                    "sectionKey": chapter["sectionKey"],
                    "startLine": chapter["startLine"],
                    "endLine": chapter["endLine"],
                },
                "当前功能说明章节",
            ),
            (
                "self.ev.source." + key,
                symbol["artifactPath"],
                "manual",
                {
                    "kind": "file_lines",
                    "startLine": symbol["startLine"],
                    "endLine": symbol["endLine"],
                    "qualifiedName": symbol["qualifiedName"],
                },
                "标准Python AST定位实际声明，不推断动态调用",
            ),
            (
                "self.ev.api." + key,
                "api/openapi.json",
                "static",
                {"kind": "file_lines", "startLine": 1, "endLine": len(files["api/openapi.json"].decode().splitlines())},
                "本工作区真实FastAPI生成的接口契约，不是运行请求",
            ),
        ):
            evidence.append(
                {
                    "evidenceKey": eid,
                    "artifactPath": path,
                    "sha256": sha256(files[path]).hexdigest(),
                    "evidenceKind": kind,
                    "locator": loc,
                    "claim": claim,
                }
            )
        apikey = (
            "api.vks."
            + sha256(canonical_json({"service": "vks", "method": verb, "route": route, "conditions": {}})).hexdigest()[
                :28
            ]
        )
        for fr, to, typ, refs in (
            (action, chapter["sectionKey"], "documents", ["self.ev.action." + key, "self.ev.chapter." + key]),
            (action, apikey, "may_call", ["self.ev.action." + key, "self.ev.api." + key]),
            (symbol["logicalKey"], apikey, "handler_of", ["self.ev.source." + key, "self.ev.api." + key]),
        ):
            bindings.append(
                {
                    "fromLogicalKey": fr,
                    "toLogicalKey": to,
                    "relationType": typ,
                    "evidenceRefs": refs,
                    "reason": "自举静态候选：实际定位可查，业务映射仍需人员审核",
                }
            )
    files["knowledge/ui-manifest.json"] = canonical_json(ui)
    files["knowledge/bindings.json"] = canonical_json(
        {
            "schemaVersion": "1.0",
            "projectKey": project,
            "baselineKey": baseline,
            "producer": {"kind": "manual", "name": "VKS actual-checkout selfhosted fixture", "version": "1"},
            "evidence": evidence,
            "bindings": bindings,
        }
    )
    manifest = {
        **original,
        "project_key": project,
        "baseline_key": baseline,
        "import_key": "selfhosted-static-v1",
        "description": "本工作区实际源码/OpenAPI的只读自举资料；无模型调用或运行因果",
        "artifacts": [],
    }
    for source in manifest["sources"]:
        if source["source_id"] in ("api", "backend"):
            source["service_key"] = "vks"
    for path, raw in sorted(files.items()):
        sid = path.split("/")[0]
        suffix = Path(path).suffix
        kind = (
            "source"
            if sid == "backend"
            else "markdown"
            if suffix == ".md"
            else "prototype"
            if suffix == ".html"
            else "openapi"
            if sid == "api"
            else "ui_manifest"
            if path.endswith("ui-manifest.json")
            else "binding_manifest"
        )
        media = (
            "text/x-python"
            if kind == "source"
            else "text/markdown"
            if kind == "markdown"
            else "text/html"
            if kind == "prototype"
            else "application/json"
        )
        manifest["artifacts"].append(
            {
                "source_id": sid,
                "path": path,
                "sha256": sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "kind": kind,
                "media_type": media,
                "encoding": "utf-8",
                "sensitivity": "internal",
            }
        )
    for source in manifest["sources"]:
        source["content_sha256"] = source_digest(
            [a for a in manifest["artifacts"] if a["source_id"] == source["source_id"]]
        )
    for key in ("uiManifest", "bindings"):
        manifest[key]["sha256"] = sha256(files[manifest[key]["path"]]).hexdigest()
    files["manifest.json"] = canonical_json(manifest)
    return files


if __name__ == "__main__":
    files = build()
    target = ROOT / "fixtures/vks-selfhosted.vksbundle.zip"
    zip_files(files, target)
    parsed = parse_bundle(target.read_bytes(), "vks-selfhosted")
    report = {
        "sha256": sha256(target.read_bytes()).hexdigest(),
        "actions": len([e for e in parsed.entities if e["type"] == "action"]),
        "relations": len(parsed.relations),
        "source_files": len([a for a in parsed.artifacts if a["kind"] == "source"]),
        "runtime_validated": False,
        "platform_model_calls": 0,
    }
    (ROOT / "fixtures/vks-selfhosted.summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report))
