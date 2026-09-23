"""Contract tests for the periodic model availability monitor."""

import asyncio
import json

import httpx

from gateway.health_monitor import ModelHealthMonitor


def test_monitor_probes_only_enabled_aliases_loaded_by_litellm():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((request.url.path, payload, request.headers))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    monitor = ModelHealthMonitor(
        models=lambda: [
            {"name": "qwen", "enabled": True, "mode": "chat", "provider_model": "ollama/qwen"},
            {"name": "vectors", "enabled": True, "mode": "embedding", "provider_model": "ollama/bge"},
            {"name": "disabled", "enabled": False, "mode": "chat", "provider_model": "openai/x"},
            {"name": "pending", "enabled": True, "mode": "chat", "provider_model": "openai/y"},
        ],
        active_names=lambda: ["qwen", "vectors", "disabled"],
        is_running=lambda: True,
        base_url="http://gateway",
        interval_seconds=300,
        transport=httpx.MockTransport(handler),
    )

    snapshot = asyncio.run(monitor.run_once())

    assert snapshot["interval_seconds"] == 300
    assert set(snapshot["models"]) == {"qwen", "vectors"}
    assert all(item["status"] == "healthy" for item in snapshot["models"].values())
    assert [call[0] for call in calls] == ["/v1/chat/completions", "/v1/embeddings"]
    assert calls[0][1]["reasoning_effort"] == "none"
    assert calls[0][1]["metadata"]["dashboard_probe"] == "periodic"
    assert calls[1][1]["metadata"]["dashboard_probe"] == "periodic"
    assert calls[0][2]["x-ia-gateway-probe"] == "periodic"


def test_monitor_reports_gateway_stopped_without_calling_models():
    monitor = ModelHealthMonitor(
        models=lambda: [{"name": "chat", "enabled": True, "mode": "chat"}],
        active_names=lambda: ["chat"],
        is_running=lambda: False,
        base_url="http://gateway",
        interval_seconds=300,
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(AssertionError("No debe llamar a LiteLLM"))
        ),
    )

    snapshot = asyncio.run(monitor.run_once())

    assert snapshot["gateway_status"] == "stopped"
    assert snapshot["models"] == {}

