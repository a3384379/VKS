from fastapi.testclient import TestClient
from vks_api.app import create_app


def test_a11_projects_keep_pagination_metadata_and_filter_authorized_search(tmp_path):
    app = create_app(tmp_path, enable_worker=False)
    s = app.state.service
    _, owner = s.login(s.bootstrap_token)
    for index in range(25):
        s.create_project(owner, {"key": f"project-{index}", "name": f"Synthetic {index:02}"}, str(index))
    with TestClient(app) as client:
        client.post("/api/v1/auth/login", json={"username": "owner", "password": "owner"})
        first = client.get("/api/v1/projects?limit=20").json()
        assert len(first["data"]) == 20 and first["meta"]["next_cursor"]
        rest = client.get("/api/v1/projects", params={"cursor": first["meta"]["next_cursor"]}).json()
        assert len(rest["data"]) == 5 and rest["meta"]["next_cursor"] is None
        found = client.get("/api/v1/projects", params={"q": "Synthetic 24"}).json()
        assert [p["name"] for p in found["data"]] == ["Synthetic 24"]
    s.engine.dispose()
