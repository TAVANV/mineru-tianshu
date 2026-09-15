"""Upstream security/runtime ports, exercised at HTTP and persistence boundaries."""

import ast
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def secured(api):
    from auth.auth_db import AuthDB
    from auth.dependencies import get_auth_db
    from auth.models import UserCreate

    auth = AuthDB(api.db.db_path)
    users = [
        auth.create_user(UserCreate(username=n, email=f"{n}@example.com", password="Password123!"))
        for n in ["alice", "bob"]
    ]
    api.app.dependency_overrides[get_auth_db] = lambda: auth
    with TestClient(api.app) as client:
        tokens = [
            client.post("/api/v1/auth/login", json={"username": u.username, "password": "Password123!"}).json()[
                "access_token"
            ]
            for u in users
        ]
        yield api, auth, client, users, tokens
    api.app.dependency_overrides.clear()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_logout_password_and_legacy_jwt(secured):
    api, auth, client, users, tokens = secured
    assert client.post("/api/v1/auth/logout", headers=bearer(tokens[0])).status_code == 200
    assert client.get("/api/v1/auth/me", headers=bearer(tokens[0])).status_code == 401
    assert client.get("/api/v1/auth/me", headers=bearer(tokens[1])).status_code == 200
    token = client.post("/api/v1/auth/login", json={"username": "alice", "password": "Password123!"}).json()[
        "access_token"
    ]
    assert (
        client.post(
            "/api/v1/auth/me/change-password",
            headers=bearer(token),
            json={"old_password": "Password123!", "new_password": "Password456!"},
        ).status_code
        == 200
    )
    assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401
    fresh = client.post("/api/v1/auth/login", json={"username": "alice", "password": "Password456!"}).json()[
        "access_token"
    ]
    assert client.get("/api/v1/auth/me", headers=bearer(fresh)).status_code == 200
    import jwt
    from auth.jwt_handler import JWT_SECRET_KEY

    legacy = jwt.encode(
        {"sub": users[1].user_id, "username": "bob", "role": "user", "exp": datetime.utcnow() + timedelta(hours=1)},
        JWT_SECRET_KEY,
        algorithm="HS256",
    )
    assert client.post("/api/v1/auth/logout", headers=bearer(legacy)).status_code == 200
    assert client.get("/api/v1/auth/me", headers=bearer(legacy)).status_code == 401
    # Revocations and epochs survive a new AuthDB instance.
    from auth.auth_db import AuthDB
    from auth.dependencies import _authenticate_jwt

    assert _authenticate_jwt(tokens[0], AuthDB(auth.db_path)) is None


