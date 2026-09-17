"""Early TLS initialization scoped exclusively to the LiteLLM child process.

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
    """Restrict laboratory mode to explicitly configured hosts."""

    return (urlparse(str(url)).hostname or "").lower() in _insecure_hosts


def _gateway_create_default_context(*args: Any, **kwargs: Any) -> ssl.SSLContext:
    """Retain public roots and add internal Cloudera certificate authorities."""

    context = _original_create_default_context(*args, **kwargs)
    loaded = False
    for bundle in _bundles:
        try:
            context.load_verify_locations(cafile=str(bundle))
            loaded = True
        except (OSError, ssl.SSLError):
            continue
    if loaded:
        # Some older corporate PKIs omit the Authority Key Identifier. Keep
        # CERT_REQUIRED and hostname checks; only bypass the strict X.509 mode
        # that Python 3.13 enables in selected clients.
        strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict:
            context.verify_flags &= ~strict
    return context


if _bundles:
    ssl.create_default_context = _gateway_create_default_context


if _insecure_hosts:
    # LiteLLM 1.83 uses aiohttp as its default OpenAI transport and loses the
    # deployment's ssl_verify value when reusing the session. Apply the choice
    # at the final point before connecting, where the destination host is known.
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
