"""Inicialización TLS temprana y exclusiva del proceso hijo LiteLLM.

Python importa automáticamente un módulo llamado ``sitecustomize`` al arrancar.
GatewayManager copia este fichero con ese nombre al directorio runtime y lo
antepone al PYTHONPATH de LiteLLM. Así las CA privadas están disponibles antes
de que LiteLLM/OpenAI construya y guarde en caché sus clientes HTTP.
"""

from __future__ import annotations

import os
from pathlib import Path
import ssl
from typing import Any
from urllib.parse import urlparse


_original_create_default_context = ssl.create_default_context


def _private_ca_bundles() -> list[Path]:
    root = Path(os.environ.get("IA_GATEWAY_ROOT", Path.cwd()))
    return sorted((root / "runtime" / "cloudera-ca").glob("*.pem"))


_bundles = _private_ca_bundles()
_insecure_hosts = {
    host.strip().lower()
    for host in os.environ.get("IA_GATEWAY_INSECURE_TLS_HOSTS", "").split(",")
    if host.strip()
}


def _is_insecure_host(url: Any) -> bool:
    """Limita el modo de laboratorio a los hosts configurados explícitamente."""

    return (urlparse(str(url)).hostname or "").lower() in _insecure_hosts


def _gateway_create_default_context(*args: Any, **kwargs: Any) -> ssl.SSLContext:
    """Conserva las raíces públicas y añade las CA internas de Cloudera."""

    context = _original_create_default_context(*args, **kwargs)
    loaded = False
    for bundle in _bundles:
        try:
            context.load_verify_locations(cafile=str(bundle))
            loaded = True
        except (OSError, ssl.SSLError):
            continue
    if loaded:
        # Algunas PKI corporativas antiguas no incluyen Authority Key Identifier.
        # Se conserva CERT_REQUIRED y check_hostname; sólo se evita el modo X.509
        # estricto que Python 3.13 activa en determinados clientes.
        strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict:
            context.verify_flags &= ~strict
    return context


if _bundles:
    ssl.create_default_context = _gateway_create_default_context


if _insecure_hosts:
    # LiteLLM 1.83 usa aiohttp como transporte OpenAI por defecto y pierde el
    # ssl_verify del deployment al reutilizar la sesión. La decisión se aplica
    # en el último punto previo a abrir la conexión, donde aún conocemos el host.
    try:
        import aiohttp

        _original_aiohttp_request = aiohttp.ClientSession._request

        async def _gateway_aiohttp_request(self: Any, method: str, str_or_url: Any,
                                           *args: Any, **kwargs: Any) -> Any:
            if _is_insecure_host(str_or_url):
                kwargs["ssl"] = False
            return await _original_aiohttp_request(self, method, str_or_url, *args, **kwargs)

        aiohttp.ClientSession._request = _gateway_aiohttp_request
    except ImportError:
        pass
