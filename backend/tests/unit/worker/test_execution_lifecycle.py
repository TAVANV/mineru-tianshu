"""真实进程执行测试：仅替换底层引擎，保留任务循环、数据库与任务处理流水线。"""

import json
import multiprocessing
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from tests.fixtures import minimal_pdf
from tests.unit.worker.test_routing import load_worker_module, make_worker


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("等待执行生命周期事件超时")


def fake_engine(self, file_path, options):
    root = Path(options["test_root"])
    name = Path(file_path).stem
    started = root / f"{name}.started"
    previous_pid = int(started.read_text()) if started.exists() else None
    mode = options.get("test_mode", "ok")
    if mode == "regex":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with (root / f"{name}.attempts").open("a") as log:
        log.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "execution_id": self.task_db._execution[1],
                    "old_alive": previous_pid is not None and psutil.pid_exists(previous_pid),
                }
            )
            + "\n"
        )
    started.write_text(str(os.getpid()))
    if mode == "retry":
        mode = "ok" if previous_pid is not None else "block"
    if mode == "spawn":
        nested = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(0.02,))
        nested.start()
        nested.join(timeout=3)
        assert nested.exitcode == 0
    if mode == "exception":
        raise RuntimeError("测试引擎异常")
    if mode == "crash":
        os._exit(17)
    if mode == "descendant":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True)
        (root / f"{name}.descendant").write_text(str(child.pid))
    if mode == "regex":
        re.search(r"(a+)+$", "a" * 100_000 + "!")
    if mode in ("block", "descendant"):
        while not (root / f"{name}.release").exists():
            time.sleep(0.02)
    (root / f"{name}.returned").write_text(str(os.getpid()))
    result = Path(self.output_dir) / name
    result.mkdir(parents=True, exist_ok=True)
    (result / "result.md").write_text(f"# {name}")
    return {"result_path": str(result), "content": name, "json_content": [], "pdf_path": None}


def fake_merge(self, parent_id):
    options = json.loads(self.task_db.get_task(parent_id)["options"] or "{}")
    if options.get("test_merge_block"):
        root = Path(options["test_root"])
        (root / "merge.started").write_text(str(os.getpid()))
        while not (root / "merge.release").exists():
            time.sleep(0.02)
    type(self)._merge_parent_task_results(self, parent_id)


def make_executor(config):
    """子进程导入入口，重型依赖仅替换为轻量桩。"""
    from types import MethodType
    from unittest.mock import Mock
    from task_db import TaskDB

    if config.get("test_init_root"):
        marker = Path(config["test_init_root"]) / "init.started"
        if not marker.exists():
            marker.write_text(str(os.getpid()))
            while True:
                time.sleep(0.02)
    patch = pytest.MonkeyPatch()
    module = load_worker_module(patch)
    module.MINERU_PIPELINE_AVAILABLE = True
    worker = module.MinerUWorkerAPI.__new__(module.MinerUWorkerAPI)
    worker.output_dir = config["output_dir"]
    worker.task_db = TaskDB(config["db_path"])
    worker.worker_id = config["worker_id"]
    worker.mineru_vllm_api = None
    worker.watermark_handler = None
    worker._should_split_pdf = Mock(return_value=False)
    worker._should_split_zip = Mock(return_value=False)
    worker._enqueue_webhook = Mock()
    worker._process_with_mineru = MethodType(fake_engine, worker)
    worker._merge_parent_task_results = MethodType(fake_merge, worker)
    return worker


