#!/usr/bin/env python3
"""在外置模型目录中选择本机 vLLM 要加载的权重。"""

import os
import sys
from pathlib import Path

import yaml


def selected_model(root=Path("/models"), explicit=None):
    root = Path(root)
    path = root / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    configured = (
        explicit or config.get("tianshu", {}).get("vllm_model_dir") or "MinerU2.5-Pro-2605-1.2B"
    )
    model = Path(configured)
    if model.is_relative_to("/app/models"):
        model = model.relative_to("/app/models")
    model = (root / model).resolve()
    if not model.is_relative_to(root.resolve()):
        raise ValueError("VLM 模型路径必须位于挂载目录内")
    return model


if __name__ == "__main__":
    model = selected_model(explicit=os.getenv("MINERU_VLM_MODEL_DIR"))
    if not model.is_dir():
        raise SystemExit(f"找不到外置 VLM 模型目录：{model}")
    os.execv(
        sys.executable,
        [sys.executable, "-m", "vllm.entrypoints.openai.api_server", "--model", str(model), *sys.argv[1:]],
    )
