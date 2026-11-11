#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/services/api:$PWD"
export VKS_DATA_DIR="${VKS_DATA_DIR:-$PWD/.vks/studio-preview/data}"
VKS_STUDIO_PORT="${VKS_STUDIO_PORT:-5193}"
VKS_API_PORT="${VKS_API_PORT:-8014}"
VKS_RUNTIME_PORT="${VKS_RUNTIME_PORT:-5195}"
export VKS_DEV_API_TARGET="http://127.0.0.1:$VKS_API_PORT"
export VITE_RUNTIME_PORT="$VKS_RUNTIME_PORT"
uv run python - "$VKS_API_PORT" "$VKS_STUDIO_PORT" "$VKS_RUNTIME_PORT" <<'PY'
import socket,sys
for port in sys.argv[1:]:
    with socket.socket() as probe:
        try: probe.bind(('127.0.0.1',int(port)))
        except OSError: raise SystemExit(f'{port} 端口已占用，请为 Studio 配置其他端口')
PY
uv run uvicorn vks_api.app:app --host 127.0.0.1 --port "$VKS_API_PORT" &
VKS_STUDIO_API_PID=$!
uv run python apps/runtime/server.py --port "$VKS_RUNTIME_PORT" &
VKS_STUDIO_RUNTIME_PID=$!
cleanup(){ kill "$VKS_STUDIO_API_PID" "$VKS_STUDIO_RUNTIME_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
uv run python - "$VKS_API_PORT" <<'PY'
import sys,time,urllib.request
for _ in range(60):
    try:
        urllib.request.urlopen('http://127.0.0.1:'+sys.argv[1]+'/ready',timeout=.5)
        break
    except OSError: time.sleep(.2)
else: raise SystemExit('Studio API 启动超时')
PY
printf '知境 Studio：http://127.0.0.1:%s\n' "$VKS_STUDIO_PORT"
npm run dev --workspace apps/studio -- --port "$VKS_STUDIO_PORT"
