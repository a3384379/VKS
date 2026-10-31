"""Fixed impact reports disclose limits and retain their actual baseline."""

from test_versions import ctx as ctx  # pytest fixture registration

from copy import deepcopy
from dataclasses import replace

import pytest

from test_versions import edit
from vks_api.documents import DocumentService
from vks_api.models import Entity, Revision, Relation, Selection, uid
from vks_api.service import canonical, sha


def graph(s, pid, aid, count, star=False):
    entities = [Entity(id=uid(), project_id=pid, type="action", stable_key="scope-" + uid()) for _ in range(count)]
    s.add_all(entities)
    s.flush()
    rows = [
        Revision(
            id=uid(),
            entity_id=e.id,
            project_id=pid,
            name=e.stable_key,
            content="",
            content_sha256=sha(e.id),
            semantic_content_sha256=sha(e.id),
            metadata_json={},
        )
        for e in entities
    ]
    s.add_all(rows)
    s.flush()
    edges = [
        Relation(
            id=uid(),
            project_id=pid,
            analysis_set_id=aid,
            from_revision_id=rows[0 if star else i - 1].id,
            to_revision_id=rows[i].id,
            relation_type="depends_on",
            excluded=False,
            validity_status="valid",
            causal_basis="not_established",
            evidence_ids=[],
            reason="Synthetic impact topology",
        )
        for i in range(1, count)
    ]
    s.add_all(edges)
    s.add_all([Selection(analysis_set_id=aid, entity_id=row.entity_id, entity_revision_id=row.id) for row in rows])
    s.flush()
    return rows, edges


@pytest.mark.parametrize("boundary", ["depth", "nodes", "paths", "complete"])
def test_impact_paths_expose_each_traversal_boundary(ctx, boundary):
    service, _, p, _, _, clone, versions = ctx
    with service.db() as s:
        rows, edges = graph(
            s, p["id"], clone["id"], 6 if boundary != "complete" else 4, star=boundary in ("nodes", "paths")
        )
        paths, scope = versions.impact_paths(
            s,
            {rows[0].entity_id},
            edges,
            depth=3,
            max_nodes=3 if boundary == "nodes" else 200,
            path_limit=2 if boundary == "paths" else 2000,
            with_scope=True,
        )
        assert scope["max_hops"] == 3
        if boundary == "complete":
            assert len(paths) == 3 and not scope["is_truncated"]
        else:
            assert scope[{"depth": "depth_truncated", "nodes": "nodes_truncated", "paths": "paths_truncated"}[boundary]]
            assert scope["is_truncated"]
            assert len(paths) <= (2 if boundary in ("nodes", "paths") else 3)


def test_compare_reports_journey_cap_and_visible_only_counts(ctx):
    service, actor, p, w, release, clone, versions = ctx
    edited = edit(ctx)
    source = w["documents"][0]
    action = w["actions"][0]
    with service.db() as s:
        entities = [
            Entity(id=uid(), project_id=p["id"], type="journey", stable_key="impact-" + uid()) for _ in range(201)
        ]
        s.add_all(entities)
        s.flush()
        for entity in entities:
            dsl = dict(
                schema_version="2.0",
                key=entity.stable_key,
                title="Synthetic source-backed flow",
                entry="action",
                nodes=[
                    dict(
                        key="action",
                        kind="action",
                        title="Action",
                        x=0,
                        y=0,
                        binding=dict(entity_id=action["entity_id"], revision_id=action["entity_revision_id"]),
                    ),
                    dict(key="end", kind="end", title="End", x=100, y=0),
                ],
                edges=[dict(key="next", source="action", target="end", label="")],
                semantics=dict(
                    kind="business_process",
                    basis="declared",
                    purpose="Synthetic purpose",
                    outcome="Synthetic outcome",
                    source_refs=[dict(entity_id=source["entity_id"], revision_id=edited["entity_revision_id"])],
                ),
            )
            data = canonical(dsl)
            row = Revision(
                id=uid(),
                entity_id=entity.id,
                project_id=p["id"],
                name=dsl["title"],
                content=data,
                content_sha256=sha(data),
                semantic_content_sha256=sha(data),
                metadata_json={"journey_dsl": dsl},
            )
            s.add(row)
            s.flush()
            s.add(Selection(analysis_set_id=clone["id"], entity_id=entity.id, entity_revision_id=row.id))
        s.flush()
        raw = versions.compare(s, p["id"], release["id"], clone["id"])
    full = versions.public_report(actor, p["id"], raw)
    assert len(full["journey_impacts"]) == 200
    assert full["impact_scope"]["journey_truncated"] and full["impact_scope"]["is_truncated"]
    limited = replace(actor, scopes=actor.scopes - {"source:read", "trace:read"})
    public = versions.public_report(limited, p["id"], raw)
    scope = public["impact_scope"]
    assert scope["visibility_limited"] and scope["is_truncated"]
    assert scope["returned_path_count"] == len(public["impact_paths"])
    assert scope["returned_journey_count"] == len(public["journey_impacts"])
    assert not {"depth_truncated", "nodes_truncated", "paths_truncated", "journey_truncated"} & set(scope)
    assert not any(key.startswith("_") for key in public)


