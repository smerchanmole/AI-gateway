#!/usr/bin/env bash
set -euo pipefail

DEPLOYMENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VLLM_VERSION="0.29.0"
CUDA_VARIANT="129"
CPU_ARCH="$(uname -m)"

case "${CPU_ARCH}" in
  x86_64|aarch64) ;;
  *)
    echo "Unsupported architecture for the official vLLM wheel: ${CPU_ARCH}" >&2
    exit 1
    ;;
esac

VLLM_WHEEL="https://github.com/vllm-project/vllm/releases/download/v${VLLM_VERSION}/vllm-${VLLM_VERSION}%2Bcu${CUDA_VARIANT}-cp38-abi3-manylinux_2_28_${CPU_ARCH}.whl"
PYTORCH_INDEX="https://download.pytorch.org/whl/cu${CUDA_VARIANT}"

python3 -m pip install --upgrade pip

# PyPI's vLLM 0.29.0 wheel uses CUDA 13.0. Cloudera's current NVIDIA driver
# exposes CUDA 12.9, so remove any incompatible stack left by an earlier build
# and install the official CUDA 12.9 release wheel explicitly.
python3 -m pip uninstall -y vllm torch torchvision torchaudio
python3 -m pip install --no-cache-dir --upgrade \
  "${VLLM_WHEEL}" \
  --extra-index-url "${PYTORCH_INDEX}"

python3 -m pip install --no-cache-dir -r "${DEPLOYMENT_DIR}/requirements.txt"

python3 - <<'PY'
import torch
import vllm

print("Installed vLLM", vllm.__version__)
print("Installed PyTorch", torch.__version__)
print("PyTorch CUDA build", torch.version.cuda)

if torch.version.cuda != "12.9":
    raise RuntimeError(
        f"Expected the CUDA 12.9 PyTorch build, found {torch.version.cuda!r}"
    )
PY
