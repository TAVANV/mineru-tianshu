"""Regression coverage for the selectively ported first upstream batch."""

import ast
import asyncio
import inspect
import io
import json
from pathlib import Path
import sqlite3
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import task_db
from backup_db import backup_database


class QueueDouble:
    def __init__(self):
        self.queued = {}
        self.processing = set()
        self.read_failed = False
        self.write_failed = False

    def enqueue(self, task_id, priority=0, task_data=None):
        if self.write_failed:
            return False
        self.queued.setdefault(task_id, (priority, len(self.queued)))
        return True

    def get_queued_ids(self):
        return None if self.read_failed else set(self.queued)

    def prune_processing(self, ids):
        self.processing.difference_update(ids)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(task_db, "get_redis_queue", lambda: None)
    return task_db.TaskDB(str(tmp_path / "tasks.db"))


def new_task(db, **kwargs):
    return db.create_task(file_name="sample.pdf", file_path="/sample.pdf", **kwargs)


def test_bulk_atomicity_and_committed_enqueue(db, monkeypatch):
    parent = new_task(db)
    db.convert_to_parent_task(parent, child_count=0)
    observed = []

    def enqueue(task_id, priority, data):
        # Independent connection must see the entire batch before any enqueue.
        with sqlite3.connect(db.db_path) as conn:
            assert conn.execute("SELECT child_count FROM tasks WHERE task_id=?", (parent,)).fetchone()[0] == 3
        observed.append(task_id)

    monkeypatch.setattr(db, "_enqueue_to_redis", enqueue)
    children = [
        dict(file_name=f"{i}.pdf", file_path=f"/{i}.pdf", options={"use_rustfs": v, "upload_images": True})
        for i, v in enumerate([None, True, False])
    ]
    ids = db.create_child_tasks_bulk(parent, children, backend="pipeline", priority=7, user_id="owner")
    assert ids == observed
    for tid, expected in zip(ids, children):
        child = db.get_task(tid)
        assert json.loads(child["options"]) == expected["options"]
        assert child["priority"] == 7 and child["user_id"] == "owner"
    with pytest.raises(sqlite3.IntegrityError):
        db.create_child_tasks_bulk(
            parent, [dict(file_name="valid.pdf", file_path="/valid"), dict(file_name=None, file_path="/bad")]
        )
    assert db.get_task(parent)["child_count"] == 3
    assert db.create_child_tasks_bulk(parent, []) == []


def test_reconciliation_and_retry_resume(db, monkeypatch):
    queue = QueueDouble()
    monkeypatch.setattr(task_db, "get_redis_queue", lambda: queue)
    first, second = new_task(db, priority=9), new_task(db)
    original = queue.queued.copy()
    assert db.sync_pending_to_redis() == 0
    assert queue.queued == original
    queue.queued.clear()
    queue.processing.add(first)
    assert db.sync_pending_to_redis() == 2
    assert not queue.processing
    queue.queued.clear()
    queue.read_failed = True
    assert db.sync_pending_to_redis() == 0 and not queue.queued
    queue.read_failed = False
    queue.write_failed = True
    assert db.sync_pending_to_redis() == 0
    queue.write_failed = False
    assert db.pause_task(first)
    assert db.sync_pending_to_redis() == 1
    assert first not in queue.queued
    assert db.resume_task(first) and first in queue.queued
    queue.queued.pop(second)
    assert db.retry_task(second) and second in queue.queued


def test_stale_recovery_preserves_heartbeat_parent_and_retry_limit(db):
    ids = [new_task(db) for _ in range(4)]
    stale, fresh, exhausted, parent = ids
    with db.get_cursor() as c:
        c.execute(
            "UPDATE tasks SET status='processing', worker_id='worker', started_at=datetime('now', '-120 minutes')"
        )
        c.execute("UPDATE tasks SET retry_count=3 WHERE task_id=?", (exhausted,))
        c.execute("UPDATE tasks SET is_parent=1 WHERE task_id=?", (parent,))
    assert db.update_heartbeat(fresh, "worker")
    assert db.reset_stale_tasks(60) == 1
    assert db.get_task(stale)["status"] == "pending"
    assert db.get_task(stale)["retry_count"] == 1
    assert db.get_task(exhausted)["status"] == "failed"
    assert db.get_task(fresh)["status"] == db.get_task(parent)["status"] == "processing"


