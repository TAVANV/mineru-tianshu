"""旧定制版本到上游数据结构的单向兼容迁移。"""

import sqlite3

from loguru import logger


def migrate_legacy_webhooks(conn: sqlite3.Connection) -> None:
    """归档旧协议投递记录，避免同名表阻塞上游投递或误发旧签名通知。"""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(webhook_deliveries)")}
        if {"id", "state", "config"}.issubset(columns) and "delivery_id" not in columns:
            conn.execute("DROP TRIGGER IF EXISTS task_webhook_terminal")
            conn.execute("ALTER TABLE webhook_deliveries RENAME TO webhook_deliveries_legacy_20260915")
            logger.warning(
                "旧 Webhook 记录已保留在 webhook_deliveries_legacy_20260915；"
                "待投递通知不自动重发，请按上游协议重新配置并核对接收方。"
            )
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise
