"""A batch reuses saved explanations and the HTML includes their exact citations."""

import base64
import gzip
import json
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.explanation_batches import KIND, explanation_history
from vks_api.materials import MaterialService
from vks_api.models import Artifact, uid
from vks_api.readonly_export import projection
from vks_api.service import future
from vks_api.vector_rag import VectorRag

from tests.interactive import test_assistant
from tests.interactive.test_explanation_separation import saved_binding
from tests.interactive.test_knowledge import current, form, root
from tests.interactive.test_native_documents_browser import login, native_studio
from tests.prototype.test_v3_workflow import _headers
from tests.ui.test_frontend_browser import chromium_path

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


def test_batch_starts_with_a_range_from_recent_matching_model_history(assistant):
    (web, service, owner, pid, aid, grant, package, section, _), _body, calls = assistant
    selection = current(web, pid, aid, grant)["selection_version"]
    response = web.post(
        root(pid) + "/bindings", json=form(aid, package, section), headers=_headers(grant, selection, "history-binding")
    )
    assert response.status_code == 200
    binding = saved_binding(web, pid, aid, grant)
    started = future(-200)
    ended = (datetime.fromisoformat(started) + timedelta(seconds=100)).isoformat().replace("+00:00", "Z")
    with service.db() as s:
        previous = service._new_job(
            s, owner, pid, KIND, {"analysis_set_id": aid, "_run_started_at": started, "items": {}}
        )
        previous.state, previous.attempt, previous.updated_at = "succeeded", 1, ended
        previous.result_json = {"items": {"previous-item": "saved"}}
        usage = ai_gateway.ModelUsage(
            project_id=pid,
            actor_id=owner.id,
            model_id="synthetic",
            ledger_id=uid(),
            created_at=started,
            state="succeeded",
            reservation_tokens=0,
            reservation_cost_micro=0,
        )
        ledger = ai_gateway.ModelLedger(id=usage.ledger_id, project_id=pid, day=started[:10])
        s.add(ledger)
        s.flush()
        s.add(usage)
    with service.db() as s:
        assert explanation_history(s, pid, aid, owner.id, "different-model", 1) is None
        assert explanation_history(s, pid, aid, owner.id, "synthetic", 3) is None
        assert explanation_history(s, pid, aid, owner.id, "synthetic", 0) is None

    selection = current(web, pid, aid, grant)["selection_version"]
    login_result = web.post("/api/v1/auth/session", json={"bootstrap_token": service.bootstrap_token})
    path = root(pid) + "/explanation-batches"
    response = web.post(
        path,
        json={"analysis_set_id": aid, "binding_ids": [binding["id"]]},
        headers={
            "X-CSRF-Token": login_result.json()["data"]["csrf_token"],
            "If-Match": str(selection),
            "Idempotency-Key": "with-history",
        },
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["processed"] == 0 and data["history"] == {"lower_seconds": 70, "upper_seconds": 150}
    assert web.get(path + "/latest", params={"analysis_set_id": aid}).json()["data"]["history"] == data["history"]
    assert not calls


def test_batch_lecture_persists_reuses_and_exports(assistant, monkeypatch):
    (web, service, owner, pid, aid, grant, package, section, _raw), body, _calls = assistant
    binding_form = form(aid, package, section)
    reply = web.post(
        root(pid) + "/bindings",
        json=binding_form,
        headers=_headers(grant, current(web, pid, aid, grant)["selection_version"], "explain-binding"),
    )
    assert reply.status_code == 200, reply.text
    binding = saved_binding(web, pid, aid, grant)
    lecture = "先打开设置，再核对已选范围。[1]"
    calls = []

    def model(config, payload):
        request = json.loads(payload["input"][1]["content"])
        calls.append(request)
        assert [row["revision_id"] for row in request["chapters"]] == [binding["sections"][0]["revision_id"]]
        output = test_assistant.response(request["chapters"][0]["revision_id"])
        answer = json.loads(output["output"][0]["content"][0]["text"])
        answer["explanation"] = lecture
        output["output"][0]["content"][0]["text"] = json.dumps(answer)
        return output

    monkeypatch.setattr(ai_gateway, "request_model", model)
    path = root(pid) + "/explanation-batches"
    selection_version = current(web, pid, aid, grant)["selection_version"]
    payload = {"analysis_set_id": aid, "binding_ids": [binding["id"], binding["id"]]}

    def start(key):
        response = web.post(path, json=payload, headers=_headers(grant, f'"{selection_version}"', key))
        assert response.status_code == 202, response.text
        return response.json()["data"]

    def latest():
        response = web.get(path + "/latest", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "read"))
        assert response.status_code == 200, response.text
        return response.json()["data"]

    assert latest() is None
    batch = start("lecture")
    assert start("lecture")["job_id"] == batch["job_id"] and batch["total"] == 1
    service.process_job(batch["job_id"])
    status = latest()
    assert status["state"] == "succeeded", service.get_job(owner, batch["job_id"])
    assert (status["processed"], status["saved"], status["reused"], status["failed"]) == (1, 1, 0, 0)
    assert len(calls) == 1 and saved_binding(web, pid, aid, grant) == binding

    # A separate API call can read directly from durable storage.
    response = web.post(root(pid) + "/explain", json=body, headers=_headers(grant, 0, "read-lecture"))
    assert response.status_code == 200, response.text
    answer = response.json()["data"]
    assert answer["explanation"] == lecture and answer["cached"] and not answer["saved"]
    service.process_job(start("second-lecture")["job_id"])
    assert latest()["reused"] == 1 and len(calls) == 1

    manager = MaterialService(service)
    release = manager.checkpoint(owner, pid, aid, "带讲解的成品", uid())
    monkeypatch.setattr(VectorRag, "export_index", lambda *args, **kwargs: {})
    export = service.export(
        owner,
        pid,
        {
            "release_id": release["id"],
            "format": "readonly_html",
            "include_source": True,
        },
        uid(),
    )
    service.process_job(export["id"])
    assert service.get_job(owner, export["id"])["state"] == "succeeded"
    raw, _ = service.download_export(owner, pid, export["id"])
    data = json.loads(
        gzip.decompress(base64.b64decode(BeautifulSoup(raw, "html.parser").select_one("#vks-payload").text))
    )
    found = next(row for row in data["bindings"] if row["target"] == binding["target"])
    assert found["saved_explanation"]["explanation"] == lecture
    assert found["saved_explanation"]["section_ids"] == [binding["sections"][0]["revision_id"]]
    assert found["summary"] == binding["summary"] and len(calls) == 1

    # A narrower export and an export queued before generation cannot inherit it.
    with service.db() as s:
        caps = {"allowed_source": True, "allowed_confidential": True, "allowed_trace": True}
        older, _, _ = projection(service, s, owner, pid, release["id"], caps, explanation_cutoff="2000-01-01T00:00:00Z")
        assert all("saved_explanation" not in row for row in older["bindings"])
        lecture_artifact = s.scalar(
            select(Artifact).where(Artifact.relative_path.startswith("prototype-explanations/"))
        )
        lecture_artifact.metadata_json = {**lecture_artifact.metadata_json, "requires_source_read": True}
        restricted, _, _ = projection(service, s, owner, pid, release["id"], {})
        assert all("saved_explanation" not in row for row in restricted["bindings"])


