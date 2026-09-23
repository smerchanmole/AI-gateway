from pathlib import Path
from types import SimpleNamespace
import asyncio
from datetime import datetime
import os
import subprocess
import sys

import yaml

from gateway.core import GatewayManager, environment_port
from gateway.cloudera import ClouderaCatalog
from gateway.tls import ensure_self_signed_certificate
from datetime import date
import zipfile
import io

from gateway.excel_export import build_logs_xlsx
from gateway.log_store import daily_log_path, insert_log, log_kpis, log_kpis_for_day, read_day_logs, read_logs
from gateway.litellm_callback import DashboardLogger, _classify_with_guardrail, _configure_private_ca, _origin_ip


def make_manager(tmp_path: Path) -> GatewayManager:
    (tmp_path / "config.yaml").write_text(
        "model_list:\n"
        "  - model_name: uno\n    litellm_params:\n      model: ollama/uno\n"
        "  - model_name: dos\n    litellm_params:\n      model: ollama/dos\n",
        encoding="utf-8",
    )
    return GatewayManager(tmp_path)


def test_cloudera_ports_use_environment_with_local_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("IA_GATEWAY_LITELLM_PORT", raising=False)
    assert GatewayManager(tmp_path).port == 14000
    monkeypatch.setenv("IA_GATEWAY_LITELLM_PORT", "32123")
    assert GatewayManager(tmp_path).port == 32123
    assert environment_port("IA_GATEWAY_LITELLM_PORT", 14000) == 32123


def test_environment_port_rejects_invalid_values(monkeypatch):
    for value in ("texto", "0", "65536"):
        monkeypatch.setenv("IA_GATEWAY_LITELLM_PORT", value)
        try:
            environment_port("IA_GATEWAY_LITELLM_PORT", 14000)
        except RuntimeError as exc:
            assert "IA_GATEWAY_LITELLM_PORT" in str(exc)
        else:
            raise AssertionError(f"Se esperaba rechazo para el puerto {value}")


def test_disable_model_filters_runtime_config(tmp_path):
    manager = make_manager(tmp_path)
    manager.set_model("dos", False)
    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))
    assert [item["model_name"] for item in active["model_list"]] == ["uno"]
    assert active["litellm_settings"]["callbacks"] == ["litellm_callback.dashboard_logger"]
    assert (manager.runtime_dir / "litellm_callback.py").exists()
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8").count("model_name") == 2


def test_cloudera_alias_maps_to_provider_model_for_callback(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0] = {
        "model_name": "nemotron-publico",
        "litellm_params": {
            "model": "openai/nvidia/nemotron-3-super-120b-a12b",
            "api_base": "https://inference.example/v1",
            "api_key": "os.environ/CLOUDERA_DEMO_CDP_TOKEN",
            "extra_body": {"model": "valor-incorrecto"},
        },
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))

    deployment = active["model_list"][0]
    assert deployment["model_name"] == "nemotron-publico"
    assert deployment["litellm_params"]["model"] == "openai/nvidia/nemotron-3-super-120b-a12b"
    assert "extra_body" not in deployment["litellm_params"]
    dashboard = __import__("json").loads(manager.dashboard_settings_file.read_text(encoding="utf-8"))
    assert dashboard["provider_models"]["nemotron-publico"] == "openai/nvidia/nemotron-3-super-120b-a12b"


def test_active_config_preserves_cai_remote_openai_namespace(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0] = {
        "model_name": "gptoss20b",
        "litellm_params": {
            "model": "openai/openai/gpt-oss-20b",
            "api_base": "https://inference.example/endpoints/gptoss20b/v1",
            "api_key": "os.environ/CLOUDERA_DEMO_CDP_TOKEN",
        },
        "model_info": {"dashboard_source": "cloudera", "dashboard_cloudera_kind": "inference"},
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))

    assert active["model_list"][0]["litellm_params"]["model"] == "openai/openai/gpt-oss-20b"


def test_active_config_repairs_old_cai_gpt_oss_draft(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0] = {
        "model_name": "gptoss20b",
        "litellm_params": {
            "model": "openai/gpt-oss-20b",
            "api_base": "https://inference.example/endpoints/gptoss20b/v1",
            "api_key": "os.environ/CLOUDERA_DEMO_CDP_TOKEN",
        },
        "model_info": {"dashboard_source": "cloudera", "dashboard_cloudera_kind": "inference"},
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))

    assert active["model_list"][0]["litellm_params"]["model"] == "openai/openai/gpt-oss-20b"


