from vks_api.models import Audit

"Opt-in real PostgreSQL tests. Use an isolated migrated DB via VKS_TEST_POSTGRES_URL."
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from uuid import uuid4
import os
import pytest
from sqlalchemy import select
from vks_api.models import Job, Release
from vks_api.service import Service, DomainError


@pytest.fixture(scope="session")
def postgres_store(tmp_path_factory):
    return tmp_path_factory.mktemp("pg-store")


@pytest.fixture
def postgres(postgres_store):
    url = os.getenv("VKS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set VKS_TEST_POSTGRES_URL to an isolated migrated PostgreSQL database")
    directory = postgres_store
    service = Service(directory, url)
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(
        owner, {"key": "pg-" + uuid4().hex, "name": "PostgreSQL synthetic verification"}, "create-" + uuid4().hex
    )
    second = Service(directory, url)
    yield (service, second, owner, project)
    service.stop_worker()
    second.stop_worker()
    service.engine.dispose()
    second.engine.dispose()


def import_demo(service, owner, project):
    job = service.demo_import(owner, project["id"], "demo")
    service.process_job(job["id"])
    result = service.get_job(owner, job["id"])
    assert result["state"] == "succeeded", result
    return service.workspace(owner, project["id"], result["result"]["analysis_set_id"])


def test_postgres_import_review_publish_and_immutable_read(postgres):
    service, second, owner, project = postgres
    workspace = import_demo(service, owner, project)
    assert {"handler_of", "maps_to_sql", "documents"} <= {r["relation_type"] for r in workspace["relations"]}
    for relation in workspace["relations"]:
        service.set_relation_state(owner, relation["id"], {"excluded": "confirm" == "reject"}, relation["row_version"])
    candidate = service.save_snapshot(
        owner, project["id"], {"analysis_set_id": workspace["selected_analysis_set"]["id"]}, "precheck"
    )
    release = candidate
    assert (
        second.workspace(owner, project["id"], release_id=release["id"])["selected_release"]["manifest_sha256"]
        == release["manifest_sha256"]
    )
    assert candidate["id"] == release["id"]
    with second.db() as session:
        rows = session.scalars(select(Release).where(Release.project_id == project["id"])).all()
        assert len(rows) == 1


def test_postgres_concurrent_review_returns_one_version_conflict(postgres):
    service, second, owner, project = postgres
    workspace = import_demo(service, owner, project)
    relation = workspace["relations"][0]
    start = Barrier(2)

    def review(instance, decision):
        start.wait(timeout=5)
        try:
            return instance.set_relation_state(
                owner, relation["id"], {"excluded": decision == "reject"}, relation["row_version"]
            )
        except DomainError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(review, service, "confirm"), pool.submit(review, second, "reject")]
        results = [future.result(timeout=10) for future in futures]
    assert sum((isinstance(result, dict) for result in results)) == 1
    assert 412 in results
    with service.db() as session:
        reviews = session.scalars(select(Audit).where(Audit.resource_id == relation["id"])).all()
        assert len(reviews) == 1


def test_postgres_duplicate_dispatch_claims_one_attempt(postgres):
    service, second, owner, project = postgres
    job = service.demo_import(owner, project["id"], "dispatch")
    parsing = Event()
    release = Event()
    call_lock = Lock()
    calls = []
    original = service._parse

    def delayed(*args):
        with call_lock:
            calls.append(1)
        result = original(*args)
        parsing.set()
        assert release.wait(timeout=5)
        return result

    service._parse = delayed
    second._parse = delayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.process_job, job["id"])
        assert parsing.wait(timeout=5)
        duplicate = pool.submit(second.process_job, job["id"])
        duplicate.result(timeout=5)
        release.set()
        first.result(timeout=5)
    assert len(calls) == 1
    assert service.get_job(owner, job["id"])["attempt"] == 1
    assert service.get_job(owner, job["id"])["state"] == "succeeded"


def test_postgres_same_import_idempotency_key_returns_same_job(postgres):
    service, second, owner, project = postgres
    path = Path(__file__).resolve().parents[2] / "fixtures/charge-demo.vksbundle.zip"
    first = service.demo_import(owner, project["id"], "upload-fixture")
    with service.db() as session:
        payload = session.get(Job, first["id"]).input_json
    start = Barrier(2)

    def enqueue(instance):
        start.wait(timeout=5)
        return instance.import_bundle(owner, project["id"], payload, "same-key")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(enqueue, instance) for instance in (service, second)]
        results = [future.result(timeout=10) for future in futures]
    assert results[0]["id"] == results[1]["id"]
    conflict = dict(payload, kind="bundle", artifact_ids=["different"])
    with pytest.raises(DomainError):
        service.import_bundle(owner, project["id"], conflict, "same-key")


def test_postgres_model_budget_reservations_are_atomic_across_workers(postgres):
    from vks_api.ai_gateway import AiService, PolicyInput, ModelLedger

    service, second, owner, project = postgres
    config = PolicyInput(max_output_tokens=100, daily_token_limit=2200).model_dump()
    start = Barrier(3)

    def reserve(instance):
        start.wait(timeout=5)
        try:
            return AiService(instance).reserve(owner, project["id"], config, 1000)
        except DomainError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = [
            f.result(timeout=10)
            for f in [pool.submit(reserve, service), pool.submit(reserve, second), pool.submit(reserve, service)]
        ]
    assert sum((isinstance(x, str) for x in results)) == 2 and results.count(429) == 1
    with service.db() as session:
        ledger = session.scalar(select(ModelLedger).where(ModelLedger.project_id == project["id"]))
        assert ledger.reserved_tokens == 2200 and ledger.spent_tokens == 0


def test_postgres_readonly_graph_search_snapshot_and_current_permissions(postgres):
    service, second, owner, project = postgres
    w = import_demo(service, owner, project)
    for relation in w["relations"]:
        service.set_relation_state(owner, relation["id"], {"excluded": "confirm" == "reject"}, relation["row_version"])
    candidate = service.save_snapshot(
        owner, project["id"], {"analysis_set_id": w["selected_analysis_set"]["id"]}, "read-candidate"
    )
    ticket = None
    released = candidate
    action = w["actions"][0]["entity_id"]
    rid = released["id"]
    before = service.action_context(owner, project["id"], action, release_id=rid, depth=3, max_nodes=100)
    token = service.create_token(
        owner, project["id"], {"name": "readonly", "scopes": ["knowledge:read"], "expires_in_days": 1}
    )
    actor = second.authenticate(token["token"], "token")
    limited = second.action_context(actor, project["id"], action, release_id=rid, depth=3, max_nodes=100)
    assert limited["release_id"] == rid
    assert second.search(actor, project["id"], {"query": "开票", "release_id": rid})
    limited_graph = second.graph(actor, project["id"], action, release_id=rid, depth=3, max_nodes=100)
    assert not any((e["type"] in ("symbol", "sql", "table") for e in limited_graph["nodes"]))
    again = service.action_context(owner, project["id"], action, release_id=rid, depth=3, max_nodes=100)
    assert {e["id"] for e in before["evidence"]} == {e["id"] for e in again["evidence"]}
    service.revoke_token(owner, project["id"], token["id"])
    with pytest.raises(DomainError):
        second.graph(actor, project["id"], action, release_id=rid)
