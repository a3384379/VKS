"""Conversation SSE: incremental prose, then a validated answer/tool receipt."""

import asyncio
import json
import queue
import threading
import time
from contextvars import copy_context

from fastapi.responses import StreamingResponse

from .model_stream import CURRENT_MODEL_STREAM, ModelStream
from .service import DomainError


def conversation_response(request, tools, actor, pid, form, key):
    from .conversation_tools import ConversationOutput, intent

    pending = queue.Queue(maxsize=64)
    context = copy_context()

    def emit(kind, data):
        while not state.cancelled.is_set():
            try:
                pending.put((kind, data), timeout=0.1)
                return
            except queue.Full:
                pass
        state.check()

    state = ModelStream(emit)
    operation = intent(form.question)

    def work():
        token = CURRENT_MODEL_STREAM.set(state) if not operation else None
        try:
            state.check()
            answer = tools.answer(actor, pid, form, key)
            emit("complete", ConversationOutput.model_validate(answer).model_dump())
        except DomainError as exc:
            if not state.cancelled.is_set():
                emit("error", {"code": exc.code, "message": exc.message})
        except Exception:
            if not state.cancelled.is_set():
                emit("error", {"code": "VKS_DEPENDENCY_UNAVAILABLE", "message": "本次回答未完成，请重试"})
        finally:
            if token is not None:
                CURRENT_MODEL_STREAM.reset(token)

    async def events():
        yield (
            "event: status\ndata: "
            + json.dumps(
                {"message": "正在执行请求并保存结果…" if operation else "正在检索文档并回答…"}, ensure_ascii=False
            )
            + "\n\n"
        )
        threading.Thread(target=lambda: context.run(work), name="vks-chat-stream", daemon=True).start()
        heartbeat = time.monotonic()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    kind, data = await asyncio.to_thread(pending.get, True, 0.2)
                except queue.Empty:
                    if time.monotonic() - heartbeat > 10:
                        yield ": keep-alive\n\n"
                        heartbeat = time.monotonic()
                    continue
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if kind in {"complete", "error"}:
                    break
        finally:
            state.cancel()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
