#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.lock
export U2NET_HOME="${U2NET_HOME:-$PWD/.data/models}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-2}"
# Prefetch the quality model only on servers large enough to run it.
.venv/bin/python - <<'PYTHON'
import gc
import app
from rembg import new_session
models = [app.MODEL] + ([app.DETAIL_MODEL] if app.detail_available() else [])
for model in dict.fromkeys(models):
    loaded = new_session(model, providers=['CPUExecutionProvider'])
    print('Model ready:', model)
    del loaded
    gc.collect()
PYTHON
