import pytest
from vks_api.service import Service, DomainError


def test_audit_search_filters_server_side_and_cursor_never_crosses_project(tmp_path):
    service = Service(tmp_path)
    _, owner = service.login(service.bootstrap_token)
    p = service.create_project(owner, {"key": "audit", "name": "审计过滤"}, "p")
    other = service.create_project(owner, {"key": "other-audit", "name": "不可混合"}, "other")
    with service.db() as s:
        for i in range(6):
            service.audit(
                s,
                owner,
                p["id"],
                "test.read" if i % 2 else "test.write",
                "resource-" + str(i),
                "succeeded" if i % 2 else "failed",
            )
        service.audit(s, owner, other["id"], "test.read", "private-resource")
    page = service.audit_events(owner, p["id"], {"action": "test.read", "outcome": "succeeded", "limit": 2})
    assert len(page) == 2 and all(e["action"] == "test.read" and e["actor_id"] == owner.id for e in page)
    more = service.audit_events(
        owner, p["id"], {"action": "test.read", "outcome": "succeeded", "limit": 2, "cursor": page[-1]["id"]}
    )
    assert len(more) == 1 and not {e["id"] for e in more} & {e["id"] for e in page}
    private = service.audit_events(owner, other["id"])[0]
    with pytest.raises(DomainError):
        service.audit_events(owner, p["id"], {"cursor": private["id"]})
    with pytest.raises(DomainError):
        service.audit_events(owner, p["id"], {"since": "2026-10-25"})
    assert service.audit_events(owner, p["id"], {"resource_id": "resource-3"})[0]["resource_id"] == "resource-3"
    token = service.create_token(owner, p["id"], {"name": "reader", "scopes": ["knowledge:read"], "expires_in_days": 1})
    with pytest.raises(DomainError) as e:
        service.audit_events(service.authenticate(token["token"], "token"), p["id"])
    assert e.value.status == 403
