#!/usr/bin/env bash
set -eu

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

PUBLISH_DIR="${PUBLISH_DIR:-/opt/rssnews/public}"
RSS_BIND="${RSS_BIND:-0.0.0.0}"
RSS_PORT="${RSS_PORT:-8081}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ -x ./.venv/bin/python3 ]; then
  PYTHON_BIN=./.venv/bin/python3
fi

mkdir -p "$PUBLISH_DIR"
exec "$PYTHON_BIN" -m app.feedback_server --directory "$PUBLISH_DIR" --bind "$RSS_BIND" --port "$RSS_PORT"
