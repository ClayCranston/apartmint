#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null; then
  echo 'Install uv using https://docs.astral.sh/uv/getting-started/installation/ and rerun.' >&2
  exit 1
fi
own_venv=0
if [ ! -e .venv ] && [ ! -L .venv ]; then own_venv=1; fi
uv sync --locked --extra dev
if [ "$own_venv" = 1 ]; then
  .venv/bin/python scripts/manage.py init --own-venv
else
  .venv/bin/python scripts/manage.py init
fi
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; fi
if [ ! -f config/sources.json ]; then cp config/sources.example.json config/sources.json; fi
mkdir -p .runtime data
chmod 700 .runtime data
.venv/bin/python scripts/preflight.py
echo 'Application environment ready. Download the hardware-appropriate model before launch.'
