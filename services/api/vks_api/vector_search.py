"""Permission-scoped native shortlist with exact original scoring and ties."""

import json
import math
import threading
from array import array
from collections import OrderedDict

import numpy as np


def decode(blob):
    result = array("f")
    result.frombytes(blob)
    return result


class ExactVectorSearch:
    def __init__(self, max_bytes=128 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.bytes = 0

    def snapshot(self, store, pid, space, revisions, dimensions):
        # Revisions come from THIS request's authorized corpus, never a cached
        # permission decision. The DB epoch detects rebuilds in other processes.
        scope = tuple(sorted(revisions))
        snapshots = store.batch_reads()
        snapshot_key = ("matrix", pid, space, scope, dimensions)
        if snapshot_key in snapshots:
            return snapshots[snapshot_key]
        with self.lock, store.db() as db:
            db.execute("BEGIN")
            epoch = db.execute("SELECT value FROM chunk_epoch WHERE id=1").fetchone()[0]
            key = (pid, space, scope, dimensions, epoch)
            cached = self.cache.get(key)
            if cached is not None:
                self.cache.move_to_end(key)
                snapshots[snapshot_key] = cached[0]
                return cached[0]
            rows = db.execute(
                "SELECT id,revision,start,content,embedding FROM chunks WHERE project=? AND space=? "
                "AND revision IN (SELECT value FROM json_each(?))",
                (pid, space, json.dumps(scope)),
            ).fetchall()
            matrix = np.zeros((len(rows), dimensions), dtype=np.float64)
            valid = np.zeros(len(rows), dtype=bool)
            for i, row in enumerate(rows):
                blob = row[4]
                if isinstance(blob, bytes) and len(blob) == dimensions * 4:
                    matrix[i] = decode(blob)
                    valid[i] = True
            # Nonstandard vectors must still use the old dot-product semantics;
            # never discard a legacy vector merely because its norm is unusual.
            normal = valid & np.isfinite(matrix).all(axis=1) & (np.linalg.norm(matrix, axis=1) <= 1.001)
            cached = (rows, matrix, valid, normal)
            size = matrix.nbytes + sum(len(r[4]) if isinstance(r[4], bytes) else 0 for r in rows)
            if size <= self.max_bytes:
                while self.cache and self.bytes + size > self.max_bytes:
                    _key, (_snapshot, previous_size) = self.cache.popitem(last=False)
                    self.bytes -= previous_size
                self.cache[key] = (cached, size)
                self.bytes += size
            snapshots[snapshot_key] = cached
            return cached

    def nearest(self, store, pid, space, revisions, query, limit=36, *, snapshot=None):
        cached = snapshot if snapshot is not None else self.snapshot(store, pid, space, revisions, len(query))
        rows, matrix, valid, normal = cached
        if not rows:
            return []
        with np.errstate(invalid="ignore", over="ignore"):
            # einsum is a native C loop without a BLAS thread pool per request.
            scores = np.einsum("ij,j->i", matrix, np.asarray(query, dtype=np.float64), optimize=False)
        normal = normal & (math.isfinite(math.hypot(*query)) and math.hypot(*query) <= 1.001)
        uncertain = ~normal | (scores >= 1.001 - 1e-10) | (scores <= -1.001 + 1e-10)
        scores[uncertain] = -2.0
        # Native summation can differ from sumprod at the last bits. Enlarge the
        # shortlist by a conservative roundoff margin, then recompute ALL close
        # ties and unusual rows with the exact original sumprod implementation.
        threshold = np.partition(scores, max(0, len(rows) - limit))[max(0, len(rows) - limit)]
        indices = np.flatnonzero((scores >= threshold - 1e-10) | uncertain)

        def exact(i):
            if not valid[i]:
                return -2.0
            try:
                score = math.sumprod(decode(rows[i][4]), query)
            except (ValueError, OverflowError):
                return -2.0
            return score if math.isfinite(score) and -1.001 <= score <= 1.001 else -2.0

        result = [(int(i), exact(i)) for i in indices]
        result.sort(key=lambda item: (-item[1], item[0]))
        return [(*rows[i][:4], score) for i, score in result[:limit]]
