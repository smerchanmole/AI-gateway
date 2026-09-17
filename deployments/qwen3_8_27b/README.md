# Qwen3.8-27B-FP8 on Cloudera AI Workbench

This directory is a self-contained PBJ Model Deployment using the public
`Qwen/Qwen3.8-27B-FP8` checkpoint and vLLM 0.29.0 on one NVIDIA A100. The checkpoint contains
official fine-grained block-FP8 weights; vLLM detects the quantization from its
configuration, so no explicit `quantization=` parameter is needed.

The Hugging Face repository is approximately 30.9 GB. A single **A100 80 GB**
has enough VRAM for the FP8 weights and a large KV cache. This profile uses
tensor parallelism 1, so vLLM does not create multiple GPU workers and avoids
the shared-memory failure that affected the previous 2 x L4 configuration.

## Deployment form

Use **Deploy model from code** and enter:

| Field | Value |
|---|---|
| Name | `Qwen3.8-27B-FP8-vLLM` |
| Description | `Qwen3.8-27B-FP8 served with vLLM 0.29.0 on 1 NVIDIA A100 80GB` |
| Enable Authentication | Enabled (recommended) |
| Root Directory | `deployments/qwen3_8_27b` |
| Build Script Path | `cdsw-build.sh` |
| File | `qwen_model.py` |
| Function | `predict` |
| Runtime | JupyterLab, Python 3.11, **NVIDIA GPU Edition**, 2026.08 |
| Enable Spark | Disabled |
| Enable GPU | Enabled |
| GPU profile | **1 × NVIDIA A100 80 GB** |
| CPU / memory | At least 8 vCPU and 64 GiB RAM; 128 GiB is preferred for loading headroom |
| Replicas | `1` |

The screenshot currently shows **Standard** edition. Before deploying, enable
the **NVIDIA GPU Edition 2026.08** runtime in **Project Settings** and select it
in this form. Enabling only the GPU resource toggle with a Standard runtime is
not sufficient because the NVIDIA edition supplies the CUDA user-space stack.

The first replica start downloads approximately 31 GB from Hugging Face. Make
sure the workspace can reach `huggingface.co` and has enough local disk. A
private `HF_TOKEN` is optional because this model is not gated, but it can avoid
anonymous Hub rate limits.

The build script deliberately installs the official
`vllm-0.29.0+cu129` wheel. The default vLLM 0.29.0 package from PyPI targets
CUDA 13.0 and cannot initialize on the current Cloudera driver, which reports
CUDA 12.9. At the end of a successful build, verify that the log contains
`PyTorch CUDA build 12.9`.

## Environment variables

Set these under **Deployment → Set Environment Variables**:

| Variable | Recommended value | Purpose |
|---|---:|---|
| `VLLM_TENSOR_PARALLEL_SIZE` | `1` | Keep the checkpoint on one A100 and avoid multiprocessing IPC |
| `VLLM_GPU_MEMORY_UTILIZATION` | `0.90` | Leave operational headroom while reserving most VRAM for weights and KV cache |
| `VLLM_MAX_MODEL_LEN` | `262144` | Full native context on an A100 80 GB |
| `VLLM_MAX_NUM_SEQS` | `1` | Maximize KV cache for the single PBJ request |
| `VLLM_MAX_NUM_BATCHED_TOKENS` | `8192` | Bound chunked-prefill activation memory |
| `VLLM_KV_CACHE_DTYPE` | `bfloat16` | Required by Triton attention on A100/SM80 |
| `VLLM_CPU_OFFLOAD_GB` | `0` | Keep execution fully on the A100 |
| `VLLM_ENFORCE_EAGER` | `true` | Avoid CUDA-graph startup hangs/OOM observed on Ampere |
| `VLLM_USE_FLASHINFER_SAMPLER` | `0` | Avoid FlashInfer sampling JIT with Cloudera's older `nvcc` |
| `QWEN_ATTENTION_BACKEND` | `TRITON_ATTN` | Avoid FlashInfer JIT incompatibility with Cloudera's `nvcc` |
| `QWEN_MODEL_ID` | `Qwen/Qwen3.8-27B-FP8` | Official FP8 checkpoint (already the code default) |
| `QWEN_SERVED_MODEL_NAME` | `qwen3.8-27b-fp8` | Name returned in the response |
| `HF_TOKEN` | secret, optional | Hugging Face token; never put it in Git |

