#!/usr/bin/env python3
"""Build deterministic, offline synthetic VKS fixtures with actual byte hashes.

The Java/OpenAPI snippets are teaching material, never a business integration.
Running this script only writes fixtures and checks the read-only import parser.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.analyzers.vks_ingest import canonical_json, parse_bundle, source_digest

PROJECT = "charge-demo"
BASELINE = "demo-v1"
TARGET = ROOT / "fixtures/charge-demo"

HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>缴费记录 · 合成业务快照</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f5f8ff;color:#14213d;font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{padding:20px 26px;background:#fff;border-bottom:1px solid #dde7f7;display:flex;justify-content:space-between}header strong{color:#2563eb}main{padding:26px}.notice{padding:10px 14px;color:#536680;background:#eaf2ff;border:1px solid #dde7f7;border-radius:6px}.panel{margin-top:18px;padding:22px;background:#fff;border:1px solid #dde7f7;border-radius:8px}h1{font-size:21px;margin:0 0 7px}h2{font-size:17px;margin:0 0 18px}p{color:#536680;line-height:1.8}.filters{display:flex;align-items:center;gap:12px;margin:20px 0}input{border:1px solid #dde7f7;border-radius:5px;padding:9px;width:180px;color:#536680;background:#fafcff}button{border:1px solid #2563eb;border-radius:5px;padding:9px 15px;background:#2563eb;color:#fff;font:inherit;cursor:pointer}button.secondary{background:#fff;color:#2563eb}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:14px 12px;text-align:left;border-bottom:1px solid #edf2fa}th{background:#f5f8ff;color:#536680;font-weight:500}.status{color:#059669;background:#eafaf3;border-radius:4px;padding:4px 8px}.demo{color:#b7791f;background:#fff6e7;border-radius:4px;padding:4px 8px}.dialog{max-width:520px;margin:24px auto;padding:24px;border:1px solid #c8dbff;background:#fff;border-radius:9px;box-shadow:0 6px 22px #14213d12}.field{display:flex;justify-content:space-between;align-items:center;margin:16px 0}.actions{display:flex;justify-content:flex-end;gap:12px;border-top:1px solid #edf2fa;padding-top:18px}footer{text-align:center;color:#8491a7;font-size:12px;margin-top:18px}
</style>
</head>
<body>
<header><strong>云收费管理系统 · 合成示例</strong><span>收费管理　 /　 缴费记录</span></header>
<main data-vks-page="payments.records">
<div class="notice">只读合成快照 · 点击热点只选择知识，不连接真实开票、支付或短信服务。</div>
<section class="panel">
<h1>缴费记录</h1><p>从一个业务操作，理解它背后的规则与实现。</p>
<div class="filters"><label>客户名称 <input readonly value="示例客户 A" aria-label="客户名称"></label><label>缴费日期 <input readonly value="2026-10-01 至 2026-10-25" aria-label="缴费日期"></label><button type="button" data-vks-action="payments.records.query">查询记录</button></div>
<table><thead><tr><th>缴费单号</th><th>客户名称</th><th>缴费日期</th><th>缴费金额</th><th>开票状态</th><th>操作</th></tr></thead><tbody>
<tr><td>DEMO-202610001</td><td>示例客户 A</td><td>2026-10-20</td><td>¥ 500.00</td><td><span class="status">可开票</span></td><td><button class="secondary" type="button" data-vks-action="payments.invoice.open">申请开票</button></td></tr>
<tr><td>DEMO-202610002</td><td>示例客户 B</td><td>2026-10-19</td><td>¥ 260.00</td><td><span class="demo">模拟已开票</span></td><td><button class="secondary" type="button" data-vks-action="payments.invoice.result">查看结果</button></td></tr>
</tbody></table>
</section>
<section class="dialog" aria-label="申请开票 · 合成状态">
<h2>申请开票 <span class="demo">讲解状态</span></h2>
<div class="field"><label>客户名称</label><input readonly value="示例客户 A"></div>
<div class="field"><label>发票类型</label><input readonly value="普通发票 · 合成"></div>
<div class="field"><label>可开票金额</label><input readonly value="500.00 元"></div>
<p>此视图用于说明金额校验及证据路径。提交操作仅选择“确认开票”知识对象，不执行接口。</p>
<div class="actions"><button type="button" data-vks-action="payments.invoice.confirm">确认开票</button></div>
</section>
<footer>人工编排状态 · 无运行观察 · 数据均为合成示例</footer>
</main>
</body>
</html>
"""

