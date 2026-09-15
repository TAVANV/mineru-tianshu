"""Second-batch compatibility, failure handling and lifecycle regressions."""

import ast
import json
from pathlib import Path
import sys
import time
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from test_phase1 import new_task, ROOT

from webhooks import dispatch, validate_url
from utils.archive_utils import extract_archive, archive_documents, convert_epub, merge_archive_results


def set_status(db, tid, status):
    with db.get_cursor() as c:
        c.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, tid))


def hook_task(db):
    return new_task(
        db,
        webhook_config={
            "url": "https://callback.example/hook",
            "timeout": 1,
            "max_attempts": 2,
            "secret": "sign",
            "authorization": "",
        },
    )


def test_webhook_outbox_atomic_parent_retry_and_recovery(db, monkeypatch):
    tid = hook_task(db)
    db.convert_to_parent_task(tid)
    child = db.create_child_task(tid, "child.pdf", "/child.pdf")
    set_status(db, child, "processing")
    assert db.update_task_status(child, "completed")
    with db.get_cursor() as c:
        assert c.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0] == 0
    assert db.on_child_task_completed(child) == tid
    assert db.on_child_task_completed(child) is None
    assert db.get_task(tid)["child_completed"] == 1
    assert db.update_task_status(tid, "failed", error_message="merge failed")
    assert db.retry_task(tid)
    assert db.get_task(tid)["is_parent"] == 0
    set_status(db, tid, "processing")
    assert db.update_task_status(tid, "completed")
    assert not db.update_task_status(tid, "failed")
    with db.get_cursor() as c:
        rows = c.execute("SELECT * FROM webhook_deliveries ORDER BY retry_count").fetchall()
        assert [(r["task_status"], r["retry_count"]) for r in rows] == [("failed", 0), ("completed", 1)]
        c.execute("UPDATE webhook_deliveries SET state='sending',next_at=0,lease_token='dead-scheduler'")
    calls = []
    monkeypatch.setattr("webhooks.send", lambda conf, body, event, allowed: calls.append((json.loads(body), event)))
    assert dispatch(db) == 2
    assert dispatch(db) == 0
    assert all(body["event_id"] == event for body, event in calls)


def test_webhook_failure_retries_then_dead_without_changing_task(db, monkeypatch):
    tid = hook_task(db)
    set_status(db, tid, "processing")
    db.update_task_status(tid, "completed")

    def fail(*args):
        raise RuntimeError("sensitive receiver response")

    monkeypatch.setattr("webhooks.send", fail)
    assert dispatch(db) == 0
    with db.get_cursor() as c:
        row = c.execute("SELECT * FROM webhook_deliveries").fetchone()
        assert row["attempt"] == 1 and row["state"] == "pending" and row["next_at"] > time.time()
        assert "sensitive" not in row["last_error"]
        c.execute("UPDATE webhook_deliveries SET next_at=0")
    assert dispatch(db) == 0
    with db.get_cursor() as c:
        assert c.execute("SELECT state FROM webhook_deliveries").fetchone()[0] == "dead"
    assert db.get_task(tid)["status"] == "completed"


def test_webhook_private_allowlist_and_override_secret_isolation(db, monkeypatch):
    import socket
    from feature_config import update_config
    from webhooks import subscription

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 8080))]
    )
    with pytest.raises(ValueError):
        validate_url("http://internal:8080/callback")
    assert validate_url("http://internal:8080/callback", "internal:8080")[1] == "10.0.0.8"
    with pytest.raises(ValueError):
        validate_url("http://internal:9090/callback", "internal:8080")
    update_config(
        {
            "webhook_enabled": True,
            "webhook_url": "http://internal:8080/default",
            "webhook_allowed_hosts": "internal:8080",
            "webhook_secret": "secret",
            "webhook_authorization": "Bearer private",
        },
        db.db_path,
    )
    normal = subscription(None, db.db_path)
    override = subscription("http://internal:8080/another", db.db_path)
    assert normal["authorization"] and normal["secret"]
    assert override["authorization"] == override["secret"] == ""


