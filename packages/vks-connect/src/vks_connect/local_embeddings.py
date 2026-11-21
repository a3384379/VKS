"""Pinned CPU embeddings for shared, offline library indexes."""

import base64
import faulthandler
import hashlib
import os
import sys
from functools import cache
from pathlib import Path

from .library_vectors import MAX_CHUNKS

MODEL = "BAAI/bge-small-zh-v1.5"
DIMENSIONS = 512
PROFILE = "bge-small-zh-v1.5-q8-cls-256-v1"
ASSET_SOURCE = "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/75c43b069aac4d136ba6bc1122f995fedcfd2781/"
ASSETS = {
    "model.onnx": ("onnx/model_quantized.onnx", 24010842,
                   "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc"),
    "tokenizer.json": ("tokenizer.json", 439125,
                       "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26"),
}
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
TOKEN_LIMIT = 256


class LocalModelError(ValueError):
    pass


def model_directory():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent / "models" / PROFILE
    configured = os.getenv("VKS_CONNECT_LOCAL_MODEL_DIR")
    return Path(configured).expanduser() if configured else None


def verified_asset(path, name):
    _, size, checksum = ASSETS[name]
    with (path / name).open("rb") as source:
        data = source.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != checksum:
        raise LocalModelError("本地向量模型资源校验失败。")
    return data


class LocalEncoder:
    def __init__(self, path):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer

            self.np = np
            self.tokenizer = Tokenizer.from_str(verified_asset(path, "tokenizer.json").decode())
            self.start = self.tokenizer.token_to_id("[CLS]")
            self.end = self.tokenizer.token_to_id("[SEP]")
            self.pad = self.tokenizer.token_to_id("[PAD]")
            options = ort.SessionOptions()
            options.intra_op_num_threads = min(4, os.cpu_count() or 1)
            options.inter_op_num_threads = 1
            self.session = ort.InferenceSession(
                verified_asset(path, "model.onnx"), options, providers=["CPUExecutionProvider"]
            )
            self.inputs = {x.name for x in self.session.get_inputs()}
        except Exception as exc:
            raise LocalModelError("本地向量模型无法加载。请检查插件安装包或运行环境。") from exc

    def tokens(self, text):
        return self.tokenizer.encode(text, add_special_tokens=False).ids

    def passage_chunks(self, title, content):
        heading = self.tokens(title)[:48]
        body = self.tokens(content)
        capacity = TOKEN_LIMIT - len(heading) - 3
        start = 0
        while start + capacity < len(body):
            yield [self.start, *heading, self.end, *body[start : start + capacity], self.end]
            start += capacity - 40
        yield [self.start, *heading, self.end, *body[start:], self.end]

    def run(self, batches):
        np = self.np
        width = max(map(len, batches))
        ids = np.full((len(batches), width), self.pad, dtype=np.int64)
        masks = np.zeros_like(ids)
        for i, tokens in enumerate(batches):
            ids[i, :len(tokens)] = tokens
            masks[i, :len(tokens)] = 1
        inputs = {"input_ids": ids, "attention_mask": masks}
        if "token_type_ids" in self.inputs:
            inputs["token_type_ids"] = np.zeros_like(ids)
        try:
            embeddings = self.session.run(["last_hidden_state"], inputs)[0][:, 0, :]
        except Exception as exc:
            raise LocalModelError("本地向量模型推理失败。") from exc
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        if embeddings.shape != (len(batches), DIMENSIONS) or not np.all(np.isfinite(norms) & (norms > 0)):
            raise LocalModelError("本地模型返回了无效的向量。")
        return embeddings / norms

    def query(self, text):
        query = self.tokens(QUERY_PREFIX + text)[:TOKEN_LIMIT - 2]
        return self.run([[self.start, *query, self.end]])[0].tolist()


@cache
def encoder(path):
    return LocalEncoder(path)


def local_query(text):
    path = model_directory()
    if path is None:
        raise LocalModelError("插件未安装本地向量模型。")
    trace_smoke = os.getenv("VKS_CONNECT_SMOKE_TRACE") == "1"
    if trace_smoke:
        faulthandler.dump_traceback_later(20)
    try:
        return encoder(path).query(text)
    finally:
        if trace_smoke:
            faulthandler.cancel_dump_traceback_later()


def build_local_index(documents, model, *, checkpoint=None, progress=None):
    """Index the exported chapters, using the same model profile as the plugin."""
    passages = []
    for document in documents:
        if checkpoint:
            checkpoint()
        for section in document["sections"]:
            title = f"{section['title']}。{document['title']}"
            for tokens in model.passage_chunks(title, section["content"]):
                passages.append((section["id"], tokens))
                if len(passages) > MAX_CHUNKS:
                    raise LocalModelError("本地向量分片超出成品库限制。")
    passages.sort(key=lambda row: len(row[1]))
    chunks = []
    for offset in range(0, len(passages), 32):
        if checkpoint:
            checkpoint()
        batch = passages[offset : offset + 32]
        vectors = model.run([tokens for _, tokens in batch])
        for (sid, _), vector in zip(batch, vectors, strict=True):
            chunks.append({"section_id": sid, "vector": base64.b64encode(vector.astype("<f4").tobytes()).decode()})
        if progress:
            progress(len(chunks), len(passages))
    return {
        "profile": PROFILE, "model": MODEL, "dimensions": DIMENSIONS, "encoding": "float32-le-base64", "chunks": chunks,
    }