def test_wal_readers_and_online_backup(db, tmp_path):
    with sqlite3.connect(db.db_path) as writer:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        tid = new_task(db)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE tasks SET status='paused' WHERE task_id=?", (tid,))
        assert db.get_task(tid)["status"] == "pending"
        writer.commit()
        assert Path(db.db_path + "-wal").exists()
        target = tmp_path / "backup.db"
        backup_database(Path(db.db_path), target)
        backup_database(Path(db.db_path), target)
        with sqlite3.connect(target) as restored:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert restored.execute("SELECT status FROM tasks").fetchone()[0] == "paused"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("UPLOAD_PATH", str(tmp_path / "uploads"))
    monkeypatch.setenv("OUTPUT_PATH", str(tmp_path / "output"))
    monkeypatch.setenv("REDIS_QUEUE_ENABLED", "false")
    monkeypatch.setenv("JWT_SECRET_KEY", "phase1-test-only-secret-not-for-deployment")
    monkeypatch.setenv("ADMIN_PASSWORD", "phase1-test-only-password")
    monkeypatch.setattr(task_db, "get_redis_queue", lambda: None)
    import api_server

    monkeypatch.setattr(api_server, "db", task_db.TaskDB(str(tmp_path / "api.db")))
    monkeypatch.setattr(api_server, "UPLOAD_DIR", tmp_path / "uploads")
    api_server.UPLOAD_DIR.mkdir(exist_ok=True)
    monkeypatch.setattr(api_server, "OUTPUT_DIR", tmp_path / "output")
    api_server.OUTPUT_DIR.mkdir(exist_ok=True)
    return api_server


def test_openapi_aliases_and_sync_handlers(api):
    from fastapi.testclient import TestClient
    from auth.dependencies import get_current_user_from_apikey, get_current_user_from_token

    with TestClient(api.app) as client:
        canonical = client.get("/api/v1/openapi.json").json()
        assert "/api/v1/tasks/submit" in canonical["paths"]
        for path in ["/openapi.json", "/api/openapi.json", "/v1/openapi.json"]:
            assert client.get(path).json() == canonical
        assert "/api/v1/openapi.json" in client.get("/docs").text
    assert not inspect.iscoroutinefunction(api.get_task_images)
    assert not inspect.iscoroutinefunction(api.get_task_status)
    assert not inspect.iscoroutinefunction(get_current_user_from_apikey)
    assert not inspect.iscoroutinefunction(get_current_user_from_token)


@pytest.mark.parametrize("use_rustfs", [None, True, False])
def test_upload_does_not_block_loop_and_preserves_options(api, monkeypatch, use_rustfs):
    from starlette.datastructures import UploadFile
    from types import SimpleNamespace
    from fastapi.params import Param, Form

    started = threading.Event()
    release = threading.Event()
    create = api.db.create_task

    def slow_create(**kwargs):
        started.set()
        assert release.wait(3), "event loop did not release blocking database call"
        return create(**kwargs)

    monkeypatch.setattr(api.db, "create_task", slow_create)
    kwargs = {
        name: p.default.default
        for name, p in inspect.signature(api.submit_task).parameters.items()
        if isinstance(p.default, (Param, Form))
    }
    kwargs.update(
        file=UploadFile(filename="image.png", file=io.BytesIO(b"upload-content")),
        current_user=SimpleNamespace(user_id="owner"),
        use_rustfs=use_rustfs,
    )

    async def exercise():
        task = asyncio.create_task(api.submit_task(**kwargs))
        try:
            for _ in range(300):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
        finally:
            release.set()
        return await task

    result = asyncio.run(exercise())
    saved = api.db.get_task(result["task_id"])
    assert json.loads(saved["options"])["use_rustfs"] is use_rustfs
    assert Path(saved["file_path"]).read_bytes() == b"upload-content"


def test_invalid_exif_rotation():
    img2pdf = pytest.importorskip("img2pdf")
    from PIL import Image

    img = Image.new("RGB", (10, 20), "white")
    exif = img.getexif()
    exif[274] = 0
    data = io.BytesIO()
    img.save(data, format="JPEG", exif=exif)
    with pytest.raises(img2pdf.ExifOrientationError):
        img2pdf.convert(data.getvalue())
    assert img2pdf.convert(data.getvalue(), rotation=img2pdf.Rotation.ifvalid).startswith(b"%PDF")


def test_image_handoff_and_source_download(api):
    from fastapi.testclient import TestClient
    from auth.dependencies import get_current_active_user
    from types import SimpleNamespace

    owner = SimpleNamespace(user_id="owner", has_permission=lambda p: False)
    other = SimpleNamespace(user_id="other", has_permission=lambda p: False)
    result = api.OUTPUT_DIR / "sample"
    images = result / "images"
    images.mkdir(parents=True)
    (images / "used.png").write_bytes(b"image-bytes")
    (images / "unused.png").write_bytes(b"unused")
    (result / "result.md").write_text("![used](images/used.png)")
    source = api.UPLOAD_DIR / "original.png"
    source.write_bytes(b"original-image")
    tid = api.db.create_task(
        file_name="original.png", file_path=str(source), user_id="owner", options={"use_rustfs": False}
    )
    with api.db.get_cursor() as c:
        c.execute("UPDATE tasks SET status='completed', result_path=? WHERE task_id=?", (str(result), tid))
    api.app.dependency_overrides[get_current_active_user] = lambda: owner
    try:
        with TestClient(api.app) as client:
            response = client.get(f"/api/v1/tasks/{tid}/images")
            assert response.status_code == 200
            image = response.json()["images"]
            assert [i["filename"] for i in image] == ["used.png"]
            assert client.get(image[0]["download_url"]).content == b"image-bytes"
            task = client.get(f"/api/v1/tasks/{tid}").json()
            assert client.get(task["source_url"]).content == b"original-image"
            assert client.get(f"/v1/tasks/{tid}/images").json() == response.json()
            api.app.dependency_overrides[get_current_active_user] = lambda: other
            assert client.get(f"/api/v1/tasks/{tid}/images").status_code == 403
    finally:
        api.app.dependency_overrides.clear()


