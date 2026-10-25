"""Read-only FlowTest adapter. Git blobs are data: never import or run FlowTest code.

UI navigation/anchors are derived from fixed source and remain candidates. The
optional running OpenAPI belongs to a separate source whose alignment is unknown.
"""

from __future__ import annotations
import argparse
import ast
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import zipfile
from packages.collector.snapshots import collect_git
from packages.analyzers.vks_ingest import canonical_json, source_digest, parse_bundle, _headings

PROJECT = "flowtest"
NAV = "frontend/src/features/navigation/navigation-config.tsx"
GUIDE = "docs/system-user-guide.md"
EXTRA_ACTIONS = [
    (
        "create-api",
        "新建接口",
        "frontend/src/features/api-console/CreateDialogs.tsx",
        "创建接口",
        "api_assets",
        "create_api_definition",
    ),
    (
        "import-api",
        "导入接口文档",
        "frontend/src/features/api-console/ImportDialog.tsx",
        "导入接口",
        "imports",
        "preview_api_document",
    ),
    (
        "create-environment",
        "新建环境",
        "frontend/src/features/api-console/CreateDialogs.tsx",
        "环境",
        "api_assets",
        "create_environment",
    ),
    (
        "create-workflow",
        "新建工作流",
        "frontend/src/pages/WorkflowsPage.tsx",
        "创建工作流",
        "workflows",
        "create_workflow",
    ),
]
DOC_HINTS = {
    "apis": "接口管理",
    "workflows": "接口工作流",
    "settings": "项目管理",
    "services": "服务目录",
    "request-targets": "请求 Service",
    "protocols": "多协议",
    "assets": "测试资产",
    "data": "数据与 Mock",
    "tasks": "任务执行",
    "environments": "环境实验室",
    "performance": "性能实验室",
    "reports": "测试报告",
    "impact": "影响分析",
    "change-regression": "变更回归",
    "quality": "质量中心",
    "release": "发布门禁",
    "ai": "AI 助手",
    "contexts": "上下文",
    "ai-changes": "AI 变更集",
    "mcp-changes": "MCP",
    "organization": "组织治理",
    "fabric": "分布式",
    "platform": "平台管理",
    "dashboard": "质量总览",
    "contracts": "契约中心",
    "test-engineering": "Test Engineering",
}


def digest(data):
    return sha256(data).hexdigest()


def api_key(method, path, service="flowtest-source"):
    return (
        "api."
        + service
        + "."
        + digest(canonical_json({"service": service, "method": method, "route": path, "conditions": {}}))[:28]
    )


