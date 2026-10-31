from __future__ import annotations

import os
import time
from tempfile import TemporaryDirectory
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit


from packages.scenario_dsl import validate_dsl

from .network import NetworkDenied, NetworkPolicy, origin


class CaptureError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass
class CaptureOutput:
    snapshots: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)


def locator(page, target):
    exact = target.get("exact", True)
    if target["by"] == "test_id":
        return page.get_by_test_id(target["value"])
    if target["by"] == "label":
        return page.get_by_label(target["value"], exact=exact)
    if target["by"] == "text":
        return page.get_by_text(target["value"], exact=exact)
    if target["by"] == "role":
        return page.get_by_role(target["role"], name=target["name"], exact=exact)
    return page.locator(target["value"])


def fixture_value(value, dataset):
    if "literal" in value:
        return value["literal"]
    current = dataset
    for part in value["fixture_field"].split("."):
        if not isinstance(current, dict) or part not in current:
            raise CaptureError("VKS_INVALID_INPUT", "数据集字段不存在")
        current = current[part]
    if not isinstance(current, (str, int, float, bool)):
        raise CaptureError("VKS_INVALID_INPUT", "数据集字段必须是标量")
    return str(current)


def run_capture(dsl, environment, dataset, *, cancelled=lambda: False, executable_path=None, auth=None):
    validate_dsl(dsl)
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import expect, sync_playwright

    policy = NetworkPolicy(
        environment["base_origin"],
        environment["allowed_origins"],
        environment["allowed_cidrs"],
        allow_loopback=environment.get("allow_loopback", False),
        write_paths=environment.get("allowed_write_paths", []),
        request_headers=environment.get("allowed_request_headers", {}),
    )
    if environment.get("auth_ref") and auth is None:
        raise CaptureError("VKS_STATE_CONFLICT", "测试认证引用未由服务器解析")
    if auth is not None and auth.origin != policy.base_origin:
        raise CaptureError("VKS_STATE_CONFLICT", "测试认证引用来源不匹配")

    viewport = environment.get("viewport", {"width": 1440, "height": 900})
    if (
        not isinstance(viewport, dict)
        or set(viewport) != {"width", "height"}
        or not 320 <= viewport["width"] <= 3840
        or not 240 <= viewport["height"] <= 2160
    ):
        raise CaptureError("VKS_INVALID_INPUT", "视口超出受控范围")
    result = CaptureOutput()
    start = time.monotonic()
    allow_write = dsl["risk_level"] == "test_write" and environment["kind"] in ("mock", "test")
    with TemporaryDirectory(prefix="vks-browser-") as browser_home, sync_playwright() as driver:
        # Page code runs in a browser with an isolated profile and receives no
        # database, broker, model, provider, or application credentials in env.
        browser_env = {
            key: os.environ[key]
            for key in ("PATH", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "SystemRoot", "WINDIR")
            if key in os.environ
        }
        browser_env.update(HOME=browser_home, XDG_CONFIG_HOME=browser_home, XDG_CACHE_HOME=browser_home)
        browser = driver.chromium.launch(
            headless=True,
            executable_path=executable_path or os.getenv("VKS_CHROMIUM_EXECUTABLE") or None,
            chromium_sandbox=True,
            env=browser_env,
            args=[
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--proxy-server=http://127.0.0.1:9",
                "--proxy-bypass-list=<-loopback>",
                "--dns-prefetch-disable",
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ],
        )
        context = browser.new_context(
            service_workers="block",
            accept_downloads=False,
            viewport=viewport,
            storage_state=auth.storage_state if auth else None,
        )
        active_step = {"key": "", "risk": "read_only", "action_window": None}
        budget = {"requests": 0, "response_bytes": 0, "capture_bytes": 0, "exceeded": False}

        def route(request_route):
            request = request_route.request
            # Playwright calls such as context.cookies() can dispatch another
            # route callback. Freeze this request's identity and authorization
            # window before any call that may yield to another callback.
            budget["requests"] += 1
            request_id = "request-" + str(budget["requests"])
            observed_step_key = active_step["key"]
            step_key = active_step["action_window"] or observed_step_key
            request_can_write = allow_write and active_step["risk"] == "test_write"
            request_started = time.monotonic()
            try:
                if cancelled():
                    raise NetworkDenied("任务已取消")
                if budget["requests"] > 200 or budget["response_bytes"] >= 50 * 1024 * 1024:
                    budget["exceeded"] = True
                    raise NetworkDenied("网络预算已达上限")
                remaining = dsl["timeout_ms"] / 1000 - (time.monotonic() - start)
                if remaining <= 0:
                    budget["exceeded"] = True
                    raise NetworkDenied("场景网络时间预算已耗尽")
                trusted_headers = {}
                if auth is not None and origin(request.url) == auth.origin:
                    trusted_headers.update(auth.headers)
                    cookies = context.cookies([request.url])
                    if cookies:
                        trusted_headers["Cookie"] = "; ".join(
                            cookie["name"] + "=" + cookie["value"] for cookie in cookies
                        )
                if cancelled():
                    raise NetworkDenied("请求发送前授权已失效")
                status, headers, body = policy.fetch(
                    request.url,
                    request.method,
                    request.post_data_buffer,
                    timeout=min(10, remaining),
                    allow_write=request_can_write,
                    headers=request.headers,
                    trusted_headers=trusted_headers,
                )
                if auth is not None and origin(request.url) == auth.origin:
                    from .auth_registry import rotated_cookies, AuthRegistryError

                    try:
                        cookies = rotated_cookies(getattr(headers, "set_cookies", []), request.url)
                    except AuthRegistryError:
                        raise NetworkDenied("认证响应Cookie策略不符") from None
                    if cookies:
                        if len(context.cookies()) + len(cookies) > 100:
                            raise NetworkDenied("隔离会话Cookie数量超过限制")
                        context.add_cookies(cookies)
                budget["response_bytes"] += len(body)
                result.requests.append(
                    {
                        "request_id": request_id,
                        "step_key": step_key,
                        "observed_step_key": observed_step_key,
                        "started_ms": round((request_started - start) * 1000),
                        "duration_ms": round((time.monotonic() - request_started) * 1000),
                        "method": request.method,
                        "origin": origin(request.url),
                        "path": urlsplit(request.url).path[:2048],
                        "path_truncated": len(urlsplit(request.url).path) > 2048,
                        "status": status,
                        "body_saved": False,
                    }
                )
                request_route.fulfill(status=status, headers=headers, body=body)
            except (NetworkDenied, OSError, ValueError):
                if len(result.blocked) < 200:
                    result.blocked.append(
                        {
                            "request_id": request_id,
                            "step_key": step_key,
                            "observed_step_key": observed_step_key,
                            "reason": "NETWORK_POLICY",
                            "method": request.method,
                        }
                    )
                request_route.abort("blockedbyclient")

        context.route("**/*", route)
        context.route_web_socket("**/*", lambda ws: ws.close(code=1008, reason="VKS_WEBSOCKET_BLOCKED"))
        page = context.new_page()
        page.on("popup", lambda popup: popup.close())
        page.on("dialog", lambda dialog: dialog.dismiss())
        try:
            for step in dsl["steps"]:
                if cancelled():
                    raise CaptureError("VKS_CANCELLED", "采集已取消，迟到结果不会保存")
                if budget["exceeded"]:
                    raise CaptureError("VKS_LIMIT_EXCEEDED", "采集超过网络资源预算")
                remaining = dsl["timeout_ms"] / 1000 - (time.monotonic() - start)
                if remaining <= 0:
                    raise CaptureError("VKS_LIMIT_EXCEEDED", "场景执行超过时间预算")
                step_started = time.monotonic()
                active_step["key"] = step["step_key"]
                active_step["risk"] = step["risk"]
                timeout = min(step["timeout_ms"], int(remaining * 1000))
                page.set_default_timeout(timeout)
                kind = step["kind"]
                if kind in ("goto", "click", "fill", "select"):
                    active_step["action_window"] = step["step_key"] if step.get("action_entity_id") else None
                if kind == "goto":
                    target = urljoin(environment["base_origin"], step["path"])
                    policy.resolve(target)
                    page.goto(target, timeout=timeout, wait_until=step.get("wait_until", "domcontentloaded"))
                elif kind == "snapshot":
                    if len(result.snapshots) >= 20:
                        raise CaptureError("VKS_LIMIT_EXCEEDED", "单次采集最多保存20个状态快照")
                    # Capture the actual page state. Passive rendering sanitizes HTML separately.
                    dom = page.content().encode()
                    png = page.screenshot(full_page=step.get("full_page", True), timeout=timeout)
                    budget["capture_bytes"] += len(dom) + len(png)
                    if (
                        len(dom) > 2 * 1024 * 1024
                        or len(png) > 20 * 1024 * 1024
                        or budget["capture_bytes"] > 50 * 1024 * 1024
                    ):
                        raise CaptureError("VKS_LIMIT_EXCEEDED", "页面快照超过采集体积预算")
                    result.snapshots.append(
                        {
                            "step_key": step["step_key"],
                            "name": step["name"],
                            "dom": dom,
                            "png": png,
                            "captured_ms": round((time.monotonic() - start) * 1000),
                            "viewport": viewport,
                        }
                    )
                else:
                    target = locator(page, step["target"])
                    count = target.count()
                    hidden_absent = kind == "wait_for" and step["state"] == "hidden" and count == 0
                    if count > 1:
                        raise CaptureError("VKS_LOCATOR_AMBIGUOUS", "定位必须唯一；不会自动选择第一个匹配项")
                    if count == 0 and not hidden_absent:
                        # SPAs may attach controls after DOMContentLoaded. Wait
                        # within this step's original deadline, retaining strict
                        # uniqueness instead of choosing an arbitrary match.
                        target.wait_for(state="attached", timeout=timeout)
                        if target.count() != 1:
                            raise CaptureError("VKS_LOCATOR_AMBIGUOUS", "等待后定位仍不唯一")
                        timeout = max(
                            1,
                            int(
                                min(step["timeout_ms"] / 1000, remaining) * 1000
                                - (time.monotonic() - step_started) * 1000
                            ),
                        )
                    if kind == "click":
                        target.click(timeout=timeout)
                    elif kind == "fill":
                        target.fill(fixture_value(step["value"], dataset["data"]), timeout=timeout)
                    elif kind == "select":
                        target.select_option(fixture_value(step["value"], dataset["data"]), timeout=timeout)
                    elif kind == "wait_for":
                        if step["state"] == "enabled":
                            expect(target).to_be_enabled(timeout=timeout)
                        elif step["state"] == "disabled":
                            expect(target).to_be_disabled(timeout=timeout)
                        else:
                            target.wait_for(state=step["state"], timeout=timeout)
                    elif kind == "assert_visible":
                        expect(target).to_be_visible(timeout=timeout)
                    elif kind == "assert_text":
                        expected = fixture_value(step["expected"], dataset["data"])
                        if step.get("match", "exact") == "contains":
                            expect(target).to_contain_text(expected, timeout=timeout)
                        else:
                            expect(target).to_have_text(expected, timeout=timeout)
                result.steps.append(
                    {
                        "step_key": step["step_key"],
                        "kind": kind,
                        "assertion_passed": True,
                        "started_ms": round((step_started - start) * 1000),
                        "duration_ms": round((time.monotonic() - step_started) * 1000),
                        "route_path": urlsplit(page.url).path,
                        **({"action_entity_id": step["action_entity_id"]} if step.get("action_entity_id") else {}),
                    }
                )
            if cancelled():
                raise CaptureError("VKS_CANCELLED", "采集已取消")
            return result
        except CaptureError:
            raise
        except (PlaywrightTimeout, AssertionError):
            raise CaptureError("VKS_CAPTURE_ASSERTION_FAILED", "页面断言或定位超时，未保存未完成观察") from None
        finally:
            context.close()
            browser.close()
