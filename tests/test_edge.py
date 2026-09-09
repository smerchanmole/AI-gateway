from gateway.edge import EdgeProxy, public_gateway_port
from gateway.edge_server import forwarded_headers, upstream_port
from multidict import CIMultiDict
from types import SimpleNamespace


def test_public_port_prefers_explicit_then_any_cloudera_port(monkeypatch):
    for name in ("IA_GATEWAY_PORT", "CDSW_READONLY_PORT", "CDSW_APP_PORT", "CDSW_PUBLIC_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert public_gateway_port() == 8090
    monkeypatch.setenv("CDSW_APP_PORT", "31000")
    assert public_gateway_port() == 31000
    monkeypatch.setenv("CDSW_READONLY_PORT", "31000")
    monkeypatch.setenv("CDSW_PUBLIC_PORT", "31000")
    assert public_gateway_port() == 31000
    monkeypatch.setenv("IA_GATEWAY_PORT", "32000")
    assert public_gateway_port() == 32000


def test_python_proxy_routes_only_v1_to_litellm():
    assert upstream_port("/v1", 18080, 14000) == 14000
    assert upstream_port("/v1/chat/completions", 18080, 14000) == 14000
    assert upstream_port("/", 18080, 14000) == 18080
    assert upstream_port("/api/status", 18080, 14000) == 18080
    assert upstream_port("/v10/example", 18080, 14000) == 18080


def test_python_proxy_command_uses_same_public_port(tmp_path):
    edge = EdgeProxy(tmp_path, 8090, 18080, 14000)
    command = edge._command()
    assert "gateway.edge_server" in command
    assert command[command.index("--port") + 1] == "8090"


def test_proxy_removes_sdk_authorization_when_litellm_auth_is_disabled():
    request = SimpleNamespace(
        headers=CIMultiDict({"Authorization": "Bearer not-required", "Accept": "application/json"}),
        remote="127.0.0.1",
        scheme="https",
        host="gateway.example",
    )

    headers = forwarded_headers(request, forward_authorization=False)

    assert "Authorization" not in headers
    assert headers["Accept"] == "application/json"
