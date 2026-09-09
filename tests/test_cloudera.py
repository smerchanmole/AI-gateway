import json
import base64
import io
import subprocess
import urllib.error
from datetime import datetime, timezone
from types import SimpleNamespace

from gateway.cloudera import ClouderaCatalog


def jwt_with_exp(expiration):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiration}).encode()).decode().rstrip("=")
    return f"e30.{payload}.signature"


def test_connection_and_model_tokens_are_never_returned(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Inference", "inference", "https://ml.example", "cdp-secret",
        "cloud", 5, "workload-user", "workload-pass", "access-id", "private-key")
    variable = catalog.save_model_token(connection["id"], "chat-model", "model-secret")
    assert connection["has_token"] is True
    assert "token" not in connection
    assert "workload_password" not in connection and "cdp_private_key" not in connection
    assert connection["has_workload_password"] is True and connection["has_cdp_private_key"] is True
    assert connection["probe_interval_minutes"] == 5 and connection["platform"] == "cloud"
    assert catalog.connections()[0]["has_token"] is True
    assert catalog.environment()[variable] == "model-secret"
    assert catalog.environment()[catalog.connection_environment_name(connection["id"])] == "cdp-secret"
    assert oct(catalog.path.stat().st_mode & 0o777) == "0o600"


def test_legacy_json_is_migrated_to_sqlite_without_losing_tokens(tmp_path):
    legacy = tmp_path / "cloudera-connections.json"
    legacy.write_text(
        '{"connections":[{"id":"abc","name":"CDP","kind":"inference",'
        '"url":"https://ml.example","token":"legacy-token"}],"model_tokens":{}}',
        encoding="utf-8",
    )

    catalog = ClouderaCatalog(tmp_path)

    assert catalog.path.name == "cloudera.sqlite3"
    assert catalog.environment()["CLOUDERA_ABC_CDP_TOKEN"] == "legacy-token"