def test_cancel_group_no_resurrection_or_late_merge(db, monkeypatch):
    import fakeredis
    from redis_queue import RedisTaskQueue, RedisConfig

    q = RedisTaskQueue(RedisConfig())
    q._client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("task_db.get_redis_queue", lambda: q)
    parent = hook_task(db)
    db.convert_to_parent_task(parent)
    children = db.create_child_tasks_bulk(parent, [dict(file_name=f"{i}.pdf", file_path=f"/{i}.pdf") for i in range(3)])
    set_status(db, children[0], "processing")
    set_status(db, children[1], "paused")
    assert db.cancel_task(children[0])
    for tid in [parent] + children:
        assert db.get_task(tid)["status"] == "cancelled"
        assert not db.retry_task(tid)
        assert not db.resume_task(tid)
        assert not db.update_task_status(tid, "completed")
        assert not db.update_task_status(tid, "pending")
    assert db.on_child_task_completed(children[0]) is None
    db.on_child_task_failed(children[0], "late error")
    assert db.get_task(parent)["status"] == "cancelled"
    db.convert_to_parent_task(parent)
    assert db.get_task(parent)["status"] == "cancelled"
    with pytest.raises(ValueError):
        db.create_child_tasks_bulk(parent, [dict(file_name="late", file_path="/late")])
    assert not q.get_queued_ids()
    assert db.sync_pending_to_redis() == 0
    assert db.reset_stale_tasks(0) == 0
    with db.get_cursor() as c:
        assert c.execute("SELECT task_status FROM webhook_deliveries").fetchall()[0][0] == "cancelled"


def test_cancel_during_child_insert_rolls_back(db):
    parent = new_task(db)
    db.convert_to_parent_task(parent)
    assert db.cancel_task(parent)
    with pytest.raises(ValueError):
        db.create_child_task(parent, "late.pdf", "/late.pdf")
    assert db.get_child_tasks(parent) == []


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)


@pytest.mark.parametrize(
    "name", ["../escape.txt", "/escape.txt", "a/../../escape.txt", "C:/escape.txt", "a\\escape.txt"]
)
def test_zip_rejects_unsafe_paths(tmp_path, name):
    source = tmp_path / "test.zip"
    make_zip(source, {name: "unsafe"})
    with pytest.raises(ValueError):
        extract_archive(source, tmp_path / "out")


def test_zip_limits_and_unique_members(tmp_path, monkeypatch):
    source = tmp_path / "test.zip"
    make_zip(source, {"a/same.md": "one", "b/same.md": "two", "nested.zip": b"not-supported"})
    docs = archive_documents(source, tmp_path / "out")
    assert len(docs) == 2
    assert len({Path(d["file_path"]).stem for d in docs}) == 2
    monkeypatch.setattr("utils.archive_utils.MAX_FILES", 1)
    with pytest.raises(ValueError):
        extract_archive(source, tmp_path / "limited")


def test_epub_local_spine_order_images_and_archive_merge(tmp_path):
    book = tmp_path / "book.epub"
    make_zip(
        book,
        {
            "META-INF/container.xml": '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>',
            "OPS/book.opf": '<package><manifest><item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/></manifest><spine><itemref idref="b"/><itemref idref="a"/></spine></package>',
            "OPS/a.xhtml": '<html><h1>Chapter A</h1><img src="img.png"/><img src="https://external/image.png"/></html>',
            "OPS/b.xhtml": "<html><h1>Chapter B</h1><p>Offline text</p></html>",
            "OPS/img.png": b"image",
        },
    )
    output = tmp_path / "epub"
    convert_epub(book, output)
    text = (output / "result.md").read_text()
    assert text.index("Chapter B") < text.index("Chapter A")
    assert "https://external" not in text and "images/" in text
    children = []
    for i in range(2):
        path = tmp_path / str(i)
        (path / "images").mkdir(parents=True)
        (path / "images" / "same.png").write_bytes(str(i).encode())
        (path / "result.md").write_text("![alt](images/same.png)")
        (path / "result.json").write_text(json.dumps([{"img_path": "images/same.png"}]))
        children.append(
            {
                "task_id": str(i),
                "file_name": str(i),
                "result_path": str(path),
                "options": json.dumps({"archive_index": i, "archive_member": f"{i}.md"}),
            }
        )
    merged = tmp_path / "merged"
    merge_archive_results(children, merged)
    text = (merged / "result.md").read_text()
    data = json.loads((merged / "result.json").read_text())
    assert "images/0_same.png" in text and "images/1_same.png" in text
    assert data["documents"][1]["data"][0]["img_path"] == "images/1_same.png"
    assert (merged / "images/0_same.png").read_bytes() == b"0"
    assert (merged / "images/1_same.png").read_bytes() == b"1"


