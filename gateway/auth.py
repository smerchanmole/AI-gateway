"""Autenticación local del panel sin almacenar contraseñas ni sesiones en claro.

El navegador recibe un identificador de sesión aleatorio. En disco sólo se
conserva su huella SHA-256; por tanto, copiar el fichero de sesiones no permite
reutilizarlas. La contraseña se deriva con Argon2id, una función deliberadamente
costosa en CPU y memoria que dificulta ataques de fuerza bruta fuera de línea.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError


class AuthenticationError(RuntimeError):
    """Error de credenciales que no revela qué parte era incorrecta."""


class LoginRateLimited(AuthenticationError):
    """Demasiados intentos recientes desde una misma dirección."""


class AuthStore:
    """Gestiona el único usuario ``admin``, sus sesiones y el control CSRF."""

    username = "admin"
    cookie_name = "ia_gateway_session"
    session_seconds = 8 * 60 * 60
    max_attempts = 5
    attempt_window_seconds = 15 * 60
    password_hasher = PasswordHasher(
        time_cost=3, memory_cost=64 * 1024, parallelism=4,
        hash_len=32, salt_len=16,
    )

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.credentials_file = runtime_dir / "admin-auth.json"
        self.sessions_file = runtime_dir / "admin-sessions.json"
        self._lock = threading.RLock()
        self._attempts: dict[str, list[float]] = {}
        runtime_dir.mkdir(parents=True, exist_ok=True)
        if not self.credentials_file.exists():
            self._write_json(self.credentials_file, {
                "username": self.username,
                "password_hash": self._hash_password("admin"),
                "password_version": 1,
                "must_change_password": True,
            })

    @classmethod
    def _hash_password(cls, password: str) -> str:
        return cls.password_hasher.hash(password)

    @classmethod
    def _verify_password(cls, password: str, encoded: str) -> bool:
        try:
            return cls.password_hasher.verify(encoded, password)
        except (VerificationError, ValueError, TypeError):
            return False

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return default

    @staticmethod
    def _session_key(token: str) -> str:
        return hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()

    def _credentials(self) -> dict[str, Any]:
        data = self._read_json(self.credentials_file, {})
        if data.get("username") != self.username or not data.get("password_hash"):
            raise RuntimeError("El almacén de autenticación está dañado")
        return data

    def _prune_attempts(self, address: str, now: float) -> list[float]:
        recent = [stamp for stamp in self._attempts.get(address, [])
                  if now - stamp < self.attempt_window_seconds]
        self._attempts[address] = recent
        return recent

    def login(self, username: str, password: str, address: str) -> tuple[str, dict[str, Any]]:
        """Valida con respuesta constante y crea una sesión de ocho horas."""
        with self._lock:
            now = time.time()
            attempts = self._prune_attempts(address, now)
            if len(attempts) >= self.max_attempts:
                raise LoginRateLimited("Demasiados intentos. Espera 15 minutos antes de volver a probar.")
            credentials = self._credentials()
            valid = hmac.compare_digest(username.strip().encode("utf-8"), self.username.encode("utf-8"))
            valid = self._verify_password(password, credentials["password_hash"]) and valid
            if not valid:
                attempts.append(now)
                # Una derivación real se ejecuta incluso si el usuario era erróneo.
                raise AuthenticationError("Usuario o contraseña incorrectos")
            self._attempts.pop(address, None)
            token = secrets.token_urlsafe(32)
            session = {
                "username": self.username,
                "csrf_token": secrets.token_urlsafe(32),
                "expires_at": now + self.session_seconds,
                "password_version": int(credentials.get("password_version", 1)),
            }
            sessions = self._active_sessions(now)
            sessions[self._session_key(token)] = session
            self._write_json(self.sessions_file, sessions)
            return token, self.public_status(session)

    def _active_sessions(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        now = now or time.time()
        sessions = self._read_json(self.sessions_file, {})
        return {key: value for key, value in sessions.items()
                if isinstance(value, dict) and float(value.get("expires_at", 0)) > now}

    def session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self._lock:
            sessions = self._active_sessions()
            session = sessions.get(self._session_key(token))
            if not session:
                return None
            credentials = self._credentials()
            if session.get("password_version") != credentials.get("password_version"):
                return None
            return session

    def public_status(self, session: dict[str, Any] | None) -> dict[str, Any]:
        credentials = self._credentials()
        return {
            "authenticated": session is not None,
            "username": self.username if session else None,
            "must_change_password": bool(credentials.get("must_change_password")) if session else False,
            "csrf_token": session.get("csrf_token") if session else None,
        }

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            sessions = self._active_sessions()
            sessions.pop(self._session_key(token), None)
            self._write_json(self.sessions_file, sessions)

    @staticmethod
    def validate_new_password(password: str) -> None:
        """Exige una frase robusta sin imponer una longitud poco práctica."""
        if len(password) < 12 or len(password) > 256:
            raise AuthenticationError("La nueva contraseña debe tener entre 12 y 256 caracteres")
        checks = [r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"]
        if not all(re.search(pattern, password) for pattern in checks):
            raise AuthenticationError("Incluye mayúscula, minúscula, número y símbolo")
        if password.lower() == "admin":
            raise AuthenticationError("La contraseña inicial no puede reutilizarse")

    def change_password(self, session: dict[str, Any], current: str, new: str) -> dict[str, Any]:
        with self._lock:
            credentials = self._credentials()
            if not self._verify_password(current, credentials["password_hash"]):
                raise AuthenticationError("La contraseña actual no es correcta")
            self.validate_new_password(new)
            version = int(credentials.get("password_version", 1)) + 1
            self._write_json(self.credentials_file, {
                "username": self.username,
                "password_hash": self._hash_password(new),
                "password_version": version,
                "must_change_password": False,
            })
            # Invalidar todas las demás sesiones reduce el impacto de una cookie robada.
            session["password_version"] = version
            # La sesión actual se reemitirá con un token nuevo desde el endpoint.
            self._write_json(self.sessions_file, {})
            return session

    def replace_session_after_password_change(self, session: dict[str, Any]) -> str:
        """Asigna un token nuevo y deja como válida únicamente la sesión actual."""
        with self._lock:
            token = secrets.token_urlsafe(32)
            self._write_json(self.sessions_file, {self._session_key(token): session})
            return token