This profile requests the model's complete native context of **262,144 tokens**.
Keep concurrency 1, BF16 KV cache and eager mode. BF16 consumes twice the KV
memory of FP8, but Qwen3.8-27B has only 16 full-attention layers and this profile
is intended for an A100 80 GB. If startup reports insufficient KV cache, change
only `VLLM_MAX_MODEL_LEN` to `196608`, then to `131072` if necessary. On an A100
40 GB, start at `32768` instead. Do not compensate for insufficient KV cache by
pushing utilization above `0.92`.

## Shared-memory requirement

With `VLLM_TENSOR_PARALLEL_SIZE=1`, the deployment avoids the multi-GPU
shared-memory transport that previously required a larger `/dev/shm`. The code
still prints `SHM_DIAGNOSTIC total=... free=...`; 64 MiB is no longer expected
to block startup in this TP=1 profile.

## Cloudera CUDA compiler compatibility

Some Cloudera NVIDIA runtimes select FlashInfer automatically but ship an
`nvcc` that rejects FlashInfer 0.6.18's `--compress-mode=size` JIT option. This
deployment defaults to `QWEN_ATTENTION_BACKEND=TRITON_ATTN` and passes it as
vLLM's Python `attention_backend` argument. Do not use the old
`VLLM_ATTENTION_BACKEND` variable: recent vLLM versions require the Python/CLI
argument. A newer Cloudera runtime with a compatible CUDA toolkit can be tested
later with `QWEN_ATTENTION_BACKEND=FLASHINFER`.

Attention and sampling are configured separately in vLLM. Set
`VLLM_USE_FLASHINFER_SAMPLER=0` as well; otherwise vLLM can still invoke
FlashInfer while profiling its top-k/top-p sampler even though attention uses
Triton. The fallback sampler supports the request parameters used by this
deployment, with a small potential throughput cost.

If image URLs are accepted from callers, optionally set
`VLLM_ALLOWED_MEDIA_DOMAINS` to a comma-separated allowlist such as
`assets.example.com,storage.example.com`. Without it, vLLM's default URL policy
applies. This deployment accepts up to four images and disables video input.

## Example input and output

Paste this in **Example Input**:

```json
{
  "messages": [
    {"role": "user", "content": "Explica en una frase qué es Cloudera AI."}
  ],
  "max_tokens": 128,
  "temperature": 0.7,
  "enable_thinking": false,
  "reasoning_effort": "low"
}
```

Paste this in **Example Output** (it is a schema example; generated text varies):

```json
{
  "id": "chatcmpl-example",
  "object": "chat.completion",
  "created": 0,
  "model": "qwen3.8-27b-fp8",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Cloudera AI es una plataforma para desarrollar, desplegar y operar soluciones de inteligencia artificial sobre datos empresariales."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 25,
    "total_tokens": 45
  }
}
```

The function also accepts a simple input:

```json
{"prompt": "Hola", "max_tokens": 64}
```

Cloudera's public model endpoint wraps the function input under `request` and
its result under `response`; use the exact invocation snippet shown on the
deployment Overview page because authentication URLs differ by installation.

The deployment defaults to 128 output tokens and accepts at most 512. This is
deliberate: Model Service commonly enforces a request deadline near 30 seconds,
while an offline vLLM generation can continue after the client has timed out.
Keeping the output bounded prevents an abandoned request from retaining the
single-generation lock and delaying every subsequent call.

## Operational notes

- Startup can take many minutes on the first download. The replica is not ready
  until the checkpoint and vision encoder are loaded on the A100.
- One Cloudera replica invokes one model function at a time. Keep a single
  replica unless another A100 is independently available for a second replica.
- Streaming is not available through a PBJ model function. The result is a
  complete JSON response after generation finishes.
- A100 is Ampere (SM80), so it does not have Hopper's native FP8 tensor-core
  path. vLLM selects an Ampere-compatible weight-only kernel for this block-FP8
  checkpoint. FP8 model weights remain enabled, but the KV cache must use
  `bfloat16` with `TRITON_ATTN`. Keep `VLLM_ENFORCE_EAGER=true` and the NVIDIA
  runtime selected.
