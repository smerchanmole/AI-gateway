"""Proxy HTTP streaming que publica panel y LiteLLM en un único puerto."""

from __future__ import annotations

import argparse
import ipaddress
import ssl

from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, TCPConnector, web
from multidict import CIMultiDict


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _client_ip(request: web.Request) -> str:
    """Conserva la IP anunciada por el ingress sin quedarse con el loopback.

    ``X-Envoy-External-Address`` es preferente en Cloudera/Istio. Como respaldo
    recorremos ``X-Forwarded-For`` de izquierda a derecha y descartamos sólo
    saltos loopback; las redes privadas siguen siendo válidas en instalaciones
    on-premise.
    """

    candidates: list[str] = []
    envoy = request.headers.get("X-Envoy-External-Address", "").strip()
    if envoy:
        candidates.append(envoy)
    candidates.extend(
        part.strip()
        for part in request.headers.get("X-Forwarded-For", "").split(",")
        if part.strip()
    )
    real = request.headers.get("X-Real-IP", "").strip()
    if real:
        candidates.append(real)
    if request.remote:
        candidates.append(request.remote)

    valid: list[tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address]] = []
    for candidate in candidates:
        value = candidate.strip().strip("[]")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        valid.append((value, address))
        if not address.is_loopback:
            return value
    return valid[0][0] if valid else ""


def upstream_port(path: str, dashboard_port: int, litellm_port: int) -> int:
    """Envía únicamente /v1 y sus descendientes directamente a LiteLLM."""

    return litellm_port if path == "/v1" or path.startswith("/v1/") else dashboard_port


def forwarded_headers(
    request: web.Request, *, forward_authorization: bool = True
) -> CIMultiDict[str]:
    blocked = HOP_BY_HOP_HEADERS | {"host", "content-length"}
    if not forward_authorization:
        blocked.add("authorization")
    headers = CIMultiDict(
        (name, value)
        for name, value in request.headers.items()
        if name.lower() not in blocked
    )
    peer = request.remote or ""
    previous = request.headers.get("X-Forwarded-For", "").strip()
    headers["X-Forwarded-For"] = ", ".join(item for item in (previous, peer) if item)
    # Siempre sobrescribimos nuestra cabecera interna: el cliente no puede
    # escoger el valor que consumirá el logger situado detrás de este proxy.
    client_ip = _client_ip(request)
    if client_ip:
        headers["X-IA-Gateway-Client-IP"] = client_ip
    headers["X-Forwarded-Proto"] = request.headers.get("X-Forwarded-Proto", request.scheme)
    headers["Host"] = request.host
    return headers


async def proxy(request: web.Request) -> web.StreamResponse:
    session: ClientSession = request.app["client"]
    port = upstream_port(
        request.path,
        request.app["dashboard_port"],
        request.app["litellm_port"],
    )
    target = f"http://127.0.0.1:{port}{request.rel_url}"
    body = request.content.iter_chunked(64 * 1024) if request.can_read_body else None
    headers = forwarded_headers(
        request,
        forward_authorization=port != request.app["litellm_port"],
    )
    try:
        upstream = await session.request(
            request.method,
            target,
            headers=headers,
            data=body,
            allow_redirects=False,
        )
    except ClientConnectionError as exc:
        raise web.HTTPBadGateway(text="El servicio interno no está disponible") from exc
    response_headers = CIMultiDict(
        (name, value)
        for name, value in upstream.headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS | {"content-length"}
    )
    response = web.StreamResponse(status=upstream.status, headers=response_headers)
    await response.prepare(request)
    try:
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await response.write(chunk)
        await response.write_eof()
    finally:
        upstream.release()
    return response


async def client_context(app: web.Application):
    timeout = ClientTimeout(total=None, sock_read=None)
    async with ClientSession(
        timeout=timeout,
        connector=TCPConnector(limit=0),
        auto_decompress=False,
    ) as session:
        app["client"] = session
        yield


def build_app(dashboard_port: int, litellm_port: int) -> web.Application:
    app = web.Application(client_max_size=100 * 1024**2)
    app["dashboard_port"] = dashboard_port
    app["litellm_port"] = litellm_port
    app.cleanup_ctx.append(client_context)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--dashboard-port", required=True, type=int)
    parser.add_argument("--litellm-port", required=True, type=int)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    args = parser.parse_args()
    ssl_context = None
    if args.cert and args.key:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(args.cert, args.key)
    web.run_app(
        build_app(args.dashboard_port, args.litellm_port),
        host=args.host,
        port=args.port,
        ssl_context=ssl_context,
        print=None,
    )


if __name__ == "__main__":
    main()