def build(root, revision="HEAD", *, runtime_openapi=None, runtime_revision=None):
    snapshot = collect_git(Path(root), revision)
    baseline = "git-" + snapshot.revision[:12] + "-pilot-v1"
    selected = {
        path: raw
        for path, raw in snapshot.files.items()
        if (
            path.startswith("backend/app/api/")
            or path.startswith("frontend/src/features/navigation/")
            or path.startswith("frontend/src/features/api-console/")
        )
        and not path.endswith((".test.ts", ".test.tsx"))
        or path in (GUIDE, "docs/NAVIGATION_GROUPING.md", "frontend/src/pages/WorkflowsPage.tsx")
    }
    for path in (NAV, GUIDE):
        if path not in selected:
            raise ValueError("Required FlowTest source file missing: " + path)
    files = dict(selected)
    evidence = []
    bindings = []
    code = []
    sections = []
    declarations = []
    for path, raw in selected.items():
        if not path.startswith("backend/app/api/v1/endpoints/") or not path.endswith(".py"):
            continue
        tree = ast.parse(raw)
        routers = {}
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "APIRouter"
            ):
                prefix = next(
                    (
                        k.value.value
                        for k in node.value.keywords
                        if k.arg == "prefix" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)
                    ),
                    "",
                )
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        routers[target.id] = prefix
        for node in tree.body:
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (
                    not isinstance(decorator, ast.Call)
                    or not isinstance(decorator.func, ast.Attribute)
                    or not isinstance(decorator.func.value, ast.Name)
                ):
                    continue
                router = decorator.func.value.id
                verb = decorator.func.attr.upper()
                if (
                    router not in routers
                    or verb not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
                    or not decorator.args
                ):
                    continue
                if not isinstance(decorator.args[0], ast.Constant) or not isinstance(decorator.args[0].value, str):
                    continue
                route = "/api/v1" + routers[router] + decorator.args[0].value
                key = "flowtest.code." + Path(path).stem + "." + node.name
                declarations.append(
                    {
                        "path": route,
                        "method": verb,
                        "operation": node.name,
                        "module": Path(path).stem,
                        "key": key,
                        "artifact": path,
                        "start": min([node.lineno] + [d.lineno for d in node.decorator_list]),
                        "end": node.end_lineno,
                    }
                )
    paths = {}
    seen = set()
    for declaration in declarations:
        ident = (declaration["method"], declaration["path"])
        if ident in seen:
            continue
        seen.add(ident)
        paths.setdefault(declaration["path"], {})[declaration["method"].lower()] = {
            "operationId": declaration["module"] + "_" + declaration["operation"],
            "summary": "静态FastAPI路由声明：" + declaration["operation"],
            "description": "从固定Git版本AST读取；未执行FlowTest代码，不包含完整Pydantic响应契约或运行证据。",
            "responses": {"200": {"description": "Declared response; inspect source schema for actual status/model"}},
        }
        source_key = "flowtest.ev.source." + declaration["module"] + "." + declaration["operation"]
        spec_key = "flowtest.ev.spec." + declaration["module"] + "." + declaration["operation"]
        if not any(x["logicalKey"] == declaration["key"] for x in code):
            code.append(
                {
                    "logicalKey": declaration["key"],
                    "entityType": "symbol",
                    "artifactPath": declaration["artifact"],
                    "sha256": digest(files[declaration["artifact"]]),
                    "qualifiedName": "app.api.v1.endpoints." + declaration["module"] + "." + declaration["operation"],
                    "startLine": declaration["start"],
                    "endLine": declaration["end"],
                }
            )
            evidence.append(
                {
                    "evidenceKey": source_key,
                    "evidenceKind": "manual",
                    "artifactPath": declaration["artifact"],
                    "sha256": digest(files[declaration["artifact"]]),
                    "locator": {"kind": "file_lines", "startLine": declaration["start"], "endLine": declaration["end"]},
                    "claim": "固定Git源码中的Python/FastAPI声明锚点；没有推断动态调用",
                }
            )
        bindings.append(
            {
                "fromLogicalKey": declaration["key"],
                "toLogicalKey": api_key(*ident),
                "relationType": "handler_of",
                "evidenceRefs": [source_key, spec_key],
                "reason": "AST中存在确切router装饰器与函数声明；人工候选，非运行事实",
            }
        )
    static_spec = {
        "openapi": "3.1.0",
        "info": {
            "title": "FlowTest 固定Git静态路由目录",
            "version": snapshot.revision,
            "description": "由固定源码AST派生，非运行实例的完整OpenAPI；来源与部署对齐未知。",
        },
        "paths": paths,
    }
    files["api-derived/openapi.json"] = canonical_json(static_spec)
    for declaration in declarations:
        evidence_key = "flowtest.ev.spec." + declaration["module"] + "." + declaration["operation"]
        if any(e["evidenceKey"] == evidence_key for e in evidence):
            continue
        evidence.append(
            {
                "evidenceKey": evidence_key,
                "evidenceKind": "manual",
                "artifactPath": "api-derived/openapi.json",
                "sha256": digest(files["api-derived/openapi.json"]),
                "locator": {"kind": "file_lines", "startLine": 1, "endLine": 1},
                "claim": "固定源码派生的静态路由目录；不证明线上响应或因果",
            }
        )
    navtext = files[NAV].decode()
    label_block = navtext.split("export const sectionLabels", 1)[1].split("\n}", 1)[0]
    nav = [(a or b, label) for a, b, label in re.findall(r"(?m)^\s*(?:'([^']+)'|([a-z-]+)):\s*'([^']+)'", label_block)]
    if len(nav) != 26:
        raise ValueError("FlowTest navigation scope changed; review adapter rather than guessing")
    headings = _headings(files[GUIDE].decode())
    for i, h in enumerate(headings):
        h["heading_path"] = h["path"]
        h["startLine"] = h["line"]
        h["endLine"] = headings[i + 1]["line"] - 1 if i + 1 < len(headings) else len(files[GUIDE].decode().splitlines())
    actions = []
    html = '<!doctype html><html lang="zh-CN"><body><main data-vks-page="flowtest.navigation"><h1>FlowTest · 固定源码知识试点</h1><p>此页由真实Git导航声明派生。点击只选择VKS知识；不执行FlowTest业务请求。源码与现有运行服务不同版本。</p>'
    curated = [(key, label, NAV, DOC_HINTS.get(key, label), None, None) for key, label in nav] + EXTRA_ACTIONS
    for index, (key, label, source_path, hint, module, operation) in enumerate(curated, 1):
        if label not in files[source_path].decode():
            raise ValueError("UI label no longer present in fixed source: " + key)
        action = "flowtest.action." + key
        actions.append({"logicalKey": action, "pageKey": "flowtest.navigation", "label": label, "event": "click"})
        html += f'<button type="button" data-vks-action="{action}">{label}</button>'
        candidates = [h for h in headings if hint in h["heading_path"][-1]]
        chosen = candidates[0] if candidates else next(h for h in headings if "模块总览" in h["heading_path"][-1])
        section_key = "flowtest.chapter." + key
        # Different actions may share one actual chapter; use the same stable key
        # for an identical locator instead of inventing duplicate chapter facts.
        existing = next((s for s in sections if s["startLine"] == chosen["startLine"]), None)
        if existing:
            section_key = existing["sectionKey"]
        else:
            sections.append(
                {
                    "sectionKey": section_key,
                    "artifactPath": GUIDE,
                    "sha256": digest(files[GUIDE]),
                    "headingPath": chosen["heading_path"],
                    "startLine": chosen["startLine"],
                    "endLine": chosen["endLine"],
                }
            )
        ui_line = next(i for i, line in enumerate(files[source_path].decode().splitlines(), 1) if label in line)
        for eid, path, locator, claim in (
            (
                f"flowtest.ev.ui.{key}",
                source_path,
                {"kind": "file_lines", "startLine": ui_line, "endLine": ui_line},
                "实际UI源码标签的只读锚点",
            ),
            (
                f"flowtest.ev.doc.{key}",
                GUIDE,
                {
                    "kind": "document_section",
                    "sectionKey": section_key,
                    "startLine": chosen["startLine"],
                    "endLine": chosen["endLine"],
                },
                "固定Git版本使用手册中的实际章节",
            ),
        ):
            evidence.append(
                {
                    "evidenceKey": eid,
                    "evidenceKind": "manual",
                    "artifactPath": path,
                    "sha256": digest(files[path]),
                    "locator": locator,
                    "claim": claim,
                }
            )
        bindings.append(
            {
                "fromLogicalKey": action,
                "toLogicalKey": section_key,
                "relationType": "documents",
                "evidenceRefs": ["flowtest.ev.ui." + key, "flowtest.ev.doc." + key],
                "reason": "真实UI标签与使用手册的人工编排候选；未完成独立人工Gold核验",
            }
        )
        if module:
            targets = [d for d in declarations if d["module"] == module and d["operation"] == operation]
            if len(targets) == 1:
                d = targets[0]
                bindings.append(
                    {
                        "fromLogicalKey": action,
                        "toLogicalKey": api_key(d["method"], d["path"]),
                        "relationType": "may_call",
                        "evidenceRefs": ["flowtest.ev.ui." + key, "flowtest.ev.source." + module + "." + operation],
                        "reason": "对应UI操作与后端声明的潜在关联；没有运行观察，不建立因果",
                    }
                )
    html += "</main></body></html>"
    files["preview/flowtest.html"] = html.encode()
    ui = {
        "schemaVersion": "1.0",
        "projectKey": PROJECT,
        "baselineKey": baseline,
        "pages": [
            {
                "logicalKey": "flowtest.navigation",
                "name": "FlowTest · 真实源码导航（派生静态页）",
                "artifactPath": "preview/flowtest.html",
                "sha256": digest(files["preview/flowtest.html"]),
            }
        ],
        "actions": actions,
        "documentSections": sections,
        "codeEntities": code,
    }
    # Each action must also be anchored in the derived passive HTML sidecar.
    files["knowledge/ui-manifest.json"] = canonical_json(ui)
    files["knowledge/bindings.json"] = canonical_json(
        {
            "schemaVersion": "1.0",
            "projectKey": PROJECT,
            "baselineKey": baseline,
            "producer": {"kind": "manual", "name": "VKS FlowTest read-only adapter", "version": "1"},
            "evidence": evidence,
            "bindings": bindings,
        }
    )
    provenance = {
        "source_commit": snapshot.revision,
        "source_language": "Python/FastAPI + React/TypeScript",
        "selection": "allowlisted code and original documentation; no env, DB, personal browser profile or test executions",
        "runtime_source_alignment": "unknown",
        "runtime_revision": runtime_revision,
        "allow_platform_llm": False,
        "independently_human_reviewed": False,
        "original_artifacts_unchanged": True,
    }
    if runtime_openapi:
        if not runtime_revision:
            raise ValueError("Runtime OpenAPI needs its separately inspected image revision")
        raw = Path(runtime_openapi).read_bytes()
        json.loads(raw)
        files["api-runtime/openapi.json"] = raw
        provenance["runtime_openapi_sha256"] = digest(raw)
    files["knowledge/provenance.json"] = canonical_json(provenance)
    source_defs = [
        ("backend", "backend", "git", "flowtest-source"),
        ("frontend", "frontend", "git", "flowtest-ui"),
        ("docs", "documentation", "git", "flowtest-docs"),
        ("api-derived", "api", "local", "flowtest-source"),
        ("preview", "prototype", "local", "flowtest-preview"),
        ("knowledge", "documentation", "local", "flowtest-pilot"),
    ]
    if runtime_openapi:
        source_defs.append(("api-runtime", "api", "local", "flowtest-runtime"))
    sources = [
        {
            "source_id": key,
            "root": key,
            "role": role,
            "provider": provider,
            "service_key": service,
            **({"revision": snapshot.revision} if provider == "git" else {}),
        }
        for key, role, provider, service in source_defs
    ]
    artifacts = []
    for path, raw in sorted(files.items()):
        sid = path.split("/")[0]
        suffix = Path(path).suffix.lower()
        kind = (
            "markdown"
            if suffix == ".md"
            else "openapi"
            if sid.startswith("api-")
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
            if kind in ("openapi", "ui_manifest", "binding_manifest", "fixture")
            else "text/plain"
        )
        artifacts.append(
            {
                "source_id": sid,
                "path": path,
                "kind": kind,
                "sha256": digest(raw),
                "size_bytes": len(raw),
                "media_type": media,
                "encoding": "utf-8",
                "sensitivity": "internal",
            }
        )
    for source in sources:
        source["content_sha256"] = source_digest([a for a in artifacts if a["source_id"] == source["source_id"]])
    manifest = {
        "schema_version": "1.0",
        "project_key": PROJECT,
        "baseline_key": baseline,
        "import_key": baseline + "-import",
        "sources": sources,
        "artifacts": artifacts,
        "analysis_origin": "manual",
        "allow_platform_llm": False,
        "description": "真实FlowTest源码与文档只读试点；部署OpenAPI单列，版本未对齐。",
        "uiManifest": {"path": "knowledge/ui-manifest.json", "sha256": digest(files["knowledge/ui-manifest.json"])},
        "bindings": {"path": "knowledge/bindings.json", "sha256": digest(files["knowledge/bindings.json"])},
    }
    files["manifest.json"] = canonical_json(manifest)
    return files, provenance


def pack(files):
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, raw in sorted(files.items()):
            item = zipfile.ZipInfo(path, (2026, 10, 25, 0, 0, 0))
            item.external_attr = 0o100644 << 16
            item.create_system = 3
            archive.writestr(item, raw, compress_type=zipfile.ZIP_DEFLATED)
    return out.getvalue()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--runtime-openapi")
    parser.add_argument("--runtime-revision")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    files, provenance = build(
        args.root, args.revision, runtime_openapi=args.runtime_openapi, runtime_revision=args.runtime_revision
    )
    raw = pack(files)
    parsed = parse_bundle(raw, PROJECT)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    output.chmod(0o600)
    report = {
        **provenance,
        "bundle_sha256": digest(raw),
        "files": len(parsed.artifacts),
        "actions": sum(e["type"] == "action" for e in parsed.entities),
        "api_declarations": sum(e["type"] == "api" for e in parsed.entities),
        "chapters": sum(e["type"] == "section" for e in parsed.entities),
        "relations": len(parsed.relations),
    }
    output.with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
