"""
MinerU Tianshu - Audit Log
审计日志

记录登录、登出、配置变更、任务删除等敏感操作，供管理员审计追溯。
所有写入均为 fire-and-forget：任何失败只记日志，绝不影响业务请求。
"""

import json
import os
import sqlite3
import queue
import threading
import time
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

from fastapi import Request
from loguru import logger


def _get_db_path() -> str:
    """获取数据库路径（与任务库、认证库共用同一个 SQLite 文件）"""
    db_path = os.getenv("DATABASE_PATH")
    if not db_path:
        project_root = Path(__file__).parent.parent.parent
        db_path = str(project_root / "data" / "db" / "mineru_tianshu.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return str(Path(db_path).resolve())


def _get_conn(db_path: str) -> sqlite3.Connection:
    """获取数据库连接，PRAGMA 设置与 task_db._get_conn 保持一致"""
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=0.1)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=100")
    conn.execute("PRAGMA synchronous=FULL")
    conn.row_factory = sqlite3.Row
    return conn


def _init_table(conn: sqlite3.Connection) -> None:
    """初始化审计日志表与索引（新表用 CREATE TABLE IF NOT EXISTS 即可）"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            user_id TEXT,
            username TEXT,
            action TEXT NOT NULL,
            resource_type TEXT,
            resource_id TEXT,
            detail TEXT,
            ip TEXT,
            user_agent TEXT,
            result TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs (created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs (user_id)")
    conn.commit()


def _extract_ip(request: Optional[Request]) -> Optional[str]:
    """提取客户端 IP，优先取 X-Forwarded-For 首段（反向代理场景）"""
    if request is None:
        return None
    if request.client:
        return request.client.host
    return None


_events = queue.Queue(maxsize=2048)
_writer_lock = threading.Lock()
_writer = None


def _drain():
    while True:
        first = _events.get()
        batch = [first]
        time.sleep(0.05)
        while len(batch) < 100:
            try:
                batch.append(_events.get_nowait())
            except queue.Empty:
                break
        try:
            # Group by database path (also makes isolated test databases safe).
            for path in {item[0] for item in batch}:
                conn = _get_conn(path)
                try:
                    _init_table(conn)
                    conn.executemany(
                        "INSERT INTO audit_logs (user_id, username, action, resource_type, resource_id, detail, ip, user_agent, result) VALUES (?,?,?,?,?,?,?,?,?)",
                        [item[1] for item in batch if item[0] == path],
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as e:
            logger.warning(f"Audit batch could not be saved: {type(e).__name__}")
        finally:
            for _ in batch:
                _events.task_done()


def record_audit(action, user=None, request=None, resource_type=None, resource_id=None, detail=None, result="success"):
    """Bounded, best-effort batching: audit failures never block business requests."""
    global _writer
    try:
        with _writer_lock:
            if _writer is None or not _writer.is_alive():
                _writer = threading.Thread(target=_drain, daemon=True, name="audit-writer")
                _writer.start()
        # Never accept credential values in audit details.
        safe_detail = {k: v for k, v in (detail or {}).items() if k in {"method", "status_code", "changed_keys"}}
        _events.put_nowait(
            (
                _get_db_path(),
                (
                    getattr(user, "user_id", None),
                    getattr(user, "username", None),
                    action,
                    resource_type,
                    resource_id,
                    json.dumps(safe_detail, ensure_ascii=False),
                    _extract_ip(request),
                    request.headers.get("User-Agent", "")[:512] if request else None,
                    result,
                ),
            )
        )
    except Exception:
        logger.warning("Audit event dropped; request continues")


def query_audit_logs(
    user_id: Optional[str] = None,
    action: Optional[str] = None,
    result: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    分页查询审计日志

    Returns:
        (items, total)：日志条目列表与总数
    """
    conn = _get_conn(_get_db_path())
    try:
        _init_table(conn)

        conditions = []
        params: List[Any] = []
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if result:
            conditions.append("result = ?")
            params.append(result)
        if start:
            conditions.append("created_at >= ?")
            params.append(start)
        if end:
            conditions.append("created_at <= ?")
            params.append(end)

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

        row = conn.execute(f"SELECT COUNT(*) FROM audit_logs{where_clause}", params).fetchone()
        total = row[0] if row else 0

        offset = (page - 1) * page_size
        rows = conn.execute(
            f"SELECT * FROM audit_logs{where_clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
        items = [dict(r) for r in rows]
        return items, total
    finally:
        conn.close()


def cleanup_expired_audit_logs(retention_days: int) -> int:
    """
    删除超过保留期的审计日志

    Args:
        retention_days: 保留天数（来自 system_config 的 audit_retention_days）

    Returns:
        删除的行数，失败返回 0（清理失败不应影响调度器主循环）
    """
    if retention_days <= 0:
        return 0
    try:
        conn = _get_conn(_get_db_path())
        try:
            _init_table(conn)
            cursor = conn.execute(
                "DELETE FROM audit_logs WHERE created_at < datetime('now', ?)",
                (f"-{retention_days} days",),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"⚠️ Failed to cleanup expired audit logs: {e}")
        return 0
