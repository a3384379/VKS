"""Whole-task SQL budget, including the vector store; synthetic model only."""

import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from threading import Lock, current_thread

import pytest
from sqlalchemy import event, select
from vks_api.ai_gateway import ModelPolicy
from vks_api.bulk_association import BulkAssociationService
from vks_api.vector_rag import RagSettings, VectorRag, VectorStore, normalize

from tests.interactive import test_bulk_association

materials = test_bulk_association.materials


@pytest.mark.parametrize(
    "vectors,reassociate,documents",
    [
        (False, False, 1),
        (True, False, 1),
        (False, True, 1),
        (True, True, 1),
        (True, False, 16),
        (True, True, 16),
    ],
)
def test_256_target_personal_task_has_under_1000_queries(materials, monkeypatch, vectors, reassociate, documents):
    source = "<title>收费项</title>" + "".join(f'<button id="action{i}">收费项操作{i}</button>' for i in range(255))
    service, actor, material_manager, pid, aid, _, _, calls, _ = test_bulk_association.setup(
        materials, monkeypatch, source
    )
    service.personal_mode = True
    if documents > 1:
        rows = [
            test_bulk_association.test_materials.stage(
                service,
                actor,
                material_manager,
                pid,
                aid,
                "documents",
                f"# 收费项业务说明{n}\n\n新增收费项需要填写名称、选择类别；收费项操作{n}应检查业务前提和结果。",
                name=f"收费文档{n}",
                filename=f"fees-{n}.md",
            )
            for n in range(1, documents)
        ]
        test_bulk_association.test_materials.activate(service, actor, material_manager, pid, aid, rows)

        def model(config, payload):
            data = json.loads(payload["input"][1]["content"])
            calls.append(data)
            number = re.search(r"\d+", data["control"]["selector"])
            offset = int(number.group()) if number else 0
            return test_bulk_association.response(data["chapters"][offset % len(data["chapters"])]["revision_id"])

        monkeypatch.setattr(test_bulk_association.ai_gateway, "request_model", model)

    with service.db() as s:
        policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
        policy.settings = {**policy.settings, "max_output_tokens": 8000}
    if vectors:
        store = VectorStore(service.data_dir)
        with store.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO config VALUES (?,?)",
                (pid, RagSettings(enabled=True, dimensions=64).model_dump_json()),
            )
        monkeypatch.setattr(
            VectorRag,
            "embed",
            lambda self, actor, pid, settings, config, texts: [normalize([1.0] + [0.0] * 63, 64) for _ in texts],
        )
    manager = BulkAssociationService(service)
    if reassociate:
        initial = test_bulk_association.enqueue(manager, actor, pid, aid, concurrency=3)
        service.process_job(initial["id"])
        calls.clear()
    job = test_bulk_association.enqueue(
        manager, actor, pid, aid, concurrency=3, mode="all" if reassociate else "missing"
    )
    counts, lock = Counter(), Lock()
    owners = Counter()

    def count(kind, sql):
        if sql.lstrip().upper().startswith(("SELECT", "WITH", "PRAGMA")):
            with lock:
                counts[kind] += 1
                if kind == "main" and os.getenv("VKS_QUERY_DIAGNOSTICS"):
                    frame = sys._getframe(1)
                    while frame and "/vks_api/" not in frame.f_code.co_filename:
                        frame = frame.f_back
                    caller = frame.f_code.co_name if frame else "unknown"
                    table = re.search(r"\bFROM\s+([a-z_]+)", sql, re.I)
                    owners[(current_thread().name, caller, table.group(1) if table else "unknown")] += 1

    event.listen(
        service.engine, "before_cursor_execute", lambda conn, cursor, sql, params, context, many: count("main", sql)
    )
    original_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        db = original_connect(database, *args, **kwargs)
        if str(database).endswith("rag-vectors.sqlite"):
            db.set_trace_callback(lambda sql: count("vector", sql))
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    service.process_job(job["id"])
    # Count the actual task only; reporting its result is a separate request.
    with lock:
        fixed = dict(counts)
        report = {
            "counts": fixed,
            "vectors": vectors,
            "reassociate": reassociate,
            "documents": documents,
            "owners": [
                {"thread": key[0], "caller": key[1], "table": key[2], "calls": n} for key, n in owners.most_common()
            ],
        }
    if os.getenv("VKS_QUERY_DIAGNOSTICS"):
        path = Path(os.environ["VKS_QUERY_DIAGNOSTICS"])
        path = path.with_name(
            f"{path.stem}-vectors{int(vectors)}-reassociate{int(reassociate)}-documents{documents}{path.suffix}"
        )
        path.write_text(json.dumps(report, indent=2))
    total = sum(fixed.values())
    result = service.get_job(actor, job["id"])
    assert result["state"] == "succeeded" and result["result"]["saved"] == 256, result
    assert len(calls) == 256
    assert total < 1000, fixed
