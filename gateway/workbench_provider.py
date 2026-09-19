"""LiteLLM adapter for the Cloudera AI Workbench HTTP contract.

Workbench publishes models as ``POST /model`` and wraps OpenAI input under
``request``. This adapter preserves LiteLLM's outward-facing OpenAI API while
translating that single protocol hop.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Iterator
from typing import Any

from litellm import EmbeddingResponse, ModelResponse
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
_RESERVED_EXTRA_PARAMETERS = {"api_base", "api_key", "authorization", "headers", "input", "messages", "model", "request"}


def _normalize_api_base(value: str) -> str:
    """Normalize a Model Service URL without ever logging its query values."""

    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CustomLLMError(status_code=500, message="api_base de Workbench no es una URL HTTP válida")
    query = [(key.strip(), item.strip()) for key, item in urllib.parse.parse_qsl(
        parsed.query, keep_blank_values=True
    )]
    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urllib.parse.urlencode(query),
        "",
    ))


def _endpoint_auth(api_base: str, api_key: str | None) -> tuple[str, str | None, str | None]:
    """Resolve both documented Workbench authentication shapes.

    Catalog deployments publish ``?accessKey=...`` and may additionally need a
    user API key as Bearer. Direct connections keep the URL secret-free and use
    LiteLLM's ``api_key`` as the model accessKey in the JSON body.
    """

    target = _normalize_api_base(api_base)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
    query_access_key = str(query.get("accessKey", [""])[0]).strip()
    embedded_access_key = ""
    embedded_authorization = ""
    if api_key and api_key.lstrip().startswith("{"):
        try:
            credential = json.loads(api_key)
            if isinstance(credential, dict):
                embedded_access_key = str(credential.get("access_key") or "").strip()
                embedded_authorization = str(credential.get("authorization") or "").strip()
        except json.JSONDecodeError:
            pass
    if query_access_key:
        return target, None, embedded_authorization or api_key or None
    if embedded_access_key:
        return target, embedded_access_key, embedded_authorization or None
    if api_key:
        return target, api_key, None
    raise CustomLLMError(status_code=500, message="Falta el accessKey del modelo Workbench")


def _timeout_seconds(value: Any) -> float:
    """Normalize float/httpx.Timeout values without coupling to its internals."""

    if isinstance(value, (int, float)):
        return float(value)
    for attribute in ("read", "connect"):
        candidate = getattr(value, attribute, None)
        if isinstance(candidate, (int, float)):
            return float(candidate)
    return 120.0


def _urlopen(request: urllib.request.Request, timeout: Any):
    """Open a Workbench request with the host-scoped TLS policy.

    LiteLLM runs this adapter through ``urllib`` rather than its usual aiohttp
    transport.  The parent process publishes only the hosts whose connection
    explicitly selected diagnostic TLS mode.  Never disable verification for
    any other destination handled by the same proxy.
    """

    hostname = (urllib.parse.urlsplit(request.full_url).hostname or "").lower()
    insecure_hosts = {
        value.strip().lower()
        for value in os.environ.get("IA_GATEWAY_INSECURE_TLS_HOSTS", "").split(",")
        if value.strip()
    }
    seconds = _timeout_seconds(timeout)
    if hostname in insecure_hosts:
        return urllib.request.urlopen(
            request,
            timeout=seconds,
            context=ssl._create_unverified_context(),
        )
    return urllib.request.urlopen(request, timeout=seconds)


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
            if key not in _RESERVED_EXTRA_PARAMETERS
        })
    max_tokens = request.get("max_tokens", _DEFAULT_MAX_TOKENS)
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
        request["max_tokens"] = min(max_tokens, _MAX_MAX_TOKENS)
    else:
        request["max_tokens"] = _DEFAULT_MAX_TOKENS
    # These match the Workbench-generated example and prevent Qwen from
    # enabling long reasoning during a basic gateway probe.
    request.setdefault("enable_thinking", False)
    request.setdefault("reasoning_effort", "low")
    return {"request": request}


def _embedding_request_body(inputs: list, optional_params: dict[str, Any]) -> dict[str, Any]:
    """Translate OpenAI embeddings into the Workbench predictor contract."""

    normalized_inputs = inputs if isinstance(inputs, list) else [inputs]
    extra_body = optional_params.get("extra_body")
    extensions = extra_body if isinstance(extra_body, dict) else {}
    request = {
        "input": normalized_inputs,
        "input_type": extensions.get("input_type", optional_params.get("input_type", "passage")),
        "normalize": extensions.get("normalize", optional_params.get("normalize", True)),
        "batch_size": extensions.get("batch_size", optional_params.get("batch_size", len(normalized_inputs) or 1)),
    }
    if request["input_type"] not in {"query", "passage"}:
        raise CustomLLMError(status_code=400, message="input_type debe ser query o passage")
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


def _unwrap_embeddings(payload: Any, status_code: int, model: str) -> EmbeddingResponse:
    """Accept OpenAI-shaped or plain embedding arrays returned by predictors."""

    if not isinstance(payload, dict):
        raise CustomLLMError(status_code=status_code, message="Workbench devolvió una respuesta JSON no válida")
    if payload.get("success") is False:
        remote_status = payload.get("StatusCode") or payload.get("statusCode") or status_code
        raise CustomLLMError(status_code=int(remote_status), message=json.dumps(payload, ensure_ascii=False))
    response = payload.get("response", payload)
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError as exc:
            raise CustomLLMError(status_code=502, message="Workbench devolvió embeddings que no son JSON") from exc
    usage: dict[str, Any] = {}
    if isinstance(response, dict):
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        if isinstance(response.get("data"), list):
            data = response["data"]
            if all(isinstance(item, dict) and isinstance(item.get("embedding"), list) for item in data):
                return EmbeddingResponse(model=model, data=data, usage=usage)
        vectors = response.get("embeddings", response.get("embedding"))
    else:
        vectors = response
    if isinstance(vectors, list) and vectors and all(isinstance(value, (int, float)) for value in vectors):
        vectors = [vectors]
    if not isinstance(vectors, list) or not all(isinstance(vector, list) for vector in vectors):
        raise CustomLLMError(status_code=502, message="Workbench no devolvió un array de embeddings válido")
    data = [{"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)]
    return EmbeddingResponse(model=model, data=data, usage=usage)


class ClouderaWorkbenchLLM(CustomLLM):
    """Translate chat completions between LiteLLM and Model Service."""

    def completion(self, model: str, messages: list, api_base: str, model_response: ModelResponse,
                   optional_params: dict, api_key: str | None = None, headers: dict | None = None,
                   timeout: Any = None, **_kwargs: Any) -> ModelResponse:
        if not api_base:
            raise CustomLLMError(status_code=500, message="Falta api_base para el modelo Workbench")
        target, body_access_key, bearer = _endpoint_auth(api_base, api_key)
        request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if bearer:
            request_headers["Authorization"] = f"Bearer {bearer}"
        payload = _request_body(messages, optional_params)
        if body_access_key:
            payload["accessKey"] = body_access_key
        encoded_payload = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            target,
            data=encoded_payload,
            method="POST",
            headers=request_headers,
        )
        try:
            with _urlopen(request, timeout) as remote:
                status_code = int(getattr(remote, "status", 200))
                raw = remote.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            roles = [str(item.get("role") or "") for item in messages if isinstance(item, dict)]
            parameter_names = sorted(key for key in payload["request"] if key != "messages")
            diagnostic = (
                f"Contrato Workbench rechazado: roles={roles}, parámetros={parameter_names}, "
                f"payload_bytes={len(encoded_payload)}"
            )
            raise CustomLLMError(
                status_code=exc.code,
                message=f"{detail or str(exc)} · {diagnostic}",
            ) from exc
        except urllib.error.URLError as exc:
            raise CustomLLMError(status_code=502, message=f"No se pudo conectar con Workbench: {exc.reason}") from exc
        try:
            normalized = _unwrap(json.loads(raw), status_code)
        except json.JSONDecodeError as exc:
            raise CustomLLMError(status_code=502, message="Workbench devolvió contenido que no es JSON") from exc
        normalized.setdefault("model", model)
        return ModelResponse(**normalized)

    def embedding(self, model: str, input: list, model_response: EmbeddingResponse,
                  print_verbose: Any = None, logging_obj: Any = None,
                  optional_params: dict | None = None, api_key: str | None = None,
                  api_base: str | None = None, timeout: Any = None,
                  litellm_params: Any = None, **_kwargs: Any) -> EmbeddingResponse:
        if not api_base:
            raise CustomLLMError(status_code=500, message="Falta api_base para el modelo Workbench")
        target, body_access_key, bearer = _endpoint_auth(api_base, api_key)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        payload = _embedding_request_body(input, optional_params or {})
        if body_access_key:
            payload["accessKey"] = body_access_key
        request = urllib.request.Request(
            target, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers,
        )
        try:
            with _urlopen(request, timeout) as remote:
                status_code = int(getattr(remote, "status", 200))
                raw = remote.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise CustomLLMError(status_code=exc.code, message=detail or str(exc)) from exc
        except urllib.error.URLError as exc:
            raise CustomLLMError(status_code=502, message=f"No se pudo conectar con Workbench: {exc.reason}") from exc
        try:
            return _unwrap_embeddings(json.loads(raw), status_code, model)
        except json.JSONDecodeError as exc:
            raise CustomLLMError(status_code=502, message="Workbench devolvió contenido que no es JSON") from exc

    async def aembedding(self, **kwargs: Any) -> EmbeddingResponse:
        return await asyncio.to_thread(self.embedding, **kwargs)

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
