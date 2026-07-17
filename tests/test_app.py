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


def test_dashboard_disables_cache_and_uses_test_tabs():
    client = TestClient(dashboard.app)
    response = client.get("/")
    assert response.headers["cache-control"].startswith("no-store")
    assert 'id="test-tabs"' in response.text
    assert 'id="test-model"' not in response.text
