"""Independent read/transfer assertions for the platform P1 remediation."""

from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile
import httpx
import pytest
from sqlalchemy import select
from test_model_provenance_remediation import relation_job
from test_platform_design_alignment import model_input, plain_actor
from packages.analyzers.vks_ingest import source_digest
from vks_api.ai_gateway import ModelPolicy, PolicyInput
from vks_api.documents import DocumentService
from vks_api.knowledge_tasks import KnowledgeTaskService, TaskInput
from vks_api.models import Analysis, Artifact, Evidence, Revision, uid
from vks_api.service import DomainError, Service, sha


@pytest.mark.parametrize("output_kind", ["relation", "document"])
@pytest.mark.parametrize("allow_source", [False, True])
def test_model_outputs_inherit_source_permission_of_actual_catalog_names(
    tmp_path, monkeypatch, output_kind, allow_source
):
    """External source anchors are source-scoped even on a public artifact."""
    marker = "SYNTHETIC_CATALOG_SOURCE"
    with zipfile.ZipFile(Path("fixtures/charge-demo.vksbundle.zip")) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    ui_path = manifest["uiManifest"]["path"]
    ui = json.loads(files[ui_path])
    prototype = next((row for row in manifest["artifacts"] if row["kind"] == "prototype"))
    anchor_line = next(
        (index for index, line in enumerate(files[prototype["path"]].decode().splitlines(), 1) if "query" in line)
    )
    ui["codeEntities"].append(
        dict(
            artifactPath=prototype["path"],
            sha256=prototype["sha256"],
            startLine=anchor_line,
            endLine=anchor_line,
            entityType="symbol",
            logicalKey="symbol.catalog.external",
            qualifiedName="com.synthetic." + marker + ".query",
        )
    )
    files[ui_path] = json.dumps(ui, ensure_ascii=False).encode()
    manifest["uiManifest"]["sha256"] = sha(files[ui_path])
    for row in manifest["artifacts"]:
        if row["path"] == ui_path:
            row.update(sha256=sha(files[ui_path]), size_bytes=len(files[ui_path]))
    for source in manifest["sources"]:
        source["content_sha256"] = source_digest(
            [row for row in manifest["artifacts"] if row["source_id"] == source["source_id"]]
        )
    files["manifest.json"] = json.dumps(manifest).encode()
    packed = BytesIO()
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    service = Service(tmp_path / "catalog", database_url=f"sqlite:///{tmp_path / 'catalog.db'}")
    try:
        _, owner = service.login(service.bootstrap_token)
        project = service.create_project(owner, dict(key="charge-demo", name="Catalog test"), "p")
        with service.db() as s:
            saved = service._commit_bundle(s, project["id"], owner.id, service._parse(packed.getvalue(), "charge-demo"))
            for artifact in s.scalars(select(Artifact)):
                artifact.metadata_json = dict(
                    artifact.metadata_json, allow_platform_llm=artifact.kind in ("prototype", "markdown")
                )
            s.add(
                ModelPolicy(
                    id=uid(),
                    project_id=project["id"],
                    settings=PolicyInput(
                        enabled=True,
                        endpoint="https://platform.invalid/v1",
                        model_id="catalog-fixture",
                        allow_source=allow_source,
                        max_input_tokens=100000,
                    ).model_dump(),
                )
            )
        requests = []

        def reply(config, body):
            context = json.loads(body["input"][1]["content"])
            requests.append(context)
            assert marker not in json.dumps(context["evidence"])
            source = next(
                (row for row in context["entities"] if marker in row["name"]), {"name": "No source names supplied"}
            )
            actions = [row["revision_id"] for row in context["entities"] if row["type"] == "action"][:2]
            answer = dict(
                title="Catalog-derived output",
                unknowns=[],
                steps=[
                    dict(
                        id="catalog_output",
                        tool="propose_relation" if output_kind == "relation" else "draft_document",
                        target_revision_ids=actions if output_kind == "relation" else [],
                        evidence_ids=[context["evidence"][0]["evidence_id"]],
                        relation_type="depends_on" if output_kind == "relation" else None,
                        rationale=source["name"],
                        markdown=source["name"] if output_kind == "document" else None,
                    )
                ],
            )
            return dict(
                status="completed",
                output=[dict(type="message", content=[dict(type="output_text", text=json.dumps(answer))])],
                usage=dict(input_tokens=100, output_tokens=50),
            )

        monkeypatch.setattr("vks_api.ai_gateway.request_model", reply)
        tasks = KnowledgeTaskService(service)
        aid = saved["analysis_set_id"]
        job = tasks.create(
            owner,
            project["id"],
            TaskInput(
                analysis_set_id=aid, task_kind="knowledge_plan", instruction="Propose relation", max_model_calls=1
            ).model_dump(),
            "catalog-plan",
        )
        service.process_job(job["id"])
        waiting = tasks.get(owner, job["id"])
        if not allow_source:
            assert marker not in json.dumps(requests)
            return
        service.process_job(job["id"])
        assert tasks.get(owner, job["id"])["state"] == "succeeded"
        assert len(requests) == 1
        reader = plain_actor(SimpleNamespace(s=service, p=project["id"], owner=owner))
        public = service.workspace(reader, project["id"], aid)
        assert all((marker not in row["name"] for row in public["entities"]))
        assert marker not in json.dumps(public["relations"] if output_kind == "relation" else public["documents"])
    finally:
        service.stop_worker()
        service.engine.dispose()


