"""Callback de observabilidad ejecutado dentro del proceso LiteLLM.

Un callback debe ser tolerante a fallos: observar nunca puede impedir que el
modelo responda. Las funciones auxiliares aceptan datos incompletos porque cada
proveedor y cada tipo de petición puede aportar metadatos ligeramente distintos.
"""

from __future__ import annotations

import os
import asyncio
import json
import socket
import ssl
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import urllib.request
from zoneinfo import ZoneInfo

from litellm.integrations.custom_logger import CustomLogger
import litellm

from gateway.log_store import daily_log_path, insert_log


def _root() -> Path:
    """Recupera la raíz que el proceso padre inyectó al arrancar LiteLLM."""

    return Path(os.environ.get("IA_GATEWAY_ROOT", Path.cwd()))


def _configure_private_ca() -> None:
    """Hace que el cliente OpenAI/aiohttp de LiteLLM confíe en las CA privadas.

    LiteLLM 1.83 crea el cliente OpenAI compartido sin trasladar el
    ``ssl_verify`` del deployment. Instalar aquí un contexto global conserva
    las CA públicas, añade todos los bundles Cloudera y mantiene verificación de
    cadena, fecha y hostname. Sólo relajamos X509_STRICT para PKI privadas
    antiguas que no publican Authority Key Identifier.
    """

    bundles = sorted((_root() / "runtime" / "cloudera-ca").glob("*.pem"))
    if not bundles:
        return
    context = ssl.create_default_context()
    loaded = False
    for bundle in bundles:
        try:
            context.load_verify_locations(cafile=str(bundle))
            loaded = True
        except (OSError, ssl.SSLError):
            continue
    if not loaded:
        return
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict:
        context.verify_flags &= ~strict
    litellm.ssl_verify = context


_configure_private_ca()


def _log_path(start: Any) -> Path:
    """Asigna la llamada al fichero diario correspondiente a su inicio."""

    day = start.astimezone(ZoneInfo("Europe/Madrid")).date() if isinstance(start, datetime) else None
    return daily_log_path(_root() / "runtime", day)


def _model(kwargs: dict[str, Any]) -> str:
    """Prefiere el alias público (`model_group`) al identificador del proveedor."""
    metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    return str(
        metadata.get("dashboard_model_alias")
        or metadata.get("model_group")
        or metadata.get("user_api_key_model")
        or kwargs.get("model")
        or "desconocido"
    )


def _duration_ms(start: Any, end: Any) -> int | None:
    """Devuelve milisegundos enteros o ``None`` si faltan relojes válidos."""

    if isinstance(start, datetime) and isinstance(end, datetime):
        return round((end - start).total_seconds() * 1000)
    return None


