from datetime import datetime, timezone, timedelta
import json
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import pytest
from vks_api.app import create_app


@pytest.fixture
def system(tmp_path):
    app = create_app(tmp_path, enable_worker=False)
    s = app.state.service
    client = TestClient(app)
    login = client.post("/api/v1/auth/session", json={"bootstrap_token": s.bootstrap_token}).json()["data"]
    headers = {"X-CSRF-Token": login["csrf_token"], "Idempotency-Key": "project"}
    p = client.post("/api/v1/projects", headers=headers, json={"key": "oauth-demo", "name": "OAuth Demo"}).json()[
        "data"
    ]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="fixture-key", alg="RS256", use="sig")
    issuer = client.post(
        "/api/v1/oauth/issuers",
        headers=headers,
        json={
            "issuer": "https://idp.example.invalid",
            "audience": "http://127.0.0.1:8788/mcp",
            "jwks": {"keys": [public]},
        },
    )
    assert issuer.status_code == 201, issuer.text
    iid = issuer.json()["data"]["id"]
    grant = client.post(
        f"/api/v1/projects/{p['id']}/oauth-grants",
        headers=headers,
        json={"issuer_id": iid, "subject": "engineer-1", "client_id": "mcp-client", "scopes": ["knowledge:read"]},
    )
    assert grant.status_code == 201, grant.text
    return app, s, client, headers, p, key, public, iid, grant.json()["data"]


def token(key, **changes):
    now = int(datetime.now(timezone.utc).timestamp())
    claims = {
        "iss": "https://idp.example.invalid",
        "aud": "http://127.0.0.1:8788/mcp",
        "sub": "engineer-1",
        "client_id": "mcp-client",
        "iat": now,
        "exp": now + 600,
        "scope": "knowledge:read links:review release:publish",
    }
    claims.update(changes)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture-key"})


def test_signed_token_is_project_grant_scoped_without_human_powers(system):
    app, s, client, h, p, key, public, iid, grant = system
    bearer = {"Authorization": "Bearer " + token(key)}
    session = client.get("/api/v1/auth/session", headers=bearer).json()["data"]
    assert session["authenticated"] and not session["human"]
    assert session["scopes"] == ["knowledge:read"]
    projects = client.get("/api/v1/projects", headers=bearer).json()["data"]
    assert [x["id"] for x in projects] == [p["id"]]
    other = client.post(
        "/api/v1/projects", headers={**h, "Idempotency-Key": "other"}, json={"key": "hidden", "name": "HIDDEN"}
    ).json()["data"]
    denied = client.get(f"/api/v1/projects/{other['id']}/workspace", headers=bearer)
    assert denied.status_code == 404 and "HIDDEN" not in denied.text
    assert (
        client.post(
            f"/api/v1/projects/{p['id']}/tokens",
            headers=bearer,
            json={"name": "escalate", "scopes": ["knowledge:read"]},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "https://wrong.invalid"},
        {"iss": "https://unregistered.invalid"},
        {"sub": "unknown"},
        {"client_id": "wrong"},
        {"azp": "different-client"},
        {"exp": 1},
        {"nbf": 4102444800},
        {"iat": 4102444800, "exp": 4102444900},
        {"exp": 4102444800},
    ],
)
def test_invalid_claims_fail_closed_without_data(system, claims):
    _, _, client, _, p, key, *_ = system
    response = client.get("/api/v1/projects", headers={"Authorization": "Bearer " + token(key, **claims)})
    assert response.status_code == 401 and p["name"] not in response.text


