"""只读检查外置模型及 mineru.json；不下载、不改写模型目录。"""

import argparse
import json
from pathlib import Path

from offline_models import MODELS, verify_model


def check_models(root: Path) -> None:
    root = root.resolve()
    config = json.loads((root / "mineru.json").read_text())
    if config.get("model-source") != "local":
        raise ValueError("外置模型的 mineru.json 必须设置 model-source=local")
    for kind in ("pipeline", "vlm"):
        configured = Path(config["models-dir"][kind])
        if not configured.is_relative_to("/app/models"):
            raise ValueError(f"{kind} 模型路径必须位于容器 /app/models 内")
        relative = configured.relative_to("/app/models")
        if not (root / relative).resolve().is_relative_to(root):
            raise ValueError(f"{kind} 模型路径逃逸外置目录")
        spec = dict(MODELS["mineru_pipeline" if kind == "pipeline" else "mineru_vlm_pro"])
        spec["target_dir"] = str(relative)
        valid, reason = verify_model(root, kind, spec)
        if not valid:
            raise ValueError(f"{kind} 模型不完整: {reason}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path("/app/models"))
    check_models(parser.parse_args().root)
    print("外置 MinerU 模型检查通过；未修改或下载模型。")