@pytest.mark.parametrize("restriction", ["confidential", "trace"])
def test_propose_links_idempotency_replay_rechecks_current_model_inputs(audit_store, monkeypatch, restriction):
    c = audit_store
    marker, input_id, relation = relation_job(c, monkeypatch)
    token = c.s.create_token(
        c.owner,
        c.p,
        dict(name="Independent proposal reader", scopes=["knowledge:read", "knowledge:edit", "links:propose"]),
    )
    reader = c.s.authenticate(token["token"], "token")
    payload = dict(
        analysis_set_id=c.aid,
        candidates=[
            {
                key: relation[key]
                for key in (
                    "from_revision_id",
                    "to_revision_id",
                    "relation_type",
                    "evidence_ids",
                    "causal_basis",
                    "reason",
                )
            }
        ],
    )
    payload["candidates"][0]["reason"] = "Independent repeated proposal"
    first = c.s.propose_links(reader, c.p, payload, "independent-sensitive-replay")
    assert marker in json.dumps(first)
    with c.s.db() as s:
        expected_version = s.get(Analysis, c.aid).selection_version
    assert c.s.propose_links(reader, c.p, payload, "independent-sensitive-replay") == first
    with c.s.db() as s:
        assert s.get(Analysis, c.aid).selection_version == expected_version
    with c.s.db() as s:
        artifact = s.get(Artifact, input_id)
        if restriction == "trace":
            artifact.kind = "trace"
        else:
            artifact.sensitivity = "confidential"
    with pytest.raises(DomainError):
        c.s.propose_links(reader, c.p, payload, "independent-fresh-request")
    try:
        replay = c.s.propose_links(reader, c.p, payload, "independent-sensitive-replay")
    except DomainError:
        replay = None
    with c.s.db() as s:
        assert s.get(Analysis, c.aid).selection_version == expected_version
    assert marker not in json.dumps(replay)


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["confidential", "trace"])
async def test_sdk_rechecks_uncited_model_input_between_calls(audit_store, monkeypatch, tmp_path, restriction):
    c = audit_store
    marker, input_id, relation = relation_job(c, monkeypatch)
    with c.s.db() as s:
        action = s.get(Revision, relation["from_revision_id"]).entity_id
        evidence_id = s.scalar(select(Evidence.id).where(Evidence.artifact_id == input_id))
    token = c.s.create_token(
        c.owner, c.p, dict(name="Independent SDK reader", scopes=["knowledge:read", "knowledge:edit"])
    )
    monkeypatch.setenv("VKS_DATA_DIR", str(tmp_path / "module-default"))
    monkeypatch.setenv("VKS_DATABASE_URL", "sqlite:///:memory:")
    from mcp.shared.memory import create_connected_server_and_client_session
    from vks_api.app import create_app
    from vks_api.mcp_gateway import APIBridge, create_server

    app = create_app(
        c.s.data_dir, database_url=c.s.engine.url.render_as_string(hide_password=False), enable_worker=False
    )
    bridge = APIBridge("http://testserver", token["token"], httpx.ASGITransport(app=app))
    arguments = dict(project_id=c.p, start_entity_id=action, analysis_set_id=c.aid, depth=3)
    try:
        async with create_connected_server_and_client_session(create_server(bridge)) as sdk:
            before = await sdk.call_tool("trace_business_flow", arguments)
            assert not before.isError, before.content
            assert marker in json.dumps(before.structuredContent)
            assert "model_provenance_artifact_ids" not in json.dumps(before.structuredContent)
            assert "input_provenance" not in json.dumps(before.structuredContent)
            with c.s.db() as s:
                artifact = s.get(Artifact, input_id)
                if restriction == "trace":
                    artifact.kind = "trace"
                else:
                    artifact.sensitivity = "confidential"
            after = await sdk.call_tool("trace_business_flow", arguments)
            assert not after.isError, after.content
            assert marker not in json.dumps(after.structuredContent)
            context = await sdk.call_tool(
                "get_action_context", dict(project_id=c.p, action_id=action, analysis_set_id=c.aid, depth=3)
            )
            assert not context.isError, context.content
            assert marker not in json.dumps(context.structuredContent)
            denied = await sdk.call_tool("get_evidence", dict(project_id=c.p, evidence_ids=[evidence_id]))
            assert denied.isError
            assert marker not in json.dumps(denied.structuredContent or {})
            offered = await sdk.list_tools()
            assert not {"approve_relation", "approve_journey", "publish_release"} & {row.name for row in offered.tools}
    finally:
        app.state.service.engine.dispose()


