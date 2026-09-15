"""Additive upstream integration: callback isolation, indexed files, offline upgrade."""

import ast
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from test_phase3 import secured as _secured, bearer

ROOT = Path(__file__).parents[1]


@pytest.fixture
def secured(api):
    yield from _secured.__wrapped__(api)


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_key_callbacks_snapshot_precedence_and_secret_isolation(secured, monkeypatch):
    api, auth, client, users, tokens = secured
    from feature_config import update_config
    from webhooks import subscription

    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    update_config(
        {"webhook_enabled": True, "webhook_url": "https://global.example/hook", "webhook_secret": "global-secret"},
        auth.db_path,
    )
    key = auth.create_api_key(users[0].user_id, "integration")
    url = f"/api/v1/auth/apikeys/{key['key_id']}/webhook"
    config = {
        "enabled": True,
        "url": "https://key.example/hook",
        "secret": "key-secret",
        "auth_type": "api_key",
        "auth_header_name": "X-Partner-Key",
        "auth_header_value": "partner-secret",
    }
    response = client.put(url, headers=bearer(tokens[0]), json=config)
    assert response.status_code == 200
    assert response.json()["webhook"]["secret"] == "********"
    assert "partner-secret" not in response.text
    assert client.get(url, headers=bearer(tokens[1])).status_code == 404
    masked = response.json()["webhook"]
    masked["url"] = "https://key.example/new"
    assert client.put(url, headers=bearer(tokens[0]), json=masked).status_code == 200
    assert auth.get_api_key_webhook(key["key_id"])["webhook_secret"] == "key-secret"
    config["url"] = "https://key.example/new"
    key_config = subscription(None, auth.db_path, key["key_id"])
    assert key_config["source"] == "api_key" and key_config["secret"] == "key-secret"
    override = subscription("https://global.example/hook", auth.db_path, key["key_id"])
    assert override["source"] == "task" and not override["secret"] and not override["authorization"]
    assert subscription(None, auth.db_path)["secret"] == "global-secret"
    submitted = client.post(
        "/api/v1/tasks/submit", headers={"X-API-Key": key["api_key"]}, files={"file": ("a.md", b"text")}
    )
    assert submitted.status_code == 200
    tid = submitted.json()["task_id"]
    assert api.db.get_task(tid)["api_key_id"] == key["key_id"]
    # Subsequent settings/deletion cannot change already submitted tasks' snapshots.
    client.put(url, headers=bearer(tokens[0]), json={"enabled": False, "url": ""})
    with api.db.get_cursor() as c:
        saved = json.loads(c.execute("SELECT config FROM task_webhooks WHERE task_id=?", (tid,)).fetchone()[0])
    assert saved["secret"] == "key-secret" and saved["url"] == "https://key.example/new"
    api.db.cancel_task(tid)
    with api.db.get_cursor() as c:
        delivery = json.loads(c.execute("SELECT config FROM webhook_deliveries WHERE task_id=?", (tid,)).fetchone()[0])
    assert delivery["api_key_id"] == key["key_id"] and delivery["source"] == "api_key"


@pytest.mark.parametrize(
    "name", ["Host", "Content-Length", "Transfer-Encoding", "X-Tianshu-Signature", "Connection", "bad name"]
)
def test_callback_reserved_headers_rejected(name):
    from webhooks import build_auth_headers

    with pytest.raises(ValueError):
        build_auth_headers({"auth_type": "api_key", "auth_header_name": name, "auth_header_value": "secret"})


def test_callback_header_modes_and_crlf():
    from webhooks import build_auth_headers

    assert build_auth_headers({"auth_type": "basic", "auth_username": "a", "auth_password": "b"}) == {
        "Authorization": "Basic YTpi"
    }
    assert build_auth_headers({"auth_type": "bearer", "auth_token": "secret"}) == {"Authorization": "Bearer secret"}
    with pytest.raises(ValueError):
        build_auth_headers({"auth_type": "bearer", "auth_token": "x\r\nBad: header"})