@pytest.fixture
def lifecycle(task_db, runtime_paths, monkeypatch, mocker):
    from types import MethodType

    module = load_worker_module(monkeypatch)
    module.MINERU_PIPELINE_AVAILABLE = True
    monkeypatch.setenv("TASK_EXECUTION_POLL_INTERVAL", "0.05")
    monkeypatch.setenv("TASK_EXECUTION_TERMINATE_GRACE", "0.15")
    root = runtime_paths["output_path"].parent
    workers = []

    def start(worker_id="worker-a", **config):
        worker = make_worker(module, mocker)
        worker.task_db = type(task_db)(task_db.db_path)
        worker.worker_id = worker_id
        worker.running = True
        worker.current_task_id = None
        worker.poll_interval = 0.02
        worker.output_dir = str(runtime_paths["output_path"])
        worker.mineru_vllm_api = None
        worker._process_with_mineru = MethodType(fake_engine, worker)
        worker._executor_factory = "tests.unit.worker.test_execution_lifecycle:make_executor"
        worker._executor_config = {
            "output_dir": worker.output_dir,
            "db_path": task_db.db_path,
            "worker_id": worker_id,
            **config,
        }
        thread = threading.Thread(target=worker._worker_loop)
        workers.append((worker, thread))
        thread.start()
        return worker

    def submit(name, mode="ok", **kwargs):
        source = runtime_paths["upload_path"] / f"{name}.pdf"
        source.write_bytes(minimal_pdf())
        return task_db.create_task(
            source.name,
            str(source),
            backend="pipeline",
            options={"test_root": str(root), "test_mode": mode},
            **kwargs,
        )

    yield start, submit, root
    for worker, _ in workers:
        worker.running = False
    for source in runtime_paths["upload_path"].glob("*.pdf"):
        (root / f"{source.stem}.release").touch()
    for worker, thread in workers:
        thread.join(timeout=10)
        assert not thread.is_alive(), f"worker 未退出: {worker.worker_id}"


@pytest.mark.parametrize("mode", ["block", "regex", "descendant"])
def test_cancel_stops_execution_and_single_worker_runs_next(lifecycle, task_db, mode, record_property):
    start, submit, root = lifecycle
    task_a = submit("a", mode, priority=10)
    task_b = submit("b")
    worker = start()
    wait_for(lambda: (root / "a.started").exists())
    pid = int((root / "a.started").read_text())
    descendant_pid = None
    if mode == "descendant":
        wait_for(lambda: (root / "a.descendant").exists())
        descendant_pid = int((root / "a.descendant").read_text())
    cancelled_at = time.monotonic()
    assert task_db.cancel_task(task_a)
    wait_for(lambda: not psutil.pid_exists(pid), timeout=3)
    if descendant_pid is not None:
        wait_for(lambda: not psutil.pid_exists(descendant_pid), timeout=3)
    record_property("reclamation_seconds", round(time.monotonic() - cancelled_at, 3))
    wait_for(lambda: task_db.get_task(task_b)["status"] == "completed", timeout=6)
    assert time.monotonic() - cancelled_at < 6
    assert not psutil.pid_exists(pid)
    if descendant_pid is not None:
        wait_for(lambda: not psutil.pid_exists(descendant_pid))
    assert not (root / "a.returned").exists()
    assert task_db.get_task(task_a)["status"] == "cancelled"
    wait_for(lambda: worker.current_task_id is None)


def test_timeout_stops_old_execution_then_requeues_without_overlap(lifecycle, task_db):
    start, submit, root = lifecycle
    task_a = submit("a", "retry", priority=10)
    task_b = submit("b")
    start()
    wait_for(lambda: (root / "a.started").exists())
    with task_db.get_cursor() as cursor:
        cursor.execute("UPDATE tasks SET started_at = datetime('now', '-120 minutes') WHERE task_id = ?", (task_a,))
    assert task_db.reset_stale_tasks(60, max_retries=1)["reset_count"] == 1
    wait_for(lambda: task_db.get_task(task_a)["status"] == "completed")
    wait_for(lambda: task_db.get_task(task_b)["status"] == "completed")
    attempts = [json.loads(line) for line in (root / "a.attempts").read_text().splitlines()]
    assert len(attempts) == 2
    assert attempts[0]["pid"] != attempts[1]["pid"]
    assert attempts[0]["execution_id"] != attempts[1]["execution_id"]
    assert not attempts[1]["old_alive"]
    assert task_db.get_task(task_a)["stale_reset_count"] == 1