def test_cloudera_active_config_keeps_supported_vllm_extra_body(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0] = {
        "model_name": "qwen",
        "litellm_params": {
            "model": "openai/Qwen/Qwen3-32B",
            "api_base": "https://inference.example/v1",
            "api_key": "os.environ/CLOUDERA_DEMO_CDP_TOKEN",
            "extra_body": {
                "model": "wrong",
                "top_k": 40,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        },
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))
    assert active["model_list"][0]["litellm_params"]["extra_body"] == {
        "top_k": 40,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_cloudera_active_config_propagates_private_ca_to_litellm(tmp_path):
    manager = make_manager(tmp_path)
    certificate, _key = ensure_self_signed_certificate(tmp_path / "certificate")
    catalog = ClouderaCatalog(manager.runtime_dir)
    connection = catalog.save_connection(
        "Private TLS", "inference", "https://ml.private", "opaque-token",
        platform="onpremise", cdp_access_key_id="machine-access",
        cdp_private_key="machine-private",
        renewal_url="https://console-cdp.apps.private.example",
        onpremise_version="7.3.2", cai_version="1.5.5_sp3",
        onpremise_auth_mode="ums_auto", tls_verification="custom_ca",
        tls_ca_pem=certificate.read_text(encoding="utf-8"),
    )
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0] = {
        "model_name": "private-model",
        "litellm_params": {
            "model": "openai/provider-model",
            "api_base": "https://ml.private/v1",
            "api_key": f"os.environ/{catalog.connection_environment_name(connection['id'])}",
        },
        "model_info": {"dashboard_source": "cloudera", "dashboard_cloudera_kind": "inference"},
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))
    ca_path = Path(active["model_list"][0]["litellm_params"]["ssl_verify"])

    assert ca_path.exists()
    assert ca_path.read_text(encoding="utf-8") == certificate.read_text(encoding="utf-8")


def test_litellm_callback_installs_private_ca_context(tmp_path, monkeypatch):
    import litellm
    import ssl

    certificate, _key = ensure_self_signed_certificate(tmp_path / "certificate")
    ca_dir = tmp_path / "runtime" / "cloudera-ca"
    ca_dir.mkdir(parents=True)
    (ca_dir / "private.pem").write_text(certificate.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr("gateway.litellm_callback._root", lambda: tmp_path)
    monkeypatch.setattr(litellm, "ssl_verify", True)

    _configure_private_ca()

    assert isinstance(litellm.ssl_verify, ssl.SSLContext)
    assert not (litellm.ssl_verify.verify_flags & getattr(ssl, "VERIFY_X509_STRICT", 0))


def test_litellm_process_loads_private_ca_before_http_clients(tmp_path):
    manager = make_manager(tmp_path)
    certificate, _key = ensure_self_signed_certificate(tmp_path / "certificate")
    ca_dir = manager.runtime_dir / "cloudera-ca"
    ca_dir.mkdir(parents=True)
    (ca_dir / "private.pem").write_text(certificate.read_text(encoding="utf-8"), encoding="utf-8")

    manager._write_active_config()
    env = manager._process_environment()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import ssl; "
                "c=ssl.create_default_context(); "
                "stats=c.cert_store_stats(); "
                "print(ssl.create_default_context.__module__); "
                "print(bool(c.verify_flags & getattr(ssl, 'VERIFY_X509_STRICT', 0))); "
                "print(c.verify_mode == ssl.CERT_REQUIRED and c.check_hostname); "
                "print(stats['x509'] > stats['x509_ca'])"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == ["sitecustomize", "False", "True", "True"]
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(manager.runtime_dir), str(tmp_path)]