def test_scoped_key_cannot_reconfigure_callbacks(secured):
    api, auth, client, users, tokens = secured
    key = auth.create_api_key(users[0].user_id, "scoped", scopes=["apikey:create", "apikey:list:own"])
    assert (
        client.put(
            f"/api/v1/auth/apikeys/{key['key_id']}/webhook",
            headers={"X-API-Key": key["api_key"]},
            json={"enabled": False},
        ).status_code
        == 403
    )


def test_file_index_tracks_raw_sql_updates_deletes_and_rollback(db, tmp_path):
    root = tmp_path / "results"
    root.mkdir()
    nested = root / "a"
    nested.mkdir()
    file = nested / "x.png"
    file.touch()
    tid = db.create_task("a.pdf", "/source", user_id="owner")
    with sqlite3.connect(db.db_path) as c:
        c.execute("UPDATE tasks SET result_path=? WHERE task_id=?", (str(nested), tid))
    assert db.get_task_by_file_path(file, root, True)["task_id"] == tid
    with db.get_cursor() as c:
        plan = c.execute(
            "EXPLAIN QUERY PLAN SELECT task_id FROM task_file_access WHERE kind=? AND canonical_path=?",
            ("output", str(nested)),
        ).fetchall()
        assert any("idx_file_access_path" in row[3] for row in plan)
    with sqlite3.connect(db.db_path) as c:
        c.execute("UPDATE tasks SET result_path='CLEARED' WHERE task_id=?", (tid,))
        c.rollback()
    assert db.get_task_by_file_path(file, root, True)["task_id"] == tid
    with sqlite3.connect(db.db_path) as c:
        c.execute("UPDATE tasks SET result_path='CLEARED' WHERE task_id=?", (tid,))
    assert db.get_task_by_file_path(file, root, True) is None
    with sqlite3.connect(db.db_path) as c:
        c.execute("DELETE FROM tasks WHERE task_id=?", (tid,))
    with db.get_cursor() as c:
        assert not c.execute("SELECT * FROM task_file_access WHERE task_id=?", (tid,)).fetchall()


def test_index_migration_and_repeated_init_do_not_backfill_again(db, tmp_path):
    from file_access_index import init_schema

    tid = db.create_task("a.pdf", str(tmp_path / "a.pdf"))
    db.get_task_by_file_path(tmp_path / "a.pdf", tmp_path)
    with db.get_cursor() as c:
        before = c.execute("SELECT canonical_path FROM task_file_access WHERE task_id=?", (tid,)).fetchone()[0]
        statements = []
        c.connection.set_trace_callback(statements.append)
        init_schema(c)
        assert not any("INSERT OR IGNORE INTO task_file_access" in line for line in statements)
        assert c.execute("SELECT canonical_path FROM task_file_access WHERE task_id=?", (tid,)).fetchone()[0] == before


@pytest.fixture
def downloader(monkeypatch):
    import download_models as module

    # Any unintended network path fails the test. Only individual tests override a fake SDK download.
    def forbidden(*args, **kwargs):
        raise AssertionError("Real model downloads are forbidden in these tests")

    for name in ["download_from_modelscope", "download_from_huggingface", "download_paddle_tar", "download_url_file"]:
        monkeypatch.setattr(module, name, forbidden)
    return module


def fake_pro(root, config):
    target = root / config["target_dir"]
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.json").write_text("{}")
    (target / "model.safetensors").write_bytes(b"fake-test-weight")


