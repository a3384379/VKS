#!/bin/sh
set -eu
VKS_REPO_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
if [ ! -x "$VKS_REPO_DIR/.venv/bin/python" ]; then
    printf '%s\n' '请先执行 uv sync --frozen，建立 VKS 的 Python 3.12 环境。' >&2
    exit 1
fi
exec "$VKS_REPO_DIR/.venv/bin/python" "$VKS_REPO_DIR/scripts/dev_studio.py" "$@"