def test_scoped_keys_expiry_and_no_privilege_amplification(secured):
    api, auth, client, users, tokens = secured
    endpoint = "/api/v1/auth/apikeys"
    key = client.post(
        endpoint, headers=bearer(tokens[0]), json={"name": "reader", "scopes": ["task:view:own", "apikey:create"]}
    ).json()
    assert 89 < (datetime.fromisoformat(key["expires_at"]) - datetime.utcnow()).total_seconds() / 86400 <= 90
    headers = {"X-API-Key": key["api_key"]}
    tid = api.db.create_task("a.pdf", "/a.pdf", user_id=users[0].user_id)
    assert client.get(f"/api/v1/tasks/{tid}", headers=headers).status_code == 200
    assert client.post(f"/api/v1/tasks/{tid}/cancel", headers=headers).status_code == 403
    assert client.post(endpoint, headers=headers, json={"name": "escape"}).status_code == 403
    assert (
        client.post(endpoint, headers=headers, json={"name": "child", "scopes": ["task:view:own"]}).status_code == 201
    )
    assert client.patch("/api/v1/auth/me", headers=headers, json={"full_name": "escape"}).status_code == 403
    assert (
        client.post(endpoint, headers=bearer(tokens[0]), json={"name": "invalid", "scopes": ["invalid"]}).status_code
        == 422
    )
    assert (
        client.post(endpoint, headers=bearer(tokens[0]), json={"name": "forever", "expires_days": None}).status_code
        == 422
    )
    with auth.get_cursor() as c:
        c.execute("UPDATE api_keys SET scopes=? WHERE key_id=?", ("{broken", key["key_id"]))
    assert client.get(f"/api/v1/tasks/{tid}", headers=headers).status_code == 403
    # ISO T timestamps must not remain valid for the rest of their expiration day.
    with auth.get_cursor() as c:
        c.execute(
            "UPDATE api_keys SET expires_at=? WHERE key_id=?",
            ((datetime.utcnow() - timedelta(seconds=1)).isoformat(), key["key_id"]),
        )
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_files_ownership_keys_preview_and_nested_results(secured):
    api, auth, client, users, tokens = secured
    result = api.OUTPUT_DIR / "root"
    result.mkdir()
    source = api.UPLOAD_DIR / "a.png"
    source.write_bytes(b"source")
    (result / "result.html").write_text("<script>alert(1)</script>")
    (result / "a.png").write_bytes(b"image")
    tid = api.db.create_task("a.png", str(source), user_id=users[0].user_id)
    with api.db.get_cursor() as c:
        c.execute("UPDATE tasks SET result_path=? WHERE task_id=?", (str(result), tid))
    key = auth.create_api_key(users[0].user_id, "images", scopes=["task:view:own"])["api_key"]
    for url in ["/api/v1/files/upload/a.png", "/api/v1/files/output/root/a.png"]:
        assert client.get(url).status_code == 401
        assert client.get(url, headers=bearer(tokens[1])).status_code == 403
        assert client.get(url, headers={"X-API-Key": key}).status_code == 200
        preview = client.get(url, params={"token": tokens[0]})
        assert preview.status_code == 200 and preview.headers["cache-control"] == "private, no-store"
    unsafe = client.get("/api/v1/files/output/root/result.html", headers=bearer(tokens[0]))
    assert unsafe.headers["content-disposition"].startswith("attachment")
    assert unsafe.headers["x-content-type-options"] == "nosniff"
    nested = result / "pdf" / "chunk"
    nested.mkdir(parents=True)
    (nested / "a.png").write_bytes(b"chunk")
    child = api.db.create_task("chunk.pdf", "/chunk.pdf", user_id=users[1].user_id)
    with api.db.get_cursor() as c:
        c.execute("UPDATE tasks SET result_path=? WHERE task_id=?", (str(nested), child))
    assert client.get("/api/v1/files/output/root/pdf/chunk/a.png", headers=bearer(tokens[0])).status_code == 403
    assert client.get("/api/v1/files/output/root/pdf/chunk/a.png", headers=bearer(tokens[1])).status_code == 200
    (result / "escape.png").symlink_to(source)
    assert client.get("/api/v1/files/output/root/escape.png", headers=bearer(tokens[0])).status_code == 404
    client.post("/api/v1/auth/logout", headers=bearer(tokens[0]))
    assert client.get("/api/v1/files/output/root/a.png", params={"token": tokens[0]}).status_code == 401


def test_upload_validation_limit_and_cleanup(secured, monkeypatch):
    api, auth, client, users, tokens = secured
    h = bearer(tokens[0])
    assert client.post("/api/v1/tasks/submit", headers=h, files={"file": ("run.exe", b"bad")}).status_code == 400
    monkeypatch.setenv("MAX_FILE_SIZE", "4")
    assert client.post("/api/v1/tasks/submit", headers=h, files={"file": ("a.pdf", b"12345")}).status_code == 413
    assert not list(api.UPLOAD_DIR.iterdir())
    ok = client.post("/api/v1/tasks/submit", headers=h, files={"file": ("../../a.md", b"text")})
    assert ok.status_code == 200
    assert Path(api.db.get_task(ok.json()["task_id"])["file_path"]).resolve().parent == api.UPLOAD_DIR.resolve()