def test_new_model_addition_preserves_existing_config_manifest_and_default_selection(downloader, tmp_path, monkeypatch):
    d = downloader
    assert "mineru_vlm_pro" not in d.selected_model_map(None)
    assert d.MODELS["mineru_vlm"]["target_dir"] == "MinerU2.5-2509-1.2B"
    d.generate_mineru_json(tmp_path)
    old = json.loads((tmp_path / "mineru.json").read_text())
    old["custom"] = {"preserve": True}
    (tmp_path / "mineru.json").write_text(json.dumps(old))
    (tmp_path / "manifest.json").write_text(json.dumps({"models": {"existing_audio": {"status": "exists"}}}))

    def download(config, target):
        fake_pro(tmp_path, config)

    monkeypatch.setattr(d, "download_from_modelscope", download)
    assert d.main(str(tmp_path), "mineru_vlm_pro", strict=True) == 0
    config = json.loads((tmp_path / "mineru.json").read_text())
    assert config["models-dir"]["vlm"].endswith("MinerU2.5-2509-1.2B") and config["custom"] == {"preserve": True}
    assert config["model-source"] == "local" and config["config_version"] == "1.3.2"
    assert "existing_audio" in json.loads((tmp_path / "manifest.json").read_text())["models"]
    monkeypatch.setattr(
        d, "download_from_modelscope", lambda *a: pytest.fail("Already verified models must be skipped")
    )
    assert d.main(str(tmp_path), "mineru_vlm_pro", strict=True, vlm_model="mineru_vlm_pro") == 0
    assert json.loads((tmp_path / "mineru.json").read_text())["models-dir"]["vlm"].endswith("MinerU2.5-Pro-2605-1.2B")
    assert d.main(str(tmp_path), "mineru_vlm_pro", verify_only=True, strict=True) == 0


def test_failed_download_never_switches_old_vlm(downloader, tmp_path):
    d = downloader
    d.generate_mineru_json(tmp_path)
    original = (tmp_path / "mineru.json").read_text()
    assert d.main(str(tmp_path), "mineru_vlm_pro", strict=True, vlm_model="mineru_vlm_pro") == 1
    assert (tmp_path / "mineru.json").read_text() == original


def test_incomplete_shards_are_not_considered_offline_ready(downloader, tmp_path):
    d = downloader
    cfg = d.MODELS["mineru_vlm_pro"]
    fake_pro(tmp_path, cfg)
    index = tmp_path / cfg["target_dir"] / "model.safetensors.index.json"
    index.write_text(json.dumps({"weight_map": {"one": "model.safetensors", "two": "missing.safetensors"}}))
    assert d.verify_model(tmp_path, "mineru_vlm_pro", cfg)[0] is False


def test_external_prepare_entrypoint_only_new_model_keeps_cache_layout(downloader, tmp_path, monkeypatch):
    mod = load_script("external_prepare", "backend/prepare_offline_models.py")
    (tmp_path / "modelscope_cache").mkdir()
    (tmp_path / "modelscope_cache/audio-sentinel").write_bytes(b"audio")
    monkeypatch.setattr(downloader, "download_from_modelscope", lambda config, target: fake_pro(tmp_path, config))
    assert mod.main([str(tmp_path), "--only", "mineru-vlm-pro", "--vlm-model", "mineru_vlm_pro"]) == 0
    assert (tmp_path / "modelscope_cache/audio-sentinel").read_bytes() == b"audio"
    assert mod.MSCOPE == tmp_path / "modelscope_cache" and mod.PADDLEX == tmp_path / "paddlex_cache"
    assert (
        json.loads((tmp_path / "mineru.json").read_text())["models-dir"]["pipeline"]
        == "/app/models/PDF-Extract-Kit-1.0"
    )


def test_legacy_pipeline_path_is_corrected_without_changing_custom_fields(downloader, tmp_path):
    (tmp_path / "mineru.json").write_text(
        json.dumps(
            {"models-dir": {"pipeline": "/app/models/PDF-Extract-Kit-1.0/models", "vlm": "/custom/old"}, "custom": 42}
        )
    )
    downloader.generate_mineru_json(tmp_path)
    data = json.loads((tmp_path / "mineru.json").read_text())
    assert (
        data["models-dir"] == {"pipeline": "/app/models/PDF-Extract-Kit-1.0", "vlm": "/custom/old"}
        and data["custom"] == 42
    )


@pytest.mark.parametrize("ram", [4, 8, 32, 128])
def test_hardware_budget_respects_host_and_explicit_values(ram):
    deploy = load_script("deploy", "scripts/deployment.py")
    values = deploy.budget(ram, [24, 24], {"GPU_COUNT": "2", "MAX_CONCURRENT_TASKS": "2"})
    assert int(values["WORKER_MEMORY_LIMIT"][:-1]) <= ram
    assert values["MINERU_VIRTUAL_VRAM_SIZE"] == "12"
    assert deploy.budget(ram, [24], {"WORKER_MEMORY_LIMIT": "18G"})["WORKER_MEMORY_LIMIT"] == "18G"


