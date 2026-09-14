#!/usr/bin/env bash
set -euo pipefail

DEPLOYMENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -m pip install --upgrade pip
python3 -m pip install --no-cache-dir -r "${DEPLOYMENT_DIR}/requirements.txt"

python3 -c 'import vllm; print("Installed vLLM", vllm.__version__)'
