from hashlib import sha256
from io import BytesIO
import json, zipfile
from packages.collector.distribution import distribution
from test_journeys import store as store  # pytest fixture registration
from fastapi.testclient import TestClient


def test_distribution_is_deterministic_self_verified_and_contains_no_instance_data():
    raw = distribution()
    assert raw == distribution()
    with zipfile.ZipFile(BytesIO(raw)) as z:
        manifest = json.loads(z.read("distribution-manifest.json"))
        assert "packages/collector/windows_files.py" in z.namelist()
        assert "packages/collector/agent_gui.py" in z.namelist()
        assert "start-windows.cmd" in z.namelist()
        assert not [
            p
            for p in z.namelist()
            if any(part in (".vks", ".env", "__pycache__", "node_modules") for part in p.split("/"))
        ]
        for item in manifest["files"]:
            content = z.read(item["path"])
            assert len(content) == item["size_bytes"] and sha256(content).hexdigest() == item["sha256"]


def test_download_requires_session_and_returns_same_distribution_and_hash(store):
    service, actor, pid, w, app, token = store
    with TestClient(app) as c:
        assert c.get("/api/v2/collector-agent").status_code == 401
        c.cookies.set("vks_session", token)
        result = c.get("/api/v2/collector-agent")
        assert result.status_code == 200 and result.content == distribution()
        assert result.headers["x-content-sha256"] == sha256(result.content).hexdigest()
        assert result.headers["cache-control"] == "no-store"
