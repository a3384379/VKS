"""Opt-in V2 concurrent edits, durable candidates and actual PG restore."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
import os
import pytest
from test_journeys import journey
from vks_api.service import Service, DomainError
from vks_api.journeys import JourneyService
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from scripts.backup_restore import backup, restore


@pytest.mark.skipif(not os.getenv("VKS_TEST_V2_POSTGRES_URL"), reason="未配置独立 V2 PostgreSQL 验证与恢复数据库")
def test_postgres_v2_concurrent_cas_candidate_review_publish_checkpoint_restore(tmp_path, monkeypatch):
    source = os.environ["VKS_TEST_V2_POSTGRES_URL"]
    target = os.environ["VKS_TEST_V2_RECOVERY_TARGET_URL"]
    monkeypatch.setenv("VKS_AUTO_MIGRATE", "1")
    root = tmp_path / "original"
    service = Service(root, source)
    second = Service(root, source)
    _, actor = service.login(service.bootstrap_token)
    project = service.create_project(actor, dict(key="charge-demo", name="Synthetic V2 PG"), "project")
    pid = project["id"]
    job = service.demo_import(actor, pid, "demo")
    service.process_job(job["id"])
    assert service.get_job(actor, job["id"])["state"] == "succeeded"
    w = service.workspace(actor, pid)
    aid = w["selected_analysis_set"]["id"]
    journeys = JourneyService(service)
    row = journeys.create(actor, pid, dict(analysis_set_id=aid, dsl=journey(w)), "flow")
    gate = Barrier(2)

    def update(index):
        dsl = deepcopy(row["dsl"])
        dsl["title"] = "Concurrent " + str(index)
        gate.wait(timeout=10)
        try:
            return JourneyService([service, second][index]).update(
                actor, row["journey_id"], dict(analysis_set_id=aid, dsl=dsl), row["revision_id"], "edit-" + str(index)
            )
        except DomainError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, range(2)))
    assert sum(isinstance(value, dict) for value in outcomes) == 1 and 412 in outcomes
    task = KnowledgeTaskService(service).create(
        actor,
        pid,
        TaskInput(
            analysis_set_id=aid, task_kind="business_flow", action_ids=[w["actions"][0]["entity_id"]]
        ).model_dump(),
        "knowledge-task",
    )
    service.process_job(task["id"])
    task_result = KnowledgeTaskService(second).get(actor, task["id"])
    assert task_result["state"] == "succeeded" and task_result["stage_artifacts"]
    for item in journeys.list(actor, pid, aid):
        journeys.review(
            actor,
            item["journey_id"],
            dict(
                analysis_set_id=aid,
                revision_id=item["revision_id"],
                decision="approve",
                reason="Synthetic PG human review",
            ),
            item["revision_id"],
            "review-" + item["revision_id"],
        )
    for item in service.workspace(actor, pid, aid)["relations"]:
        service.review_relation(
            actor,
            item["id"],
            dict(decision="confirm", reason="Synthetic", evidence_ids=item["evidence_ids"]),
            item["row_version"],
        )
    candidate = service.request_release(actor, pid, dict(analysis_set_id=aid), "precheck")
    ticket = service.confirm_release(actor, candidate["candidate_id"])
    release = service.publish(
        actor,
        candidate["candidate_id"],
        {k: ticket[k] for k in ("human_confirmation_ref", "expected_manifest_sha256")}
        | dict(change_note="Synthetic V2 recovery"),
        "publish",
    )
    from packages.analyzers.vks_ingest import ParsedBundle, _entity

    with service.db() as db:
        unicode_analysis = service._commit_bundle(
            db,
            pid,
            actor.id,
            ParsedBundle(
                manifest=dict(baseline_key="unicode-search"),
                entities=[
                    _entity(
                        "document.search-unicode",
                        "document",
                        "Straße",
                        "Unrelated content",
                        "",
                        {},
                        {"search_label": "İstanbul"},
                    )
                ],
            ),
        )["analysis_set_id"]
    assert any(
        row["entity"]["name"] == "Straße"
        for row in service.search(actor, pid, dict(analysis_set_id=unicode_analysis, query="strasse", limit=10))
    )
    assert service.search(actor, pid, dict(analysis_set_id=unicode_analysis, query="i̇stanbul", limit=10))
    old_token = service.create_token(actor, pid, dict(name="old", scopes=["knowledge:read"]))["token"]
    before = [(r["revision_id"], r["content_sha256"]) for r in journeys.list(actor, pid, release_id=release["id"])]
    report = backup(
        root, tmp_path / "pg-v2.zip", database_url=source, pg_tools_container=os.environ["VKS_TEST_PG_TOOLS_CONTAINER"]
    )
    restored_root = tmp_path / "restored"
    restore(
        report["path"],
        restored_root,
        expected_sha256=report["sha256"],
        database_url=target,
        pg_tools_container=os.environ["VKS_TEST_PG_TOOLS_CONTAINER"],
    )
    rebuilt = Service(restored_root, target)
    _, recovered = rebuilt.login(rebuilt.bootstrap_token)
    assert before == [
        (r["revision_id"], r["content_sha256"])
        for r in JourneyService(rebuilt).list(recovered, pid, release_id=release["id"])
    ]
    assert (
        rebuilt.workspace(recovered, pid, release_id=release["id"])["selected_release"]["manifest_sha256"]
        == release["manifest_sha256"]
    )
    recovered_task = KnowledgeTaskService(rebuilt).get(recovered, task["id"])
    assert recovered_task["report"] == task_result["report"]
    assert recovered_task["stage_artifacts"] == task_result["stage_artifacts"]
    point = recovered_task["stage_artifacts"][0]
    assert (
        KnowledgeTaskService(rebuilt).stage_artifact(recovered, task["id"], point["sha256"])["sha256"]
        == point["sha256"]
    )
    with pytest.raises(DomainError):
        rebuilt.authenticate(old_token, "token")
    for instance in (service, second, rebuilt):
        instance.engine.dispose()
