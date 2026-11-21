"""Transport configuration and platform-private file regressions."""

import os
import stat
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest
from fastapi.responses import HTMLResponse
from vks_api.app import create_app
from vks_api.mcp_gateway import REQUIREMENTS_READONLY, APIBridge, GatewayError, create_http_app
from vks_connect.clinic import doctor
from vks_connect.config import Connection, validate_url
from vks_connect.credentials import credential_store, token_reference

from apps.runtime.server import origin_secret
from packages.private_files import read_private_file, write_private_file
from tests.prototype import test_v3_workflow
from tests.ui.test_frontend_browser import live_api

workspace = test_v3_workflow.workspace


@pytest.mark.parametrize(
    "value",
    [
        "http://api.example.org:8000",
        "http://127.0.0.1.example.org",
        "http://2130706433",
        "https://login@example.org",
        "https://example.org/?token=x",
        "https://example.org/#a",
        "https://example.org\\@evil.example",
        "https://example.org:65536",
    ],
)
def test_endpoint_configuration_rejects_remote_plaintext_and_credentials(value):
    with pytest.raises(ValueError):
        validate_url(value, "API")


def test_endpoint_scopes_use_distinct_native_credential_keys(tmp_path):
    assert validate_url("http://[::1]:8014/", "API") == "http://[::1]:8014"
    origin = "https://vks.example.com"
    assert token_reference(tmp_path / "default.json", "api", origin) != token_reference(
        tmp_path / "default.json", "mcp", origin
    )
    assert token_reference(tmp_path / "default.json", "api", origin) != token_reference(
        tmp_path / "default.json", "api", "https://another.example.com"
    )


async def test_api_redirect_cannot_forward_bearer():
    targets = []

    def redirect(request):
        targets.append((str(request.url), request.headers["authorization"]))
        return httpx.Response(302, headers={"Location": "https://redirect.example.net"})

    bridge = APIBridge("https://vks.example.com", "synthetic", httpx.MockTransport(redirect))
    with pytest.raises(GatewayError, match="重定向"):
        await bridge.dispatch("list_projects", {})
    assert targets == [("https://vks.example.com/api/v1/projects", "Bearer synthetic")]


async def test_preview_api_response_is_bounded_before_json_parsing():
    class OversizedPreview(httpx.AsyncByteStream):
        chunks_read = 0
        closed = False

        async def __aiter__(self):
            for _ in range(10):
                self.chunks_read += 1
                yield b"x" * 65536

        async def aclose(self):
            self.closed = True

    stream = OversizedPreview()

    def preview(request):
        assert request.headers["authorization"] == "Bearer synthetic"
        assert request.headers["accept-encoding"] == "identity"
        assert request.url.path.endswith("/interactive/previews/read")
        return httpx.Response(200, stream=stream)

    bridge = APIBridge("https://vks.example.com", "synthetic", httpx.MockTransport(preview))
    with pytest.raises(GatewayError, match="超过读取预算") as exc:
        await bridge.dispatch("get_prototype_preview", {"project_id": str(uuid4())})
    assert exc.value.error["code"] == "VKS_BUDGET_EXCEEDED"
    assert stream.closed and stream.chunks_read == 6


async def test_doctor_matches_server_identities_and_saved_release(workspace, tmp_path, monkeypatch):
    api, _, service, owner, project_id, draft_id, _, _ = workspace
    stray = create_app(tmp_path / "different studio", enable_worker=False)
    for instance in (api, stray):
        instance.add_api_route("/", lambda: HTMLResponse("<title>知境 Studio</title>"))
    try:
        release = service.save_snapshot(owner, project_id, {"analysis_set_id": draft_id}, str(uuid4()))
        token = service.create_token(owner, project_id, {"name": "reader", "scopes": ["knowledge:read"]})
        monkeypatch.setenv("VKS_CONNECT_MCP_TOKEN", token["token"])
        with live_api(api) as api_url, live_api(stray) as different:
            gateway = create_http_app(APIBridge(api_url), REQUIREMENTS_READONLY)
            with live_api(gateway) as mcp_url:

                def connection(studio_url):
                    return Connection(
                        api_base_url=api_url,
                        studio_base_url=studio_url,
                        runtime_mode="documents-only",
                        runtime_url=None,
                        mcp_url=mcp_url + "/mcp",
                        default_project_id=project_id,
                    )

                result = await doctor(connection(api_url), tmp_path / "default.json", "http")
                assert result["ok"], result
                assert result["checks"]["mcp_http"]["release_id"] == release["id"]
                mismatch = await doctor(connection(different), tmp_path / "default.json", "http")
                assert not mismatch["ok"] and mismatch["checks"]["instance_match"]["status"] == "failed"
    finally:
        stray.state.service.engine.dispose()


