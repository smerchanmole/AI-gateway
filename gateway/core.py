"""Dominio operativo del gateway: configuración, procesos, salud y métricas.

La idea pedagógica importante es separar *estar ejecutándose* de *estar listo*.
Un proceso puede conservar un PID después de que su servidor haya fallado. Por
eso esta clase exige dos señales: proceso vivo y puerto accesible.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
from datetime import date, datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlparse
from pathlib import Path
from typing import Any

import yaml
import psutil


def environment_port(name: str, default: int) -> int:
    """Lee un puerto de entorno con validación explícita y fallback local."""
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
    """Orquesta LiteLLM conservando `config.yaml` como fuente de verdad inmutable."""

    def __init__(self, root: Path) -> None:
        """Centraliza todas las rutas generadas bajo `runtime/`."""
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
        # LiteLLM nunca ocupa el puerto publicado por Cloudera. El proxy atiende
        # ese puerto único y reenvía /v1 directamente a este upstream interno.
        self.port = environment_port("IA_GATEWAY_LITELLM_PORT", 14000)
        self._metric_processes: dict[int, psutil.Process] = {}
        self._ollama_metric_processes: dict[int, psutil.Process] = {}
        self._config_lock = threading.RLock()
        self.runtime_dir.mkdir(exist_ok=True)

    def process_log_path(self, day: date | None = None) -> Path:
        """Calcula el fichero técnico diario usando el calendario de Madrid."""

        selected = day or datetime.now(ZoneInfo("Europe/Madrid")).date()
        path = self.runtime_dir / "logs" / f"litellm-{selected.isoformat()}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def clear_process_log(self, day: date | None = None) -> None:
        """Vacía únicamente la jornada elegida, sin tocar logs de modelos."""
        self.process_log_path(day).write_text("", encoding="utf-8")

    def _capture_process_output(self, process: subprocess.Popen) -> None:
        """Añade hora Madrid a cada línea emitida por LiteLLM."""
        if process.stdout is None:
            return
        with self.process_log_path().open("a", encoding="utf-8") as output:
            for line in process.stdout:
                stamp = datetime.now(ZoneInfo("Europe/Madrid")).isoformat(timespec="milliseconds")
                output.write(f"[{stamp}] {line}")
                output.flush()

    def _source(self) -> dict[str, Any]:
        """Carga y valida la forma mínima del contrato YAML del usuario."""
        if not self.source_config.exists():
            raise RuntimeError(f"No existe {self.source_config}")
        data = yaml.safe_load(self.source_config.read_text(encoding="utf-8")) or {}
        if not isinstance(data.get("model_list", []), list):
            raise RuntimeError("config.yaml debe contener una lista 'model_list'")
        return data

    @staticmethod
    def _validate_config(data: Any) -> dict[str, Any]:
        """Valida el contrato editable antes de tocar la fuente de verdad."""
        if not isinstance(data, dict):
            raise RuntimeError("El YAML debe contener un objeto en el nivel raíz")
        model_list = data.get("model_list")
        if not isinstance(model_list, list):
            raise RuntimeError("El YAML debe contener una lista 'model_list'")
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
        return data

    def validate_config_text(self, content: str) -> dict[str, Any]:
        """Analiza el YAML sin modificarlo y resume lo que se aplicaría."""
        if len(content.encode("utf-8")) > 1_000_000:
            raise RuntimeError("El YAML no puede superar 1 MB")
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise RuntimeError(f"YAML no válido: {exc}") from exc
        config = self._validate_config(parsed)
        referenced: set[str] = set()

        def visit(value: Any) -> None:
            """Recorre contenedores YAML y acumula referencias de entorno."""

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

    def config_text(self) -> str:
        """Devuelve el YAML fuente, incluidos comentarios y formato manual."""
        return self.source_config.read_text(encoding="utf-8")

    def dashboard_settings(self) -> dict[str, Any]:
        """Expone sólo opciones propias del panel conservadas en el YAML fuente."""

        return dict(self._source().get("dashboard_settings") or {})

    def set_guardrail(self, enabled: bool, model: str, policy: str = "warn", restart: bool = True) -> dict[str, Any]:
        """Configura el filtro global y reutiliza la actualización transaccional."""

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
            config["dashboard_settings"] = {"guardrail": {
                "enabled": enabled, "model": model, "policy": policy, "timeout": 8,
            }}
            return self.update_config(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), restart=restart)

    def update_config(self, content: str, restart: bool = True) -> dict[str, Any]:
        """Valida, sustituye atómicamente y reinicia con rollback ante error."""
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
        """Añade un modelo mediante el mismo camino transaccional del editor."""
        with self._config_lock:
            config = self._source()
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
        """Sustituye el fallback de un alias sin alterar los de otros modelos."""
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
        """Modifica un modelo y repara todas las referencias si cambia el alias."""
        with self._config_lock:
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
            managed = {"model", "api_base", "api_key", "reasoning_effort", "keep_alive", "timeout", "drop_params"}
            if "api_key" not in new_params and old_params.get("api_key"):
                managed.remove("api_key")
            models[index] = {**models[index], "model_name": new_name,
                             "litellm_params": {**{k: v for k, v in old_params.items() if k not in managed}, **new_params}}
            if entry.get("model_info"):
                models[index]["model_info"] = entry["model_info"]
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
            disabled = set(self._state().get("disabled_models", []))
            if current_name in disabled:
                disabled.remove(current_name); disabled.add(new_name); self._write_state(disabled)
            return self.update_config(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), restart=restart)

    def delete_model(self, name: str, restart: bool = True) -> dict[str, Any]:
        """Elimina el alias y limpia fallbacks, guardrail y estado asociados."""
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
            disabled = set(self._state().get("disabled_models", [])); disabled.discard(name); self._write_state(disabled)
            return self.update_config(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), restart=restart)

    def apply_pending_config(self) -> dict[str, Any]:
        """Reinicia explícitamente una configuración guardada sin aplicar."""
        with self._config_lock:
            if self.process_alive():
                self.stop()
                self.start()
            self.restart_pending_file.unlink(missing_ok=True)
            return self.status()

    def _state(self) -> dict[str, Any]:
        """Lee estado efímero; ante corrupción vuelve a un estado seguro vacío."""
        if not self.state_file.exists():
            return {"disabled_models": []}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"disabled_models": []}
        except (json.JSONDecodeError, OSError):
            return {"disabled_models": []}

    def models(self) -> list[dict[str, Any]]:
        """Proyecta el YAML en datos de UI sin exponer parámetros sensibles."""
        disabled = set(self._state().get("disabled_models", []))
        source = self._source()
        fallback_map: dict[str, list[str]] = {}
        for mapping in (source.get("router_settings") or {}).get("fallbacks", []):
            if isinstance(mapping, dict):
                fallback_map.update({str(k): [str(v) for v in values] for k, values in mapping.items() if isinstance(values, list)})
        # Relacionamos cada API base con su conexión Cloudera para que el
        # inventario no pierda el origen al convertir un endpoint a LiteLLM.
        from gateway.cloudera import ClouderaCatalog
        try:
            connections = ClouderaCatalog(self.runtime_dir).connections()
        except RuntimeError:
            # Un catálogo opcional dañado no debe ocultar el inventario YAML.
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
            # LiteLLM soporta muchos endpoints. Para esta UI basta una heurística
            # explícita que distingue los embeddings locales conocidos del chat.
            searchable_name = f"{name} {provider_model}".lower()
            api_base = str(params.get("api_base", ""))
            api_hostname = (urlparse(api_base).hostname or "").lower()
            cloudera_kind = str(model_info.get("dashboard_cloudera_kind") or "")
            if not cloudera_kind:
                cloudera_kind = next((kind for hostname, kind in cloudera_origins if hostname == api_hostname), "")
            is_cloudera = model_info.get("dashboard_source") == "cloudera" or bool(cloudera_kind) or "cloudera.site" in api_hostname or str(params.get("api_key", "")).startswith("os.environ/CLOUDERA_")
            if is_cloudera and not cloudera_kind:
                cloudera_kind = "workbench" if any(marker in api_base.lower() for marker in ("/model-deployments", "/api/v2")) else "inference"
            result.append({
                "name": name,
                "provider_model": provider_model,
                "api_base": params.get("api_base", ""),
                "api_key": (params.get("api_key", "") if str(params.get("api_key", "")).startswith("os.environ/")
                            else ("Configurada (oculta)" if params.get("api_key") else "")),
                "reasoning_effort": params.get("reasoning_effort", ""),
                "keep_alive": params.get("keep_alive", ""),
                "timeout": params.get("timeout", ""),
                "drop_params": bool(params.get("drop_params", False)),
                "fallbacks": fallback_map.get(name, []),
                "enabled": name not in disabled,
                "mode": "embedding" if "embedding" in searchable_name or "bge-" in searchable_name else "chat",
                "source": "cloudera" if is_cloudera else ("ollama" if provider_model.startswith("ollama/") else "remote"),
                "cloudera_kind": cloudera_kind,
            })
        return result

    def _ollama_cpu_percent(self) -> float | None:
        """Mide CPU compartida de Ollama como porcentaje de la máquina completa."""
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
        """Combina procesos locales con `/api/ps` sin fingir métricas remotas."""
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
        """Devuelve RAM libre sólo cuando Ollama reside en esta máquina.

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
        """Persiste la lista de aliases apagados mediante sustitución atómica."""

        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps({"disabled_models": sorted(disabled)}, indent=2), encoding="utf-8")
        temp.replace(self.state_file)

    def missing_environment_variables(self) -> list[str]:
        """Descubre referencias `os.environ/VAR` antes de lanzar LiteLLM."""
        referenced: set[str] = set()

        def visit(value: Any) -> None:
            """Busca referencias ``os.environ`` dentro de estructuras anidadas."""

            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str) and value.startswith("os.environ/"):
                referenced.add(value.removeprefix("os.environ/"))

        visit(self._source())
        from gateway.cloudera import ClouderaCatalog
        local_credentials = ClouderaCatalog(self.runtime_dir).environment()
        return sorted(name for name in referenced if not os.environ.get(name) and not local_credentials.get(name))

    def _write_active_config(self) -> None:
        """Genera una configuración filtrada y carga el callback junto a ella."""
        config = self._source()
        dashboard_settings = config.pop("dashboard_settings", {}) or {}
        disabled = set(self._state().get("disabled_models", []))
        config["model_list"] = [
            item for item in config.get("model_list", [])
            if item.get("model_name") not in disabled
        ]
        # Los endpoints OpenAI-compatible de Cloudera validan estrictamente el
        # identificador interno del modelo. Conservamos aquí la traducción para
        # que el callback sustituya el alias sólo después de que el router haya
        # elegido el deployment correcto.
        provider_models: dict[str, str] = {}
        database_enabled = bool(os.environ.get("DATABASE_URL", "").strip())
        for item in config["model_list"]:
            params = item.get("litellm_params") or {}
            provider_model = str(params.get("model") or "")
            api_key = str(params.get("api_key") or "")
            if provider_model.startswith("openai/") and api_key.startswith("os.environ/CLOUDERA_"):
                if database_enabled:
                    variable_name = api_key.removeprefix("os.environ/")
                    params["litellm_credential_name"] = self.credential_name(variable_name)
                public_alias = str(item.get("model_name") or "")
                if public_alias:
                    provider_models[public_alias] = provider_model
                # `extra_body.model` no sustituye el campo superior que genera
                # el SDK OpenAI y puede producir dos valores contradictorios.
                params.pop("extra_body", None)
        settings = dict(config.get("litellm_settings") or {})
        current_callbacks = settings.get("callbacks", [])
        if isinstance(current_callbacks, str):
            current_callbacks = [current_callbacks]
        # LiteLLM resuelve callbacks Python desde el directorio del config activo.
        callback = "litellm_callback.dashboard_logger"
        settings["callbacks"] = list(dict.fromkeys([*current_callbacks, callback]))
        config["litellm_settings"] = settings
        if database_enabled:
            if not os.environ.get("LITELLM_SALT_KEY", "").strip():
                raise RuntimeError(
                    "DATABASE_URL está configurada, pero falta LITELLM_SALT_KEY. "
                    "Debe ser estable para poder descifrar las credenciales tras un reinicio."
                )
            general_settings = dict(config.get("general_settings") or {})
            general_settings["database_url"] = "os.environ/DATABASE_URL"
            general_settings["store_model_in_db"] = True
            config["general_settings"] = general_settings
        guardrail = dict(dashboard_settings.get("guardrail") or {})
        guardrail["enabled"] = bool(guardrail.get("enabled") and guardrail.get("model"))
        guardrail_name = guardrail.get("model")
        guardrail_entry = next((item for item in config.get("model_list", []) if item.get("model_name") == guardrail_name), None)
        if guardrail["enabled"] and guardrail_entry:
            params = guardrail_entry.get("litellm_params") or {}
            guardrail["provider_model"] = str(params.get("model", "")).removeprefix("ollama/")
            guardrail["api_base"] = params.get("api_base", "http://localhost:11434")
        self.dashboard_settings_file.write_text(
            json.dumps({"guardrail": guardrail, "provider_models": provider_models}, indent=2),
            encoding="utf-8",
        )
        shutil.copy2(Path(__file__).with_name("litellm_callback.py"), self.runtime_dir / "litellm_callback.py")
        self.active_config.write_text(
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )

    def _process_environment(self) -> dict[str, str]:
        """Construye el entorno de LiteLLM respetando la política del YAML.

        LiteLLM interpreta ``LITELLM_MASTER_KEY`` aunque no aparezca en su
        configuración y también puede volver a cargarla directamente desde
        `.env`. Por eso la pasamos vacía —en vez de omitirla— cuando el usuario
        no declara una ``master_key`` en `general_settings`: `python-dotenv` no
        sobrescribe variables ya presentes y las claves de proveedores siguen
        disponibles con normalidad.
        """
        env = os.environ.copy()
        from gateway.cloudera import ClouderaCatalog
        env.update(ClouderaCatalog(self.runtime_dir).environment())
        general_settings = self._source().get("general_settings") or {}
        if not general_settings.get("master_key"):
            env["LITELLM_MASTER_KEY"] = ""
        env["PYTHONPATH"] = str(self.root) + os.pathsep + env.get("PYTHONPATH", "")
        env["IA_GATEWAY_ROOT"] = str(self.root)
        return env

    def _prepare_database(self, env: dict[str, str]) -> None:
        """Genera el cliente Prisma y aplica migraciones antes de LiteLLM."""

        if not env.get("DATABASE_URL", "").strip():
            return
        prisma = self.root / ".venv" / "bin" / "prisma"
        try:
            import litellm_proxy_extras
        except ImportError as exc:
            raise RuntimeError("LiteLLM no incluye el esquema PostgreSQL requerido") from exc
        schema = Path(litellm_proxy_extras.__file__).resolve().with_name("schema.prisma")
        if not prisma.exists() or not schema.exists():
            raise RuntimeError("No se encuentran Prisma o el esquema de base de datos de LiteLLM")
        prisma_env = env.copy()
        prisma_home = self.runtime_dir / "prisma"
        prisma_home.mkdir(exist_ok=True)
        prisma_env["PATH"] = str(prisma.parent) + os.pathsep + prisma_env.get("PATH", "")
        prisma_env.setdefault("PRISMA_HOME_DIR", str(prisma_home))
        for action in (("generate",), ("migrate", "deploy")):
            completed = subprocess.run(
                [str(prisma), *action, "--schema", str(schema)],
                cwd=self.root,
                env=prisma_env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if completed.returncode:
                detail = (completed.stderr or completed.stdout).strip()[-1500:]
                label = "generar el cliente" if action == ("generate",) else "aplicar las migraciones"
                raise RuntimeError(f"No se pudo {label} Prisma: {detail}")

    @staticmethod
    def credential_name(variable_name: str) -> str:
        """Nombre estable de la credencial cifrada asociada a un token CDP."""

        safe = re.sub(r"[^A-Z0-9_]+", "_", variable_name.upper()).strip("_")
        return f"ia_gateway_{safe.lower()}"

    def sync_cloudera_credentials(self) -> bool:
        """Actualiza tokens en PostgreSQL y memoria de LiteLLM sin reiniciarlo.

        LiteLLM cifra ``credential_values`` usando ``LITELLM_SALT_KEY``. La API
        PATCH actualiza además la lista de credenciales del proceso en curso,
        por lo que las peticiones siguientes reciben el token nuevo.
        """

        if not os.environ.get("DATABASE_URL", "").strip():
            return False
        master_key = os.environ.get("LITELLM_MASTER_KEY", "").strip()
        if not master_key:
            raise RuntimeError("Falta LITELLM_MASTER_KEY para sincronizar credenciales con LiteLLM")
        from gateway.cloudera import ClouderaCatalog

        headers = {
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        for variable_name, token in ClouderaCatalog(self.runtime_dir).environment().items():
            if not variable_name.startswith("CLOUDERA_") or not token:
                continue
            name = self.credential_name(variable_name)
            quoted_name = quote(name, safe="")
            payload = json.dumps({
                "credential_name": name,
                "credential_info": {"managed_by": "ia-gateway", "source": variable_name},
                "credential_values": {"api_key": token},
            }).encode()
            lookup = urllib.request.Request(
                f"http://{self.host}:{self.port}/credentials/by_name/{quoted_name}",
                headers=headers,
                method="GET",
            )
            exists = False
            try:
                with urllib.request.urlopen(lookup, timeout=10):
                    exists = True
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    detail = exc.read().decode(errors="replace")[:500]
                    raise RuntimeError(f"LiteLLM no pudo consultar la credencial {name}: HTTP {exc.code} {detail}") from exc
            method = "PATCH" if exists else "POST"
            url = (
                f"http://{self.host}:{self.port}/credentials/{quoted_name}"
                if exists else f"http://{self.host}:{self.port}/credentials"
            )
            request = urllib.request.Request(url, data=payload, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=15):
                    pass
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                raise RuntimeError(f"LiteLLM no pudo guardar la credencial {name}: HTTP {exc.code} {detail}") from exc
            except OSError as exc:
                raise RuntimeError(f"No se pudo sincronizar la credencial {name} con LiteLLM: {exc}") from exc
        return True

    def _pid(self) -> int | None:
        """Lee el PID gestionado; un valor ausente o corrupto equivale a parado."""

        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
        """Comprueba existencia del proceso sin enviarle una señal destructiva."""

        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def is_running(self) -> bool:
        """Sólo es verdadero si el supervisor vive y el socket está listo."""
        return self.process_alive() and self._port_ready()

    def process_alive(self) -> bool:
        """Comprueba el PID guardado y elimina referencias obsoletas."""
        pid = self._pid()
        if pid and self._alive(pid):
            return True
        self.pid_file.unlink(missing_ok=True)
        return False

    def _port_ready(self, timeout: float = 0.25) -> bool:
        """Usa una conexión TCP corta como prueba de disponibilidad objetiva."""
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True
        except OSError:
            return False

    def _metrics(self, pid: int | None) -> dict[str, float | int | None]:
        """Agrega CPU y RSS del árbol, con degradación segura ante permisos."""
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
        """Construye el snapshot que la interfaz renueva cada tres segundos."""
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
            **self._metrics(pid),
        }

    def start(self) -> dict[str, Any]:
        """Arranca el CLI del `.venv` y espera activamente a que abra el puerto."""
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
        self._prepare_database(env)
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
        # `monotonic()` no cambia si el reloj del sistema se sincroniza durante
        # el arranque, por eso es preferible a comparar timestamps de pared.
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
                    # Uvicorn puede abrir el socket antes de que LiteLLM termine
                    # de conectar/migrar PostgreSQL y registrar sus rutas.
                    credential_error = exc
            time.sleep(0.25)
        self.stop()
        tail = self.process_log(35)
        if credential_error is not None:
            tail = f"{tail}\nSincronización de credenciales: {credential_error}"
        raise RuntimeError(f"LiteLLM no pudo abrir el puerto {self.port}. Últimas líneas:\n{tail}")

    def stop(self) -> dict[str, Any]:
        """Aplica terminación amable y escala a SIGKILL sólo como último recurso."""
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
        """Persiste el override y reinicia para aplicar la topología nueva."""
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
        """Devuelve la cola del log sin códigos ANSI propios de terminal."""
        path = self.process_log_path(day)
        if not path.exists() and (day is None or day == datetime.now(ZoneInfo("Europe/Madrid")).date()):
            path = self.output_log
        if not path.exists(): return ""
        content = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", content)

    def process_log_days(self) -> list[str]:
        """Lista las jornadas técnicas existentes, incluida la fecha de hoy."""

        days = {path.stem.removeprefix("litellm-") for path in (self.runtime_dir / "logs").glob("litellm-????-??-??.log")}
        if self.output_log.exists(): days.add(datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat())
        return sorted(days, reverse=True)
