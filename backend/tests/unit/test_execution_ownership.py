"""执行令牌与内核锁分别约束迟到数据库写入和同一输出目录的并行执行。"""

import sqlite3
from unittest.mock import Mock

import pytest

from task_db import ExecutionRevoked, TaskDB


@pytest.mark.parametrize("invalidate", ["cancel", "retry", "timeout_retry", "timeout_fail"])
def test_revoked_attempt_cannot_write_results_or_create_children(task_db, invalidate):
    task_id = task_db.create_task("document.pdf", "/tmp/document.pdf")
    old = task_db.get_next_task("worker-a", with_execution_lock=True)
    scoped_db = TaskDB(task_db.db_path)
    if invalidate == "cancel":
        task_db.cancel_task(task_id)
    elif invalidate == "retry":
        task_db.retry_task(task_id)
    else:
        with task_db.get_cursor() as cursor:
            cursor.execute(
                "UPDATE tasks SET started_at = datetime('now', '-120 minutes') WHERE task_id = ?", (task_id,)
            )
        task_db.reset_stale_tasks(60, max_retries=0 if invalidate == "timeout_fail" else 1)
    before = task_db.get_task(task_id)
    try:
        with scoped_db.execution_scope(old):
            for operation in (
                lambda: scoped_db.update_task_status(task_id, "completed", result_path="/tmp/late"),
                lambda: scoped_db.update_task_status(task_id, "failed", error_message="late failure"),
                lambda: scoped_db.convert_to_parent_task(task_id, 9),
                lambda: scoped_db.create_child_tasks_bulk(task_id, [{"file_name": "late", "file_path": "/tmp/late"}]),
            ):
                with pytest.raises(ExecutionRevoked):
                    operation()
        assert task_db.get_task(task_id) == before
        assert task_db.get_child_tasks(task_id) == []
    finally:
        task_db.release_execution_lock(task_id)


def test_same_worker_retry_gets_new_token_and_rejects_old_attempt(task_db):
    task_id = task_db.create_task("document.pdf", "/tmp/document.pdf")
    old = task_db.get_next_task("worker-a", with_execution_lock=True)
    assert task_db.cancel_task(task_id)
    assert task_db.retry_task(task_id)
    other = TaskDB(task_db.db_path)
    assert other.get_next_task("worker-b", with_execution_lock=True) is None
    task_db.release_execution_lock(task_id)
    new = other.get_next_task("worker-a", with_execution_lock=True)
    try:
        assert new["execution_id"] != old["execution_id"]
        with task_db.execution_scope(old):
            with pytest.raises(ExecutionRevoked):
                task_db.update_task_status(task_id, "completed", result_path="/tmp/old")
        with other.execution_scope(new):
            assert other.update_task_status(task_id, "completed", result_path="/tmp/new")
        assert other.get_task(task_id)["result_path"] == "/tmp/new"
    finally:
        other.release_execution_lock(task_id)


def test_redis_claim_obeys_execution_lock_and_records_token(task_db, monkeypatch):
    import task_db as module

    task_id = task_db.create_task("document.pdf", "/tmp/document.pdf")
    old = task_db.get_next_task("worker-a", with_execution_lock=True)
    task_db.retry_task(task_id)
    queue = Mock()
    queue.dequeue.return_value = task_id
    monkeypatch.setattr(module, "get_redis_queue", lambda: queue)
    monkeypatch.setattr(module, "REDIS_QUEUE_AVAILABLE", True)
    other = TaskDB(task_db.db_path)
    assert other._get_next_task_redis("worker-b", with_execution_lock=True) is None
    queue.fail.assert_called_with(task_id, "worker-b", requeue=True)
    task_db.release_execution_lock(task_id)
    new = other._get_next_task_redis("worker-b", with_execution_lock=True)
    try:
        assert new["execution_id"] != old["execution_id"]
        assert new["worker_id"] == "worker-b"
        assert other.owns_execution(new)
    finally:
        other.release_execution_lock(task_id)


def test_execution_column_migrates_existing_database(task_db):
    with sqlite3.connect(task_db.db_path) as connection:
        connection.execute("ALTER TABLE tasks DROP COLUMN execution_id")
    upgraded = TaskDB(task_db.db_path)
    task_id = upgraded.create_task("document.pdf", "/tmp/document.pdf")
    assert upgraded.get_task(task_id)["execution_id"] is None
