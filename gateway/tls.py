"""Generación y protección del certificado TLS local del panel."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import shutil
import socket
import subprocess


def _local_addresses() -> list[str]:
    """Recoge nombres/IP locales para reducir avisos por nombre no coincidente."""
    values = {"localhost", "127.0.0.1"}
    hostname = socket.gethostname()
    if hostname:
        values.add(hostname)
    try:
        for item in socket.getaddrinfo(hostname, None):
            address = item[4][0].split("%", 1)[0]
            ipaddress.ip_address(address)
            values.add(address)
    except (OSError, ValueError):
        pass
    return sorted(values)


def ensure_self_signed_certificate(runtime_dir: Path) -> tuple[Path, Path]:
    """Crea una clave RSA y certificado autofirmado si aún no existen.

    La clave privada queda con permisos ``0600`` dentro de ``runtime/`` y, al
    estar esa carpeta ignorada por Git, nunca se publica accidentalmente.
    """
    tls_dir = runtime_dir / "tls"
    certificate = tls_dir / "ia-gateway.crt"
    private_key = tls_dir / "ia-gateway.key"
    if certificate.exists() and private_key.exists():
        os.chmod(private_key, 0o600)
        return certificate, private_key
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("No se encuentra OpenSSL; es necesario para arrancar el panel por HTTPS")
    tls_dir.mkdir(parents=True, exist_ok=True)
    san = []
    for value in _local_addresses():
        try:
            ipaddress.ip_address(value)
            san.append(f"IP:{value}")
        except ValueError:
            san.append(f"DNS:{value}")
    subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes",
        "-days", "825", "-keyout", str(private_key), "-out", str(certificate),
        "-subj", "/CN=IA Gateway local",
        "-addext", f"subjectAltName={','.join(san)}",
        "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
        "-addext", "extendedKeyUsage=serverAuth",
    ], check=True, capture_output=True, text=True)
    os.chmod(private_key, 0o600)
    os.chmod(certificate, 0o644)
    return certificate, private_key

