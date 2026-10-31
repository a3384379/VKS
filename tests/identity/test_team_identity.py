from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from vks_api.app import create_app
from vks_api import identity
from vks_api.models import Audit, Credential, Member
from vks_api.service import DomainError, Service

PASSWORD = "fixture-passphrase-2026"


@pytest.fixture
def store(tmp_path):
    service = Service(tmp_path / "store")
    _, owner = service.login(service.bootstrap_token)
    project = service.create_project(owner, {"key": "team-project", "name": "Team project"}, "team-project")
    yield (service, owner, project)
    service.stop_worker()
    service.engine.dispose()


def add_user(service, owner, username="alice"):
    return identity.create_user(
        service, owner, {"username": username, "display_name": username.title(), "password": PASSWORD}
    )


def assign(service, owner, project, user, role="editor", status="active"):
    current = service.get_project(owner, project["id"])
    return service.set_member(
        owner, project["id"], {"user_id": user["id"], "role": role, "status": status}, current["row_version"]
    )


def login(service, username="alice"):
    secret, _ = identity.password_login(service, username, PASSWORD)
    return (secret, service.authenticate(secret, "session"))


def test_bootstrap_owner_migrates_without_changing_identity_and_secret(store):
    service, owner, project = store
    assert identity.list_users(service, owner)[0]["id"] == service.owner_id
    with service.db() as session:
        row = session.get(identity.UserIdentity, owner.id)
        assert row.username == "owner" and identity.verify_password("owner", row.password_hash)
    assert (service.data_dir / "owner-id").read_text() == owner.id
    assert service.session_data(owner)["user"]["instance_admin"] is True


def test_initial_owner_login_and_password_change_survive_restart(tmp_path):
    service = Service(tmp_path / "initial")
    token, owner = identity.password_login(service, "owner", "owner")
    assert service.authenticate(token, "session").id == service.owner_id
    identity.change_password(service, owner, "owner", PASSWORD)
    service.engine.dispose()
    restarted = Service(tmp_path / "initial")
    with pytest.raises(DomainError) as denied:
        identity.password_login(restarted, "owner", "owner")
    assert denied.value.status == 401
    token, actor = identity.password_login(restarted, "owner", PASSWORD)
    assert restarted.authenticate(token, "session").id == owner.id == actor.id
    with pytest.raises(DomainError):
        identity.create_user(restarted, actor, {"username": "short", "display_name": "Short", "password": "owner"})
    with pytest.raises(DomainError):
        identity.reset_password(restarted, actor, actor.id, "owner")
    restarted.engine.dispose()


def test_scrypt_hashes_are_salted_not_exposed_and_authentication_is_generic(store):
    service, owner, project = store
    first = add_user(service, owner)
    second = add_user(service, owner, "bravo")
    with service.db() as session:
        hashes = [session.get(identity.UserIdentity, u["id"]).password_hash for u in (first, second)]
    assert hashes[0].startswith("scrypt$16384$8$1$") and hashes[0] != hashes[1]
    assert PASSWORD not in str(hashes)
    assert not any(("hash" in field or "password" == field for field in first))
    errors = []
    for name in ("alice", "nonexistent"):
        with pytest.raises(DomainError) as exc:
            identity.password_login(service, name, "incorrect-password")
        errors.append((exc.value.code, exc.value.status, exc.value.message))
    assert errors[0] == errors[1] == ("VKS_UNAUTHENTICATED", 401, "用户名或密码不正确")


def test_identity_login_failure_budget_is_persisted_and_does_not_log_secrets(store):
    service, owner, project = store
    add_user(service, owner)
    for _ in range(5):
        with pytest.raises(DomainError) as exc:
            identity.password_login(service, "alice", "incorrect-password")
        assert exc.value.status == 401
    other = Service(service.data_dir)
    try:
        with pytest.raises(DomainError) as exc:
            identity.password_login(other, "alice", PASSWORD)
        assert exc.value.status == 429
    finally:
        other.engine.dispose()
    with service.db() as session:
        rows = list(session.scalars(select(Audit)))
        assert all(
            (
                PASSWORD not in str(row.metadata_json) and "incorrect-password" not in str(row.metadata_json)
                for row in rows
            )
        )


