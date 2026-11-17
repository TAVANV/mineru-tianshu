"""外部 MCP 地址必须显式放行，不能靠关闭 DNS 重绑定防护解决部署问题。"""

import asyncio

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware
from starlette.requests import Request

from mcp_server import _transport_security_settings


def validate(settings, host, origin=None):
    headers = [(b"host", host.encode())]
    if origin:
        headers.append((b"origin", origin.encode()))
    request = Request({"type": "http", "headers": headers, "method": "GET", "path": "/sse"})
    return asyncio.run(TransportSecurityMiddleware(settings).validate_request(request))


@pytest.mark.parametrize(
    ("host", "origin", "expected"),
    [
        ("192.168.100.27:8002", "http://192.168.100.27:8080", None),
        ("localhost:8002", None, None),
        ("evil.example:8002", None, 421),
        ("192.168.100.27:8002", "https://evil.example", 403),
    ],
)
def test_explicit_deployment_allowlist_keeps_host_and_origin_checks(monkeypatch, host, origin, expected):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", " 192.168.100.27:8002, localhost:*,127.0.0.1:* , ")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "http://192.168.100.27:8080, http://localhost:8080")
    settings = _transport_security_settings("0.0.0.0")
    assert settings.enable_dns_rebinding_protection
    result = validate(settings, host, origin)
    assert (result.status_code if result else None) == expected


@pytest.mark.parametrize("setting", [None, " , "])
def test_unconfigured_allowlist_preserves_loopback_defaults(monkeypatch, setting):
    for key in ("MCP_ALLOWED_HOSTS", "MCP_ALLOWED_ORIGINS"):
        if setting is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, setting)
    settings = _transport_security_settings("0.0.0.0")
    assert validate(settings, "localhost:8002", "http://localhost:8080") is None
    assert validate(settings, "192.168.100.27:8002").status_code == 421