@pytest.mark.parametrize("suffix", [".docx", ".xlsx", ".pptx"])
def test_native_office_preserves_extension_without_loading_models(tmp_path, suffix):
    import tempfile
    import shutil
    import time
    from loguru import logger

    tree = ast.parse((ROOT / "backend/mineru_pipeline/engine.py").read_text())
    parse = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "parse")
    ns = {
        "Path": Path,
        "Optional": object,
        "Dict": dict,
        "Any": object,
        "time": time,
        "tempfile": tempfile,
        "shutil": shutil,
        "logger": logger,
        "json": json,
    }
    # Strip annotations for isolated execution; no torch or model imports.
    parse.returns = None
    for arg in parse.args.args:
        arg.annotation = None
    exec(compile(ast.Module(body=[parse], type_ignores=[]), "<native-office>", "exec"), ns)
    observed = []

    def fake_parse(**kwargs):
        observed.append(kwargs)
        out = Path(kwargs["output_dir"]) / "result"
        out.mkdir()
        (out / "result.md").write_text("content")

    obj = SimpleNamespace(
        is_offloaded=False, _load_pipeline=lambda: fake_parse, _clean_markdown=lambda s: s, vlm_api_base=None
    )
    source = tmp_path / f"office{suffix}"
    source.write_bytes(b"office-test-data")
    result = ns["parse"](obj, str(source), str(tmp_path / "out"), {"parse_mode": "pipeline"})
    assert observed[0]["pdf_file_names"] == ["result" + suffix]
    assert observed[0]["pdf_bytes_list"] == [b"office-test-data"] and result["markdown"] == "content"


def test_mcp_service_credentials_are_isolated_per_request(monkeypatch):
    import mcp_server

    monkeypatch.setattr(mcp_server, "MCP_API_KEYS", [])
    monkeypatch.setattr(mcp_server, "MCP_API_KEY_MAP", {"first": "backend-one", "second": "backend-two"})

    async def exercise():
        async def endpoint(scope, receive, send):
            await asyncio.sleep(0)
            return mcp_server._api_headers()["X-API-Key"]

        seen = {}

        async def wrapped(scope, receive, send):
            seen[scope["headers"][0][1]] = await endpoint(scope, receive, send)

        app = mcp_server.MCPAuthMiddleware(wrapped)

        async def call(key):
            await app({"type": "http", "path": "/messages", "headers": [(b"x-api-key", key)]}, None, None)

        await asyncio.gather(call(b"first"), call(b"second"))
        assert seen == {b"first": "backend-one", b"second": "backend-two"}
        assert mcp_server._service_key.get() is None

    asyncio.run(exercise())


def test_english_locale_is_complete_and_contains_no_chinese():
    import re

    locales = [
        json.loads((ROOT / f"frontend/src/locales/{lang}.ts").read_text().removeprefix("export default "))
        for lang in ["zh-CN", "en-US"]
    ]

    def flatten(obj, prefix=""):
        return {
            k: v
            for name, value in obj.items()
            for k, v in (
                flatten(value, prefix + name + ".").items() if isinstance(value, dict) else [(prefix + name, value)]
            )
        }

    zh, en = map(flatten, locales)
    assert set(zh) == set(en)
    assert not [(k, v) for k, v in en.items() if isinstance(v, str) and re.search("[\u4e00-\u9fff]", v)]


def test_optional_upload_content_check(secured, monkeypatch):
    api, auth, client, users, tokens = secured
    monkeypatch.setenv("UPLOAD_VALIDATE_CONTENT", "true")
    bad = client.post("/api/v1/tasks/submit", headers=bearer(tokens[0]), files={"file": ("fake.png", b"not png")})
    assert bad.status_code == 400 and not list(api.UPLOAD_DIR.iterdir())
    good = client.post(
        "/api/v1/tasks/submit", headers=bearer(tokens[0]), files={"file": ("real.pdf", b"%PDF-1.7\nfixture")}
    )
    assert good.status_code == 200


