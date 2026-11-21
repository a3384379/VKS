"""Opted-in query embeddings for the local library. Documents stay local."""

import json
import math
from dataclasses import asdict, dataclass

import anyio
import httpx

from .config import save_private_json, validate_url
from .credentials import CredentialError, credential_store, load_token, token_reference, validate_token
from .library_vectors import MAX_DIMENSIONS

MAX_RESPONSE = 512 * 1024
DEFAULT_ENDPOINT = "https://api.openai.com/v1"


@dataclass(frozen=True)
class EmbeddingSettings:
    endpoint: str
    model: str
    dimensions: int

    def __post_init__(self):
        object.__setattr__(self, "endpoint", validate_url(self.endpoint, "嵌入服务"))
        if (
            not isinstance(self.model, str)
            or not self.model
            or len(self.model) > 200
            or not 1 <= self.dimensions <= MAX_DIMENSIONS
            or type(self.dimensions) is not int
        ):
            raise ValueError("嵌入模型或维度无效。")

    def matches(self, index):
        return (self.model, self.dimensions) == (index.model, index.dimensions)


class LibraryEmbeddings:
    def __init__(self, path, transport=None):
        self.path = path
        self.transport = transport

    def settings(self):
        try:
            with self.path.open(encoding="utf-8") as stream:
                return EmbeddingSettings(**json.loads(stream.read(8193)))
        except (OSError, TypeError, ValueError):
            return None

    def configure(self, settings, key=None):
        if key is not None:
            store = credential_store()
            store.set(token_reference(self.path, "embedding", settings.endpoint), validate_token(key))
        save_private_json(self.path, asdict(settings))

    def reset_key(self):
        settings = self.settings()
        if settings is not None:
            credential_store().remove(token_reference(self.path, "embedding", settings.endpoint))

    async def query(self, index, text):
        if index is None:
            return None, "no_vector_index"
        settings = self.settings()
        if settings is None:
            return None, "embedding_not_configured"
        if not settings.matches(index):
            return None, "embedding_model_mismatch"
        try:
            key = await anyio.to_thread.run_sync(load_token, self.path, "embedding", settings.endpoint)
        except (CredentialError, OSError):
            return None, "embedding_key_missing"
        try:
            with anyio.fail_after(6):
                async with (
                    httpx.AsyncClient(follow_redirects=False, trust_env=False, transport=self.transport) as client,
                    client.stream(
                        "POST",
                        settings.endpoint + "/embeddings",
                        json={"model": settings.model, "dimensions": settings.dimensions, "input": text},
                        headers={"Authorization": f"Bearer {key}", "Accept-Encoding": "identity"},
                    ) as response,
                ):
                    if response.status_code != 200:
                        return None, "embedding_unavailable"
                    raw = bytearray()
                    async for block in response.aiter_bytes(chunk_size=64 * 1024):
                        if len(raw) + len(block) > MAX_RESPONSE:
                            return None, "embedding_response_invalid"
                        raw.extend(block)
            data = json.loads(raw)
            if data.get("model", settings.model) != settings.model or len(data["data"]) != 1:
                return None, "embedding_response_invalid"
            vector = data["data"][0]["embedding"]
            if (
                not isinstance(vector, list)
                or len(vector) != settings.dimensions
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
            ):
                return None, "embedding_response_invalid"
            norm_sq = math.sumprod(vector, vector)
            if not math.isfinite(norm_sq) or norm_sq <= 0:
                return None, "embedding_response_invalid"
            return vector, None
        except (httpx.HTTPError, TimeoutError):
            return None, "embedding_unavailable"
        except (AttributeError, ValueError, TypeError, KeyError, IndexError, OverflowError):
            return None, "embedding_response_invalid"
