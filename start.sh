#!/usr/bin/env bash
set -eu

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -x ./.venv/bin/python3 ]; then
  PYTHON_BIN=./.venv/bin/python3
fi

"$PYTHON_BIN" build_rss.py
ls -lah "${OUTDIR:-dist}"