def test_user_with_no_memberships_cannot_discover_or_create_projects(store):
    service, owner, project = store
    add_user(service, owner)
    _, alice = login(service)
    assert service.list_projects(alice)[0] == []
    with pytest.raises(DomainError) as exc:
        service.get_project(alice, project["id"])
    assert exc.value.status == 404
    with pytest.raises(DomainError) as exc:
        service.create_project(alice, {"key": "unauthorized", "name": "No creation"}, "denied-create")
    assert exc.value.status == 403
    with pytest.raises(DomainError):
        identity.list_users(service, alice)


def test_member_roles_and_independent_sensitive_permissions(store):
    service, owner, project = store
    user = add_user(service, owner)
    assign(service, owner, project, user)
    _, alice = login(service)
    assert service.get_project(alice, project["id"])["id"] == project["id"]
    with service.db() as session:
        service.require(session, alice, project["id"], "knowledge:edit")
        with pytest.raises(DomainError):
            service.require(session, alice, project["id"], "source:read")
    grants = identity.set_member_permissions(
        service,
        owner,
        project["id"],
        {"user_id": user["id"], "permissions": ["source:read"]},
        service.get_project(owner, project["id"])["row_version"],
    )
    alice = service.authenticate(login(service)[0], "session")
    with service.db() as session:
        service.require(session, alice, project["id"], "source:read")
        with pytest.raises(DomainError):
            service.require(session, alice, project["id"], "trace:read")
    assert grants["permissions"] == ["source:read"]
    with pytest.raises(DomainError):
        identity.set_member_permissions(
            service, alice, project["id"], {"user_id": user["id"], "permissions": ["trace:read"]}, grants["row_version"]
        )


def test_viewer_cannot_be_granted_recording_or_editing(store):
    service, owner, project = store
    user = add_user(service, owner)
    assign(service, owner, project, user, "viewer")
    _, alice = login(service)
    with service.db() as session:
        with pytest.raises(DomainError):
            service.require(session, alice, project["id"], "knowledge:edit")
    with pytest.raises(DomainError) as exc:
        identity.set_member_permissions(
            service,
            owner,
            project["id"],
            {"user_id": user["id"], "permissions": ["capture:run"]},
            service.get_project(owner, project["id"])["row_version"],
        )
    assert exc.value.status == 403


def test_project_admin_does_not_become_instance_admin_and_maintainer_cannot_promote_self(store):
    service, owner, project = store
    user = add_user(service, owner)
    assign(service, owner, project, user, "maintainer")
    _, alice = login(service)
    with pytest.raises(DomainError) as exc:
        assign(service, alice, project, user, "admin")
    assert exc.value.status == 403
    assign(service, owner, project, user, "admin")
    _, alice = login(service)
    with pytest.raises(DomainError):
        identity.create_user(service, alice, {"username": "evil", "display_name": "Forbidden", "password": PASSWORD})
    with pytest.raises(DomainError):
        service.create_project(alice, {"key": "another", "name": "Denied"}, "admin-cannot-create-instance-project")


def test_instance_owner_cannot_bypass_a_project_membership(store):
    service, owner, project = store
    user = add_user(service, owner)
    assign(service, owner, project, user, "admin")
    assign(service, owner, project, {"id": owner.id}, "viewer", "disabled")
    with pytest.raises(DomainError) as exc:
        service.get_project(owner, project["id"])
    assert exc.value.status == 404
    assert identity.list_users(service, owner)


def test_member_revocation_revokes_owned_machine_tokens_and_user_sessions(store):
    service, owner, project = store
    user = add_user(service, owner)
    assign(service, owner, project, user, "admin")
    secret, alice = login(service)
    machine = service.create_token(alice, project["id"], {"name": "Alice AI", "scopes": ["knowledge:read"]})
    assign(service, owner, project, user, "admin", "disabled")
    for value, kind in ((secret, "session"), (machine["token"], "token")):
        with pytest.raises(DomainError) as exc:
            service.authenticate(value, kind)
        assert exc.value.status == 401


