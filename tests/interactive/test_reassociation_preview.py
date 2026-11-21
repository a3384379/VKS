"""Explicit preview/confirmation stores precisely one verified generation."""

from copy import deepcopy

import pytest
from sqlalchemy import select
from vks_api.interactive_ai import InteractiveAssistant, ReassociationInput
from vks_api.models import Analysis, Artifact, Entity, Revision
from vks_api.service import DomainError

from tests.interactive import test_assistant, test_knowledge
from tests.prototype.test_v3_workflow import _headers

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant
root = test_knowledge.root


def bindings(web, pid, aid, grant):
    return web.get(root(pid) + "/bindings", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "read")).json()[
        "data"
    ]


def points(service):
    with service.db() as s:
        return [
            (r.id, r.content, r.content_sha256)
            for r in s.scalars(
                select(Revision)
                .join(Entity, Revision.entity_id == Entity.id)
                .where(Entity.type == "business_point")
                .order_by(Revision.id)
            )
        ]


def preview(web, pid, grant, body):
    response = web.post(root(pid) + "/reassociate", json=body, headers=_headers(grant, 0, "preview"))
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["saved"] is False and result["can_confirm"] and result["preview_id"]
    return result


def test_edited_preview_saves_manual_text_with_same_evidence_and_replays(assistant):
    (web, service, _owner, pid, aid, grant, _package, _section, _), body, calls = assistant
    result = preview(web, pid, grant, body)
    endpoint = root(pid) + "/reassociate/confirm"
    before = points(service)
    request = {"preview_id": result["preview_id"], "edit": {"title": "手工核对", "summary": "业务口径。[99]"}}
    invalid = web.post(endpoint, json=request, headers=_headers(grant, 0, "edit-test"))
    assert invalid.status_code == 422 and points(service) == before
    request["edit"]["summary"] = "核对后的计算说明。[1]"
    valid = web.post(endpoint, json=request, headers=_headers(grant, 0, "edit-test"))
    assert valid.status_code == 200, valid.text
    current = bindings(web, pid, aid, grant)[0]
    assert (current["origin"], current["title"], current["summary"]) == (
        "manual", request["edit"]["title"], request["edit"]["summary"],
    )
    assert current["sections"] == result["sections"]
    repeated = web.post(endpoint, json=request, headers=_headers(grant, 0, "edit-test"))
    assert repeated.status_code == 200 and repeated.json()["data"] == valid.json()["data"]
    assert len(calls) == 1


def test_preview_cancel_and_confirm_same_generation_once(assistant):
    (web, service, _owner, pid, aid, grant, package, section, _), body, calls = assistant
    saved = web.post(
        root(pid) + "/bindings",
        json=test_knowledge.form(aid, package, section),
        headers=_headers(grant, test_knowledge.current(web, pid, aid, grant)["selection_version"], "initial"),
    )
    assert saved.status_code == 200, saved.text
    old = deepcopy(bindings(web, pid, aid, grant)[0])
    before = points(service)
    form = {
        **body,
        "instruction": "按当前业务前提关联",
        **{k: old[k] for k in ["binding_version", "point_revision_id"]},
        "binding_id": old["id"],
    }
    result = preview(web, pid, grant, form)
    assert bindings(web, pid, aid, grant)[0] == old and points(service) == before and len(calls) == 1
    read = web.get(
        root(pid) + "/reassociate/previews/" + result["preview_id"], headers=_headers(grant, 0, "read-preview")
    )
    assert read.status_code == 200 and read.json()["data"] == result
    request = {"preview_id": result["preview_id"]}
    confirm = web.post(root(pid) + "/reassociate/confirm", json=request, headers=_headers(grant, 0, "confirm"))
    assert confirm.status_code == 200, confirm.text
    output = confirm.json()["data"]
    current = bindings(web, pid, aid, grant)[0]
    assert output["saved"] and current["origin"] == "model"
    assert current["title"] == result["title"] and current["summary"] == result["explanation"]
    assert [r["revision_id"] for r in current["sections"]] == [r["revision_id"] for r in result["sections"]]
    after = points(service)
    repeat = web.post(root(pid) + "/reassociate/confirm", json=request, headers=_headers(grant, 0, "repeat"))
    assert repeat.status_code == 200 and repeat.json()["data"] == confirm.json()["data"]
    assert points(service) == after and len(calls) == 1
    with service.db() as s:
        assert s.get(Revision, old["point_revision_id"]).content == old["summary"]
        art = s.get(Artifact, s.get(Revision, current["point_revision_id"]).artifact_id)
        assert art.metadata_json["interactive_generation"]["preview_id"] == result["preview_id"]