def freeze(c):
    docs = DocumentService(c.s)
    for doc in c.s.workspace(c.owner, c.p, c.aid)["documents"]:
        if doc.get("metadata", {}).get("source_kind") != "ai_generated":
            continue
        current = docs.read_revision(c.owner, doc["entity_id"], doc["entity_revision_id"])
        payload = dict(
            analysis_set_id=c.aid, revision_id=doc["entity_revision_id"], reason="Independent fixed document check"
        )
    for relation in c.s.workspace(c.owner, c.p, c.aid)["relations"]:
        c.s.set_relation_state(
            c.owner,
            relation["id"],
            {
                "excluded": dict(
                    decision="confirm", reason="Independent synthetic release", evidence_ids=relation["evidence_ids"]
                )["decision"]
                == "reject"
            },
            relation["row_version"],
        )
    candidate = c.s.save_snapshot(c.owner, c.p, dict(analysis_set_id=c.aid), "independent-precheck")
    pass
    ticket = None
    return candidate


@pytest.mark.parametrize("restriction", ["confidential", "trace"])
def test_generated_document_bundle_roundtrip_keeps_current_input_scope(audit_store, monkeypatch, tmp_path, restriction):
    c = audit_store
    marker, input_id, sent = model_input(c, monkeypatch, document=True)
    job = c.s.create_analysis_job(c.owner, c.aid, dict(kind="generate_documents"), "independent-document")
    c.s.process_job(job["id"])
    assert c.s.get_job(c.owner, job["id"])["state"] == "succeeded"
    assert len(sent) == 1
    release = freeze(c)
    with c.s.db() as s:
        artifact = s.get(Artifact, input_id)
        if restriction == "trace":
            artifact.kind = "trace"
        else:
            artifact.sensitivity = "confidential"
    exported = c.s.export(
        c.owner, c.p, dict(release_id=release["id"], format="vksbundle", include_source=True), "independent-roundtrip"
    )
    c.s.process_job(exported["id"])
    assert c.s.get_job(c.owner, exported["id"])["state"] == "succeeded"
    raw, _ = c.s.download_export(c.owner, c.p, exported["id"])
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        generated = [row for row in manifest["artifacts"] if row["path"].startswith("documents/generated-")]
        if restriction == "trace":
            assert not generated
        else:
            assert generated and all((row["sensitivity"] == "confidential" for row in generated))
    target = Service(
        tmp_path / "independent-reimport", database_url=f"sqlite:///{tmp_path / 'independent-reimport.db'}"
    )
    try:
        _, owner = target.login(target.bootstrap_token)
        project = target.create_project(owner, dict(key="charge-demo", name="Independent reimport"), "p")
        with target.db() as s:
            target._commit_bundle(s, project["id"], owner.id, target._parse(raw, "charge-demo"))
        reader = plain_actor(SimpleNamespace(s=target, p=project["id"], owner=owner))
        assert marker not in json.dumps(target.workspace(reader, project["id"]))
    finally:
        target.stop_worker()
        target.engine.dispose()