def test_worker_split_preserves_size_cleanup_and_completed_count(db, tmp_path, monkeypatch):
    # Execute the real worker method without importing GPU/model dependencies.
    import os
    from types import SimpleNamespace
    from loguru import logger
    import utils.pdf_utils as pdf_utils

    tree = ast.parse((ROOT / "backend/litserve_worker.py").read_text())
    worker = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MinerUWorkerAPI")
    method = next(n for n in worker.body if isinstance(n, ast.FunctionDef) and n.name == "_should_split_pdf")
    namespace = {"Path": Path, "os": os, "logger": logger}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "<worker-split>", "exec"), namespace)
    pdf = tmp_path / "large.pdf"
    with pdf.open("wb") as f:
        f.truncate(21 * 1024 * 1024)
    monkeypatch.setenv("PDF_SPLIT_THRESHOLD_PAGES", "500")
    monkeypatch.setenv("PDF_SPLIT_CHUNK_SIZE", "500")
    monkeypatch.setenv("PDF_SPLIT_SIZE_MB", "20")
    monkeypatch.setenv("PDF_SPLIT_ENABLED", "true")
    monkeypatch.setattr(pdf_utils, "get_pdf_page_count", lambda path: 100)
    chunk_sizes = []

    def split(path, directory, chunk_size, tid):
        chunk_sizes.append(chunk_size)
        return [
            {"path": str(directory / f"{i}.pdf"), "start_page": i * 10 + 1, "end_page": i * 10 + 10, "page_count": 10}
            for i in range(10)
        ]

    monkeypatch.setattr(pdf_utils, "split_pdf_file", split)
    parent = new_task(db)
    old = db.create_child_task(parent, "old.pdf", "/old.pdf")

    def enqueue(tid, priority, data):
        # Simulate an immediately completed child while the creator is enqueueing.
        with db.get_cursor() as c:
            c.execute("UPDATE tasks SET child_completed=child_completed+1 WHERE task_id=?", (parent,))

    monkeypatch.setattr(db, "_enqueue_to_redis", enqueue)
    options = {"use_rustfs": False, "upload_images": True}
    assert namespace["_should_split_pdf"](
        SimpleNamespace(task_db=db, output_dir=tmp_path), parent, str(pdf), {"priority": 4, "user_id": "owner"}, options
    )
    assert 0 < chunk_sizes[0] < 500
    assert db.get_task(old) is None
    assert db.get_task(parent)["child_count"] == db.get_task(parent)["child_completed"] == 10
    children = db.get_task_with_children(parent)["children"]
    assert len(children) == 10
    assert all(json.loads(c["options"])["use_rustfs"] is False for c in children)
    assert options == {"use_rustfs": False, "upload_images": True}


def test_redis_queue_atomic_fifo_and_processing(db, monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    from redis_queue import RedisTaskQueue, RedisConfig

    queue = RedisTaskQueue(RedisConfig())
    queue._client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(task_db, "get_redis_queue", lambda: queue)
    first = new_task(db, priority=5)
    second = new_task(db, priority=5)
    score = queue.client.zscore(queue.config.queue_key, first)
    assert queue.enqueue(first, 5)
    assert queue.client.zscore(queue.config.queue_key, first) == score
    assert queue.dequeue("worker", timeout=0.01) == first
    assert queue.client.hexists(queue.config.processing_key, first)
    assert db.sync_pending_to_redis() == 1
    assert not queue.client.hexists(queue.config.processing_key, first)
    assert queue.get_queued_ids() == {first, second}
    queue.client.flushall()
    assert db.sync_pending_to_redis() == 2


def test_without_redis_package(tmp_path):
    import subprocess

    script = """
import sys
sys.modules['redis'] = None
from task_db import TaskDB
from redis_queue import get_redis_queue
assert get_redis_queue() is None
db = TaskDB(sys.argv[1])
tid = db.create_task('file.pdf', '/file.pdf')
assert db.get_next_task('worker')['task_id'] == tid
"""
    import os

    env = dict(os.environ, PYTHONPATH=str(ROOT / "backend"))
    subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "no-redis.db")],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_redis_unavailable_at_startup_can_recover(db, monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    import redis_queue

    monkeypatch.setenv("REDIS_QUEUE_ENABLED", "true")
    monkeypatch.setattr(redis_queue, "_queue_instance", None)
    monkeypatch.setattr(redis_queue.RedisTaskQueue, "is_available", lambda self: False)
    queue = redis_queue.get_redis_queue()
    assert queue is not None
    queue._client = fakeredis.FakeRedis(decode_responses=True)
    tid = new_task(db)
    monkeypatch.setattr(task_db, "get_redis_queue", redis_queue.get_redis_queue)
    assert db.sync_pending_to_redis() == 1
    assert queue.get_queued_ids() == {tid}