def test_connection_crud_preserves_and_removes_credentials(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    original = catalog.save_connection("Original", "inference", "https://old.example", "secret")
    catalog.save_model_token(original["id"], "model-1", "model-secret")
    updated = catalog.update_connection(original["id"], "Nueva", "inference", "https://new.example", "")
    assert updated["name"] == "Nueva" and updated["has_token"] is True
    assert any(name.startswith(f"CLOUDERA_{updated['id'].upper()}") for name in catalog.environment())
    catalog.delete_connection(updated["id"])
    assert catalog.connections() == []
    assert catalog.environment() == {}


def test_stale_edit_can_recreate_connection_with_new_credential(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    recreated = catalog.update_connection("stale-id", "Recuperada", "inference", "https://ml.example", "opaque-api-key")
    assert recreated["name"] == "Recuperada"
    assert recreated["has_token"] is True


def test_expired_jwt_is_reported_before_discovery(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    # JWT sintético sin firma: sólo probamos la lectura informativa de `exp`.
    expired = "e30.eyJleHAiOjF9.signature"
    try:
        catalog.save_connection("Caducada", "inference", "https://ml.example", expired)
    except RuntimeError as exc:
        assert "caducado" in str(exc)
    else:
        raise AssertionError("Un JWT caducado debe rechazarse antes de consultar Cloudera")


def test_full_endpoint_url_is_normalized_and_console_url_is_rejected(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Inference", "inference",
        "https://ml-64288d82-5dd.go01-dem.ylcu-atmi.cloudera.site/namespaces/serving-default/endpoints/llama-guard-3/openai/v1", "token")
    assert connection["url"] == "https://ml-64288d82-5dd.go01-dem.ylcu-atmi.cloudera.site"
    try:
        catalog.save_connection("Consola", "inference", "https://console.us-west-1.cdp.cloudera.com/ml/#/ml-serving")
    except RuntimeError as exc:
        assert "consola CDP" in str(exc)
    else:
        raise AssertionError("La URL de la consola no debe aceptarse como API")


def test_discovers_inference_endpoints(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Inference", "inference", "https://ml.example", "token")
    def response(url, *_args, **_kwargs):
        if url.endswith("listEndpoints"):
            return {"endpoints": [{"name": "sql-chat", "state": "Loaded"}]}
        return {"name": "sql-chat", "url": "https://ml.example/endpoints/sql-chat/v1",
                "state": "Loaded", "api_standard": "OpenAI Protocol", "model_name": "meta/sql-chat",
                "replica_count": 1,
                "conditions": [{"type": "IngressReady", "status": "True"}]}
    monkeypatch.setattr(catalog, "_request", response)
    models = catalog.discover(connection["id"])
    assert models[0]["name"] == "sql-chat"
    assert models[0]["protocol"] == "openai"
    assert models[0]["url_source"] == "describeEndpoint"
    assert models[0]["model_name"] == "meta/sql-chat"
    assert models[0]["has_chat_template"] is True
    assert models[0]["api_key_env"] == catalog.connection_environment_name(connection["id"])
    assert models[0]["replica_count"] == 1


def test_discovers_workbench_deployments(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Workbench", "workbench", "https://wb.example", "token")
    def response(url, *_args, **_kwargs):
        if url.endswith("projects?page_size=100"): return {"projects": [{"id": "p1", "name": "Proyecto"}]}
        if url.endswith("models?page_size=100"): return {"models": [{"id": "m1", "name": "Modelo"}]}
        return {"deployments": [{"id": "d1", "status": "deployed", "endpoint_url": "https://model.example"}]}
    monkeypatch.setattr(catalog, "_request", response)
    models = catalog.discover(connection["id"])
    assert models[0]["deployment_id"] == "d1"
    assert models[0]["protocol"] == "workbench"


def test_model_probe_prefers_specific_token_and_reports_auth_failure(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Inference", "inference", "https://ml.example", "cdp-token")
    catalog.save_model_token(connection["id"], "sql-chat", "specific-token")

    def denied(request, timeout=20):
        assert request.headers["Authorization"] == "Bearer specific-token"
        assert request.full_url == "https://ml.example/openai/v1/chat/completions"
        assert json.loads(request.data)["max_tokens"] == 1
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("gateway.cloudera.urllib.request.urlopen", denied)
    result = catalog.probe_model(connection["id"], "sql-chat", "https://ml.example/openai/v1", "openai",
                                 "meta/sql-chat", "text-generation", True)
    assert result["ok"] is False
    assert result["http_status"] == 401
    assert result["credential_source"] == "Token del modelo"


def test_openai_probe_uses_exact_cloudera_base_url(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Inference", "inference", "https://ml.example", "cdp-token")

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    monkeypatch.setattr("gateway.cloudera.urllib.request.urlopen", lambda request, timeout=20: Response())
    result = catalog.probe_model(connection["id"], "guard", "https://ml.example/openai/v1", "openai",
                                 "meta/llama-guard-3", "text-generation", True)
    assert result["ok"] is True
    assert result["probe_url"] == "https://ml.example/openai/v1/chat/completions"
    assert "inferencia mínima" in result["message"]


def test_openai_probe_does_not_duplicate_route_returned_by_cloudera(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Inference", "inference", "https://ml.example", "cdp-token")

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def accepted(request, timeout=20):
        assert request.full_url == "https://ml.example/endpoints/nemotron/v1/chat/completions"
        assert json.loads(request.data)["model"] == "nvidia/nemotron-3-super-120b-a12b"
        return Response()

    monkeypatch.setattr("gateway.cloudera.urllib.request.urlopen", accepted)
    result = catalog.probe_model(connection["id"], "nemotron",
        "https://ml.example/endpoints/nemotron/v1/chat/completions", "openai",
        "nvidia/nemotron-3-super-120b-a12b", "TEXT_GENERATION", True)
    assert result["ok"] is True


def test_onpremise_token_renewal_replaces_connection_token(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    old = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 60)
    new = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
    connection = catalog.save_connection("Private", "inference", "https://ml.private", old,
        platform="onpremise", workload_user="worker", workload_password="secret",
        renewal_url="https://cde.private/gateway/authtkn/knoxtoken/api/v1/token")

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def renewed(request, timeout=20):
        assert request.headers["Authorization"].startswith("Basic ")
        return Response(json.dumps({"access_token": new}).encode())

    monkeypatch.setattr("gateway.cloudera.urllib.request.urlopen", renewed)
    result = catalog.renew_token(connection["id"])
    assert result["renewed"] is True
    assert catalog._connection_record(connection["id"])["token"] == new
    visible = catalog.connections()[0]
    assert visible["token_renewed_at"]
    assert visible["token_expires_at"] == result["token_expires_at"]


def test_missing_token_is_generated_by_the_automatic_renewal_flow(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    generated = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
    connection = catalog.save_connection(
        "Private", "inference", "https://ml.private", "",
        platform="onpremise", workload_user="worker", workload_password="secret",
        renewal_url="https://cde.private/gateway/authtkn/knoxtoken/api/v1/token",
    )

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    monkeypatch.setattr(
        "gateway.cloudera.urllib.request.urlopen",
        lambda _request, timeout=20: Response(json.dumps({"access_token": generated}).encode()),
    )

    result = catalog.renew_token(connection["id"], force=False)

    assert connection["has_token"] is False
    assert connection["renewal_ready"] is True
    assert result["renewed"] is True
    assert result["generated"] is True
    assert catalog.connections()[0]["has_token"] is True


def test_cloud_renewal_repairs_markdown_url_and_calls_cdp_cli(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    executable = tmp_path / ".venv" / "bin" / "cdp"
    executable.parent.mkdir(parents=True)
    executable.touch()
    catalog = ClouderaCatalog(runtime)
    old = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 60)
    new = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
    connection = catalog.save_connection(
        "CDP", "inference", "https://ml.example", old, "cloud", 5, "", "",
        "access-id", "private-key",
        "[https://iamapi.us-west-1.cdp.cloudera.com](https://iamapi.us-west-1.cdp.cloudera.com/)",
        "DE",
    )
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["timeout"] = kwargs["timeout"]
        return SimpleNamespace(returncode=0, stdout=json.dumps({"token": new}), stderr="")

    monkeypatch.setattr("gateway.cloudera.subprocess.run", run)
    result = catalog.renew_token(connection["id"], force=True)

    assert result["renewed"] is True
    assert captured["command"][2] == "https://iamapi.us-west-1.cdp.cloudera.com"
    assert captured["timeout"] == 60
    assert catalog.connections()[0]["renewal_url"] == "https://iamapi.us-west-1.cdp.cloudera.com"


def test_cloud_renewal_timeout_becomes_controlled_runtime_error(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    executable = tmp_path / ".venv" / "bin" / "cdp"
    executable.parent.mkdir(parents=True)
    executable.touch()
    catalog = ClouderaCatalog(runtime)
    connection = catalog.save_connection(
        "CDP", "inference", "https://ml.example", "opaque-token", "cloud", 5, "", "",
        "access-id", "private-key", "https://iamapi.us-west-1.cdp.cloudera.com", "DE",
    )

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("gateway.cloudera.subprocess.run", timeout)

    try:
        catalog.renew_token(connection["id"], force=True)
    except RuntimeError as exc:
        assert "salida HTTPS" in str(exc)
        assert "iamapi.us-west-1.cdp.cloudera.com" in str(exc)
    else:
        raise AssertionError("El timeout del CDP CLI debe convertirse en un error controlado")


"""Pruebas aisladas del CRUD, descubrimiento, autenticación y renovación CDP."""