def test_manager_starts_a_batch_and_recovers_progress_after_reload(assistant, tmp_path, monkeypatch):
    (web, service, _owner, pid, aid, grant, package, section, _), _body, calls = assistant
    binding = web.post(
        root(pid) + "/bindings",
        json=form(aid, package, section),
        headers=_headers(grant, current(web, pid, aid, grant)["selection_version"], "browser-lecture"),
    )
    assert binding.status_code == 200, binding.text
    with native_studio(web.app, tmp_path, monkeypatch) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium_path(playwright), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script("localStorage.setItem('vks.studio.auto-associate','off')")
            login(page, url, pid, aid)
            page.get_by_role("button", name="关联管理", exact=True).click()
            start = page.get_by_role("button", name="批量 AI 讲解", exact=True)
            expect(start).to_be_disabled()
            page.get_by_role("checkbox", name="选择关联：Add rule", exact=True).check()
            start.click()
            panel = page.get_by_role("dialog", name="批量 AI 讲解", exact=True)
            expect(panel).to_contain_text("已选 1 项有效关联")
            with page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/explanation-batches")
            ) as queued:
                panel.get_by_role("button", name="补齐所选讲解", exact=True).click()
            assert queued.value.status == 202
            service.process_job(queued.value.json()["data"]["job_id"])
            expect(panel).to_contain_text("新生成 1 · 已复用 0 · 失败 0")
            panel.get_by_role("button", name="关闭", exact=True).click()
            page.reload()
            page.get_by_role("button", name="讲解进度", exact=True).click()
            expect(panel).to_contain_text("批量讲解已完成")
            assert len(calls) == 1
        finally:
            browser.close()
