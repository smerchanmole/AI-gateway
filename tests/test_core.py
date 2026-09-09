from pathlib import Path
from types import SimpleNamespace
import asyncio
from datetime import datetime

import yaml

from gateway.core import GatewayManager, environment_port
from datetime import date
import zipfile
import io

from gateway.excel_export import build_logs_xlsx
from gateway.log_store import daily_log_path, insert_log, log_kpis, read_day_logs, read_logs
from gateway.litellm_callback import DashboardLogger


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


def test_cloudera_callback_logs_with_public_alias_after_provider_rewrite(tmp_path, monkeypatch):
    """El modelo estricto enviado a CDP no debe convertirse en la clave del log."""

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
"""Pruebas unitarias de configuración, procesos, observabilidad y exportación."""