@pytest.mark.parametrize("change", ["binding", "excluded", "corpus", "frozen"])
def test_confirm_rejects_stale_state_without_a_second_model_call(assistant, change):
    (web, service, _owner, pid, aid, grant, package, section, _), body, calls = assistant
    if change in ("binding", "excluded"):
        web.post(
            root(pid) + "/bindings",
            json=test_knowledge.form(aid, package, section),
            headers=_headers(grant, test_knowledge.current(web, pid, aid, grant)["selection_version"], "initial"),
        )
        old = bindings(web, pid, aid, grant)[0]
        body = {**body, "binding_id": old["id"], **{k: old[k] for k in ["binding_version", "point_revision_id"]}}
    result = preview(web, pid, grant, body)
    if change == "binding":
        payload = {
            **test_knowledge.form(aid, package, section),
            "binding_id": old["id"],
            **{k: old[k] for k in ["binding_version", "point_revision_id"]},
        }
        payload["point"]["summary"] = "人工随后修改，必须保留"
        version = web.get(
            root(pid) + "/catalog", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "version")
        ).json()["data"]["selection_version"]
        update = web.post(root(pid) + "/bindings", json=payload, headers=_headers(grant, version, "later-manual"))
        assert update.status_code == 200, update.text
    elif change == "excluded":
        version = web.get(
            root(pid) + "/catalog", params={"analysis_set_id": aid}, headers=_headers(grant, 0, "version")
        ).json()["data"]["selection_version"]
        excluded = web.post(
            root(pid) + "/bindings/" + old["id"] + "/exclude",
            json={"analysis_set_id": aid, "binding_version": old["binding_version"], "excluded": True},
            headers=_headers(grant, version, "exclude"),
        )
        assert excluded.status_code == 200, excluded.text
    elif change == "corpus":
        with service.db() as s:
            from vks_api.ai_gateway import ModelPolicy

            policy = s.scalar(select(ModelPolicy).where(ModelPolicy.project_id == pid))
            policy.settings = {**policy.settings, "max_output_tokens": 8000}
    else:
        with service.db() as s:
            s.get(Analysis, aid).state = "frozen"
    before = bindings(web, pid, aid, grant)
    facts = points(service)
    confirm = web.post(
        root(pid) + "/reassociate/confirm",
        json={"preview_id": result["preview_id"]},
        headers=_headers(grant, 0, "confirm-stale"),
    )
    assert confirm.status_code in (409, 412), confirm.text
    assert bindings(web, pid, aid, grant) == before and points(service) == facts and len(calls) == 1


def test_atomic_multiple_confirm_rolls_back_and_can_create_unlinked_targets(assistant, monkeypatch):
    (web, service, owner, pid, aid, grant, _package, _section, _), body, calls = assistant
    engine = InteractiveAssistant(service)
    first = engine.reassociate(owner, pid, ReassociationInput.model_validate(body), preview=True)
    other = {**body, "target": {**body["target"], "selector": "#other"}}
    second = engine.reassociate(owner, pid, ReassociationInput.model_validate(other), preview=True)
    assert len(calls) == 2 and not bindings(web, pid, aid, grant)
    before = points(service)
    save = engine.links.save
    count = 0

    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise DomainError("VKS_STATE_CONFLICT", "Synthetic second write conflict", 409)
        return save(*args, **kwargs)

    monkeypatch.setattr(engine.links, "save", fail_second)
    with pytest.raises(DomainError):
        engine.confirm_reassociations(owner, pid, [first["preview_id"], second["preview_id"]])
    assert not bindings(web, pid, aid, grant) and points(service) == before and len(calls) == 2
    monkeypatch.setattr(engine.links, "save", save)
    result = engine.confirm_reassociations(owner, pid, [first["preview_id"], second["preview_id"]])
    assert result["saved"] and result["count"] == 2 and len(bindings(web, pid, aid, grant)) == 2
    assert engine.confirm_reassociations(owner, pid, [first["preview_id"], second["preview_id"]]) == result
    assert len(calls) == 2


def test_confirm_replay_never_replaces_a_later_manual_edit(assistant):
    (web, service, _owner, pid, aid, grant, package, section, _), body, calls = assistant
    candidate = preview(web, pid, grant, body)
    request = {"preview_id": candidate["preview_id"]}
    confirmed = web.post(root(pid) + "/reassociate/confirm", json=request, headers=_headers(grant, 0, "first"))
    assert confirmed.status_code == 200, confirmed.text
    current = bindings(web, pid, aid, grant)[0]
    payload = {
        **test_knowledge.form(aid, package, section),
        "binding_id": current["id"],
        **{k: current[k] for k in ("binding_version", "point_revision_id")},
    }
    payload["point"]["summary"] = "确认之后的人工修订"
    update = web.post(
        root(pid) + "/bindings",
        json=payload,
        headers=_headers(grant, test_knowledge.current(web, pid, aid, grant)["selection_version"], "manual-after"),
    )
    assert update.status_code == 200, update.text
    before = deepcopy(bindings(web, pid, aid, grant))
    facts = points(service)
    repeated = web.post(
        root(pid) + "/reassociate/confirm", json=request, headers=_headers(grant, 0, "replay-after-edit")
    )
    assert repeated.status_code == 412, repeated.text
    assert bindings(web, pid, aid, grant) == before and points(service) == facts and len(calls) == 1


