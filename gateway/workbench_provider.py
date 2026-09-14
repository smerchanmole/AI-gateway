"""Adaptador LiteLLM para el contrato HTTP de Cloudera AI Workbench.

Workbench publica modelos como ``POST /model`` y envuelve la entrada OpenAI
bajo ``request``. Este adaptador conserva hacia fuera la API OpenAI de
LiteLLM, pero traduce ese único salto de protocolo.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from typing import Any

from litellm import ModelResponse
from litellm.llms.custom_llm import CustomLLM, CustomLLMError
from litellm.types.utils import GenericStreamingChunk


_FORWARDED_PARAMETERS = {
    "enable_thinking",
    "frequency_penalty",
    "max_tokens",
    "min_p",
    "presence_penalty",
    "preserve_thinking",
    "reasoning_effort",
    "repetition_penalty",
    "seed",
    "stop",
    "temperature",
    "top_k",
    "top_p",
}
_DEFAULT_MAX_TOKENS = 128
_MAX_MAX_TOKENS = 512


def _timeout_seconds(value: Any) -> float:
    """Normaliza float/httpx.Timeout sin acoplar el adaptador a su internals."""

    if isinstance(value, (int, float)):
        return float(value)
    for attribute in ("read", "connect"):
        candidate = getattr(value, attribute, None)
        if isinstance(candidate, (int, float)):
            return float(candidate)
    return 120.0


def _request_body(messages: list, optional_params: dict[str, Any]) -> dict[str, Any]:
    request = {"messages": messages}
    request.update({
        key: value for key, value in optional_params.items()
        if key in _FORWARDED_PARAMETERS and value is not None
    })
    extra_body = optional_params.get("extra_body")
    if isinstance(extra_body, dict):
        request.update({
            key: value for key, value in extra_body.items()
            if key in _FORWARDED_PARAMETERS
        })
    max_tokens = request.get("max_tokens", _DEFAULT_MAX_TOKENS)
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
        request["max_tokens"] = min(max_tokens, _MAX_MAX_TOKENS)
    else:
        request["max_tokens"] = _DEFAULT_MAX_TOKENS
    # Son los valores del ejemplo generado por Workbench y evitan que Qwen
    # active razonamiento largo en una prueba básica del gateway.
    request.setdefault("enable_thinking", False)
    request.setdefault("reasoning_effort", "low")
    return {"request": request}


def _unwrap(payload: Any, status_code: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CustomLLMError(status_code=status_code, message="Workbench devolvió una respuesta JSON no válida")
    if payload.get("success") is False:
        remote_status = payload.get("StatusCode") or payload.get("statusCode") or status_code
        raise CustomLLMError(status_code=int(remote_status), message=json.dumps(payload, ensure_ascii=False))
    response = payload.get("response", payload)
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            response = {"choices": [{"index": 0, "message": {"role": "assistant", "content": response}, "finish_reason": "stop"}]}
    if not isinstance(response, dict):
        raise CustomLLMError(status_code=status_code, message="Workbench no devolvió un objeto en 'response'")
    if response.get("choices"):
        return response
    content = response.get("generated_text") or response.get("content") or response.get("text") or response.get("output")
    if isinstance(content, dict):
        content = content.get("content") or content.get("text")
    if content is None and isinstance(response.get("message"), dict):
        content = response["message"].get("content")
    if content is None:
        content = json.dumps(response, ensure_ascii=False)
    return {
        "id": response.get("id", f"chatcmpl-workbench-{int(time.time() * 1000)}"),
        "object": "chat.completion",
        "created": response.get("created", int(time.time())),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": str(content)}, "finish_reason": "stop"}],
        "usage": response.get("usage", {}),
    }


class ClouderaWorkbenchLLM(CustomLLM):
    """Traduce chat completions entre LiteLLM y Model Service."""

    def completion(self, model: str, messages: list, api_base: str, model_response: ModelResponse,
                   optional_params: dict, api_key: str | None = None, headers: dict | None = None,
                   timeout: Any = None, **_kwargs: Any) -> ModelResponse:
        if not api_base:
            raise CustomLLMError(status_code=500, message="Falta api_base para el modelo Workbench")
        request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key:
            request_headers.setdefault("Authorization", f"Bearer {api_key}")
        request = urllib.request.Request(
            api_base,
            data=json.dumps(_request_body(messages, optional_params)).encode("utf-8"),
            method="POST",
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=_timeout_seconds(timeout)) as remote:
                status_code = int(getattr(remote, "status", 200))
                raw = remote.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CustomLLMError(status_code=exc.code, message=detail or str(exc)) from exc
        except urllib.error.URLError as exc:
            raise CustomLLMError(status_code=502, message=f"No se pudo conectar con Workbench: {exc.reason}") from exc
        try:
            normalized = _unwrap(json.loads(raw), status_code)
        except json.JSONDecodeError as exc:
            raise CustomLLMError(status_code=502, message="Workbench devolvió contenido que no es JSON") from exc
        normalized.setdefault("model", model)
        return ModelResponse(**normalized)

    async def acompletion(self, **kwargs: Any) -> ModelResponse:
        return await asyncio.to_thread(self.completion, **kwargs)

    def streaming(self, **kwargs: Any) -> Iterator[GenericStreamingChunk]:
        response = self.completion(**kwargs)
        choice = response.choices[0]
        yield {
            "text": choice.message.content or "",
            "is_finished": True,
            "finish_reason": choice.finish_reason or "stop",
            "usage": response.usage,
            "index": 0,
        }

    async def astreaming(self, **kwargs: Any) -> AsyncIterator[GenericStreamingChunk]:
        response = await self.acompletion(**kwargs)
        choice = response.choices[0]
        yield {
            "text": choice.message.content or "",
            "is_finished": True,
            "finish_reason": choice.finish_reason or "stop",
            "usage": response.usage,
            "index": 0,
        }


workbench_llm = ClouderaWorkbenchLLM()
