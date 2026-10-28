from copy import deepcopy
from hashlib import sha256

import pytest

from packages.analyzers.java_spring import analyze_sources
from packages.analyzers.syntax_cache import MemoryUnitCache, cache_scope


def artifact(path, text, service="billing"):
    raw = text.encode()
    return dict(path=path, content=raw, sha256=sha256(raw).hexdigest(), kind="source", source_id=service)


def analyze(files, cache=None):
    sources = [dict(source_id=name, service_key=name) for name in sorted({r["source_id"] for r in files})]
    if cache is None:
        return analyze_sources(files, sources)
    with cache_scope(cache):
        return analyze_sources(files, sources)


def facts(result):
    return result.entities, result.evidence, result.relations, result.issues, result.supported_paths


def chain():
    return [
        artifact("A.java", "package demo; class A { Leaf leaf; int run() { return leaf.value(); } }"),
        artifact("B.java", "package demo; class B { Leaf leaf; int run() { return leaf.value(); } }"),
        artifact("Leaf.java", "package demo; class Leaf { int value() { return 1; } }"),
        artifact("Other.java", "package demo; class Other { int independent() { return 9; } }"),
    ]


def test_reader_change_reuses_other_callers_and_real_dependency_results():
    files, cache = chain(), MemoryUnitCache()
    analyze(files, cache)
    files[0] = artifact("A.java", "package demo; class A { Leaf leaf; int run() { return leaf.value() + 2; } }")
    result = analyze(files, cache)
    assert facts(result) == facts(analyze(files))
    assert result.metrics["dependency_recomputed_files"] == 2  # A and its fixed Leaf context, not B/Other.
    assert result.metrics["dependency_reused_files"] == 2
    assert result.metrics["dependency_fingerprint_misses"] == 1
    assert result.metrics["dependency_calls_recomputed"] == 1
    assert result.metrics["dependency_cache_fallbacks"] == 0


@pytest.mark.parametrize("replacement", [None, "package demo; class Leaf { int renamed() { return 2; } }"])
def test_dependency_removal_or_rename_invalidates_callers_but_not_unrelated_files(replacement):
    files, cache = chain(), MemoryUnitCache()
    analyze(files, cache)
    files = [row for row in files if row["path"] != "Leaf.java"]
    if replacement:
        files.append(artifact("Leaf.java", replacement))
    result = analyze(files, cache)
    assert facts(result) == facts(analyze(files))
    assert result.metrics["dependency_reused_files"] == 1
    assert result.metrics["dependency_cache_fallbacks"] == 0
    assert result.metrics["unresolved_calls"] == 2
    assert not any(r["type"] == "may_call" for r in result.relations)


def test_newly_available_target_invalidates_previously_unresolved_reader():
    files, cache = chain(), MemoryUnitCache()
    absent = [row for row in files if row["path"] != "Leaf.java"]
    assert analyze(absent, cache).metrics["unresolved_calls"] == 2
    result = analyze(files, cache)
    assert facts(result) == facts(analyze(files))
    assert result.metrics["resolved_calls"] == 2 and result.metrics["dependency_reused_files"] == 1
    assert result.metrics["dependency_cache_fallbacks"] == 0


def test_deleting_first_caller_does_not_drop_shared_foreign_evidence():
    files, cache = chain(), MemoryUnitCache()
    analyze(files, cache)
    result = analyze(files[1:], cache)
    assert facts(result) == facts(analyze(files[1:]))
    assert result.metrics["dependency_recomputed_files"] == 0
    assert result.metrics["dependency_cache_fallbacks"] == 0
    evidence = {row["stable_key"] for row in result.evidence}
    assert all(set(row["evidence_keys"]) <= evidence for row in result.relations)


def test_overloads_and_duplicate_types_are_recomputed_without_selecting_latest():
    files = chain()
    files[0] = artifact("A.java", "package demo; class A { Leaf leaf; int run() { return leaf.value(null); } }")
    files[2] = artifact("Leaf.java", "package demo; class Leaf { int value(String v) { return 1; } }")
    cache = MemoryUnitCache()
    analyze(files, cache)
    files[2] = artifact(
        "Leaf.java", "package demo; class Leaf { int value(String v) { return 1; } int value(Object v) { return 2; } }"
    )
    result = analyze(files, cache)
    assert facts(result) == facts(analyze(files))
    assert any(row["resolution_state"] == "ambiguous" for row in result.issues)
    assert result.metrics["dependency_reused_files"] == 1
    duplicate = files + [
        artifact("duplicate/Leaf.java", "package demo; class Leaf { int value(String v) { return 3; } }")
    ]
    result = analyze(duplicate, cache)
    assert facts(result) == facts(analyze(duplicate))
    assert result.metrics["dependency_cache_fallbacks"] == 0


