"""Local setup uses synthetic secrets; tests never access developer credentials."""

import json
import os

import pytest
from fastapi.testclient import TestClient
from vks_api.app import create_app
from vks_api.model_credentials import credential_path, load_credential


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.delenv("VKS_MODEL_API_KEY", raising=False)
    app = create_app(tmp_path, enable_worker=False)
    service = app.state.service
    with TestClient(app) as web:
        _, owner = service.login(service.bootstrap_token)
        pid = service.create_project(owner, {"key": "model-setup", "name": "Model setup"}, "project")["id"]
        login = web.post("/api/v1/auth/login", json={"username": "owner", "password": "owner"})
        headers = {"X-CSRF-Token": login.json()["data"]["csrf_token"]}
        yield web, service, owner, pid, headers
    service.engine.dispose()


def test_store_never_returns_key_and_reloads_after_restart(site, monkeypatch):
    web, service, _, pid, headers = site
    url = f"/api/v1/projects/{pid}/model-credential"
    assert not web.get(url).json()["data"]["configured"]
    fake = "synthetic-test-key-not-a-provider-credential"
    denied = web.post(url, json={"api_key": fake}, headers=headers)
    assert denied.status_code == 422
    assert not credential_path(service).exists()
    saved = web.post(url, json={"api_key": fake, "confirm_local_storage": True}, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["configured"]
    assert fake not in saved.text
    assert fake not in web.get(url).text
    assert fake not in web.get(f"/api/v1/projects/{pid}/model-policy").text
    assert fake not in web.get(f"/api/v1/projects/{pid}/audit-events").text
    path = credential_path(service)
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["VKS_MODEL_API_KEY"] == fake
    monkeypatch.delenv("VKS_MODEL_API_KEY")
    load_credential(service)
    assert os.environ.get("VKS_MODEL_API_KEY") == fake


def test_rejects_machine_credentials_and_symlinks(site, tmp_path):
    web, service, owner, pid, headers = site
    url = f"/api/v1/projects/{pid}/model-credential"
    token = service.create_token(owner, pid, {"name": "editor", "scopes": ["knowledge:read", "knowledge:edit"]})
    response = web.post(
        url,
        json={"api_key": "synthetic-key", "confirm_local_storage": True},
        headers={"Authorization": "Bearer " + token["token"]},
    )
    assert response.status_code == 403
    other = tmp_path / "outside"
    other.write_text("preserve")
    credential_path(service).symlink_to(other)
    response = web.post(url, json={"api_key": "synthetic-key", "confirm_local_storage": True}, headers=headers)
    assert response.status_code == 422
    assert other.read_text() == "preserve"


def test_bad_key_has_no_echo_and_corrupt_optional_file_does_not_block_app(site):
    web, service, _, pid, headers = site
    url = f"/api/v1/projects/{pid}/model-credential"
    response = web.post(
        url, json={"api_key": "synthetic key with spaces", "confirm_local_storage": True}, headers=headers
    )
    assert response.status_code == 422
    assert "synthetic key" not in response.text
    response = web.post(url, json={"api_key": "synthetic\u0000key", "confirm_local_storage": True}, headers=headers)
    assert response.status_code == 422
    assert not credential_path(service).exists()
    credential_path(service).write_text("{bad-json")
    reopened = create_app(service.data_dir, enable_worker=False)
    with TestClient(reopened) as client:
        assert client.get("/ready").status_code == 200
    reopened.state.service.engine.dispose()
