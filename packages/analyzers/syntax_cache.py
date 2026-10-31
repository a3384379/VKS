"""Content-addressed syntax units, encoded as bounded JSON data, never pickle.

Syntax and dependency-scoped semantic packets are disposable JSON data. Semantic
reuse additionally binds the complete current dependency closure and file identity.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
from typing import NamedTuple
from xml.etree.ElementTree import Element

from .cancellation import checkpoint

MAX_NODES = 250_000
MAX_PACKET_BYTES = 32 * 1024 * 1024
_ACTIVE = ContextVar("vks_syntax_cache", default=None)


@lru_cache(maxsize=1)
def processor_version():
    digest = sha256()
    for filename in ("java_spring.py", "syntax_cache.py", "async_topology.py", "incremental.py"):
        digest.update(Path(__file__).with_name(filename).read_bytes())
    for package in ("tree-sitter", "tree-sitter-java", "defusedxml"):
        digest.update(version(package).encode())
    return "source-unit-v1:" + digest.hexdigest()[:32]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class MemoryUnitCache:
    def __init__(self):
        self.values = {}

    def load(self, kind, key, processor):
        return self.values.get((kind, key, processor))

    def store(self, kind, key, processor, packet):
        self.values[(kind, key, processor)] = json.loads(canonical(packet))


@contextmanager
def cache_scope(backend):
    token = _ACTIVE.set(backend)
    try:
        yield
        if hasattr(backend, "flush"):
            backend.flush()
    finally:
        _ACTIVE.reset(token)


def active_cache():
    return _ACTIVE.get()


def unit(kind, source, parse, encode, decode):
    backend = _ACTIVE.get()
    if backend is None:
        return parse(), False
    key, processor = sha256(source).hexdigest(), processor_version()
    checkpoint()
    packet = backend.load(kind, key, processor)
    if packet is not None:
        try:
            raw = canonical(packet["payload"])
            if (
                packet["source_sha256"] != key
                or packet["processor"] != processor
                or len(raw) > MAX_PACKET_BYTES
                or sha256(raw).hexdigest() != packet["payload_sha256"]
            ):
                raise ValueError("Invalid syntax unit digest")
            result = decode(packet["payload"], len(source))
            checkpoint()
            return result, True
        except (ValueError, KeyError, TypeError, IndexError, OverflowError, AttributeError, RecursionError):
            pass  # Corrupt cache is a miss; original authorized bytes remain authoritative.
    result = parse()
    try:
        payload = encode(result)
        raw = canonical(payload)
        if len(raw) > MAX_PACKET_BYTES:
            return result, False
    except (ValueError, RecursionError):
        return result, False
    checkpoint()
    backend.store(
        kind,
        key,
        processor,
        dict(source_sha256=key, processor=processor, payload_sha256=sha256(raw).hexdigest(), payload=payload),
    )
    return result, False


class Point(NamedTuple):
    row: int
    column: int


class SyntaxNode:
    def child_by_field_name(self, name):
        return self.fields.get(name)

    @property
    def named_children(self):
        return [child for child in self.children if child.is_named]


def encode_java(root):
    records, pending = [], [(root, None, None)]
    while pending:
        node, parent, field = pending.pop()
        if len(records) >= MAX_NODES:
            raise ValueError("Syntax unit too large")
        index = len(records)
        records.append(
            dict(
                type=node.type,
                span=[node.start_byte, node.end_byte],
                points=[*node.start_point, *node.end_point],
                named=node.is_named,
                error=node.has_error,
                children=[],
                fields={},
            )
        )
        if parent is not None:
            records[parent]["children"].append(index)
            if field:
                records[parent]["fields"].setdefault(field, index)
        children = list(node.children)
        pending.extend((child, index, node.field_name_for_child(i)) for i, child in reversed(list(enumerate(children))))
    return records


def decode_java(records, source_size):
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_NODES:
        raise ValueError("Invalid syntax node count")
    nodes = [SyntaxNode() for _ in records]
    owners = set()
    for index, (record, node) in enumerate(zip(records, nodes)):
        if (
            not isinstance(record["type"], str)
            or type(record["named"]) is not bool
            or type(record["error"]) is not bool
        ):
            raise ValueError("Invalid syntax node")
        start, end = record["span"]
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= source_size:
            raise ValueError("Invalid syntax span")
        if len(record["points"]) != 4 or any(type(n) is not int or n < 0 or n > source_size for n in record["points"]):
            raise ValueError("Invalid syntax location")
        children = record["children"]
        if any(type(i) is not int or not index < i < len(nodes) or i in owners for i in children) or len(
            set(children)
        ) != len(children):
            raise ValueError("Invalid syntax tree")
        owners.update(children)
        if any(not isinstance(k, str) or v not in children for k, v in record["fields"].items()):
            raise ValueError("Invalid syntax fields")
        node.type, node.start_byte, node.end_byte = record["type"], start, end
        node.start_point, node.end_point = Point(*record["points"][:2]), Point(*record["points"][2:])
        node.is_named, node.has_error = record["named"], record["error"]
        node.children = [nodes[i] for i in children]
        node.fields = {k: nodes[i] for k, i in record["fields"].items()}
        if index == 0:
            node.parent = None
        for child in node.children:
            child.parent = node
    if len(owners) != len(nodes) - 1:
        raise ValueError("Disconnected syntax tree")
    return nodes[0]


def encode_xml(value):
    root, spans = value
    records, pending = [], [(root, None)]
    while pending:
        node, parent = pending.pop()
        if len(records) >= MAX_NODES:
            raise ValueError("XML unit too large")
        index = len(records)
        records.append(dict(tag=node.tag, attributes=dict(node.attrib), text=node.text, tail=node.tail, children=[]))
        if parent is not None:
            records[parent]["children"].append(index)
        pending.extend((child, index) for child in reversed(list(node)))
    return dict(nodes=records, spans=[dict(tag=tag, key=key, ranges=ranges) for (tag, key), ranges in spans.items()])


def decode_xml(payload, source_size):
    records = payload["nodes"]
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_NODES:
        raise ValueError("Invalid XML count")
    nodes, owners = [], set()
    for record in records:
        if not isinstance(record["tag"], str) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in record["attributes"].items()
        ):
            raise ValueError("Invalid XML record")
        if any(text is not None and not isinstance(text, str) for text in (record["text"], record["tail"])):
            raise ValueError("Invalid XML text")
        node = Element(record["tag"], record["attributes"])
        node.text, node.tail = record["text"], record["tail"]
        nodes.append(node)
    for index, record in enumerate(records):
        children = record["children"]
        if any(type(i) is not int or not index < i < len(nodes) or i in owners for i in children) or len(
            set(children)
        ) != len(children):
            raise ValueError("Invalid XML hierarchy")
        owners.update(children)
        nodes[index].extend(nodes[i] for i in children)
    if len(owners) != len(nodes) - 1:
        raise ValueError("Disconnected XML tree")
    spans = {}
    for item in payload["spans"]:
        ranges = item["ranges"]
        if (
            not isinstance(item["tag"], str)
            or not isinstance(item["key"], str)
            or any(
                len(pair) != 2
                or any(type(n) is not int for n in pair)
                or not 1 <= pair[0] <= pair[1] <= source_size + 1
                for pair in ranges
            )
        ):
            raise ValueError("Invalid XML location")
        spans[(item["tag"], item["key"])] = [tuple(pair) for pair in ranges]
    return nodes[0], spans