def test_read_transaction_grants_cache_checks_each_scope_and_refreshes_membership(store):
    service, owner, project = store
    user = add_user(service, owner)
    assign(service, owner, project, user, "editor")
    _, alice = login(service)
    with service.db() as session:
        session.info["vks_read_only"] = True
        service.require(session, alice, project["id"], "knowledge:read")
        cached = session.info["vks_auth_grants"]
        service.require(session, alice, project["id"], "knowledge:edit")
        assert session.info["vks_auth_grants"] is cached
        with pytest.raises(DomainError) as denied:
            service.require(session, alice, project["id"], "source:read")
        assert denied.value.status == 403
    assign(service, owner, project, user, "viewer")
    with service.db() as session:
        session.info["vks_read_only"] = True
        service.require(session, alice, project["id"], "knowledge:read")
        with pytest.raises(DomainError) as denied:
            service.require(session, alice, project["id"], "knowledge:edit")
        assert denied.value.status == 403


def test_read_transaction_cache_never_preserves_a_revoked_machine_credential(store):
    service, owner, project = store
    created = service.create_token(owner, project["id"], {"name": "read cache test", "scopes": ["knowledge:read"]})
    machine = service.authenticate(created["token"], "token")
    with service.db() as session:
        session.info["vks_read_only"] = True
        service.require(session, machine, project["id"], "knowledge:read")
    with service.db() as session:
        session.get(Credential, created["id"]).revoked = True
    with service.db() as session:
        session.info["vks_read_only"] = True
        with pytest.raises(DomainError) as denied:
            service.require(session, machine, project["id"], "knowledge:read")
        assert denied.value.status == 401


def test_token_scope_cannot_exceed_issuer_grants_and_reduction_revokes_it(store):
    service, owner, project = store
    user = add_user(service, owner)
    assign(service, owner, project, user, "maintainer")
    _, alice = login(service)
    with pytest.raises(DomainError):
        service.create_token(alice, project["id"], {"name": "Sensitive", "scopes": ["knowledge:read", "source:read"]})
    machine = service.create_token(alice, project["id"], {"name": "Ordinary", "scopes": ["knowledge:read"]})
    assign(service, owner, project, user, "viewer")
    with pytest.raises(DomainError):
        service.authenticate(machine["token"], "token")


def test_disabling_user_preserves_last_project_admin_rule_and_invalidates_all_access(store):
    service, owner, project = store
    user = add_user(service, owner)
    assign(service, owner, project, user, "admin")
    secret, alice = login(service)
    machine = service.create_token(alice, project["id"], {"name": "AI", "scopes": ["knowledge:read"]})
    result = identity.update_user(service, owner, user["id"], {"status": "disabled"}, 1)
    assert result["status"] == "disabled"
    for value, kind in ((secret, "session"), (machine["token"], "token")):
        with pytest.raises(DomainError):
            service.authenticate(value, kind)
    with pytest.raises(DomainError):
        identity.password_login(service, "alice", PASSWORD)
    assert (
        next((m for m in service.members(owner, project["id"]) if m["user_id"] == user["id"]))["status"] == "disabled"
    )
    with pytest.raises(DomainError):
        identity.update_user(service, owner, owner.id, {"status": "disabled"}, 1)


def test_password_reset_and_self_change_revoke_old_credentials(store):
    service, owner, project = store
    user = add_user(service, owner)
    old, alice = login(service)
    identity.reset_password(service, owner, user["id"], "replacement-password-2026")
    with pytest.raises(DomainError):
        service.authenticate(old, "session")
    with pytest.raises(DomainError):
        identity.password_login(service, "alice", PASSWORD)
    new, alice = identity.password_login(service, "alice", "replacement-password-2026")
    identity.change_password(service, alice, "replacement-password-2026", "third-password-fixture-2026")
    with pytest.raises(DomainError):
        service.authenticate(new, "session")
    assert identity.password_login(service, "alice", "third-password-fixture-2026")


