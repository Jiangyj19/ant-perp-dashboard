#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/backend${PYTHONPATH:+:$PYTHONPATH}"
if [[ ! -x .venv/bin/python ]]; then
  ANT_PYTHON="${ANT_PYTHON:-python3}"
  "$ANT_PYTHON" -c 'import sys; assert sys.version_info >= (3,12), "需要 Python 3.12+；设置 ANT_PYTHON 指定解释器"'
  "$ANT_PYTHON" -m venv .venv
  .venv/bin/python -m pip install -r backend/requirements.txt
fi
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi
if [[ ! -f frontend/dist/index.html ]]; then
  (cd frontend && npm ci && npm run build)
fi
exec .venv/bin/python -m uvicorn antmonitor.app:create_app --factory --host "${HOST:-127.0.0.1}" --port "${PORT:-8787}" --no-access-log --no-proxy-headers