def test_algorithm_or_unregistered_key_cannot_choose_trust(system):
    _, _, client, _, p, key, *_ = system
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for signed in (
        token(wrong),
        jwt.encode({"sub": "engineer-1"}, "attacker-shared-secret-32-characters-minimum", algorithm="HS256"),
    ):
        assert client.get("/api/v1/projects", headers={"Authorization": "Bearer " + signed}).status_code == 401
    malicious = jwt.encode(
        {"iss": "https://idp.example.invalid"},
        key,
        algorithm="RS256",
        headers={"kid": "fixture-key", "jku": "http://169.254.169.254/keys"},
    )
    assert client.get("/api/v1/projects", headers={"Authorization": "Bearer " + malicious}).status_code == 401


def test_grant_and_issuer_revocation_apply_next_request(system):
    _, _, client, h, p, key, _, iid, grant = system
    bearer = {"Authorization": "Bearer " + token(key)}
    assert client.get("/api/v1/projects", headers=bearer).status_code == 200
    r = client.patch(
        f"/api/v1/projects/{p['id']}/oauth-grants/{grant['id']}", headers={**h, "If-Match": "1"}, json={"active": False}
    )
    assert r.status_code == 200
    assert client.get("/api/v1/projects", headers=bearer).status_code == 401
    client.patch(
        f"/api/v1/projects/{p['id']}/oauth-grants/{grant['id']}", headers={**h, "If-Match": "2"}, json={"active": True}
    )
    r = client.patch(f"/api/v1/oauth/issuers/{iid}", headers={**h, "If-Match": "1"}, json={"active": False})
    assert r.status_code == 200
    assert client.get("/api/v1/projects", headers=bearer).status_code == 401


def test_metadata_is_resource_discovery_and_private_jwks_are_rejected(system):
    _, _, client, h, p, key, public, _, _ = system
    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"] == "http://127.0.0.1:8788/mcp"
    assert metadata["authorization_servers"] == ["https://idp.example.invalid"]
    private = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key))
    private["kid"] = "private-key"
    response = client.post(
        "/api/v1/oauth/issuers",
        headers=h,
        json={"issuer": "https://other.invalid", "audience": metadata["resource"], "jwks": {"keys": [private]}},
    )
    assert response.status_code == 422 and "PRIVATE KEY" not in response.text
    assert (
        client.post(
            f"/api/v1/projects/{p['id']}/oauth-grants",
            headers=h,
            json={"issuer_id": "invalid", "subject": "a", "client_id": "b", "scopes": ["release:publish"]},
        ).status_code
        == 422
    )


@pytest.mark.asyncio
async def test_actual_streamable_sdk_jwt_resource_metadata_and_live_revocation(system):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from vks_api.mcp_gateway import APIBridge, create_http_app

    app, s, web, h, p, key, public, iid, grant = system
    gateway = create_http_app(APIBridge("http://testserver", transport=httpx.ASGITransport(app=app)))
    async with gateway.router.lifespan_context(gateway):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://localhost:8788"
        ) as bare:
            challenge = await bare.post("/mcp", json={})
            assert challenge.status_code == 401
            assert (
                'resource_metadata="http://127.0.0.1:8788/.well-known/oauth-protected-resource/mcp"'
                in challenge.headers["www-authenticate"]
            )
            metadata = await bare.get("/.well-known/oauth-protected-resource/mcp")
            assert metadata.status_code == 200 and metadata.json()["authorization_servers"] == [
                "https://idp.example.invalid"
            ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), headers={"Authorization": "Bearer " + token(key)}
        ) as client:
            async with streamable_http_client("http://localhost:8788/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as sdk:
                    await sdk.initialize()
                    projects = await sdk.call_tool("list_projects", {})
                    assert not projects.isError and projects.structuredContent["data"][0]["id"] == p["id"]
                    # Grants revoke on the next actual tool request, with no restart.
                    assert (
                        web.patch(
                            f"/api/v1/projects/{p['id']}/oauth-grants/{grant['id']}",
                            headers={**h, "If-Match": str(grant["row_version"])},
                            json={"active": False},
                        ).status_code
                        == 200
                    )
                    response = await client.post("http://localhost:8788/mcp", json={})
                    assert response.status_code == 401
