"""Descubrimiento opcional de servicios Cloudera sin exponer credenciales al navegador."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from datetime import datetime, timezone


class ClouderaCatalog:
    """Repositorio local y adaptador HTTP para el catálogo de Cloudera.

    Esta clase concentra tres responsabilidades que comparten las mismas
    credenciales: CRUD de conexiones, descubrimiento/prueba de endpoints y
    renovación de tokens. La frontera es deliberada: el resto de la aplicación
    sólo recibe estructuras saneadas y variables de entorno temporales.
    """

    SECRET_FIELDS = {"token", "workload_password", "cdp_private_key"}

    @staticmethod
    def _normalize_renewal_url(value: str) -> str:
        """Acepta una URL normal o un enlace Markdown copiado de documentación."""

        candidate = str(value or "").strip()
        markdown = re.fullmatch(r"\[[^\]]+\]\((https?://[^)]+)\)", candidate)
        if markdown:
            candidate = markdown.group(1).strip()
        candidate = candidate.strip("<>").rstrip("/")
        if not candidate:
            return ""
        parsed = urllib.parse.urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("La URL de renovación debe ser una URL HTTP o HTTPS válida")
        # IAM conserva el hostname histórico ``altus`` para el Control Plane
        # us-west-1. El nombre simétrico bajo ``cdp.cloudera.com`` parece
        # plausible, pero no tiene DNS y provoca NameResolutionError.
        if (parsed.hostname or "").lower() == "iamapi.us-west-1.cdp.cloudera.com":
            parsed = parsed._replace(netloc="iamapi.us-west-1.altus.cloudera.com")
            candidate = urllib.parse.urlunparse(parsed)
        return candidate

    def __init__(self, runtime_dir: Path) -> None:
        """Ubica el almacén SQLite privado y migra el JSON legado si existe."""

        self.path = runtime_dir / "cloudera.sqlite3"
        self.legacy_path = runtime_dir / "cloudera-connections.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._renewal_lock = threading.RLock()
        self._initialize_store()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def _initialize_store(self) -> None:
        """Crea SQLite e importa una sola vez el catálogo JSON anterior."""

        with self._renewal_lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS catalog_state (
                id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )"""
            )
            os.chmod(self.path, 0o600)
            exists = connection.execute("SELECT 1 FROM catalog_state WHERE id=1").fetchone()
            if exists:
                return
            initial: dict[str, Any] = {"connections": [], "model_tokens": {}}
            try:
                legacy = json.loads(self.legacy_path.read_text(encoding="utf-8"))
                if isinstance(legacy, dict):
                    initial = legacy
            except (OSError, ValueError):
                pass
            connection.execute(
                "INSERT INTO catalog_state(id,payload) VALUES(1,?)",
                (json.dumps(initial, ensure_ascii=False),),
            )

    def _read(self) -> dict[str, Any]:
        """Lee el documento del catálogo dentro de una transacción SQLite corta."""

        try:
            with self._renewal_lock, self._connect() as connection:
                row = connection.execute("SELECT payload FROM catalog_state WHERE id=1").fetchone()
            data = json.loads(row[0]) if row else {}
            return data if isinstance(data, dict) else {"connections": [], "model_tokens": {}}
        except (OSError, ValueError, sqlite3.Error):
            return {"connections": [], "model_tokens": {}}

    def _write(self, data: dict[str, Any]) -> None:
        """Actualiza el catálogo atómicamente y restringe permisos a su dueño."""

        payload = json.dumps(data, ensure_ascii=False)
        with self._renewal_lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO catalog_state(id,payload,updated_at)
                VALUES(1,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                updated_at=excluded.updated_at""",
                (payload,),
            )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _id(kind: str, url: str) -> str:
        """Crea un ID estable sin almacenar una segunda copia de la URL."""

        return hashlib.sha256(f"{kind}:{url.rstrip('/')}".encode()).hexdigest()[:12]

    @staticmethod
    def _token_metadata(token: str) -> dict[str, Any]:
        """Lee fechas declaradas de un JWT sin confundirlo con una verificación criptográfica."""
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            expiration = int(claims["exp"])
            return {"token_expires_at": datetime.fromtimestamp(expiration, timezone.utc).isoformat(),
                    "token_expired": expiration <= datetime.now(timezone.utc).timestamp()}
        except (IndexError, KeyError, ValueError, TypeError, json.JSONDecodeError):
            return {"token_expires_at": None, "token_expired": False}

    def connections(self) -> list[dict[str, Any]]:
        """Devuelve la vista pública de conexiones con indicadores de secretos."""

        return [{"platform": "cloud", "probe_interval_minutes": 5,
                 **{k: v for k, v in item.items() if k not in self.SECRET_FIELDS}} |
                {"has_token": bool(item.get("token")),
                 "has_workload_password": bool(item.get("workload_password")),
                 "has_cdp_private_key": bool(item.get("cdp_private_key")),
                 "renewal_ready": self._renewal_ready(item)} |
                (self._token_metadata(item.get("token", "")) if item.get("token") else {})
                for item in self._read().get("connections", [])]

    @staticmethod
    def _renewal_ready(connection: dict[str, Any]) -> bool:
        """Indica si hay datos suficientes para generar o renovar el token."""

        if not str(connection.get("renewal_url") or "").strip():
            return False
        if connection.get("platform", "cloud") == "cloud":
            return bool(connection.get("cdp_access_key_id") and connection.get("cdp_private_key"))
        return bool(connection.get("workload_user") and connection.get("workload_password"))

    def save_connection(self, name: str, kind: str, url: str, token: str = "", platform: str = "cloud",
                        probe_interval_minutes: int = 5, workload_user: str = "", workload_password: str = "",
                        cdp_access_key_id: str = "", cdp_private_key: str = "", renewal_url: str = "",
                        workload_name: str = "DE") -> dict[str, Any]:
        """Da de alta una conexión tras validar tipo, plataforma, URL y JWT.

        Una URL completa de endpoint se reduce a esquema+dominio porque las
        APIs de descubrimiento viven en ese origen. Un campo secreto vacío
        conserva el valor anterior, lo que permite editar sin exponerlo.
        """

        if kind not in {"inference", "workbench"}: raise RuntimeError("Tipo de conexión Cloudera no válido")
        if platform not in {"cloud", "onpremise"}: raise RuntimeError("La instalación debe ser Cloud u On-premise")
        if not 1 <= probe_interval_minutes <= 1440: raise RuntimeError("El intervalo debe estar entre 1 y 1440 minutos")
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc: raise RuntimeError("La URL Cloudera no es válida")
        if parsed.hostname and parsed.hostname.startswith("console."):
            raise RuntimeError("Has pegado la URL de la consola CDP. Abre un Model Endpoint y usa el dominio que empieza por ml-")
        # Es habitual copiar la URL completa de un endpoint. Para descubrir el
        # catálogo sólo necesitamos su origen; normalizar aquí evita un 404 poco claro.
        normalized = f"{parsed.scheme}://{parsed.netloc}"
        metadata = self._token_metadata(token.strip()) if token.strip() else {}
        if metadata.get("token_expired"):
            raise RuntimeError(f"El JWT está caducado desde {metadata['token_expires_at']}. Genera uno nuevo en Cloudera")
        data = self._read(); connection_id = self._id(kind, normalized)
        previous = next((item for item in data.get("connections", []) if item.get("id") == connection_id), {})
        entry = {"id": connection_id, "name": name.strip() or parsed.netloc, "kind": kind, "url": normalized,
                 "platform": platform, "probe_interval_minutes": probe_interval_minutes,
                 "token": token.strip() or previous.get("token", ""),
                 "workload_user": workload_user.strip() or previous.get("workload_user", ""),
                 "workload_password": workload_password or previous.get("workload_password", ""),
                 "cdp_access_key_id": cdp_access_key_id.strip() or previous.get("cdp_access_key_id", ""),
                 "cdp_private_key": cdp_private_key.strip() or previous.get("cdp_private_key", ""),
                 "renewal_url": self._normalize_renewal_url(renewal_url) or previous.get("renewal_url", ""),
                 "workload_name": workload_name.strip() or previous.get("workload_name", "DE")}
        data["connections"] = [item for item in data.get("connections", []) if item.get("id") != connection_id] + [entry]
        self._write(data)
        return next(item for item in self.connections() if item["id"] == connection_id)

    def delete_connection(self, connection_id: str) -> None:
        """Borra una conexión y todas sus credenciales específicas de modelo."""

        data = self._read()
        data["connections"] = [item for item in data.get("connections", []) if item.get("id") != connection_id]
        data["model_tokens"] = {k: v for k, v in data.get("model_tokens", {}).items() if not k.startswith(f"{connection_id}:")}
        self._write(data)

    def update_connection(self, connection_id: str, name: str, kind: str, url: str, token: str = "",
                          platform: str = "cloud", probe_interval_minutes: int = 5, workload_user: str = "",
                          workload_password: str = "", cdp_access_key_id: str = "", cdp_private_key: str = "",
                          renewal_url: str = "", workload_name: str = "DE") -> dict[str, Any]:
        """Edita una conexión, conservando secretos si los campos no cambian."""
        if kind not in {"inference", "workbench"}: raise RuntimeError("Tipo de conexión Cloudera no válido")
        data = self._read()
        previous = next((item for item in data.get("connections", []) if item.get("id") == connection_id), None)
        if not previous:
            if not token.strip():
                raise RuntimeError("La conexión que estabas editando ya no existe. Cancela la edición o pega de nuevo la credencial para recrearla")
            return self.save_connection(name, kind, url, token, platform, probe_interval_minutes, workload_user,
                                        workload_password, cdp_access_key_id, cdp_private_key, renewal_url, workload_name)
        if platform not in {"cloud", "onpremise"}: raise RuntimeError("La instalación debe ser Cloud u On-premise")
        if not 1 <= probe_interval_minutes <= 1440: raise RuntimeError("El intervalo debe estar entre 1 y 1440 minutos")
        # Reutilizamos la validación/normalización y luego migramos credenciales
        # de modelos si el cambio de URL produce un identificador nuevo.
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc: raise RuntimeError("La URL Cloudera no es válida")
        if parsed.hostname and parsed.hostname.startswith("console."):
            raise RuntimeError("Has pegado la URL de la consola CDP. Abre un Model Endpoint y usa el dominio que empieza por ml-")
        normalized = f"{parsed.scheme}://{parsed.netloc}"
        metadata = self._token_metadata(token.strip()) if token.strip() else {}
        if metadata.get("token_expired"):
            raise RuntimeError(f"El JWT está caducado desde {metadata['token_expires_at']}. Genera uno nuevo en Cloudera")
        new_id = self._id(kind, normalized)
        entry = {"id": new_id, "name": name.strip() or parsed.netloc, "kind": kind, "url": normalized,
                 "platform": platform, "probe_interval_minutes": probe_interval_minutes,
                 "token": token.strip() or previous.get("token", ""),
                 "workload_user": workload_user.strip() or previous.get("workload_user", ""),
                 "workload_password": workload_password or previous.get("workload_password", ""),
                 "cdp_access_key_id": cdp_access_key_id.strip() or previous.get("cdp_access_key_id", ""),
                 "cdp_private_key": cdp_private_key.strip() or previous.get("cdp_private_key", ""),
                 "renewal_url": self._normalize_renewal_url(renewal_url) or previous.get("renewal_url", ""),
                 "workload_name": workload_name.strip() or previous.get("workload_name", "DE")}
        data["connections"] = [item for item in data.get("connections", []) if item.get("id") not in {connection_id, new_id}] + [entry]
        migrated = {}
        for key, value in data.get("model_tokens", {}).items():
            migrated[f"{new_id}:{key.split(':', 1)[1]}" if key.startswith(f"{connection_id}:") else key] = value
        data["model_tokens"] = migrated
        self._write(data)
        return next(item for item in self.connections() if item["id"] == new_id)

    def save_model_token(self, connection_id: str, external_id: str, token: str) -> str:
        """Asocia un token a un endpoint y devuelve su variable de entorno."""

        if not self._connection_record(connection_id): raise KeyError(connection_id)
        metadata = self._token_metadata(token.strip())
        if metadata.get("token_expired"):
            raise RuntimeError(f"El JWT del modelo ya está caducado desde {metadata['token_expires_at']}")
        data = self._read(); key = f"{connection_id}:{external_id}"
        data.setdefault("model_tokens", {})[key] = token.strip(); self._write(data)
        return self.environment_name(connection_id, external_id)

    def _connection_record(self, connection_id: str) -> dict[str, Any] | None:
        """Busca el registro privado sin exigir que su credencial sea válida."""

        return next((item for item in self._read().get("connections", []) if item.get("id") == connection_id), None)

    def model_credential(self, connection_id: str, external_id: str) -> tuple[str, str, dict[str, Any]]:
        """Resuelve prioridad: token del modelo primero, token general después."""

        data = self._read(); specific = data.get("model_tokens", {}).get(f"{connection_id}:{external_id}")
        connection = next((item for item in data.get("connections", []) if item.get("id") == connection_id), None)
        if not connection: raise KeyError(connection_id)
        token = specific or connection.get("token", "")
        if not token: raise RuntimeError("No hay credencial de modelo ni CDP token para realizar la prueba")
        return token, "Token del modelo" if specific else "CDP token de la conexión", self._token_metadata(token)

    def model_token_status(self, connection_id: str, external_id: str) -> dict[str, Any]:
        """Convierte una credencial privada en estado presentable por la UI."""

        try:
            _token, source, metadata = self.model_credential(connection_id, external_id)
            return {"credential_source": source, "credential_available": True, **metadata}
        except RuntimeError:
            return {"credential_source": None, "credential_available": False,
                    "token_expires_at": None, "token_expired": False}

    def renew_token(self, connection_id: str, force: bool = False) -> dict[str, Any]:
        """Genera o renueva un token y sustituye atómicamente la credencial."""
        data = self._read()
        connection = next((item for item in data.get("connections", []) if item.get("id") == connection_id), None)
        if not connection: raise KeyError(connection_id)
        token_missing = not str(connection.get("token") or "").strip()
        metadata = self._token_metadata(connection.get("token", ""))
        expires_at = metadata.get("token_expires_at")
        if token_missing and not force and not self._renewal_ready(connection):
            return {"renewed": False, "generated": False,
                    "message": "Falta el CDP token o completar los datos de generación", **metadata}
        if not token_missing and not expires_at and not force:
            return {"renewed": False, "message": "La credencial no declara caducidad", **metadata}
        if expires_at and not force:
            remaining = datetime.fromisoformat(expires_at).timestamp() - datetime.now(timezone.utc).timestamp()
            if remaining > 600:
                return {"renewed": False, "message": "El token todavía no está próximo a caducar", **metadata}

        renewal_url = self._normalize_renewal_url(connection.get("renewal_url", ""))
        if not renewal_url:
            raise RuntimeError("Configura la URL de renovación de esta conexión")
        # Repara también registros creados por versiones anteriores que
        # guardaron literalmente un enlace Markdown en lugar de su destino.
        if renewal_url != connection.get("renewal_url"):
            connection["renewal_url"] = renewal_url
            self._write(data)
        operation_message = ("Generando el CDP token inicial…" if token_missing else
                             "Token caducado o próximo a caducar, generando de nuevo…")
        connection.update({"renewal_state": "renewing",
                           "renewal_message": operation_message,
                           "renewal_last_attempt": datetime.now(timezone.utc).isoformat()})
        self._write(data)
        if connection.get("platform", "cloud") == "cloud":
            access_key = connection.get("cdp_access_key_id", "")
            private_key = connection.get("cdp_private_key", "")
            if not access_key or not private_key:
                raise RuntimeError("Para Cloud se necesitan CDP_ACCESS_KEY_ID y CDP_PRIVATE_KEY")
            executable = self.path.parent.parent / ".venv" / "bin" / "cdp"
            if not executable.exists():
                raise RuntimeError("No está instalado CDP CLI. Ejecuta de nuevo la instalación de requirements.txt")
            env = os.environ.copy()
            env.update({"CDP_ACCESS_KEY_ID": access_key, "CDP_PRIVATE_KEY": private_key})
            try:
                timeout = max(10, min(300, int(os.environ.get("CDP_RENEWAL_TIMEOUT_SECONDS", "60"))))
            except ValueError:
                timeout = 60
            try:
                completed = subprocess.run([str(executable), "--endpoint-url", renewal_url, "iam",
                    "generate-workload-auth-token", "--workload-name", connection.get("workload_name") or "DE"],
                    capture_output=True, text=True, timeout=timeout, env=env)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"CDP CLI no respondió en {timeout} segundos. Comprueba que la WebApp tenga "
                    f"salida HTTPS hacia {urllib.parse.urlparse(renewal_url).hostname}"
                ) from exc
            if completed.returncode:
                raise RuntimeError(f"CDP CLI no pudo renovar el token: {completed.stderr.strip()[-500:]}")
            try: payload = json.loads(completed.stdout)
            except ValueError as exc: raise RuntimeError("CDP CLI devolvió una respuesta no válida") from exc
            token = payload.get("token")
            declared_expiry = payload.get("expireAt")
        else:
            user, password = connection.get("workload_user", ""), connection.get("workload_password", "")
            if not user or not password:
                raise RuntimeError("Para On-premise se necesitan WORKLOAD-USER y WORKLOAD-PASS")
            basic = base64.b64encode(f"{user}:{password}".encode()).decode()
            request = urllib.request.Request(renewal_url, method="GET",
                headers={"Authorization": f"Basic {basic}", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=20) as response: payload = json.load(response)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"Knox rechazó la renovación con HTTP {exc.code}") from exc
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"No se pudo renovar mediante Knox: {exc}") from exc
            token = payload.get("access_token")
            declared_expiry = None
        if not token: raise RuntimeError("La respuesta de renovación no contiene ningún token")
        connection["token"] = token
        connection["token_renewed_at"] = datetime.now(timezone.utc).isoformat()
        connection["renewal_state"] = "ok"
        connection["renewal_message"] = ("Token inicial generado correctamente" if token_missing else
                                         "Token renovado correctamente")
        if declared_expiry: connection["token_expire_at"] = declared_expiry
        self._write(data)
        fresh = self._token_metadata(token)
        return {"renewed": True, "generated": token_missing,
                "message": ("Token generado y guardado" if token_missing else "Token renovado y guardado"), **fresh,
                "declared_expire_at": declared_expiry}

    def record_renewal_error(self, connection_id: str, message: str) -> None:
        """Persiste un fallo de generación/renovación para hacerlo visible en UI."""

        with self._renewal_lock:
            data = self._read()
            connection = next(
                (item for item in data.get("connections", []) if item.get("id") == connection_id),
                None,
            )
            if connection:
                connection.update({
                    "renewal_state": "error",
                    "renewal_message": message,
                    "renewal_next_retry": "Dentro de 1 minuto",
                })
                self._write(data)

    def renew_due_tokens(self) -> list[dict[str, Any]]:
        """Genera tokens ausentes y renueva los próximos a caducar."""
        results = []
        with self._renewal_lock:
            for connection in list(self._read().get("connections", [])):
                try:
                    result = self.renew_token(connection["id"], force=False)
                    if result.get("renewed"):
                        results.append({"connection_id": connection["id"], **result})
                except RuntimeError as exc:
                    self.record_renewal_error(str(connection.get("id")), str(exc))
                    results.append({"connection_id": connection.get("id"), "renewed": False, "error": str(exc)})
        return results

    def probe_model(self, connection_id: str, external_id: str, url: str, protocol: str,
                    model_name: str = "", task: str = "", has_chat_template: bool = True) -> dict[str, Any]:
        """Hace una inferencia mínima en la URL exacta publicada por Cloudera.

        Un endpoint OpenAI de Cloudera no está obligado a exponer ``/models``.
        La única prueba concluyente es usar el contrato y la URL anunciados por
        ``listEndpoints``/``describeEndpoint``; limitamos la salida a un token.
        """
        connection = self._connection_record(connection_id)
        if not connection: raise KeyError(connection_id)
        endpoint = urllib.parse.urlparse(url)
        if endpoint.scheme not in {"http", "https"} or endpoint.hostname != urllib.parse.urlparse(connection["url"]).hostname:
            raise RuntimeError("La URL del modelo no pertenece a la conexión Cloudera seleccionada")
        token, source, metadata = self.model_credential(connection_id, external_id)
        if metadata.get("token_expired"):
            return {"ok": False, "reachable": False, "credential_source": source, **metadata,
                    "message": "La credencial está caducada; no se envió ninguna petición"}
        target = url.rstrip("/")
        payload = None
        method = "GET"
        if protocol == "openai":
            method = "POST"
            normalized_task = task.lower()
            if "embed" in normalized_task:
                if not target.endswith("/embeddings"):
                    target = f"{target}/embeddings"
                payload = {"model": model_name or external_id, "input": "health check"}
            elif has_chat_template:
                if not target.endswith("/chat/completions"):
                    target = f"{target}/chat/completions"
                payload = {"model": model_name or external_id,
                           "messages": [{"role": "user", "content": "Responde solo OK"}],
                           "max_tokens": 1, "temperature": 0, "stream": False}
            else:
                if not target.endswith("/completions"):
                    target = f"{target}/completions"
                payload = {"model": model_name or external_id, "prompt": "OK", "max_tokens": 1}
        elif "/v2/models/" in target:
            # En OIP la URL publicada ya acaba en /infer. Una comprobación real
            # requiere conocer el esquema tensorial del modelo, así que usamos
            # el readiness estándar sin fingir una inferencia válida.
            target = f"{target.split('/v2/models/', 1)[0]}/v2/health/ready"
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(target, data=body, method=method,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "Content-Type": "application/json"})
        started = datetime.now(timezone.utc)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                code = response.status
            return {"ok": 200 <= code < 300, "reachable": True, "http_status": code,
                    "latency_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                    "credential_source": source, **metadata,
                    "probe_url": target, "message": ("El modelo respondió a una inferencia mínima" if payload is not None
                                                        else "Endpoint preparado y credencial aceptada")}
        except urllib.error.HTTPError as exc:
            authenticated = exc.code not in {401, 403}
            return {"ok": False, "reachable": True, "http_status": exc.code,
                    "latency_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                    "credential_source": source, "probe_url": target, **metadata,
                    "message": ("Credencial rechazada o sin permiso de acceso" if not authenticated else
                                "El endpoint rechazó el formato de la prueba; revisa el protocolo y el tipo de tarea publicados por Cloudera")}
        except OSError as exc:
            return {"ok": False, "reachable": False, "http_status": None, "credential_source": source,
                    "probe_url": target, **metadata, "message": f"No se pudo conectar con el endpoint: {exc}"}

    @staticmethod
    def environment_name(connection_id: str, external_id: str) -> str:
        """Construye un nombre de entorno determinista para un token específico."""

        safe = re.sub(r"[^A-Z0-9]+", "_", external_id.upper()).strip("_")[:35]
        return f"CLOUDERA_{connection_id.upper()}_{safe}_TOKEN"

    @staticmethod
    def connection_environment_name(connection_id: str) -> str:
        """Construye el nombre compartido por los modelos de una conexión."""

        return f"CLOUDERA_{connection_id.upper()}_CDP_TOKEN"

    def environment(self) -> dict[str, str]:
        """Materializa secretos sólo para el entorno del subproceso LiteLLM."""

        data = self._read()
        result = {self.connection_environment_name(item["id"]): item["token"]
                  for item in data.get("connections", []) if item.get("id") and item.get("token")}
        for key, token in data.get("model_tokens", {}).items():
            connection_id, external_id = key.split(":", 1)
            result[self.environment_name(connection_id, external_id)] = token
        return result

    def _connection(self, connection_id: str) -> dict[str, Any]:
        """Obtiene una conexión lista para API o explica credencial/caducidad."""

        connection = next((item for item in self._read().get("connections", []) if item.get("id") == connection_id), None)
        if not connection: raise KeyError(connection_id)
        if not connection.get("token"): raise RuntimeError("Añade el CDP/API token antes de descubrir modelos")
        metadata = self._token_metadata(connection["token"])
        if metadata.get("token_expired"):
            raise RuntimeError(f"El JWT de esta conexión caducó en {metadata['token_expires_at']}. Edítala y pega un token nuevo")
        return connection

    @staticmethod
    def _request(url: str, token: str, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        """Cliente JSON pequeño que traduce errores HTTP a mensajes operables."""

        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response: return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            if exc.code in {401, 403}:
                message = "Credencial rechazada o sin permisos para listar endpoints"
            elif exc.code == 404:
                message = "Ruta API no encontrada. Comprueba que la URL contiene sólo el dominio de la instancia"
            else:
                message = f"Cloudera respondió HTTP {exc.code}"
            raise RuntimeError(f"{message}. Detalle: {detail or 'sin cuerpo de respuesta'}") from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"No se pudo consultar Cloudera: {exc}") from exc

    @staticmethod
    def _items(payload: Any, *keys: str) -> list[dict[str, Any]]:
        """Normaliza listas directas o colecciones envueltas por distintas APIs."""

        if isinstance(payload, list): return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in keys:
                if isinstance(payload.get(key), list): return payload[key]
        return []

    def discover(self, connection_id: str) -> list[dict[str, Any]]:
        """Descubre endpoints de AI Inference o deployments de Workbench.

        El resultado común incluye URL, estado, protocolo, modelo interno y
        credencial efectiva. Así JavaScript no necesita conocer las diferencias
        entre ``listEndpoints`` y el árbol proyectos→modelos→deployments.
        """

        connection = self._connection(connection_id)
        if connection["kind"] == "inference":
            payload = self._request(f"{connection['url']}/api/v1alpha1/listEndpoints", connection["token"], "POST", {"namespace": "serving-default"})
            endpoints = self._items(payload, "endpoints", "items")
            discovered = []
            for item in endpoints:
                external_id = str(item.get("name") or item.get("id"))
                detail: dict[str, Any] = {}
                try:
                    described = self._request(f"{connection['url']}/api/v1alpha1/describeEndpoint",
                        connection["token"], "POST", {"namespace": item.get("namespace") or "serving-default", "name": external_id})
                    if isinstance(described, dict): detail = described
                except RuntimeError:
                    # listEndpoints ya contiene los campos esenciales y debe
                    # seguir siendo útil aunque el detalle no esté permitido.
                    pass
                merged = {**item, **detail}
                api_standard = str(merged.get("api_standard") or merged.get("apiStandard") or "").lower()
                endpoint_url = str(merged.get("url") or "")
                task = str(merged.get("task") or "")
                chat_value = merged.get("has_chat_template", merged.get("hasChatTemplate"))
                has_chat_template = (bool(chat_value) if chat_value is not None else
                                     endpoint_url.rstrip("/").endswith("/v1") and "embed" not in task.lower())
                # CAII no siempre usa exactamente el literal "openai" en sus
                # distintas versiones. Los NIM de texto también se identifican
                # por su base /v1 o su plantilla de chat. OIP publica /v2/infer.
                openai_protocol = (
                    "openai" in api_standard
                    or endpoint_url.rstrip("/").endswith("/v1")
                    or "/openai/v1" in endpoint_url.lower()
                    or has_chat_template
                    or ("generation" in task.lower() and "/v2/models/" not in endpoint_url.lower())
                )
                protocol = "openai" if openai_protocol else "open-inference"
                conditions = merged.get("conditions") if isinstance(merged.get("conditions"), list) else []
                discovered.append({"connection_id": connection_id, "source": "Cloudera AI Inference",
                    "external_id": external_id, "name": external_id,
                    "url": endpoint_url, "state": merged.get("state") or merged.get("status") or "desconocido",
                    "protocol": protocol, "api_standard": api_standard or "desconocido",
                    "model_name": merged.get("model_name") or merged.get("modelName") or external_id,
                    "task": task, "has_chat_template": has_chat_template,
                    "replica_count": merged.get("replica_count"), "conditions": conditions,
                    "url_source": "describeEndpoint" if detail.get("url") else "listEndpoints",
                    "has_model_token": f"{connection_id}:{external_id}" in self._read().get("model_tokens", {}),
                    "api_key_env": (self.environment_name(connection_id, external_id)
                                    if f"{connection_id}:{external_id}" in self._read().get("model_tokens", {})
                                    else self.connection_environment_name(connection_id)),
                    **self.model_token_status(connection_id, external_id)})
            return discovered
        projects = self._items(self._request(f"{connection['url']}/api/v2/projects?page_size=100", connection["token"]), "projects", "items")
        discovered = []
        for project in projects[:50]:
            project_id = str(project.get("id") or "")
            if not project_id: continue
            models = self._items(self._request(f"{connection['url']}/api/v2/projects/{project_id}/models?page_size=100", connection["token"]), "models", "items")
            for item in models:
                external_id = str(item.get("id") or item.get("name"))
                try:
                    deployment_payload = self._request(
                        f"{connection['url']}/api/v2/projects/{project_id}/models/{external_id}/deployments?page_size=100",
                        connection["token"])
                    deployments = self._items(deployment_payload, "deployments", "items")
                except RuntimeError:
                    deployments = []
                candidates = deployments or [item.get("latest_deployment") or item]
                for deployment in candidates:
                    if not isinstance(deployment, dict): continue
                    discovered.append({"connection_id": connection_id, "source": "Cloudera AI Workbench", "project": project.get("name") or project_id,
                        "external_id": external_id, "deployment_id": deployment.get("id"), "name": item.get("name") or external_id,
                        "url": deployment.get("endpoint_url") or deployment.get("url") or deployment.get("model_endpoint") or item.get("endpoint_url") or item.get("url") or "",
                        "state": deployment.get("status") or item.get("status") or "detectado",
                        "protocol": "workbench", "has_model_token": f"{connection_id}:{external_id}" in self._read().get("model_tokens", {}),
                        "api_key_env": (self.environment_name(connection_id, external_id)
                                        if f"{connection_id}:{external_id}" in self._read().get("model_tokens", {})
                                        else self.connection_environment_name(connection_id)),
                        **self.model_token_status(connection_id, external_id)})
        return discovered