def test_runtime_private_origin_key_is_stable_and_rejects_public_file(tmp_path, monkeypatch):
    monkeypatch.setenv("VKS_RUNTIME_DATA_DIR", str(tmp_path))
    first = origin_secret()
    assert len(first) == 32 and origin_secret() == first
    path = tmp_path / "origin.key"
    if os.name == "nt":
        import win32security as security

        descriptor = security.GetNamedSecurityInfo(
            str(path), security.SE_FILE_OBJECT, security.DACL_SECURITY_INFORMATION
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        dacl.AddAccessAllowedAce(2, 0x80000000, security.ConvertStringSidToSid("S-1-1-0"))
        security.SetNamedSecurityInfo(
            str(path), security.SE_FILE_OBJECT, security.DACL_SECURITY_INFORMATION, None, None, dacl, None
        )
    else:
        path.chmod(0o644)
    with pytest.raises(PermissionError):
        origin_secret()


def test_atomic_private_replace_and_link_refusal(tmp_path):
    path = tmp_path / "本机私有凭据"
    write_private_file(path, b"first", create_only=True)
    with pytest.raises(FileExistsError):
        write_private_file(path, b"ignore", create_only=True)
    assert read_private_file(path, 5) == b"first"
    with pytest.raises(ValueError, match="大小"):
        read_private_file(path, 4)
    write_private_file(path, b"replace")
    assert read_private_file(path, 7) == b"replace"
    link = tmp_path / "linked"
    try:
        link.symlink_to(path)
    except OSError:
        if os.name == "nt":
            return  # Administrator/developer mode controls symlink creation.
        raise
    with pytest.raises(OSError):
        read_private_file(link, 7)
    with pytest.raises(OSError):
        write_private_file(link, b"never")
    assert read_private_file(path, 7) == b"replace"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs")
def test_private_file_rejects_shared_macos_acl_even_with_mode_0600(tmp_path):
    import pwd

    private = tmp_path / "restrictive"
    private.mkdir()
    subprocess.run(
        ["/bin/chmod", "+a", "everyone deny writeextattr,file_inherit", str(private)], check=True
    )
    path = private / "origin.key"
    write_private_file(path, b"synthetic", create_only=True)
    user = pwd.getpwuid(os.geteuid()).pw_name
    subprocess.run(["/bin/chmod", "+a", f"user:{user} allow read", str(path)], check=True)
    assert read_private_file(path, 20) == b"synthetic"

    subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(path)], check=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(PermissionError, match="ACL"):
        read_private_file(path, 20)
    with pytest.raises(PermissionError, match="ACL"):
        write_private_file(path, b"changed")

    shared = tmp_path / "shared"
    shared.mkdir()
    subprocess.run(["/bin/chmod", "+a", "everyone allow read,file_inherit", str(shared)], check=True)
    with pytest.raises(PermissionError, match="ACL"):
        write_private_file(shared / "first.key", b"synthetic", create_only=True)
    assert not list(shared.iterdir())


@pytest.mark.skipif(
    sys.platform != "win32" or os.getenv("VKS_TEST_NATIVE_CREDENTIALS") != "1",
    reason="native Credential Manager test runs on the disposable Windows runner",
)
def test_native_credential_manager_round_trip():
    store = credential_store()
    reference = uuid4().hex
    try:
        assert store.get(reference) is None
        store.set(reference, "synthetic-connector-token")
        assert store.get(reference) == "synthetic-connector-token"
    finally:
        store.remove(reference)
    assert store.get(reference) is None
