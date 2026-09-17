"""Gateway operational domain: configuration, processes, health, and metrics.

La idea pedagógica importante es separar *estar ejecutándose* de *estar listo*.
Un proceso puede conservar un PID después de que su servidor haya fallado. Por
eso esta clase exige dos señales: proceso vivo y puerto accesible.
"""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

import yaml
import psutil


def environment_port(name: str, default: int) -> int:
    """Read an environment port with explicit validation and a local fallback."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} debe contener un puerto numérico válido") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"{name} debe estar entre 1 y 65535")
    return port


class GatewayManager:
    """Orchestrate LiteLLM while keeping ``config.yaml`` as the immutable source of truth."""

    def __init__(self, root: Path) -> None:
        """Centralize every generated path under ``runtime/``."""
        self.root = root.resolve()
        self.source_config = self.root / "config.yaml"
        self.runtime_dir = self.root / "runtime"
        self.active_config = self.runtime_dir / "active_config.yaml"
        self.state_file = self.runtime_dir / "state.json"
        self.pid_file = self.runtime_dir / "litellm.pid"
        self.output_log = self.runtime_dir / "litellm-process.log"
        self.dashboard_settings_file = self.runtime_dir / "dashboard_settings.json"
        self.restart_pending_file = self.runtime_dir / "config-restart-pending"
        self.host = "127.0.0.1"
        # LiteLLM never binds the port published by Cloudera. The proxy owns
        # that single port and forwards /v1 directly to this internal upstream.
        self.port = environment_port("IA_GATEWAY_LITELLM_PORT", 14000)
        self._metric_processes: dict[int, psutil.Process] = {}
        self._ollama_metric_processes: dict[int, psutil.Process] = {}
        self._config_lock = threading.RLock()
        self.runtime_dir.mkdir(exist_ok=True)

    def process_log_path(self, day: date | None = None) -> Path:
        """Select the daily technical log using the Europe/Madrid calendar."""

        selected = day or datetime.now(ZoneInfo("Europe/Madrid")).date()
        path = self.runtime_dir / "logs" / f"litellm-{selected.isoformat()}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def clear_process_log(self, day: date | None = None) -> None:
        """Clear only the selected day without touching model request logs."""
        self.process_log_path(day).write_text("", encoding="utf-8")

    def _capture_process_output(self, process: subprocess.Popen) -> None:
        """Prefix every LiteLLM line with its Europe/Madrid time."""
        if process.stdout is None:
            return
        with self.process_log_path().open("a", encoding="utf-8") as output:
            for line in process.stdout:
                stamp = datetime.now(ZoneInfo("Europe/Madrid")).isoformat(timespec="milliseconds")
                output.write(f"[{stamp}] {line}")
                output.flush()

    def _source(self) -> dict[str, Any]:
        """Load and validate the minimum shape of the user-editable YAML contract."""
        if not self.source_config.exists():
            raise RuntimeError(f"No existe {self.source_config}")
        data = yaml.safe_load(self.source_config.read_text(encoding="utf-8")) or {}
        if not isinstance(data.get("model_list", []), list):
            raise RuntimeError("config.yaml debe contener una lista 'model_list'")
        return data

    @staticmethod
    def _validate_config(data: Any) -> dict[str, Any]:
        """Validate the editable contract before changing the source of truth."""
        if not isinstance(data, dict):
            raise RuntimeError("El YAML debe contener un objeto en el nivel raíz")
        model_list = data.get("model_list")
        if not isinstance(model_list, list):
            raise RuntimeError("El YAML debe contener una lista 'model_list'")
        general_settings = data.get("general_settings") or {}
        if isinstance(general_settings, dict) and general_settings.get("database_url"):
            raise RuntimeError(
                "'general_settings.database_url' no está soportado: "
                "IA Gateway usa únicamente SQLite local"
            )
        names: list[str] = []
        for position, entry in enumerate(model_list, start=1):
            if not isinstance(entry, dict):
                raise RuntimeError(f"El modelo {position} debe ser un objeto YAML")
            name = str(entry.get("model_name") or "").strip()
            params = entry.get("litellm_params")
            if not name:
                raise RuntimeError(f"El modelo {position} no tiene 'model_name'")
            if not isinstance(params, dict) or not str(params.get("model") or "").strip():
                raise RuntimeError(f"El modelo '{name}' necesita 'litellm_params.model'")
            names.append(name)
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise RuntimeError(f"Alias duplicados: {', '.join(duplicates)}")
        for mapping in (data.get("router_settings") or {}).get("fallbacks", []):
            if not isinstance(mapping, dict):
                raise RuntimeError("Cada fallback debe ser un mapa alias: [alternativas]")
            for source, alternatives in mapping.items():
                if source not in names or not isinstance(alternatives, list) or any(item not in names for item in alternatives):
                    raise RuntimeError(f"Fallback no válido para '{source}': usa alias existentes")
        guardrail = (data.get("dashboard_settings") or {}).get("guardrail", {})
        if guardrail.get("enabled") and guardrail.get("model") not in names:
            raise RuntimeError("El modelo guardrail debe ser un alias existente")
        exclusions = guardrail.get("excluded_models") or []
        if (not isinstance(exclusions, list)
                or any(not isinstance(name, str) or name not in names for name in exclusions)):
            raise RuntimeError("Las exclusiones del guardrail deben usar alias existentes")
        advisor = (data.get("dashboard_settings") or {}).get("advisor", {})
        if advisor.get("enabled") and advisor.get("model") not in names:
            raise RuntimeError("El modelo asesor debe ser un alias existente")
        return data

    def validate_config_text(self, content: str) -> dict[str, Any]:
        """Analyze YAML without changing it and summarize what would be applied."""
        if len(content.encode("utf-8")) > 1_000_000:
            raise RuntimeError("El YAML no puede superar 1 MB")
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise RuntimeError(f"YAML no válido: {exc}") from exc
        config = self._validate_config(parsed)
        referenced: set[str] = set()

        def visit(value: Any) -> None:
            """Walk YAML containers and collect environment references."""

            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str) and value.startswith("os.environ/"):
                referenced.add(value.removeprefix("os.environ/"))

        visit(config)
        names = [str(item["model_name"]) for item in config["model_list"]]
        from gateway.cloudera import ClouderaCatalog
        local_credentials = ClouderaCatalog(self.runtime_dir).environment()
        return {"valid": True, "model_count": len(names), "models": names,
                "missing_environment_variables": sorted(v for v in referenced if not os.environ.get(v) and not local_credentials.get(v))}

    @staticmethod
    def _activation_fingerprint(entry: dict[str, Any]) -> str:
        """Build a stable signature of everything that can alter inference."""

        clean = json.loads(json.dumps(entry, ensure_ascii=False, default=str))
        info = clean.get("model_info") or {}
        info.pop("dashboard_validated_at", None)
        info.pop("dashboard_validation_fingerprint", None)
        encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    def validate_model_activation_changes(self, content: str) -> None:
        """Prevent the YAML editor from activating new or changed models without a probe."""

        try:
            candidate = self._validate_config(yaml.safe_load(content))
        except yaml.YAMLError as exc:
            raise RuntimeError(f"YAML no válido: {exc}") from exc
        current = {str(item.get("model_name")): item for item in self._source().get("model_list", [])}
        for entry in candidate.get("model_list", []):
            name = str(entry.get("model_name") or "")
            previous = current.get(name)
            if previous and self._activation_fingerprint(previous) == self._activation_fingerprint(entry):
                continue
            info = entry.get("model_info") or {}
            fingerprint = str(info.get("dashboard_validation_fingerprint") or "")
            if not fingerprint or not hmac.compare_digest(fingerprint, self._activation_fingerprint(entry)):
                raise RuntimeError(
                    f"El modelo '{name}' es nuevo o ha cambiado. Usa el formulario guiado para "
                    "probar todos sus parámetros antes de activarlo"
                )

    def config_text(self) -> str:
        """Return source YAML, preserving comments and manual formatting."""
        return self.source_config.read_text(encoding="utf-8")

    def dashboard_settings(self) -> dict[str, Any]:
        """Expose only dashboard-specific options retained in the source YAML."""

        return dict(self._source().get("dashboard_settings") or {})

    def set_guardrail(self, enabled: bool, model: str, policy: str = "warn",
                      excluded_models: list[str] | None = None,
                      restart: bool = True) -> dict[str, Any]:
        """Configure the global filter through the same transactional update path."""

        with self._config_lock:
            config = self._source()
            enabled = bool(enabled and model)
            if not enabled:
                model = ""
            names = {str(item.get("model_name")) for item in config.get("model_list", [])}
            if enabled and model not in names:
                raise RuntimeError("Selecciona un modelo guardrail existente")
            if policy not in {"warn", "block"}:
                raise RuntimeError("La política del guardrail debe ser permisiva o restringida")
            exclusions = list(dict.fromkeys(
                str(name).strip() for name in (excluded_models or []) if str(name).strip()
            ))
            unknown = [name for name in exclusions if name not in names]
            if unknown:
                raise RuntimeError(f"Exclusiones de guardrail no encontradas: {', '.join(unknown)}")
            exclusions = [name for name in exclusions if name != model]
            dashboard = dict(config.get("dashboard_settings") or {})
            dashboard["guardrail"] = {
                "enabled": enabled, "model": model, "policy": policy, "timeout": 8,
                "excluded_models": exclusions,
            }
            config["dashboard_settings"] = dashboard
            return self.update_config(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), restart=restart)

    def set_advisor(self, enabled: bool, model: str, restart: bool = True) -> dict[str, Any]:
        """Select the model that recommends parameters in the dashboard."""

        with self._config_lock:
            config = self._source()
            enabled = bool(enabled and model)
            if not enabled:
                model = ""
            names = {str(item.get("model_name")) for item in config.get("model_list", [])}
            if enabled and model not in names:
                raise RuntimeError("Selecciona un modelo asesor existente")
            dashboard = dict(config.get("dashboard_settings") or {})
            dashboard["advisor"] = {"enabled": enabled, "model": model, "timeout": 120}
            config["dashboard_settings"] = dashboard
            return self.update_config(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), restart=restart)

    def update_config(self, content: str, restart: bool = True) -> dict[str, Any]:
        """Validate, replace atomically, and restart with rollback on failure."""
        self.validate_config_text(content)

        with self._config_lock:
            previous = self.config_text()
            was_running = self.process_alive()
            if was_running and restart:
                self.stop()
            temp = self.source_config.with_suffix(".yaml.tmp")
            temp.write_text(content.rstrip() + "\n", encoding="utf-8")
            temp.replace(self.source_config)
            try:
                if was_running and restart:
                    self.start()
            except RuntimeError as exc:
                temp.write_text(previous, encoding="utf-8")
                temp.replace(self.source_config)
                recovery = ""
                try:
                    self.start()
                except RuntimeError:
                    recovery = " No se pudo recuperar automáticamente el gateway anterior."
                raise RuntimeError(f"La configuración fue rechazada y se restauró la anterior.{recovery} {exc}") from exc
            if was_running and not restart:
                self.restart_pending_file.touch()
            elif restart:
                self.restart_pending_file.unlink(missing_ok=True)
            return {"content": self.config_text(), "restarted": was_running and restart,
                    "restart_pending": self.restart_pending_file.exists()}

    def add_model(self, entry: dict[str, Any], fallback_model: str = "", restart: bool = True) -> dict[str, Any]:
        """Add a model through the editor's same transactional path."""
        with self._config_lock:
            config = self._source()
            entry = dict(entry)
            entry.pop("_dashboard_managed_parameters", None)
            name = str(entry.get("model_name") or "").strip()
            if any(str(item.get("model_name")) == name for item in config.get("model_list", [])):
                raise RuntimeError(f"Ya existe un modelo con el alias '{name}'")
            config.setdefault("model_list", []).append(entry)
            if fallback_model:
                names = {str(item.get("model_name")) for item in config["model_list"]}
                if fallback_model not in names or fallback_model == name:
                    raise RuntimeError("El fallback debe ser otro alias existente")
                config.setdefault("router_settings", {}).setdefault("fallbacks", []).append({name: [fallback_model]})
            content = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
            return self.update_config(content, restart=restart)

    @staticmethod
    def _set_fallback(config: dict[str, Any], source: str, target: str) -> None:
        """Replace one alias fallback without changing other models."""
        router = config.setdefault("router_settings", {})
        existing = router.get("fallbacks", [])
        router["fallbacks"] = [item for item in existing if not (isinstance(item, dict) and source in item)]
        if target:
            router["fallbacks"].append({source: [target]})
        if not router["fallbacks"]:
            router.pop("fallbacks", None)
        if not router:
            config.pop("router_settings", None)

    def update_model(self, current_name: str, entry: dict[str, Any], fallback_model: str = "",
                     restart: bool = True) -> dict[str, Any]:
        """Update a model and repair every reference when its alias changes."""
        with self._config_lock:
            entry = dict(entry)
            managed_update = bool(entry.pop("_dashboard_managed_parameters", False))
            config = self._source()
            models = config.get("model_list", [])
            index = next((i for i, item in enumerate(models) if str(item.get("model_name")) == current_name), None)
            if index is None:
                raise KeyError(current_name)
            new_name = str(entry.get("model_name") or "").strip()
            if new_name != current_name and any(str(item.get("model_name")) == new_name for item in models):
                raise RuntimeError(f"Ya existe un modelo con el alias '{new_name}'")
            old_params = dict(models[index].get("litellm_params") or {})
            new_params = dict(entry.get("litellm_params") or {})
            if "api_key" not in new_params and old_params.get("api_key"):
                new_params["api_key"] = old_params["api_key"]
            if managed_update:
                models[index] = {"model_name": new_name, "litellm_params": new_params}
            else:
                managed = {"model", "api_base", "api_key", "reasoning_effort", "keep_alive", "timeout",
                           "drop_params", "encoding_format"}
                models[index] = {**models[index], "model_name": new_name,
                                 "litellm_params": {**{k: v for k, v in old_params.items() if k not in managed}, **new_params}}
            if managed_update or entry.get("model_info"):
                old_info = dict(models[index].get("model_info") or {})
                new_info = dict(entry.get("model_info") or {})
                managed_info = {
                    "dashboard_backend_profile", "dashboard_compatibility_profile",
                    "dashboard_reasoning_mode", "dashboard_preserve_thinking",
                    "dashboard_parameter_policy", "dashboard_extra_parameters",
                    "dashboard_validated_at", "dashboard_validation_fingerprint",
                    "dashboard_context_window", "max_input_tokens", "max_output_tokens",
                    "supports_reasoning", "dashboard_source", "dashboard_cloudera_kind",
                    "dashboard_serving_engine", "dashboard_task", "dashboard_embedding_input_type",
                } if managed_update else set(new_info)
                merged_info = (new_info if managed_update else
                               {**{k: v for k, v in old_info.items() if k not in managed_info}, **new_info})
                if merged_info:
                    models[index]["model_info"] = merged_info
                else:
                    models[index].pop("model_info", None)
            router = config.get("router_settings") or {}
            for mapping in router.get("fallbacks", []):
                if isinstance(mapping, dict):
                    if current_name in mapping and new_name != current_name:
                        mapping[new_name] = mapping.pop(current_name)
                    for source, targets in mapping.items():
                        if isinstance(targets, list):
                            mapping[source] = [new_name if target == current_name else target for target in targets]
            self._set_fallback(config, new_name, fallback_model)
            guardrail = (config.get("dashboard_settings") or {}).get("guardrail", {})
            if guardrail.get("model") == current_name:
                guardrail["model"] = new_name
            guardrail["excluded_models"] = [
                new_name if name == current_name else name
                for name in (guardrail.get("excluded_models") or [])
            ]
            advisor = (config.get("dashboard_settings") or {}).get("advisor", {})
            if advisor.get("model") == current_name:
                advisor["model"] = new_name
            disabled = set(self._state().get("disabled_models", []))
            if current_name in disabled:
                disabled.remove(current_name); disabled.add(new_name); self._write_state(disabled)
            return self.update_config(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), restart=restart)

    def delete_model(self, name: str, restart: bool = True) -> dict[str, Any]:
        """Delete an alias and clear associated fallbacks, guardrail, and state."""
        with self._config_lock:
            config = self._source()
            before = len(config.get("model_list", []))
            config["model_list"] = [item for item in config.get("model_list", []) if str(item.get("model_name")) != name]
            if len(config["model_list"]) == before:
                raise KeyError(name)
            router = config.get("router_settings") or {}
            cleaned = []
            for mapping in router.get("fallbacks", []):
                if not isinstance(mapping, dict) or name in mapping:
                    continue
                revised = {source: [target for target in targets if target != name]
                           for source, targets in mapping.items() if isinstance(targets, list)}
                cleaned.extend([{source: targets} for source, targets in revised.items() if targets])
            if cleaned:
                router["fallbacks"] = cleaned
            else:
                router.pop("fallbacks", None)
            guardrail = (config.get("dashboard_settings") or {}).get("guardrail", {})
            if guardrail.get("model") == name:
                guardrail.update({"enabled": False, "model": ""})
            guardrail["excluded_models"] = [
                item for item in (guardrail.get("excluded_models") or []) if item != name
            ]
            advisor = (config.get("dashboard_settings") or {}).get("advisor", {})
            if advisor.get("model") == name:
                advisor.update({"enabled": False, "model": ""})
            disabled = set(self._state().get("disabled_models", [])); disabled.discard(name); self._write_state(disabled)
            return self.update_config(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), restart=restart)

    def apply_pending_config(self) -> dict[str, Any]:
        """Explicitly restart a saved configuration that has not been applied."""
        with self._config_lock:
            if self.process_alive():
                self.stop()
                self.start()
            self.restart_pending_file.unlink(missing_ok=True)
            return self.status()

    def _state(self) -> dict[str, Any]:
        """Read ephemeral state; corruption falls back to a safe empty state."""
        if not self.state_file.exists():
            return {"disabled_models": []}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"disabled_models": []}
        except (json.JSONDecodeError, OSError):
            return {"disabled_models": []}

    def models(self) -> list[dict[str, Any]]:
        """Project YAML into UI data without exposing sensitive parameters."""
        disabled = set(self._state().get("disabled_models", []))
        source = self._source()
        fallback_map: dict[str, list[str]] = {}
        for mapping in (source.get("router_settings") or {}).get("fallbacks", []):
            if isinstance(mapping, dict):
                fallback_map.update({str(k): [str(v) for v in values] for k, values in mapping.items() if isinstance(values, list)})
        # Link each API base to its Cloudera connection so the inventory retains
        # provenance when an endpoint is converted into a LiteLLM deployment.
        from gateway.cloudera import ClouderaCatalog
        try:
            connections = ClouderaCatalog(self.runtime_dir).connections()
        except RuntimeError:
            # A damaged optional catalog must not hide the YAML inventory.
            connections = []
        cloudera_origins = []
        for connection in connections:
            parsed = urlparse(str(connection.get("url") or ""))
            if parsed.hostname:
                cloudera_origins.append((parsed.hostname.lower(), connection.get("kind", "inference")))
        result = []
        for entry in source.get("model_list", []):
            name = str(entry.get("model_name", "sin-nombre"))
            params = entry.get("litellm_params") or {}
            model_info = entry.get("model_info") or {}
            provider_model = str(params.get("model", ""))
            # LiteLLM supports many endpoints. This UI needs only an explicit
            # heuristic that separates known local embeddings from chat models.
            searchable_name = f"{name} {provider_model} {model_info.get('dashboard_task', '')}".lower()
            api_base = str(params.get("api_base", ""))
            api_hostname = (urlparse(api_base).hostname or "").lower()
            cloudera_kind = str(model_info.get("dashboard_cloudera_kind") or "")
            if not cloudera_kind:
                cloudera_kind = next((kind for hostname, kind in cloudera_origins if hostname == api_hostname), "")
            is_cloudera = model_info.get("dashboard_source") == "cloudera" or bool(cloudera_kind) or "cloudera.site" in api_hostname or str(params.get("api_key", "")).startswith("os.environ/CLOUDERA_")
            if is_cloudera and not cloudera_kind:
                cloudera_kind = "workbench" if (
                    api_hostname.startswith("modelservice.")
                    or provider_model.startswith("cloudera_workbench/")
                    or any(marker in api_base.lower() for marker in ("/model-deployments", "/api/v2"))
                ) else "inference"
            result.append({
                "name": name,
                "provider_model": provider_model,
                "api_base": params.get("api_base", ""),
                "api_key": (params.get("api_key", "") if str(params.get("api_key", "")).startswith("os.environ/")
                            else ("Configurada (oculta)" if params.get("api_key") else "")),
                "backend_profile": str(model_info.get("dashboard_backend_profile") or "auto"),
                "compatibility_profile": str(model_info.get("dashboard_compatibility_profile") or "auto"),
                "context_window": model_info.get("dashboard_context_window", ""),
                "max_input_tokens": model_info.get("max_input_tokens", ""),
                "max_output_tokens": model_info.get("max_output_tokens", ""),
                "default_max_tokens": params.get("max_tokens", ""),
                "temperature": params.get("temperature", ""),
                "top_p": params.get("top_p", ""),
                "top_k": (params.get("extra_body") or {}).get("top_k", ""),
                "min_p": (params.get("extra_body") or {}).get("min_p", ""),
                "repetition_penalty": (params.get("extra_body") or {}).get("repetition_penalty", ""),
                "frequency_penalty": params.get("frequency_penalty", ""),
                "presence_penalty": params.get("presence_penalty", ""),
                "seed": params.get("seed", ""),
                "stop": params.get("stop", []),
                "reasoning_mode": str(model_info.get("dashboard_reasoning_mode") or "auto"),
                "reasoning_effort": params.get("reasoning_effort", ""),
                "preserve_thinking": bool(model_info.get("dashboard_preserve_thinking", False)),
                "num_retries": params.get("num_retries", ""),
                "max_parallel_requests": params.get("max_parallel_requests", ""),
                "keep_alive": params.get("keep_alive", ""),
                "timeout": params.get("timeout", ""),
                "drop_params": bool(params.get("drop_params", False)),
                "parameter_policy": str(model_info.get("dashboard_parameter_policy") or "caller_wins"),
                "extra_parameters": dict(model_info.get("dashboard_extra_parameters") or {}),
                "validated_at": str(model_info.get("dashboard_validated_at") or ""),
                "fallbacks": fallback_map.get(name, []),
                "enabled": name not in disabled,
                "mode": "embedding" if "embed" in searchable_name or "bge-" in searchable_name else "chat",
                "source": "cloudera" if is_cloudera else ("ollama" if provider_model.startswith("ollama/") else "remote"),
                "cloudera_kind": cloudera_kind,
                "serving_engine": str(model_info.get("dashboard_serving_engine") or ""),
                "task": str(model_info.get("dashboard_task") or ""),
                "embedding_input_type": str(model_info.get("dashboard_embedding_input_type") or ""),
            })
        return result

    def _ollama_cpu_percent(self) -> float | None:
        """Measure shared Ollama CPU as a percentage of the entire machine."""
        try:
            candidates = []
            for process in psutil.process_iter(["pid", "name"]):
                if "ollama" in (process.info.get("name") or "").lower():
                    candidates.append(process)
            live_pids = {process.pid for process in candidates}
            cpu = 0.0
            for process in candidates:
                tracked = self._ollama_metric_processes.setdefault(process.pid, process)
                try:
                    cpu += tracked.cpu_percent(interval=None)
                except (psutil.Error, OSError, PermissionError):
                    continue
            self._ollama_metric_processes = {
                pid: process for pid, process in self._ollama_metric_processes.items() if pid in live_pids
            }
            return round(cpu / (psutil.cpu_count() or 1), 1)
        except (psutil.Error, OSError, PermissionError):
            return None

    def model_resources(self) -> list[dict[str, Any]]:
        """Combine local processes with ``/api/ps`` without inventing remote metrics."""
        models = self.models()
        ollama_cpu = self._ollama_cpu_percent()
        bases = {model["api_base"].rstrip("/") for model in models if model["provider_model"].startswith("ollama/")}
        loaded_by_base: dict[str, dict[str, Any] | None] = {}
        for base in bases:
            try:
                with urllib.request.urlopen(f"{base}/api/ps", timeout=0.7) as response:
                    payload = json.load(response)
                loaded_by_base[base] = {
                    str(item.get("model") or item.get("name")): item for item in payload.get("models", [])
                }
            except (OSError, ValueError, TypeError):
                loaded_by_base[base] = None

        result = []
        for model in models:
            provider_model = model["provider_model"]
            if not provider_model.startswith("ollama/"):
                result.append({
                    "name": model["name"], "source": model.get("source", "remote"),
                    "cloudera_kind": model.get("cloudera_kind", ""), "available": False,
                    "cpu_percent": None, "memory_gb": None, "vram_gb": None,
                    "server_memory_free_percent": None, "loaded": None,
                })
                continue
            base = model["api_base"].rstrip("/")
            loaded_models = loaded_by_base.get(base)
            ollama_name = provider_model.removeprefix("ollama/")
            item = loaded_models.get(ollama_name) if loaded_models is not None else None
            result.append({
                "name": model["name"], "source": "ollama", "available": loaded_models is not None,
                "cpu_percent": ollama_cpu, "memory_gb": round(item.get("size", 0) / (1024 ** 3), 3) if item else 0.0,
                "vram_gb": round(item.get("size_vram", 0) / (1024 ** 3), 3) if item else 0.0,
                "server_memory_free_percent": self._host_memory_free_percent(base),
                "loaded": item is not None,
            })
        return result

    @staticmethod
    def _host_memory_free_percent(api_base: str) -> float | None:
        """Return free RAM only when Ollama runs on this machine.

        `/api/ps` informa del tamaño de los modelos, pero no de la RAM total del
        host. Para un servidor remoto no debemos presentar la memoria del panel
        como si perteneciera a ese servidor.
        """
        hostname = (urlparse(api_base).hostname or "").lower()
        if hostname not in {"localhost", "127.0.0.1", "::1"}:
            return None
        memory = psutil.virtual_memory()
        return round(memory.available / memory.total * 100, 1) if memory.total else None

    def _write_state(self, disabled: set[str]) -> None:
        """Persist disabled aliases through atomic replacement."""

        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps({"disabled_models": sorted(disabled)}, indent=2), encoding="utf-8")
        temp.replace(self.state_file)

    def missing_environment_variables(self) -> list[str]:
        """Discover ``os.environ/VAR`` references before starting LiteLLM."""
        referenced: set[str] = set()

        def visit(value: Any) -> None:
            """Find ``os.environ`` references inside nested structures."""

            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str) and value.startswith("os.environ/"):
                referenced.add(value.removeprefix("os.environ/"))

        source = self._source()
        disabled = set(self._state().get("disabled_models", []))
        # A disabled provider is absent from the active configuration and must
        # not block startup because of a credential that will never be used.
        source["model_list"] = [
            item for item in source.get("model_list", [])
            if item.get("model_name") not in disabled
        ]
        visit(source)
        from gateway.cloudera import ClouderaCatalog
        local_credentials = ClouderaCatalog(self.runtime_dir).environment()
        # IA Gateway disables these inherited LiteLLM options: the only allowed
        # persistence is the application-managed local SQLite database.
        optional = {"LITELLM_MASTER_KEY", "DATABASE_URL"}
        return sorted(
            name for name in referenced
            if name not in optional and not os.environ.get(name) and not local_credentials.get(name)
        )

    def _write_active_config(self) -> None:
        """Generate a filtered configuration and load the callback beside it."""
        config = self._source()
        from gateway.cloudera import ClouderaCatalog
        cloudera_catalog = ClouderaCatalog(self.runtime_dir)
        dashboard_settings = config.pop("dashboard_settings", {}) or {}
        disabled = set(self._state().get("disabled_models", []))
        config["model_list"] = [
            item for item in config.get("model_list", [])
            if item.get("model_name") not in disabled
        ]
        # Cloudera OpenAI-compatible endpoints strictly validate the internal
        # model identifier. Keep the mapping here so the callback replaces the
        # alias only after the router has selected the correct deployment.
        provider_models: dict[str, str] = {}
        provider_api_key_env: dict[str, str] = {}
        model_parameters: dict[str, dict[str, Any]] = {}
        has_workbench_models = False
        for item in config["model_list"]:
            params = item.get("litellm_params") or {}
            provider_model = str(params.get("model") or "")
            api_key = str(params.get("api_key") or "")
            model_info = item.get("model_info") or {}
            public_name = str(item.get("model_name") or "")
            if (model_info.get("dashboard_source") == "cloudera"
                    or api_key.startswith("os.environ/CLOUDERA_")):
                while provider_model.lower().startswith("openai/openai/openai/"):
                    provider_model = provider_model[len("openai/"):]
                remote_model = str(model_info.get("dashboard_remote_model") or "").strip()
                if remote_model and model_info.get("dashboard_cloudera_kind") != "workbench":
                    provider_model = f"openai/{remote_model}"
                elif (model_info.get("dashboard_cloudera_kind") == "inference"
                      and provider_model.lower().startswith("openai/gpt-oss")):
                    # Migrate drafts created by the former normalization logic:
                    # CAI requires `openai/gpt-oss-*` inside the JSON in addition
                    # to LiteLLM's outer provider-selection prefix.
                    provider_model = f"openai/{provider_model}"
                params["model"] = provider_model
            tls_verify = cloudera_catalog.tls_verification_for(
                api_key, str(params.get("api_base") or ""), as_context=False,
            )
            if tls_verify is not None:
                params["ssl_verify"] = tls_verify
            configured = {
                key: value for key, value in params.items()
                if key not in {"api_base", "api_key", "drop_params", "max_parallel_requests",
                               "model", "num_retries", "timeout"}
            }
            extra_parameters = model_info.get("dashboard_extra_parameters") or {}
            for dotted_key, value in extra_parameters.items():
                target = configured
                parts = str(dotted_key).split(".")
                for part in parts[:-1]:
                    child = target.get(part)
                    if not isinstance(child, dict):
                        child = {}
                        target[part] = child
                    target = child
                target[parts[-1]] = value
            model_parameters[public_name] = {
                "policy": str(model_info.get("dashboard_parameter_policy") or "caller_wins"),
                "configured": configured,
                "backend": str(model_info.get("dashboard_backend_profile") or
                               model_info.get("dashboard_serving_engine") or "auto"),
            }
            api_hostname = (urlparse(str(params.get("api_base") or "")).hostname or "").lower()
            is_workbench = (
                model_info.get("dashboard_cloudera_kind") == "workbench"
                or api_hostname.startswith("modelservice.")
            )
            is_nim_embedding = (
                str(model_info.get("dashboard_serving_engine") or "").lower() == "nim"
                and "embed" in str(model_info.get("dashboard_task") or "").lower()
            ) or bool(re.search(r"(?:nv-embedqa|retriever).*(?:-query|-passage)$", provider_model,
                                flags=re.IGNORECASE))
            if is_nim_embedding:
                # LiteLLM 1.83.9 sends encoding_format=null when omitted by the
                # client; NVIDIA NIM accepts only float or base64.
                params.setdefault("encoding_format", "float")
            if is_workbench:
                has_workbench_models = True
                # Model Service commonly imposes a deadline near 30 seconds. An
                # automatic retry leaves multiple generations alive on the same
                # replica and can block later calls.
                params["num_retries"] = 0
                # Support old drafts saved as custom/<model> before this adapter existed.
                if provider_model.startswith("custom/"):
                    params["model"] = f"cloudera_workbench/{provider_model.removeprefix('custom/')}"
            if provider_model.startswith("openai/") and api_key.startswith("os.environ/CLOUDERA_"):
                public_alias = str(item.get("model_name") or "")
                if public_alias:
                    provider_models[public_alias] = provider_model
                    provider_api_key_env[public_alias] = api_key.removeprefix("os.environ/")
                # `extra_body.model` does not replace the top-level field emitted
                # by the OpenAI SDK and may create two contradictory values. Keep
                # everything else: vLLM/NIM receive sampling and thinking here.
                extra_body = dict(params.get("extra_body") or {})
                extra_body.pop("model", None)
                if extra_body:
                    params["extra_body"] = extra_body
                else:
                    params.pop("extra_body", None)
        settings = dict(config.get("litellm_settings") or {})
        current_callbacks = settings.get("callbacks", [])
        if isinstance(current_callbacks, str):
            current_callbacks = [current_callbacks]
        # LiteLLM resolves Python callbacks from the active config directory.
        callback = "litellm_callback.dashboard_logger"
        settings["callbacks"] = list(dict.fromkeys([*current_callbacks, callback]))
        if has_workbench_models:
            custom_providers = settings.get("custom_provider_map", [])
            if not isinstance(custom_providers, list):
                custom_providers = []
            custom_providers = [
                item for item in custom_providers
                if not isinstance(item, dict) or item.get("provider") != "cloudera_workbench"
            ]
            custom_providers.append({
                "provider": "cloudera_workbench",
                "custom_handler": "workbench_provider.workbench_llm",
            })
            settings["custom_provider_map"] = custom_providers
        config["litellm_settings"] = settings
        general_settings = dict(config.get("general_settings") or {})
        # Inbound authentication belongs to the Cloudera WebApp and persistence
        # belongs to our SQLite database. Remove inherited options so neither a
        # master key nor an external database can be enabled accidentally.
        general_settings.pop("master_key", None)
        general_settings.pop("database_url", None)
        if general_settings:
            config["general_settings"] = general_settings
        else:
            config.pop("general_settings", None)
        guardrail = dict(dashboard_settings.get("guardrail") or {})
        guardrail["enabled"] = bool(guardrail.get("enabled") and guardrail.get("model"))
        automatic_embedding_exclusions = {
            str(item.get("model_name") or "") for item in config.get("model_list", [])
            if "embed" in f"{item.get('model_name', '')} {(item.get('litellm_params') or {}).get('model', '')} {(item.get('model_info') or {}).get('dashboard_task', '')}".lower()
            or "bge-" in str((item.get("litellm_params") or {}).get("model") or "").lower()
        }
        guardrail["excluded_models"] = sorted({
            str(name) for name in (guardrail.get("excluded_models") or []) if name
        } | automatic_embedding_exclusions)
        guardrail_name = guardrail.get("model")
        guardrail_entry = next((item for item in config.get("model_list", []) if item.get("model_name") == guardrail_name), None)
        if guardrail["enabled"] and guardrail_entry:
            params = guardrail_entry.get("litellm_params") or {}
            configured_model = str(params.get("model", ""))
            guardrail["protocol"] = "ollama" if configured_model.startswith("ollama/") else "openai"
            guardrail["provider_model"] = (
                configured_model.removeprefix("ollama/")
                if guardrail["protocol"] == "ollama" else configured_model
            )
            guardrail["api_base"] = params.get("api_base", "http://localhost:11434")
            api_key = str(params.get("api_key") or "")
            if api_key.startswith("os.environ/"):
                guardrail["api_key_env"] = api_key.removeprefix("os.environ/")
        self.dashboard_settings_file.write_text(
            json.dumps({
                "guardrail": guardrail,
                "provider_models": provider_models,
                "provider_api_key_env": provider_api_key_env,
                "model_parameters": model_parameters,
                "advisor": dict(dashboard_settings.get("advisor") or {}),
            }, indent=2),
            encoding="utf-8",
        )
        shutil.copy2(Path(__file__).with_name("litellm_callback.py"), self.runtime_dir / "litellm_callback.py")
        # Load this when the child interpreter starts, before LiteLLM creates and
        # caches its OpenAI/aiohttp transport. In some LiteLLM versions, loading
        # it from the callback alone is too late.
        shutil.copy2(
            Path(__file__).with_name("litellm_sitecustomize.py"),
            self.runtime_dir / "sitecustomize.py",
        )
        if has_workbench_models:
            shutil.copy2(Path(__file__).with_name("workbench_provider.py"), self.runtime_dir / "workbench_provider.py")
        self.active_config.write_text(
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )

    def _process_environment(self) -> dict[str, str]:
        """Build the LiteLLM environment while respecting YAML policy.

        Una cadena vacía no desactiva la autenticación en LiteLLM: se interpreta
        como una master key válida y obliga a enviar ``Authorization``. Quitamos
        la variable y usamos el modo de producción para impedir que el CLI vuelva
        a cargarla desde `.env`; las claves de proveedores se heredan normalmente.
        """
        env = os.environ.copy()
        from gateway.cloudera import ClouderaCatalog
        env.update(ClouderaCatalog(self.runtime_dir).environment())
        env.pop("LITELLM_MASTER_KEY", None)
        env.pop("DATABASE_URL", None)
        env["LITELLM_MODE"] = "PRODUCTION"
        env["IA_GATEWAY_ROOT"] = str(self.root)
        # LiteLLM 1.83 does not always propagate a deployment's `ssl_verify:
        # false` to its shared OpenAI/aiohttp client. Communicate only hosts that
        # explicitly requested it; TLS verification is never disabled for Cloud
        # providers or other destinations served by the same gateway.
        try:
            active = yaml.safe_load(self.active_config.read_text(encoding="utf-8")) or {}
            insecure_hosts = sorted({
                (urlparse(str(params.get("api_base") or "")).hostname or "").lower()
                for item in active.get("model_list", [])
                if isinstance(item, dict)
                for params in [item.get("litellm_params") or {}]
                if params.get("ssl_verify") is False
            } - {""})
        except (OSError, yaml.YAMLError, TypeError):
            insecure_hosts = []
        if insecure_hosts:
            env["IA_GATEWAY_INSECURE_TLS_HOSTS"] = ",".join(insecure_hosts)
        else:
            env.pop("IA_GATEWAY_INSECURE_TLS_HOSTS", None)
        python_paths = [str(self.runtime_dir), str(self.root)]
        if env.get("PYTHONPATH"):
            python_paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
        return env

    def sync_cloudera_credentials(self) -> bool:
        """Confirm dynamic mode: the callback reads SQLite on every request."""

        return True

    def _pid(self) -> int | None:
        """Read the managed PID; a missing or corrupt value means stopped."""

        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        """Check process existence without sending a destructive signal."""

        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def is_running(self) -> bool:
        """Return true only when the supervisor lives and the socket is ready."""
        return self.process_alive() and self._port_ready()

    def process_alive(self) -> bool:
        """Check the saved PID and remove stale references."""
        pid = self._pid()
        if pid and self._alive(pid):
            return True
        self.pid_file.unlink(missing_ok=True)
        return False

    def _port_ready(self, timeout: float = 0.25) -> bool:
        """Use a short TCP connection as an objective availability check."""
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True
        except OSError:
            return False

    def _metrics(self, pid: int | None) -> dict[str, float | int | None]:
        """Aggregate CPU and RSS for the process tree, degrading safely on permission errors."""
        if not pid:
            self._metric_processes.clear()
            return {"cpu_percent": None, "memory_gb": None, "cores": psutil.cpu_count() or 1}
        try:
            root = psutil.Process(pid)
            try:
                processes = [root, *root.children(recursive=True)]
            except (psutil.Error, OSError, PermissionError):
                processes = [root]
            live_pids = {process.pid for process in processes}
            cpu = 0.0
            memory = 0
            for process in processes:
                tracked = self._metric_processes.setdefault(process.pid, process)
                try:
                    cpu += tracked.cpu_percent(interval=None)
                    memory += tracked.memory_info().rss
                except (psutil.Error, OSError, PermissionError):
                    continue
            self._metric_processes = {
                process_id: process for process_id, process in self._metric_processes.items()
                if process_id in live_pids
            }
            cores = psutil.cpu_count() or 1
            return {
                "cpu_percent": round(cpu / cores, 1),
                "memory_gb": round(memory / (1024 ** 3), 3),
                "cores": cores,
            }
        except (psutil.Error, OSError, PermissionError):
            return {"cpu_percent": None, "memory_gb": None, "cores": psutil.cpu_count() or 1}

    def status(self) -> dict[str, Any]:
        """Build the snapshot refreshed by the UI every three seconds."""
        process_alive = self.process_alive()
        pid = self._pid() if process_alive else None
        port_ready = self._port_ready()
        running = process_alive and port_ready
        return {
            "running": running,
            "process_alive": process_alive,
            "port_conflict": port_ready and not process_alive,
            "pid": pid,
            "host": self.host,
            "port": self.port,
            "restart_pending": self.restart_pending_file.exists(),
            "persistence": self.persistence_status(),
            **self._metrics(pid),
        }

    def persistence_status(self) -> dict[str, Any]:
        """Report the sole supported mode: local SQLite and dynamic tokens."""

        dynamic_tokens = self.sync_cloudera_credentials()
        return {
            "credential_store": "SQLite",
            "token_updates_dynamic": dynamic_tokens,
            "token_restart_required": not dynamic_tokens,
        }

    def active_model_names(self) -> list[str]:
        """Read loaded aliases while distinguishing pending YAML changes."""

        try:
            config = yaml.safe_load(self.active_config.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return []
        return [
            str(item.get("model_name"))
            for item in config.get("model_list", [])
            if isinstance(item, dict) and item.get("model_name")
        ]

    def start(self) -> dict[str, Any]:
        """Start the ``.venv`` CLI and actively wait for it to open the port."""
        if self.process_alive():
            if self.is_running():
                return self.status()
            self.stop()
        if self._port_ready():
            raise RuntimeError(
                f"El puerto {self.port} está ocupado por un proceso que esta app no controla. "
                "Detén ese LiteLLM antiguo antes de arrancar una instancia nueva."
            )
        missing = self.missing_environment_variables()
        if missing:
            variables = ", ".join(missing)
            raise RuntimeError(
                f"Faltan variables de entorno requeridas: {variables}. "
                "Añádelas al fichero .env y reinicia la webapp."
            )
        self._write_active_config()
        env = self._process_environment()
        litellm_cli = self.root / ".venv" / "bin" / "litellm"
        if not litellm_cli.exists():
            raise RuntimeError("No se encuentra el ejecutable de LiteLLM en el entorno activo")
        environment_python = self.root / ".venv" / "bin" / "python"
        try:
            version = subprocess.run(
                [str(environment_python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            major, minor = (int(part) for part in version.split(".", 1))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise RuntimeError("No se pudo validar la versión de Python del entorno .venv") from exc
        if (major, minor) < (3, 11):
            raise RuntimeError(
                f"El entorno .venv usa Python {version}. IA Gateway requiere Python 3.11 o superior; "
                "recréalo para evitar errores de carga en LiteLLM."
            )
        process = subprocess.Popen(
            [str(litellm_cli), "--config", str(self.active_config),
             "--host", self.host, "--port", str(self.port)],
            cwd=self.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        threading.Thread(target=self._capture_process_output, args=(process,), daemon=True).start()
        self.pid_file.write_text(str(process.pid), encoding="utf-8")
        # `monotonic()` is unaffected if the system clock synchronizes during
        # startup, so it is safer than comparing wall-clock timestamps.
        deadline = time.monotonic() + 30
        credential_error: RuntimeError | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if self._port_ready(timeout=0.4):
                try:
                    self.sync_cloudera_credentials()
                    return self.status()
                except RuntimeError as exc:
                    # Uvicorn may open the socket before LiteLLM finishes
                    # registering its routes and callbacks.
                    credential_error = exc
            time.sleep(0.25)
        self.stop()
        tail = self.process_log(35)
        if credential_error is not None:
            tail = f"{tail}\nSincronización de credenciales: {credential_error}"
        raise RuntimeError(f"LiteLLM no pudo abrir el puerto {self.port}. Últimas líneas:\n{tail}")

    def stop(self) -> dict[str, Any]:
        """Attempt graceful termination and escalate to SIGKILL only as a last resort."""
        pid = self._pid()
        if not pid or not self._alive(pid):
            self.pid_file.unlink(missing_ok=True)
            return self.status()
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        for _ in range(30):
            if not self._alive(pid):
                break
            time.sleep(0.1)
        if self._alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.pid_file.unlink(missing_ok=True)
        return self.status()

    def set_model(self, name: str, enabled: bool) -> dict[str, Any]:
        """Persist the override and restart to apply the new topology."""
        names = {model["name"] for model in self.models()}
        if name not in names:
            raise KeyError(name)
        disabled = set(self._state().get("disabled_models", []))
        disabled.discard(name) if enabled else disabled.add(name)
        self._write_state(disabled)
        was_running = self.process_alive()
        if was_running:
            self.stop()
            self.start()
        return next(model for model in self.models() if model["name"] == name)

    def process_log(self, lines: int = 100, day: date | None = None) -> str:
        """Return the log tail without terminal-specific ANSI codes."""
        path = self.process_log_path(day)
        if not path.exists() and (day is None or day == datetime.now(ZoneInfo("Europe/Madrid")).date()):
            path = self.output_log
        if not path.exists(): return ""
        content = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", content)

    def process_log_days(self) -> list[str]:
        """List existing technical log days, including today."""

        days = {path.stem.removeprefix("litellm-") for path in (self.runtime_dir / "logs").glob("litellm-????-??-??.log")}
        if self.output_log.exists(): days.add(datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat())
        return sorted(days, reverse=True)
