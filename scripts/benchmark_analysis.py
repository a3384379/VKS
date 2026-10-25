"""AT-35 phase evidence with exactly 5,000 Java files / 100 MiB material bytes."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256, shake_256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import httpx
from vks_api.service import Service

from packages.analyzers.cancellation import AnalysisCancelled, cancellation_scope
from packages.analyzers.java_spring import Analyzer
from packages.analyzers.vks_ingest import (
    MAX_BUNDLE_BYTES,
    MAX_FILES,
    MAX_MANIFEST_BYTES,
    MAX_TRANSPORT_BYTES,
    _read_zip,
    parse_bundle,
)
from packages.collector.snapshots import Snapshot, bundle_bytes


def percentile(values, p):
    values = sorted(values)
    return round(values[min(len(values) - 1, int((len(values) - 1) * p))], 6)


@contextmanager
def isolated_api(service, actor, pid, store, enabled):
    if not enabled:
        yield None
        return
    token = service.create_token(
        actor,
        pid,
        {
            "name": "Synthetic AT35 HTTP client",
            "scopes": [
                "knowledge:read",
                "knowledge:edit",
                "source:read",
                "source:import",
                "analysis:run",
                "trace:read",
            ],
            "expires_in_days": 1,
        },
    )
    with socket.socket() as allocation:
        allocation.bind(("127.0.0.1", 0))
        port = allocation.getsockname()[1]
    with (store / "benchmark-server.log").open("w") as logfile:
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
            env=dict(os.environ, VKS_DATA_DIR=str(store), VKS_TASK_MODE="external"),
            stdout=logfile,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                headers={"Authorization": "Bearer " + token["token"]},
                timeout=60,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
            ) as client:
                for _ in range(100):
                    try:
                        if client.get("/api/v1/live").is_success:
                            break
                    except httpx.HTTPError:
                        pass
                    if process.poll() is not None:
                        raise RuntimeError("Isolated API startup failed")
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Isolated API readiness timeout")
                yield client
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def run(args):
    started = time.monotonic()
    files = {}
    target_bytes = 100 * 1024 * 1024
    count = 5000
    base, remainder = divmod(target_bytes, count)
    for index in range(count):
        header = f"package synthetic;\npublic class C{index} {{ public int read() {{ return {index}; }} }}\n/*".encode()
        footer = b"*/\n"
        size = base + (index < remainder)
        padding = size - len(header) - len(footer)
        files[f"source/C{index:04}.java"] = (
            header + shake_256(str(index).encode()).hexdigest((padding + 1) // 2)[:padding].encode() + footer
        )
    generated = time.monotonic() - started
    assert len(files) == count and sum(map(len, files.values())) == target_bytes
    tick = time.monotonic()
    raw = bundle_bytes(
        Snapshot("local", "synthetic-100mib", files), "at35-poc", "source-1", source_key="source", role="backend"
    )
    packaged = time.monotonic() - tick
    with ZipFile(BytesIO(raw)) as archive:
        entries = len(archive.infolist())
        expanded = sum(item.file_size for item in archive.infolist())
        manifest_size = archive.getinfo("manifest.json").file_size
    tick = time.monotonic()
    decoded = _read_zip(raw)
    assert len(decoded) == count + 1
    zip_seconds = time.monotonic() - tick
    tick = time.monotonic()
    parsed = parse_bundle(raw, "at35-poc")
    parse_seconds = time.monotonic() - tick
    assert parsed.analysis_metrics["parsed_files"] == 5000, parsed.analysis_metrics
    calls = []
    cancel_requested_at = None
    original = Analyzer.java

    def count_java(self, artifact):
        nonlocal cancel_requested_at
        calls.append(artifact["path"])
        if len(calls) == 10:
            cancel_requested_at = time.monotonic()
        return original(self, artifact)

    Analyzer.java = count_java
    checks = 0
    cancel_tick = time.monotonic()

    def stop_during_java():
        nonlocal checks
        checks += 1
        return cancel_requested_at is not None

    cancelled = False
    try:
        with cancellation_scope(stop_during_java, interval_seconds=0.1):
            parse_bundle(raw, "at35-poc")
    except AnalysisCancelled:
        cancelled = True
    finally:
        Analyzer.java = original
    cancel_seconds = time.monotonic() - cancel_tick
    result = {
        "profile": "AT-35 local PoC; not specified benchmark-machine acceptance",
        "source_files": count,
        "source_bytes": target_bytes,
        "zip_entries_including_manifest": entries,
        "manifest_bytes": manifest_size,
        "transport_bytes": len(raw),
        "expanded_container_bytes": expanded,
        "quotas": {
            "material_files": MAX_FILES,
            "material_bytes": MAX_BUNDLE_BYTES,
            "manifest_bytes": MAX_MANIFEST_BYTES,
            "transport_bytes": MAX_TRANSPORT_BYTES,
        },
        "sha256": sha256(raw).hexdigest(),
        "phases_seconds": {
            "generate": generated,
            "package": packaged,
            "zip_validate_and_read": zip_seconds,
            "parse_bundle_including_zip_schema_and_static": parse_seconds,
        },
        "static_metrics": parsed.analysis_metrics,
        "cancel": {
            "cancelled": cancelled,
            "java_files_started": len(calls),
            "checks": checks,
            "seconds_from_start": cancel_seconds,
            "files_after_threshold": len(calls) - 10,
            "poll_interval_seconds": 0.1,
            "seconds_after_request": time.monotonic() - cancel_requested_at if cancel_requested_at else None,
        },
        "external_model_calls": 0,
        "fixture_complexity": "5000 simple Java classes with one method each; deterministic hex comments pad to 100MiB, not enterprise code complexity",
        "source_sha256": {
            name: sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()
            for name in (
                "packages/analyzers/vks_ingest.py",
                "packages/analyzers/java_spring.py",
                "packages/analyzers/cancellation.py",
                "packages/analyzers/document_links.py",
                "services/api/vks_api/knowledge.py",
                "services/api/vks_api/jobs.py",
            )
        },
    }
    assert cancelled and cancel_requested_at is not None and time.monotonic() - cancel_requested_at < 10
    if args.persist_job:
        store = Path(tempfile.mkdtemp(prefix="vks-at35-job-poc-"))
        service = Service(store)
        _, actor = service.login(service.bootstrap_token)
        project = service.create_project(actor, {"key": "at35-poc", "name": "Synthetic 5000-file PoC"}, "project")
        metadata = {
            "filename": "source.vksbundle.zip",
            "size_bytes": len(raw),
            "sha256": sha256(raw).hexdigest(),
            "media_type": "application/zip",
        }
        with isolated_api(service, actor, project["id"], store, args.http) as client:
            upload_started = time.monotonic()
            if client:
                response = client.post(f"/api/v1/projects/{project['id']}/upload-sessions", json=metadata)
                response.raise_for_status()
                upload = response.json()["data"]
                response = client.put(upload["upload_url"], content=raw, headers={"Content-Type": "application/zip"})
                response.raise_for_status()
            else:
                upload = service.create_upload(actor, project["id"], metadata)
                service.put_upload(actor, project["id"], upload["upload_session_id"], raw)
            upload_seconds = time.monotonic() - upload_started

            def submit(index):
                tick = time.monotonic()
                payload = {"kind": "bundle", "artifact_ids": [upload["artifact_id"]]}
                if client:
                    response = client.post(
                        f"/api/v1/projects/{project['id']}/imports",
                        json=payload,
                        headers={"Idempotency-Key": "submit-" + str(index)},
                    )
                    response.raise_for_status()
                    job = response.json()["data"]
                else:
                    job = service.import_bundle(actor, project["id"], payload, "submit-" + str(index))
                return job, time.monotonic() - tick

            with ThreadPoolExecutor(max_workers=20) as pool:
                submitted = list(pool.map(submit, range(20)))
            for job, _ in submitted[1:]:
                service.cancel_job(actor, job["id"], "Unprocessed synthetic submission sample")
            chosen = submitted[0][0]
            tick = time.monotonic()
            service.process_job(chosen["id"])
            job_seconds = time.monotonic() - tick
            state = service.get_job(actor, chosen["id"])
            latencies = [value for _, value in submitted]
            result["job"] = {
                "transport": "HTTP loopback upload + 20 concurrent submissions"
                if client
                else "application-service methods",
                "store": str(store),
                "id": chosen["id"],
                "state": state["state"],
                "execution_seconds": job_seconds,
                "upload_seconds": upload_seconds,
                "submission": {
                    "concurrency": 20,
                    "requests": 20,
                    "p50_seconds": percentile(latencies, 0.5),
                    "p95_seconds": percentile(latencies, 0.95),
                    "p99_seconds": percentile(latencies, 0.99),
                },
                "issue_count": state.get("issue_count"),
                "result": state.get("result"),
            }
            assert state["state"] == "succeeded", state
        service.engine.dispose()
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--persist-job", action="store_true")
    parser.add_argument("--http", action="store_true")
    run(parser.parse_args())
