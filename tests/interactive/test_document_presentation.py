"""Reader choices follow immutable versions and share project authorization."""

import json

import pytest
from sqlalchemy import select
from vks_api import ai_gateway
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.document_presentation import PresentationInput, PresentationService
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.materials import RestoreMaterialVersion
from vks_api.models import Artifact, Revision, uid
from vks_api.service import DomainError

from tests.interactive import test_assistant, test_materials

materials = test_materials.materials


def test_presentation_is_versioned_without_rewriting_document_or_source(materials):
    _, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Rules\n\nExact original text.")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    links = InteractiveKnowledgeService(service)
    before = links.catalog(owner, pid, aid)
    settings = PresentationService(service)
    original = settings.get(owner, pid, aid)
    assert original["settings"]["mode"] == "auto"
    changed = settings.update(
        owner, pid, PresentationInput(analysis_set_id=aid, mode="handbook"), str(original["selection_version"]), uid()
    )
    frozen = manager.checkpoint(owner, pid, aid, "Handbook version", uid())
    assert frozen["manifest"]["document_presentation"]["mode"] == "handbook"
    assert links.catalog(owner, pid, aid)["documents"] == before["documents"]
    assert links.catalog(owner, pid, aid)["packages"] == before["packages"]
    settings.update(
        owner, pid, PresentationInput(analysis_set_id=aid, mode="standard"), str(changed["selection_version"]), uid()
    )
    assert settings.get(owner, pid, release_id=frozen["id"])["settings"]["mode"] == "handbook"
    restored = manager.restore(
        owner,
        pid,
        RestoreMaterialVersion(analysis_set_id=aid, release_id=frozen["id"]),
        str(settings.get(owner, pid, aid)["selection_version"]),
        uid(),
    )
    assert settings.get(owner, pid, restored["analysis_set_id"])["settings"]["mode"] == "handbook"
    with pytest.raises(DomainError):
        settings.update(
            owner,
            pid,
            PresentationInput(analysis_set_id=frozen["manifest"]["analysis_set_id"], mode="standard"),
            "0",
            uid(),
        )


def test_presentation_access_idempotency_conflict_and_http_schema(materials):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    other, other_aid = projects[1]
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", "# Reader\n\nVisible content.")
    test_materials.activate(service, owner, manager, pid, aid, [package])
    shared = PresentationService(service)
    initial = shared.get(owner, pid, aid)
    form = PresentationInput(analysis_set_id=aid, mode="handbook")
    key = uid()
    first = shared.update(owner, pid, form, str(initial["selection_version"]), key)
    assert shared.update(owner, pid, form, str(initial["selection_version"]), key) == first
    with pytest.raises(DomainError, match="变化"):
        shared.update(
            owner,
            pid,
            PresentationInput(analysis_set_id=aid, mode="standard"),
            str(initial["selection_version"]),
            uid(),
        )
    with pytest.raises(DomainError):
        shared.update(owner, pid, PresentationInput(analysis_set_id=other_aid, mode="handbook"), "1", uid())
    assert shared.get(owner, other, other_aid)["settings"]["mode"] == "auto"
    credential = service.create_token(owner, pid, {"name": "Reader", "scopes": ["knowledge:read"]})
    limited = service.authenticate(credential["token"], "token")
    with pytest.raises(DomainError):
        shared.get(limited, pid, aid)
    frozen = test_materials.MaterialService(service).checkpoint(owner, pid, aid, "Reader version", uid())
    assert shared.get(limited, pid, release_id=frozen["id"])["settings"]["mode"] == "handbook"
    with pytest.raises(DomainError):
        shared.update(limited, pid, form, str(first["selection_version"]), uid())
    assert web.post("/api/v1/auth/login", json={"username": "owner", "password": "owner"}).status_code == 200
    assert (
        web.get(f"/api/v3/projects/{pid}/document-presentation?analysis_set_id={aid}").json()["data"]["settings"][
            "mode"
        ]
        == "handbook"
    )


