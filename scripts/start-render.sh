#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${APP_ACCESS_TOKEN:?Set a private workspace access key in Render environment settings}"
: "${DATA_DIR:?Set a persistent data directory}"
export U2NET_HOME="${U2NET_HOME:-$DATA_DIR/models}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-2}"
mkdir -p "$DATA_DIR"
# Persistent disks are available at runtime, not during the Render build.
.venv/bin/python -c 'import os; from rembg import new_session; new_session(os.getenv("REMBG_MODEL", "isnet-general-use"), providers=["CPUExecutionProvider"]); print("Background-removal model ready", flush=True)'
exec .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-10000}" --workers 1 --proxy-headers --forwarded-allow-ips '*'