@pytest.mark.parametrize("use_rustfs", [None, True, False])
def test_caption_precedes_upload_without_losing_rustfs_switch(tmp_path, monkeypatch, use_rustfs):
    from output_normalizer.base_output_normalizer import BaseOutputNormalizer
    from image_caption import ImageCaptionConfig
    from image_caption.processor import _write_back_markdown, _write_back_json

    class Normalizer(BaseOutputNormalizer):
        def _normalize_local_files(self, output_dir):
            return {"image_dir": output_dir / "images", "image_count": 1}

        def _process_rustfs_upload(self, result, use_rustfs=None):
            assert "caption" in (tmp_path / "result.md").read_text()
            assert use_rustfs is expected

    expected = use_rustfs
    (tmp_path / "images").mkdir()
    (tmp_path / "images/x.png").write_bytes(b"image")
    (tmp_path / "result.md").write_text("![alt](images/x.png)")
    (tmp_path / "result.json").write_text('[{"img_path":"images/x.png"}]')
    monkeypatch.setattr(ImageCaptionConfig, "load", lambda: object())

    def caption(directory, config):
        _write_back_markdown(directory / "result.md", {"x.png": "caption [safe]"})
        _write_back_json(directory / "result.json", {"x.png": "caption [safe]"})

    monkeypatch.setattr("image_caption.process_output_dir", caption)
    Normalizer().normalize(tmp_path, use_rustfs=use_rustfs)
    assert "\\[safe\\]" in (tmp_path / "result.md").read_text()
    assert json.loads((tmp_path / "result.json").read_text())[0]["img_caption"] == ["caption [safe]"]


def test_caption_disabled_or_failure_keeps_local_output(tmp_path, monkeypatch):
    from output_normalizer import normalize_output
    from image_caption import ImageCaptionConfig

    (tmp_path / "result.md").write_text("plain")
    monkeypatch.setattr(ImageCaptionConfig, "load", lambda: None)
    monkeypatch.setattr("image_caption.process_output_dir", lambda *args: pytest.fail("disabled caption invoked"))
    normalize_output(tmp_path, use_rustfs=False)
    assert (tmp_path / "result.md").read_text() == "plain"


def test_invitation_registration_public_config_and_admin_controls(api, monkeypatch):
    from fastapi.testclient import TestClient
    from auth.auth_db import AuthDB
    from auth.dependencies import get_auth_db, get_current_active_user
    from types import SimpleNamespace
    from feature_config import update_config

    auth = AuthDB(api.db.db_path)
    api.app.dependency_overrides[get_auth_db] = lambda: auth
    try:
        with TestClient(api.app) as client:
            user = {
                "username": "phase2user",
                "email": "phase2@example.com",
                "password": "test-password123",
                "role": "admin",
            }
            response = client.post("/api/v1/auth/register", json=user)
            assert response.status_code == 201 and response.json()["role"] == "user"
            update_config({"registration_invite_code": "邀请码", "image_caption_api_key": "secret"}, api.db.db_path)
            user["username"] = "seconduser"
            user["email"] = "second@example.com"
            assert client.post("/api/v1/auth/register", json=user).status_code == 403
            assert client.post("/api/v1/auth/register", json=dict(user, invite_code="邀请码")).status_code == 201
            public = client.get("/api/v1/auth/system/config").json()["config"]
            assert (
                public["registration_invite_required"]
                and "registration_invite_code" not in public
                and "image_caption_api_key" not in public
            )
            assert client.get("/api/v1/admin/optional-features").status_code == 401
            api.app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
                user_id="admin", has_permission=lambda p: True
            )
            config = client.get("/api/v1/admin/optional-features").json()["config"]
            assert config["image_caption_api_key"] == "********"
            assert (
                client.post("/api/v1/admin/optional-features", json={"image_caption_concurrency": 100}).status_code
                == 400
            )
    finally:
        api.app.dependency_overrides.clear()


def test_audit_failures_are_nonblocking_and_secrets_not_recorded(api, monkeypatch):
    from auth import audit

    audit._events.join()
    audit.record_audit("test", detail={"password": "do-not-store", "changed_keys": ["image_caption_api_key"]})
    audit._events.join()
    rows, count = audit.query_audit_logs(action="test")
    assert count == 1 and "do-not-store" not in rows[0]["detail"]
    monkeypatch.setattr(audit, "_get_conn", lambda path: (_ for _ in ()).throw(RuntimeError("database unavailable")))
    before = time.monotonic()
    audit.record_audit("failed-write")
    assert time.monotonic() - before < 0.1
    audit._events.join()


