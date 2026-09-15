"""Validated optional feature settings; credentials are admin-only and masked."""

from auth.system_config import SystemConfig

MASK = "********"
DEFAULTS = {
    "registration_invite_code": "",
    "audit_retention_days": "90",
    "image_caption_enabled": "false",
    "image_caption_api_base": "",
    "image_caption_api_key": "",
    "image_caption_model": "",
    "image_caption_prompt": "",
    "image_caption_max_images": "30",
    "image_caption_concurrency": "4",
    "image_caption_timeout": "60",
    "webhook_enabled": "false",
    "webhook_url": "",
    "webhook_secret": "",
    "webhook_authorization": "",
    "webhook_allowed_hosts": "",
    "webhook_timeout": "10",
    "webhook_max_attempts": "5",
}
SECRETS = {"registration_invite_code", "image_caption_api_key", "webhook_secret", "webhook_authorization"}
LIMITS = {
    "audit_retention_days": (1, 3650),
    "image_caption_max_images": (1, 500),
    "image_caption_concurrency": (1, 16),
    "image_caption_timeout": (1, 300),
    "webhook_timeout": (1, 60),
    "webhook_max_attempts": (1, 20),
}


def load_config(db_path=None):
    return DEFAULTS | {k: v for k, v in SystemConfig(db_path).get_all_configs().items() if k in DEFAULTS}


def public_admin_config(values):
    return {k: MASK if k in SECRETS and v else v for k, v in values.items()}


def update_config(data, db_path=None):
    changes = {}
    for key, value in data.items():
        if key not in DEFAULTS:
            raise ValueError(f"Unknown setting: {key}")
        if key in SECRETS and value == MASK:
            continue
        if key.endswith("_enabled"):
            if value not in (True, False, "true", "false"):
                raise ValueError(f"Invalid boolean: {key}")
            value = str(value).lower()
        elif key in LIMITS:
            low, high = LIMITS[key]
            value = int(value)
            if not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
        elif not isinstance(value, str) or len(value) > 4096:
            raise ValueError(f"Invalid setting: {key}")
        if key == "registration_invite_code" and len(value.strip()) > 100:
            raise ValueError("registration_invite_code must be at most 100 characters")
        changes[key] = str(value).strip()
    values = load_config(db_path) | changes
    from webhooks import validate_url

    if values["webhook_enabled"] == "true" and not values["webhook_url"]:
        raise ValueError("Enabling the default webhook requires a URL")
    if values["webhook_enabled"] == "true" and (
        {"webhook_url", "webhook_allowed_hosts", "webhook_enabled"} & changes.keys()
    ):
        validate_url(values["webhook_url"], values["webhook_allowed_hosts"])
    if "\r" in values["webhook_authorization"] or "\n" in values["webhook_authorization"]:
        raise ValueError("Invalid authorization header")
    if values["image_caption_enabled"] == "true":
        from urllib.parse import urlsplit

        url = urlsplit(values["image_caption_api_base"])
        if url.scheme not in ("http", "https") or not url.hostname or not values["image_caption_model"]:
            raise ValueError("Image caption requires an HTTP(S) endpoint and model")
    if not SystemConfig(db_path).update_configs(changes):
        raise RuntimeError("Failed to update settings")
    return public_admin_config(values)
