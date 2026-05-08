#!/bin/bash
# 启动 Aegis 投研 OS 本地 Web 服务。
# 打开浏览器访问 http://localhost:8000
#
# 依赖: fastapi / uvicorn（已在 pyproject.toml 中声明）
# 退出: Ctrl+C

set -euo pipefail

cd "$(dirname "$0")"

HOST="${AEGIS_HOST:-127.0.0.1}"
PORT="${AEGIS_PORT:-8000}"

echo "Aegis Research OS · Web Server"
echo "  URL:  http://$HOST:$PORT"
echo "  Docs: http://$HOST:$PORT/docs"
echo

exec python -m uvicorn server.app:app --host "$HOST" --port "$PORT" \
    --reload --reload-dir server --reload-dir web
