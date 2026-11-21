"""Bounded coalescing of already-authorized control explanations.

Each caller retains its normal retrieval, accounting and commit fences. A batch
only shares transport: every target keeps the original full answer schema and
candidate snippets. No authorization decision is cached here.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field

from .ai_gateway import Draft202012Validator, ModelNotSent
from .service import DomainError, canonical
from .token_budget import batch_fits, request_input_tokens

CURRENT_BATCH = ContextVar("vks_association_batch", default=None)
BATCH_INSTRUCTIONS = """\n本次同时处理多个独立控件。targets 中每个 task_id 必须分别返回完整讲解，不合并同名控件，不缩短或省略任何一个控件的讲解。chapter_pool 只是原文章节片段的去重存储；每个 target 的 chapter_keys 指定它自己的全部候选原文。每个控件只能根据自己的候选片段回答并引用对应的 revision_id，不能借用其他控件的候选或业务结论。每个答案独立遵守前述用途、前提、结果、注意事项、置信度与资料缺口要求。保持与逐项回答相同的信息粒度和简明程度，不为合批增加重复背景或无依据细节。返回 answers 对象，各属性名准确对应 task_id。"""


@dataclass
class Pending:
    config: dict
    body: dict
    preflight: object
    candidates: tuple[str, ...]
    done: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None
    error: BaseException | None = None


def messages(body):
    return body.get("input", body.get("messages"))


def answer_schema(body):
    return body["text"]["format"]["schema"] if "input" in body else body["response_format"]["json_schema"]["schema"]


def decoded(response, protocol):
    if protocol == "responses":
        if response.get("status") not in (None, "completed"):
            raise ValueError("incomplete output")
        raw = "".join(
            c["text"]
            for item in response.get("output", [])
            if item.get("type") == "message"
            for c in item.get("content", [])
            if c.get("type") == "output_text"
        )
    else:
        choice = response["choices"][0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise ValueError("incomplete output")
        raw = choice["message"]["content"]
    return json.loads(raw)


def usage(response):
    data = response.get("usage", {})
    result = (
        data.get("input_tokens", data.get("prompt_tokens")),
        data.get("output_tokens", data.get("completion_tokens")),
    )
    if any(type(v) is not int or not 0 <= v <= 1000000000 for v in result):
        raise ValueError("invalid model usage")
    return result


class BatchResponse(dict):
    """Trusted transport metadata cannot be supplied by a provider JSON object."""

    def __init__(self, value, inputs):
        super().__init__(value)
        self.input_revision_ids = tuple(inputs)


def response_for(answer, protocol, tokens, inputs):
    raw = json.dumps(answer, ensure_ascii=False)
    result = {"usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]}}
    if protocol == "responses":
        result.update(
            status="completed", output=[{"type": "message", "content": [{"type": "output_text", "text": raw}]}]
        )
    else:
        result["choices"] = [{"finish_reason": "stop", "message": {"content": raw}}]
    return BatchResponse(result, inputs)


class AssociationBatch:
    def __init__(
        self,
        actor,
        pid,
        request,
        *,
        max_items=8,
        concurrency=3,
        gather_seconds=0.35,
        max_input_bytes=None,
        participants=None,
    ):
        self.actor, self.pid, self.request_model = actor, pid, request
        self.max_items = max(1, min(12, max_items))
        self.gather_seconds = gather_seconds
        # A batch may explicitly aggregate existing per-target input allowances.
        # Individual preparation still enforces the original policy bound.
        self.max_input_bytes = max_input_bytes
        self.capacity = threading.BoundedSemaphore(max(1, concurrency))
        self.lock = threading.Lock()
        self.waiting = defaultdict(list)
        self.unsupported = set()
        # Internal page cohorts know exactly how many operation groups run.
        # Their workers finish in a finally block, including manual/skipped or
        # failed groups which never request a model. Preparation skew must not
        # split a ready cohort into an extra wave of remote requests.
        self.participants = participants
        self.arrived = 0
        self.skipped = 0
        self.local = threading.local()

    def _ready(self, key=None):
        """Take waiting requests under self.lock; never send while holding it."""
        if self.participants is not None and self.arrived + self.skipped >= self.participants:
            groups = list(self.waiting.items())
            self.waiting.clear()
            return groups
        if key is not None and (len(self.waiting[key]) >= self.max_items or key in self.unsupported):
            return [(key, self.waiting.pop(key))]
        return []

    def finish_participant(self):
        """Finish one cohort worker, even when it needed no model request."""
        if self.participants is None:
            return
        with self.lock:
            if not getattr(self.local, "requested", False):
                self.skipped += 1
            # A pool may reuse this thread for another skipped operation group.
            self.local.requested = False
            groups = self._ready()
        for key, group in groups:
            self._dispatch(group, key)

    def request(self, config, body, *, preflight, candidates, page):
        # Different actors, projects, pages, model settings or system contracts
        # never share a request, even if worker contexts accidentally overlap.
        key = canonical([config, messages(body)[0], page])
        pending = Pending(config, body, preflight, tuple(candidates))
        with self.lock:
            if self.participants is not None and not getattr(self.local, "requested", False):
                self.arrived += 1
                self.local.requested = True
            self.waiting[key].append(pending)
            groups = self._ready(key)
        for ready_key, group in groups:
            self._dispatch(group, ready_key)
        if not groups and self.participants is None and not pending.done.wait(self.gather_seconds):
            with self.lock:
                group = self.waiting.pop(key, None)
            if group:
                self._dispatch(group, key)
        pending.done.wait()
        if pending.error:
            raise pending.error
        return pending.response

    def _body(self, group):
        config = group[0].config
        pool, chapter_keys, targets = {}, {}, []
        properties = {}
        for index, pending in enumerate(group):
            task = "t" + str(index)
            data = json.loads(messages(pending.body)[1]["content"])
            keys = []
            for chapter in data["chapters"]:
                identity = canonical(chapter)
                if identity not in chapter_keys:
                    chapter_keys[identity] = "c" + str(len(pool))
                    pool[chapter_keys[identity]] = chapter
                keys.append(chapter_keys[identity])
            targets.append(
                {"task_id": task, **{k: v for k, v in data.items() if k != "chapters"}, "chapter_keys": keys}
            )
            properties[task] = deepcopy(answer_schema(pending.body))
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["answers"],
            "properties": {
                "answers": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(properties),
                    "properties": properties,
                }
            },
        }
        prompt = [
            {"role": "system", "content": messages(group[0].body)[0]["content"] + BATCH_INSTRUCTIONS},
            {"role": "user", "content": json.dumps({"targets": targets, "chapter_pool": pool}, ensure_ascii=False)},
        ]
        body = {"model": config["model_id"]}
        limit = sum(p.config["max_output_tokens"] for p in group)
        if config["protocol"] == "responses":
            body.update(
                input=prompt,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "control_assistance_batch",
                        "strict": True,
                        "schema": schema,
                    }
                },
                max_output_tokens=limit,
                store=False,
            )
        else:
            body.update(
                messages=prompt,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "control_assistance_batch", "strict": True, "schema": schema},
                },
                max_completion_tokens=limit,
            )
        return body

    def _input_bound(self, group):
        original = group[0].config["max_input_tokens"]
        if self.max_input_bytes is None:
            return original
        return min(128000, self.max_input_bytes, sum(p.config["max_input_tokens"] for p in group))

    def _dispatch(self, group, key):
        # Split without dropping any target's candidate text or output allowance.
        # Aggregation beyond one request's bound requires an explicit batch cap.
        remaining = list(group)
        try:
            while remaining:
                selected = remaining[: self.max_items]
                while len(selected) > 1 and not batch_fits(
                    selected[0].config, self._body(selected), self._input_bound(selected)
                ):
                    selected.pop()
                remaining = remaining[len(selected) :]
                self._send(selected, key)
        except BaseException as exc:
            # A malformed local payload must not strand sibling workers waiting
            # for a batch which was never transmitted.
            for pending in group:
                if not pending.done.is_set():
                    pending.error = exc
                    pending.done.set()

    def _send(self, group, key):
        valid = []
        try:
            with self.capacity:
                # Waiting for another request slot can outlive a cancellation or
                # revocation. Validate after acquiring the slot, before sending.
                for pending in group:
                    try:
                        pending.preflight()
                        valid.append(pending)
                    except DomainError as exc:
                        pending.error = ModelNotSent(
                            exc.code, exc.message, exc.status, details=exc.details, retryable=False
                        )
                        pending.done.set()
                    except BaseException as exc:
                        pending.error = exc
                        pending.done.set()
                if not valid:
                    return
                if len(valid) == 1 or key in self.unsupported:
                    for pending in valid:
                        pending.response = BatchResponse(
                            self.request_model(
                                {**pending.config, "_association_preflight": pending.preflight}, pending.body
                            ), pending.candidates
                        )
                        pending.done.set()
                    return
                config = valid[0].config
                combined = self._body(valid)
                output_cap = combined.get("max_output_tokens", combined.get("max_completion_tokens", 0))
                def revalidate():
                    for pending in valid:
                        pending.preflight()

                response = self.request_model(
                    {**config, "_association_preflight": revalidate,
                     "_request_timeout_seconds": 900 if output_cap > 16000 else min(120, 30 * len(valid))}, combined
                )
                tokens = usage(response)
                all_inputs = tuple(dict.fromkeys(rid for p in valid for rid in p.candidates))
                try:
                    answers = decoded(response, config["protocol"]).get("answers", {})
                except (ValueError, KeyError, TypeError, AttributeError):
                    answers = {}
                allocations = [
                    (
                        tokens[0] // len(valid) + (index < tokens[0] % len(valid)),
                        tokens[1] // len(valid) + (index < tokens[1] % len(valid)),
                    )
                    for index in range(len(valid))
                ]
                failed = []
                for index, pending in enumerate(valid):
                    answer = answers.get("t" + str(index)) if isinstance(answers, dict) else None
                    try:
                        Draft202012Validator(answer_schema(pending.body)).validate(answer)
                    except Exception:
                        failed.append((pending, allocations[index]))
                        continue
                    pending.response = response_for(answer, config["protocol"], allocations[index], all_inputs)
                    pending.done.set()
                if len(failed) == len(valid):
                    with self.lock:
                        self.unsupported.add(key)
                # Only completed, classified structured-output failures are
                # retried individually. An uncertain transport is never replayed.
                for pending, spent in failed:
                    attempted = False
                    try:
                        pending.preflight()
                        attempted = True
                        single = self.request_model(
                            {**pending.config, "_association_preflight": pending.preflight}, pending.body
                        )
                        extra = usage(single)
                        single["usage"] = {"input_tokens": spent[0] + extra[0], "output_tokens": spent[1] + extra[1]}
                        # The first response may have influenced this derivation;
                        # retain the conservative union even after a fallback.
                        pending.response = BatchResponse(single, all_inputs)
                    except BaseException as exc:
                        # A failed fallback still owes its share of the first
                        # completed batch. If the fallback may have been sent,
                        # charge its original conservative reservation as well.
                        error = DomainError(
                            exc.code if isinstance(exc, DomainError) else "VKS_DEPENDENCY_UNAVAILABLE",
                            exc.message if isinstance(exc, DomainError) else "模型连接或响应格式错误",
                            exc.status if isinstance(exc, DomainError) else 503,
                        )
                        bound = request_input_tokens(pending.body)
                        extra = (
                            (bound, pending.config["max_output_tokens"])
                            if attempted and not isinstance(exc, ModelNotSent)
                            else (0, 0)
                        )
                        error._vks_accounted_usage = (spent[0] + extra[0], spent[1] + extra[1])
                        pending.error = error
                    finally:
                        pending.done.set()
        except BaseException as exc:
            for pending in valid:
                if not pending.done.is_set():
                    pending.error = exc
                    pending.done.set()
