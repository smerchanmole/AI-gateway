import io
import json
import ssl
import urllib.error

from litellm import EmbeddingResponse, ModelResponse

from gateway.workbench_provider import ClouderaWorkbenchLLM
from gateway.workbench_provider import _normalize_api_base, _request_body, _urlopen
from litellm.llms.custom_llm import CustomLLMError


def test_workbench_provider_defaults_and_caps_output_tokens():
    messages = [{"role": "user", "content": "hola"}]

    assert _request_body(messages, {})["request"]["max_tokens"] == 128
    assert _request_body(messages, {"max_tokens": 4096})["request"]["max_tokens"] == 512
    assert _request_body(messages, {"extra_body": {"max_tokens": 900}})["request"]["max_tokens"] == 512
    assert _request_body(messages, {"extra_body": {"custom_sampler": "mi-plugin"}})["request"]["custom_sampler"] == "mi-plugin"
    assert _request_body(messages, {"extra_body": {"messages": "inseguro"}})["request"]["messages"] == messages


def test_workbench_provider_normalizes_pasted_access_key_whitespace():
    normalized = _normalize_api_base(
        " https://modelservice.wb.example/model?accessKey= model-key \n"
    )

    assert normalized == "https://modelservice.wb.example/model?accessKey=model-key"


def test_workbench_urlopen_disables_tls_only_for_configured_host(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout, **kwargs):
        captured[request.full_url] = {"timeout": timeout, **kwargs}
        return object()

    monkeypatch.setenv(
        "IA_GATEWAY_INSECURE_TLS_HOSTS",
        "modelservice.private.example",
    )
    monkeypatch.setattr("gateway.workbench_provider.urllib.request.urlopen", fake_urlopen)

    _urlopen(urllib.request.Request("https://modelservice.private.example/model"), 7)
    _urlopen(urllib.request.Request("https://api.openai.com/v1/models"), 9)

    private = captured["https://modelservice.private.example/model"]
    assert private["timeout"] == 7
    assert isinstance(private["context"], ssl.SSLContext)
    assert private["context"].verify_mode == ssl.CERT_NONE
    assert captured["https://api.openai.com/v1/models"] == {"timeout": 9}


def test_workbench_provider_wraps_request_and_unwraps_openai_response(monkeypatch):
    captured = {}

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["authorization"] = request.headers.get("Authorization")
        captured["timeout"] = timeout
        return Response(json.dumps({
            "success": True,
            "ReplicaID": "qwen38-replica",
            "response": {
                "id": "chatcmpl-1",
                "model": "qwen3.8-27b-fp8",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "¡Hola!"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        }).encode())

    monkeypatch.setattr("gateway.workbench_provider.urllib.request.urlopen", fake_urlopen)
    handler = ClouderaWorkbenchLLM()
    result = handler.completion(
        model="qwen3.8-27b-fp8",
        messages=[{"role": "user", "content": "hola"}],
        api_base="https://modelservice.wb.example/model?accessKey=model-key",
        api_key="workbench-api-key",
        model_response=ModelResponse(),
        optional_params={"max_tokens": 128, "temperature": 0.7},
        timeout=30,
    )

    assert captured["url"].endswith("/model?accessKey=model-key")
    assert captured["authorization"] == "Bearer workbench-api-key"
    assert captured["body"] == {"request": {
        "messages": [{"role": "user", "content": "hola"}],
        "max_tokens": 128,
        "temperature": 0.7,
        "enable_thinking": False,
        "reasoning_effort": "low",
    }}
    assert result.choices[0].message.content == "¡Hola!"
    assert result.usage.total_tokens == 5


def test_direct_workbench_uses_api_key_as_body_access_key(monkeypatch):
    captured = {}

    class Response(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def accepted(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["authorization"] = request.headers.get("Authorization")
        return Response(json.dumps({"response": {"content": "OK"}}).encode())

    monkeypatch.setattr("gateway.workbench_provider.urllib.request.urlopen", accepted)
    result = ClouderaWorkbenchLLM().completion(
        model="qwen", messages=[{"role": "user", "content": "hola"}],
        api_base="https://modelservice.workbench.example/model",
        api_key="model-access-key", model_response=ModelResponse(),
        optional_params={}, timeout=30,
    )

    assert captured["body"]["accessKey"] == "model-access-key"
    assert captured["authorization"] is None
    assert result.choices[0].message.content == "OK"


def test_direct_workbench_supports_optional_user_api_key(monkeypatch):
    captured = {}

    class Response(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def accepted(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["authorization"] = request.headers.get("Authorization")
        return Response(json.dumps({"response": {"content": "OK"}}).encode())

    monkeypatch.setattr("gateway.workbench_provider.urllib.request.urlopen", accepted)
    credential = json.dumps({"access_key": "model-access", "authorization": "user-api-key"})
    ClouderaWorkbenchLLM().completion(
        model="qwen", messages=[{"role": "user", "content": "hola"}],
        api_base="https://modelservice.workbench.example/model",
        api_key=credential, model_response=ModelResponse(), optional_params={}, timeout=30,
    )

    assert captured["body"]["accessKey"] == "model-access"
    assert captured["authorization"] == "Bearer user-api-key"


def test_workbench_embedding_contract_and_response(monkeypatch):
    captured = {}

    class Response(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def accepted(request, timeout):
        captured["body"] = json.loads(request.data)
        return Response(json.dumps({
            "success": True,
            "response": {"embeddings": [[0.1, 0.2], [0.3, 0.4]]},
        }).encode())

    monkeypatch.setattr("gateway.workbench_provider.urllib.request.urlopen", accepted)
    result = ClouderaWorkbenchLLM().embedding(
        model="nemotron-embed", input=["uno", "dos"],
        api_base="https://modelservice.workbench.example/model",
        api_key="model-access-key", model_response=EmbeddingResponse(),
        optional_params={"extra_body": {"input_type": "passage", "normalize": True}},
        timeout=30,
    )

    assert captured["body"] == {
        "accessKey": "model-access-key",
        "request": {"input": ["uno", "dos"], "input_type": "passage",
                    "normalize": True, "batch_size": 2},
    }
    assert result.data[0]["embedding"] == [0.1, 0.2]
    assert result.data[1]["index"] == 1


def test_workbench_http_error_reports_safe_request_shape(monkeypatch):
    def rejected(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://modelservice.example/model", 400, "Bad Request", {},
            io.BytesIO(b'{"success":false,"StatusCode":400}'),
        )

    monkeypatch.setattr("gateway.workbench_provider.urllib.request.urlopen", rejected)
    handler = ClouderaWorkbenchLLM()

    try:
        handler.completion(
            model="qwen8", messages=[{"role": "system", "content": "secreto"}, {"role": "user", "content": "caso"}],
            api_base="https://modelservice.example/model?accessKey=model-key",
            model_response=ModelResponse(), optional_params={"temperature": 0.2}, timeout=30,
        )
    except CustomLLMError as exc:
        message = str(exc)
        assert "roles=['system', 'user']" in message
        assert "temperature" in message
        assert "secreto" not in message
        assert "model-key" not in message
    else:
        raise AssertionError("Workbench debía devolver un error diagnóstico")
