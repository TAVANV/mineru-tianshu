import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth.models import Permission
from task_db import TaskDB
from tests.integration.test_api_safety import load_api_module
from tests.unit.auth.test_auth_db import create_user


@pytest.mark.parametrize("storage,expected", [(None, None), ("false", False), ("true", True)])
def test_submit_preserves_storage_option(runtime_paths, auth_db, storage, expected):
    user = create_user(auth_db)
    key = auth_db.create_api_key(user.user_id, "submit")
    api = load_api_module()
    with TestClient(api.app) as client:
        response = client.post(
            "/api/v1/tasks/submit",
            files={"file": ("sample.txt", b"hello")},
            data={} if storage is None else {"use_rustfs": storage},
            headers={"X-API-Key": key["api_key"]},
        )
    assert response.status_code == 200
    task = api.db.get_task(response.json()["task_id"])
    assert json.loads(task["options"])["use_rustfs"] is expected


def test_image_handoff_download_and_ownership(runtime_paths, auth_db):
    owner = create_user(auth_db)
    stranger = create_user(auth_db, username="stranger")
    key = auth_db.create_api_key(owner.user_id, "reader", scopes=[Permission.TASK_VIEW_OWN.value])
    denied_key = auth_db.create_api_key(owner.user_id, "submit-only", scopes=[Permission.TASK_SUBMIT.value])
    other_key = auth_db.create_api_key(stranger.user_id, "other")
    api = load_api_module()
    result_dir = runtime_paths["output_path"] / "report"
    image_dir = result_dir / "images"
    image_dir.mkdir(parents=True)
    (image_dir / "image (1).png").write_bytes(b"test image")
    (image_dir / "unused.png").write_bytes(b"unused")
    outside = runtime_paths["output_path"].parent / "private.png"
    outside.write_bytes(b"private")
    (image_dir / "escape.png").symlink_to(outside)
    (result_dir / "result.md").write_text(
        "See https://example.com\n![a \\[bracket\\]](<images/image (1).png>)\n![escape](images/escape.png)"
    )
    task_id = api.db.create_task("report.pdf", "/synthetic/report.pdf", user_id=owner.user_id)
    with api.db.get_cursor() as cursor:
        cursor.execute("UPDATE tasks SET status='completed', result_path=? WHERE task_id=?", (str(result_dir), task_id))

    with TestClient(api.app) as client:
        headers = {"X-API-Key": key["api_key"]}
        listing = client.get(f"/api/v1/tasks/{task_id}/images", headers=headers)
        assert listing.status_code == 200
        assert [i["filename"] for i in listing.json()["images"]] == ["image (1).png"]
        download_url = listing.json()["images"][0]["download_url"]
        assert client.get(download_url, headers=headers).content == b"test image"
        assert client.get(download_url).status_code == 401
        for rejected in (denied_key, other_key):
            rejected_headers = {"X-API-Key": rejected["api_key"]}
            assert client.get(download_url, headers=rejected_headers).status_code == 403
            assert client.get(f"/api/v1/tasks/{task_id}/images", headers=rejected_headers).status_code == 403
        assert client.get("/api/v1/files/output/report/images/escape.png", headers=headers).status_code == 404
        rendered = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert download_url in rendered.json()["data"]["content"]


@pytest.mark.parametrize("delivery_first", [False, True])
def test_legacy_database_upgrade_preserves_data_and_archives_old_protocol(runtime_paths, delivery_first):
    from auth.auth_db import AuthDB
    from webhook import delivery

    schema = Path(__file__).parents[1] / "fixtures" / "legacy_20260915.sql"
    with sqlite3.connect(runtime_paths["database_path"]) as conn:
        conn.executescript(schema.read_text())
        conn.execute(
            "INSERT INTO tasks(task_id,file_name,status,options) VALUES ('legacy-task','old.pdf','pending','{}')"
        )
        conn.execute("INSERT INTO task_webhooks VALUES ('legacy-task', '{}')")
        conn.execute("UPDATE tasks SET status='completed' WHERE task_id='legacy-task'")
        old_delivery = conn.execute("SELECT id,payload FROM webhook_deliveries").fetchone()
        conn.execute(
            "INSERT INTO users(user_id,username,email,role) VALUES ('old-user','legacy','old@example.com','user')"
        )
    if delivery_first:
        with sqlite3.connect(runtime_paths["database_path"]) as conn:
            delivery._ensure_table(conn)
    db = TaskDB(runtime_paths["database_path"])
    auth = AuthDB(str(runtime_paths["database_path"]))
    assert auth.get_user_by_id("old-user").username == "legacy"
    assert db.get_task("legacy-task")["status"] == "completed"
    assert db.get_task("legacy-task")["stale_reset_count"] == 0
    with db.get_cursor() as cursor:
        assert (
            tuple(cursor.execute("SELECT id,payload FROM webhook_deliveries_legacy_20260915").fetchone())
            == old_delivery
        )
        assert not cursor.execute("SELECT name FROM sqlite_master WHERE name='task_webhook_terminal'").fetchone()
        cursor.execute("UPDATE tasks SET status='failed' WHERE task_id='legacy-task'")
    delivery.insert_delivery("legacy-task", "task.failed", "https://example.com/hook", {"task_id": "legacy-task"})
    TaskDB(runtime_paths["database_path"])
    with sqlite3.connect(runtime_paths["database_path"]) as conn:
        assert conn.execute("SELECT count(*) FROM webhook_deliveries").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM webhook_deliveries_legacy_20260915").fetchone()[0] == 1
