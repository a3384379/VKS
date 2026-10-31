"""Frozen synthetic JF-A scale fixture. Its labels are not independent human Gold."""

from io import BytesIO
from pathlib import Path
from hashlib import sha256
import json
import zipfile
from scripts import build_demo
from packages.analyzers.vks_ingest import canonical_json, source_digest, parse_bundle

ROOT = Path(__file__).resolve().parents[1]


def build():
    files = build_demo.build()
    old = json.loads(files.pop("manifest.json"))
    project = "pilot-jfa"
    baseline = "synthetic-jfa-v1"
    ui = json.loads(files["knowledge/ui-manifest.json"])
    ui["projectKey"] = project
    ui["baselineKey"] = baseline
    keys = [a["logicalKey"] for a in ui["actions"]] + [f"jfa.action.{i:02}" for i in range(5, 31)]
    labels = [a["label"] for a in ui["actions"]] + [f"合成操作{i:02}" for i in range(5, 31)]
    ui["actions"] = [
        {"logicalKey": key, "pageKey": "payments.records", "label": label, "event": "click"}
        for key, label in zip(keys, labels)
    ]
    html = '<!doctype html><html lang="zh-CN"><body><main data-vks-page="payments.records"><h1>JF-A 合成工作台</h1><p>30个静态动作；无真实客户、业务请求和运行因果。</p>'
    html += (
        "".join(f'<button data-vks-action="{key}" type="button">{label}</button>' for key, label in zip(keys, labels))
        + "</main></body></html>"
    )
    files["preview/index.html"] = html.encode()
    markdown = ""
    sections = []
    for i in range(1, 31):
        heading = "# JF-A 合成知识基线" if i == 1 else f"## 合成规则{i:02}"
        start = len(markdown.splitlines()) + 1
        markdown += heading + "\n\n" + f"第{i:02}条合成规则：仅作为版本、定位和权限验收资料，未验证真实生产业务。\n\n"
        sections.append(
            {
                "sectionKey": f"jfa.doc.section{i:02}",
                "artifactPath": "docs/invoice-rules.md",
                "headingPath": ["JF-A 合成知识基线"] + ([] if i == 1 else [f"合成规则{i:02}"]),
                "startLine": start,
                "endLine": start + 2,
            }
        )
    files["docs/invoice-rules.md"] = markdown.encode()
    for section in sections:
        section["sha256"] = sha256(files["docs/invoice-rules.md"]).hexdigest()
    ui["documentSections"] = sections
    spec = json.loads(files["api/openapi.json"])
    for i in range(3, 16):
        spec["paths"][f"/jfa/read/{i:02}"] = {
            "get": {
                "operationId": f"read{i:02}",
                "summary": f"合成声明{i:02}",
                "responses": {"200": {"description": "Synthetic only"}},
            }
        }
    files["api/openapi.json"] = json.dumps(spec, ensure_ascii=False, indent=2).encode()
    java = "package demo.jfa;\nimport org.springframework.web.bind.annotation.*;\n@RestController\npublic class PilotController {\n"
    for i in range(3, 16):
        java += f' @GetMapping("/jfa/read/{i:02}") public String read{i:02}() {{ return "synthetic"; }}\n'
    java += "}\n"
    files["backend/PilotController.java"] = java.encode()
    ui["pages"][0]["name"] = "JF-A · 30动作合成试点"
    ui["pages"][0]["sha256"] = sha256(files["preview/index.html"]).hexdigest()
    evidence = []
    relations = []
    for i, key in enumerate(keys, 1):
        evidence.append(
            {
                "evidenceKey": f"jfa.ev.action{i:02}",
                "evidenceKind": "static",
                "artifactPath": "preview/index.html",
                "sha256": sha256(files["preview/index.html"]).hexdigest(),
                "locator": {"kind": "dom_attribute", "pageKey": "payments.records", "actionKey": key},
                "claim": "合成静态DOM锚点；不是运行证据",
            }
        )
    for i, section in enumerate(sections, 1):
        evidence.append(
            {
                "evidenceKey": f"jfa.ev.section{i:02}",
                "evidenceKind": "static",
                "artifactPath": section["artifactPath"],
                "sha256": section["sha256"],
                "locator": {
                    "kind": "document_section",
                    "sectionKey": section["sectionKey"],
                    "startLine": section["startLine"],
                    "endLine": section["endLine"],
                },
                "claim": "固定合成文档章节",
            }
        )
    evidence.append(
        {
            "evidenceKey": "jfa.ev.api",
            "evidenceKind": "static",
            "artifactPath": "api/openapi.json",
            "sha256": sha256(files["api/openapi.json"]).hexdigest(),
            "locator": {
                "kind": "file_lines",
                "startLine": 1,
                "endLine": len(files["api/openapi.json"].decode().splitlines()),
            },
            "claim": "合成规范声明，未发请求",
        }
    )
    apis = [build_demo.api_key(method.upper(), route) for route, methods in spec["paths"].items() for method in methods]
    for i, key in enumerate(keys, 1):
        relations.append(
            {
                "fromLogicalKey": key,
                "toLogicalKey": sections[i - 1]["sectionKey"],
                "relationType": "documents",
                "evidenceRefs": [f"jfa.ev.action{i:02}", f"jfa.ev.section{i:02}"],
                "reason": "合成预期映射；业务含义需核对",
            }
        )
        relations.append(
            {
                "fromLogicalKey": key,
                "toLogicalKey": apis[(i - 1) % 15],
                "relationType": "may_call",
                "evidenceRefs": [f"jfa.ev.action{i:02}", "jfa.ev.api"],
                "reason": "合成潜在关联；不宣称运行因果",
            }
        )
    bindings = {
        "schemaVersion": "1.0",
        "projectKey": project,
        "baselineKey": baseline,
        "producer": {"kind": "manual", "name": "Synthetic JF-A generator", "version": "1"},
        "evidence": evidence,
        "bindings": relations,
    }
    files["knowledge/ui-manifest.json"] = canonical_json(ui)
    files["knowledge/bindings.json"] = canonical_json(bindings)
    manifest = {
        **old,
        "project_key": project,
        "baseline_key": baseline,
        "import_key": "jfa-synthetic-initial",
        "description": "合成30动作/15规范化API/30章节基线；非独立人工Gold；allow_platform_llm=false",
    }
    rows = {a["path"]: a for a in old["artifacts"]}
    rows["backend/PilotController.java"] = {
        **rows["backend/InvoiceController.java"],
        "path": "backend/PilotController.java",
    }
    for path, raw in files.items():
        row = rows[path]
        row["sha256"] = sha256(raw).hexdigest()
        row["size_bytes"] = len(raw)
    manifest["artifacts"] = [rows[path] for path in sorted(files)]
    for source in manifest["sources"]:
        source["content_sha256"] = source_digest(
            [a for a in manifest["artifacts"] if a["source_id"] == source["source_id"]]
        )
    for key in ("uiManifest", "bindings"):
        manifest[key]["sha256"] = sha256(files[manifest[key]["path"]]).hexdigest()
    files["manifest.json"] = canonical_json(manifest)
    return files


def bundle_bytes(files=None):
    files = files or build()
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in sorted(files.items()):
            item = zipfile.ZipInfo(name, (2026, 10, 25, 0, 0, 0))
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            archive.writestr(item, raw, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


if __name__ == "__main__":
    target = ROOT / "fixtures/pilot-jfa.vksbundle.zip"
    raw = bundle_bytes()
    parsed = parse_bundle(raw, "pilot-jfa")
    target.write_bytes(raw)
    report = {
        "fixture": target.name,
        "sha256": sha256(raw).hexdigest(),
        "actions": len([e for e in parsed.entities if e["type"] == "action"]),
        "normalized_apis": len(
            {
                (e["metadata"]["method"], e["metadata"]["normalized_route"])
                for e in parsed.entities
                if e["type"] == "api"
            }
        ),
        "chapters": len([e for e in parsed.entities if e["type"] == "section"]),
        "candidate_relations": len(parsed.relations),
        "independently_human_reviewed": False,
        "runtime_validated": False,
    }
    (ROOT / "fixtures/pilot-jfa.summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
