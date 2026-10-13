#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
root="$PWD"
runtime="${APARTMINT_METAL_RUNTIME:-$root/.runtime/metal}"
model="$root/models/qwen3-8b-mlx-4bit"
model_port="${APARTMINT_MODEL_PORT:-11437}"
model_name="${APARTMINT_MODEL:-apartmint-qwen3}"
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  echo 'This launcher requires native Apple Silicon macOS.' >&2
  exit 1
fi
if [ ! -x "$runtime/bin/python" ]; then
  echo 'Set APARTMINT_METAL_RUNTIME to an installed vllm-metal virtual environment.' >&2
  exit 1
fi
"$root/.venv/bin/python" scripts/download_model.py mlx --verify-only
if [ -f "$root/.apartmint-install.json" ]; then
  "$root/.venv/bin/python" "$root/scripts/manage.py" prepare-caches >/dev/null
else
  mkdir -p .runtime/cache .runtime/config .runtime/tmp .runtime/ipc
fi
manager=(/usr/bin/env)
if [ -f "$root/.apartmint-install.json" ]; then
  "$root/.venv/bin/python" "$root/scripts/manage.py" external-runtime "$runtime" >/dev/null
  manager=("$root/.venv/bin/python" "$root/scripts/manage.py" run model --)
fi
# Clean environment prevents unrelated .env/provider credentials entering inference.
# The sandbox denies non-loopback network traffic, including model downloads.
exec "${manager[@]}" /usr/bin/sandbox-exec -f "$root/config/local-only.sb" /usr/bin/env -i \
  HOME="$root/.runtime" PATH="$runtime/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  VLLM_PLUGINS=metal VLLM_HOST_IP=127.0.0.1 MASTER_ADDR=127.0.0.1 GLOO_SOCKET_IFNAME=lo0 \
  VLLM_RPC_BASE_PATH=.runtime/ipc \
  VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1 VLLM_METAL_USE_MLX=1 VLLM_MLX_DEVICE=gpu \
  VLLM_METAL_USE_PAGED_ATTENTION=1 VLLM_METAL_MEMORY_FRACTION=0.40 VLLM_METAL_DEBUG=0 \
  HF_HOME="$root/.runtime/huggingface" HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 TRANSFORMERS_OFFLINE=1 \
  XDG_CONFIG_HOME="$root/.runtime/config" XDG_CACHE_HOME="$root/.runtime/cache" TMPDIR="$root/.runtime/tmp" \
  "$runtime/bin/python" -m vllm.entrypoints.cli.main serve "$model" \
  --served-model-name "$model_name" --host 127.0.0.1 --port "$model_port" \
  --max-model-len 8192 --max-num-seqs 1 --gpu-memory-utilization 0.40 \
  --enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3 \
  --no-enable-log-requests --disable-uvicorn-access-log --disable-log-stats --disable-fastapi-docs
