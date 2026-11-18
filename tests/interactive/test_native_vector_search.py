"""Native shortlist must reproduce the original exact SQL ranking."""

import math
import random
from array import array
from concurrent.futures import ThreadPoolExecutor

import pytest
from vks_api.vector_rag import VectorStore, normalize
from vks_api.vector_search import ExactVectorSearch, decode


def reference(store, revisions, query):
    def dot(blob):
        if not isinstance(blob, bytes) or len(blob) != 4 * len(query):
            return -2.0
        try:
            score = math.sumprod(decode(blob), query)
        except (ValueError, OverflowError):
            return -2.0
        return score if math.isfinite(score) and -1.001 <= score <= 1.001 else -2.0

    import json

    with store.db() as db:
        db.create_function("vks_dot", 1, dot)
        return db.execute(
            "SELECT id,revision,start,content,vks_dot(embedding) AS score FROM chunks "
            "WHERE project='p' AND space='s' AND revision IN (SELECT value FROM json_each(?)) "
            "ORDER BY score DESC LIMIT 36",
            (json.dumps(revisions),),
        ).fetchall()


@pytest.mark.parametrize("dimensions", [64, 512, 3072])
def test_exact_candidates_scores_ties_and_authorized_scope(tmp_path, dimensions):
    rng = random.Random(31)
    store, search = VectorStore(tmp_path), ExactVectorSearch()
    query = decode(normalize([rng.uniform(-1, 1) for _ in range(dimensions)], dimensions))
    random_vectors = [normalize([rng.uniform(-1, 1) for _ in range(dimensions)], dimensions) for _ in range(90)]
    vectors = random_vectors + [random_vectors[0]] * 40
    vectors += [array("f", query).tobytes()] * 3
    vectors += [b"invalid", None, array("f", [float("nan")] * dimensions).tobytes()]
    # Legacy nonnormalized vectors use exactly the original dot semantics.
    vectors += [array("f", [2.0] + [0.0] * (dimensions - 1)).tobytes()]
    with store.db() as db:
        db.executemany(
            "INSERT INTO chunks VALUES ('p','s',?,?,0,'text',?)",
            [(str(i), "r" + str(i % 3), blob) for i, blob in enumerate(vectors)],
        )
    for revisions in [["r0", "r1", "r2"], ["r1"], [], ["missing"]]:
        expected = reference(store, revisions, query)
        assert search.nearest(store, "p", "s", revisions, query) == expected
        # Cached and concurrent reads preserve exact order, including ties.
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert all(
                value == expected
                for value in pool.map(
                    lambda _, revisions=revisions: search.nearest(store, "p", "s", revisions, query), range(16)
                )
            )


def test_rebuild_and_other_store_writes_invalidate_snapshot(tmp_path):
    store, search = VectorStore(tmp_path), ExactVectorSearch()
    blob = normalize([1.0] + [0.0] * 63, 64)
    with store.db() as db:
        db.execute("INSERT INTO chunks VALUES ('p','s','one','r',0,'first',?)", (blob,))
    query = decode(blob)
    assert search.nearest(store, "p", "s", ["r"], query)[0][3] == "first"
    other = VectorStore(tmp_path)
    with other.db() as db:
        db.execute("UPDATE chunks SET content='second' WHERE id='one'")
    assert search.nearest(store, "p", "s", ["r"], query)[0][3] == "second"
    with other.db() as db:
        db.execute("DELETE FROM chunks")
    assert search.nearest(store, "p", "s", ["r"], query) == []


def test_memory_bound_does_not_change_results(tmp_path):
    store, search = VectorStore(tmp_path), ExactVectorSearch(max_bytes=1)
    blob = normalize([1.0] + [0.0] * 63, 64)
    with store.db() as db:
        db.execute("INSERT INTO chunks VALUES ('p','s','one','r',0,'first',?)", (blob,))
    assert search.nearest(store, "p", "s", ["r"], decode(blob)) == reference(store, ["r"], decode(blob))
    assert not search.cache