def test_litellm_process_disables_tls_only_for_configured_cloudera_host(tmp_path):
    manager = make_manager(tmp_path)
    catalog = ClouderaCatalog(manager.runtime_dir)
    connection = catalog.save_connection(
        "Laboratorio", "inference", "https://inference.lab.example", "opaque-token",
        platform="onpremise", onpremise_version="7.3.2", cai_version="1.5.5_sp3",
        onpremise_auth_mode="manual", tls_verification="disabled",
    )
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0] = {
        "model_name": "lab-model",
        "litellm_params": {
            "model": "openai/provider-model",
            "api_base": "https://inference.lab.example/endpoints/model/v1",
            "api_key": f"os.environ/{catalog.connection_environment_name(connection['id'])}",
        },
        "model_info": {"dashboard_source": "cloudera", "dashboard_cloudera_kind": "inference"},
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    manager._write_active_config()
    env = manager._process_environment()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import aiohttp, sitecustomize; "
                "print(sitecustomize._is_insecure_host('https://inference.lab.example/v1')); "
                "print(sitecustomize._is_insecure_host('https://api.openai.com/v1')); "
                "print(aiohttp.ClientSession._request.__module__)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert env["IA_GATEWAY_INSECURE_TLS_HOSTS"] == "inference.lab.example"
    assert result.stdout.splitlines() == ["True", "False", "sitecustomize"]


def test_workbench_model_loads_custom_provider_and_migrates_old_prefix(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0] = {
        "model_name": "qwen38",
        "litellm_params": {
            "model": "custom/qwen3.8-27b-fp8",
            "api_base": "https://modelservice.wb.example/model?accessKey=secret",
        },
        "model_info": {"dashboard_source": "cloudera", "dashboard_cloudera_kind": "workbench"},
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))

    assert active["model_list"][0]["litellm_params"]["model"] == "cloudera_workbench/qwen3.8-27b-fp8"
    assert active["model_list"][0]["litellm_params"]["num_retries"] == 0
    assert active["litellm_settings"]["custom_provider_map"] == [{
        "provider": "cloudera_workbench",
        "custom_handler": "workbench_provider.workbench_llm",
    }]
    assert (manager.runtime_dir / "workbench_provider.py").exists()


