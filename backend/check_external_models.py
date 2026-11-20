"""只读校验外置的 MinerU 4 配置和权重。"""

import argparse
import os
from pathlib import Path

import yaml

from offline_models import MODELS, verify_model


def check_models(root: Path) -> None:
    root = root.resolve()
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    model = config["model"]
    if model.get("source") != "local" or model.get("small_backend") != "torch":
        raise ValueError("外置模型的 config.yaml 必须设置 model.source=local、model.small_backend=torch")

    mounted_root = Path("/app/models")
    base_dir = Path(model["base_dir"])
    if not base_dir.is_relative_to(mounted_root):
        raise ValueError("model.base_dir 必须位于容器 /app/models 内")
    relative_base = base_dir.relative_to(mounted_root)
    vlm_config = config.get("tianshu", {})
    vlm_dir = Path(
        os.getenv("MINERU_VLM_MODEL_DIR") or vlm_config.get("vllm_model_dir") or MODELS["mineru_vlm_pro"]["target_dir"]
    )
    if vlm_dir.is_absolute():
        if not vlm_dir.is_relative_to(mounted_root):
            raise ValueError("VLM 模型路径必须位于容器 /app/models 内")
        vlm_dir = vlm_dir.relative_to(mounted_root)
    vlm_key = "mineru_vlm" if vlm_dir.name == MODELS["mineru_vlm"]["target_dir"] else "mineru_vlm_pro"
    for kind, relative in (
        ("mineru_pipeline", relative_base / MODELS["mineru_pipeline"]["target_dir"]),
        (vlm_key, vlm_dir),
    ):
        if not (root / relative).resolve().is_relative_to(root):
            raise ValueError(f"{kind} 模型路径逃逸外置目录")
        spec = MODELS[kind] | {"target_dir": str(relative)}
        valid, reason = verify_model(root, kind, spec)
        if not valid:
            raise ValueError(f"{kind} 模型不完整：{reason}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path("/app/models"))
    check_models(parser.parse_args().root)
    print("外置 MinerU 模型校验通过。")
