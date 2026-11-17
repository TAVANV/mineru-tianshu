"""任务执行隔离：监督进程不导入引擎，独立解释器保留模型缓存并可被强制回收。"""

import ctypes
import importlib
import os
import signal
import socket
import subprocess
import sys
import time
from multiprocessing.connection import Connection
from multiprocessing.reduction import recv_handle, send_handle
from pathlib import Path

import psutil


class ExecutionCleanupError(RuntimeError):
    """尚未确认执行树退出，禁止复用容量。"""


def _positive_env(name, default):
    value = float(os.getenv(name, default))
    if not 0 < value <= 60:
        raise ValueError(f"{name} 必须大于 0 且不超过 60 秒")
    return value


def _launch(role):
    local, remote = socket.socketpair()
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), role, str(remote.fileno())],
            pass_fds=(remote.fileno(),),
            start_new_session=True,
        )
    except BaseException:
        local.close()
        raise
    finally:
        remote.close()
    return process, Connection(local.detach())


class TaskExecutor:
    """每个 LitServe worker 一个监督进程；引擎正常返回时不重新加载模型。"""

    def __init__(self, factory, config):
        self.poll_interval = _positive_env("TASK_EXECUTION_POLL_INTERVAL", "0.25")
        self.terminate_grace = _positive_env("TASK_EXECUTION_TERMINATE_GRACE", "2")
        self.process, self.connection = _launch("--guardian")
        self.connection.send({"factory": factory, "config": config, "grace": self.terminate_grace})

    def run(self, task, task_db, running):
        # 控制通道只发送归属，重试记录可能含数百 MB 的旧解析结果，不能堵住停止命令。
        claim = {key: task[key] for key in ("task_id", "execution_id", "worker_id")}
        self.connection.send({"kind": "run", "task": claim})
        send_handle(self.connection, task_db.execution_lock_fd(task["task_id"]), self.process.pid)
        stopping = False
        stop_deadline = None
        while True:
            if self.connection.poll(self.poll_interval):
                try:
                    message = self.connection.recv()
                except EOFError as error:
                    raise ExecutionCleanupError("监督进程退出，无法确认执行树已清理") from error
                if message["kind"] == "fatal":
                    raise ExecutionCleanupError(message["error"])
                if message["kind"] in ("done", "stopped"):
                    if stopping and message["kind"] != "stopped":
                        continue
                    return message.get("error")
            if not stopping and (not running() or not task_db.owns_execution(task)):
                self.connection.send({"kind": "stop"})
                stopping = True
                stop_deadline = time.monotonic() + self.terminate_grace + 10
            if stop_deadline is not None and time.monotonic() > stop_deadline:
                raise ExecutionCleanupError("执行进程退出尚未确认，停止领取新任务")
            if self.process.poll() is not None:
                raise ExecutionCleanupError("监督进程异常退出，停止领取新任务")

    def close(self):
        if self.process.poll() is None:
            try:
                self.connection.send({"kind": "close"})
                self.process.wait(timeout=self.terminate_grace + 10)
            except (BrokenPipeError, EOFError, subprocess.TimeoutExpired) as error:
                raise ExecutionCleanupError("监督进程未确认关闭") from error
        self.connection.close()
        if self.process.returncode:
            raise ExecutionCleanupError(f"监督进程退出码异常: {self.process.returncode}")


def _alive(process):
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _stop_tree(executor, grace):
    """只清理本监督进程的后代，包含另建 session 的 Office/FFmpeg 子进程。"""
    descendants = {}

    def collect():
        for process in psutil.Process().children(recursive=True):
            try:
                descendants[(process.pid, process.create_time())] = process
            except psutil.NoSuchProcess:
                pass

    collect()
    for process in descendants.values():
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_alive(p) for p in descendants.values()):
        time.sleep(0.02)
    deadline = time.monotonic() + 5
    while True:
        collect()
        for process in descendants.values():
            if _alive(process):
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    pass
        if executor is not None:
            executor.poll()
        # Linux subreaper 接管孤儿孙进程，必须 wait，不能只向其发送信号。
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break
            except ChildProcessError:
                break
        collect()
        # 孤儿可能在 waitpid 之后才交给 subreaper；僵尸仍须下一轮 wait，不能提前确认退出。
        if not any(p.is_running() for p in descendants.values()):
            if executor is not None:
                executor.wait(timeout=1)
            return
        if time.monotonic() >= deadline:
            raise ExecutionCleanupError("执行树收到 SIGKILL 后仍未退出，禁止复用执行容量")
        time.sleep(0.02)


def _guardian(connection):
    # 监督进程独立于 LitServe 的 daemon 标志，exec 启动的引擎不会继承 CUDA 运行状态。
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "无法启用执行子进程回收")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    config = connection.recv()
    executor = None
    engine = None
    task_fd = None
    active = False

    def stop():
        nonlocal executor, engine, task_fd, active
        _stop_tree(executor, config["grace"])
        if engine is not None:
            engine.close()
        if task_fd is not None:
            os.close(task_fd)
        executor, engine, task_fd, active = None, None, None, False

    try:
        while True:
            if connection.poll(0.02):
                message = connection.recv()
                if message["kind"] == "close":
                    return
                if message["kind"] == "stop":
                    stop()
                    connection.send({"kind": "stopped"})
                elif message["kind"] == "run":
                    if active:
                        raise RuntimeError("执行容量尚未释放")
                    if executor is not None and executor.poll() is not None:
                        stop()
                    task_fd = recv_handle(connection)
                    try:
                        if executor is None:
                            executor, engine = _launch("--engine")
                            engine.send(config)
                        engine.send(message["task"])
                        send_handle(engine, task_fd, executor.pid)
                    except (BrokenPipeError, EOFError, OSError) as error:
                        stop()
                        connection.send({"kind": "done", "error": f"执行进程接收任务失败: {error}"})
                        continue
                    active = True
            if engine is not None and engine.poll(0):
                try:
                    message = engine.recv()
                except EOFError:
                    message = None
                if message is not None:
                    os.close(task_fd)
                    task_fd = None
                    active = False
                    connection.send(message)
            if executor is not None and executor.poll() is not None:
                exitcode = executor.returncode
                was_active = active
                stop()
                if was_active:
                    connection.send({"kind": "done", "error": f"执行进程异常退出: {exitcode}"})
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        connection.send({"kind": "fatal", "error": str(error)})
        raise
    finally:
        stop()
        connection.close()


def _engine(connection):
    config = connection.recv()
    module, name = config["factory"].split(":")
    worker = None
    while True:
        task = connection.recv()
        task_fd = recv_handle(connection)
        # macOS 的 send_handle 等待接收 ACK；必须先接收锁，监督进程才能继续处理停止命令。
        if worker is None:
            worker = getattr(importlib.import_module(module), name)(config["config"])
        try:
            with worker.task_db.execution_scope(task):
                worker._process_task(worker.task_db.get_task(task["task_id"]))
            error = None
        except Exception as exception:
            error = f"{type(exception).__name__}: {exception}"[:2000]
        finally:
            os.close(task_fd)
        connection.send({"kind": "done", "error": error})


if __name__ == "__main__":
    channel = Connection(int(sys.argv[2]))
    if sys.argv[1] == "--guardian":
        _guardian(channel)
    elif sys.argv[1] == "--engine":
        _engine(channel)