def test_cloudera_callback_logs_with_public_alias_after_provider_rewrite(tmp_path, monkeypatch):
    """The strict model sent to CDP must not become the public log key."""

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    from gateway.cloudera import ClouderaCatalog

    catalog = ClouderaCatalog(runtime)
    connection = catalog.save_connection("CDP", "inference", "https://ml.example", "fresh-token")
    variable_name = catalog.connection_environment_name(connection["id"])
    (runtime / "dashboard_settings.json").write_text(
        '{"guardrail":{"enabled":false},'
        '"provider_models":{"nemotron-publico":"openai/nvidia/nemotron-3-super-120b-a12b"},'
        f'"provider_api_key_env":{{"nemotron-publico":"{variable_name}"}}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("IA_GATEWAY_ROOT", str(tmp_path))
    callback = DashboardLogger()
    data = {"model": "nemotron-publico", "messages": [], "metadata": {}}
    rewritten = asyncio.run(callback.async_pre_call_hook(None, None, data, "completion"))
    assert rewritten["model"] == "openai/nvidia/nemotron-3-super-120b-a12b"
    assert rewritten["api_key"] == "fresh-token"

    now = datetime.now().astimezone()
    kwargs = {
        "model": rewritten["model"],
        "messages": [],
        "litellm_params": {"metadata": rewritten["metadata"]},
    }
    asyncio.run(callback.async_log_success_event(kwargs, {"usage": {}}, now, now))
    assert len(read_day_logs(runtime, "nemotron-publico", now.date())) == 1


def test_callback_enforces_model_parameters_and_logs_effective_values(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "dashboard_settings.json").write_text(__import__("json").dumps({
        "guardrail": {"enabled": False},
        "model_parameters": {"qwen": {
            "policy": "model_wins",
            "configured": {"temperature": 0.2, "extra_body": {"top_k": 40}},
        }},
    }), encoding="utf-8")
    monkeypatch.setenv("IA_GATEWAY_ROOT", str(tmp_path))

    callback = DashboardLogger()
    data = {"model": "qwen", "messages": [], "temperature": 1.0,
            "extra_body": {"top_k": 5, "guided_json": {"type": "object"}}}
    rewritten = asyncio.run(callback.async_pre_call_hook(None, None, data, "completion"))

    assert rewritten["temperature"] == 0.2
    assert rewritten["extra_body"] == {"top_k": 40, "guided_json": {"type": "object"}}
    effective = rewritten["metadata"]["dashboard_effective_parameters"]
    assert effective["temperature"] == 0.2
    assert effective["extra_body"]["top_k"] == 40

    now = datetime.now().astimezone()
    asyncio.run(callback.async_log_success_event({
        "model": "qwen", "messages": [], "temperature": rewritten["temperature"],
        "litellm_params": {"metadata": rewritten["metadata"]},
    }, {"usage": {}}, now, now))
    row = read_day_logs(runtime, "qwen", now.date())[0]
    assert row["parameters"]["temperature"] == 0.2
    assert row["parameters"]["extra_body"]["guided_json"] == {"type": "object"}


def test_callback_skips_guardrail_for_explicit_exclusions_and_embeddings(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "dashboard_settings.json").write_text(__import__("json").dumps({
        "guardrail": {
            "enabled": True,
            "model": "guardian",
            "provider_model": "ollama/guardian",
            "api_base": "http://localhost:11434",
            "excluded_models": ["chat-interno"],
        },
    }), encoding="utf-8")
    monkeypatch.setenv("IA_GATEWAY_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "gateway.litellm_callback._classify_with_guardrail",
        lambda *_args: (_ for _ in ()).throw(AssertionError("El guardrail no debe ejecutarse")),
    )
    callback = DashboardLogger()

    excluded = asyncio.run(callback.async_pre_call_hook(
        None, None,
        {"model": "chat-interno", "messages": [{"role": "user", "content": "hola"}]},
        "completion",
    ))
    embedding = asyncio.run(callback.async_pre_call_hook(
        None, None,
        {"model": "vectores", "input": "hola"},
        "embedding",
    ))

    assert "dashboard_guardrail" not in excluded["metadata"]
    assert "dashboard_guardrail" not in embedding["metadata"]


def test_callback_skips_guardrail_for_periodic_health_probe(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "dashboard_settings.json").write_text(__import__("json").dumps({
        "guardrail": {
            "enabled": True,
            "model": "guardian",
            "provider_model": "ollama/guardian",
            "api_base": "http://localhost:11434",
        },
    }), encoding="utf-8")
    monkeypatch.setenv("IA_GATEWAY_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "gateway.litellm_callback._classify_with_guardrail",
        lambda *_args: (_ for _ in ()).throw(AssertionError("La sonda no debe duplicar el guardrail")),
    )

    rewritten = asyncio.run(DashboardLogger().async_pre_call_hook(
        None,
        None,
        {
            "model": "chat",
            "messages": [{"role": "user", "content": "health"}],
            "metadata": {"dashboard_probe": "periodic"},
        },
        "completion",
    ))

    assert rewritten["metadata"]["dashboard_probe"] == "periodic"
    assert "dashboard_guardrail" not in rewritten["metadata"]


def test_origin_ip_prefers_edge_header_over_litellm_loopback():
    kwargs = {"litellm_params": {"metadata": {
        "requester_ip_address": "127.0.0.6",
        "headers": {"X-IA-Gateway-Client-IP": "198.51.100.27"},
    }}}

    assert _origin_ip(kwargs) == "198.51.100.27"


def test_openai_guardrail_uses_chat_completions_and_dynamic_token(monkeypatch):
    captured = {}

    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def open_guardrail(request, timeout=8):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = __import__("json").loads(request.data)
        return Response(b'{"choices":[{"message":{"content":"unsafe\\nS7"}}]}')

    monkeypatch.setattr("gateway.litellm_callback._provider_api_key", lambda alias: "fresh-cdp-token")
    monkeypatch.setattr("gateway.litellm_callback.urllib.request.urlopen", open_guardrail)

    verdict = _classify_with_guardrail({
        "model": "llama-guard-3",
        "protocol": "openai",
        "provider_model": "openai/meta-llama/Llama-Guard-3-8B",
        "api_base": "https://ml.example/openai/v1",
        "timeout": 8,
    }, [{"role": "user", "content": "dame un DNI"}])

    assert verdict == {"status": "warning", "reason": "unsafe\nS7"}
    assert captured["url"] == "https://ml.example/openai/v1/chat/completions"
    assert captured["authorization"] == "Bearer fresh-cdp-token"
    assert captured["payload"]["model"] == "meta-llama/Llama-Guard-3-8B"


def test_process_log_adds_timestamp_and_can_be_cleared(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    path = tmp_path / "litellm.log"
    monkeypatch.setattr(manager, "process_log_path", lambda day=None: path)

    manager._capture_process_output(SimpleNamespace(stdout=["primera linea\n", "segunda linea\n"]))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(line.startswith("[202") for line in lines)
    assert lines[0].endswith("primera linea")
    manager.clear_process_log()
    assert path.read_text(encoding="utf-8") == ""


def test_log_store_redacts_secrets(tmp_path):
    db = tmp_path / "requests.sqlite3"
    insert_log(db, "uno", "success", 12, {"api_key": "secret", "messages": ["hola"]}, {"ok": True})
    logs = read_logs(db, "uno")
    assert logs[0]["request"]["api_key"] == "[OCULTO]"
    assert logs[0]["response"] == {"ok": True}


def test_log_store_compacts_embedding_vectors(tmp_path):
    db = tmp_path / "requests.sqlite3"
    insert_log(db, "embed", "success", 12, {"input": ["hola"]}, {
        "data": [{"embedding": [float(index) for index in range(256)]}],
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    })

    response = read_logs(db, "embed")[0]["response"]
    assert len(response["data"][0]["embedding"]) == 17
    assert response["data"][0]["embedding"][-1] == "[… 240 valores omitidos]"


def test_log_store_keeps_network_and_latency_metadata(tmp_path):
    db = tmp_path / "requests.sqlite3"
    insert_log(
        db, "uno", "success", 240, {"messages": []}, {"ok": True},
        started_at="2026-07-17T10:30:00+02:00", origin_ip="192.168.1.20",
        provider_ip="104.18.6.192", ttft_ms=85,
    )
    log = read_logs(db, "uno")[0]
    assert log["started_at"] == "2026-07-17T10:30:00+02:00"
    assert log["origin_ip"] == "192.168.1.20"
    assert log["provider_ip"] == "104.18.6.192"
    assert log["ttft_ms"] == 85


def test_runtime_config_exposes_parameter_policy_and_typed_extras(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0]["litellm_params"]["temperature"] = 0.3
    config["model_list"][0]["model_info"] = {
        "dashboard_parameter_policy": "model_wins",
        "dashboard_extra_parameters": {"extra_body.guided_json": {"type": "object"}},
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    manager._write_active_config()
    settings = __import__("json").loads(manager.dashboard_settings_file.read_text(encoding="utf-8"))

    assert settings["model_parameters"]["uno"] == {
        "policy": "model_wins",
        "configured": {"temperature": 0.3, "extra_body": {"guided_json": {"type": "object"}}},
        "backend": "auto",
    }


def test_daily_logs_kpis_and_excel_export(tmp_path):
    runtime = tmp_path / "runtime"
    day = date(2026, 7, 17)
    db = daily_log_path(runtime, day)
    insert_log(db, "uno", "success", 200, {"messages": [{"role": "user", "content": "hola"}]},
               {"choices": [{"message": {"content": "respuesta"}}]}, started_at="2026-07-17T08:30:00+02:00", ttft_ms=80)
    insert_log(db, "uno", "error", 500, {"input": "fallo"}, None, "error", started_at="2026-07-17T09:30:00+02:00")
    rows = read_day_logs(runtime, "uno", day)
    kpis = log_kpis(rows)
    assert kpis["requests"] == 2
    assert kpis["success_rate"] == 50.0
    assert kpis["hourly"][8:10] == [1, 1]
    complete_kpis = log_kpis_for_day(runtime, ["uno"], day)
    assert complete_kpis["requests"] == 2
    assert complete_kpis["success_rate"] == 50.0
    assert complete_kpis["p95_duration_ms"] == 500
    assert complete_kpis["hourly"][8:10] == [1, 1]
    content = build_logs_xlsx("uno", day.isoformat(), rows, kpis)
    with zipfile.ZipFile(io.BytesIO(content)) as workbook:
        assert "xl/worksheets/sheet1.xml" in workbook.namelist()


def test_models_detect_embedding_mode(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][1]["model_name"] = "embedding-local"
    config["model_list"][1]["litellm_params"]["model"] = "ollama/bge-m3"
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")
    modes = {item["name"]: item["mode"] for item in manager.models()}
    assert modes == {"uno": "chat", "embedding-local": "embedding"}


def test_models_use_discovered_task_for_embedqa_and_expose_engine(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0] = {
        "model_name": "embedqa-query",
        "litellm_params": {
            "model": "openai/nvidia/llama-3.2-nv-embedqa-1b-v2-query",
            "api_base": "https://ml.example/v1",
        },
        "model_info": {
            "dashboard_source": "cloudera",
            "dashboard_cloudera_kind": "inference",
            "dashboard_serving_engine": "nim",
            "dashboard_task": "EMBED",
            "dashboard_embedding_input_type": "query",
        },
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    model = manager.models()[0]
    assert model["mode"] == "embedding"
    assert model["serving_engine"] == "nim"
    assert model["embedding_input_type"] == "query"


def test_active_config_repairs_encoding_format_for_existing_nim_embedding(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0] = {
        "model_name": "embedqa-passage",
        "litellm_params": {
            "model": "openai/nvidia/llama-3.2-nv-embedqa-1b-v2-passage",
            "api_base": "https://ml.example/v1",
            "api_key": "os.environ/CLOUDERA_ABC_CDP_TOKEN",
        },
        "model_info": {
            "dashboard_source": "cloudera",
            "dashboard_cloudera_kind": "inference",
            "dashboard_serving_engine": "nim",
            "dashboard_task": "EMBED",
            "dashboard_embedding_input_type": "passage",
        },
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))

    assert active["model_list"][0]["litellm_params"]["encoding_format"] == "float"


def test_active_config_repairs_legacy_nim_embedding_by_model_suffix(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0]["litellm_params"] = {
        "model": "openai/nvidia/llama-3.2-nv-embedqa-1b-v2-query",
        "api_base": "https://ml.example/v1",
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))

    assert active["model_list"][0]["litellm_params"]["encoding_format"] == "float"


def test_reports_missing_referenced_environment_variable(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"][0]["litellm_params"]["api_key"] = "os.environ/TEST_OPENAI_KEY"
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.delenv("TEST_OPENAI_KEY", raising=False)
    assert manager.missing_environment_variables() == ["TEST_OPENAI_KEY"]


def test_validation_rejects_external_litellm_database(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.config_text())
    config["general_settings"] = {"database_url": "os.environ/DATABASE_URL"}

    try:
        manager.validate_config_text(yaml.safe_dump(config))
    except RuntimeError as exc:
        assert "únicamente SQLite local" in str(exc)
    else:
        raise AssertionError("Una BBDD externa de LiteLLM no debe estar permitida")


def test_process_environment_removes_implicit_master_key(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "must-not-reach-proxy")
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-reach-proxy")

    environment = manager._process_environment()
    assert "LITELLM_MASTER_KEY" not in environment
    assert "DATABASE_URL" not in environment
    assert environment["LITELLM_MODE"] == "PRODUCTION"


def test_process_environment_removes_explicit_legacy_master_key(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["general_settings"] = {"master_key": "os.environ/LITELLM_MASTER_KEY"}
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "explicit-key")

    assert "LITELLM_MASTER_KEY" not in manager._process_environment()


def test_missing_master_key_is_optional_and_removed_from_active_config(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.config_text())
    config["general_settings"] = {"master_key": "os.environ/LITELLM_MASTER_KEY"}
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)

    assert "LITELLM_MASTER_KEY" not in manager.missing_environment_variables()
    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))
    assert "master_key" not in active.get("general_settings", {})