MARKDOWN = """# 缴费记录开票说明

本资料为 VKS 本地开发合成 fixture，不代表任何实际公司项目；Java、接口和 SQL 仅作为来源可定位的示例。当前版本 demo-v1，未采集真实运行证据。

## 查询缴费记录

在缴费记录列表按客户与日期筛选。声明接口为 `GET /pay/invoice/records`，来源为 billing 服务的 OpenAPI 与 `InvoiceController.queryRecords` 人工源码锚点。
仅说明规范和源码中存在对应声明；未验证测试环境、网关映射或真实查询结果。

## 申请开票

选择一笔可开票的合成记录，查看客户名称、票种及可开票金额。此动作展示申请表单，不直接建立“触发后端接口”的关系。
生产开票属于有副作用业务；本平台讲解模式不执行此类请求。

## 确认开票与金额校验

合成规则：申请金额必须大于零，且不超过该记录的可用金额。代码示例在 `InvoiceService.openInvoice` 中执行本地校验。
声明接口为 `POST /pay/invoice/openInvoice`。示例 Controller、Service、Mapper 与 XML 片段仅为人工可检查的静态来源；不存在运行因果证据。
Mapper 示例写入 `demo_invoice_order`，该 SQL 只是静态文本，导入及阅读期间不执行。

## 查看开票结果

查看合成记录的展示状态。此状态由人工编排，没有实际发票号码、支付信息或业务成功记录。
未采集失败、权限不足等运行路径；不能以这个快照宣称业务全场景已覆盖。
"""

CONTROLLER = """package demo.invoice;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Synthetic Java 8 fixture. Never compiled or executed by VKS. */
@RestController
@RequestMapping("/pay/invoice")
public class InvoiceController {
    private InvoiceService invoiceService;

    @GetMapping("/records")
    public String queryRecords() {
        return "synthetic records only";
    }

    @PostMapping("/openInvoice")
    public void openInvoice(double amount, double availableAmount) {
        invoiceService.openInvoice(amount, availableAmount);
    }
}
"""

SERVICE = """package demo.invoice;

/** Synthetic service: the rule and call are examples, not runtime evidence. */
public class InvoiceService {
    private InvoiceMapper invoiceMapper;

    public void openInvoice(double amount, double availableAmount) {
        if (amount <= 0 || amount > availableAmount) {
            throw new IllegalArgumentException("synthetic amount validation");
        }
        invoiceMapper.insert(amount);
    }
}
"""

MAPPER = """package demo.invoice;

/** Synthetic MyBatis mapper declaration. */
public interface InvoiceMapper {
    int insert(double amount);
}
"""

XML = """<?xml version="1.0" encoding="UTF-8"?>
<!-- Synthetic statement. No datasource and no execution. -->
<mapper namespace="demo.invoice.InvoiceMapper">
  <insert id="insert" parameterType="double">
    INSERT INTO demo_invoice_order (amount, state)
    VALUES (#{amount}, 'SYNTHETIC')
  </insert>
</mapper>
"""


def sha(data: bytes) -> str:
    return sha256(data).hexdigest()


def span(text: str, needle: str, end_needle: str | None = None) -> tuple[int, int]:
    lines = text.splitlines()
    start = next(i + 1 for i, line in enumerate(lines) if needle in line)
    end = next(i + start for i, line in enumerate(lines[start - 1 :]) if end_needle in line) if end_needle else start
    return start, end


