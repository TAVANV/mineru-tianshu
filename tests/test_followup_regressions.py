"""Interleaved recovery/cancellation, old callbacks after retry, and ZIP image names."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading

import pytest

from test_phase2 import hook_task
from utils.archive_utils import merge_archive_results
from utils.image_references import image_filenames


@pytest.mark.parametrize("first", ["recovery", "cancel"])
def test_recovery_and_cancel_are_serialized_before_reading(db, monkeypatch, first):
    root = hook_task(db)
    db.convert_to_parent_task(root)
    child = db.create_child_task(root, "chunk.pdf", "/chunk.pdf")
    with db.get_cursor() as c:
        c.execute("UPDATE tasks SET started_at=datetime('now','-121 minutes') WHERE task_id=?", (root,))
        c.execute("UPDATE tasks SET status='completed' WHERE task_id=?", (child,))

    at_read, release, second_attempted = threading.Event(), threading.Event(), threading.Event()
    second_entered = threading.Event()
    actor = threading.local()
    original = db.get_cursor

    class Cursor:
        def __init__(self, cursor):
            self.cursor, self.sql = cursor, ""

        def execute(self, sql, params=()):
            self.sql = sql
            if sql == "BEGIN IMMEDIATE" and actor.name != first:
                second_attempted.set()
            self.cursor.execute(sql, params)
            if sql == "BEGIN IMMEDIATE" and actor.name != first:
                second_entered.set()
            return self

        def fetchall(self):
            rows = self.cursor.fetchall()
            if actor.name == first == "recovery" and "SELECT status FROM tasks WHERE parent_task_id" in self.sql:
                at_read.set()
                assert release.wait(5)
            return rows

        def fetchone(self):
            row = self.cursor.fetchone()
            if actor.name == first == "cancel" and self.sql == "SELECT * FROM tasks WHERE task_id=?":
                at_read.set()
                assert release.wait(5)
            return row

        def __getattr__(self, key):
            return getattr(self.cursor, key)

    @contextmanager
    def monitored_cursor():
        with original() as cursor:
            yield Cursor(cursor)

    monkeypatch.setattr(db, "get_cursor", monitored_cursor)

    def run(name):
        actor.name = name
        return db.reap_stale_parent_tasks(120) if name == "recovery" else db.cancel_task(root)

    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(run, first)
        try:
            assert at_read.wait(5), "First operation did not reach its read barrier"
            other = pool.submit(run, "cancel" if first == "recovery" else "recovery")
            assert second_attempted.wait(5), "Second operation did not attempt a write transaction"
            assert not second_entered.wait(0.1), "Second writer acquired the lock while the first was deciding"
            assert not other.done(), "Second operation wrote while the first operation was deciding"
        finally:
            release.set()
        initial_result, other_result = initial.result(timeout=5), other.result(timeout=5)
    monkeypatch.setattr(db, "get_cursor", original)
    assert (initial_result, other_result) == ((1, True) if first == "recovery" else (True, 0))
    assert db.get_task(root)["status"] == "cancelled"
    assert (db.get_task(child) is None) == (first == "recovery")
    assert db.reap_stale_parent_tasks(0) == 0
    with db.get_cursor() as c:
        assert [r[0] for r in c.execute("SELECT task_status FROM webhook_deliveries")] == ["cancelled"]


def test_recovery_state_and_descendant_deletion_roll_back_together(db, monkeypatch):
    root = hook_task(db)
    db.convert_to_parent_task(root)
    child = db.create_child_task(root, "chunk.pdf", "/chunk.pdf")
    with db.get_cursor() as c:
        c.execute("UPDATE tasks SET started_at=datetime('now','-121 minutes') WHERE task_id=?", (root,))
        c.execute("UPDATE tasks SET status='completed' WHERE task_id=?", (child,))
    delete = db._delete_descendant_records

    def failed_delete(cursor, task_id, include_root=False):
        delete(cursor, task_id, include_root)
        raise RuntimeError("injected recovery failure")

    monkeypatch.setattr(db, "_delete_descendant_records", failed_delete)
    with pytest.raises(RuntimeError, match="injected recovery failure"):
        db.reap_stale_parent_tasks(120)
    assert db.get_task(root)["status"] == "processing"
    assert db.get_task(child)["status"] == "completed"


@pytest.mark.parametrize("nested", [False, True])
def test_accepted_old_failure_callback_cannot_cross_root_retry(db, tmp_path, monkeypatch, nested):
    root = hook_task(db)
    db.convert_to_parent_task(root)
    parent = root
    old_ids, old_paths = [], []
    if nested:
        parent = db.create_child_task(root, "member.pdf", "/member.pdf")
        db.convert_to_parent_task(parent)
        old_ids.append(parent)
    for index in range(2):
        path = tmp_path / f"{index}.pdf"
        path.write_bytes(b"old source")
        old_paths.append(path)
        old_ids.append(db.create_child_task(parent, path.name, str(path)))
    late, sibling = old_ids[-2:]
    assert db.get_next_task("old-worker")["task_id"] == late
    assert db.get_next_task("sibling-worker")["task_id"] == sibling
    assert db.update_task_status(late, "failed", worker_id="old-worker")
    assert db.update_task_status(sibling, "failed", worker_id="sibling-worker")
    db.on_child_task_failed(sibling, "first callback received")
    assert db.get_task(root)["status"] == "failed"

    def requeue(tid):
        assert tid == root
        assert not db.get_descendant_tasks(root), "Old tree still reachable when retry was published"
        assert all(not path.exists() for path in old_paths)

    monkeypatch.setattr(db, "_requeue_pending", requeue)
    assert db.retry_task(root)
    assert all(db.get_task(tid) is None for tid in old_ids)
    assert db.get_next_task("new-worker")["task_id"] == root
    db.on_child_task_failed(late, "accepted old failure delivered after retry")
    assert db.get_task(root)["status"] == "processing"
    assert db.get_task(root)["retry_count"] == 1
    # A real failure in the new tree must still propagate and notify normally.
    db.convert_to_parent_task(root)
    fresh = db.create_child_task(root, "new.pdf", "/new.pdf")
    assert db.get_next_task("fresh-worker")["task_id"] == fresh
    assert db.update_task_status(fresh, "failed", worker_id="fresh-worker")
    db.on_child_task_failed(fresh, "new attempt failure")
    with db.get_cursor() as c:
        events = c.execute("SELECT retry_count,task_status FROM webhook_deliveries ORDER BY retry_count").fetchall()
        assert [(r[0], r[1]) for r in events] == [(0, "failed"), (1, "failed")]


@pytest.mark.parametrize(
    ("filename", "syntax"),
    [
        ("figure(1).png", "![figure](images/figure(1).png)"),
        ("figure 1.png", '<img src="images/figure 1.png" alt="figure">'),
        ("figure 1.png", "![figure](<images/figure 1.png>)"),
        ("figure).png", r"![figure](images/figure\).png)"),
    ],
)
def test_zip_rename_uses_shared_reference_handlers(tmp_path, filename, syntax):
    children = []
    # Two members with equal image names but distinct contents must remain distinct.
    for index in range(2):
        child = tmp_path / str(index)
        (child / "images").mkdir(parents=True)
        (child / "images" / filename).write_bytes(str(index).encode())
        body = f"Opening paragraph {index}.\n{syntax}\nClosing paragraph."
        (child / "result.md").write_text(body)
        (child / "result.json").write_text(json.dumps({"img_path": "images/" + filename, "content": body}))
        children.append(dict(task_id=str(index), file_name=f"{index}.md", result_path=str(child), options="{}"))
    output = tmp_path / "merged"
    merge_archive_results(children, output)
    documents = json.loads((output / "result.json").read_text())["documents"]
    expected = {f"{i}_{filename}" for i in range(2)}
    assert image_filenames((output / "result.md").read_text()) == expected
    for index, document in enumerate(documents):
        path = f"images/{index}_{filename}"
        assert document["data"]["img_path"] == path
        assert (output / path).read_bytes() == str(index).encode()
        for body in [document["content"], document["data"]["content"]]:
            assert "Opening paragraph" in body and "Closing paragraph" in body
            assert image_filenames(body) == {f"{index}_{filename}"}
