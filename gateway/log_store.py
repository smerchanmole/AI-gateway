"""Repositorio SQLite para eventos de inferencia.

Este módulo no sabe nada de HTTP ni de LiteLLM. Recibe valores ya extraídos,
redacta secretos y ofrece operaciones pequeñas de escritura/lectura. Esa frontera
hace posible probar privacidad y migraciones sin arrancar ningún modelo.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SECRET_KEYS = {"authorization", "api_key", "apikey", "token", "access_token", "secret"}
MADRID = ZoneInfo("Europe/Madrid")


def daily_log_path(runtime_dir: Path, day: date | None = None) -> Path:
    """Separa físicamente cada jornada según el calendario de Madrid."""
    selected = day or datetime.now(MADRID).date()
    return runtime_dir / "logs" / f"{selected.isoformat()}.sqlite3"


def parse_day(value: str | None) -> date:
    """Valida ``AAAA-MM-DD`` o elige hoy según Europe/Madrid."""

    try:
        return date.fromisoformat(value) if value else datetime.now(MADRID).date()
    except ValueError as exc:
        raise ValueError("La fecha debe tener formato AAAA-MM-DD") from exc


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
        "started_at": "TEXT", "origin_ip": "TEXT", "provider_ip": "TEXT", "ttft_ms": "INTEGER",
        "guardrail_status": "TEXT", "guardrail_reason": "TEXT"
    }.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE requests ADD COLUMN {name} {column_type}")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model, id DESC)")
    return connection


def insert_log(db_path: Path, model: str, status: str, duration_ms: int | None,
               request: Any, response: Any, error: str | None = None, *,
               started_at: str | None = None, origin_ip: str | None = None,
               provider_ip: str | None = None, ttft_ms: int | None = None,
               guardrail_status: str | None = None, guardrail_reason: str | None = None) -> None:
    """Inserta un evento en una transacción corta para no bloquear el callback."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as connection:
        connection.execute(
            """INSERT INTO requests(
            model,status,duration_ms,request_json,response_json,error,
            started_at,origin_ip,provider_ip,ttft_ms,guardrail_status,guardrail_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (model, status, duration_ms,
             json.dumps(safe_value(request), ensure_ascii=False, default=str),
             json.dumps(safe_value(response), ensure_ascii=False, default=str), error,
             started_at, origin_ip, provider_ip, ttft_ms, guardrail_status, guardrail_reason),
        )


def read_logs(db_path: Path, model: str, limit: int = 100) -> list[dict[str, Any]]:
    """Hidrata JSON y devuelve primero las llamadas más recientes."""
    if not db_path.exists():
        return []
    with connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM requests WHERE model=? ORDER BY id DESC LIMIT ?", (model, min(limit, 50_000))
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json") or "null")
        item["response"] = json.loads(item.pop("response_json") or "null")
        result.append(item)
    return result


def _madrid_day(timestamp: str | None) -> date | None:
    """Convierte un ISO timestamp al día civil usado por el dashboard."""

    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(MADRID).date()
    except ValueError:
        return None


def read_day_logs(runtime_dir: Path, model: str, day: date, limit: int = 5000) -> list[dict[str, Any]]:
    """Lee el fichero diario y suma registros del antiguo fichero monolítico."""
    rows = read_logs(daily_log_path(runtime_dir, day), model, limit)
    legacy = runtime_dir / "requests.sqlite3"
    if legacy.exists():
        legacy_rows = read_logs(legacy, model, 10_000)
        rows.extend(item for item in legacy_rows if _madrid_day(item.get("started_at") or item.get("created_at")) == day)
    rows.sort(key=lambda item: item.get("started_at") or item.get("created_at") or "", reverse=True)
    return rows[:limit]


def available_days(runtime_dir: Path, model: str) -> list[str]:
    """Descubre jornadas con datos para el alias, incluyendo formato legado."""

    days = {path.stem for path in (runtime_dir / "logs").glob("????-??-??.sqlite3")}
    legacy = runtime_dir / "requests.sqlite3"
    if legacy.exists():
        days.update(day.isoformat() for item in read_logs(legacy, model, 10_000)
                    if (day := _madrid_day(item.get("started_at") or item.get("created_at"))))
    return sorted(days, reverse=True)


def log_kpis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Resume volumen, éxito, latencia, tokens y distribución por hora."""

    durations = sorted(int(row["duration_ms"]) for row in rows if row.get("duration_ms") is not None)
    ttfts = [int(row["ttft_ms"]) for row in rows if row.get("ttft_ms") is not None]
    success = sum(row.get("status") == "success" for row in rows)
    usages = [row.get("response", {}).get("usage", {}) for row in rows if isinstance(row.get("response"), dict)]
    prompt_tokens = sum(int(usage.get("prompt_tokens") or 0) for usage in usages)
    completion_tokens = sum(int(usage.get("completion_tokens") or 0) for usage in usages)
    hourly = [0] * 24
    for row in rows:
        stamp = row.get("started_at") or row.get("created_at")
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
            hourly[parsed.astimezone(MADRID).hour] += 1
        except (ValueError, TypeError):
            pass
    percentile = durations[min(len(durations) - 1, int(len(durations) * .95))] if durations else None
    return {"requests": len(rows), "success_rate": round(success / len(rows) * 100, 1) if rows else None,
            "errors": len(rows) - success, "avg_duration_ms": round(sum(durations) / len(durations)) if durations else None,
            "p95_duration_ms": percentile, "avg_ttft_ms": round(sum(ttfts) / len(ttfts)) if ttfts else None,
            "guardrail_warnings": sum(row.get("guardrail_status") == "warning" for row in rows),
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens, "hourly": hourly}
