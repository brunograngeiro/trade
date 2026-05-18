#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
exec streamlit run dashboard/app.py --server.port "${DASHBOARD_PORT:-8501}" --server.address 0.0.0.0
