#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo '请先安装 uv：https://docs.astral.sh/uv/'; exit 1; }
command -v npm >/dev/null || { echo '请先安装 Node.js 22.12+（包含 npm）'; exit 1; }
uv sync --frozen
npm ci --silent
uv run python scripts/build_demo.py
export PYTHONPATH="$PWD/services/api:$PWD"
export VKS_DATA_DIR="${VKS_DATA_DIR:-$PWD/.vks}"
uv run python - <<'PYPORT'
import socket
with socket.socket() as probe:
    try:
        probe.bind(('127.0.0.1', 8000))
    except OSError:
        raise SystemExit('8000 端口已被占用，请先停止已有服务再启动本项目。')
PYPORT
uv run uvicorn vks_api.app:app --host 127.0.0.1 --port 8000 &
VKS_API_PID=$!
cleanup() { kill "$VKS_API_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
uv run python - "$VKS_API_PID" <<'PY'
import os, sys, time, urllib.request
for _ in range(50):
    try:
        os.kill(int(sys.argv[1]), 0)
        urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=.5)
        break
    except ProcessLookupError:
        raise SystemExit('API 启动失败。请检查 8000 端口和终端日志。')
    except OSError:
        time.sleep(.2)
else:
    raise SystemExit('API 就绪超时。请检查终端日志。')
PY
echo '知境工作台：http://127.0.0.1:5173'
echo '初始管理员账号 / 密码：owner / owner；也可使用 .vks/bootstrap-token 初始化凭证。'
npm run dev --workspace apps/web -- --strictPort
