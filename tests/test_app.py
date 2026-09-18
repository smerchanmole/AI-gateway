from fastapi.testclient import TestClient
from datetime import date
import asyncio
import json
import ssl
import pytest

import app as dashboard
from gateway.auth import AuthStore
from gateway.cloudera import ClouderaCatalog
from gateway.log_store import daily_log_path, insert_log
from gateway.tls import ensure_self_signed_certificate


def test_dependency_bootstrap_runs_before_external_imports():
    """Cloudera must prepare the environment even when executing code as a cell."""
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
    """Authenticated HTTPS client that mirrors the real browser contract."""
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


def test_quick_test_propagates_external_ip_to_litellm(monkeypatch, client):
    calls = []

    class Response:
        is_error = False
        def json(self): return {"choices": [{"message": {"content": "OK"}}]}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(dashboard.manager, "models", lambda: [{
        "name": "modelo", "enabled": True, "mode": "chat",
    }])
    monkeypatch.setattr(dashboard.manager, "is_running", lambda: True)
    monkeypatch.setattr(dashboard.manager, "active_model_names", lambda: ["modelo"])
    monkeypatch.setattr(dashboard.httpx, "AsyncClient", lambda **_kwargs: Client())

    response = client.post(
        "/api/models/modelo/test",
        json={"prompt": "hola"},
        headers={"X-Envoy-External-Address": "198.51.100.27"},
    )

    assert response.status_code == 200
    assert calls[0][1]["headers"]["X-IA-Gateway-Client-IP"] == "198.51.100.27"


def test_manual_token_renewal_records_controlled_error(monkeypatch, client):
    recorded = []

    class Catalog:
        def renew_token(self, connection_id, force=False):
            raise RuntimeError("CDP CLI no respondió en 60 segundos")

        def record_renewal_error(self, connection_id, message):
            recorded.append((connection_id, message))

    monkeypatch.setattr(dashboard, "cloudera", Catalog())

    response = client.post("/api/cloudera/connections/cdp-1/renew-token", json={"force": True})

    assert response.status_code == 422
    assert recorded == [("cdp-1", "CDP CLI no respondió en 60 segundos")]


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


def test_onpremise_cai_profile_and_auth_mode_reach_catalog(monkeypatch, client):
    captured = {}

    class Catalog:
        def save_connection(self, *args):
            captured["args"] = args
            return {"id": "private-1", "has_token": True, "renewal_ready": False}

    monkeypatch.setattr(dashboard, "cloudera", Catalog())
    monkeypatch.setattr(dashboard.manager, "process_alive", lambda: False)

    response = client.post("/api/cloudera/connections", json={
        "name": "Private", "kind": "inference", "url": "https://ml.private",
        "platform": "onpremise", "onpremise_version": "7.3.2",
        "cai_version": "1.5.5_sp3", "onpremise_auth_mode": "ums_auto",
        "credential_type": "cdp_token", "renewal_url": "https://console-cdp.apps.example",
        "cdp_access_key_id": "access", "cdp_private_key": "private",
    })

    assert response.status_code == 200
    assert captured["args"][-9:] == (
        "1.5.5_sp3", "ums_auto", "cdp_token", "system", "",
        "catalog", "chat", "passage", "",
    )


def test_dashboard_disables_cache_and_uses_test_tabs(client):
    response = client.get("/")
    assert response.headers["cache-control"].startswith("no-store")
    assert 'id="test-tabs"' in response.text
    assert 'id="test-model"' not in response.text
    assert "Configuración YAML" in response.text
    assert 'id="yaml-editor"' in response.text
    assert 'id="model-form"' in response.text
    assert 'id="persistence"' in response.text
    assert 'id="download-yaml-backup"' in response.text
    assert 'id="yaml-import-file"' in response.text
    assert 'id="import-yaml-backup"' in response.text
    assert 'data-view="benchmark"' in response.text
    assert 'id="benchmark-form"' in response.text


def test_cloudera_model_entry_preserves_remote_openai_namespace():
    model = dashboard.ModelCreate(
        model_name="gptoss20b", model="openai/openai/gpt-oss-20b",
        api_base="https://inference.example/endpoints/gptoss20b/v1",
        source="cloudera", cloudera_kind="inference",
    )

    entry = dashboard._model_entry(model)

    assert entry["litellm_params"]["model"] == "openai/openai/gpt-oss-20b"


