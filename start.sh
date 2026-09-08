#!/usr/bin/env bash
# Lanzador mínimo para macOS/Linux. `exec` entrega las señales al servidor
# Python, permitiendo que FastAPI cierre también su proceso hijo LiteLLM.
set -euo pipefail
if [[ ! -x .venv/bin/python ]]; then
  echo "No existe .venv. Créalo con: python3 -m venv .venv" >&2
  exit 1
fi
.venv/bin/python -c 'import sys; assert sys.version_info >= (3, 11), "IA Gateway requiere Python 3.11 o superior. Recrea .venv con un Python moderno."'
source .venv/bin/activate
exec python app.py
