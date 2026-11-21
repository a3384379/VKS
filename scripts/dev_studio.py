"""Start a loopback-only VKS Studio development instance on macOS or Windows."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
STUDIO = ROOT / "apps/studio"
VITE = ROOT / "node_modules/vite/bin/vite.js"
STUDIO_STATE = ROOT / ".vks/studio-preview"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def default_data_dir():
    if configured := os.getenv("VKS_DATA_DIR"):
        return Path(configured)
    pointer = STUDIO_STATE / "data-directory"
    if pointer.is_file() and (configured := pointer.read_text(encoding="utf-8").strip()):
        return Path(configured)
    return STUDIO_STATE / "data"


def port_value(value):
    port = int(value)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("请选择介于 1024 和 65535 之间的端口")
    return port


def reserve_check(ports):
    if len(set(ports)) != len(ports):
        raise ValueError("API、Studio 与 Runtime 需要各自的端口")
    for port in ports:
        with closing(socket.socket()) as probe:
            if os.name == "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise ValueError(f"端口 {port} 已占用，请重新配置相应端口") from None


def configure_preview_runtime(index: Path, port: int):
    port = port_value(port)
    html = index.read_text(encoding="utf-8")
    if "</head>" not in html or 'name="vks-runtime-port"' in html:
        raise ValueError("固定 Studio 构建没有可配置的运行时端口入口")
    index.write_text(
        html.replace("</head>", f'<meta name="vks-runtime-port" content="{port}"></head>', 1),
        encoding="utf-8",
    )


def find_node(explicit):
    if explicit:
        return explicit.resolve(strict=True)
    command = shutil.which("node")
    if command:
        return Path(command).resolve()
    if os.name == "nt":
        locations = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs/node.exe"]
    else:
        locations = [Path("/opt/homebrew/bin/node"), Path("/usr/local/bin/node"), Path("/usr/bin/node")]
    for path in locations:
        if path.is_file():
            return path.resolve()
    raise ValueError("找不到 Node.js；请通过 --node 指定绝对可执行文件路径")


class OwnedProcesses:
    def __init__(self):
        self.processes = []

    def start(self, name, args, env, cwd):
        process = subprocess.Popen([str(arg) for arg in args], cwd=cwd, env=env, stdin=subprocess.DEVNULL)
        self.processes.append((name, process))
        print(f"{name} PID {process.pid}", flush=True)

    def verify(self):
        for name, process in self.processes:
            if process.poll() is not None:
                raise ValueError(f"{name} 子进程错误退出，退出码 {process.returncode}")

    def close(self):
        for _, process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
        for _, process in reversed(self.processes):
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def wait_ready(group, url, name, marker, cancelled, *, studio=False):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and not cancelled():
        group.verify()
        try:
            with opener.open(url + ("/health" if name == "Runtime" else "/api/v1/ready"), timeout=0.5) as response:
                info = json.load(response)
            if studio:
                with opener.open(url + "/", timeout=0.5) as response:
                    if "<title>知境 Studio" not in response.read().decode("utf-8"):
                        raise ValueError
            if info.get("launcher_instance") == marker:
                group.verify()
                return
        except (OSError, HTTPError, ValueError):
            pass
        time.sleep(0.15)
    if not cancelled():
        raise ValueError(f"{name} 就绪检查超时，请检查该子进程的输出")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--api-port", type=port_value, default=os.getenv("VKS_API_PORT", "8014"))
    parser.add_argument("--studio-port", type=port_value, default=os.getenv("VKS_STUDIO_PORT", "5193"))
    parser.add_argument("--runtime-port", type=port_value, default=os.getenv("VKS_RUNTIME_PORT", "5195"))
    parser.add_argument("--node", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--preview", action="store_true", default=os.getenv("VKS_STUDIO_PREVIEW") == "1")
    args = parser.parse_args(argv)
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)

    def cancelled():
        return stopped

    group = OwnedProcesses()
    try:
        if sys.version_info[:2] != (3, 12):
            raise ValueError("首发启动器使用仓库 Python 3.12 环境")
        reserve_check((args.api_port, args.studio_port, args.runtime_port))
        node = find_node(args.node)
        if not VITE.is_file():
            raise ValueError("请先在仓库执行 npm ci")
        if args.preview and not (STUDIO / "dist/index.html").is_file():
            raise ValueError("请先构建 Studio，再使用 --preview")
        studio_url = f"http://127.0.0.1:{args.studio_port}"
        api_url = f"http://127.0.0.1:{args.api_port}"
        runtime_url = f"http://127.0.0.1:{args.runtime_port}"
        marker = secrets.token_hex(16)
        common_env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.pathsep.join((str(ROOT / "services/api"), str(ROOT))),
            "VKS_DATA_DIR": str(args.data_dir.resolve()),
            "VKS_LAUNCHER_INSTANCE": marker,
        }
        api_env = {
            **common_env,
            "VKS_PERSONAL_MODE": os.getenv("VKS_PERSONAL_MODE", "1"),
            "VKS_STUDIO_BASE_URL": studio_url,
            "VKS_MODEL_ALLOWED_ORIGINS": os.getenv("VKS_MODEL_ALLOWED_ORIGINS", "https://api.openai.com"),
        }
        child_env = {
            k: v
            for k, v in common_env.items()
            if not k.startswith(("VKS_MODEL_", "VKS_DATABASE_", "VKS_DB_", "VKS_CONNECT_"))
            and k not in {"VKS_BOOTSTRAP_TOKEN", "VKS_TOKEN"}
        }
        runtime_env = {
            **child_env,
            "VKS_STUDIO_ALLOWED_ORIGINS": studio_url + f",http://localhost:{args.studio_port}",
            "VKS_RUNTIME_DATA_DIR": str(
                Path(os.environ.get("VKS_RUNTIME_DATA_DIR") or args.data_dir / "runtime").expanduser().resolve()
            ),
        }
        studio_env = {**child_env, "VKS_DEV_API_TARGET": api_url, "VITE_RUNTIME_PORT": str(args.runtime_port)}
        uvicorn = [sys.executable, "-m", "uvicorn", "vks_api.app:app", "--host", "127.0.0.1", "--port", args.api_port]
        if args.env_file:
            uvicorn.extend(("--env-file", args.env_file.resolve(strict=True)))

        with ExitStack() as stack:
            stack.callback(group.close)
            group.start("API", uvicorn, api_env, ROOT)
            wait_ready(group, api_url, "API", marker, cancelled)
            if stopped:
                return 0
            group.start(
                "Runtime", [sys.executable, "-m", "apps.runtime.server", "--port", args.runtime_port], runtime_env, ROOT
            )
            wait_ready(group, runtime_url, "Runtime", marker, cancelled)
            if stopped:
                return 0
            studio = [node, VITE]
            if args.preview:
                snapshot = stack.enter_context(TemporaryDirectory(prefix="vks-preview-"))
                shutil.copytree(STUDIO / "dist", snapshot, dirs_exist_ok=True)
                configure_preview_runtime(Path(snapshot) / "index.html", args.runtime_port)
                studio.extend(("preview", "--outDir", snapshot))
            studio.extend(("--host", "127.0.0.1", "--port", args.studio_port, "--strictPort"))
            group.start("Studio", studio, studio_env, STUDIO)
            wait_ready(group, studio_url, "Studio", marker, cancelled, studio=True)
            if stopped:
                return 0
            print(f"VKS Studio {studio_url}", flush=True)
            while not stopped:
                group.verify()
                time.sleep(0.2)
    except (OSError, ValueError) as exc:
        print(f"VKS 启动失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