def test_disabled_model_does_not_require_its_provider_api_key(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.config_text())
    config["model_list"][0]["litellm_params"]["api_key"] = "os.environ/OPENAI_API_KEY"
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    manager.set_model("uno", False)

    assert "OPENAI_API_KEY" not in manager.missing_environment_variables()


def test_local_ollama_reports_host_free_memory(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)

    class Memory:
        total = 1000
        available = 375

    monkeypatch.setattr("gateway.core.psutil.virtual_memory", lambda: Memory())

    assert manager._host_memory_free_percent("http://localhost:11434") == 37.5
    assert manager._host_memory_free_percent("http://127.0.0.1:11434") == 37.5


def test_remote_ollama_does_not_report_dashboard_memory(tmp_path):
    manager = make_manager(tmp_path)

    assert manager._host_memory_free_percent("http://ollama.example:11434") is None


def test_update_config_rejects_invalid_yaml_without_touching_source(tmp_path):
    manager = make_manager(tmp_path)
    previous = manager.config_text()

    try:
        manager.update_config("model_list: [")
    except RuntimeError as exc:
        assert "YAML no válido" in str(exc)
    else:
        raise AssertionError("Se esperaba rechazo del YAML inválido")

    assert manager.config_text() == previous


def test_add_model_updates_yaml_and_rejects_duplicate_alias(tmp_path):
    manager = make_manager(tmp_path)
    entry = {
        "model_name": "tres",
        "litellm_params": {"model": "ollama/tres", "api_base": "http://localhost:11434"},
    }

    result = manager.add_model(entry)
    updated = yaml.safe_load(result["content"])

    assert [item["model_name"] for item in updated["model_list"]] == ["uno", "dos", "tres"]
    assert result["restarted"] is False
    try:
        manager.add_model(entry)
    except RuntimeError as exc:
        assert "Ya existe" in str(exc)
    else:
        raise AssertionError("Se esperaba rechazo del alias duplicado")


