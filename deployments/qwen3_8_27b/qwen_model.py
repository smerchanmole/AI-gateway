"""Cloudera AI Model Deployment for Qwen3.8-27B-FP8 using vLLM.

The model is loaded while the replica starts. This intentionally makes the
Cloudera readiness state reflect whether all GPU shards are usable.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

# vLLM must see these settings before it is imported.
os.environ.setdefault("VLLM_WORKER_MULTIPROC_METHOD", "spawn")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from vllm import LLM, SamplingParams

try:
    import cml.models_v1 as models
except ImportError:  # Allows local inspection outside a Cloudera PBJ runtime.
    class _LocalModels:
        @staticmethod
        def cml_model(function):
            return function

    models = _LocalModels()


MODEL_ID = os.getenv("QWEN_MODEL_ID", "Qwen/Qwen3.8-27B-FP8")
SERVED_MODEL_NAME = os.getenv("QWEN_SERVED_MODEL_NAME", "qwen3.8-27b-fp8")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


TENSOR_PARALLEL_SIZE = _env_int("VLLM_TENSOR_PARALLEL_SIZE", 2, 1, 8)
MAX_MODEL_LEN = _env_int("VLLM_MAX_MODEL_LEN", 16384, 2048, 262144)
GPU_MEMORY_UTILIZATION = _env_float(
    "VLLM_GPU_MEMORY_UTILIZATION", 0.92, 0.50, 0.98
)
MAX_NUM_SEQS = _env_int("VLLM_MAX_NUM_SEQS", 4, 1, 64)
CPU_OFFLOAD_GB = _env_float("VLLM_CPU_OFFLOAD_GB", 0.0, 0.0, 128.0)
KV_CACHE_DTYPE = os.getenv("VLLM_KV_CACHE_DTYPE", "fp8")
# Cloudera's CUDA compiler rejects a FlashInfer 0.6.18 JIT flag
# (--compress-mode=size). Pass the backend through the Python API because the
# legacy VLLM_ATTENTION_BACKEND environment variable is no longer authoritative.
ATTENTION_BACKEND = os.getenv("QWEN_ATTENTION_BACKEND", "TRITON_ATTN")

_allowed_domains = [
    domain.strip()
    for domain in os.getenv("VLLM_ALLOWED_MEDIA_DOMAINS", "").split(",")
    if domain.strip()
]


def _load_engine() -> LLM:
    shm = os.statvfs("/dev/shm")
    shm_total_mb = shm.f_blocks * shm.f_frsize / 1024**2
    shm_free_mb = shm.f_bavail * shm.f_frsize / 1024**2
    print(
        f"SHM_DIAGNOSTIC total={shm_total_mb:.0f} MiB "
        f"free={shm_free_mb:.0f} MiB"
    )

    return LLM(
        model=MODEL_ID,
        served_model_name=SERVED_MODEL_NAME,
        tensor_parallel_size=TENSOR_PARALLEL_SIZE,
        # The official checkpoint declares block-FP8 quantization itself.
        # dtype=auto keeps vLLM's recommended compute dtype for the hardware.
        dtype="auto",
        kv_cache_dtype=KV_CACHE_DTYPE,
        attention_backend=ATTENTION_BACKEND,
        cpu_offload_gb=CPU_OFFLOAD_GB,
        max_model_len=MAX_MODEL_LEN,
        gpu_memory_utilization=GPU_MEMORY_UTILIZATION,
        max_num_seqs=MAX_NUM_SEQS,
        enable_prefix_caching=True,
        trust_remote_code=False,
        hf_token=os.getenv("HF_TOKEN") or None,
        allowed_media_domains=_allowed_domains or None,
        limit_mm_per_prompt={"image": 4, "video": 0},
    )


# Cloudera imports this module during replica startup. Loading here avoids a
# first request that appears to time out while the ~31 GB repository is loaded.
ENGINE = _load_engine()
_GENERATION_LOCK = threading.Lock()


def _messages(args: dict[str, Any]) -> list[dict[str, Any]]:
    messages = args.get("messages")
    if messages is None:
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Provide a non-empty 'prompt' or a 'messages' array")
        messages = [{"role": "user", "content": prompt}]

    if not isinstance(messages, list) or not messages:
        raise ValueError("'messages' must be a non-empty array")
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Every message must be a JSON object")
        if message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ValueError("Every message needs a valid 'role'")
        if "content" not in message:
            raise ValueError("Every message needs 'content'")
    return messages


def _number(
    args: dict[str, Any], name: str, default: float, minimum: float, maximum: float
) -> float:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{name}' must be numeric")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ValueError(f"'{name}' must be between {minimum} and {maximum}")
    return result


@models.cml_model
def predict(args: dict[str, Any]) -> dict[str, Any]:
    """Generate one non-streaming Qwen chat completion from a JSON object."""

    if not isinstance(args, dict):
        raise ValueError("The request must be a JSON object")

    messages = _messages(args)
    thinking = bool(args.get("enable_thinking", False))
    reasoning_effort = str(args.get("reasoning_effort", "low"))
    if reasoning_effort not in {"low", "medium", "xhigh"}:
        raise ValueError("'reasoning_effort' must be low, medium, or xhigh")

    # Qwen publishes different recommended defaults for thinking and direct mode.
    default_temperature = 1.0 if thinking else 0.7
    default_top_p = 0.95 if thinking else 0.80
    default_presence_penalty = 0.0 if thinking else 1.5
    sampling = SamplingParams(
        # Model Service corta normalmente las peticiones largas antes de que
        # el motor termine. El límite evita que una generación huérfana retenga
        # _GENERATION_LOCK y encadene timeouts en las peticiones siguientes.
        max_tokens=int(_number(args, "max_tokens", 128, 1, 512)),
        temperature=_number(args, "temperature", default_temperature, 0.0, 2.0),
        top_p=_number(args, "top_p", default_top_p, 0.0, 1.0),
        top_k=int(_number(args, "top_k", 20, -1, 1000)),
        min_p=_number(args, "min_p", 0.0, 0.0, 1.0),
        presence_penalty=_number(
            args, "presence_penalty", default_presence_penalty, -2.0, 2.0
        ),
        repetition_penalty=_number(args, "repetition_penalty", 1.0, 0.01, 2.0),
        seed=int(_number(args, "seed", 0, 0, 2**31 - 1)),
        stop=args.get("stop"),
    )

    with _GENERATION_LOCK:
        result = ENGINE.chat(
            messages=messages,
            sampling_params=sampling,
            use_tqdm=False,
            chat_template_kwargs={
                "enable_thinking": thinking,
                "preserve_thinking": bool(args.get("preserve_thinking", True)),
                "reasoning_effort": reasoning_effort,
            },
        )[0]

    generated = result.outputs[0]
    prompt_tokens = len(result.prompt_token_ids or [])
    completion_tokens = len(generated.token_ids or [])
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": SERVED_MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": generated.text},
                "finish_reason": generated.finish_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