def test_template_search_uses_exact_visible_chapters_and_rechecks_restrictions(materials):
    _, service, owner, manager, projects = materials
    from vks_api.document_presentation import PresentationSearchInput

    pid, aid = projects[0]
    p = test_materials.stage(
        service,
        owner,
        manager,
        pid,
        aid,
        "documents",
        "# Policy\n\nPublic intro.\n\n## Invoice\n\nneedle-rule invoice fact.",
    )
    test_materials.activate(service, owner, manager, pid, aid, [p])
    shared = PresentationService(service)
    credential = service.create_token(owner, pid, {"name": "Reader", "scopes": ["knowledge:read"]})
    limited = service.authenticate(credential["token"], "token")
    frozen = manager.checkpoint(owner, pid, aid, "Search version", uid())
    result = shared.search(limited, pid, PresentationSearchInput(release_id=frozen["id"], query="needle-rule"))
    assert result and any(item["section"] and "needle-rule" in item["snippet"] for item in result)
    with service.db() as s:
        ids = [item["document"] for item in result]
        for revision in s.scalars(select(Revision).where(Revision.id.in_(ids))):
            s.get(Artifact, revision.artifact_id).sensitivity = "confidential"
    assert shared.search(limited, pid, PresentationSearchInput(release_id=frozen["id"], query="needle-rule")) == []


def test_document_questions_work_without_prototype_and_do_not_create_bindings(materials, monkeypatch):
    web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    package = test_materials.stage(
        service, owner, manager, pid, aid, "documents", "# 发票规则\n\n开票前核对账单金额与抬头。"
    )
    test_materials.activate(service, owner, manager, pid, aid, [package])
    links = InteractiveKnowledgeService(service)
    doc = links.catalog(owner, pid, aid)["documents"][0]
    with service.db() as s:
        s.add(
            ModelPolicy(
                id=uid(),
                project_id=pid,
                settings=PolicyInput(enabled=True, allow_external=True, model_id="synthetic").model_dump(),
            )
        )
    calls = []

    def model(_config, body):
        payload = json.loads(body["input"][1]["content"])
        calls.append(payload)
        return test_assistant.response(payload["chapters"][0]["revision_id"])

    monkeypatch.setattr(ai_gateway, "request_model", model)
    auth = web.post("/api/v1/auth/login", json={"username": "owner", "password": "owner"}).json()["data"]
    body = {
        "analysis_set_id": aid,
        "package_id": None,
        "document_revision_id": doc["revision_id"],
        "target": {
            "route": "/documents",
            "frame_path": [],
            "selector": "body",
            "label": "发票规则",
            "state": "",
            "open_steps": [],
        },
        "context": {"scope": "module", "page_title": "发票规则"},
        "question": "开票前核对什么？",
        "history": [],
    }
    response = web.post(
        f"/api/v3/projects/{pid}/interactive/questions", json=body, headers={"X-CSRF-Token": auth["csrf_token"]}
    )
    assert response.status_code == 200, response.text
    answer = response.json()["data"]
    assert answer["sections"] and not answer["saved"] and answer["binding_id"] is None
    assert len(calls) == 1 and links.bindings(owner, pid, aid) == []
    body["document_revision_id"] = uid()
    rejected = web.post(
        f"/api/v3/projects/{pid}/interactive/questions", json=body, headers={"X-CSRF-Token": auth["csrf_token"]}
    )
    assert rejected.status_code == 404 and len(calls) == 1
    body["document_revision_id"] = doc["revision_id"]
    body["package_id"] = uid()
    ambiguous = web.post(
        f"/api/v3/projects/{pid}/interactive/questions",
        json=body,
        headers={"X-CSRF-Token": auth["csrf_token"]},
    )
    assert ambiguous.status_code == 422 and len(calls) == 1
