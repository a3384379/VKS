"""Actual filesystem collection of exactly 5,000 files / 100 MiB synthetic source."""

import argparse
from hashlib import sha256, shake_256
import json
from pathlib import Path
import tempfile
import time
from packages.collector.snapshots import collect_directory, bundle_bytes
from packages.analyzers.vks_ingest import source_digest


def run(output):
    records = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="vks2-synthetic-collector-") as directory:
        root = Path(directory)
        size, remainder = divmod(100 * 1024 * 1024, 5000)
        for index in range(5000):
            path = f"source/C{index:04}.java"
            header = (
                f"package synthetic;\npublic class C{index} {{ public int read() {{ return {index}; }} }}\n/*".encode()
            )
            padding = size + (index < remainder) - len(header) - 3
            raw = header + shake_256(str(index).encode()).hexdigest((padding + 1) // 2)[:padding].encode() + b"*/\n"
            target = root / path
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(raw)
            records.append(dict(path=path, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
        generated = time.monotonic() - started
        tick = time.monotonic()
        snapshot = collect_directory(root)
        read_seconds = time.monotonic() - tick
        assert len(snapshot.files) == 5000 and sum(map(len, snapshot.files.values())) == 100 * 1024 * 1024
        assert snapshot.revision == source_digest(records)
        tick = time.monotonic()
        raw = bundle_bytes(snapshot, "at35-poc", "source-1", source_key="source", role="backend")
        package_seconds = time.monotonic() - tick
        report = dict(
            profile="synthetic filesystem collection, not Windows or enterprise repository acceptance",
            files=5000,
            source_bytes=100 * 1024 * 1024,
            source_inventory_sha256=snapshot.revision,
            bundle_sha256=sha256(raw).hexdigest(),
            transport_bytes=len(raw),
            generation_write_seconds=generated,
            readonly_collection_seconds=read_seconds,
            package_seconds=package_seconds,
            source_identity="same deterministic synthetic Java class/padding profile as benchmark_analysis.py",
            external_model_calls=0,
        )
    Path(output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    run(parser.parse_args().output)
