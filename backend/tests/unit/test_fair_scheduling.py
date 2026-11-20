from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest

import task_db as task_db_module
from redis_queue import RedisTaskQueue
from task_db import TaskDB


@pytest.fixture(autouse=True)
def enable_fair_scheduling(monkeypatch):
    monkeypatch.setenv("FAIR_SCHEDULING_ENABLED", "true")


def _create_task(db, user_id, *, priority=0, created_at="2025-01-01 00:00:00"):
    task_id = db.create_task("document.pdf", "/tmp/document.pdf", user_id=user_id, priority=priority)
    with db.get_cursor() as cursor:
        cursor.execute("UPDATE tasks SET created_at = ? WHERE task_id = ?", (created_at, task_id))
    return task_id


def test_default_fair_scheduling_shares_five_slots(task_db, monkeypatch):
    monkeypatch.delenv("FAIR_SCHEDULING_ENABLED", raising=False)
    db = TaskDB(task_db.db_path)
    for _ in range(100):
        _create_task(db, "A")
    for user in "BCDE":
        _create_task(db, user, created_at="2025-01-02 00:00:00")

    claimed = [db.get_next_task(f"worker-{i}") for i in range(5)]

    assert claimed[0]["user_id"] == "A"
    assert {task["user_id"] for task in claimed} == set("ABCDE")

    for task in claimed[1:]:
        assert db.update_task_status(task["task_id"], "completed", worker_id=task["worker_id"])

    assert [db.get_next_task(f"worker-{i}")["user_id"] for i in range(1, 5)] == ["A"] * 4
    assert db.get_queue_stats()["processing"] == 5


def test_single_user_can_fill_every_slot(task_db):
    for _ in range(6):
        _create_task(task_db, "A")

    assert [task_db.get_next_task(f"worker-{i}")["user_id"] for i in range(5)] == ["A"] * 5
    assert task_db.get_queue_stats()["pending"] == 1


def test_higher_priority_still_wins_over_lower_occupancy(task_db):
    _create_task(task_db, "A")
    task_db.get_next_task("worker-0")
    _create_task(task_db, "B", priority=1)
    high_priority = _create_task(task_db, "A", priority=2, created_at="2025-01-02 00:00:00")

    assert task_db.get_next_task("worker-1")["task_id"] == high_priority


def test_tasks_with_null_user_id_share_occupancy(task_db):
    active_anonymous = _create_task(task_db, None)
    pending_anonymous = _create_task(task_db, None, created_at="2025-01-02 00:00:00")
    empty_user_id = _create_task(task_db, "", created_at="2025-01-03 00:00:00")

    assert task_db.get_next_task("worker-0")["task_id"] == active_anonymous
    assert task_db.get_next_task("worker-1")["task_id"] == empty_user_id
    assert task_db.get_next_task("worker-2")["task_id"] == pending_anonymous


@pytest.mark.parametrize("waiting_for_children", [True, False])
def test_only_executing_split_parent_counts_toward_occupancy(task_db, waiting_for_children):
    parent_id = _create_task(task_db, "A")
    parent = task_db.get_next_task("worker-0", with_execution_lock=True)
    try:
        task_db.convert_to_parent_task(parent_id)
        [child_id] = task_db.create_child_tasks_bulk(
            parent_id, [{"file_name": "child.pdf", "file_path": "/tmp/child.pdf"}], user_id="A"
        )
        with task_db.get_cursor() as cursor:
            cursor.execute("UPDATE tasks SET created_at = '2025-01-01 00:00:00' WHERE task_id = ?", (child_id,))
        other_id = _create_task(task_db, "B", created_at="2025-01-02 00:00:00")

        if waiting_for_children:
            task_db.finish_execution(parent)
        claimed = task_db.get_next_task("worker-1")

        assert claimed["task_id"] == (child_id if waiting_for_children else other_id)
    finally:
        task_db.release_execution_lock(parent_id)


def test_disabling_fairness_restores_priority_and_creation_order(runtime_paths, monkeypatch):
    monkeypatch.setenv("FAIR_SCHEDULING_ENABLED", "false")
    db = TaskDB(runtime_paths["database_path"])
    later_b = _create_task(db, "B", created_at="2025-01-03 00:00:00")
    earlier_a = _create_task(db, "A", created_at="2025-01-02 00:00:00")
    oldest_a = _create_task(db, "A")
    high_priority = _create_task(db, "A", priority=1, created_at="2025-01-04 00:00:00")

    assert [db.get_next_task(f"worker-{i}")["task_id"] for i in range(4)] == [
        high_priority,
        oldest_a,
        earlier_a,
        later_b,
    ]