def test_cloudera_model_entry_builds_provider_prefix_from_remote_model():
    model = dashboard.ModelCreate(
        model_name="gptoss20b", model="openai/gpt-oss-20b",
        remote_model="openai/gpt-oss-20b",
        api_base="https://inference.example/endpoints/gptoss20b/v1",
        source="cloudera", cloudera_kind="inference",
    )

    entry = dashboard._model_entry(model)

    assert entry["litellm_params"]["model"] == "openai/openai/gpt-oss-20b"
    assert entry["model_info"]["dashboard_remote_model"] == "openai/gpt-oss-20b"


def test_benchmark_rejects_too_few_requests_for_concurrency(monkeypatch, client):
    monkeypatch.setattr(dashboard.manager, "models", lambda: [{
        "name": "modelo", "enabled": True, "mode": "chat", "provider_model": "openai/demo",
    }])
    monkeypatch.setattr(dashboard.manager, "is_running", lambda: True)
    monkeypatch.setattr(dashboard.manager, "active_model_names", lambda: ["modelo"])

    response = client.post("/api/benchmarks", json={
        "targets": [{"model": "modelo", "max_concurrency": 5}],
        "limit_mode": "requests", "requests_per_model": 10,
    })

    assert response.status_code == 422
    assert "al menos 15 peticiones" in response.json()["detail"]


def test_benchmark_duration_accepts_up_to_one_hundred_thousand_seconds():
    request = dashboard.BenchmarkRequest(
        targets=[{"model": "modelo", "max_concurrency": 1}],
        limit_mode="duration",
        duration_seconds=100_000,
    )

    assert request.duration_seconds == 100_000
    with pytest.raises(ValueError):
        dashboard.BenchmarkRequest(
            targets=[{"model": "modelo", "max_concurrency": 1}],
            limit_mode="duration",
            duration_seconds=100_001,
        )


def test_benchmark_starts_against_internal_gateway(monkeypatch, client):
    captured = {}

    class Runner:
        def start(self, config, models, base_url, headers):
            captured.update(config=config, models=models, base_url=base_url, headers=headers)
            return {"id": "run", "status": "running", "models": {}}

    monkeypatch.setattr(dashboard, "benchmarks", Runner())
    monkeypatch.setattr(dashboard.manager, "models", lambda: [{
        "name": "modelo", "enabled": True, "mode": "chat", "provider_model": "openai/demo",
    }])
    monkeypatch.setattr(dashboard.manager, "is_running", lambda: True)
    monkeypatch.setattr(dashboard.manager, "active_model_names", lambda: ["modelo"])

    response = client.post("/api/benchmarks", json={
        "targets": [{"model": "modelo", "max_concurrency": 3}],
        "limit_mode": "requests", "requests_per_model": 12,
        "strategy": "parallel", "request_timeout_seconds": 30,
    })

    assert response.status_code == 200
    assert captured["base_url"] == f"http://{dashboard.manager.host}:{dashboard.manager.port}"
    assert captured["models"][0]["name"] == "modelo"


def test_config_backup_downloads_exact_yaml_as_attachment(client):
    response = client.get("/api/config/backup")

    assert response.status_code == 200
    assert response.text == dashboard.manager.config_text()
    assert response.headers["content-type"].startswith("application/yaml")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith('.yaml"')


def test_yaml_import_uses_validation_size_limit_and_transactional_endpoint():
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "async function importYamlBackup()" in javascript
    assert "file.size > 1_000_000" in javascript
    assert 'api("/api/config/validate"' in javascript
    assert 'api("/api/config", {method: "PUT"' in javascript
    assert "await askRestart()" in javascript


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
    """A non-JSON response must not cause a 'body stream already read' error."""
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "const rawBody = await response.text();" in javascript
    assert "JSON.parse(rawBody)" in javascript
    assert "(await response.json()).detail" not in javascript


def test_dashboard_exposes_architecture_infographic(client):
    """The landing page must not reference an image the server does not publish."""
    dashboard_response = client.get("/")
    image_response = client.get("/static/ia-gateway-architecture.svg")

    assert "ia-gateway-architecture.svg" in dashboard_response.text
    assert image_response.status_code == 200
    assert image_response.headers["content-type"] == "image/svg+xml"
    assert dashboard_response.text.index('class="logs-section"') < dashboard_response.text.index(
        'class="architecture-hero"'
    )


