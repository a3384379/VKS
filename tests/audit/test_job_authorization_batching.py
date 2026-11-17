"""One current grant evaluation bounds a job without caching mutable permissions."""

from copy import deepcopy

import pytest
from sqlalchemy import event

from vks_api.identity import MemberGrant
from vks_api.jobs import current_actor
from vks_api.models import Job, Member
from vks_api.oauth import OAuthGrant
from vks_api.service import DomainError

from tests.audit.test_batch_one import oauth_actor
from tests.identity.test_team_identity import add_user, assign, login


@pytest.mark.parametrize("revoked", ["source:read", "trace:read"])
def test_human_job_scope_batch_rechecks_grants_even_in_same_write_transaction(audit_store, revoked):
    c = audit_store
    user = add_user(c.s, c.owner)
    assign(c.s, c.owner, {"id": c.p}, user, role="maintainer")
    with c.s.db() as s:
        s.add(MemberGrant(project_id=c.p, user_id=user["id"], permissions=["source:read", "trace:read"]))
    _, actor = login(c.s)
    with c.s.db() as s:
        job = c.s._new_job(s, actor, c.p, "associate_all", {"analysis_set_id": c.aid})
        before = deepcopy(job.input_json)
        queries = []

        def queried(_conn, _cursor, statement, *_):
            if "FROM project \n" in statement:
                queries.append(statement)

        event.listen(c.s.engine, "before_cursor_execute", queried)
        try:
            first = current_actor(c.s, actor.id, c.p, s, job=job)
        finally:
            event.remove(c.s.engine, "before_cursor_execute", queried)
        assert {"source:read", "trace:read"} <= first.scopes
        assert len(queries) == 1, "one project grant evaluation must cover every requested scope"
        grant = s.get(MemberGrant, (c.p, actor.id))
        grant.permissions = [scope for scope in grant.permissions if scope != revoked]
        s.flush()
        changed = current_actor(c.s, actor.id, c.p, s, job=job)
        assert revoked not in changed.scopes
        assert ({"source:read", "trace:read"} - {revoked}) <= changed.scopes
        s.get(Member, (c.p, actor.id)).status = "inactive"
        s.flush()
        assert not current_actor(c.s, actor.id, c.p, s, job=job).scopes
        assert job.input_json == before


def test_oauth_current_grants_cannot_expand_snapshot_and_revocation_is_immediate(audit_store):
    c = audit_store
    actor, gid = oauth_actor(c, ["knowledge:read", "knowledge:edit", "source:read"])
    with c.s.db() as s:
        job = c.s._new_job(s, actor, c.p, "associate_all", {"analysis_set_id": c.aid})
        frozen = deepcopy(job.input_json)
        grant = s.get(OAuthGrant, gid)
        grant.scopes = [*grant.scopes, "trace:read"]
        s.flush()
        bounded = current_actor(c.s, actor.id, c.p, s, job=job)
        assert "source:read" in bounded.scopes and "trace:read" not in bounded.scopes
        grant.scopes = [scope for scope in grant.scopes if scope != "source:read"]
        s.flush()
        assert "source:read" not in current_actor(c.s, actor.id, c.p, s, job=job).scopes
        grant.active = False
        s.flush()
        with pytest.raises(DomainError):
            current_actor(c.s, actor.id, c.p, s, job=job)
        assert job.input_json == frozen
