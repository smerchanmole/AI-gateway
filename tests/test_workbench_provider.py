import io
import json
import urllib.error

from litellm import ModelResponse

from gateway.workbench_provider import ClouderaWorkbenchLLM
from gateway.workbench_provider import _normalize_api_base, _request_body
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
