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
import time
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

import yaml
import psutil


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
        self.port = 4000
        self._metric_processes: dict[int, psutil.Process] = {}
        self._ollama_metric_processes: dict[int, psutil.Process] = {}
        self.runtime_dir.mkdir(exist_ok=True)

    def _source(self) -> dict[str, Any]:
        """Carga y valida la forma mínima del contrato YAML del usuario."""
        if not self.source_config.exists():
            raise RuntimeError(f"No existe {self.source_config}")
        data = yaml.safe_load(self.source_config.read_text(encoding="utf-8")) or {}
        if not isinstance(data.get("model_list", []), list):
            raise RuntimeError("config.yaml debe contener una lista 'model_list'")
        return data

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
        result = []
        for entry in self._source().get("model_list", []):
            name = str(entry.get("model_name", "sin-nombre"))
            params = entry.get("litellm_params") or {}
            provider_model = str(params.get("model", ""))
            # LiteLLM soporta muchos endpoints. Para esta UI basta una heurística
            # explícita que distingue los embeddings locales conocidos del chat.
            searchable_name = f"{name} {provider_model}".lower()
            result.append({
                "name": name,
                "provider_model": provider_model,
                "api_base": params.get("api_base", ""),
                "enabled": name not in disabled,
                "mode": "embedding" if "embedding" in searchable_name or "bge-" in searchable_name else "chat",
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
                    "name": model["name"], "source": "remote", "available": False,
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
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps({"disabled_models": sorted(disabled)}, indent=2), encoding="utf-8")
        temp.replace(self.state_file)

    def missing_environment_variables(self) -> list[str]:
        """Descubre referencias `os.environ/VAR` antes de lanzar LiteLLM."""
        referenced: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str) and value.startswith("os.environ/"):
                referenced.add(value.removeprefix("os.environ/"))

        visit(self._source())
        return sorted(name for name in referenced if not os.environ.get(name))

    def _write_active_config(self) -> None:
        """Genera una configuración filtrada y carga el callback junto a ella."""
        config = self._source()
        disabled = set(self._state().get("disabled_models", []))
        config["model_list"] = [
            item for item in config.get("model_list", [])
            if item.get("model_name") not in disabled
        ]
        settings = dict(config.get("litellm_settings") or {})
        current_callbacks = settings.get("callbacks", [])
        if isinstance(current_callbacks, str):
            current_callbacks = [current_callbacks]
        # LiteLLM resuelve callbacks Python desde el directorio del config activo.
        callback = "litellm_callback.dashboard_logger"
        settings["callbacks"] = list(dict.fromkeys([*current_callbacks, callback]))
        config["litellm_settings"] = settings
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
        general_settings = self._source().get("general_settings") or {}
        if not general_settings.get("master_key"):
            env["LITELLM_MASTER_KEY"] = ""
        env["PYTHONPATH"] = str(self.root) + os.pathsep + env.get("PYTHONPATH", "")
        env["IA_GATEWAY_ROOT"] = str(self.root)
        return env

    def _pid(self) -> int | None:
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError):
            return None

    @staticmethod
    def _alive(pid: int) -> bool:
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
            with socket.create_connection(("127.0.0.1", self.port), timeout=timeout):
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
        running = process_alive and self._port_ready()
        return {
            "running": running,
            "process_alive": process_alive,
            "pid": pid,
            "host": "127.0.0.1",
            "port": self.port,
            **self._metrics(pid),
        }

    def start(self) -> dict[str, Any]:
        """Arranca el CLI del `.venv` y espera activamente a que abra el puerto."""
        if self.process_alive():
            if self.is_running():
                return self.status()
            self.stop()
        if self.is_running():
            return self.status()
        missing = self.missing_environment_variables()
        if missing:
            variables = ", ".join(missing)
            raise RuntimeError(
                f"Faltan variables de entorno requeridas: {variables}. "
                "Añádelas al fichero .env y reinicia la webapp."
            )
        self._write_active_config()
        env = self._process_environment()
        output = self.output_log.open("a", encoding="utf-8")
        litellm_cli = self.root / ".venv" / "bin" / "litellm"
        if not litellm_cli.exists():
            raise RuntimeError("No se encuentra el ejecutable de LiteLLM en el entorno activo")
        process = subprocess.Popen(
            [str(litellm_cli), "--config", str(self.active_config),
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=self.root,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        output.close()
        self.pid_file.write_text(str(process.pid), encoding="utf-8")
        # `monotonic()` no cambia si el reloj del sistema se sincroniza durante
        # el arranque, por eso es preferible a comparar timestamps de pared.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if self._port_ready(timeout=0.4):
                return self.status()
            time.sleep(0.25)
        self.stop()
        tail = self.process_log(35)
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

    def process_log(self, lines: int = 100) -> str:
        """Devuelve la cola del log sin códigos ANSI propios de terminal."""
        if not self.output_log.exists():
            return ""
        content = "\n".join(self.output_log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", content)
