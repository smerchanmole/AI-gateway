from pathlib import Path

import yaml

from gateway.core import GatewayManager
from gateway.log_store import insert_log, read_logs


def make_manager(tmp_path: Path) -> GatewayManager:
    (tmp_path / "config.yaml").write_text(
        "model_list:\n"
        "  - model_name: uno\n    litellm_params:\n      model: ollama/uno\n"
        "  - model_name: dos\n    litellm_params:\n      model: ollama/dos\n",
        encoding="utf-8",
    )
    return GatewayManager(tmp_path)


def test_disable_model_filters_runtime_config(tmp_path):
    manager = make_manager(tmp_path)
    manager.set_model("dos", False)
    manager._write_active_config()
    active = yaml.safe_load(manager.active_config.read_text(encoding="utf-8"))
    assert [item["model_name"] for item in active["model_list"]] == ["uno"]
    assert active["litellm_settings"]["callbacks"] == ["litellm_callback.dashboard_logger"]
    assert (manager.runtime_dir / "litellm_callback.py").exists()
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8").count("model_name") == 2


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