def test_concurrent_last_admin_removal_accepts_only_one_operation(store):
    service, owner, project = store
    first = add_user(service, owner)
    second = add_user(service, owner, "bravo")
    assign(service, owner, project, first, "admin")
    assign(service, owner, project, second, "admin")
    assign(service, owner, project, {"id": owner.id}, "viewer", "disabled")
    second_service = Service(service.data_dir)
    _, alice = login(service)
    _, bravo = login(second_service, "bravo")
    barrier = Barrier(2)
    version = service.get_project(alice, project["id"])["row_version"]

    def demote(s, actor):
        barrier.wait(timeout=5)
        try:
            return s.set_member(
                actor, project["id"], {"user_id": actor.id, "role": "viewer", "status": "active"}, version
            )
        except DomainError as exc:
            return exc

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                f.result(timeout=10)
                for f in (pool.submit(demote, service, alice), pool.submit(demote, second_service, bravo))
            ]
        assert sum((isinstance(item, list) for item in results)) == 1
        assert [item.status for item in results if isinstance(item, DomainError)] == [412]
        with service.db() as session:
            assert (
                len(
                    list(
                        session.scalars(
                            select(Member).where(
                                Member.project_id == project["id"], Member.role == "admin", Member.status == "active"
                            )
                        )
                    )
                )
                == 1
            )
    finally:
        second_service.engine.dispose()


def test_identity_http_crud_csrf_roles_and_password_login(tmp_path):
    app = create_app(data_dir=tmp_path / "http-store", enable_worker=False)
    service = app.state.service
    with TestClient(app) as owner_web, TestClient(app) as user_web:
        signed = owner_web.post("/api/v1/auth/session", json={"bootstrap_token": service.bootstrap_token})
        assert signed.status_code == 200
        assert "HttpOnly" in signed.headers["set-cookie"] and "SameSite=strict" in signed.headers["set-cookie"]
        owner_headers = {"X-CSRF-Token": signed.json()["data"]["csrf_token"], "Idempotency-Key": str(uuid4())}
        denied = owner_web.post(
            "/api/v1/users", json={"username": "alice", "display_name": "Alice", "password": PASSWORD}
        )
        assert denied.status_code == 403
        created = owner_web.post(
            "/api/v1/users",
            headers=owner_headers,
            json={"username": "alice", "display_name": "Alice", "password": PASSWORD},
        )
        assert created.status_code == 201, created.text
        user = created.json()["data"]
        listed = owner_web.get("/api/v1/users").json()["data"]
        assert {u["username"] for u in listed} == {"owner", "alice"}
        assert PASSWORD not in str(listed) and "password_hash" not in str(listed)
        project = owner_web.post(
            "/api/v1/projects", headers=owner_headers, json={"key": "http-team", "name": "HTTP team"}
        ).json()["data"]
        assigned = owner_web.put(
            f"/api/v1/projects/{project['id']}/members",
            headers={**owner_headers, "If-Match": "1"},
            json={"user_id": user["id"], "role": "viewer", "status": "active"},
        )
        assert assigned.status_code == 200, assigned.text
        login_response = user_web.post("/api/v1/auth/login", json={"username": "ALICE", "password": PASSWORD})
        assert login_response.status_code == 200, login_response.text
        session = login_response.json()["data"]
        assert session["user"]["display_name"] == "Alice" and (not session["user"]["instance_admin"])
        assert user_web.get("/api/v1/projects").json()["data"][0]["id"] == project["id"]
        assert user_web.get("/api/v1/users").status_code == 403
        assert user_web.get(f"/api/v1/projects/{project['id']}/members").status_code == 403
        user_headers = {"X-CSRF-Token": session["csrf_token"]}
        forged = user_web.patch(
            "/api/v1/auth/profile",
            headers={**user_headers, "If-Match": "1"},
            json={"display_name": "Intruder", "instance_admin": True},
        )
        assert forged.status_code == 422
        changed = user_web.patch(
            "/api/v1/auth/profile", headers={**user_headers, "If-Match": "1"}, json={"display_name": "Alice Updated"}
        )
        assert changed.status_code == 200
        stale = user_web.patch(
            "/api/v1/auth/profile", headers={**user_headers, "If-Match": "1"}, json={"display_name": "Lost update"}
        )
        assert stale.status_code == 412
        reset = owner_web.post(
            f"/api/v1/users/{user['id']}/password-reset",
            headers=owner_headers,
            json={"new_password": "replacement-fixture-password"},
        )
        assert reset.status_code == 200
        assert user_web.get("/api/v1/auth/session").json()["data"]["authenticated"] is False
        assert user_web.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 401
        assert (
            user_web.post(
                "/api/v1/auth/login", json={"username": "alice", "password": "replacement-fixture-password"}
            ).status_code
            == 200
        )
    service.engine.dispose()