def test_exhausted_timeout_frees_slot_and_runs_next(lifecycle, task_db):
    start, submit, root = lifecycle
    task_a = submit("a", "block", priority=10)
    task_b = submit("b")
    start()
    wait_for(lambda: (root / "a.started").exists())
    pid = int((root / "a.started").read_text())
    with task_db.get_cursor() as cursor:
        cursor.execute("UPDATE tasks SET started_at = datetime('now', '-120 minutes') WHERE task_id = ?", (task_a,))
    assert task_db.reset_stale_tasks(60, max_retries=0)["failed_count"] == 1
    wait_for(lambda: task_db.get_task(task_b)["status"] == "completed")
    assert not psutil.pid_exists(pid)
    assert not (root / "a.returned").exists()
    assert task_db.get_task(task_a)["status"] == "failed"


@pytest.mark.parametrize("mode", ["exception", "crash"])
def test_exception_or_executor_exit_does_not_strand_next_task(lifecycle, task_db, mode):
    start, submit, root = lifecycle
    task_a = submit("a", mode, priority=10)
    task_b = submit("b")
    start()
    wait_for(lambda: task_db.get_task(task_b)["status"] == "completed")
    assert task_db.get_task(task_a)["status"] == "failed"
    a_pid = int((root / "a.started").read_text())
    b_pid = int((root / "b.started").read_text())
    assert (a_pid == b_pid) == (mode == "exception")


def test_cancel_isolated_from_other_worker_and_task(lifecycle, task_db):
    start, submit, root = lifecycle
    task_a = submit("a", "block", priority=10)
    start("worker-a")
    wait_for(lambda: (root / "a.started").exists())
    task_c = submit("c", "block", priority=10)
    start("worker-c")
    wait_for(lambda: (root / "c.started").exists())
    c_pid = int((root / "c.started").read_text())
    task_b = submit("b")
    assert task_db.cancel_task(task_a)
    wait_for(lambda: task_db.get_task(task_b)["status"] == "completed")
    assert psutil.pid_exists(c_pid)
    assert task_db.get_task(task_c)["status"] == "processing"
    (root / "c.release").touch()
    wait_for(lambda: task_db.get_task(task_c)["status"] == "completed")


def test_immediate_cancel_retry_on_other_worker_waits_for_old_exit(lifecycle, task_db):
    start, submit, root = lifecycle
    task_a = submit("a", "retry", priority=10)
    start("worker-a")
    wait_for(lambda: (root / "a.started").exists())
    old = task_db.get_task(task_a)
    assert task_db.cancel_task(task_a)
    assert task_db.retry_task(task_a)
    start("worker-b")
    wait_for(lambda: task_db.get_task(task_a)["status"] == "completed")
    attempts = [json.loads(line) for line in (root / "a.attempts").read_text().splitlines()]
    assert len(attempts) == 2
    assert not attempts[1]["old_alive"]
    assert attempts[1]["execution_id"] != old["execution_id"]
    assert task_db.get_task(task_a)["error_message"] is None


def split_parent(task_db, submit, root, mode="ok", merge_block=False):
    parent_id = submit("archive")
    task_db.convert_to_parent_task(parent_id)
    with task_db.get_cursor() as cursor:
        cursor.execute(
            "UPDATE tasks SET options = ? WHERE task_id = ?",
            (json.dumps({"use_rustfs": False, "test_root": str(root), "test_merge_block": merge_block}), parent_id),
        )
    child_id = task_db.create_child_tasks_bulk(
        parent_id,
        [
            {
                "file_name": "part.pdf",
                "file_path": str(root / "uploads" / "part.pdf"),
                "options": {"test_root": str(root), "test_mode": mode, "chunk_info": {"index": 1}},
            }
        ],
    )[0]
    (root / "uploads" / "part.pdf").write_bytes(minimal_pdf())
    return parent_id, child_id