@pytest.mark.parametrize("path", ["/sse", "/sse/", "/messages", "/messages/", "/messages/child"])
def test_mcp_requires_own_key(path, monkeypatch):
    import mcp_server
    from starlette.responses import PlainTextResponse

    async def endpoint(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await PlainTextResponse("ok")(scope, receive, send)

    monkeypatch.setattr(mcp_server, "MCP_API_KEYS", ["secret"])
    with TestClient(mcp_server.MCPAuthMiddleware(endpoint)) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get(path, headers={"X-API-Key": "secret"}).status_code == 200
        assert client.get(path, headers={"Authorization": "Bearer secret"}).status_code == 200
        monkeypatch.setattr(mcp_server, "MCP_API_KEYS", [])
        assert client.get(path, headers={"X-API-Key": "secret"}).status_code == 401
    monkeypatch.setattr(mcp_server, "TIANSHU_API_KEY", "backend-key")
    assert mcp_server._api_headers() == {"X-API-Key": "backend-key"}


def test_mcp_dns_rebinding_blocked(monkeypatch):
    import mcp_server

    async def check():
        async def rebound(*a, **kw):
            return [(2, 1, 6, "", ("127.0.0.1", 80))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", rebound)
        with pytest.raises(ValueError, match="public"):
            await mcp_server.PublicResolver().resolve("public.example", 80)

    asyncio.run(check())


def controller_class():
    from loguru import logger

    tree = ast.parse((Path(__file__).parents[1] / "backend/litserve_worker.py").read_text())
    klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VLLMController")
    ns = {"logger": logger}
    exec(compile(ast.Module(body=[klass], type_ignores=[]), "<controller>", "exec"), ns)
    return ns["VLLMController"]


@pytest.mark.parametrize("missing", [True, False])
def test_vllm_target_resolved_before_stopping_conflict(monkeypatch, missing):
    class DockerException(Exception):
        pass

    class NotFound(DockerException):
        pass

    events = []

    def get(name):
        events.append(name)
        if name == "target" and missing:
            raise NotFound()
        return SimpleNamespace(
            status="running" if name == "conflict" else "exited",
            stop=lambda: events.append("stop"),
            start=lambda: events.append("start"),
        )

    client = SimpleNamespace(containers=SimpleNamespace(get=get), close=lambda: events.append("close"))
    monkeypatch.setitem(
        sys.modules,
        "docker",
        SimpleNamespace(
            from_env=lambda: client, errors=SimpleNamespace(DockerException=DockerException, NotFound=NotFound)
        ),
    )
    controller_class()().ensure_service("target", "conflict")
    assert events == (["target", "close"] if missing else ["target", "conflict", "stop", "start", "close"])


@pytest.mark.parametrize("backend", ["vlm-auto-engine", "hybrid-auto-engine", "vlm-http-client", "hybrid-http-client"])
@pytest.mark.parametrize("external", [False, True])
def test_vllm_worker_cold_start_and_external_bypass(db, tmp_path, backend, external):
    from test_review_regressions import make_worker

    obj = make_worker(db, tmp_path)
    obj.mineru_vllm_api = "http://local:30000/v1"
    calls = []
    obj.vllm_controller = SimpleNamespace(ensure_service=lambda **kw: calls.append(kw))
    obj._process_with_mineru = lambda *a: {"result_path": str(tmp_path)}
    tid = db.create_task(
        "a.png",
        str(tmp_path / "a.png"),
        backend=backend,
        options={"server_url": "https://external/v1"} if external else {},
    )
    obj._process_task(db.get_next_task(obj.worker_id))
    assert len(calls) == (0 if external else 1)
    assert db.get_task(tid)["status"] == "completed"


def test_mcp_real_routing_and_host_validation(monkeypatch):
    import mcp_server

    monkeypatch.setattr(mcp_server, "MCP_API_KEYS", ["secret"])
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "testserver")
    with TestClient(mcp_server.create_app()) as client:
        assert client.get("/health").json() == {"status": "healthy"}
        assert client.post("/messages").status_code == 401
        # SDK generates exactly one HTTP response, without a Starlette None response error.
        assert (
            client.post(
                "/messages", headers={"X-API-Key": "secret", "Content-Type": "application/json"}, json={}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/messages",
                headers={"X-API-Key": "secret", "Host": "evil.example", "Content-Type": "application/json"},
                json={},
            ).status_code
            == 421
        )


def test_password_epoch_snapshot_blocks_racing_login(secured, monkeypatch):
    api, auth, client, users, tokens = secured
    original = auth.authenticate_user

    def authenticate_then_change(username, password):
        user = original(username, password)
        auth.change_password(user.user_id, password, "Changed123!")
        return user

    monkeypatch.setattr(auth, "authenticate_user", authenticate_then_change)
    token = client.post("/api/v1/auth/login", json={"username": "alice", "password": "Password123!"}).json()[
        "access_token"
    ]
    assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401
