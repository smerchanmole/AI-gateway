"""Repositorio SQLite para eventos de inferencia.

Este módulo no sabe nada de HTTP ni de LiteLLM. Recibe valores ya extraídos,
redacta secretos y ofrece operaciones pequeñas de escritura/lectura. Esa frontera
hace posible probar privacidad y migraciones sin arrancar ningún modelo.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


SECRET_KEYS = {"authorization", "api_key", "apikey", "token", "access_token", "secret"}


def safe_value(value: Any, depth: int = 0) -> Any:
    """Convierte objetos complejos a JSON y oculta secretos por nombre de campo."""
    if depth > 8:
        return "[profundidad limitada]"
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    elif hasattr(value, "dict") and callable(value.dict):
        value = value.dict()
    if isinstance(value, dict):
        return {
            str(k): "[OCULTO]" if str(k).lower() in SECRET_KEYS else safe_value(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [safe_value(v, depth + 1) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def connect(db_path: Path) -> sqlite3.Connection:
    """Abre SQLite, activa WAL y aplica migraciones aditivas idempotentes."""
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        model TEXT NOT NULL, status TEXT NOT NULL, duration_ms INTEGER,
        request_json TEXT, response_json TEXT, error TEXT
        )"""
    )
    # No usamos un framework de migración para cuatro columnas, pero mantenemos
    # la propiedad esencial de una migración: ejecutarla N veces equivale a una.
    existing = {row[1] for row in connection.execute("PRAGMA table_info(requests)")}
    for name, column_type in {
        "started_at": "TEXT", "origin_ip": "TEXT", "provider_ip": "TEXT", "ttft_ms": "INTEGER"
    }.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE requests ADD COLUMN {name} {column_type}")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model, id DESC)")
    return connection


def insert_log(db_path: Path, model: str, status: str, duration_ms: int | None,
               request: Any, response: Any, error: str | None = None, *,
               started_at: str | None = None, origin_ip: str | None = None,
               provider_ip: str | None = None, ttft_ms: int | None = None) -> None:
    """Inserta un evento en una transacción corta para no bloquear el callback."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as connection:
        connection.execute(
            """INSERT INTO requests(
            model,status,duration_ms,request_json,response_json,error,
            started_at,origin_ip,provider_ip,ttft_ms
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (model, status, duration_ms,
             json.dumps(safe_value(request), ensure_ascii=False, default=str),
             json.dumps(safe_value(response), ensure_ascii=False, default=str), error,
             started_at, origin_ip, provider_ip, ttft_ms),
        )


def read_logs(db_path: Path, model: str, limit: int = 100) -> list[dict[str, Any]]:
    """Hidrata JSON y devuelve primero las llamadas más recientes."""
    if not db_path.exists():
        return []
    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM requests WHERE model=? ORDER BY id DESC LIMIT ?", (model, min(limit, 500))
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json") or "null")
        item["response"] = json.loads(item.pop("response_json") or "null")
        result.append(item)
    return result
