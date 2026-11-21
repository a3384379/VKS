"""Offline capture using the Studio runtime and a network-denied browser."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from .runner import CaptureError, isolated_chromium_options

PARENT = "https://vks-preview.invalid"
RUNTIME = "https://vks-preview-runtime.invalid"
ASSETS = Path(__file__).resolve().parents[2] / "apps/runtime"
INSTANCE = "offline-preview"
UNCHANGED = "当前页面或绑定状态无法准确复现，请在 Studio 检查目标"
INIT = """
async ({ origin, instance, target, pageOnly }) => {
    const bytes = await fetch("/material").then(response => response.arrayBuffer());
    const frame = document.createElement("iframe");
    frame.style.cssText = "width:100%;height:100%;border:0;display:block";
    const channel = new MessageChannel();
    const call = (type, payload, request_id = "") => channel.port1.postMessage({
        protocol_version: 1, runtime_instance_id: instance, type, payload, request_id
    });
    const result = new Promise(resolve => {
        const schedule = (message) => {
            if (message.source !== frame.contentWindow || message.origin !== origin ||
                message.data?.type !== "vks.runtime.ready" || message.data?.instance !== instance) return;
            window.removeEventListener("message", schedule);
            frame.contentWindow.postMessage({type:"vks.connect", instance}, origin, [channel.port2]);
        };
        window.addEventListener("message", schedule);
        channel.port1.onmessage = ({data}) => {
            if (data.runtime_instance_id !== instance || data.protocol_version !== 1) return;
            if (data.type === "runtime.ready") {
                channel.port1.postMessage({
                    protocol_version: 1, runtime_instance_id: instance, type: "material.load",
                    payload: {bytes}
                }, [bytes]);
            } else if (data.type === "material.loaded") {
                call("target.reveal", {target, page_only: pageOnly, highlight: false}, "preview");
            } else if (data.type === "command.result" && data.request_id === "preview") {
                resolve(data.payload);
            }
        };
    });
    window.observeVksPreview = () => new Promise(resolve => {
        channel.port1.onmessage = ({data}) => {
            if (data.runtime_instance_id === instance && data.protocol_version === 1 &&
                data.type === "command.result" && data.request_id === "observe") resolve(data.payload);
        };
        call("target.observe", {target, page_only: pageOnly}, "observe");
    });
    frame.src = origin + "/?instance=" + instance;
    document.body.append(frame);
    return result;
}
"""


def _matches(result, target, page_only):
    if (
        not isinstance(result, dict)
        or not result.get("ok")
        or (result.get("route"), result.get("frame_path")) != (target["route"], target["frame_path"])
    ):
        return False
    if page_only:
        return result.get("navigation_level") == "page"
    actual = result.get("target") or {}
    return (
        result.get("navigation_level") in ("control", "state")
        and (not target["state"] or result.get("state_verified") is True)
        and result.get("observed_state") == target["state"]
        and (actual.get("selector"), actual.get("label")) == (target["selector"], target["label"])
    )


def _observed_position(result, target, page_only):
    if not _matches(result, target, page_only) or result.get("state_complete") is not True:
        return None
    document_id = result.get("document_id")
    state = result.get("observed_state")
    if type(document_id) is int and document_id > 0 and isinstance(state, str):
        return document_id, state
    return None


async def _render(raw, target, page_only, viewport):
    from apps.runtime.server import POLICY

    config = json.dumps({"host": PARENT, "mode": "prototype"})
    runtime = (
        (ASSETS / "index.html")
        .read_text(encoding="utf-8")
        .replace(" allow-downloads", "")
        .replace(
            "<!--RUNTIME_CONFIG-->", '<script id="vks-runtime-config" type="application/json">' + config + "</script>"
        )
    )
    bridge = (ASSETS / "bridge.js").read_bytes()
    with TemporaryDirectory(prefix="vks-preview-profile-") as profile:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**isolated_chromium_options(profile), timeout=12_000)
            try:
                context = await browser.new_context(
                    viewport=viewport,
                    device_scale_factor=1,
                    service_workers="block",
                    accept_downloads=False,
                )
                page = await context.new_page()

                async def route(intercept):
                    request = intercept.request
                    url = request.url
                    # All successful responses are fixed, in-memory resources
                    # in this isolated context. No request reaches a network.
                    if request.method == "GET":
                        if url in (PARENT + "/", PARENT + "/material") and request.frame == page.main_frame:
                            if url.endswith("/material"):
                                await intercept.fulfill(body=raw, content_type="application/octet-stream")
                            else:
                                await intercept.fulfill(
                                    content_type="text/html",
                                    body='<!doctype html><body style="margin:0;width:100vw;height:100vh"></body>',
                                    headers={
                                        "Content-Security-Policy": f"default-src 'none'; frame-src {RUNTIME}; connect-src 'self'; style-src 'unsafe-inline'"
                                    },
                                )
                            return
                        if url in (f"{RUNTIME}/?instance={INSTANCE}", RUNTIME + "/bridge.js"):
                            await intercept.fulfill(
                                body=bridge if url.endswith(".js") else runtime,
                                content_type="text/javascript" if url.endswith(".js") else "text/html; charset=utf-8",
                                headers={"Content-Security-Policy": POLICY, "Cache-Control": "no-store"},
                            )
                            return
                    await intercept.abort("blockedbyclient")

                await context.route("**/*", route)
                await context.route_web_socket("**/*", lambda socket: socket.close(code=1008))
                await page.goto(PARENT + "/", wait_until="domcontentloaded", timeout=5000)
                result = await page.evaluate(
                    INIT,
                    {
                        "origin": RUNTIME,
                        "instance": INSTANCE,
                        "target": target,
                        "pageOnly": page_only,
                    },
                )
                if not _matches(result, target, page_only):
                    raise CaptureError("VKS_TARGET_UNAVAILABLE", UNCHANGED)
                # Both observations are passive. A second reveal could repair
                # a changed page and incorrectly certify the earlier pixels.
                before = await page.evaluate("window.observeVksPreview()")
                position = _observed_position(before, target, page_only)
                if position is None:
                    raise CaptureError("VKS_TARGET_UNAVAILABLE", UNCHANGED)
                image = await page.screenshot(type="png", timeout=4000)
                after = await page.evaluate("window.observeVksPreview()")
                if _observed_position(after, target, page_only) != position:
                    raise CaptureError("VKS_TARGET_UNAVAILABLE", UNCHANGED)
                return image
            finally:
                await browser.close()


async def _bounded(raw, target, page_only, viewport, cancelled):
    task = asyncio.create_task(_render(raw, target, page_only, viewport))
    try:
        async with asyncio.timeout(35):
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.4)
                if cancelled():
                    raise CaptureError("VKS_CANCELLED", "预览生成已取消")
                if done:
                    return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def render_preview(raw, page, binding, viewport, cancelled):
    if cancelled():
        raise CaptureError("VKS_CANCELLED", "预览生成已取消")
    target = binding["target"] if binding else {"route": page["route"], "frame_path": page["frame_path"]}
    try:
        return asyncio.run(_bounded(raw, target, binding is None, viewport, cancelled))
    except (BrowserError, OSError):
        raise CaptureError("VKS_DEPENDENCY_UNAVAILABLE", "隔离 Chromium 无法启动或加载预览") from None
    except TimeoutError:
        raise CaptureError("VKS_TARGET_UNAVAILABLE", "隔离原型未在时限内到达指定页面或绑定状态") from None
