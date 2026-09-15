#!/usr/bin/env python3
"""Resolve the offline VLM selection before starting the existing vLLM server."""

import json
import os
from pathlib import Path
import sys


def selected_model(root=Path("/models"), explicit=None):
    root = Path(root)
    if explicit:
        return root / explicit
    config = root / "mineru.json"
    if config.exists():
        configured = json.loads(config.read_text()).get("models-dir", {}).get("vlm")
        if configured:
            path = Path(configured)
            if path.is_relative_to("/app/models"):
                return root / path.relative_to("/app/models")
            if path.is_relative_to(root):
                return path
            raise ValueError("VLM path is not mounted in this container; set MINERU_VLM_MODEL_DIR explicitly")
    return root / "MinerU2.5-2509-1.2B"


if __name__ == "__main__":
    model = selected_model(explicit=os.getenv("MINERU_VLM_MODEL_DIR"))
    if not model.is_dir():
        raise SystemExit(f"Offline VLM directory is missing: {model}. Prepare the selected model first.")
    os.execv(
        sys.executable,
        [sys.executable, "-m", "vllm.entrypoints.openai.api_server", "--model", str(model), *sys.argv[1:]],
    )