def test_parent_cancel_stops_child_and_parent_retry_merges_in_own_attempt(lifecycle, task_db):
    start, submit, root = lifecycle
    parent, child = split_parent(task_db, submit, root, mode="retry")
    start()
    wait_for(lambda: (root / "part.started").exists())
    old_pid = int((root / "part.started").read_text())
    assert task_db.cancel_task(parent)
    task_b = submit("b")
    wait_for(lambda: task_db.get_task(task_b)["status"] == "completed")
    assert not psutil.pid_exists(old_pid)
    assert task_db.get_task(child)["status"] == "cancelled"
    assert task_db.retry_task(parent)
    wait_for(lambda: task_db.get_task(parent)["status"] == "completed")
    assert task_db.get_task(child)["status"] == "completed"
    assert task_db.get_task(parent)["execution_id"] != task_db.get_task(child)["execution_id"]
    assert (root / "output" / "archive" / "result.md").read_text() == "# part"


@pytest.mark.parametrize("invalidate", ["cancel", "timeout"])
def test_parent_merge_has_cancellable_and_expiring_execution(lifecycle, task_db, invalidate):
    start, submit, root = lifecycle
    parent, child = split_parent(task_db, submit, root, merge_block=True)
    start()
    wait_for(lambda: (root / "merge.started").exists())
    pid = int((root / "merge.started").read_text())
    assert task_db.get_task(child)["status"] == "completed"
    if invalidate == "cancel":
        assert task_db.cancel_task(parent)
    else:
        with task_db.get_cursor() as cursor:
            cursor.execute("UPDATE tasks SET started_at = datetime('now', '-120 minutes') WHERE task_id = ?", (parent,))
        assert task_db.reset_stale_tasks(60, max_retries=0)["failed_count"] == 1
    task_b = submit("b")
    wait_for(lambda: task_db.get_task(task_b)["status"] == "completed")
    assert not psutil.pid_exists(pid)
    (root / "merge.release").touch()
    assert task_db.retry_task(parent)
    wait_for(lambda: task_db.get_task(parent)["status"] == "completed")
    assert len((root / "part.attempts").read_text().splitlines()) == 1


def test_shutdown_confirms_exit_and_requeues_interrupted_task(lifecycle, task_db):
    start, submit, root = lifecycle
    task_a = submit("a", "retry")
    worker = start()
    wait_for(lambda: (root / "a.started").exists())
    pid = int((root / "a.started").read_text())
    worker.teardown()
    assert not worker.running
    wait_for(lambda: not psutil.pid_exists(pid))
    wait_for(lambda: task_db.get_task(task_a)["status"] == "pending")
    start("replacement")
    wait_for(lambda: task_db.get_task(task_a)["status"] == "completed")


def test_supervisor_parent_exit_kills_engine_outside_its_gil(lifecycle, task_db, runtime_paths):
    start, submit, root = lifecycle
    task_a = submit("a", "retry")
    config = {"output_dir": str(runtime_paths["output_path"]), "db_path": task_db.db_path, "worker_id": "doomed"}
    program = """
import json, sys
from task_db import TaskDB
from task_execution import TaskExecutor
config = json.loads(sys.argv[1])
db = TaskDB(config['db_path'])
task = db.get_next_task('doomed', with_execution_lock=True)
executor = TaskExecutor('tests.unit.worker.test_execution_lifecycle:make_executor', config)
executor.run(task, db, lambda: True)
"""
    with (root / "parent.log").open("w") as log:
        parent = subprocess.Popen([sys.executable, "-c", program, json.dumps(config)], stdout=log, stderr=log)
        try:
            wait_for(lambda: (root / "a.started").exists())
            engine_pid = int((root / "a.started").read_text())
            guardian_pid = psutil.Process(parent.pid).children()[0].pid
            parent.kill()
            parent.wait(timeout=3)
            wait_for(lambda: not psutil.pid_exists(engine_pid), timeout=4)
            wait_for(lambda: not psutil.pid_exists(guardian_pid), timeout=4)
            with task_db.get_cursor() as cursor:
                cursor.execute(
                    "UPDATE tasks SET started_at = datetime('now', '-120 minutes') WHERE task_id = ?", (task_a,)
                )
            assert task_db.reset_stale_tasks(60, max_retries=1)["reset_count"] == 1
            start("replacement")
            wait_for(lambda: task_db.get_task(task_a)["status"] == "completed")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=3)