def test_zip_worker_end_to_end_without_models(db, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from loguru import logger
    import shutil
    import os
    from output_normalizer import normalize_output

    tree = ast.parse((ROOT / "backend/litserve_worker.py").read_text())
    worker = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MinerUWorkerAPI")
    methods = [
        n
        for n in worker.body
        if isinstance(n, ast.FunctionDef)
        and n.name
        in {
            "_process_task",
            "_split_archive",
            "_merge_parent_task_results",
            "_complete_parent_merge",
            "_cleanup_child_task_files",
        }
    ]
    namespace = {
        "Path": Path,
        "os": os,
        "json": json,
        "logger": logger,
        "shutil": shutil,
        "normalize_output": normalize_output,
        "FORMAT_ENGINES_AVAILABLE": False,
        "SENSEVOICE_AVAILABLE": False,
        "VIDEO_ENGINE_AVAILABLE": False,
        "MINERU_PIPELINE_AVAILABLE": False,
    }
    exec(compile(ast.Module(body=methods, type_ignores=[]), "<worker-archive>", "exec"), namespace)
    instance = SimpleNamespace(task_db=db, output_dir=str(tmp_path), watermark_handler=None)
    for method in methods:
        setattr(instance, method.name, namespace[method.name].__get__(instance))

    def process_md(path, options):
        assert options["use_rustfs"] is False
        out = tmp_path / Path(path).stem
        out.mkdir(exist_ok=True)
        (out / "result.md").write_text(Path(path).read_text())
        return {"result_path": str(out)}

    instance._process_markdown = process_md
    archive = tmp_path / "input.zip"
    make_zip(archive, {"a.md": "First document", "b.md": "Second document"})
    parent = db.create_task("input.zip", str(archive), backend="auto", options={"use_rustfs": False})
    instance._process_task(db.get_next_task("worker"))
    assert db.get_task(parent)["is_parent"] == 1 and db.get_task(parent)["child_count"] == 2
    for _ in range(2):
        instance._process_task(db.get_next_task("worker"))
    result = db.get_task(parent)
    assert result["status"] == "completed"
    content = (Path(result["result_path"]) / "result.md").read_text()
    assert "First document" in content and "Second document" in content
    assert db.get_task(parent)["child_completed"] == 2


def test_http_webhook_signature_and_no_redirects():
    import hashlib
    import hmac
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from webhooks import send

    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(302 if self.path == "/redirect" else 200)
            self.send_header("Location", "/must-not-follow")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"127.0.0.1:{server.server_port}"
    config = {"url": f"http://{host}/hook", "secret": "key", "authorization": "Bearer test", "timeout": 1}
    body = b'{"status":"completed"}'
    try:
        send(config, body, "event-1", host)
        _, headers, actual_body = received[0]
        assert actual_body == body
        assert headers["X-Tianshu-Signature"] == "sha256=" + hmac.new(b"key", body, hashlib.sha256).hexdigest()
        assert headers["Authorization"] == "Bearer test"
        with pytest.raises(RuntimeError):
            send(dict(config, url=f"http://{host}/redirect"), body, "event-2", host)
        assert [p for p, _, _ in received] == ["/hook", "/redirect"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_caption_html_without_alt_and_exact_filename(tmp_path):
    from image_caption.processor import _write_back_markdown
    from bs4 import BeautifulSoup

    path = tmp_path / "result.md"
    path.write_text('<img src="images/x.png"><img src="images/not-x.png" alt="keep">')
    _write_back_markdown(path, {"x.png": 'caption "quote" <tag>'})
    images = BeautifulSoup(path.read_text(), "html.parser").find_all("img")
    assert images[0]["alt"] == 'caption "quote" <tag>'
    assert images[1]["alt"] == "keep"


def test_retry_cleanup_happens_before_pending_and_enqueue(db, monkeypatch):
    tid = new_task(db)
    set_status(db, tid, "failed")
    events = []

    def cleanup():
        assert db.get_task(tid)["status"] == "failed"
        events.append("cleanup")

    def enqueue(*args):
        assert db.get_task(tid)["status"] == "pending"
        events.append("enqueue")

    monkeypatch.setattr(db, "_enqueue_to_redis", enqueue)
    assert db.retry_task(tid, cleanup=cleanup)
    assert events == ["cleanup", "enqueue"]
