#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
root="$PWD"
mode="${1:-worker}"
case "$mode" in
  worker) args=(worker -Q collector --pool=solo --concurrency=1 --loglevel=WARNING --without-gossip --without-mingle --without-heartbeat) ;;
  beat) args=(beat --schedule="$root/.runtime/celerybeat-schedule" --loglevel=WARNING) ;;
  *) echo 'Choose worker or beat.' >&2; exit 1 ;;
esac
mkdir -p .runtime
manager=(/usr/bin/env)
if [ -f "$root/.apartmint-install.json" ]; then
  service=collector
  if [ "$mode" = beat ]; then service=scheduler; fi
  manager=("$root/.venv/bin/python" "$root/scripts/manage.py" run "$service" --)
fi
exec "${manager[@]}" /usr/bin/env -i HOME="$root/.runtime" PATH="$root/.venv/bin:/usr/bin:/bin" \
  APARTMINT_CONFIG="${APARTMINT_CONFIG:-$root/config/sources.json}" \
  APARTMINT_DATA_DIR="${APARTMINT_DATA_DIR:-$root/data}" \
  APARTMINT_BROKER_URL="${APARTMINT_BROKER_URL:-redis://127.0.0.1:16379/0}" \
  APARTMINT_POLL_SECONDS="${APARTMINT_POLL_SECONDS:-900}" \
  "$root/.venv/bin/celery" -A apartmint.tasks:collector_app "${args[@]}"
