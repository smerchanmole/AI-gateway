import io
import json

from litellm import ModelResponse

from gateway.workbench_provider import ClouderaWorkbenchLLM


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
