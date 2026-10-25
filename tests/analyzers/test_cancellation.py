from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import packages.analyzers.vks_ingest as ingest
from packages.analyzers.cancellation import AnalysisCancelled, cancellation_scope, checkpoint
from packages.analyzers.java_spring import Analyzer, analyze_sources
from packages.analyzers.vks_ingest import BundleError, _read_zip


def zipped(files):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    return output.getvalue()


def test_cancellation_scope_stops_before_more_java_files_and_resets(monkeypatch):
    files = [
        {
            "path": f"C{i}.java",
            "kind": "source",
            "source_id": "src",
            "content": f"class C{i} {{ void run() {{}} }}".encode(),
            "sha256": "synthetic",
        }
        for i in range(50)
    ]
    seen = []
    original = Analyzer.java

    def counted(self, artifact):
        seen.append(artifact["path"])
        return original(self, artifact)

    monkeypatch.setattr(Analyzer, "java", counted)
    with pytest.raises(AnalysisCancelled), cancellation_scope(lambda: len(seen) >= 3, interval_seconds=0):
        analyze_sources(files, [{"source_id": "src", "service_key": "test"}])
    assert len(seen) == 3
    # No active callback survives the job's processing scope.
    checkpoint(force=True)
    result = analyze_sources(files[:1], [{"source_id": "src", "service_key": "test"}])
    assert result.metrics["parsed_files"] == 1


def test_container_manifest_is_additional_to_material_quota(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_FILES", 2)
    monkeypatch.setattr(ingest, "MAX_BUNDLE_BYTES", 20)
    accepted = _read_zip(zipped({"manifest.json": b"{}", "a.java": b"1234567890", "b.java": b"1234567890"}))
    assert len(accepted) == 3
    with pytest.raises(BundleError) as exc:
        _read_zip(zipped({"manifest.json": b"{}", "a.java": b"x", "b.java": b"y", "c.java": b"z"}))
    assert exc.value.code == "VKS_BUNDLE_TOO_LARGE"
    with pytest.raises(BundleError) as exc:
        _read_zip(zipped({"manifest.json": b"{}", "a.java": b"12345678901", "b.java": b"1234567890"}))
    assert exc.value.code == "VKS_BUNDLE_TOO_LARGE"


def test_manifest_and_transport_have_separate_finite_limits(monkeypatch):
    raw = zipped({"manifest.json": b"12345", "a.java": b"class A {}"})
    monkeypatch.setattr(ingest, "MAX_MANIFEST_BYTES", 4)
    with pytest.raises(BundleError) as exc:
        _read_zip(raw)
    assert exc.value.code == "VKS_BUNDLE_TOO_LARGE"
    monkeypatch.setattr(ingest, "MAX_MANIFEST_BYTES", 1024)
    monkeypatch.setattr(ingest, "MAX_TRANSPORT_BYTES", len(raw) - 1)
    with pytest.raises(BundleError) as exc:
        _read_zip(raw)
    assert exc.value.code == "VKS_BUNDLE_TOO_LARGE"


def test_compression_bomb_guard_still_applies_to_manifest_bytes():
    with pytest.raises(BundleError) as exc:
        _read_zip(zipped({"manifest.json": b" " * 2_000_000, "a.java": b"class A {}"}))
    assert exc.value.code == "VKS_COMPRESSION_LIMIT"