def test_sensitive_grants_http_use_project_optimistic_version(tmp_path):
    app = create_app(data_dir=tmp_path / "grants-http", enable_worker=False)
    service = app.state.service
    with TestClient(app) as web:
        session = web.post("/api/v1/auth/session", json={"bootstrap_token": service.bootstrap_token}).json()["data"]
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": str(uuid4())}
        user = web.post(
            "/api/v1/users",
            headers=headers,
            json={"username": "editor", "display_name": "Editor", "password": PASSWORD},
        ).json()["data"]
        project = web.post(
            "/api/v1/projects", headers=headers, json={"key": "grant-project", "name": "Grant project"}
        ).json()["data"]
        assert (
            web.put(
                f"/api/v1/projects/{project['id']}/members",
                headers={**headers, "If-Match": "1"},
                json={"user_id": user["id"], "role": "editor", "status": "active"},
            ).status_code
            == 200
        )
        permissions = web.get(f"/api/v1/projects/{project['id']}/member-permissions").json()["data"]
        response = web.put(
            f"/api/v1/projects/{project['id']}/member-permissions",
            headers={**headers, "If-Match": str(permissions["row_version"])},
            json={"user_id": user["id"], "permissions": ["source:read", "trace:read"]},
        )
        assert response.status_code == 200, response.text
        stale = web.put(
            f"/api/v1/projects/{project['id']}/member-permissions",
            headers={**headers, "If-Match": str(permissions["row_version"])},
            json={"user_id": user["id"], "permissions": []},
        )
        assert stale.status_code == 412
        forbidden = web.put(
            f"/api/v1/projects/{project['id']}/member-permissions",
            headers={**headers, "If-Match": str(response.json()["data"]["row_version"])},
            json={"user_id": user["id"], "permissions": ["knowledge:edit"]},
        )
        assert forbidden.status_code == 422
    service.engine.dispose()


def test_last_remaining_project_admin_cannot_be_disabled_by_instance_owner(store):
    service, owner, project = store
    user = add_user(service, owner)
    assign(service, owner, project, user, "admin")
    assign(service, owner, project, {"id": owner.id}, "viewer", "disabled")
    with pytest.raises(DomainError) as exc:
        identity.update_user(service, owner, user["id"], {"status": "disabled"}, 1)
    assert exc.value.status == 409
    assert identity.password_login(service, "alice", PASSWORD)


def test_pre_team_machine_tokens_receive_owner_link_and_rotate_with_owner(store):
    from vks_api.service import sha

    service, owner, project = store
    with service.db() as session:
        session.execute(identity.MachineOwner.__table__.delete())
        session.execute(identity.MemberGrant.__table__.delete())
        session.execute(identity.UserIdentity.__table__.delete())
        session.add(
            Credential(
                id=str(uuid4()),
                token_hash=sha("legacy-synthetic-token"),
                kind="token",
                actor_id=str(uuid4()),
                project_id=project["id"],
                name="Legacy",
                scopes=["knowledge:read"],
                csrf_token="",
                expires_at="2099-01-01T00:00:00Z",
            )
        )
    service.login(service.bootstrap_token)
    assert service.authenticate("legacy-synthetic-token", "token")
    identity.reset_password(service, owner, owner.id, "owner-replacement-password")
    with pytest.raises(DomainError):
        service.authenticate("legacy-synthetic-token", "token")
