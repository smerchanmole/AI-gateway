#!/usr/bin/env bash
# Lanzador mínimo para macOS/Linux. `exec` entrega las señales al servidor
# Python, permitiendo que FastAPI cierre también su proceso hijo LiteLLM.
set -euo pipefail
source .venv/bin/activate
exec python app.py
