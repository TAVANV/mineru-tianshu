"""Admin controls for incremental features, isolated from public configuration."""

import json
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from auth import User, Permission, require_permission
from auth.audit import query_audit_logs
from feature_config import load_config, public_admin_config, update_config

router = APIRouter(prefix="/api/v1/admin", tags=["可选功能"])
admin = require_permission(Permission.SYSTEM_CONFIG)


@router.get("/optional-features")
def get_features(current_user: User = Depends(admin)):
    return {"success": True, "config": public_admin_config(load_config())}


@router.post("/optional-features")
def save_features(data: dict, request: Request, current_user: User = Depends(admin)):
    try:
        result = update_config(data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))
    request.state.audit_detail = {"changed_keys": sorted(data)}
    return {"success": True, "config": result}


@router.get("/audit-logs")
def audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    action: str = None,
    user_id: str = None,
    result: str = None,
    start: str = None,
    end: str = None,
    current_user: User = Depends(admin),
):
    items, total = query_audit_logs(
        user_id=user_id, action=action, result=result, start=start, end=end, page=page, page_size=page_size
    )
    return {"success": True, "items": items, "total": total}


@router.get("/webhook-deliveries")
def deliveries(
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100), current_user: User = Depends(admin)
):
    from task_db import TaskDB

    with TaskDB().get_cursor() as c:
        total = c.execute("SELECT COUNT(*) FROM webhook_deliveries").fetchone()[0]
        rows = c.execute(
            "SELECT id,task_id,state,attempt,created_at,last_error,task_status FROM webhook_deliveries ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        ).fetchall()
    return {"success": True, "items": [dict(r) for r in rows], "total": total}


@router.post("/optional-features/test-caption")
def test_caption(current_user: User = Depends(admin)):
    from image_caption import ImageCaptionConfig, ImageCaptioner

    config = ImageCaptionConfig.load()
    if not config:
        raise HTTPException(400, "Enable and save image caption settings first")
    captioner = ImageCaptioner(config)
    try:
        ok, _, latency = captioner.test_connection()
    finally:
        captioner.client.close()
    return {
        "success": ok,
        "message": "Connection succeeded" if ok else "Connection failed; check endpoint and credentials",
        "latency_ms": latency,
    }


@router.post("/webhooks/test")
def test_webhook(current_user: User = Depends(admin)):
    from webhooks import subscription, send

    config = subscription()
    if not config:
        raise HTTPException(400, "Enable and save a webhook first")
    event_id = uuid.uuid4().hex
    try:
        send(
            config,
            json.dumps({"event_id": event_id, "status": "test"}).encode(),
            event_id,
            load_config()["webhook_allowed_hosts"],
        )
    except Exception:
        raise HTTPException(400, "Webhook delivery failed; check endpoint, allowlist and credentials")
    return {"success": True, "message": "Test delivered"}