def test_cleanup_keeps_files_outside_configured_roots(db, tmp_path, monkeypatch):
    upload = tmp_path / "uploads"
    output = tmp_path / "output"
    upload.mkdir()
    output.mkdir()
    monkeypatch.setenv("UPLOAD_PATH", str(upload))
    monkeypatch.setenv("OUTPUT_PATH", str(output))
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "keep").write_bytes(b"keep")
    db._delete_task_files({"task_id": "unsafe", "file_path": str(outside / "keep"), "result_path": str(outside)})
    assert (outside / "keep").read_bytes() == b"keep"
    source = output / "nested-source.pdf"
    source.touch()
    result = output / "result"
    result.mkdir()
    db._delete_task_files({"task_id": "safe", "file_path": str(source), "result_path": str(result)})
    assert not source.exists() and not result.exists()


def test_runtime_model_selection_follows_offline_config(tmp_path, monkeypatch):
    from model_layout import active_vlm_path

    monkeypatch.delenv("MINERU_VLM_MODEL_DIR", raising=False)
    (tmp_path / "mineru.json").write_text(json.dumps({"models-dir": {"vlm": "/custom/MinerU2.5-Pro-2605-1.2B"}}))
    assert str(active_vlm_path(tmp_path)) == "/custom/MinerU2.5-Pro-2605-1.2B"
    assert active_vlm_path(tmp_path, "MinerU2.5-2509-1.2B") == tmp_path / "MinerU2.5-2509-1.2B"


def test_interrupted_download_does_not_leave_a_valid_looking_file(downloader, tmp_path, monkeypatch):
    def interrupted(url, path):
        Path(path).write_bytes(b"partial")
        raise OSError("connection lost")

    monkeypatch.setattr(downloader, "urlretrieve", interrupted)
    # Exercise the real file-transfer wrapper; network is still replaced by the stub.
    original = load_script("download_source", "backend/download_models.py")
    monkeypatch.setattr(original, "urlretrieve", interrupted)
    with pytest.raises(OSError):
        original.download_url_file({"target_file": "font.ttf", "url": "https://example.test/font"}, tmp_path)
    assert not (tmp_path / "font.ttf").exists() and not (tmp_path / "font.ttf.download").exists()


def test_atomic_config_replace_failure_keeps_previous_file(downloader, tmp_path, monkeypatch):
    downloader.generate_mineru_json(tmp_path)
    previous = (tmp_path / "mineru.json").read_bytes()

    def fail(*a):
        raise OSError("read only destination")

    monkeypatch.setattr(downloader.os, "replace", fail)
    with pytest.raises(OSError):
        downloader.generate_mineru_json(tmp_path)
    assert (tmp_path / "mineru.json").read_bytes() == previous


def test_pipeline_mode_is_opt_in_and_rejects_accidental_vlm_start(monkeypatch):
    from runtime_modes import validate_pipeline_task

    monkeypatch.delenv("TIANSHU_DEPLOY_MODE", raising=False)
    validate_pipeline_task("a.wav", "sensevoice")
    monkeypatch.setenv("TIANSHU_DEPLOY_MODE", "pipeline")
    validate_pipeline_task("a.pdf", "pipeline")
    with pytest.raises(ValueError):
        validate_pipeline_task("a.pdf", "vlm-http-client")
    with pytest.raises(ValueError):
        validate_pipeline_task("a.wav", "auto")


@pytest.mark.parametrize("available,expected", [(True, "mps"), (False, "cpu")])
def test_mps_auto_detection_without_loading_torch(monkeypatch, available, expected):
    tree = ast.parse((ROOT / "backend/litserve_worker.py").read_text())
    resolve = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "resolve_auto_accelerator")
    monkeypatch.setattr("importlib.metadata.distribution", lambda name: object())
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: available))),
    )
    namespace = {"check_cuda_with_nvidia_smi": lambda: 0}
    exec(compile(ast.Module(body=[resolve], type_ignores=[]), "<mps-auto>", "exec"), namespace)
    assert namespace["resolve_auto_accelerator"]() == expected


