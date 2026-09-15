"""Durable, at-least-once task notifications with bounded retries and pinned DNS."""

import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
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


def subscription(override_url=None, db_path=None):
    from feature_config import load_config

    values = load_config(db_path)
    url = override_url or (values["webhook_url"] if values["webhook_enabled"] == "true" else "")
    if not url:
        return None
    if override_url:
        validate_url(url, values["webhook_allowed_hosts"])
    same_target = url == values["webhook_url"]
    return {
        "url": url,
        "secret": values["webhook_secret"] if same_target else "",
        "authorization": values["webhook_authorization"] if same_target else "",
        "timeout": int(values["webhook_timeout"]),
        "max_attempts": int(values["webhook_max_attempts"]),
    }


def send(config, body, event_id, allowed_hosts):
    parsed, address = validate_url(config["url"], allowed_hosts)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    headers = {"Content-Type": "application/json", "X-Tianshu-Event-Id": event_id}
    if config.get("secret"):
        headers["X-Tianshu-Signature"] = (
            "sha256=" + hmac.new(config["secret"].encode(), body, hashlib.sha256).hexdigest()
        )
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