def test_dashboard_offers_persistent_spanish_english_and_italian_localization(client):
    dashboard_response = client.get("/")
    javascript_response = client.get("/static/i18n.js")

    assert dashboard_response.text.count('class="language-select"') == 2
    assert '<option value="es">ES</option>' in dashboard_response.text
    assert '<option value="en">EN</option>' in dashboard_response.text
    assert '<option value="it">IT</option>' in dashboard_response.text
    assert dashboard_response.text.count('id="password-dialog"') == 1
    assert javascript_response.status_code == 200
    assert 'localStorage.getItem("ia-gateway-language")' in javascript_response.text
    assert 'document.documentElement.lang = language' in javascript_response.text
    assert 'new MutationObserver' in javascript_response.text
    assert '"Split across all levels · maximum 100,000 s."' in javascript_response.text
    assert '"Suddiviso tra tutti i livelli · massimo 100.000 s."' in javascript_response.text
    assert '"The connection will be vulnerable to impersonation.' in javascript_response.text
    assert '"La connessione sarà vulnerabile all\'impersonificazione.' in javascript_response.text


def test_model_metrics_use_vertical_rows_and_accessible_statuses():
    """Traffic-light indicators complement text rather than replacing its meaning."""
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")
    stylesheet = (dashboard.ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert 'class="metric-row"' in javascript
    assert 'aria-label="${status.label}"' in javascript
    assert 'value >= 80' in javascript
    assert 'value < 20' in javascript
    assert 'kind === "latency"' in javascript
    assert "await loadRemoteLatencies()" in javascript
    assert ".metric-row + .metric-row" in stylesheet


def test_model_cards_offer_accessible_curl_and_python_examples():
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")
    stylesheet = (dashboard.ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert 'aria-label="Cómo llamar al modelo' in javascript
    assert "/v1/chat/completions" in javascript
    assert "/v1/embeddings" in javascript
    assert "import requests" in javascript
    assert 'event.key === "Escape"' in javascript
    assert ".model-help:hover .model-help-popover" in stylesheet
    assert ".model-help:focus-within .model-help-popover" in stylesheet


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
    monkeypatch.setattr(dashboard, "_consume_model_validation", lambda *_args: None)
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


def test_guided_cloudera_model_preserves_discovered_engine_and_embedding_role(monkeypatch, client):
    captured = []
    monkeypatch.setattr(dashboard, "_consume_model_validation", lambda *_args: None)
    monkeypatch.setattr(
        dashboard.manager,
        "add_model",
        lambda entry: captured.append(entry) or {"content": "model_list: []\n", "restarted": False},
    )

    response = client.post("/api/config/models", json={
        "model_name": "embedqa-query",
        "model": "openai/nvidia/llama-3.2-nv-embedqa-1b-v2-query",
        "source": "cloudera",
        "cloudera_kind": "inference",
        "serving_engine": "nim",
        "task": "EMBED",
        "embedding_input_type": "query",
    })

    assert response.status_code == 200
    assert captured[0]["model_info"] == {
        "dashboard_source": "cloudera",
        "dashboard_cloudera_kind": "inference",
        "dashboard_serving_engine": "nim",
        "dashboard_task": "EMBED",
        "dashboard_embedding_input_type": "query",
        "dashboard_parameter_policy": "caller_wins",
        "dashboard_validation_fingerprint": captured[0]["model_info"]["dashboard_validation_fingerprint"],
        "dashboard_validated_at": captured[0]["model_info"]["dashboard_validated_at"],
    }
    assert captured[0]["litellm_params"]["encoding_format"] == "float"


def test_guided_model_config_rejects_invalid_environment_name(client):
    response = client.post("/api/config/models", json={
        "model_name": "inseguro",
        "model": "openai/modelo",
        "api_key_env": "sk-clave-en-claro",
    })

    assert response.status_code == 422
    assert "MAYUSCULAS" in response.json()["detail"]


def test_guided_vllm_profile_serializes_capacity_sampling_and_reasoning(monkeypatch, client):
    captured = []
    monkeypatch.setattr(dashboard, "_consume_model_validation", lambda *_args: None)
    monkeypatch.setattr(
        dashboard.manager,
        "add_model",
        lambda entry: captured.append(entry) or {"content": "model_list: []\n", "restarted": False},
    )

    response = client.post("/api/config/models", json={
        "model_name": "qwen-reasoning",
        "model": "openai/Qwen/Qwen3-32B",
        "backend_profile": "vllm",
        "compatibility_profile": "cloudera_1_5_5_sp3",
        "context_window": 32768,
        "max_output_tokens": 4096,
        "default_max_tokens": 1024,
        "temperature": 0.6,
        "top_p": 0.9,
        "top_k": 40,
        "min_p": 0.05,
        "repetition_penalty": 1.05,
        "seed": 42,
        "stop": ["</answer>"],
        "reasoning_mode": "enabled",
        "reasoning_effort": "high",
        "num_retries": 1,
        "max_parallel_requests": 4,
    })

    assert response.status_code == 200
    params = captured[0]["litellm_params"]
    assert params["max_tokens"] == 1024
    assert params["temperature"] == 0.6
    assert params["top_p"] == 0.9
    assert params["stop"] == ["</answer>"]
    assert params["extra_body"] == {
        "top_k": 40, "min_p": 0.05, "repetition_penalty": 1.05,
        "chat_template_kwargs": {"enable_thinking": True},
    }
    assert captured[0]["model_info"] == {
        "dashboard_backend_profile": "vllm",
        "dashboard_compatibility_profile": "cloudera_1_5_5_sp3",
        "dashboard_reasoning_mode": "enabled",
        "dashboard_context_window": 32768,
        "max_input_tokens": 28672,
        "max_output_tokens": 4096,
        "supports_reasoning": True,
        "dashboard_parameter_policy": "caller_wins",
        "dashboard_validation_fingerprint": captured[0]["model_info"]["dashboard_validation_fingerprint"],
        "dashboard_validated_at": captured[0]["model_info"]["dashboard_validated_at"],
    }


def test_model_must_pass_real_validation_before_it_can_be_saved(monkeypatch, client):
    payload = {
        "model_name": "qwen-json",
        "model": "openai/Qwen/Qwen3-32B",
        "api_base": "https://models.example/v1",
        "backend_profile": "vllm",
        "parameter_policy": "model_wins",
        "extra_parameters": {"extra_body.guided_json": {"type": "object"}},
    }
    assert client.post("/api/config/models", json=payload).status_code == 422

    async def successful_probe(model, entry):
        return {"status": 200, "parameters": dashboard._candidate_parameters(entry), "response_type": "dict"}

    captured = []
    monkeypatch.setattr(dashboard, "_probe_model_candidate", successful_probe)
    monkeypatch.setattr(
        dashboard.manager, "add_model",
        lambda entry: captured.append(entry) or {"content": "model_list: []\n", "restarted": False},
    )
    validation = client.post("/api/config/models/validate", json=payload)
    assert validation.status_code == 200
    payload["validation_id"] = validation.json()["validation_id"]

    saved = client.post("/api/config/models", json=payload)
    assert saved.status_code == 200
    assert captured[0]["model_info"]["dashboard_parameter_policy"] == "model_wins"
    assert captured[0]["model_info"]["dashboard_extra_parameters"] == {
        "extra_body.guided_json": {"type": "object"},
    }
    assert captured[0]["model_info"]["dashboard_validation_fingerprint"]


def test_guided_model_probe_reuses_cloudera_private_ca(tmp_path, monkeypatch):
    certificate, _key = ensure_self_signed_certificate(tmp_path / "certificate")
    catalog = ClouderaCatalog(tmp_path / "runtime")
    connection = catalog.save_connection(
        "Private TLS", "inference", "https://ml.private", "opaque-token",
        platform="onpremise", cdp_access_key_id="machine-access",
        cdp_private_key="machine-private",
        renewal_url="https://console-cdp.apps.private.example",
        onpremise_version="7.3.2", cai_version="1.5.5_sp3",
        onpremise_auth_mode="ums_auto", tls_verification="custom_ca",
        tls_ca_pem=certificate.read_text(encoding="utf-8"),
    )
    model = dashboard.ModelCreate(
        model_name="private-model", model="openai/provider-model",
        api_base="https://ml.private/v1",
        api_key_env=catalog.connection_environment_name(connection["id"]),
        source="cloudera", cloudera_kind="inference",
    )
    entry = dashboard._model_entry(model)
    captured = {}

    class Response:
        status_code = 200
        is_error = False
        def json(self): return {"data": []}

    class Client:
        def __init__(self, **kwargs): captured.update(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        async def post(self, *_args, **_kwargs): return Response()

    monkeypatch.setattr(dashboard, "cloudera", catalog)
    monkeypatch.setattr(dashboard.httpx, "AsyncClient", Client)
    result = asyncio.run(dashboard._probe_model_candidate(model, entry))

    assert result["status"] == 200
    assert isinstance(captured["verify"], ssl.SSLContext)
    assert captured["verify"].check_hostname is True
    assert captured["verify"].verify_mode == ssl.CERT_REQUIRED


def test_cai_gpt_oss_probe_sends_complete_remote_model_identifier(monkeypatch):
    model = dashboard.ModelCreate(
        model_name="gptoss20b", model="openai/openai/gpt-oss-20b",
        remote_model="openai/gpt-oss-20b",
        api_base="https://inference.example/endpoints/gptoss20b/v1",
        source="cloudera", cloudera_kind="inference",
    )
    entry = dashboard._model_entry(model)
    captured = {}

    class Response:
        status_code = 200
        is_error = False
        def json(self): return {"choices": []}

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        async def post(self, url, json, **_kwargs):
            captured.update({"url": url, "json": json})
            return Response()

    monkeypatch.setattr(dashboard.httpx, "AsyncClient", Client)
    result = asyncio.run(dashboard._probe_model_candidate(model, entry))

    assert result["status"] == 200
    assert captured["url"].endswith("/chat/completions")
    assert captured["json"]["model"] == "openai/gpt-oss-20b"


def test_cloudera_155_sp3_workbench_caps_guided_default_output(client):
    response = client.post("/api/config/models", json={
        "model_name": "qwen-workbench",
        "model": "cloudera_workbench/qwen38",
        "source": "cloudera",
        "cloudera_kind": "workbench",
        "backend_profile": "workbench",
        "compatibility_profile": "cloudera_1_5_5_sp3",
        "default_max_tokens": 513,
    })

    assert response.status_code == 422
    assert "512 tokens" in response.json()["detail"]


def test_triton_profile_rejects_openai_generation_parameters(client):
    response = client.post("/api/config/models", json={
        "model_name": "triton-oip",
        "model": "custom/fraud-model",
        "backend_profile": "triton",
        "temperature": 0.2,
    })

    assert response.status_code == 422
    assert "configura generación, tensores y batching en el deployment" in response.json()["detail"]


def test_detected_triton_profile_also_rejects_openai_generation_parameters(client):
    response = client.post("/api/config/models", json={
        "model_name": "triton-oip",
        "model": "custom/fraud-model",
        "backend_profile": "auto",
        "serving_engine": "triton",
        "temperature": 0.2,
    })

    assert response.status_code == 422
    assert "configura generación, tensores y batching en el deployment" in response.json()["detail"]


def test_manual_workbench_profile_gets_cloudera_155_output_cap(client):
    response = client.post("/api/config/models", json={
        "model_name": "qwen-workbench",
        "model": "cloudera_workbench/qwen38",
        "backend_profile": "workbench",
        "compatibility_profile": "cloudera_1_5_5_sp3",
        "default_max_tokens": 513,
    })

    assert response.status_code == 422
    assert "512 tokens" in response.json()["detail"]


def test_guided_workbench_embedding_serializes_input_contract():
    entry = dashboard._model_entry(dashboard.ModelCreate(
        model_name="nemotron-passage",
        model="cloudera_workbench/nemotron-embed",
        api_base="https://modelservice.ml-private.example/model",
        api_key_env="CLOUDERA_DIRECT_TOKEN",
        source="cloudera",
        cloudera_kind="workbench",
        backend_profile="workbench",
        task="embedding",
        embedding_input_type="passage",
    ))

    params = entry["litellm_params"]
    assert params["extra_body"] == {"input_type": "passage", "normalize": True}
    assert entry["model_info"]["dashboard_task"] == "embedding"
    assert entry["model_info"]["dashboard_embedding_input_type"] == "passage"


def test_workbench_embedding_validation_uses_direct_model_contract(monkeypatch):
    model = dashboard.ModelCreate(
        model_name="nemotron-passage",
        model="cloudera_workbench/nemotron-embed",
        api_base="https://modelservice.ml-private.example/model",
        api_key_env="CLOUDERA_DIRECT_TOKEN",
        source="cloudera", cloudera_kind="workbench",
        backend_profile="workbench", task="embedding",
        embedding_input_type="passage",
    )
    entry = dashboard._model_entry(model)
    captured = {}

    class Catalog:
        def environment(self):
            return {"CLOUDERA_DIRECT_TOKEN": json.dumps({
                "access_key": "model-access", "authorization": "user-api-key",
            })}
        def tls_verification_for(self, *_args, **_kwargs): return None

    class Response:
        status_code = 200
        is_error = False
        def json(self): return {"response": {"embeddings": [[0.1, 0.2]]}}

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False
        async def post(self, url, json, headers):
            captured.update({"url": url, "json": json, "headers": headers})
            return Response()

    monkeypatch.setattr(dashboard, "cloudera", Catalog())
    monkeypatch.setattr(dashboard.httpx, "AsyncClient", Client)
    result = asyncio.run(dashboard._probe_model_candidate(model, entry))

    assert result["status"] == 200
    assert captured["url"] == "https://modelservice.ml-private.example/model"
    assert captured["headers"]["Authorization"] == "Bearer user-api-key"
    assert captured["json"] == {
        "accessKey": "model-access",
        "request": {"input": ["Prueba de configuración"], "input_type": "passage",
                    "normalize": True, "batch_size": 1},
    }


def test_model_parameter_editor_exposes_backend_specific_controls():
    html = (dashboard.ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")

    for control in ("config-context-window", "config-default-max-tokens", "config-temperature",
                    "config-top-k", "config-reasoning-mode", "config-max-parallel",
                    "config-parameter-policy", "config-extra-parameters",
                    "config-validation-payload", "advisor-model", "advisor-dialog",
                    "guardrail-excluded-models", "save-guardrail-exclusions"):
        assert f'id="{control}"' in html
    assert "updateModelParameterContext" in javascript
    assert "Triton OIP recibe tensores" in javascript


def test_onprem_workbench_form_exposes_direct_model_and_embedding_fields():
    html = (dashboard.ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert '<option value="workbench">Cloudera AI Workbench</option>' in html
    for control in ("cloudera-onprem-workbench-fields", "cloudera-workbench-model-type",
                    "cloudera-workbench-input-type", "cloudera-workbench-access-key",
                    "cloudera-workbench-api-key", "cloudera-workbench-tls-verification"):
        assert f'id="{control}"' in html
    assert "updateClouderaWorkbenchFields" in javascript
    assert 'workbench_mode: $("#cloudera-kind").value === "workbench" ? "direct"' in javascript
    assert "Probando el deployment con todos los parámetros" in javascript
    assert "dashboard_effective_parameters" in (dashboard.ROOT / "gateway" / "litellm_callback.py").read_text(encoding="utf-8")


def test_dashboard_has_three_primary_areas_and_warn_only_guardrail():
    html = (dashboard.ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert 'data-view="models"' in html
    assert 'data-view="config"' in html
    assert 'data-view="logs"' in html
    assert "Riesgo detectado; la petición continuó" in javascript


def test_guardrail_endpoint_accepts_per_model_exclusions(monkeypatch, client):
    captured = {}

    def set_guardrail(enabled, model, policy, excluded_models=None, restart=True):
        captured.update({
            "enabled": enabled, "model": model, "policy": policy,
            "excluded_models": excluded_models, "restart": restart,
        })
        return {"content": "model_list: []\n", "restarted": False}

    monkeypatch.setattr(dashboard.manager, "set_guardrail", set_guardrail)
    response = client.put("/api/config/guardrail", json={
        "enabled": True,
        "model": "guardian",
        "policy": "warn",
        "excluded_models": ["embedding-local", "chat-interno"],
        "restart": False,
    })

    assert response.status_code == 200
    assert captured["excluded_models"] == ["embedding-local", "chat-interno"]
    assert captured["restart"] is False


def test_workbench_advisor_retries_without_system_role_or_temperature(monkeypatch, client):
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = __import__("json").dumps(payload)

        @property
        def is_error(self):
            return self.status_code >= 400

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return False

        async def post(self, _url, **kwargs):
            calls.append(kwargs["json"])
            if len(calls) == 1:
                return FakeResponse(500, {"error": {"message": '{"success":false,"StatusCode":400}'}})
            return FakeResponse(200, {"choices": [{"message": {"content": (
                '<think>breve</think>\n{"summary":"Perfil RAG","rationale":["Precisión"],'
                '"parameters":{"temperature":0.2,"unknown":"drop"}}'
            )}}]})

    monkeypatch.setattr(dashboard.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(dashboard.manager, "dashboard_settings", lambda: {
        "advisor": {"enabled": True, "model": "qwen8", "timeout": 120},
    })
    monkeypatch.setattr(dashboard.manager, "models", lambda: [
        {"name": "objetivo", "provider_model": "openai/modelo", "backend_profile": "vllm"},
        {"name": "qwen8", "provider_model": "cloudera_workbench/qwen8", "cloudera_kind": "workbench"},
    ])
    monkeypatch.setattr(dashboard.manager, "is_running", lambda: True)
    monkeypatch.setattr(dashboard.manager, "active_model_names", lambda: ["objetivo", "qwen8"])

    response = client.post("/api/config/advisor/recommend", json={
        "model_name": "objetivo", "use_case": "RAG de documentación técnica",
    })

    assert response.status_code == 200
    assert [message["role"] for message in calls[0]["messages"]] == ["system", "user"]
    assert calls[0]["temperature"] == 0.2
    assert [message["role"] for message in calls[1]["messages"]] == ["user"]
    assert "temperature" not in calls[1]
    assert calls[1]["max_tokens"] == 512
    assert response.json()["parameters"] == {"temperature": 0.2}


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


def test_cloudera_ui_distinguishes_engine_and_asymmetric_embedding_roles():
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'serving_engine: model.serving_engine' in javascript
    assert 'data-input-type="query">Preparar consulta' in javascript
    assert 'data-input-type="passage">Preparar documentos' in javascript
    assert 'Embedding asimétrico' in javascript


def test_cloudera_form_is_contextual_and_explains_urls_and_credential_lifecycle():
    html = (dashboard.ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="cloudera-onpremise-version"' in html
    assert 'id="cloudera-cai-version"' in html
    assert 'value="1.5.5_sp2"' in html and 'value="1.5.5_sp3"' in html
    assert 'value="7.1.9_sp1"' in html and 'value="7.3.1"' in html and 'value="7.3.2"' in html
    assert 'id="cloudera-cloud-fields"' in html
    assert 'id="cloudera-onprem-inference-fields"' in html
    assert 'value="manual"' in html and 'value="ums_auto"' in html
    assert 'id="cloudera-onprem-renewal-url"' in html
    assert 'id="cloudera-onprem-access-key-id"' in html
    assert 'id="cloudera-onprem-private-key"' in html
    assert 'id="cloudera-onprem-expiry"' in html
    assert 'id="cloudera-onprem-tls-verification"' in html
    assert 'id="cloudera-onprem-ca-pem"' in html
    assert "CA privada (PEM)" in html and "No verificar (inseguro)" in html
    assert "URL de endpoints" in html and "CDP_TOKEN (UMS)" in html
    assert "/api/v1/iam/generateWorkloadAuthToken" in html
    assert "usuario/contraseña general no sirve" in html
    assert "updateClouderaFormContext" in javascript
    assert "credential.accessKeyId" in javascript
    assert "credential.tlsVerification" in javascript
    assert "credential.tlsCaPem" in javascript
    assert "Renovación prevista" in javascript
    assert "Caduca · sustitución manual" in javascript
    assert "Clave larga · verifica vigencia en Knox" in javascript


def test_api_helper_accepts_empty_success_responses():
    javascript = (dashboard.ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "if (response.status === 204) return null" in javascript
    assert "if (!rawBody) return null" in javascript


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
    """The proxy may strip X-CSRF-Token without breaking protected operations."""
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
    """The style-src self CSP must remain compatible with dynamic bar heights."""

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
"""Tests for the HTTP contract and essential dashboard elements."""
