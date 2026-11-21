"""Read and rank only the vectors already present in a saved library."""

import math
import sys
from array import array
from base64 import b64decode
from dataclasses import dataclass

MAX_DIMENSIONS = 8192
MAX_CHUNKS = 60000


@dataclass(frozen=True)
class LibraryVectors:
    model: str
    dimensions: int
    chunks: list

    @classmethod
    def read(cls, rag, sections):
        if not isinstance(rag, dict) or rag.get("encoding") != "float32-le-base64":
            return None
        model, dimensions, chunks = (rag.get(key) for key in ("model", "dimensions", "chunks"))
        if (
            not isinstance(model, str)
            or not model.strip()
            or len(model) > 200
            or type(dimensions) is not int
            or not 1 <= dimensions <= MAX_DIMENSIONS
            or not isinstance(chunks, list)
            or len(chunks) > MAX_CHUNKS
        ):
            return None
        parsed = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            section_id, vector = chunk.get("section_id"), chunk.get("vector")
            if not isinstance(section_id, str) or section_id not in sections or not isinstance(vector, str):
                continue
            if len(vector) != 4 * ((4 * dimensions + 2) // 3):
                continue
            try:
                raw = b64decode(vector, validate=True)
            except ValueError:
                continue
            if len(raw) != 4 * dimensions:
                continue
            values = array("f")
            values.frombytes(raw)
            if sys.byteorder != "little":
                values.byteswap()
            norm = math.sqrt(math.sumprod(values, values))
            if math.isfinite(norm) and norm > 0:
                parsed.append((section_id, values, 1 / norm))
        return cls(model, dimensions, parsed) if parsed else None

    def info(self):
        return {"model": self.model, "dimensions": self.dimensions, "indexed_chunks": len(self.chunks)}

    def rank(self, vector, limit):
        norm = math.sqrt(math.sumprod(vector, vector))
        if not norm or not math.isfinite(norm):
            return []
        ranked = {}
        for section_id, values, inverse_norm in self.chunks:
            score = math.sumprod(vector, values) * inverse_norm / norm
            if score > ranked.get(section_id, 0):
                ranked[section_id] = score
        return sorted(ranked, key=lambda sid: (-ranked[sid], sid))[:limit]
