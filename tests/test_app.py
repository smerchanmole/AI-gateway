from fastapi.testclient import TestClient

import app as dashboard


def test_quick_test_requires_running_gateway(monkeypatch):
    monkeypatch.setattr(dashboard.manager, "is_running", lambda: False)
    client = TestClient(dashboard.app)
    response = client.post("/api/models/topito/test", json={"prompt": "SELECT 1"})
    assert response.status_code == 409
    assert "Arranca LiteLLM" in response.json()["detail"]


def test_quick_test_rejects_empty_prompt():
    client = TestClient(dashboard.app)
    response = client.post("/api/models/topito/test", json={"prompt": "   "})
    assert response.status_code == 422


def test_gateway_auth_headers_use_general_key(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "general-test-key")

    assert dashboard.gateway_auth_headers() == {
        "Authorization": "Bearer general-test-key"
    }


def test_gateway_auth_headers_require_general_key(monkeypatch):
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)

    try:
        dashboard.gateway_auth_headers()
    except RuntimeError as exc:
        assert "LITELLM_MASTER_KEY" in str(exc)
    else:
        raise AssertionError("Se esperaba un error sin master key")


def test_dashboard_disables_cache_and_uses_test_tabs():
    client = TestClient(dashboard.app)
    response = client.get("/")
    assert response.headers["cache-control"].startswith("no-store")
    assert 'id="test-tabs"' in response.text
    assert 'id="test-model"' not in response.text
    assert "Configuración YAML" in response.text
    assert 'id="yaml-editor"' in response.text
    assert 'id="model-form"' in response.text


def test_dashboard_exposes_architecture_infographic():
    """La portada no debe apuntar a una imagen que el servidor no publique."""
    client = TestClient(dashboard.app)

    dashboard_response = client.get("/")
    image_response = client.get("/static/ia-gateway-arquitectura.png")

    assert "ia-gateway-arquitectura.png" in dashboard_response.text
    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/png"
    assert dashboard_response.text.index('class="logs-section"') < dashboard_response.text.index(
        'class="architecture-hero"'
    )


def test_model_metrics_use_vertical_rows_and_accessible_statuses():
    """Los semáforos complementan al texto y no sustituyen su significado."""
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")
    stylesheet = (dashboard.ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert 'class="metric-row"' in javascript
    assert 'aria-label="${status.label}"' in javascript
    assert 'value >= 80' in javascript
    assert 'value < 20' in javascript
    assert 'kind === "latency"' in javascript
    assert "await loadRemoteLatencies()" in javascript
    assert ".metric-row + .metric-row" in stylesheet


def test_remote_latency_probe_uses_gateway_auth(monkeypatch):
    calls = []

    class Response:
        is_error = False

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(dashboard.manager, "models", lambda: [{
        "name": "topito", "provider_model": "openai/test", "enabled": True, "mode": "chat",
    }])
    monkeypatch.setattr(dashboard.manager, "is_running", lambda: True)
    monkeypatch.setattr(dashboard.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setenv("LITELLM_MASTER_KEY", "general-test-key")

    response = TestClient(dashboard.app).post("/api/models/topito/latency")

    assert response.status_code == 200
    assert response.json()["latency_ms"] >= 0
    assert len(calls) == 1
    assert calls[0][1]["headers"] == {"Authorization": "Bearer general-test-key"}
    assert calls[0][1]["json"]["model"] == "topito"


def test_guided_model_config_uses_environment_reference(monkeypatch):
    captured = []
    monkeypatch.setattr(
        dashboard.manager,
        "add_model",
        lambda entry: captured.append(entry) or {"content": "model_list: []\n", "restarted": False},
    )

    response = TestClient(dashboard.app).post("/api/config/models", json={
        "model_name": "nuevo-openai",
        "model": "openai/modelo",
        "api_key_env": "OPENAI_API_KEY",
    })

    assert response.status_code == 200
    assert captured[0]["litellm_params"]["api_key"] == "os.environ/OPENAI_API_KEY"


def test_guided_model_config_rejects_invalid_environment_name():
    response = TestClient(dashboard.app).post("/api/config/models", json={
        "model_name": "inseguro",
        "model": "openai/modelo",
        "api_key_env": "sk-clave-en-claro",
    })

    assert response.status_code == 422
    assert "MAYUSCULAS" in response.json()["detail"]


def test_dashboard_has_three_primary_areas_and_warn_only_guardrail():
    html = (dashboard.ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'data-view="models"' in html
    assert 'data-view="config"' in html
    assert 'data-view="logs"' in html
    assert "Riesgo detectado; la petición continuó" in javascript


def test_cloudera_models_show_independent_deployment_token_and_probe_states():
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "clouderaDeploymentState" in javascript
    assert "Token propio disponible" in javascript
    assert "Usará el CDP token general" in javascript
    assert "Respuesta sin probar" in javascript
    assert "Responde correctamente" in javascript
"""Pruebas del contrato HTTP y de los elementos esenciales del dashboard."""
