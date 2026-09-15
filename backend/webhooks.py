"""Durable, at-least-once task notifications with bounded retries and pinned DNS."""

import base64
import re
import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import sqlite3
import ssl
import time
import uuid
from urllib.parse import urlsplit


def init_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS task_webhooks (
        task_id TEXT PRIMARY KEY, config TEXT NOT NULL)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS webhook_deliveries (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'pending', next_at REAL NOT NULL DEFAULT 0,
        lease_token TEXT, config TEXT NOT NULL, payload TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_error TEXT,
        retry_count INTEGER NOT NULL, task_status TEXT NOT NULL,
        UNIQUE(task_id, retry_count, task_status))""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_webhook_due ON webhook_deliveries(state, next_at)")
    # Outbox insertion is atomic with every terminal transition, including recovery SQL.
    cursor.execute("""CREATE TRIGGER IF NOT EXISTS task_webhook_terminal
        AFTER UPDATE OF status ON tasks
        WHEN NEW.parent_task_id IS NULL AND NEW.status IN ('completed','failed','cancelled')
             AND OLD.status != NEW.status
        BEGIN
          INSERT OR IGNORE INTO webhook_deliveries
          (id, task_id, config, payload, retry_count, task_status)
          SELECT lower(hex(randomblob(16))), NEW.task_id, w.config,
            json_object('task_id',NEW.task_id,'file_name',NEW.file_name,'status',NEW.status,
              'retry_count',NEW.retry_count,'completed_at',NEW.completed_at),
            NEW.retry_count, NEW.status
          FROM task_webhooks w WHERE w.task_id=NEW.task_id;
        END""")


