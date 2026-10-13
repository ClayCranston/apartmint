#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
preset="${1:-nvfp4}"
if [ "$(uname -s)" != Linux ] || ! command -v nvidia-smi >/dev/null; then
  echo 'CUDA deployment requires a Linux NVIDIA GPU host. Use start_metal.sh on Apple Silicon.' >&2
  exit 1
fi
case "$preset" in
  nvfp4) folder=qwen3-8b-nvfp4 ;;
  awq) folder=qwen3-8b-awq ;;
  *) echo 'Choose nvfp4 or awq.' >&2; exit 1 ;;
esac
.venv/bin/python scripts/preflight.py
.venv/bin/python scripts/download_model.py "$preset" --verify-only
export APARTMINT_MODEL_DIR="$PWD/models/$folder"
export APARTMINT_GPU="${APARTMINT_GPU:-0}"
export APARTMINT_GPU_MEMORY="${APARTMINT_GPU_MEMORY:-0.70}"
.venv/bin/python - <<'PY'
import os,subprocess
gpu=os.environ['APARTMINT_GPU']
fraction=float(os.environ['APARTMINT_GPU_MEMORY'])
total,free=map(float,subprocess.check_output(['nvidia-smi','-i',gpu,'--query-gpu=memory.total,memory.free','--format=csv,noheader,nounits'],text=True).strip().split(','))
if not 0 < fraction <= 0.90 or total*fraction + 1024 > free or total*fraction < 8192:
 raise SystemExit('Insufficient free VRAM for this memory fraction; use an idle GPU with at least 8 GiB allocated plus 1 GiB free reserve.')
PY
if [ -f .apartmint-install.json ]; then
  .venv/bin/python scripts/manage.py compose -- --profile cuda up -d --build
else
  docker compose --profile cuda up -d --build
fi
