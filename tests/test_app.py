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


def test_dashboard_exposes_architecture_infographic():
    """La portada no debe apuntar a una imagen que el servidor no publique."""
    client = TestClient(dashboard.app)

    dashboard_response = client.get("/")
    image_response = client.get("/static/ia-gateway-beta-infografia.png")

    assert "ia-gateway-beta-infografia.png" in dashboard_response.text
    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/png"
