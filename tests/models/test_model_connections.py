"""Provider setup through real HTTP stubs; no developer credentials are used."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from vks_api.model_credentials import credential_path, key_for_ref

from tests.models import test_model_credentials

site = test_model_credentials.site


@pytest.fixture
def upstream(monkeypatch):
    requests = []
    state = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization"), None))
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "data": [
                            {"id": "text-model-a", "name": "Text Model A"},
                            {"id": "text-model-b"},
                            {"id": "embedding-example"},
                        ]
                    }
                ).encode()
            )

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = (
                {
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                }
                if self.path.endswith("/responses")
                else {
                    "choices": [{"message": {"content": "OK"}}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 1},
                }
            )
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("VKS_MODEL_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("VKS_MODEL_ALLOWED_ORIGINS", "")
    previous = {k: v for k, v in os.environ.items() if k.startswith("VKS_MODEL_CONNECTION_KEY_")}
    yield f"http://127.0.0.1:{server.server_port}/v1", requests, state
    for key in list(os.environ):
        if key.startswith("VKS_MODEL_CONNECTION_KEY_") and key not in previous:
            os.environ.pop(key, None)
    server.shutdown()
    server.server_close()
    thread.join()


def form(endpoint, **patch):
    return {
        "provider": "custom",
        "name": "Custom test",
        "endpoint": endpoint,
        "protocol": "responses",
        "model_id": "text-model-a",
        "api_key": "synthetic-provider-one",
        **patch,
    }


def test_discovery_and_each_protocol_test_use_the_selected_key_without_saving(site, upstream):
    web, service, _, pid, headers = site
    endpoint, requests, _ = upstream
    base = f"/api/v1/projects/{pid}/model-connections"
    r = web.post(base + "/models", json=form(endpoint), headers=headers)
    assert r.status_code == 200, r.text
    assert [m["id"] for m in r.json()["data"]["models"]] == ["embedding-example", "text-model-a", "text-model-b"]
    assert requests[-1][:2] == ("/v1/models", "Bearer synthetic-provider-one")
    assert not credential_path(service).exists()
    for protocol, path in [("responses", "/v1/responses"), ("chat_completions", "/v1/chat/completions")]:
        r = web.post(base + "/test", json=form(endpoint, protocol=protocol), headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["data"]["ok"]
        assert requests[-1][0] == path
        assert requests[-1][2].get("input", requests[-1][2].get("messages")) == [
            {"role": "user", "content": "Reply with OK."}
        ]
        assert "synthetic-provider-one" not in r.text
    assert not credential_path(service).exists()
    usage = web.get(f"/api/v1/projects/{pid}/model-usage").json()["data"]
    assert len(usage) == 2 and all(r["state"] == "succeeded" for r in usage)


def test_multiple_connections_preserve_keys_active_choice_and_scope(site, upstream):
    web, service, _, pid, headers = site
    endpoint, requests, _ = upstream
    base = f"/api/v1/projects/{pid}/model-connections"
    saved = web.post(base, json=form(endpoint), headers={**headers, "If-Match": "1"})
    assert saved.status_code == 200, saved.text
    one = saved.json()["data"]
    c1 = one["connections"][0]
    assert c1["active"] and c1["configured"]
    assert "synthetic-provider-one" not in saved.text
    assert key_for_ref(one["policy"]["secret_ref"], endpoint) == "synthetic-provider-one"
    assert key_for_ref(one["policy"]["secret_ref"], endpoint + "/other") == ""
    saved = web.post(
        base,
        json=form(
            endpoint + "/other", api_key="synthetic-provider-two", name="Second connection", model_id="text-model-b"
        ),
        headers={**headers, "If-Match": str(one["policy"]["row_version"])},
    )
    assert saved.status_code == 200, saved.text
    two = saved.json()["data"]
    assert len(two["connections"]) == 2
    assert sum(c["active"] for c in two["connections"]) == 1
    body = form(endpoint, connection_id=c1["id"])
    body.pop("api_key")
    response = web.post(base + "/models", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert requests[-1][1] == "Bearer synthetic-provider-one"
    body["endpoint"] = endpoint + "/moved"
    assert web.post(base + "/models", json=body, headers=headers).status_code == 422
    body["endpoint"] = endpoint
    activated = web.post(base, json=body, headers={**headers, "If-Match": str(two["policy"]["row_version"])})
    assert activated.status_code == 200, activated.text
    assert next(c["id"] for c in activated.json()["data"]["connections"] if c["active"]) == c1["id"]
    assert credential_path(service).stat().st_mode & 0o777 == 0o600
    assert "synthetic-provider-one" not in web.get(base).text
    assert "synthetic-provider-two" not in web.get(f"/api/v1/projects/{pid}/audit-events").text


def test_legacy_key_is_retained_and_not_sent_to_another_provider(site, upstream, monkeypatch):
    web, _service, _, pid, headers = site
    endpoint, requests, _ = upstream
    monkeypatch.setenv("VKS_MODEL_API_KEY", "synthetic-old-openai-key")
    base = f"/api/v1/projects/{pid}/model-connections"
    catalog = web.get(base).json()["data"]
    assert catalog["legacy_configured"]
    body = form(endpoint, provider="deepseek")
    body.pop("api_key")
    response = web.post(base + "/models", json=body, headers=headers)
    assert response.status_code == 422 and not requests
    body = form("https://api.openai.com/v1", provider="openai")
    body.pop("api_key")
    saved = web.post(base, json=body, headers={**headers, "If-Match": "1"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["connections"][0]["configured"]
    assert os.environ["VKS_MODEL_API_KEY"] == "synthetic-old-openai-key"


def test_failures_are_clear_versions_are_checked_and_foreign_profiles_hidden(site, upstream):
    web, service, owner, pid, headers = site
    endpoint, _requests, state = upstream
    base = f"/api/v1/projects/{pid}/model-connections"
    state["status"] = 401
    denied = web.post(base + "/models", json=form(endpoint), headers=headers)
    assert denied.status_code == 502 and "API Key" in denied.json()["error"]["message"]
    assert "synthetic-provider-one" not in denied.text
    state["status"] = 200
    stale = web.post(base, json=form(endpoint), headers={**headers, "If-Match": "99"})
    assert stale.status_code == 412 and not credential_path(service).exists()
    saved = web.post(base, json=form(endpoint), headers={**headers, "If-Match": "1"}).json()["data"]
    other = service.create_project(owner, {"key": "other", "name": "Other"}, "other")["id"]
    body = form(endpoint, connection_id=saved["connections"][0]["id"])
    assert web.post(f"/api/v1/projects/{other}/model-connections/models", json=body, headers=headers).status_code == 404
    bad = web.post(base + "/models", json=form("https://secret:password@example.invalid/v1"), headers=headers)
    assert bad.status_code == 422