def test_fixed_change_report_keeps_selection_version_and_source_vectors_after_draft_changes(ctx):
    service, actor, p, w, release, clone, versions = ctx
    first = edit(ctx)
    with service.db() as s:
        raw = deepcopy(versions.compare(s, p["id"], release["id"], clone["id"]))
    report = versions.public_report(actor, p["id"], raw)
    docs = DocumentService(service)
    doc = w["documents"][0]
    old = docs.read_revision(actor, doc["entity_id"], first["entity_revision_id"])
    docs.save_draft(
        actor,
        doc["entity_id"],
        dict(
            analysis_set_id=clone["id"],
            base_revision_id=first["entity_revision_id"],
            markdown=old["markdown"] + "\n## Later source\nAnother change.\n",
            locked_section_ids=[],
        ),
        2,
    )
    current = service.workspace(actor, p["id"], clone["id"])
    assert current["selected_analysis_set"]["selection_version"] != report["to_selection_version"]
    frozen = versions.public_report(actor, p["id"], raw)
    assert frozen["to_selection_version"] == report["to_selection_version"]
    assert frozen["source_vectors"] == report["source_vectors"]
    assert frozen["source_vectors"]["from"] == service.workspace(actor, p["id"], release_id=release["id"])["sources"]
    assert frozen["source_vectors"]["to"] != current["sources"]
    assert frozen["changes"] == report["changes"]
    assert not any(key.startswith("_") for key in frozen)


def test_legacy_impact_report_does_not_invent_scope_vectors_or_target_version(ctx):
    service, actor, p, _, release, clone, versions = ctx
    edit(ctx)
    with service.db() as s:
        raw = versions.compare(s, p["id"], release["id"], clone["id"])
    legacy = {
        key: value
        for key, value in raw.items()
        if not key.startswith("_") and key not in ("impact_scope", "source_vectors", "to_selection_version")
    }
    public = versions.public_report(actor, p["id"], legacy)
    assert not {"impact_scope", "source_vectors", "to_selection_version"} & set(public)


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_official_sdk_returns_fixed_impact_scope_vectors_and_final_selection_version(
    ctx, monkeypatch, tmp_path, automatic
):
    import httpx
    from mcp.shared.memory import create_connected_server_and_client_session
    from vks_api.models import Project

    pass

    service, actor, p, _, release, clone, _ = ctx
    edit(ctx)
    if automatic:
        with service.db() as s:
            version = s.get(Project, p["id"]).row_version
        pass
    job = service.create_analysis_job(
        actor, clone["id"], dict(kind="detect_drift", from_release_id=release["id"]), "sdk-impact"
    )
    service.process_job(job["id"])
    result = service.get_job(actor, job["id"])
    assert result["state"] == "succeeded", result
    monkeypatch.setenv("VKS_DATA_DIR", str(tmp_path / "module-app"))
    monkeypatch.setenv("VKS_DATABASE_URL", "sqlite:///:memory:")
    from vks_api.app import create_app
    from vks_api.mcp_gateway import APIBridge, create_server

    app = create_app(
        service.data_dir, database_url=service.engine.url.render_as_string(hide_password=False), enable_worker=False
    )
    token = service.create_token(
        actor,
        p["id"],
        dict(name="Impact scope SDK", scopes=["knowledge:read", "knowledge:edit", "source:read", "trace:read"]),
    )
    bridge = APIBridge("http://testserver", token["token"], httpx.ASGITransport(app=app))
    try:
        async with create_connected_server_and_client_session(create_server(bridge)) as sdk:
            called = await sdk.call_tool("get_change_impact_report", dict(project_id=p["id"], job_id=job["id"]))
            assert not called.isError, called.content
            report = called.structuredContent["data"]["result"]
            assert report["impact_scope"]["max_hops"] == 3
            assert report["impact_scope"]["max_nodes_per_change"] == report["impact_scope"]["journey_limit"] == 200
            assert report["source_vectors"] == result["result"]["source_vectors"]
            assert (
                report["to_selection_version"]
                == service.workspace(actor, p["id"], clone["id"])["selected_analysis_set"]["selection_version"]
            )
            assert not any(key.startswith("_") for key in report)
    finally:
        app.state.service.engine.dispose()
