"""Shared offline model selection; contains no model SDK imports or network calls."""

import json
import os
from pathlib import Path


def active_vlm_path(model_root=None, explicit=None):
    root = Path(model_root or os.getenv("MODEL_PATH", "/app/models"))
    selection = explicit if explicit is not None else os.getenv("MINERU_VLM_MODEL_DIR")
    if selection:
        return root / selection
    config = root / "mineru.json"
    if config.exists():
        path = json.loads(config.read_text()).get("models-dir", {}).get("vlm")
        if path:
            return Path(path)
    return root / "MinerU2.5-2509-1.2B"