def validate_url(url, allowed_hosts=""):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("Webhook URL must be HTTP(S), without credentials or fragments")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = parsed.hostname.lower()
    allow = {h.strip().lower() for h in allowed_hosts.split(",") if h.strip()}
    explicit = f"{host}:{port}" in allow
    addresses = sorted({r[4][0] for r in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or (not ip.is_global and not explicit):
            raise ValueError("Private webhook addresses require an administrator host:port allowlist entry")
    if not addresses:
        raise ValueError("Webhook host has no address")
    return parsed, addresses[0]


def validate_key_webhook(config, db_path=None):
    from feature_config import load_config

    if config.get("webhook_enabled"):
        validate_url(config.get("webhook_url", ""), load_config(db_path)["webhook_allowed_hosts"])
    auth = {k.removeprefix("webhook_"): v for k, v in config.items()}
    build_auth_headers(auth)


def key_subscription(row, values):
    if not row or not row.get("webhook_enabled"):
        return None
    config = {k.removeprefix("webhook_"): (v or "") for k, v in row.items() if k.startswith("webhook_")}
    return config | {
        "timeout": int(values["webhook_timeout"]),
        "max_attempts": int(values["webhook_max_attempts"]),
        "source": "api_key",
        "api_key_id": row["key_id"],
    }


def subscription(override_url=None, db_path=None, api_key_id=None):
    from feature_config import load_config

    values = load_config(db_path)
    key_config = None
    if api_key_id:
        # Authentication has already migrated this database. Avoid re-running AuthDB
        # schema/bootstrap work for every submitted task.
        if db_path is None:
            from auth.system_config import SystemConfig

            db_path = SystemConfig().db_path
        with sqlite3.connect(db_path, timeout=30) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM api_keys WHERE key_id=?", (api_key_id,)).fetchone()
        key_config = key_subscription(dict(row) if row else None, values)
    default_url = values["webhook_url"] if values["webhook_enabled"] == "true" else ""
    url = override_url or (key_config["url"] if key_config else default_url)
    if not url:
        return None
    if override_url:
        validate_url(url, values["webhook_allowed_hosts"])
    if key_config and url == key_config["url"]:
        return key_config | {"source": "task" if override_url else "api_key"}
    # Do not inherit credentials from a global callback when a key-bound callback
    # was overridden, even if the new URL happens to match the global URL.
    same_global = not key_config and url == values["webhook_url"]
    return {
        "url": url,
        "secret": values["webhook_secret"] if same_global else "",
        "authorization": values["webhook_authorization"] if same_global else "",
        "timeout": int(values["webhook_timeout"]),
        "max_attempts": int(values["webhook_max_attempts"]),
        "source": "task" if override_url else "global",
        "api_key_id": api_key_id,
    }


def send(config, body, event_id, allowed_hosts):
    parsed, address = validate_url(config["url"], allowed_hosts)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    headers = {"Content-Type": "application/json", "X-Tianshu-Event-Id": event_id}
    if config.get("secret"):
        headers["X-Tianshu-Signature"] = (
            "sha256=" + hmac.new(config["secret"].encode(), body, hashlib.sha256).hexdigest()
        )
    headers.update(build_auth_headers(config))
    if config.get("authorization"):
        headers["Authorization"] = config["authorization"]
    # Connect to the validated address, while retaining the original HTTP Host and TLS name.
    # No environment proxies, second DNS lookup, or automatic redirects.
    connection = http.client.HTTPConnection(parsed.hostname, port, timeout=config["timeout"])
    try:
        connection.sock = socket.create_connection((address, port), timeout=config["timeout"])
        if parsed.scheme == "https":
            connection.sock = ssl.create_default_context().wrap_socket(connection.sock, server_hostname=parsed.hostname)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise RuntimeError(f"HTTP {response.status}")
        return response.status
    finally:
        connection.close()


def dispatch(db, limit=20):
    from feature_config import load_config

    allowed_hosts = load_config(db.db_path)["webhook_allowed_hosts"]
    delivered = 0
    for _ in range(limit):
        now, token = time.time(), uuid.uuid4().hex
        with db.get_cursor() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM webhook_deliveries WHERE state IN ('pending','sending') AND next_at<=? ORDER BY created_at,id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                break
            config = json.loads(row["config"])
            # Lease permits scheduler crash recovery. Attempts count completed delivery attempts.
            c.execute(
                "UPDATE webhook_deliveries SET state='sending', next_at=?, lease_token=? WHERE id=?",
                (now + config["timeout"] + 60, token, row["id"]),
            )
        error = None
        try:
            body = json.dumps(
                dict(json.loads(row["payload"]), event_id=row["id"]), ensure_ascii=False, separators=(",", ":")
            ).encode()
            send(config, body, row["id"], allowed_hosts)
            delivered += 1
        except Exception as exc:
            # Do not store response bodies, URLs with tokens, or authorization values.
            error = f"HTTP delivery failed ({type(exc).__name__})"
        attempt = row["attempt"] + 1
        state = "delivered" if error is None else ("dead" if attempt >= config["max_attempts"] else "pending")
        with db.get_cursor() as c:
            c.execute(
                "UPDATE webhook_deliveries SET state=?,attempt=?,next_at=?,last_error=?,lease_token=NULL WHERE id=? AND lease_token=?",
                (state, attempt, time.time() + min(3600, 2 ** min(attempt, 11) * 5), error, row["id"], token),
            )
    return delivered


def build_auth_headers(auth: dict) -> dict:
    """按 auth_type 构造出站鉴权头；none 或配置不完整时不附加任何头

    api_key 的自定义头名只放行字母数字和连字符，防止注入非法头名。
    """
    if not auth:
        return {}
    for key, value in auth.items():
        if key.startswith("auth_") and isinstance(value, str) and any(c in value for c in ("\r", "\n", "\x00")):
            raise ValueError("Invalid authentication header")
    auth_type = auth.get("auth_type", "none")
    if auth_type == "bearer":
        token = (auth.get("auth_token") or "").strip()
        return {"Authorization": f"Bearer {token}"} if token else {}
    if auth_type == "basic":
        username = auth.get("auth_username") or ""
        password = auth.get("auth_password") or ""
        if not (username or password):
            return {}
        encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    if auth_type == "api_key":
        name = (auth.get("auth_header_name") or "").strip()
        value = (auth.get("auth_header_value") or "").strip()
        reserved = {
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
            "content-type",
            "x-tianshu-signature",
            "x-tianshu-event-id",
            "proxy-authorization",
        }
        if name.lower() in reserved or not re.fullmatch(r"[A-Za-z0-9-]+", name):
            raise ValueError("Invalid or reserved header name")
        if not value:
            return {}
        return {name: value}
    return {}
