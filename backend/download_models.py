#!/usr/bin/env python3
"""下载部署所需的模型，并生成 MinerU 4.2.3 的本地配置。"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import urlretrieve

import yaml
from loguru import logger


MODELS = {
    "mineru_pipeline": {
        "repo_id": "opendatalab/MinerU-4_models_torch",
        "revision": "2b3afb86f4d23fa623f6b2f4b2279ed4ef93a89d",
        "target_dir": "MinerU-4_models_torch",
        "required": True,
        "verify": [
            "Layout/PP-DocLayoutV2/config.json",
            "Layout/PP-DocLayoutV2/inference.yml",
            "Layout/PP-DocLayoutV2/model.safetensors",
            "Layout/PP-DocLayoutV2/preprocessor_config.json",
            "MFR/pp_formulanet_plus_m/PP-FormulaNet_plus-M.pth",
            "MFR/pp_formulanet_plus_m/PP-FormulaNet_plus-M_inference.yml",
            "OCR/paddleocr/ch_PP-OCRv6_tiny_det_infer.safetensors",
            "OCR/paddleocr/ch_PP-OCRv6_small_rec_infer.safetensors",
            "OCR/paddleocr/seal_PP-OCRv4_det_infer.pth",
            "Table/slanet-plus.onnx",
            "Table/unet.onnx",
            "Table/PP-LCNet_x1_0_table_cls.onnx",
        ],
    },
    "mineru_vlm": {
        "repo_id": "opendatalab/MinerU2.5-Pro-2605-1.2B",
        "revision": "08aaea840498d49ce16247b6263196cf02814885",
        "target_dir": "MinerU2.5-Pro-2605-1.2B",
        "required": True,
        "verify": ["config.json", "model.safetensors", "preprocessor_config.json", "tokenizer.json"],
    },
    "mineru_vlm_gguf": {
        "repo_id": "jinzhenj/MinerU2.5-Pro-2605-1.2B-GGUF",
        "revision": "9185688a0495e1577d521a757c7c0b62dd38ca48",
        "target_dir": "MinerU2.5-Pro-2605-1.2B-GGUF",
        "required": True,
        "verify": ["MinerU2.5-Pro-2605-1.2B-Q8_0.gguf", "mmproj-MinerU2.5-Pro-2605-1.2B-Q8_0.gguf"],
    },
    "sensevoice": {
        "model_id": "iic/SenseVoiceSmall",
        "target_dir": "SenseVoiceSmall",
        "required": True,
    },
    "paraformer": {
        "model_id": "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "target_dir": "Paraformer",
        "required": False,
    },
    "yolo11": {
        "repo_id": "corzent/yolo11x_watermark_detection",
        "filename": "best.pt",
        "target_dir": "YOLO11",
        "verify": ["best.pt"],
        "required": False,
    },
}
MODEL_COMPLETE_MARKER = ".mineru_complete"
RUNTIME_AUDIO_MODELS = {
    MODELS["sensevoice"]["model_id"]: "SenseVoiceSmall",
    MODELS["paraformer"]["model_id"]: "Paraformer",
    "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch": "speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch": "punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
    "iic/speech_campplus_sv_zh-cn_16k-common": "speech_campplus_sv_zh-cn_16k-common",
}
LAMA_URL = "https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt"


def _has_files(target: Path, names: list[str]) -> bool:
    return all((target / name).is_file() and (target / name).stat().st_size > 0 for name in names)


def verify_mineru_snapshot(target: Path, config: dict) -> bool:
    if not _has_files(target, config["verify"]):
        return False
    try:
        record = json.loads((target / MODEL_COMPLETE_MARKER).read_text(encoding="utf-8"))
        files = record["files"]
        return (
            record["version"] == 1
            and all(name in files for name in config["verify"])
            and all(
                (target / name).is_file() and (target / name).stat().st_size == size for name, size in files.items()
            )
        )
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return False


def download_mineru_snapshot(config: dict, target: Path, force: bool = False) -> None:
    from huggingface_hub import HfApi, snapshot_download

    repo_id, revision = config["repo_id"], config["revision"]
    endpoint = os.getenv("HF_ENDPOINT") or None
    info = HfApi(endpoint=endpoint).model_info(repo_id, revision=revision, files_metadata=True)
    if info.sha != revision:
        raise ValueError(f"{repo_id} 返回了非预期的提交 {info.sha}")
    files = {item.rfilename: item.size for item in info.siblings or []}
    if any(not isinstance(files.get(name), int) or files[name] <= 0 for name in config["verify"]):
        raise ValueError(f"{repo_id}@{revision} 缺少必需的模型文件")

    target.mkdir(parents=True, exist_ok=True)
    # Hugging Face 会按固定快照校验已有权重，旧版 Pro 权重内容相同时可原地复用。
    snapshot_download(repo_id, revision=revision, local_dir=target, endpoint=endpoint, force_download=force)
    if any(not (target / name).is_file() or (target / name).stat().st_size != size for name, size in files.items()):
        raise ValueError(f"{repo_id}@{revision} 的模型文件不完整")

    # 这是 MinerU 4.2.3 本地加载目录型模型所需的上游完成标记。
    marker = target / MODEL_COMPLETE_MARKER
    temp = marker.with_suffix(".tmp")
    temp.write_text(json.dumps({"version": 1, "files": files}, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(marker)


def generate_mineru_config(output_path: Path, runtime_root: str = "/app/models") -> None:
    path = output_path / "config.yaml"
    config = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    model = config.setdefault("model", {})
    model.update(source="local", base_dir=runtime_root, small_backend="torch")
    vlm = model.setdefault("vlm", {})
    vlm.setdefault("engine", "auto")
    vlm.setdefault("model", "MinerU-1.2B")
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (output_path / ".locks").mkdir(exist_ok=True)
    logger.info(f"已生成 MinerU 配置 {path}")


def reuse_models(source: Path, target: Path) -> None:
    source = source.resolve(strict=True)
    if not source.is_dir() or source == target.resolve():
        raise ValueError("复用目录必须是独立的旧模型目录")

    names = {
        *RUNTIME_AUDIO_MODELS.values(),
        MODELS["mineru_vlm"]["target_dir"],
        "YOLO11",
        "watermark_models",
        "big-lama.pt",
        "torch_cache",
        "ultralytics_cfg",
        "modelscope_cache",
        "huggingface_cache",
    }
    for name in sorted(names):
        old, new = source / name, target / name
        if old.is_dir():
            if not new.exists():
                # 已复用的权重仍由固定快照校验，避免把旧目录的完成标记当成当前快照的凭据。
                shutil.copytree(old, new, ignore=shutil.ignore_patterns(MODEL_COMPLETE_MARKER, ".mineru_files.json"))
                logger.info(f"已复用模型目录 {old}")
        elif old.is_file() and not new.exists():
            shutil.copy2(old, new)
            logger.info(f"已复用模型文件 {old}")


def _prepare_runtime_assets(output_path: Path) -> None:
    from modelscope import snapshot_download
    from modelscope_hub.errors import CacheNotFound

    cache = output_path / "modelscope_cache"
    cache.mkdir(exist_ok=True)
    for model_id, directory in RUNTIME_AUDIO_MODELS.items():
        target = output_path / directory
        weights = "campplus_cn_common.bin" if model_id == "iic/speech_campplus_sv_zh-cn_16k-common" else "model.pt"
        required = ["configuration.json", "config.yaml", weights]
        if _has_files(target, required):
            continue
        try:
            try:
                cached = Path(snapshot_download(model_id, cache_dir=cache, local_files_only=True))
            except CacheNotFound:
                cached = None
            if cached and _has_files(cached, required):
                shutil.copytree(cached, target, dirs_exist_ok=True)
            else:
                snapshot_download(model_id, local_dir=str(target), revision="master")
            if not _has_files(target, required):
                raise ValueError(f"离线音频模型不完整：{target}")
        except Exception as error:
            if directory in ("SenseVoiceSmall", "speech_fsmn_vad_zh-cn-16k-common-pytorch"):
                raise
            logger.warning(f"准备可选的音频权重失败（{directory}）：{error}")

    watermark = output_path / "watermark_models"
    watermark.mkdir(exist_ok=True)
    old_yolo = output_path / "YOLO11" / "best.pt"
    yolo = watermark / "yolo11x_watermark.pt"
    if old_yolo.is_file() and not yolo.exists():
        shutil.copy2(old_yolo, yolo)

    lama = watermark / "big-lama.pt"
    if not lama.exists():
        reused_lama = next(
            (
                path
                for path in [output_path / "big-lama.pt", output_path / "torch_cache/hub/checkpoints/big-lama.pt"]
                if path.is_file()
            ),
            None,
        )
        if reused_lama:
            shutil.copy2(reused_lama, lama)
        else:
            partial = lama.with_suffix(".download")
            try:
                urlretrieve(LAMA_URL, partial)
                if not partial.stat().st_size:
                    raise ValueError("LaMa 下载为空")
                partial.replace(lama)
            except Exception as error:
                logger.warning(f"准备可选的 LaMa 权重失败：{error}")
            finally:
                partial.unlink(missing_ok=True)


def main(
    output_dir: str,
    selected_models: str | None = None,
    force: bool = False,
    *,
    reuse_from: list[str] | None = None,
    runtime_root: str = "/app/models",
    offline: bool = False,
) -> int:
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    for path in reuse_from or []:
        reuse_models(Path(path), output_path)

    names = selected_models.split(",") if selected_models else [key for key in MODELS if key != "mineru_vlm_gguf"]
    manifest = {"created": datetime.now().isoformat(), "models": {}}
    failed = False
    for name in names:
        name = name.strip()
        config = MODELS[name]
        if offline and config.get("model_id"):
            continue
        target = output_path / config["target_dir"]
        try:
            if config.get("revision"):
                if force or not verify_mineru_snapshot(target, config):
                    download_mineru_snapshot(config, target, force=force)
            elif config.get("model_id"):
                if force or not target.is_dir() or not any(target.iterdir()):
                    from modelscope import snapshot_download

                    snapshot_download(config["model_id"], local_dir=str(target), revision="master")
            elif force or not _has_files(target, config["verify"]):
                if _has_files(output_path, ["watermark_models/yolo11x_watermark.pt"]) and not force:
                    target.mkdir(exist_ok=True)
                    shutil.copy2(output_path / "watermark_models/yolo11x_watermark.pt", target / config["filename"])
                else:
                    from huggingface_hub import hf_hub_download

                    hf_hub_download(config["repo_id"], config["filename"], local_dir=target, force_download=force)

            manifest["models"][name] = {"status": "ready", "path": str(target)}
            logger.info(f"模型已就绪 {target}")
        except Exception as error:
            failed = failed or config["required"]
            manifest["models"][name] = {"status": "error"}
            logger.error(f"准备模型 {name} 失败：{error}")
    if offline and not failed:
        _prepare_runtime_assets(output_path)
    if not failed:
        generate_mineru_config(output_path, runtime_root)
    (output_path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="./models")
    parser.add_argument("--models")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reuse-from", action="append", help="复用旧模型目录，可重复指定")
    parser.add_argument("--runtime-root", default="/app/models", help="写入配置的运行时模型目录")
    parser.add_argument("--offline", action="store_true", help="准备离线推理所需的音频缓存及水印权重")
    args = parser.parse_args()
    sys.exit(
        main(
            args.output,
            args.models,
            args.force,
            reuse_from=args.reuse_from,
            runtime_root=args.runtime_root,
            offline=args.offline,
        )
    )
