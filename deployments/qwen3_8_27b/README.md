# Qwen3.8-27B-FP8 on Cloudera AI Workbench

This directory is a self-contained PBJ Model Deployment using the public
`Qwen/Qwen3.8-27B-FP8` checkpoint and vLLM 0.29.0. The checkpoint contains
official fine-grained block-FP8 weights; vLLM detects the quantization from its
configuration, so no explicit `quantization=` parameter is needed.

The Hugging Face repository is approximately 30.9 GB. It does not fit on one
24 GB NVIDIA L4 without CPU offload, but it fits normally across **2 x L4**.
Compared with the BF16 checkpoint, FP8 roughly halves weight memory. It does
not remove vLLM's `/dev/shm` requirement whenever tensor parallelism is greater
than one.

## Deployment form

Use **Deploy model from code** and enter:

| Field | Value |
|---|---|
| Name | `Qwen3.8-27B-FP8-vLLM` |
| Description | `Qwen3.8-27B-FP8 served with vLLM 0.29.0 on 2 NVIDIA L4 GPUs` |
| Enable Authentication | Enabled (recommended) |
| Root Directory | `deployments/qwen3_8_27b` |
| Build Script Path | `cdsw-build.sh` |
| File | `qwen_model.py` |
| Function | `predict` |
| Runtime | JupyterLab, Python 3.11, **NVIDIA GPU Edition**, 2026.08 |
| Enable Spark | Disabled |
| Enable GPU | Enabled |
| GPU profile | **2 × NVIDIA L4** |
| CPU / memory | At least 8 vCPU and 64 GiB RAM; more RAM helps model download/loading |
| Replicas | `1` |

The screenshot currently shows **Standard** edition. Before deploying, enable
the **NVIDIA GPU Edition 2026.08** runtime in **Project Settings** and select it
in this form. Enabling only the GPU resource toggle with a Standard runtime is
not sufficient because the NVIDIA edition supplies the CUDA user-space stack.

The first replica start downloads approximately 31 GB from Hugging Face. Make
sure the workspace can reach `huggingface.co` and has enough local disk. A
private `HF_TOKEN` is optional because this model is not gated, but it can avoid
anonymous Hub rate limits.

## Environment variables

Set these under **Deployment → Set Environment Variables**:

| Variable | Recommended value | Purpose |
|---|---:|---|
| `VLLM_TENSOR_PARALLEL_SIZE` | `2` | Shard the FP8 checkpoint across two L4 GPUs |
| `VLLM_GPU_MEMORY_UTILIZATION` | `0.92` | Reserve most remaining VRAM for the model and KV cache |
| `VLLM_MAX_MODEL_LEN` | `16384` | Conservative initial context; raise after measuring VRAM |
| `VLLM_MAX_NUM_SEQS` | `4` | Conservative concurrency for this deployment |
| `VLLM_KV_CACHE_DTYPE` | `fp8` | Halve KV-cache memory versus BF16 |
| `VLLM_CPU_OFFLOAD_GB` | `0` | Keep normal execution fully on the two GPUs |
| `QWEN_ATTENTION_BACKEND` | `TRITON_ATTN` | Avoid FlashInfer JIT incompatibility with Cloudera's `nvcc` |
| `QWEN_MODEL_ID` | `Qwen/Qwen3.8-27B-FP8` | Official FP8 checkpoint (already the code default) |
| `QWEN_SERVED_MODEL_NAME` | `qwen3.8-27b-fp8` | Name returned in the response |
| `HF_TOKEN` | secret, optional | Hugging Face token; never put it in Git |

Do not start with the model's native 262,144-token context on L4 GPUs. Once the
deployment is stable, try `32768`, then `65536`, checking startup logs and peak
VRAM after each change. Higher concurrency also consumes KV cache.

## Shared-memory requirement

With `VLLM_TENSOR_PARALLEL_SIZE=2`, vLLM still starts multiple worker processes
and requires more than Cloudera Model Deployment's default 64 MiB `/dev/shm`.
FP8 reduces GPU VRAM, not this IPC buffer. The code prints
`SHM_DIAGNOSTIC total=... free=...` before vLLM starts. The serving pod should
have at least 256 MiB; 1 GiB is recommended. A project terminal showing a larger
value only proves that the session pod received it, not the model-serving pod.

If the serving pod remains fixed at 64 MiB, a supported platform-side mount is
required. As a last-resort experiment, one L4 can be selected with
`VLLM_TENSOR_PARALLEL_SIZE=1`, `VLLM_CPU_OFFLOAD_GB=12`,
`VLLM_MAX_MODEL_LEN=8192`, and `VLLM_MAX_NUM_SEQS=1`. This avoids tensor-parallel
IPC but transfers weights over PCIe during every forward pass, is substantially
slower, and is not the recommended production configuration.

## Cloudera CUDA compiler compatibility

Some Cloudera NVIDIA runtimes select FlashInfer automatically but ship an
`nvcc` that rejects FlashInfer 0.6.18's `--compress-mode=size` JIT option. This
deployment defaults to `QWEN_ATTENTION_BACKEND=TRITON_ATTN` and passes it as
vLLM's Python `attention_backend` argument. Do not use the old
`VLLM_ATTENTION_BACKEND` variable: recent vLLM versions require the Python/CLI
argument. A newer Cloudera runtime with a compatible CUDA toolkit can be tested
later with `QWEN_ATTENTION_BACKEND=FLASHINFER`.

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
  until the checkpoint is loaded across both GPUs.
- One Cloudera replica invokes one model function at a time. Keep a single
  replica unless another set of two GPUs is available.
- Streaming is not available through a PBJ model function. The result is a
  complete JSON response after generation finishes.
- L4 (Ada, compute capability 8.9) supports vLLM's FP8 W8A8 kernels. Keep the
  NVIDIA runtime and CUDA stack selected in the deployment form.