def test_preview_and_confirm_recheck_all_input_permissions_and_project_identity(assistant):
    (web, service, owner, pid, aid, grant, _package, _section, _), body, calls = assistant
    candidate = preview(web, pid, grant, body)
    with service.db() as s:
        art = s.get(Artifact, candidate["preview_id"])
        # Provenance includes all candidates sent, not only selected citations.
        sent = {c["revision_id"] for c in calls[0]["chapters"]}
        assert {r["revision_id"] for r in art.metadata_json["input_revision_provenance"]} == sent
        ref = art.metadata_json["input_revision_provenance"][-1]
        s.get(Artifact, ref["artifact_id"]).sensitivity = "confidential"
    token = service.create_token(
        owner, pid, {"name": "Limited confirmer", "scopes": ["knowledge:read", "knowledge:edit"]}
    )
    headers = _headers(token, 0, "denied")
    read = web.get(root(pid) + "/reassociate/previews/" + candidate["preview_id"], headers=headers)
    assert read.status_code in (403, 404), read.text
    confirm = web.post(
        root(pid) + "/reassociate/confirm", json={"preview_id": candidate["preview_id"]}, headers=headers
    )
    assert confirm.status_code in (403, 404), confirm.text
    assert not bindings(web, pid, aid, grant) and len(calls) == 1


def test_insufficient_preview_and_cancel_fence_never_create_binding_or_candidate_fact(assistant, monkeypatch):
    import json

    from vks_api import ai_gateway

    (web, service, owner, pid, aid, grant, _package, _section, _), body, _calls = assistant
    cancelled = False
    calls = []

    def model(_config, payload):
        nonlocal cancelled
        data = json.loads(payload["input"][1]["content"])
        calls.append(data)
        answer = test_assistant.response(data["chapters"][0]["revision_id"])
        if len(calls) == 1:
            result = json.loads(answer["output"][0]["content"][0]["text"])
            result["confidence"] = 0.3
            answer["output"][0]["content"][0]["text"] = json.dumps(result)
        else:
            cancelled = True
        return answer

    monkeypatch.setattr(ai_gateway, "request_model", model)
    result = web.post(root(pid) + "/reassociate", json=body, headers=_headers(grant, 0, "insufficient"))
    assert result.status_code == 200 and not result.json()["data"]["can_confirm"]
    assert result.json()["data"]["preview_id"] is None and not bindings(web, pid, aid, grant)

    def fence(_s):
        if cancelled:
            raise DomainError("VKS_STATE_CONFLICT", "Preview job was cancelled", 409)

    engine = InteractiveAssistant(service)
    with pytest.raises(DomainError, match="cancelled"):
        engine.reassociate(owner, pid, ReassociationInput.model_validate(body), preview=True, validate_scope=fence)
    with service.db() as s:
        assert not [a for a in s.scalars(select(Artifact)) if a.metadata_json.get("reassociation_preview")]
    assert not bindings(web, pid, aid, grant) and len(calls) == 2


def test_a_b_a_regeneration_preserves_latest_immutable_candidate_identity(assistant):
    (web, service, owner, pid, aid, grant, _package, _section, _), body, calls = assistant
    engine = InteractiveAssistant(service)
    first = engine.reassociate(
        owner, pid, ReassociationInput.model_validate({**body, "instruction": "A"}), preview=True
    )
    middle = engine.reassociate(
        owner, pid, ReassociationInput.model_validate({**body, "instruction": "B"}), preview=True
    )
    latest = engine.reassociate(
        owner, pid, ReassociationInput.model_validate({**body, "instruction": "A"}), preview=True
    )
    assert first["explanation"] == latest["explanation"]
    assert len({first["preview_id"], middle["preview_id"], latest["preview_id"]}) == 3
    with service.db() as s:
        from sqlalchemy import desc

        artifacts = list(
            s.scalars(
                select(Artifact)
                .where(Artifact.id.in_([first["preview_id"], middle["preview_id"], latest["preview_id"]]))
                .order_by(desc(Artifact.created_at), desc(Artifact.id))
            )
        )
        assert artifacts[0].id == latest["preview_id"]
        assert len({a.metadata_json["model_usage_id"] for a in artifacts}) == 3
    assert len(calls) == 3 and not bindings(web, pid, aid, grant)