def test_add_model_can_register_fallback_and_pending_restart(tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    monkeypatch.setattr(manager, "process_alive", lambda: True)
    result = manager.add_model(
        {"model_name": "tres", "litellm_params": {"model": "ollama/tres"}},
        fallback_model="dos", restart=False,
    )
    config = yaml.safe_load(result["content"])
    assert config["router_settings"]["fallbacks"] == [{"tres": ["dos"]}]
    assert result["restart_pending"] is True


def test_config_rejects_fallback_between_embedding_and_chat_models(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.config_text())
    config["model_list"][0]["model_name"] = "embedding-local"
    config["model_list"][0]["litellm_params"]["model"] = "ollama/bge-m3:latest"
    config["router_settings"] = {"fallbacks": [{"embedding-local": ["dos"]}]}

    try:
        manager.validate_config_text(yaml.safe_dump(config))
    except RuntimeError as exc:
        assert "chat y embeddings" in str(exc)
    else:
        raise AssertionError("Se esperaba rechazo del fallback entre APIs incompatibles")


def test_dashboard_settings_are_not_forwarded_to_litellm(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.config_text())
    config["dashboard_settings"] = {"guardrail": {"enabled": True, "model": "dos", "policy": "warn"}}
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")
    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))
    settings = __import__("json").loads(manager.dashboard_settings_file.read_text(encoding="utf-8"))
    assert "dashboard_settings" not in active
    assert settings["guardrail"]["provider_model"] == "dos"


