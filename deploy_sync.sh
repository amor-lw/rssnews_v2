#!/usr/bin/env bash
set -eu

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

OUTDIR="${OUTDIR:-dist}"
PUBLISH_DIR="${PUBLISH_DIR:-}"
PUBLISH_SUBPATH="${PUBLISH_SUBPATH:-rss}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ -x ./.venv/bin/python3 ]; then
  PYTHON_BIN=./.venv/bin/python3
fi

if [ -z "$PUBLISH_DIR" ]; then
  echo "Missing PUBLISH_DIR in .env" >&2
  exit 1
fi

"$PYTHON_BIN" build_rss.py
TARGET_DIR="$PUBLISH_DIR"
if [ -n "$PUBLISH_SUBPATH" ]; then
  TARGET_DIR="$PUBLISH_DIR/$PUBLISH_SUBPATH"
fi

mkdir -p "$TARGET_DIR"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete "${OUTDIR}/" "${TARGET_DIR}/"
else
  cp -a "${OUTDIR}/." "${TARGET_DIR}/"
fi

echo "[OK] Published ${OUTDIR} -> ${TARGET_DIR}"
