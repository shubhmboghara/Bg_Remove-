#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.lock
export U2NET_HOME="${U2NET_HOME:-$PWD/.data/models}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-2}"
.venv/bin/python -c 'import os; from rembg import new_session; s = new_session(os.getenv("REMBG_MODEL", "isnet-general-use"), providers=["CPUExecutionProvider"]); print("Model ready:", s.model_name)'