def test_embeddings_are_automatically_excluded_from_guardrail(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.config_text())
    config["model_list"][0] = {
        "model_name": "vectores",
        "litellm_params": {"model": "ollama/bge-m3:latest"},
    }
    config["dashboard_settings"] = {
        "guardrail": {"enabled": True, "model": "dos", "policy": "warn", "excluded_models": []},
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    manager._write_active_config()
    settings = __import__("json").loads(manager.dashboard_settings_file.read_text(encoding="utf-8"))

    assert settings["guardrail"]["excluded_models"] == ["vectores"]


def test_sqlite_mode_maps_dynamic_credentials_and_removes_external_database(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.config_text())
    config["model_list"][0]["litellm_params"] = {
        "model": "openai/modelo-cloudera",
        "api_key": "os.environ/CLOUDERA_ABC_CDP_TOKEN",
    }
    config["general_settings"] = {
        "database_url": "postgresql://usuario:secreto@db.example/litellm"
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")
    manager._write_active_config()

    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))
    params = active["model_list"][0]["litellm_params"]
    assert params["api_key"] == "os.environ/CLOUDERA_ABC_CDP_TOKEN"
    assert "database_url" not in active.get("general_settings", {})
    dashboard = __import__("json").loads(manager.dashboard_settings_file.read_text(encoding="utf-8"))
    assert dashboard["provider_api_key_env"]["uno"] == "CLOUDERA_ABC_CDP_TOKEN"


def test_cloudera_credential_sync_is_dynamic_without_external_database(tmp_path):
    manager = make_manager(tmp_path)
    assert manager.sync_cloudera_credentials() is True

    persistence = manager.persistence_status()
    assert persistence == {
        "credential_store": "SQLite",
        "token_updates_dynamic": True,
        "token_restart_required": False,
    }


def test_active_models_come_from_runtime_config_not_pending_yaml(tmp_path):
    manager = make_manager(tmp_path)
    manager._write_active_config()
    config = yaml.safe_load(manager.config_text())
    config["model_list"].append({
        "model_name": "pendiente",
        "litellm_params": {"model": "ollama/pendiente"},
    })
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    assert manager.active_model_names() == ["uno", "dos"]


def test_guardrail_without_selected_model_is_disabled(tmp_path):
    manager = make_manager(tmp_path)
    result = manager.set_guardrail(True, "", policy="warn", restart=False)
    updated = yaml.safe_load(result["content"])
    assert updated["dashboard_settings"]["guardrail"]["enabled"] is False
    assert updated["dashboard_settings"]["guardrail"]["model"] == ""
    manager._write_active_config()
    settings = __import__("json").loads(manager.dashboard_settings_file.read_text(encoding="utf-8"))
    assert settings["guardrail"]["enabled"] is False
    assert "provider_model" not in settings["guardrail"]


