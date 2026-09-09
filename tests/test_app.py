from fastapi.testclient import TestClient
from datetime import date
import pytest

import app as dashboard
from gateway.auth import AuthStore
from gateway.log_store import daily_log_path, insert_log


def test_dependency_bootstrap_runs_before_external_imports():
    """Cloudera debe preparar el entorno incluso si ejecuta el código como celda."""
    source = (dashboard.ROOT / "app.py").read_text(encoding="utf-8")

    bootstrap_call = source.index("\nbootstrap_private_environment()\n")
    first_external_import = source.index("import httpx")

    assert bootstrap_call < first_external_import
    assert 'globals().get("__file__")' in source
    assert "Path.cwd().resolve()" in source
    assert 'VENV_DIR = BOOTSTRAP_ROOT / ".venv"' in source
    assert "os.execve" in source
    assert 'runtime_environment.pop("PYTHONPATH", None)' in source
    assert 'runtime_environment["PYTHONNOUSERSITE"] = "1"' in source
    assert '"PIP_CONSTRAINT",' in source
    assert '"PIP_USER",' in source
    assert 'pip_environment["PIP_CONFIG_FILE"] = os.devnull' in source
    assert '"-I",' in source
    assert 'completed = subprocess.run(' in source
    assert 'Modo de ejecución:' in source
    assert '"pip", "check"' in source
    assert "last_lines = last_lines[-30:]" in source


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Cliente HTTPS autenticado; replica el contrato real de navegador."""
    store = AuthStore(tmp_path / "runtime")
    monkeypatch.setattr(dashboard, "auth", store)
    browser = TestClient(dashboard.app, base_url="https://testserver")
    response = browser.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    browser.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    response = browser.post("/api/auth/change-password", json={
        "current_password": "admin", "new_password": "Clave-Segura-123",
    })
    browser.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return browser


def test_quick_test_requires_running_gateway(monkeypatch, client):
    monkeypatch.setattr(dashboard.manager, "is_running", lambda: False)
    response = client.post("/api/models/topito/test", json={"prompt": "SELECT 1"})
    assert response.status_code == 409
    assert "Arranca LiteLLM" in response.json()["detail"]


def test_quick_test_rejects_empty_prompt(client):
    response = client.post("/api/models/topito/test", json={"prompt": "   "})
    assert response.status_code == 422


def test_quick_test_explains_when_model_is_pending_restart(monkeypatch, client):
    monkeypatch.setattr(dashboard.manager, "models", lambda: [{
        "name": "nuevo", "enabled": True, "mode": "chat",
    }])
    monkeypatch.setattr(dashboard.manager, "is_running", lambda: True)
    monkeypatch.setattr(dashboard.manager, "active_model_names", lambda: [])

    response = client.post("/api/models/nuevo/test", json={"prompt": "hola"})

    assert response.status_code == 409
    assert "pendiente de aplicar" in response.json()["detail"]
    assert "Aplicar cambios pendientes" in response.json()["detail"]


def test_gateway_auth_headers_ignore_legacy_general_key(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "general-test-key")

    assert dashboard.gateway_auth_headers() == {}


def test_gateway_auth_headers_allow_disabled_master_key(monkeypatch):
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)

    assert dashboard.gateway_auth_headers() == {}


def test_dynamic_cloudera_credentials_do_not_restart_litellm(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard.manager, "process_alive", lambda: True)
    monkeypatch.setattr(dashboard.manager, "sync_cloudera_credentials", lambda: True)
    monkeypatch.setattr(dashboard.manager, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(dashboard.manager, "start", lambda: calls.append("start"))

    result = dashboard.apply_cloudera_credential_changes()

    assert result == {"litellm_credentials_updated": True, "litellm_restarted": False}
    assert calls == []


def test_initial_cloudera_token_is_generated_when_renewal_is_ready(monkeypatch):
    connection = {"id": "connection-1", "has_token": False, "renewal_ready": True}

    class Catalog:
        def renew_token(self, connection_id, force=False):
            assert connection_id == "connection-1"
            assert force is True
            return {"renewed": True, "generated": True, "message": "Token generado"}

        def connections(self):
            return [{"id": "connection-1", "has_token": True, "renewal_ready": True}]

    monkeypatch.setattr(dashboard, "cloudera", Catalog())
    monkeypatch.setattr(dashboard.manager, "process_alive", lambda: False)

    result = dashboard.generate_initial_cloudera_token(connection)

    assert result["has_token"] is True
    assert result["token_generated"] is True


def test_dashboard_disables_cache_and_uses_test_tabs(client):
    response = client.get("/")
    assert response.headers["cache-control"].startswith("no-store")
    assert 'id="test-tabs"' in response.text
    assert 'id="test-model"' not in response.text
    assert "Configuración YAML" in response.text
    assert 'id="yaml-editor"' in response.text
    assert 'id="model-form"' in response.text
    assert 'id="persistence"' in response.text


def test_dashboard_explains_sqlite_dynamic_token_mode():
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")
    html = (dashboard.ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'persistence.credential_store || "SQLite"' in javascript
    assert "tokens sin reinicio · modelos con reinicio" in javascript
    assert "LiteLLM con BBDD" not in javascript
    assert "LiteLLM sin BBDD" not in javascript
    assert "Añadir, editar o eliminar modelos requiere reiniciar LiteLLM" in html
    assert "Los cambios de credenciales Cloudera se aplican sin reinicio" in html


def test_browser_reads_an_error_response_body_only_once():
    """Una respuesta no JSON no debe provocar 'body stream already read'."""
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "const rawBody = await response.text();" in javascript
    assert "JSON.parse(rawBody)" in javascript
    assert "(await response.json()).detail" not in javascript


def test_dashboard_exposes_architecture_infographic(client):
    """La portada no debe apuntar a una imagen que el servidor no publique."""
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


def test_remote_latency_probe_works_without_gateway_auth(monkeypatch, client):
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

    response = client.post("/api/models/topito/latency")

    assert response.status_code == 200
    assert response.json()["latency_ms"] >= 0
    assert len(calls) == 1
    assert calls[0][1]["headers"] == {}
    assert calls[0][1]["json"]["model"] == "topito"


def test_guided_model_config_uses_environment_reference(monkeypatch, client):
    captured = []
    monkeypatch.setattr(
        dashboard.manager,
        "add_model",
        lambda entry: captured.append(entry) or {"content": "model_list: []\n", "restarted": False},
    )

    response = client.post("/api/config/models", json={
        "model_name": "nuevo-openai",
        "model": "openai/modelo",
        "api_key_env": "OPENAI_API_KEY",
    })

    assert response.status_code == 200
    assert captured[0]["litellm_params"]["api_key"] == "os.environ/OPENAI_API_KEY"


def test_guided_model_config_rejects_invalid_environment_name(client):
    response = client.post("/api/config/models", json={
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


def test_cloudera_token_status_refreshes_without_page_reload():
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")
    refresh_function = javascript.split("async function refreshClouderaConnections()", 1)[1].split("\n}\n", 1)[0]

    assert "clouderaCredentialSnapshot" in javascript
    assert "refreshClouderaConnections" in javascript
    assert "CDP token actualizado automáticamente" in javascript
    assert "token_renewed_at" in javascript
    assert "document.addEventListener(\"visibilitychange\"" in javascript
    assert "location.reload()" not in refresh_function


def test_api_requires_login_and_rejects_csrf(tmp_path, monkeypatch):
    store = AuthStore(tmp_path / "runtime")
    monkeypatch.setattr(dashboard, "auth", store)
    browser = TestClient(dashboard.app, base_url="https://testserver")
    assert browser.get("/api/status").status_code == 401
    login = browser.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert login.status_code == 200
    assert login.cookies.get(store.cookie_name)
    assert browser.post("/api/gateway/stop").status_code == 403


def test_csrf_json_fallback_survives_proxy_header_filter(monkeypatch, client):
    """El proxy puede retirar X-CSRF-Token sin inutilizar operaciones seguras."""
    csrf_token = client.headers.pop("X-CSRF-Token")
    monkeypatch.setattr(dashboard.manager, "stop", lambda: {"process_alive": False})

    response = client.post("/api/gateway/stop", json={"_csrf_token": csrf_token})

    assert response.status_code == 200
    assert response.json()["process_alive"] is False


def test_initial_password_must_be_changed_and_hash_is_persisted(tmp_path):
    store = AuthStore(tmp_path / "runtime")
    credentials = store.credentials_file.read_text(encoding="utf-8")
    assert '"password_hash": "$argon2id$' in credentials
    assert '"password": "admin"' not in credentials
    token, status = store.login("admin", "admin", "127.0.0.1")
    assert token and status["must_change_password"] is True


def test_dashboard_exposes_secure_login_and_password_change():
    html = (dashboard.ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="login-form"' in html
    assert 'id="change-password-button"' in html
    assert 'autocomplete="current-password"' in html


def test_hourly_chart_does_not_depend_on_inline_styles_blocked_by_csp():
    """La CSP style-src self debe ser compatible con la altura de las barras."""

    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'style="height:' not in javascript
    assert 'class="hour-bar"' in javascript
    assert 'viewBox="0 0 100 100"' in javascript


def test_cloudera_log_reader_includes_historical_provider_model_rows(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "dashboard_settings.json").write_text(
        '{"provider_models":{"nemotron-publico":'
        '"openai/nvidia/nemotron-3-super-120b-a12b"}}',
        encoding="utf-8",
    )
    selected = date(2026, 9, 9)
    insert_log(
        daily_log_path(runtime, selected),
        "openai/nvidia/nemotron-3-super-120b-a12b",
        "success",
        100,
        {},
        {"usage": {}},
    )
    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    rows = dashboard.read_model_day_logs("nemotron-publico", selected, 500)
    assert len(rows) == 1
"""Pruebas del contrato HTTP y de los elementos esenciales del dashboard."""
