"""A repeatable synthetic requirements evidence trial through official MCP stdio."""

import asyncio
from collections import Counter
from io import BytesIO
from time import perf_counter
from uuid import uuid4
from zipfile import ZipFile

from tests.interactive import test_materials
from tests.mcp.test_requirements_readonly import connect
from tests.ui.test_frontend_browser import live_api

materials = test_materials.materials

CASES = [
    (
        "late-fees",
        "滞纳金豁免",
        "# 滞纳金豁免\n\n仅逾期余额小于 20 元且账单未锁定时允许申请。\n\n审批来源编号 LATE-20。\n",
        "变更滞纳金豁免申请时，如何确定逾期余额上限以及账单锁定例外？",
        "LATE-20",
    ),
    (
        "vat",
        "VAT 分摊",
        "# VAT 分摊\n\nVAT 分摊按照未税金额比例计算。余额尾差补到金额最大的一行，规则 VAT-RND。\n",
        "新增 VAT 分摊，税额比例及余额尾差应怎样处理？",
        "VAT-RND",
    ),
    (
        "invoice-freeze",
        "发票冻结",
        "# 发票冻结\n\n作废中的发票冻结，禁止重复申请。来源编号 INV-HOLD。\n",
        "发票冻结",
        "INV-HOLD",
    ),
    (
        "eod",
        "EOD 对账",
        "# EOD 对账\n\nEOD 对账在本地 23:30 完成。重复记账后退回排查队列 EOD-DUP。\n",
        "日终 EOD 对账遇到重复记账时发送到哪个队列？",
        "EOD-DUP",
    ),
    (
        "ratio",
        "计费比例",
        "# 计费比例\n\n"
        + "历史备注与此规则无关。\n" * 210
        + "\n计费比例按下表应用。\n\n| 范围 | 公式 |\n| --- | --- |\n| 物业 | A乘B CORE-TABLE |\n",
        "计费比例",
        "CORE-TABLE",
    ),
]


def test_five_synthetic_requirements_reference_comparison(materials):
    web, service, actor, manager, projects = materials
    pid, aid = projects[0]
    archive = BytesIO()
    with ZipFile(archive, "w") as bundle:
        for key, _, markdown, _, _ in CASES:
            bundle.writestr(f"{key}.md", markdown)
    item = test_materials.stage(
        service,
        actor,
        manager,
        pid,
        aid,
        "documents",
        archive.getvalue(),
        filename="synthetic-pilot.zip",
    )
    test_materials.activate(service, actor, manager, pid, aid, [item])
    frozen = manager.checkpoint(actor, pid, aid, "synthetic-pilot", str(uuid4()))
    reader = service.create_token(actor, pid, {"name": "synthetic pilot", "scopes": ["knowledge:read"]})

    async def measure():
        calls, cited, excerpt = Counter(), Counter(), Counter()
        timings = {}
        with live_api(web.app) as url:
            async with connect("stdio", url, reader["token"]) as sdk:
                await sdk.initialize()
                scope = {"project_id": pid, "release_id": frozen["id"]}
                for key, section, _, query, evidence in CASES:
                    for mode in ("legacy", "keyword"):
                        started = perf_counter()
                        result = await sdk.call_tool(
                            "search_knowledge",
                            {
                                **scope,
                                "query": query,
                                "kinds": ["section"],
                                "retrieval_mode": mode,
                            },
                        )
                        assert not result.isError, result.content
                        calls[mode] += 1
                        hit = next(
                            (
                                row
                                for row in result.structuredContent["data"][:3]
                                if row["entity"]["name"] == section and "locator" in row
                            ),
                            None,
                        )
                        if not hit:
                            continue
                        if evidence in hit["excerpt"]:
                            excerpt[mode] += 1
                        locator = hit["locator"]
                        assert locator["release_id"] == scope["release_id"]
                        document = await sdk.call_tool(
                            "get_workspace_document",
                            {
                                **scope,
                                "document_id": locator["document_id"],
                                "revision_id": locator["document_revision_id"],
                                "section_revision_ids": [locator["section_revision_id"]],
                                "max_chars": 6000,
                            },
                        )
                        calls[mode] += 1
                        assert not document.isError, document.content
                        parts = document.structuredContent["data"]["sections"]
                        assert len(parts) == 1 and parts[0]["locator"] == locator
                        if evidence in parts[0]["content"]:
                            cited[mode] += 1
                            timings[(key, mode)] = (perf_counter() - started) * 1000
        return calls, cited, excerpt, timings

    calls, cited, excerpt, timings = asyncio.run(measure())
    print(
        {
            "calls": dict(calls),
            "verified": dict(cited),
            "shown_in_excerpt": dict(excerpt),
            "end_to_end_ms": {"/".join(key): round(value, 1) for key, value in timings.items()},
        }
    )
    assert calls == {"legacy": 7, "keyword": 10}
    assert cited == {"legacy": 2, "keyword": 5}
    assert excerpt["legacy"] == 1
