#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
root="$PWD"
config="${APARTMINT_CONFIG:-$root/config/sources.json}"
data="${APARTMINT_DATA_DIR:-$root/data}"
broker="${APARTMINT_BROKER_URL:-redis://127.0.0.1:16379/0}"
model_url="${APARTMINT_MODEL_BASE_URL:-http://127.0.0.1:11437/v1}"
model_name="${APARTMINT_MODEL:-apartmint-qwen3}"
mkdir -p "$data"
chmod 700 "$data"
if [ "$(uname -s)" != Darwin ]; then
  echo 'Use the isolated Compose worker on Linux.' >&2
  exit 1
fi
manager=(/usr/bin/env)
if [ -f "$root/.apartmint-install.json" ]; then
  manager=("$root/.venv/bin/python" "$root/scripts/manage.py" run worker --)
fi
exec "${manager[@]}" /usr/bin/sandbox-exec -f "$root/config/local-only.sb" /usr/bin/env -i \
  HOME="$root/.runtime" PATH="$root/.venv/bin:/usr/bin:/bin" \
  APARTMINT_CONFIG="$config" APARTMINT_DATA_DIR="$data" \
  APARTMINT_MODEL_BASE_URL="$model_url" APARTMINT_MODEL="$model_name" \
  APARTMINT_BROKER_URL="$broker" \
  APARTMINT_MAX_SNAPSHOT_AGE_SECONDS="${APARTMINT_MAX_SNAPSHOT_AGE_SECONDS:-1800}" \
  "$root/.venv/bin/celery" -A apartmint.tasks:inference_app worker -Q inference \
  --concurrency=1 --pool=solo --loglevel=WARNING --without-gossip --without-mingle --without-heartbeat
