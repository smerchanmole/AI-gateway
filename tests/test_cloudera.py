import json
import base64
import io
import ssl
import subprocess
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.cloudera import ClouderaCatalog
from gateway.tls import ensure_self_signed_certificate


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
    assert connection["has_workload_password"] is False and connection["has_cdp_private_key"] is True
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
    # Unsigned synthetic JWT: this test covers only informative `exp` parsing.
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

    try:
        catalog.save_connection("Consola Private", "inference",
            "https://console-cdp.apps.private.example.com", "token")
    except RuntimeError as exc:
        assert "consola CDP" in str(exc)
    else:
        raise AssertionError("Una consola console-cdp tampoco debe aceptarse como API")


def test_onpremise_modern_uses_control_plane_origin_not_knox_token_url(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    try:
        catalog.save_connection(
            "Private", "inference", "https://ml.private", "knox-key", platform="onpremise",
            renewal_url="https://knox:8443/gateway/homepage/token-generation/index.html",
            onpremise_version="7.3.2_plus",
        )
    except RuntimeError as exc:
        assert "página web Token Generation" in str(exc)
    else:
        raise AssertionError("La página HTML no es una URL de API")

    with pytest.raises(RuntimeError, match="sin rutas de Knox"):
        catalog.save_connection(
            "Private", "inference", "https://ml.private", "knox-key", platform="onpremise",
            renewal_url="https://knox:8443/gateway/homepage/knoxtoken/api/v2/token",
            onpremise_version="7.3.2_plus",
        )

    connection = catalog.save_connection(
        "Private", "inference", "https://ml.private", "knox-key", platform="onpremise",
        renewal_url="https://console-cdp.apps.private.example/api/v1",
        onpremise_version="7.3.2_plus",
    )
    assert connection["renewal_url"] == "https://console-cdp.apps.private.example"


@pytest.mark.parametrize(("cai_version", "runtime_version"), [
    ("1.5.5_sp2", "7.1.9_sp1"),
    ("1.5.5_sp2", "7.3.1"),
    ("1.5.5_sp2_chf1", "7.3.2"),
    ("1.5.5_sp3", "7.1.9_sp2"),
    ("1.5.5_sp3", "7.3.1"),
    ("1.5.5_sp3", "7.3.2"),
])
def test_documented_onpremise_cai_combinations_accept_ums_auto(tmp_path, cai_version, runtime_version):
    catalog = ClouderaCatalog(tmp_path)

    connection = catalog.save_connection(
        "Private", "inference", "https://ml.private", platform="onpremise",
        onpremise_version=runtime_version, cai_version=cai_version,
        onpremise_auth_mode="ums_auto", renewal_url="https://console-cdp.apps.private.example/api/v1",
        cdp_access_key_id="machine-access", cdp_private_key="machine-private",
    )

    assert connection["renewal_ready"] is True
    assert connection["renewal_url"] == "https://console-cdp.apps.private.example"
    assert connection["cai_version"] == cai_version
    assert connection["onpremise_version"] == runtime_version


def test_sp2_requires_chf1_for_runtime_732(tmp_path):
    catalog = ClouderaCatalog(tmp_path)

    with pytest.raises(RuntimeError, match="no figura como compatible"):
        catalog.save_connection(
            "Private", "inference", "https://ml.private", "token", platform="onpremise",
            onpremise_version="7.3.2", cai_version="1.5.5_sp2",
            onpremise_auth_mode="manual",
        )


def test_knox_api_key_is_limited_to_certified_sp3_runtimes(tmp_path):
    catalog = ClouderaCatalog(tmp_path)

    with pytest.raises(RuntimeError, match="Knox API key sólo está certificada"):
        catalog.save_connection(
            "Private", "inference", "https://ml.private", "opaque-key", platform="onpremise",
            onpremise_version="7.3.1", cai_version="1.5.5_sp3",
            onpremise_auth_mode="manual", credential_type="knox_api_key",
        )
    connection = catalog.save_connection(
        "Private", "inference", "https://ml.private", "opaque-key", platform="onpremise",
        onpremise_version="7.3.2", cai_version="1.5.5_sp3",
        onpremise_auth_mode="manual", credential_type="knox_api_key",
    )
    assert connection["credential_lifecycle"] == "long_lived_unverified"


def test_username_password_cannot_be_presented_as_ums_token_generation(tmp_path):
    catalog = ClouderaCatalog(tmp_path)

    with pytest.raises(RuntimeError, match="CDP_ACCESS_KEY_ID y CDP_PRIVATE_KEY"):
        catalog.save_connection(
            "Private", "inference", "https://ml.private", platform="onpremise",
            onpremise_version="7.3.1", cai_version="1.5.5_sp3",
            onpremise_auth_mode="ums_auto", renewal_url="https://console-cdp.apps.private.example",
            workload_user="general-user", workload_password="password",
        )


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


def test_discovery_profiles_nim_asymmetric_embedding(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Inference", "inference", "https://ml.example", "token")

    def response(url, *_args, **_kwargs):
        if url.endswith("listEndpoints"):
            return {"endpoints": [{"name": "embedqa", "state": "Running"}]}
        return {
            "name": "embedqa",
            "url": "https://ml.example/endpoints/embedqa/v1/embeddings",
            "api_standard": "openai",
            "model_name": "nvidia/llama-3.2-nv-embedqa-1b-v2",
            "task": "EMBED",
        }

    monkeypatch.setattr(catalog, "_request", response)
    model = catalog.discover(connection["id"])[0]

    assert model["protocol"] == "openai"
    assert model["serving_engine"] == "nim"
    assert model["engine_source"] == "model"
    assert model["task_family"] == "embedding"
    assert model["requires_input_type"] is True
    assert model["embedding_input_types"] == ["query", "passage"]
    assert model["canonical_model_name"] == "nvidia/llama-3.2-nv-embedqa-1b-v2"


def test_endpoint_profile_prefers_explicit_vllm_and_detects_triton_route():
    vllm = ClouderaCatalog._endpoint_profile({
        "url": "https://ml.example/v1",
        "model_name": "nvidia/llama-3.2-nv-embedqa-1b-v2",
        "task": "EMBED",
        "runtime": "vLLM",
        "api_standard": "openai",
    })
    triton = ClouderaCatalog._endpoint_profile({
        "url": "https://ml.example/v2/models/fraud/infer",
        "model_name": "fraud",
    })

    assert vllm["serving_engine"] == "vllm"
    assert vllm["requires_input_type"] is False
    assert triton["serving_engine"] == "triton"
    assert triton["task_family"] == "inference"


def test_endpoint_profile_recognizes_nim_runtime_wrapping_triton():
    profile = ClouderaCatalog._endpoint_profile({
        "url": "https://ml.example/endpoints/embedqa/v1/embeddings",
        "model_name": "nvidia/llama-3.2-nv-embedqa-1b-v2",
        "task": "EMBED",
        "api_standard": "openai",
        "runtime_info": {
            "runtime_name": "nim-nvidia-llama-32-nv-embedqa-1b-v2-v1.10.0",
            "image_identifier": "registry/cloudera_thirdparty/nim/nvidia/embedqa:1.10.0",
        },
        "revisions": [{"container": "triton-inference-server"}],
    })

    assert profile["serving_engine"] == "nim"
    assert profile["runtime_backend"] == "triton"
    assert profile["requires_input_type"] is True


def test_discovers_workbench_deployments(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Workbench", "workbench", "https://wb.example", "token")
    def response(url, *_args, **_kwargs):
        if url.endswith("projects?page_size=100"): return {"projects": [{"id": "p1", "name": "Proyecto"}]}
        if url.endswith("models?page_size=100"): return {"models": [{"id": "m1", "name": "Modelo", "access_key": "model-key"}]}
        if url.endswith("builds?page_size=100"): return {"builds": [{"id": "b1"}]}
        return {"deployments": [{"id": "d1", "status": "deployed"}]}
    monkeypatch.setattr(catalog, "_request", response)
    models = catalog.discover(connection["id"])
    assert models[0]["deployment_id"] == "d1"
    assert models[0]["protocol"] == "workbench"
    assert models[0]["url"] == "https://modelservice.wb.example/model?accessKey=model-key"
    assert models[0]["credential_source"] == "API key de Workbench"


def test_direct_onprem_workbench_extracts_access_key_and_discovers_embedding(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection(
        "Nemotron Embed", "workbench",
        "https://modelservice.ml-private.example/model?accessKey=model-secret",
        platform="onpremise", onpremise_version="7.3.2",
        workbench_mode="direct", workbench_model_type="embedding",
        workbench_input_type="passage",
    )

    assert connection["url"] == "https://modelservice.ml-private.example/model"
    assert "model-secret" not in json.dumps(connection)
    assert connection["has_token"] is True
    models = catalog.discover(connection["id"])
    assert len(models) == 1
    assert models[0]["task_family"] == "embedding"
    assert models[0]["requires_input_type"] is True
    assert models[0]["embedding_input_types"] == ["query", "passage"]
    assert models[0]["api_key_env"].startswith("CLOUDERA_")

    credential = json.loads(catalog.environment()[models[0]["api_key_env"]])
    assert credential == {"access_key": "model-secret", "authorization": ""}


def test_direct_workbench_embedding_probe_uses_access_key_body(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection(
        "Nemotron Embed", "workbench", "https://modelservice.ml-private.example/model",
        "model-secret", platform="onpremise", onpremise_version="7.3.2",
        workbench_mode="direct", workbench_model_type="embedding",
        workbench_input_type="query",
    )
    model = catalog.discover(connection["id"])[0]

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def accepted(request, timeout=20):
        assert request.headers.get("Authorization") is None
        assert json.loads(request.data) == {
            "accessKey": "model-secret",
            "request": {"input": ["health check"], "input_type": "query",
                        "normalize": True, "batch_size": 1},
        }
        return Response()

    monkeypatch.setattr("gateway.cloudera.urllib.request.urlopen", accepted)
    result = catalog.probe_model(
        connection["id"], model["external_id"], model["url"], "workbench",
        model_name=model["model_name"], task=model["task"], has_chat_template=False,
    )

    assert result["ok"] is True
    assert result["probe_contract"] == "workbench"
    assert result["credential_source"] == "accessKey del modelo Workbench"


def test_direct_workbench_access_key_rotation_preserves_connection_id(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection(
        "Embed", "workbench", "https://modelservice.ml-private.example/model",
        "old-key", platform="onpremise", onpremise_version="7.3.2",
        workbench_mode="direct", workbench_model_type="embedding",
    )
    environment_name = catalog.connection_environment_name(connection["id"])

    updated = catalog.update_connection(
        connection["id"], "Embed", "workbench",
        "https://modelservice.ml-private.example/model", "new-key",
        platform="onpremise", onpremise_version="7.3.2",
        workbench_mode="direct", workbench_model_type="embedding",
    )

    assert updated["id"] == connection["id"]
    assert json.loads(catalog.environment()[environment_name])["access_key"] == "new-key"


@pytest.mark.parametrize(("api_key", "expected_authorization"), [
    ("", None),
    ("app-secret", "Bearer app-secret"),
])
def test_workbench_app_discovers_and_probes_optional_bearer_endpoint(
    tmp_path, monkeypatch, api_key, expected_authorization,
):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection(
        "Qwen streaming", "workbench_app", "https://qwen38.example/v1/chat/completions",
        api_key, platform="onpremise", onpremise_version="7.3.2",
        workbench_app_model="qwen3.8-27b-fp8",
        workbench_app_auth_mode="bearer" if api_key else "public",
    )

    assert connection["url"] == "https://qwen38.example/v1/chat/completions"
    assert connection["has_token"] is bool(api_key)
    models = catalog.discover(connection["id"])
    assert len(models) == 1
    model = models[0]
    assert model["protocol"] == "openai"
    assert model["supports_streaming"] is True
    assert model["model_name"] == "qwen3.8-27b-fp8"
    assert model["api_key_env"] == (
        catalog.workbench_app_environment_name(connection["id"]) if api_key else ""
    )

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def accepted(request, timeout=20):
        assert request.full_url == "https://qwen38.example/v1/chat/completions"
        assert request.headers.get("Authorization") == expected_authorization
        body = json.loads(request.data)
        assert body["model"] == "qwen3.8-27b-fp8"
        assert body["stream"] is False
        return Response()

    monkeypatch.setattr("gateway.cloudera.urllib.request.urlopen", accepted)
    result = catalog.probe_model(
        connection["id"], model["external_id"], model["url"], model["protocol"],
        model_name=model["model_name"], task=model["task"], has_chat_template=True,
        serving_engine=model["serving_engine"],
    )

    assert result["ok"] is True
    assert result["credential_source"] == (
        "API key de Workbench App" if api_key else "Sin autenticación"
    )


def test_workbench_app_accepts_v1_base_and_preserves_api_key_when_edited(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection(
        "Qwen", "workbench_app", "https://qwen38.example/v1", "secret",
        workbench_app_model="qwen3.8-27b-fp8",
        workbench_app_auth_mode="bearer",
    )

    assert connection["url"] == "https://qwen38.example/v1/chat/completions"
    updated = catalog.update_connection(
        connection["id"], "Qwen renamed", "workbench_app", connection["url"], "",
        workbench_app_model="qwen3.8-27b-fp8",
        workbench_app_auth_mode="bearer",
    )

    assert updated["id"] == connection["id"]
    assert updated["has_token"] is True
    assert catalog.environment()[catalog.workbench_app_environment_name(connection["id"])] == "secret"

    public = catalog.update_connection(
        connection["id"], "Qwen public", "workbench_app", connection["url"], "",
        workbench_app_model="qwen3.8-27b-fp8", workbench_app_auth_mode="public",
    )
    assert public["has_token"] is False
    assert catalog.environment()[catalog.workbench_app_environment_name(connection["id"])] == catalog.PUBLIC_APP_NO_AUTH


def test_workbench_app_repairs_duplicated_classic_model_route(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    malformed = (
        "https://qwen38.example/v1/chat/completions/model/v1/chat/completions"
    )
    connection = catalog.save_connection(
        "Qwen", "workbench_app", malformed, "",
        workbench_app_model="qwen3.8-27b-fp8", workbench_app_auth_mode="public",
    )

    expected = "https://qwen38.example/v1/chat/completions"
    assert connection["url"] == expected
    assert catalog._read()["connections"][0]["url"] == expected

    # Records saved by the earlier release are repaired on read/discovery too,
    # so operators do not need to delete and recreate the connection.
    data = catalog._read()
    data["connections"][0]["url"] = malformed
    catalog._write(data)
    assert catalog.connections()[0]["url"] == expected
    assert catalog.discover(connection["id"])[0]["url"] == expected


def test_workbench_probe_uses_model_contract_and_allows_modelservice_host(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Workbench", "workbench", "https://wb.example", "api-key")

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def accepted(request, timeout=20):
        assert request.method == "POST"
        assert request.full_url == "https://modelservice.wb.example/model?accessKey=model-key"
        assert request.headers["Authorization"] == "Bearer api-key"
        payload = json.loads(request.data)
        assert payload["request"]["messages"][0]["content"] == "Responde solo OK"
        assert payload["request"]["enable_thinking"] is False
        return Response()

    monkeypatch.setattr("gateway.cloudera.urllib.request.urlopen", accepted)
    result = catalog.probe_model(connection["id"], "m1",
        "https://modelservice.wb.example/model?accessKey=model-key", "workbench")

    assert result["ok"] is True
    assert result["credential_source"] == "API key de Workbench"


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


def test_modern_onprem_discovery_auth_error_mentions_knox_preauth(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection(
        "Inference", "inference", "https://ml.example", "opaque-knox-key",
        platform="onpremise", onpremise_version="7.3.2_plus",
    )

    def denied(request, timeout=20):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("gateway.cloudera.urllib.request.urlopen", denied)
    with pytest.raises(RuntimeError, match="cdp-preauth"):
        catalog.discover(connection["id"])


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


def test_nim_embedding_probe_adds_required_query_contract(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Inference", "inference", "https://ml.example", "cdp-token")

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def accepted(request, timeout=20):
        assert request.full_url == "https://ml.example/endpoints/embedqa/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer cdp-token"
        assert json.loads(request.data) == {
            "model": "nvidia/llama-3.2-nv-embedqa-1b-v2",
            "input": ["health check"],
            "input_type": "query",
        }
        return Response()

    monkeypatch.setattr("gateway.cloudera.urllib.request.urlopen", accepted)
    result = catalog.probe_model(
        connection["id"], "embedqa", "https://ml.example/endpoints/embedqa/v1/embeddings",
        "openai", "nvidia/llama-3.2-nv-embedqa-1b-v2-query", "EMBED", False,
        "nim", True,
    )

    assert result["ok"] is True
    assert result["probe_contract"] == "nim-embedding-query"


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


def test_onpremise_732_static_knox_credential_is_manual_without_iam_keys(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection(
        "Private 7.3.2", "inference", "https://ml.private", "opaque-knox-key",
        platform="onpremise", workload_user="must-not-be-stored", workload_password="secret",
        onpremise_version="7.3.2_plus",
    )

    assert connection["renewal_ready"] is False
    assert connection["renewal_supported"] is False
    assert connection["credential_lifecycle"] == "long_lived_unverified"
    assert connection["has_workload_password"] is False
    assert catalog._connection_record(connection["id"])["workload_user"] == ""
    monkeypatch.setattr("gateway.cloudera.urllib.request.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")))
    try:
        catalog.renew_token(connection["id"], force=True)
    except RuntimeError as exc:
        assert "Configura la URL de renovación" in str(exc)
    else:
        raise AssertionError("Una Knox API key opaca sin IAM no puede regenerarse")


def test_onpremise_732_inference_renews_ums_token_through_private_iam(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    executable = tmp_path / ".venv" / "bin" / "cdp"
    executable.parent.mkdir(parents=True)
    executable.touch()
    catalog = ClouderaCatalog(runtime)
    old = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 60)
    new = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
    connection = catalog.save_connection(
        "Private 7.3.2", "inference", "https://ml.private", old,
        platform="onpremise", cdp_access_key_id="machine-access",
        cdp_private_key="machine-private",
        renewal_url="https://console-cdp.apps.private.example/api/v1",
        onpremise_version="7.3.2", cai_version="1.5.5_sp3",
        onpremise_auth_mode="ums_auto",
    )
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0,
            stdout=json.dumps({"token": new, "expireAt": "2099-01-01T00:00:00Z"}), stderr="")

    monkeypatch.setattr("gateway.cloudera.subprocess.run", run)
    result = catalog.renew_token(connection["id"], force=True)

    assert result["renewed"] is True
    assert connection["renewal_ready"] is True
    assert captured["command"][1:5] == ["--endpoint-url", "https://console-cdp.apps.private.example",
                                        "--form-factor", "private"]
    assert captured["env"]["CDP_ACCESS_KEY_ID"] == "machine-access"
    assert catalog._connection_record(connection["id"])["token"] == new


def test_private_iam_renewal_uses_connection_ca_bundle_without_exposing_it(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    executable = tmp_path / ".venv" / "bin" / "cdp"
    executable.parent.mkdir(parents=True)
    executable.touch()
    certificate, _key = ensure_self_signed_certificate(tmp_path / "certificate")
    ca_pem = certificate.read_text(encoding="utf-8")
    catalog = ClouderaCatalog(runtime)
    new = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
    connection = catalog.save_connection(
        "Private TLS", "inference", "https://ml.private", "",
        platform="onpremise", cdp_access_key_id="machine-access",
        cdp_private_key="machine-private",
        renewal_url="https://console-cdp.apps.private.example",
        onpremise_version="7.3.2", cai_version="1.5.5_sp3",
        onpremise_auth_mode="ums_auto", tls_verification="custom_ca",
        tls_ca_pem=ca_pem,
    )
    captured = {}

    def run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout=json.dumps({"token": new}), stderr="")

    monkeypatch.setattr("gateway.cloudera.subprocess.run", run)
    catalog.renew_token(connection["id"], force=True)

    assert connection["tls_verification"] == "custom_ca"
    assert connection["has_tls_ca"] is True
    assert "tls_ca_pem" not in connection
    assert captured["command"][1] == "--ca-bundle"
    bundle = captured["command"][2]
    assert bundle.endswith(f"{connection['id']}.pem")
    assert Path(bundle).read_text(encoding="utf-8") == ca_pem


def test_discovery_reuses_private_ca_and_relaxes_only_legacy_x509_strictness(tmp_path, monkeypatch):
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
    captured = {}

    class Response(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def accepted(request, timeout=20, context=None):
        captured["context"] = context
        return Response(json.dumps({"endpoints": []}).encode())

    monkeypatch.setattr("gateway.cloudera.urllib.request.urlopen", accepted)
    assert catalog.discover(connection["id"]) == []

    context = captured["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        assert context.verify_flags & strict_flag == 0


def test_private_iam_can_explicitly_disable_tls_only_when_configured(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    executable = tmp_path / ".venv" / "bin" / "cdp"
    executable.parent.mkdir(parents=True)
    executable.touch()
    catalog = ClouderaCatalog(runtime)
    new = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
    connection = catalog.save_connection(
        "Private insecure", "inference", "https://ml.private", "",
        platform="onpremise", cdp_access_key_id="machine-access",
        cdp_private_key="machine-private",
        renewal_url="https://console-cdp.apps.private.example",
        onpremise_version="7.3.2", cai_version="1.5.5_sp3",
        onpremise_auth_mode="ums_auto", tls_verification="disabled",
    )
    captured = {}

    def run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout=json.dumps({"token": new}), stderr="")

    monkeypatch.setattr("gateway.cloudera.subprocess.run", run)
    catalog.renew_token(connection["id"], force=True)

    assert "--no-verify-tls" in captured["command"]


def test_custom_ca_rejects_missing_or_invalid_pem(tmp_path):
    catalog = ClouderaCatalog(tmp_path / "runtime")

    with pytest.raises(RuntimeError, match="certificado raíz/intermedio"):
        catalog.save_connection(
            "Private TLS", "inference", "https://ml.private", "",
            platform="onpremise", cdp_access_key_id="machine-access",
            cdp_private_key="machine-private",
            renewal_url="https://console-cdp.apps.private.example",
            onpremise_version="7.3.2", cai_version="1.5.5_sp3",
            onpremise_auth_mode="ums_auto", tls_verification="custom_ca",
        )


def test_expiring_manual_token_is_reported_without_claiming_renewal(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    token = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
    connection = catalog.save_connection(
        "Private 7.3.2", "workbench", "https://ml.private", token,
        platform="onpremise", onpremise_version="7.3.2_plus",
    )

    assert connection["token_expires_at"]
    assert connection["credential_lifecycle"] == "expiring_manual"
    assert connection["renewal_ready"] is False
    assert connection["rotation_due_at"]


def test_modern_workbench_never_persists_iam_renewal_credentials(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection(
        "Workbench", "workbench", "https://ml.private", "workbench-api-key",
        platform="onpremise", cdp_access_key_id="must-not-be-stored",
        cdp_private_key="must-not-be-stored",
        renewal_url="https://console-cdp.apps.private.example",
        onpremise_version="7.3.2_plus",
        credential_expires_at="2099-12-31",
    )

    record = catalog._connection_record(connection["id"])
    assert connection["renewal_ready"] is False
    assert record["renewal_url"] == ""
    assert record["cdp_access_key_id"] == ""
    assert record["cdp_private_key"] == ""
    assert connection["token_expires_at"].startswith("2099-12-31")
    assert connection["rotation_due_at"]


def test_declared_api_key_expiry_must_be_in_the_future(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    with pytest.raises(RuntimeError, match="debe estar en el futuro"):
        catalog.save_connection(
            "Workbench", "workbench", "https://ml.private", "workbench-api-key",
            platform="onpremise", onpremise_version="7.3.2_plus",
            credential_expires_at="2000-01-01",
        )


def test_renewal_window_is_early_and_bounded_by_token_lifetime(tmp_path, monkeypatch):
    catalog = ClouderaCatalog(tmp_path)
    now = int(datetime.now(timezone.utc).timestamp())
    payload = base64.urlsafe_b64encode(json.dumps({"iat": now, "exp": now + 3600}).encode()).decode().rstrip("=")
    token = f"e30.{payload}.signature"
    connection = catalog.save_connection(
        "Cloud", "inference", "https://ml.example", token, "cloud", 5, "", "",
        "access-id", "private-key", "https://iamapi.us-west-1.altus.cloudera.com", "DE",
    )
    monkeypatch.setenv("CDP_RENEWAL_LEAD_SECONDS", "604800")

    visible = catalog.connections()[0]
    due = datetime.fromisoformat(visible["renewal_due_at"]).timestamp()

    assert 850 <= (now + 3600 - due) <= 950
    result = catalog.renew_token(connection["id"], force=False)
    assert result["renewed"] is False
    assert result["renewal_due_at"] == visible["renewal_due_at"]


def test_expired_model_token_falls_back_to_renewed_connection_token(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    future = jwt_with_exp(int(datetime.now(timezone.utc).timestamp()) + 3600)
    expired = jwt_with_exp(1)
    connection = catalog.save_connection("Inference", "inference", "https://ml.example", future)
    variable = catalog.save_model_token(connection["id"], "chat-model", future)
    data = catalog._read()
    data["model_tokens"][f"{connection['id']}:chat-model"] = expired
    catalog._write(data)

    token, source, metadata = catalog.model_credential(connection["id"], "chat-model")

    assert token == future
    assert "fallback" in source
    assert metadata["token_expired"] is False
    assert catalog.environment()[variable] == future


def test_cloud_declared_expiry_is_used_when_credential_is_opaque(tmp_path):
    catalog = ClouderaCatalog(tmp_path)
    connection = catalog.save_connection("Cloud", "inference", "https://ml.example", "opaque-token")
    data = catalog._read()
    record = next(item for item in data["connections"] if item["id"] == connection["id"])
    record["token_expire_at"] = (datetime.now(timezone.utc).timestamp() + 3600) * 1000
    catalog._write(data)

    visible = catalog.connections()[0]
    assert visible["token_expires_at"]
    assert visible["credential_lifecycle"] == "expiring_manual"


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
    assert captured["command"][2] == "https://iamapi.us-west-1.altus.cloudera.com"
    assert captured["command"][3:5] == ["--form-factor", "public"]
    assert captured["timeout"] == 60
    assert catalog.connections()[0]["renewal_url"] == "https://iamapi.us-west-1.altus.cloudera.com"


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
        assert "iamapi.us-west-1.altus.cloudera.com" in str(exc)
    else:
        raise AssertionError("El timeout del CDP CLI debe convertirse en un error controlado")


"""Isolated tests for CRUD, discovery, authentication, and CDP renewal."""
