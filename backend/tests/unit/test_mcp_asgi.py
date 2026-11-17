"""传输层已写入 ASGI 响应后，路由不得再次调用空的 Response。"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from starlette.testclient import TestClient

import mcp_server


def test_native_message_handler_returns_its_own_bad_request_response(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "testserver")
    monkeypatch.setattr(mcp_server, "MCP_API_KEYS", ["unit-test-key"])
    with TestClient(mcp_server.create_mcp_app("0.0.0.0")) as client:
        assert client.get("/health").json() == {"status": "healthy"}
        assert client.post("/messages", json={}).status_code == 401
        response = client.post("/messages", json={}, headers={"X-API-Key": "unit-test-key"})
    assert response.status_code == 400


def test_stream_and_message_routes_are_raw_asgi_apps(monkeypatch):
    class Transport:
        def __init__(self, path, security_settings):
            assert path == "/messages"
            assert security_settings.enable_dns_rebinding_protection

        @asynccontextmanager
        async def connect_sse(self, scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"event: endpoint\n\n", "more_body": True})
            yield (None, None)
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        async def handle_post_message(self, scope, receive, send):
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    monkeypatch.setattr(mcp_server, "MCP_API_KEYS", ["unit-test-key"])
    monkeypatch.setattr(mcp_server, "SseServerTransport", Transport)
    run = AsyncMock()
    monkeypatch.setattr(mcp_server.app, "run", run)
    with TestClient(mcp_server.create_mcp_app("0.0.0.0")) as client:
        stream = client.get("/sse", headers={"X-API-Key": "unit-test-key"})
        message = client.post("/messages", json={}, headers={"X-API-Key": "unit-test-key"})
    assert stream.status_code == 200 and b"event: endpoint" in stream.content
    assert message.status_code == 202
    run.assert_awaited_once()