def test_cancel_during_engine_initialization_with_large_retry_data(lifecycle, task_db):
    start, submit, root = lifecycle
    task_a = submit("a", priority=10)
    with task_db.get_cursor() as cursor:
        cursor.execute("UPDATE tasks SET data = ? WHERE task_id = ?", ("x" * 2_000_000, task_a))
    task_b = submit("b")
    start(test_init_root=str(root))
    wait_for(lambda: (root / "init.started").exists())
    pid = int((root / "init.started").read_text())
    assert task_db.cancel_task(task_a)
    wait_for(lambda: task_db.get_task(task_b)["status"] == "completed")
    assert not psutil.pid_exists(pid)
    assert not (root / "a.started").exists()


@pytest.mark.parametrize("device", ["cpu", "cuda:2", "mps"])
def test_litserve_setup_only_prepares_supervision_without_loading_models(runtime_paths, monkeypatch, mocker, device):
    module = load_worker_module(monkeypatch)
    fail = mocker.Mock(side_effect=AssertionError("模型不能在 LitServe 监督进程初始化"))
    monkeypatch.setattr(module, "apply_cpu_threads_to_libs", fail)
    monkeypatch.setattr(module, "configure_onnxruntime_sessions", fail)
    monkeypatch.setattr(module.MinerUWorkerAPI, "_setup_engines", fail)
    worker = module.MinerUWorkerAPI(enable_worker_loop=False)
    worker.setup(device)
    assert worker._executor_config["device"] == device
    assert worker._executor_config["db_path"] == str(runtime_paths["database_path"].resolve())
    assert worker.current_task_id is None
    fail.assert_not_called()


def run_daemon_worker(config):
    worker = make_executor(config)
    worker.running = True
    worker.current_task_id = None
    worker.poll_interval = 0.02
    worker._executor_factory = "tests.unit.worker.test_execution_lifecycle:make_executor"
    worker._executor_config = config
    worker._worker_loop()


def test_daemon_litserve_worker_uses_executor_that_can_spawn_children(lifecycle, task_db, runtime_paths):
    _, submit, root = lifecycle
    task_a = submit("a", "block", priority=10)
    task_b = submit("b", "spawn")
    config = {"output_dir": str(runtime_paths["output_path"]), "db_path": task_db.db_path, "worker_id": "daemon"}
    parent = multiprocessing.get_context("spawn").Process(target=run_daemon_worker, args=(config,), daemon=True)
    parent.start()
    engine_pid = guardian_pid = None
    try:
        wait_for(lambda: (root / "a.started").exists())
        assert task_db.cancel_task(task_a)
        wait_for(lambda: task_db.get_task(task_b)["status"] == "completed")
        engine_pid = int((root / "b.started").read_text())
        guardian_pid = psutil.Process(parent.pid).children()[0].pid
    finally:
        parent.terminate()
        parent.join(timeout=3)
        assert not parent.is_alive()
        if engine_pid is not None:
            wait_for(lambda: not psutil.pid_exists(engine_pid))
        if guardian_pid is not None:
            wait_for(lambda: not psutil.pid_exists(guardian_pid))