def zip_files(files: dict[str, bytes], target: Path) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in sorted(files.items()):
            entry = zipfile.ZipInfo(name, (2026, 10, 25, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def api_key(method: str, route: str) -> str:
    payload = {"service": "billing", "method": method, "route": route, "conditions": {}}
    return "api.billing." + sha(canonical_json(payload))[:28]


def build() -> dict[str, bytes]:
    openapi = {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic billing fixture", "version": "demo-v1"},
        "paths": {
            "/pay/invoice/records": {
                "get": {
                    "operationId": "queryRecords",
                    "summary": "查询合成缴费记录",
                    "responses": {"200": {"description": "Synthetic records"}},
                }
            },
            "/pay/invoice/openInvoice": {
                "post": {
                    "operationId": "openInvoice",
                    "summary": "合成开票声明；平台不调用",
                    "responses": {"200": {"description": "Synthetic declaration only"}},
                }
            },
        },
    }
    files = {
        "preview/index.html": HTML.encode(),
        "docs/invoice-rules.md": MARKDOWN.encode(),
        "api/openapi.json": (json.dumps(openapi, ensure_ascii=False, indent=2) + "\n").encode(),
        "backend/InvoiceController.java": CONTROLLER.encode(),
        "backend/InvoiceService.java": SERVICE.encode(),
        "backend/InvoiceMapper.java": MAPPER.encode(),
        "backend/InvoiceMapper.xml": XML.encode(),
    }
    actions = [
        ("payments.records.query", "查询记录"),
        ("payments.invoice.open", "申请开票"),
        ("payments.invoice.confirm", "确认开票"),
        ("payments.invoice.result", "查看结果"),
    ]
    section_data = [
        ("docs.invoice.query", "查询缴费记录"),
        ("docs.invoice.open", "申请开票"),
        ("docs.invoice.confirm", "确认开票与金额校验"),
        ("docs.invoice.result", "查看开票结果"),
    ]
    sections = []
    for key, title in section_data:
        start = span(MARKDOWN, "## " + title)[0]
        lines = MARKDOWN.splitlines()
        end = next((i + start - 1 for i, line in enumerate(lines[start:], 1) if line.startswith("## ")), len(lines))
        while end > start and not lines[end - 1].strip():
            end -= 1
        sections.append(
            {
                "sectionKey": key,
                "artifactPath": "docs/invoice-rules.md",
                "sha256": sha(files["docs/invoice-rules.md"]),
                "headingPath": ["缴费记录开票说明", title],
                "startLine": start,
                "endLine": end,
            }
        )
    code_specs = [
        (
            "symbol.invoice.controller.query",
            "symbol",
            "backend/InvoiceController.java",
            "demo.invoice.InvoiceController.queryRecords",
            "public String queryRecords()",
            "    }",
        ),
        (
            "symbol.invoice.controller.open",
            "symbol",
            "backend/InvoiceController.java",
            "demo.invoice.InvoiceController.openInvoice",
            "public void openInvoice(",
            "    }",
        ),
        (
            "symbol.invoice.service.open",
            "symbol",
            "backend/InvoiceService.java",
            "demo.invoice.InvoiceService.openInvoice",
            "public void openInvoice(",
            "\n",
        ),
        (
            "symbol.invoice.mapper.insert",
            "symbol",
            "backend/InvoiceMapper.java",
            "demo.invoice.InvoiceMapper.insert",
            "int insert(",
            None,
        ),
        (
            "sql.invoice.insert",
            "sql",
            "backend/InvoiceMapper.xml",
            "demo.invoice.InvoiceMapper.insert",
            '<insert id="insert"',
            "</insert>",
        ),
    ]
    code_entities = []
    for key, kind, path, qname, needle, end_needle in code_specs:
        text = files[path].decode()
        if key == "symbol.invoice.service.open":
            start = span(text, needle)[0]
            end = len(text.splitlines()) - 1
        else:
            start, end = span(text, needle, end_needle)
        code_entities.append(
            {
                "logicalKey": key,
                "entityType": kind,
                "artifactPath": path,
                "sha256": sha(files[path]),
                "qualifiedName": qname,
                "startLine": start,
                "endLine": end,
            }
        )
    ui = {
        "schemaVersion": "1.0",
        "projectKey": PROJECT,
        "baselineKey": BASELINE,
        "pages": [
            {
                "logicalKey": "payments.records",
                "name": "缴费记录开票",
                "artifactPath": "preview/index.html",
                "sha256": sha(files["preview/index.html"]),
                "stateKey": "invoice.application",
            }
        ],
        "actions": [
            {"logicalKey": key, "pageKey": "payments.records", "label": label, "event": "click"}
            for key, label in actions
        ],
        "documentSections": sections,
        "codeEntities": code_entities,
    }
    evidence = []
    for key, label in actions:
        evidence.append(
            {
                "evidenceKey": "evidence." + key,
                "evidenceKind": "static",
                "artifactPath": "preview/index.html",
                "sha256": sha(files["preview/index.html"]),
                "locator": {"kind": "dom_attribute", "pageKey": "payments.records", "actionKey": key},
                "claim": f"合成 HTML 中存在“{label}”动作属性；仅证明静态结构。",
            }
        )
    for section in sections:
        evidence.append(
            {
                "evidenceKey": "evidence." + section["sectionKey"],
                "evidenceKind": "static",
                "artifactPath": section["artifactPath"],
                "sha256": section["sha256"],
                "locator": {
                    "kind": "document_section",
                    "sectionKey": section["sectionKey"],
                    "startLine": section["startLine"],
                    "endLine": section["endLine"],
                },
                "claim": "人工编写的合成章节在当前文件定位存在，业务意义仍需审核。",
            }
        )
    for entity in code_entities:
        evidence.append(
            {
                "evidenceKey": "evidence." + entity["logicalKey"],
                "evidenceKind": "manual",
                "artifactPath": entity["artifactPath"],
                "sha256": entity["sha256"],
                "locator": {
                    "kind": "file_lines",
                    "startLine": entity["startLine"],
                    "endLine": entity["endLine"],
                    "qualifiedName": entity["qualifiedName"],
                },
                "claim": "人工提供的合成源码锚点；未运行自动 Java 调用分析或业务服务。",
            }
        )
    api_lines = files["api/openapi.json"].decode().splitlines()
    evidence.append(
        {
            "evidenceKey": "evidence.api.specification",
            "evidenceKind": "static",
            "artifactPath": "api/openapi.json",
            "sha256": sha(files["api/openapi.json"]),
            "locator": {"kind": "file_lines", "startLine": 1, "endLine": len(api_lines)},
            "claim": "规范中的 method/path 声明；未发出请求或验证真实业务行为。",
        }
    )
    edges = []

    def edge(source, target, kind, refs, reason):
        edges.append(
            {
                "fromLogicalKey": source,
                "toLogicalKey": target,
                "relationType": kind,
                "evidenceRefs": refs,
                "reason": reason,
            }
        )

    for (action_key, _), (section_key, _) in zip(actions, section_data):
        edge(
            action_key,
            section_key,
            "documents",
            ["evidence." + action_key, "evidence." + section_key],
            "显式 logical key 指向对应业务章节；作为候选等待人工审核。",
        )
    query_api = api_key("GET", "/pay/invoice/records")
    submit_api = api_key("POST", "/pay/invoice/openInvoice")
    edge(
        "payments.records.query",
        query_api,
        "may_call",
        ["evidence.payments.records.query", "evidence.api.specification"],
        "合成动作与声明接口的人工关联候选；未建立运行因果。",
    )
    edge(
        "payments.invoice.confirm",
        submit_api,
        "may_call",
        ["evidence.payments.invoice.confirm", "evidence.api.specification"],
        "合成提交动作的潜在接口候选；讲解模式不会发请求。",
    )
    edge(
        "symbol.invoice.controller.query",
        query_api,
        "handler_of",
        ["evidence.symbol.invoice.controller.query", "evidence.api.specification"],
        "人工对照 Spring 注解与规范声明，尚未由自动解析器验证。",
    )
    edge(
        "symbol.invoice.controller.open",
        submit_api,
        "handler_of",
        ["evidence.symbol.invoice.controller.open", "evidence.api.specification"],
        "人工源码锚点与 API 声明的处理关系候选。",
    )
    edge(
        "symbol.invoice.controller.open",
        "symbol.invoice.service.open",
        "may_call",
        ["evidence.symbol.invoice.controller.open", "evidence.symbol.invoice.service.open"],
        "示例源码包含直接调用文本；人工定位，不是运行调用链。",
    )
    edge(
        "symbol.invoice.service.open",
        "symbol.invoice.mapper.insert",
        "may_call",
        ["evidence.symbol.invoice.service.open", "evidence.symbol.invoice.mapper.insert"],
        "示例服务包含 Mapper 调用文本；动态分派未验证。",
    )
    edge(
        "symbol.invoice.mapper.insert",
        "sql.invoice.insert",
        "maps_to_sql",
        ["evidence.symbol.invoice.mapper.insert", "evidence.sql.invoice.insert"],
        "人工检查 namespace/id 的示例映射；未执行 SQL。",
    )
    edge(
        submit_api,
        "docs.invoice.confirm",
        "documents",
        ["evidence.api.specification", "evidence.docs.invoice.confirm"],
        "接口声明对应金额校验说明；业务实现正确性需独立核验。",
    )
    bindings = {
        "schemaVersion": "1.0",
        "projectKey": PROJECT,
        "baselineKey": BASELINE,
        "producer": {"kind": "manual", "name": "VKS reproducible synthetic fixture", "version": "0.1"},
        "evidence": evidence,
        "bindings": edges,
    }
    files["knowledge/ui-manifest.json"] = canonical_json(ui) + b"\n"
    files["knowledge/bindings.json"] = canonical_json(bindings) + b"\n"
    sources = [
        {"source_id": "preview", "role": "prototype", "provider": "local", "root": "preview"},
        {"source_id": "docs", "role": "documentation", "provider": "local", "root": "docs"},
        {"source_id": "api", "role": "api", "service_key": "billing", "provider": "local", "root": "api"},
        {"source_id": "backend", "role": "backend", "service_key": "billing", "provider": "local", "root": "backend"},
        {"source_id": "knowledge", "role": "documentation", "provider": "local", "root": "knowledge"},
    ]
    artifacts = []
    for path, data in sorted(files.items()):
        source_id = path.split("/")[0]
        if path.endswith(".html"):
            kind, media = "prototype", "text/html"
        elif path.endswith(".md"):
            kind, media = "markdown", "text/markdown"
        elif path == "api/openapi.json":
            kind, media = "openapi", "application/json"
        elif path == "knowledge/ui-manifest.json":
            kind, media = "ui_manifest", "application/json"
        elif path == "knowledge/bindings.json":
            kind, media = "binding_manifest", "application/json"
        else:
            kind, media = "source", "application/xml" if path.endswith(".xml") else "text/x-java-source"
        artifacts.append(
            {
                "source_id": source_id,
                "kind": kind,
                "path": path,
                "sha256": sha(data),
                "size_bytes": len(data),
                "media_type": media,
                "encoding": "utf-8",
                "sensitivity": "public",
            }
        )
    for source in sources:
        source["content_sha256"] = source_digest([a for a in artifacts if a["source_id"] == source["source_id"]])
    manifest = {
        "schema_version": "1.0",
        "project_key": PROJECT,
        "baseline_key": BASELINE,
        "import_key": "charge-demo-initial-import",
        "description": "离线合成缴费开票知识包；无真实客户、运行观察或业务请求。所有绑定导入为候选。",
        "sources": sources,
        "artifacts": artifacts,
        "uiManifest": {"path": "knowledge/ui-manifest.json", "sha256": sha(files["knowledge/ui-manifest.json"])},
        "bindings": {"path": "knowledge/bindings.json", "sha256": sha(files["knowledge/bindings.json"])},
        "analysis_origin": "manual",
        "allow_platform_llm": False,
    }
    files["manifest.json"] = canonical_json(manifest) + b"\n"
    return files


def main():
    TARGET.mkdir(parents=True, exist_ok=True)
    files = build()
    for path, content in files.items():
        destination = TARGET / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    zip_target = ROOT / "fixtures/charge-demo.vksbundle.zip"
    zip_files(files, zip_target)
    result = parse_bundle(zip_target.read_bytes(), PROJECT)
    minimal_source = ROOT / "docs/design/contracts/examples/minimal-explicit-binding"
    minimal_files = {
        str(path.relative_to(minimal_source)): path.read_bytes() for path in minimal_source.rglob("*") if path.is_file()
    }
    minimal_target = ROOT / "fixtures/minimal.vksbundle.zip"
    zip_files(minimal_files, minimal_target)
    parse_bundle(minimal_target.read_bytes(), PROJECT)
    print(
        f"Built {zip_target.relative_to(ROOT)}: {len(result.entities)} entities, {len(result.evidence)} evidence, {len(result.relations)} candidate bindings"
    )
    print(f"SHA-256 {sha(zip_target.read_bytes())}")


if __name__ == "__main__":
    main()
