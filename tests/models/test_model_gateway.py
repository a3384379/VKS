from io import BytesIO
from pathlib import Path
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import zipfile
import pytest
from sqlalchemy import select
from vks_api.service import Service, DomainError
from vks_api.ai_gateway import AiService, PolicyInput, ModelLedger
from vks_api.models import Artifact, Relation


@pytest.fixture
def system(tmp_path, monkeypatch):
    service = Service(tmp_path / "store")
    _, actor = service.login(service.bootstrap_token)
    p = service.create_project(actor, {"key": "charge-demo", "name": "模型测试"}, "project")
    with zipfile.ZipFile(Path("fixtures/charge-demo.vksbundle.zip")) as z:
        files = {i.filename: z.read(i) for i in z.infolist()}
    manifest = json.loads(files["manifest.json"])
    manifest["allow_platform_llm"] = True
    files["manifest.json"] = json.dumps(manifest).encode()
    packed = BytesIO()
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    with service.db() as s:
        result = service._commit_bundle(s, p["id"], actor.id, service._parse(packed.getvalue(), p["key"]))
    requests = []
    response = {"bad_id": False, "missing_citation": False, "revoke": None, "mutate": False, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, dict(self.headers), body))
            if response["revoke"]:
                response["revoke"]()
            content = body.get("input", body.get("messages"))[1]["content"]
            context = json.loads(content)
            ref = "unknown-evidence" if response["bad_id"] else context["evidence"][0]["evidence_id"]
            if "vks_plan" in json.dumps(body.get("text", body.get("response_format"))):
                answer = response["plan"](context)
            elif "document" in json.dumps(body.get("text", body.get("response_format"))):
                answer = {
                    "title": "规则摘要",
                    "paragraphs": [{"markdown": "查看规则依照固定文档。", "evidence_ids": [ref]}],
                    "unknowns": ["未进行真实环境验收"],
                }
            else:
                answer = {
                    "answer": "查看规则依照固定文档。",
                    "evidence_ids": [] if response["missing_citation"] else [ref],
                    "unknowns": [],
                }
            self.send_response(response["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if "input" in body:
                payload = {
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                }
            else:
                payload = {
                    "choices": [{"message": {"content": json.dumps(answer)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                }
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", endpoint)
    monkeypatch.setenv("VKS_MODEL_ALLOW_LOOPBACK", "1")
    ai = AiService(service)
    config = PolicyInput(
        enabled=True,
        endpoint=endpoint,
        model_id="synthetic-fixture",
        secret_ref="model-default",
        max_input_tokens=30000,
    ).model_dump()
    monkeypatch.setenv("VKS_MODEL_API_KEY", "synthetic-model-secret")
    ai.set_policy(actor, p["id"], config, 1)
    yield service, actor, p, result["analysis_set_id"], ai, requests, response, config
    server.shutdown()
    thread.join()
    service.engine.dispose()


def test_both_protocols_actual_http_citations_budget_and_no_actor_token(system):
    s, a, p, aid, ai, requests, response, config = system
    result = s.question(a, p["id"], {"use_model": True, "question": "查看规则", "analysis_set_id": aid})
    assert result["evidence_ids"] and result["model_id"] == "synthetic-fixture"
    assert requests[0][0] == "/v1/responses"
    assert requests[0][1]["Authorization"] == "Bearer synthetic-model-secret"
    assert requests[0][2]["store"] is False
    assert not requests[0][2].get("tools")
    config = {**config, "protocol": "chat_completions"}
    ai.set_policy(a, p["id"], config, 2)
    result = ai.run(a, p["id"], "查看规则", analysis_set_id=aid)
    assert requests[-1][0] == "/v1/chat/completions" and result["evidence_ids"]
    usage = ai.usage(a, p["id"])
    assert len(usage) == 2 and all(u["state"] == "succeeded" for u in usage)
    with s.db() as db:
        ledger = db.scalar(select(ModelLedger))
        assert ledger.reserved_tokens == 0 and ledger.spent_tokens == 300


def test_document_task_reuses_completed_proposal_after_validation_interruption_without_second_http_call(
    system, monkeypatch
):
    from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
    from vks_api.models import Entity

    service, actor, project, aid, ai, requests, response, config = system
    original = KnowledgeTaskService._validate_report
    validations = []

    def interrupted(self, jid, report):
        validations.append(jid)
        if len(validations) == 1:
            raise DomainError("VKS_DEPENDENCY_UNAVAILABLE", "Synthetic interrupted validation", 503)
        return original(self, jid, report)

    monkeypatch.setattr(KnowledgeTaskService, "_validate_report", interrupted)
    tasks = KnowledgeTaskService(service)
    job = tasks.create(
        actor,
        project["id"],
        TaskInput(analysis_set_id=aid, task_kind="document_draft", max_model_calls=1).model_dump(),
        "resume-document",
    )
    service.process_job(job["id"])
    assert tasks.get(actor, job["id"])["state"] == "failed"
    assert len(requests) == 1
    service.retry_job(actor, job["id"])
    service.process_job(job["id"])
    result = tasks.get(actor, job["id"])
    assert result["state"] == "succeeded", result
    assert len(requests) == 1
    assert [row["stage"] for row in result["stage_history"] if row.get("cached") == "true"] == [
        "Collect",
        "Analyze",
        "Propose",
    ]
    with service.db() as db:
        documents = [
            row
            for row in service._revisions(db, aid)
            if db.get(Entity, row.entity_id).type == "document"
            and row.metadata_json.get("source_kind") == "ai_generated"
        ]
        assert len(documents) == 1
    assert result["report"]["candidate_only"] and result["report"]["model_calls"] == 1


@pytest.mark.parametrize("flag", ["bad_id", "missing_citation"])
def test_hallucinated_or_missing_references_fail_and_usage_is_recorded(system, flag):
    s, a, p, aid, ai, requests, response, config = system
    response[flag] = True
    with pytest.raises(DomainError) as error:
        ai.run(a, p["id"], "规则", analysis_set_id=aid)
    assert error.value.code == "VKS_INVALID_EVIDENCE"
    assert ai.usage(a, p["id"])[0]["state"] == "failed"


def test_manifest_no_llm_is_enforced_before_any_model_request(system):
    s, a, p, aid, ai, requests, response, config = system
    with s.db() as db:
        for art in db.scalars(select(Artifact)):
            art.metadata_json = {**art.metadata_json, "allow_platform_llm": False}
    result = ai.run(a, p["id"], "规则", analysis_set_id=aid)
    assert result["model_id"] == "none:insufficient-evidence" and not requests


def test_unregistered_origin_and_budget_block_before_network(system):
    s, a, p, aid, ai, requests, response, config = system
    with pytest.raises(DomainError):
        ai.set_policy(a, p["id"], {**config, "endpoint": "http://169.254.169.254"}, 2)
    ai.set_policy(a, p["id"], {**config, "daily_token_limit": 1000}, 2)
    with pytest.raises(DomainError) as e:
        ai.run(a, p["id"], "规则", analysis_set_id=aid)
    assert e.value.code == "VKS_LIMIT_EXCEEDED" and not requests


def test_policy_revocation_while_in_flight_discards_output(system):
    s, a, p, aid, ai, requests, response, config = system
    response["revoke"] = lambda: ai.set_policy(a, p["id"], {**config, "enabled": False}, 2)
    with pytest.raises(DomainError) as e:
        ai.run(a, p["id"], "规则", analysis_set_id=aid)
    assert e.value.code == "VKS_VERSION_CONFLICT" and ai.usage(a, p["id"])[0]["state"] == "failed"


def test_generated_document_is_immutable_draft_with_real_evidence_and_human_gate(system):
    s, a, p, aid, ai, requests, response, config = system
    job = s.create_analysis_job(a, aid, {"kind": "generate_documents"}, "generate")
    s.process_job(job["id"])
    result = s.get_job(a, job["id"])
    assert result["state"] == "succeeded", result
    w = s.workspace(a, p["id"], aid)
    generated = next(d for d in w["documents"] if d["metadata"].get("source_kind") == "ai_generated")
    raw, _ = s.artifact_bytes(a, p["id"], generated["artifact_id"])
    assert "查看规则".encode() in raw
    c = s.request_release(a, p["id"], {"analysis_set_id": aid}, "precheck")
    assert any("文档修订" in b for b in c["blockers"])
    with s.db() as db:
        links = list(
            db.scalars(
                select(Relation).where(
                    Relation.analysis_set_id == aid,
                    Relation.from_revision_id == generated["entity_revision_id"],
                    Relation.relation_type == "documents",
                )
            )
        )
        assert links and all(x.evidence_ids for x in links)