@pytest.mark.parametrize("adapter", ["spring", "feign", "xxl"])
def test_shared_adapter_identity_conflicts_follow_current_declarations(adapter):
    if adapter == "spring":
        template = 'package demo; import org.springframework.web.bind.annotation.GetMapping; class %s { @GetMapping("/same") int get() { return 1; } }'
    elif adapter == "feign":
        template = 'package demo; import org.springframework.web.bind.annotation.GetMapping; import org.springframework.cloud.openfeign.FeignClient; @FeignClient("target") interface %s { @GetMapping("/same") int get(); }'
    else:
        template = 'package demo; import com.xxl.job.core.handler.annotation.XxlJob; class %s { @XxlJob("same") void job() {} }'
    files = [artifact("A.java", template % "A"), artifact("B.java", template % "B"), chain()[-1]]
    cache = MemoryUnitCache()
    analyze(files, cache)
    result = analyze(files[1:], cache)
    assert facts(result) == facts(analyze(files[1:]))
    assert result.metrics["dependency_recomputed_files"] == 1
    assert result.metrics["dependency_reused_files"] == 1
    assert result.metrics["dependency_cache_fallbacks"] == 0


def test_mapper_include_table_owner_changes_and_source_moves_recompute_exact_context():
    files = [
        artifact("M1.java", "package demo; interface M1 { int find(); }"),
        artifact("M2.java", "package demo; interface M2 { int find(); }"),
        artifact(
            "A.xml",
            '<mapper namespace="demo.M1"><select id="find">SELECT id FROM <include refid="demo.Shared.table"/></select></mapper>',
        ),
        artifact("Shared.xml", '<mapper namespace="demo.Shared"><sql id="table">invoice</sql></mapper>'),
        artifact("B.xml", '<mapper namespace="demo.M2"><select id="find">SELECT id FROM invoice</select></mapper>'),
        chain()[-1],
    ]
    cache = MemoryUnitCache()
    analyze(files, cache)
    files[3] = artifact("Shared.xml", '<mapper namespace="demo.Shared"><sql id="table">changed_invoice</sql></mapper>')
    result = analyze(files, cache)
    assert facts(result) == facts(analyze(files))
    assert {e["metadata"]["table_name"] for e in result.entities if e["type"] == "table"} == {
        "invoice",
        "changed_invoice",
    }
    assert result.metrics["dependency_cache_fallbacks"] == 0
    assert result.metrics["dependency_reused_files"] == 1
    files[0] = dict(files[0], path="moved/M1.java")
    result = analyze(files, cache)
    assert facts(result) == facts(analyze(files))
    assert result.metrics["dependency_cache_fallbacks"] == 0


def test_cache_corruption_recovers_without_executable_deserialization():
    files, cache = chain(), MemoryUnitCache()
    analyze(files, cache)
    for key, packet in list(cache.values.items()):
        if key[0] == "semantic":
            broken = deepcopy(packet)
            broken["payload_sha256"] = "0" * 64
            cache.values[key] = broken
    result = analyze(files, cache)
    assert facts(result) == facts(analyze(files))
    assert result.metrics["dependency_recomputed_files"] == 4
    assert result.metrics["syntax_parsed_files"] == 0


@pytest.mark.parametrize("invalid", ["oversized", "trailing", "invalid-base64", "wrong-size"])
def test_compressed_semantic_cache_is_bounded_and_rejects_invalid_streams(monkeypatch, invalid):
    import base64
    import zlib
    from packages.analyzers import incremental
    from packages.analyzers.syntax_cache import canonical

    monkeypatch.setattr(incremental, "MAX_PACKET_BYTES", 1024)
    raw = b"x" * (2048 if invalid == "oversized" else 100)
    compressed = zlib.compress(raw) + (b"untrusted-tail" if invalid == "trailing" else b"")
    payload = dict(encoding="json-zlib-v1", decoded_bytes=100, data=base64.b64encode(compressed).decode())
    if invalid == "invalid-base64":
        payload["data"] = "!invalid!"
    if invalid == "wrong-size":
        payload["decoded_bytes"] = 99
    cache = MemoryUnitCache()
    cache.store(
        "semantic",
        "k",
        "p",
        dict(source_sha256="k", processor="p", payload=payload, payload_sha256=sha256(canonical(payload)).hexdigest()),
    )
    assert incremental.load(cache, "k", "p", "A.java", {"A.java"}) is None
