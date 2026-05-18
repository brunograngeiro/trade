#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
exec uvicorn app.api.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-8020}" --reload