def _metadata(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Fusiona metadatos de llamada y payload estándar con prioridad al estándar."""
    params_metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    standard = kwargs.get("standard_logging_object") or {}
    if hasattr(standard, "model_dump"):
        standard = standard.model_dump()
    standard_metadata = standard.get("metadata", {}) if isinstance(standard, dict) else {}
    return {**params_metadata, **standard_metadata}


def _origin_ip(kwargs: dict[str, Any]) -> str | None:
    """Prefiere la IP preservada por nuestro proxy al salto local de LiteLLM."""
    metadata = _metadata(kwargs)
    raw_headers = metadata.get("headers") or {}
    headers = {str(key).lower(): value for key, value in raw_headers.items()}
    value = (headers.get("x-ia-gateway-client-ip") or
             headers.get("x-envoy-external-address") or
             headers.get("x-forwarded-for") or
             headers.get("x-real-ip") or
             metadata.get("requester_ip_address") or metadata.get("client_ip"))
    return str(value).split(",", 1)[0].strip() if value else None


def _provider_ip(kwargs: dict[str, Any]) -> str | None:
    """Resuelve la IPv4 efectiva del endpoint; puede variar por balanceo DNS."""
    params = kwargs.get("litellm_params") or {}
    api_base = params.get("api_base") or kwargs.get("api_base")
    model = str(kwargs.get("model") or "")
    if not api_base and model.startswith("openai/"):
        api_base = "https://api.openai.com"
    if not api_base:
        return None
    host = urlparse(str(api_base)).hostname
    if not host:
        return None
    try:
        addresses = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        return addresses[0][4][0] if addresses else host
    except OSError:
        return host


def _started_at(start: Any) -> str | None:
    """Serializa la fecha conservando zona horaria para análisis posteriores."""

    return start.isoformat() if isinstance(start, datetime) else None


def _ttft_ms(kwargs: dict[str, Any], start: Any, end: Any) -> int | None:
    """Calcula time-to-first-token; en no streaming puede coincidir con el total."""
    first = kwargs.get("completion_start_time") or end
    return _duration_ms(start, first)


def _details(kwargs: dict[str, Any], start: Any, end: Any) -> dict[str, Any]:
    """Agrupa metadatos comunes para que éxito y error compartan esquema."""

    guardrail = _metadata(kwargs).get("dashboard_guardrail") or {}
    return {
        "started_at": _started_at(start),
        "origin_ip": _origin_ip(kwargs),
        "provider_ip": _provider_ip(kwargs),
        "ttft_ms": _ttft_ms(kwargs, start, end),
        "guardrail_status": guardrail.get("status"),
        "guardrail_reason": guardrail.get("reason"),
        "parameters": _metadata(kwargs).get("dashboard_effective_parameters"),
    }


def _guardrail_settings() -> dict[str, Any]:
    """Lee la copia runtime; un fichero inválido desactiva el filtro con seguridad."""

    try:
        return json.loads((_root() / "runtime" / "dashboard_settings.json").read_text(encoding="utf-8")).get("guardrail", {})
    except (OSError, ValueError, TypeError):
        return {}


def _runtime_settings() -> dict[str, Any]:
    """Lee las reglas dinámicas; un fallo nunca debe impedir la inferencia."""

    try:
        value = json.loads((_root() / "runtime" / "dashboard_settings.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


_NON_LOGGED_PARAMETERS = {
    "api_base", "api_key", "authorization", "headers", "input", "messages",
    "metadata", "model", "prompt", "request_timeout",
}


def _merge_parameters(target: dict[str, Any], configured: dict[str, Any], overwrite: bool) -> None:
    """Fusiona parámetros incluyendo diccionarios como ``extra_body``."""

    for key, value in configured.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_parameters(target[key], value, overwrite)
        elif overwrite or key not in target:
            target[key] = value


def _effective_parameters(data: dict[str, Any]) -> dict[str, Any]:
    """Devuelve sólo opciones de inferencia, nunca contenido ni credenciales."""

    return {
        str(key): value for key, value in data.items()
        if str(key).lower() not in _NON_LOGGED_PARAMETERS and not str(key).startswith("litellm_")
    }


def _provider_model(alias: str) -> str | None:
    """Resuelve el alias público al identificador estricto de Cloudera."""
    try:
        settings = json.loads((_root() / "runtime" / "dashboard_settings.json").read_text(encoding="utf-8"))
        value = (settings.get("provider_models") or {}).get(alias)
        return str(value) if value else None
    except (OSError, ValueError, TypeError):
        return None


def _provider_api_key(alias: str) -> str | None:
    """Obtiene desde SQLite el token más reciente asociado al alias Cloudera."""

    try:
        settings = json.loads(
            (_root() / "runtime" / "dashboard_settings.json").read_text(encoding="utf-8")
        )
        variable_name = (settings.get("provider_api_key_env") or {}).get(alias)
        if not variable_name:
            return None
        from gateway.cloudera import ClouderaCatalog

        return ClouderaCatalog(_root() / "runtime").environment().get(str(variable_name))
    except (OSError, ValueError, TypeError):
        return None


def _classify_with_guardrail(settings: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, str]:
    """Clasifica mediante un guardrail Ollama u OpenAI-compatible.

    El adaptador llama directamente al endpoint para evitar recursión a través
    del propio proxy. Las credenciales Cloudera se leen de SQLite en cada
    petición, igual que para el modelo principal.
    """

    protocol = str(settings.get("protocol") or "ollama")
    api_base = str(settings["api_base"]).rstrip("/")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    provider_model = str(settings["provider_model"])
    if protocol == "openai":
        endpoint = api_base if api_base.endswith("/chat/completions") else f"{api_base}/chat/completions"
        token = _provider_api_key(str(settings.get("model") or ""))
        if not token and settings.get("api_key_env"):
            token = os.environ.get(str(settings["api_key_env"]))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        provider_model = provider_model.removeprefix("openai/")
    else:
        endpoint = f"{api_base}/api/chat"
        provider_model = provider_model.removeprefix("ollama/")
    body = json.dumps({"model": provider_model, "messages": messages, "stream": False}).encode()
    request = urllib.request.Request(endpoint, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=float(settings.get("timeout", 8))) as response:
            payload = json.load(response)
        if protocol == "openai":
            choices = payload.get("choices") or []
            content = str((choices[0].get("message") or {}).get("content", "") if choices else "").strip()
        else:
            content = str(payload.get("message", {}).get("content", "")).strip()
        first = content.lower().splitlines()[0] if content else ""
        return {"status": "warning" if first.startswith("unsafe") else "safe",
                "reason": content or "Sin explicación del guardrail"}
    except Exception as exc:  # La seguridad en modo aviso jamás debe bloquear el modelo principal.
        return {"status": "unavailable", "reason": f"Guardrail no disponible: {exc}"}


class DashboardLogger(CustomLogger):
    """Adaptador LiteLLM → SQLite para éxitos y errores con el mismo esquema."""

    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type):
        """Clasifica la entrada y adapta aliases Cloudera antes del proveedor.

        En modo permisivo el veredicto se adjunta a metadatos y la llamada
        continúa. En modo restringido un resultado inseguro o no disponible
        levanta una excepción antes de consumir el modelo principal.
        """

        runtime = _runtime_settings()
        settings = runtime.get("guardrail") or {}
        target = str(data.get("model") or "")
        parameter_settings = (runtime.get("model_parameters") or {}).get(target) or {}
        configured = parameter_settings.get("configured") or {}
        _merge_parameters(data, configured, parameter_settings.get("policy") == "model_wins")
        metadata = dict(data.get("metadata") or {})
        metadata["dashboard_effective_parameters"] = _effective_parameters(data)
        metadata["dashboard_parameter_policy"] = parameter_settings.get("policy", "caller_wins")
        data["metadata"] = metadata
        provider_model = _provider_model(target)
        messages = data.get("messages")
        if provider_model:
            metadata["dashboard_model_alias"] = target
            data["metadata"] = metadata
            api_key = _provider_api_key(target)
            if api_key:
                data["api_key"] = api_key
        excluded = {str(name) for name in (settings.get("excluded_models") or [])}
        embedding_call = "embedding" in str(call_type).lower()
        if (not settings.get("enabled") or not settings.get("provider_model") or
                not isinstance(messages, list) or target == settings.get("model") or
                target in excluded or embedding_call):
            if provider_model:
                data["model"] = provider_model
            return data
        guardrail_started = datetime.now().astimezone()
        verdict = await asyncio.to_thread(_classify_with_guardrail, settings, messages)
        guardrail_ended = datetime.now().astimezone()
        insert_log(_log_path(guardrail_started), str(settings.get("model")),
                   "error" if verdict["status"] == "unavailable" else "success",
                   _duration_ms(guardrail_started, guardrail_ended), {"messages": messages}, verdict,
                   verdict["reason"] if verdict["status"] == "unavailable" else None,
                   started_at=_started_at(guardrail_started),
                   origin_ip=_origin_ip({"litellm_params": {"metadata": data.get("metadata") or {}}}),
                   provider_ip=_provider_ip({"api_base": settings.get("api_base")}),
                   ttft_ms=_duration_ms(guardrail_started, guardrail_ended),
                   guardrail_status=verdict["status"], guardrail_reason=verdict["reason"],
                   parameters={"stream": False})
        metadata = dict(data.get("metadata") or {})
        metadata["dashboard_guardrail"] = verdict
        data["metadata"] = metadata
        if settings.get("policy") == "block" and verdict["status"] in {"warning", "unavailable"}:
            raise ValueError(f"Petición bloqueada por el guardrail restringido: {verdict['reason']}")
        if provider_model:
            data["model"] = provider_model
        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """Normaliza y persiste una respuesta correcta sin bloquear al cliente."""

        insert_log(_log_path(start_time), _model(kwargs), "success",
                   _duration_ms(start_time, end_time), {"messages": kwargs.get("messages"), "input": kwargs.get("input")},
                   response_obj, **_details(kwargs, start_time, end_time))

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        """Conserva el mismo esquema en fallos para facilitar KPIs comparables."""

        error = str(kwargs.get("exception") or response_obj or "Error desconocido")
        insert_log(_log_path(start_time), _model(kwargs), "error",
                   _duration_ms(start_time, end_time), {"messages": kwargs.get("messages"), "input": kwargs.get("input")},
                   response_obj, error, **_details(kwargs, start_time, end_time))


# LiteLLM importa esta instancia por el nombre configurado en active_config.yaml.
dashboard_logger = DashboardLogger()
