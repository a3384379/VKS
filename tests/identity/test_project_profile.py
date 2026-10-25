import pytest
from vks_api.project_profile import ProfileService, ProjectProfile
from vks_api.service import Service, DomainError
from vks_api.models import Member, Entity


def test_project_business_metadata_uses_member_identity_cas_and_does_not_grant_access(tmp_path):
    service = Service(tmp_path)
    _, owner = service.login(service.bootstrap_token)
    p = service.create_project(owner, {"key": "profile", "name": "项目资料"}, "project")
    profiles = ProfileService(service)
    current = profiles.get(owner, p["id"])
    assert current["visibility"] == "project_members"
    payload = {
        "business_domain": "收费业务",
        "responsible_user_id": owner.id,
        "visibility": "project_members",
        "default_analysis_language": "mixed",
    }
    changed = profiles.update(owner, p["id"], payload, current["row_version"])
    assert changed["business_domain"] == "收费业务" and changed["row_version"] == 2
    with pytest.raises(DomainError) as e:
        profiles.update(owner, p["id"], payload, 1)
    assert e.value.status == 412
    with pytest.raises(DomainError):
        profiles.update(owner, p["id"], {**payload, "responsible_user_id": "unassigned-user"}, 2)
    with service.db() as db:
        assert db.get(Member, (p["id"], owner.id)).role == "admin"
        assert db.query(Entity).filter(Entity.project_id == p["id"]).count() == 0
