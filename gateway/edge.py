"""Publicación de panel y LiteLLM detrás de un único puerto HTTP."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import time

from gateway.core import environment_port


def public_gateway_port() -> int:
    """Elige el único puerto que publica el runtime, con 8090 en local."""

    if os.environ.get("IA_GATEWAY_PORT", "").strip():
        return environment_port("IA_GATEWAY_PORT", 8090)
    for name in ("CDSW_READONLY_PORT", "CDSW_APP_PORT", "CDSW_PUBLIC_PORT"):
        if os.environ.get(name, "").strip():
            return environment_port(name, 8090)
    return 8090


class EdgeProxy:
    """Gestiona el proxy streaming que publica la única entrada HTTP."""

    def __init__(
        self,
        root: Path,
        listen_port: int,
        dashboard_port: int,
        litellm_port: int,
        listen_host: str = "127.0.0.1",
        ssl_certificate: Path | None = None,
        ssl_certificate_key: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.listen_port = listen_port
        self.dashboard_port = dashboard_port
        self.litellm_port = litellm_port
        if listen_host not in {"127.0.0.1", "0.0.0.0"}:
            raise RuntimeError("IA_GATEWAY_BIND_HOST sólo puede ser 127.0.0.1 o 0.0.0.0")
        self.listen_host = listen_host
        self.ssl_certificate = ssl_certificate
        self.ssl_certificate_key = ssl_certificate_key
        self.runtime_dir = self.root / "runtime" / "edge"
        self.log_file = self.runtime_dir / "edge.log"
        self.process: subprocess.Popen[str] | None = None
        self._output = None

    def _command(self) -> list[str]:
        command = [
            os.environ.get("IA_GATEWAY_PYTHON", "").strip() or os.sys.executable,
            "-m",
            "gateway.edge_server",
            "--host",
            self.listen_host,
            "--port",
            str(self.listen_port),
            "--dashboard-port",
            str(self.dashboard_port),
            "--litellm-port",
            str(self.litellm_port),
        ]
        if self.ssl_certificate and self.ssl_certificate_key:
            command.extend(
                ["--cert", str(self.ssl_certificate), "--key", str(self.ssl_certificate_key)]
            )
        return command

    def start(self) -> None:
        """Mantiene el proxy Python como proceso hijo independiente."""

        if self.process is not None and self.process.poll() is None:
            return
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._output = self.log_file.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            self._command(),
            cwd=self.root,
            stdout=self._output,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"El proxy HTTP terminó durante el arranque; revisa {self.log_file}")
            try:
                with socket.create_connection(("127.0.0.1", self.listen_port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"El proxy HTTP no abrió el puerto público {self.listen_port}")

    def stop(self) -> None:
        """Detiene únicamente el proceso proxy creado por esta aplicación."""

        process = self.process
        if not process:
            return
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self._output is not None:
            self._output.close()
            self._output = None