def test_pipeline_upgrade_detects_v5_only_pack_and_adds_v6_without_removing_old_weights(
    downloader, tmp_path, monkeypatch
):
    config = downloader.MODELS["mineru_pipeline"]
    target = tmp_path / config["target_dir"]
    old = target / "models/OCR/paddleocr_torch/ch_PP-OCRv5_rec_infer.pth"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old-weights-fixture")
    assert not downloader.verify_model(tmp_path, "mineru_pipeline", config)[0]
    calls = []

    def supplement(cfg, destination):
        calls.append(destination)
        for name in cfg["verify"]:
            file = destination / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"new-fixture")

    monkeypatch.setattr(downloader, "download_from_modelscope", supplement)
    assert downloader.main(str(tmp_path), "mineru_pipeline", strict=True) == 0
    assert old.read_bytes() == b"old-weights-fixture" and calls == [target]
    assert downloader.main(str(tmp_path), "mineru_pipeline", verify_only=True, strict=True) == 0


def test_mineru_dependency_groups_do_not_reintroduce_gradio_conflict():
    for path in [
        "backend/requirements.txt",
        "backend/requirements.native.txt",
        "backend/Dockerfile",
        "backend/Dockerfile.cpu",
        "backend/Dockerfile.offline",
    ]:
        data = (ROOT / path).read_text()
        assert "mineru[pipeline,vlm,s3]==3.4.5" in data
        assert "mineru[core]==3.4.5" not in data and "mineru[all]==3.4.5" not in data


def test_vllm_entrypoint_uses_same_model_selection_as_offline_config(tmp_path):
    module = load_script("vlm_entrypoint", "scripts/vllm-model-entrypoint.py")
    assert module.selected_model(tmp_path) == tmp_path / "MinerU2.5-2509-1.2B"
    (tmp_path / "mineru.json").write_text(json.dumps({"models-dir": {"vlm": "/app/models/MinerU2.5-Pro-2605-1.2B"}}))
    assert module.selected_model(tmp_path) == tmp_path / "MinerU2.5-Pro-2605-1.2B"
    assert module.selected_model(tmp_path, "MinerU2.5-2509-1.2B") == tmp_path / "MinerU2.5-2509-1.2B"


@pytest.mark.parametrize("suffix", [".xlsx", ".pptx"])
@pytest.mark.parametrize("parser", ["compatible", "mineru"])
def test_office_parser_choice_preserves_existing_default(db, tmp_path, suffix, parser):
    from test_review_regressions import make_worker

    obj = make_worker(db, tmp_path)
    called = []
    obj._process_office = lambda *a: called.append("compatible") or {"result_path": str(tmp_path)}
    obj._process_with_mineru = lambda *a: called.append("mineru") or {"result_path": str(tmp_path)}
    options = {} if parser == "compatible" else {"office_parser": "mineru"}
    tid = db.create_task("document" + suffix, str(tmp_path / ("document" + suffix)), backend="auto", options=options)
    obj._process_task(db.get_next_task(obj.worker_id))
    assert called == [parser] and db.get_task(tid)["status"] == "completed"


def test_mcp_sdk_rejects_cross_client_session_messages(monkeypatch):
    import uuid
    import mcp_server
    from mcp.server.sse import SseServerTransport, authorization_context
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    monkeypatch.setattr(mcp_server, "MCP_API_KEYS", ["first", "second"])
    transport = SseServerTransport(
        "/messages", security_settings=TransportSecuritySettings(allowed_hosts=["testserver"], allowed_origins=[])
    )
    session = uuid.uuid4()
    received = []

    class Writer:
        async def send(self, message):
            received.append(message)

    transport._read_stream_writers[session] = Writer()
    transport._session_owners[session] = authorization_context(mcp_server._mcp_identity("first"))

    class Endpoint:
        async def __call__(self, scope, receive, send):
            await transport.handle_post_message(scope, receive, send)

    app = mcp_server.MCPAuthMiddleware(Starlette(routes=[Route("/messages", endpoint=Endpoint(), methods=["POST"])]))
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with TestClient(app) as client:
        url = f"/messages?session_id={session.hex}"
        assert client.post(url, json=payload, headers={"X-API-Key": "second"}).status_code == 404
        assert client.post(url, json=payload, headers={"X-API-Key": "first"}).status_code == 202
    assert len(received) == 1