def test_concurrent_workers_claim_distinct_tasks_and_share_capacity(task_db):
    for _ in range(10):
        _create_task(task_db, "A")
    for user in "BCDE":
        _create_task(task_db, user, created_at="2025-01-02 00:00:00")

    workers = [TaskDB(task_db.db_path) for _ in range(5)]
    ready = Barrier(len(workers))

    def claim(index, db):
        ready.wait(timeout=10)
        task = db.get_next_task(f"worker-{index}", with_execution_lock=True)
        if task is not None:
            db.release_execution_lock(task["task_id"])
        return task

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        claims = list(pool.map(claim, range(len(workers)), workers))

    assert len({task["task_id"] for task in claims}) == 5
    assert {task["user_id"] for task in claims} == set("ABCDE")
    assert all(task["execution_id"] for task in claims)
    assert task_db.get_queue_stats()["processing"] == 5


def test_fair_mode_discards_old_redis_memberships_and_avoids_queue_writes(task_db, monkeypatch):
    monkeypatch.setenv("FAIR_SCHEDULING_ENABLED", "true")
    monkeypatch.setenv("REDIS_QUEUE_ENABLED", "true")
    monkeypatch.setenv("REDIS_QUEUE_KEY", "test:queue")
    monkeypatch.setenv("REDIS_PROCESSING_KEY", "test:processing")
    cached_task_id = _create_task(task_db, "A")
    db = TaskDB(task_db.db_path)
    client = Mock()
    client.delete.return_value = 2
    monkeypatch.setattr(RedisTaskQueue, "client", client)
    redis_queue = Mock()
    monkeypatch.setattr(task_db_module, "get_redis_queue", redis_queue)

    new_task_id = _create_task(db, "B", created_at="2025-01-02 00:00:00")
    assert db.pause_task(new_task_id)
    assert db.resume_task(new_task_id)
    assert db.sync_pending_to_redis() == 0
    assert db.get_next_task("worker-0")["task_id"] == cached_task_id
    assert db.update_task_status(cached_task_id, "completed", worker_id="worker-0")
    assert db.get_next_task("worker-0")["task_id"] == new_task_id
    assert db.get_next_task("worker-1") is None

    client.delete.assert_called_once_with("test:queue", "test:processing")
    redis_queue.assert_not_called()


def test_redis_cleanup_retries_without_losing_sqlite_claims(task_db, monkeypatch):
    monkeypatch.setenv("FAIR_SCHEDULING_ENABLED", "true")
    monkeypatch.setenv("REDIS_QUEUE_ENABLED", "true")
    db = TaskDB(task_db.db_path)
    client = Mock()
    client.delete.side_effect = [ConnectionError("Redis 暂时不可用"), 2]
    monkeypatch.setattr(RedisTaskQueue, "client", client)
    now = 0.0
    monkeypatch.setattr(task_db_module, "monotonic", lambda: now)
    task_id = _create_task(db, "A")

    assert db.get_next_task("worker-0")["task_id"] == task_id
    assert db.sync_pending_to_redis() == 0
    assert client.delete.call_count == 1

    now = 61.0
    assert db.get_next_task("worker-1") is None
    assert client.delete.call_count == 2


def test_disabling_fair_mode_restores_missing_redis_tasks_in_original_order(task_db, monkeypatch):
    older = _create_task(task_db, "A", priority=2)
    monkeypatch.setenv("FAIR_SCHEDULING_ENABLED", "false")
    db = TaskDB(task_db.db_path)
    client = Mock()
    client.pipeline.return_value = client
    monkeypatch.setattr(RedisTaskQueue, "client", client)
    queue = RedisTaskQueue()
    monkeypatch.setattr(task_db_module, "get_redis_queue", lambda: queue)
    newer = _create_task(db, "B", priority=2, created_at="2025-01-02 00:00:00")
    client.zrange.return_value = [newer]

    def dequeue(key, *, timeout):
        client.zadd.assert_called_with(key, {older: -2 * 1e10 + 1735689600}, nx=True)
        scores = {task_id: score for call in client.zadd.call_args_list for task_id, score in call.args[1].items()}
        next_task_id = min(scores, key=scores.__getitem__)
        return key, next_task_id, scores[next_task_id]

    client.bzpopmin.side_effect = dequeue

    assert db.get_next_task("worker-0")["task_id"] == older
    assert db.get_task(newer)["status"] == "pending"
