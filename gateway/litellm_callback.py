from __future__ import annotations

import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from litellm.integrations.custom_logger import CustomLogger

from gateway.log_store import insert_log


def _root() -> Path:
    return Path(os.environ.get("IA_GATEWAY_ROOT", Path.cwd()))


def _model(kwargs: dict[str, Any]) -> str:
    metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    return str(metadata.get("model_group") or metadata.get("user_api_key_model") or kwargs.get("model") or "desconocido")


def _duration_ms(start: Any, end: Any) -> int | None:
    if isinstance(start, datetime) and isinstance(end, datetime):
        return round((end - start).total_seconds() * 1000)
    return None


def _metadata(kwargs: dict[str, Any]) -> dict[str, Any]:
    params_metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    standard = kwargs.get("standard_logging_object") or {}
    if hasattr(standard, "model_dump"):
        standard = standard.model_dump()
    standard_metadata = standard.get("metadata", {}) if isinstance(standard, dict) else {}
    return {**params_metadata, **standard_metadata}


def _origin_ip(kwargs: dict[str, Any]) -> str | None:
    metadata = _metadata(kwargs)
    value = metadata.get("requester_ip_address") or metadata.get("client_ip")
    if value:
        return str(value).split(",", 1)[0].strip()
    headers = metadata.get("headers") or {}
    value = headers.get("x-forwarded-for") or headers.get("x-real-ip")
    return str(value).split(",", 1)[0].strip() if value else None


def _provider_ip(kwargs: dict[str, Any]) -> str | None:
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
    return start.isoformat() if isinstance(start, datetime) else None


def _ttft_ms(kwargs: dict[str, Any], start: Any, end: Any) -> int | None:
    first = kwargs.get("completion_start_time") or end
    return _duration_ms(start, first)


def _details(kwargs: dict[str, Any], start: Any, end: Any) -> dict[str, Any]:
    return {
        "started_at": _started_at(start),
        "origin_ip": _origin_ip(kwargs),
        "provider_ip": _provider_ip(kwargs),
        "ttft_ms": _ttft_ms(kwargs, start, end),
    }


class DashboardLogger(CustomLogger):
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        insert_log(_root() / "runtime" / "requests.sqlite3", _model(kwargs), "success",
                   _duration_ms(start_time, end_time), {"messages": kwargs.get("messages"), "input": kwargs.get("input")},
                   response_obj, **_details(kwargs, start_time, end_time))

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        error = str(kwargs.get("exception") or response_obj or "Error desconocido")
        insert_log(_root() / "runtime" / "requests.sqlite3", _model(kwargs), "error",
                   _duration_ms(start_time, end_time), {"messages": kwargs.get("messages"), "input": kwargs.get("input")},
                   response_obj, error, **_details(kwargs, start_time, end_time))


dashboard_logger = DashboardLogger()