def test_advisor_selection_coexists_with_guardrail(tmp_path):
    manager = make_manager(tmp_path)
    manager.set_advisor(True, "uno", restart=False)
    manager.set_guardrail(True, "dos", policy="warn", restart=False)

    dashboard = manager.dashboard_settings()
    assert dashboard["advisor"] == {"enabled": True, "model": "uno", "timeout": 120}
    assert dashboard["guardrail"]["model"] == "dos"


def test_guardrail_exclusions_follow_rename_and_delete(tmp_path):
    manager = make_manager(tmp_path)
    manager.set_guardrail(True, "dos", policy="warn", excluded_models=["uno"], restart=False)

    renamed = manager.update_model(
        "uno", {"model_name": "nuevo", "litellm_params": {"model": "ollama/nuevo"}}, restart=False,
    )
    config = yaml.safe_load(renamed["content"])
    assert config["dashboard_settings"]["guardrail"]["excluded_models"] == ["nuevo"]

    deleted = manager.delete_model("nuevo", restart=False)
    config = yaml.safe_load(deleted["content"])
    assert config["dashboard_settings"]["guardrail"]["excluded_models"] == []


def test_advanced_yaml_rejects_unvalidated_model_changes(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.source_config.read_text(encoding="utf-8"))
    config["model_list"].append({
        "model_name": "sin-probar",
        "litellm_params": {"model": "openai/modelo"},
    })

    try:
        manager.validate_model_activation_changes(yaml.safe_dump(config))
    except RuntimeError as exc:
        assert "probar todos sus parámetros" in str(exc)
    else:
        raise AssertionError("Un modelo nuevo sin prueba no debe poder activarse desde YAML")


def test_cloudera_model_origin_metadata_is_exposed(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.config_text())
    config["model_list"][0]["model_info"] = {
        "dashboard_source": "cloudera", "dashboard_cloudera_kind": "workbench",
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")
    model = manager.models()[0]
    assert model["source"] == "cloudera"
    assert model["cloudera_kind"] == "workbench"


def test_modelservice_hostname_is_identified_as_workbench(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.config_text())
    config["model_list"][0]["litellm_params"] = {
        "model": "cloudera_workbench/qwen38",
        "api_base": "https://modelservice.wb.cloudera.site/model?accessKey=secret",
    }
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")

    model = manager.models()[0]
    assert model["source"] == "cloudera"
    assert model["cloudera_kind"] == "workbench"


def test_update_model_renames_references_and_preserves_advanced_params(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.config_text())
    config["model_list"][0]["litellm_params"]["temperature"] = 0.2
    config["router_settings"] = {"fallbacks": [{"dos": ["uno"]}]}
    config["dashboard_settings"] = {"guardrail": {"enabled": True, "model": "uno", "policy": "warn"}}
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")
    result = manager.update_model("uno", {"model_name": "nuevo", "litellm_params": {"model": "openai/nuevo"}}, "dos")
    updated = yaml.safe_load(result["content"])
    assert updated["model_list"][0]["litellm_params"]["temperature"] == 0.2
    assert updated["dashboard_settings"]["guardrail"]["model"] == "nuevo"
    assert {"dos": ["nuevo"]} in updated["router_settings"]["fallbacks"]
    assert {"nuevo": ["dos"]} in updated["router_settings"]["fallbacks"]


def test_delete_model_cleans_references_and_disables_guardrail(tmp_path):
    manager = make_manager(tmp_path)
    config = yaml.safe_load(manager.config_text())
    config["router_settings"] = {"fallbacks": [{"uno": ["dos"]}]}
    config["dashboard_settings"] = {"guardrail": {"enabled": True, "model": "dos", "policy": "block"}}
    manager.source_config.write_text(yaml.safe_dump(config), encoding="utf-8")
    result = manager.delete_model("dos")
    updated = yaml.safe_load(result["content"])
    assert [item["model_name"] for item in updated["model_list"]] == ["uno"]
    assert updated["router_settings"].get("fallbacks") is None
    assert updated["dashboard_settings"]["guardrail"]["enabled"] is False
"""Unit tests for configuration, processes, observability, and export."""
