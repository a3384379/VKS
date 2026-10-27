"""Reproducible synthetic AT-34 local PoC. Never opens the user's .vks store."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import event, insert
from vks_api.knowledge import AnalysisSource, SourceSnapshot
from vks_api.models import Analysis, Artifact, Entity, Evidence, Project, Relation, Release, Revision, Selection, now
from vks_api.service import Service, canonical


def key(name):
    return str(uuid5(NAMESPACE_URL, "vks-at34-poc:" + name))


def digest(value):
    return sha256(value.encode()).hexdigest()


def seed(service):
    _, actor = service.login(service.bootstrap_token)
    existing, _ = service.list_projects(actor)
    if existing:
        p = existing[0]
        return actor, p["id"], key("release"), key("action-0")
    p = service.create_project(actor, {"key": "at34-poc", "name": "Synthetic AT-34 performance fixture"}, "seed")
    pid = p["id"]
    aid = key("analysis")
    snapshot = key("snapshot")
    stamp = now()
    entities = []
    revisions = []
    selections = []
    evidence = []
    relations = []
    artifacts = []

    def add_artifact(name, kind, raw):
        h = service._put_object(raw.encode())
        identifier = key("artifact-" + name)
        artifacts.append(
            {
                "id": identifier,
                "created_at": stamp,
                "project_id": pid,
                "snapshot_id": snapshot,
                "relative_path": name,
                "sha256": h,
                "size_bytes": len(raw.encode()),
                "kind": kind,
                "media_type": "text/plain",
                "sensitivity": "internal",
                "object_key": h,
                "metadata_json": {},
            }
        )
        return identifier

    page_art = add_artifact(
        "preview.html", "prototype", '<main data-vks-page="poc.page">Synthetic passive snapshot</main>'
    )
    api_art = add_artifact("api.json", "openapi", '{"synthetic":true}')

    def add(name, type_, artifact, metadata=None):
        eid = key(name)
        rid = key(name + "-revision")
        content = "Synthetic " + name
        entities.append({"id": eid, "created_at": stamp, "project_id": pid, "type": type_, "stable_key": "poc." + name})
        revisions.append(
            {
                "id": rid,
                "created_at": stamp,
                "entity_id": eid,
                "project_id": pid,
                "artifact_id": artifact,
                "name": name,
                "content": content,
                "content_sha256": digest(name + "revision"),
                "semantic_content_sha256": digest(content),
                "schema_version": "1.0",
                "locator": {"kind": "file", "relative_path": "synthetic"},
                "metadata_json": metadata or {},
            }
        )
        selections.append({"analysis_set_id": aid, "entity_id": eid, "entity_revision_id": rid})
        return rid

    page = add("page", "page", page_art)
    api = [
        add(
            "api-" + str(i),
            "api",
            api_art,
            {
                "service_key": "poc",
                "method": "GET",
                "normalized_route": "/synthetic/" + str(i),
                "condition_fingerprint": digest("{}"),
            },
        )
        for i in range(49)
    ]
    for i in range(200):
        doc_art = add_artifact(f"docs/{i}.md", "markdown", f"# Document {i}\nSynthetic rules {i}\n")
        add("document-" + str(i), "document", doc_art)
        add("section-" + str(i), "section", doc_art)
    for i in range(1000):
        action = add("action-" + str(i), "action", page_art, {"page_key": "poc.page", "event": "click"})
        eid = key("evidence-" + str(i))
        evidence.append(
            {
                "id": eid,
                "created_at": stamp,
                "project_id": pid,
                "analysis_set_id": aid,
                "artifact_id": page_art,
                "stable_key": "poc.evidence." + str(i),
                "evidence_kind": "manual",
                "content_sha256": artifacts[0]["sha256"],
                "locator": {"relative_path": "preview.html"},
                "excerpt": "Synthetic performance annotation",
                "claim": "Synthetic load only; no runtime or business claim",
            }
        )
        for j, target in enumerate([page, *api]):
            source, destination, kind = (page, action, "contains") if j == 0 else (action, target, "may_call")
            relations.append(
                {
                    "id": key(f"relation-{i}-{j}"),
                    "created_at": stamp,
                    "project_id": pid,
                    "analysis_set_id": aid,
                    "from_revision_id": source,
                    "to_revision_id": destination,
                    "relation_type": kind,
                    "review_status": "confirmed",
                    "validity_status": "valid",
                    "causal_basis": "not_established",
                    "evidence_ids": [eid],
                    "reason": "Synthetic benchmark fixture only",
                    "row_version": 1,
                    "manual_lock": True,
                }
            )
    sources = [
        {
            "source_snapshot_id": snapshot,
            "source_id": "synthetic-poc",
            "provider": "local",
            "provider_revision": "frozen-poc-1",
            "content_sha256": digest(canonical(artifacts)),
            "role": "prototype",
            "service_key": "poc",
        }
    ]
    manifest = {
        "schema_version": "1.0",
        "analysis_set_id": aid,
        "project_id": pid,
        "selection_version": 1,
        "sources": sources,
        "selections": [
            dict(
                row, content_sha256=next(r["content_sha256"] for r in revisions if r["id"] == row["entity_revision_id"])
            )
            for row in selections
        ],
        "relations": [
            {k: v for k, v in row.items() if k not in ("created_at", "project_id", "manual_lock")} for row in relations
        ],
        "runtime_alignment": "unknown",
        "ruleset_version": "synthetic-poc-1",
    }
    with service.db() as db:
        db.add(
            SourceSnapshot(
                id=snapshot,
                project_id=pid,
                source_key="synthetic-poc",
                provider="local",
                provider_revision="frozen-poc-1",
                content_sha256=sources[0]["content_sha256"],
                role="prototype",
                service_key="poc",
            )
        )
        db.flush()
        db.add(
            Analysis(
                id=aid,
                project_id=pid,
                key="poc-1",
                name="Synthetic PoC 1",
                state="frozen",
                sources=sources,
                ruleset_version="synthetic-poc-1",
                runtime_alignment="unknown",
                created_by=actor.id,
                selection_version=1,
            )
        )
        db.flush()
        for model, rows in (
            (Artifact, artifacts),
            (Entity, entities),
            (Revision, revisions),
            (Selection, selections),
            (Evidence, evidence),
            (Relation, relations),
        ):
            for offset in range(0, len(rows), 1000):
                db.execute(insert(model), rows[offset : offset + 1000])
        db.add(AnalysisSource(analysis_set_id=aid, source_snapshot_id=snapshot, role="prototype"))
        release = Release(
            id=key("release"),
            project_id=pid,
            analysis_set_id=aid,
            release_no="poc-1",
            manifest_sha256=digest(canonical(manifest)),
            manifest_json=manifest,
            published_by=actor.id,
            change_note="Frozen synthetic performance data; no user content",
        )
        db.add(release)
        db.flush()
        db.get(Project, pid).current_release_id = release.id
    return actor, pid, key("release"), key("action-0")


def percentile(values, p):
    values = sorted(values)
    return round(values[min(len(values) - 1, int((len(values) - 1) * p))] * 1000, 3) if values else None


def run(args):
    import os
    import socket
    import sys
    from threading import Event, Thread

    import httpx

    folder = Path(args.store).resolve() if args.store else Path(tempfile.mkdtemp(prefix="vks-at34-poc-"))
    if folder.name == ".vks" or (folder / "uploads").exists():
        raise SystemExit("Use a dedicated synthetic benchmark directory")
    root = Path(__file__).resolve().parents[1]
    source_names = (
        "services/api/vks_api/reading.py",
        "services/api/vks_api/service.py",
        "services/api/vks_api/identity.py",
        "services/api/vks_api/workbench_v2.py",
        "services/api/vks_api/journeys.py",
        "scripts/benchmark_reads.py",
    )
    source_hashes = {name: sha256((root / name).read_bytes()).hexdigest() for name in source_names}
    service = Service(folder)
    actor, pid, rid, action = seed(service)
    sql = Counter()
    event.listen(
        service.engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, params, context, many: sql.update(["queries"]),
    )
    process, client, server_log = None, None, None
    resources = []
    stopped = Event()
    if args.http:
        token = service.create_token(
            actor,
            pid,
            {
                "name": "Synthetic benchmark reader",
                "scopes": ["knowledge:read", "source:read", "trace:read"],
                "expires_in_days": 1,
            },
        )
        with socket.socket() as allocation:
            allocation.bind(("127.0.0.1", 0))
            port = allocation.getsockname()[1]
        server_log = (folder / "benchmark-server.log").open("w")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "vks_api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
                "--no-access-log",
            ],
            env=dict(os.environ, VKS_DATA_DIR=str(folder), VKS_TASK_MODE="external"),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + token["token"]},
            timeout=30,
            limits=httpx.Limits(
                max_connections=max(20, args.concurrency), max_keepalive_connections=max(20, args.concurrency)
            ),
        )
        for _ in range(100):
            try:
                if client.get("/api/v1/live").is_success:
                    break
            except httpx.HTTPError:
                pass
            if process.poll() is not None:
                raise RuntimeError("Isolated benchmark API did not start")
            time.sleep(0.1)
        else:
            raise RuntimeError("Isolated benchmark API readiness timeout")
    started = time.monotonic()
    deadline = started + args.seconds

    def sample_resources():
        target = process.pid if process else os.getpid()
        while not stopped.is_set():
            try:
                values = subprocess.check_output(["/bin/ps", "-o", "rss=,%cpu=", "-p", str(target)], text=True).split()
                resources.append(
                    {
                        "seconds": round(time.monotonic() - started, 3),
                        "rss_kib": int(values[0]),
                        "cpu_percent": float(values[1]),
                    }
                )
            except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
                pass
            stopped.wait(10)

    sampler = Thread(target=sample_resources, daemon=True)
    sampler.start()

    def worker(index):
        samples = []
        iteration = 0
        while time.monotonic() < deadline or iteration == 0:
            kind = ("action", "search", "graph")[iteration % 3] if args.operation == "mixed" else args.operation
            tick = time.monotonic()
            error, size = None, None
            try:
                if client:
                    if kind == "action":
                        response = client.get(
                            f"/api/v2/actions/{action}/explanation" if args.v2 else f"/api/v1/actions/{action}/context",
                            params={"project_id": pid, "release_id": rid},
                        )
                    elif kind == "graph":
                        response = client.get(
                            f"/api/v2/entities/{action}/knowledge-map"
                            if args.v2
                            else f"/api/v1/entities/{action}/relations",
                            params={
                                "project_id": pid,
                                "release_id": rid,
                                "depth": 2,
                                "max_nodes": 100 if args.v2 else 50,
                            },
                        )
                    elif kind == "workspace":
                        response = client.get(f"/api/v1/projects/{pid}/workspace", params={"release_id": rid})
                    else:
                        response = client.post(
                            f"/api/v1/projects/{pid}/search", json={"release_id": rid, "query": "action-1", "limit": 10}
                        )
                    if not response.is_success:
                        error = "HTTP_" + str(response.status_code)
                    size = len(response.content)
                elif kind == "action":
                    service.action_context(actor, pid, action, release_id=rid)
                elif kind == "graph":
                    service.graph(actor, pid, action, release_id=rid, depth=2, max_nodes=50)
                elif kind == "workspace":
                    service.workspace(actor, pid, release_id=rid)
                else:
                    service.search(actor, pid, {"release_id": rid, "query": "action-1", "limit": 10})
            except Exception as exc:  # noqa: BLE001 - benchmark must count every failure
                error = type(exc).__name__ + ":" + str(exc)[:160]
            samples.append(
                {
                    "operation": kind,
                    "seconds": time.monotonic() - tick,
                    "error": error,
                    "phase": "first_wave" if iteration == 0 else "warm",
                    "bytes": size,
                }
            )
            iteration += 1
        return samples

    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            rows = [row for batch in pool.map(worker, range(args.concurrency)) for row in batch]
    finally:
        stopped.set()
        sampler.join(timeout=2)
        if client:
            client.close()
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if server_log:
            server_log.close()
    elapsed = time.monotonic() - started

    def summary(selected):
        values = [row["seconds"] for row in selected]
        return {
            "requests": len(selected),
            "p50_ms": percentile(values, 0.5),
            "p95_ms": percentile(values, 0.95),
            "p99_ms": percentile(values, 0.99),
            "errors": dict(Counter(row["error"] for row in selected if row["error"])),
        }

    stats = {
        operation: summary([row for row in rows if row["operation"] == operation])
        for operation in sorted({row["operation"] for row in rows})
    }
    phases = {phase: summary([row for row in rows if row["phase"] == phase]) for phase in ("first_wave", "warm")}

    def sysctl(name):
        return (
            subprocess.check_output(["/usr/sbin/sysctl", "-n", name], text=True).strip()
            if platform.system() == "Darwin"
            else None
        )

    root = Path(__file__).resolve().parents[1]
    result = {
        "profile": "AT-34 local synthetic PoC; not specified 8vCPU/16GiB/network acceptance",
        "transport": "HTTP loopback, machine-token auth and response serialization"
        if args.http
        else "application-service method calls; HTTP/serialization excluded",
        "api_profile": "v2 explanation/100-node map + shared search"
        if args.v2
        else "legacy v1 context/50-node map + shared search",
        "hardware": {
            "platform": platform.platform(),
            "cpu_count": sysctl("hw.ncpu"),
            "memory_bytes": sysctl("hw.memsize"),
        },
        "dataset": {"relations": 50000, "actions": 1000, "documents": 200},
        "concurrency": args.concurrency,
        "requested_seconds": args.seconds,
        "elapsed_seconds": round(elapsed, 3),
        "sql_queries": None if args.http else sql["queries"],
        "operations": stats,
        "phases": phases,
        "resources": resources,
        "external_model_calls": 0,
        "cache": "server release-fact cache cold on start; first concurrent wave reported separately; no grants or policies cached across transactions",
        "cache_counts": None if args.http else getattr(service, "_read_cache_metrics", {}),
        "store": str(folder),
        "source_sha256": source_hashes,
        "source_changed_during_run": any(
            sha256((root / name).read_bytes()).hexdigest() != source_hashes[name] for name in source_names
        ),
    }
    Path(args.output).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    service.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--operation", choices=["mixed", "action", "search", "graph", "workspace"], default="mixed")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--v2", action="store_true", help="Exercise the V2 action composer and 100-node knowledge map over HTTP"
    )
    parser.add_argument(
        "--http", action="store_true", help="Start an isolated loopback API and include auth/serialization"
    )
    args = parser.parse_args()
    if args.v2 and not args.http:
        parser.error("--v2 requires --http so its measured endpoints cannot fall back to legacy service calls")
    run(args)
