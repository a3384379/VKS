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
export VKS_STUDIO_ALLOWED_ORIGINS="${VKS_STUDIO_ALLOWED_ORIGINS:-http://127.0.0.1:$VKS_STUDIO_PORT,http://localhost:$VKS_STUDIO_PORT}"
export VKS_MODEL_ALLOWED_ORIGINS="${VKS_MODEL_ALLOWED_ORIGINS:-https://api.openai.com}"
VKS_STUDIO_API_COMMAND=(uv run)
if [[ -f .env.local ]]; then VKS_STUDIO_API_COMMAND+=(--env-file "$PWD/.env.local"); fi
uv run python - "$VKS_API_PORT" "$VKS_STUDIO_PORT" "$VKS_RUNTIME_PORT" <<'PY'
import socket,sys
for port in sys.argv[1:]:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try: probe.bind(('127.0.0.1',int(port)))
        except OSError: raise SystemExit(f'{port} 端口已占用，请为 Studio 配置其他端口')
PY
"${VKS_STUDIO_API_COMMAND[@]}" uvicorn vks_api.app:app --host 127.0.0.1 --port "$VKS_API_PORT" &
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
if [[ "${VKS_STUDIO_PREVIEW:-0}" == "1" ]]; then
  if [[ ! -f apps/studio/dist/index.html ]]; then
    printf '请先运行 npm run build --workspace apps/studio，再启动固定构建预览。\n' >&2
    exit 1
  fi
  npm run preview --workspace apps/studio -- --port "$VKS_STUDIO_PORT"
else
  npm run dev --workspace apps/studio -- --port "$VKS_STUDIO_PORT"
fi
