#!/usr/bin/env python3
"""准备外置模型目录，供 docker-compose.external-models.yml 挂载使用。"""

import argparse
import os
import sys
import traceback
import urllib.request
from pathlib import Path

# ------------------------------------------------------------------------------
# 0. 目录与环境
# ------------------------------------------------------------------------------
ROOT = Path("/data/modelfiles")
PADDLEX = ROOT / "paddlex_cache"
MSCOPE = ROOT / "modelscope_cache"
WMARK = ROOT / "watermark_models"
ULTRA = ROOT / "ultralytics_cfg"
ACTIVE_VLM = None


def configure_root(root):
    global ROOT, PADDLEX, MSCOPE, WMARK, ULTRA
    ROOT = Path(root).resolve()
    PADDLEX, MSCOPE, WMARK, ULTRA = [
        ROOT / name for name in ("paddlex_cache", "modelscope_cache", "watermark_models", "ultralytics_cfg")
    ]
    for path in [ROOT, PADDLEX / "official_models", PADDLEX / "fonts", MSCOPE, WMARK, ULTRA]:
        path.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ["MODELSCOPE_CACHE"] = str(MSCOPE)
    os.environ["PADDLEX_HOME"] = str(PADDLEX)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"


_ok, _fail = [], []
# 收集"墙外源、下载失败需手动放置"的文件，末尾统一给出放置指引
_manual = []


def _fetch_or_keep(dst, url, hint=""):
    """已存在则跳过；否则尝试下载；失败不抛异常，仅登记为待手动放置
    （避免一两个 HF/github 墙外文件中断整个离线准备流程）。"""
    if dst.exists() and dst.stat().st_size > 0:
        print(f"   ✓ 已就位: {dst}")
        return True
    try:
        partial = dst.with_name(dst.name + ".download")
        try:
            urllib.request.urlretrieve(url, str(partial))
            if not partial.stat().st_size:
                raise ValueError("Downloaded file is empty")
            os.replace(partial, dst)
        finally:
            partial.unlink(missing_ok=True)
        print(f"   -> 下载完成: {dst}")
        return True
    except Exception as e:
        print(f"   ⚠️ 下载失败（{e.__class__.__name__}），需手动放置: {dst}")
        _manual.append((str(dst), url))
        return False


def step(title):
    print("\n" + "=" * 70)
    print(f"📦 {title}")
    print("=" * 70)


# ------------------------------------------------------------------------------
# 1. MinerU 4 Torch 小模型与 Pro VLM
# ------------------------------------------------------------------------------
def dl_mineru():
    from offline_models import main as prepare

    models = "mineru_pipeline,mineru_vlm_pro"
    if ACTIVE_VLM == "mineru_vlm":
        models += ",mineru_vlm"
    if prepare(str(ROOT), selected_models=models, strict=True, vlm_model=ACTIVE_VLM):
        raise RuntimeError("MinerU 模型准备失败")


# ------------------------------------------------------------------------------
# 2. 音频 5 个 —— 必须落到 MODELSCOPE_CACHE（funasr 运行时从这里读，不是 /app/models）
# ------------------------------------------------------------------------------
def dl_audio():
    step("Audio: SenseVoice / Paraformer / VAD / Punc / CAM++ (-> modelscope_cache)")
    from modelscope import snapshot_download as ms

    for m in [
        "iic/SenseVoiceSmall",
        "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
        "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        "iic/speech_campplus_sv_zh-cn_16k-common",
    ]:
        print(f"   -> {m}")
        ms(m)  # 不传 local_dir，落到 $MODELSCOPE_CACHE/hub/iic/...


# ------------------------------------------------------------------------------
# 3. PaddleOCR-VL + 它真正用到的全部子模型 —— paddlex 自动拉到 PADDLEX_HOME/official_models
#    （子模型清单由 PaddleX 库的 pipeline 定义，create_pipeline 触发后最权威）
# ------------------------------------------------------------------------------
def dl_paddleocr_vl():
    # 改用 ModelScope 直接下载完整仓库（不依赖 create_pipeline，规避 "pipeline does not exist" 报错）。
    # 引擎 paddleocr_vl/engine.py 离线时读 PADDLEX_HOME/official_models/<model_name>，
    # 默认 model_name="PaddleOCR-VL-1.5-0.9B"，所以直接把仓库下到该目录，引擎 local_cache 逻辑会直接用上，
    # 运行时无需联网、无需 create_pipeline 触发下载。
    step("PaddleOCR-VL (ModelScope 完整仓库 -> official_models/PaddleOCR-VL-1.5-0.9B)")
    from modelscope import snapshot_download as ms

    target = PADDLEX / "official_models" / "PaddleOCR-VL-1.5-0.9B"
    ms("PaddlePaddle/PaddleOCR-VL", local_dir=str(target))
    print(f"   -> {target}")
    print("   ⚠️ 若运行时 paddlex 仍报缺子模型（如 PP-DocLayoutV2），说明该仓库未含全部 pipeline 子模型，")
    print("      需在能联网的机器上用 create_pipeline('PaddleOCR-VL') 触发补全 official_models 后整体拷贝。")


# ------------------------------------------------------------------------------
# 4. YOLO 水印 —— 代码认死 ~/.cache/watermark_models/yolo11x_watermark.pt
# ------------------------------------------------------------------------------
def dl_yolo():
    # YOLO 水印模型在 HuggingFace（墙外）。下载机连不上 HF 时，请在能上网的机器下好后手动放置：
    #   https://hf-mirror.com/corzent/yolo11x_watermark_detection/resolve/main/best.pt
    #   -> watermark_models/yolo11x_watermark.pt （文件名必须如此，引擎 watermark_remover.py 认死）
    step("YOLO11x watermark (-> watermark_models/yolo11x_watermark.pt)")
    _fetch_or_keep(
        WMARK / "yolo11x_watermark.pt",
        "https://hf-mirror.com/corzent/yolo11x_watermark_detection/resolve/main/best.pt",
    )


# ------------------------------------------------------------------------------
# 5. 字体 + LaMa（直链）
# ------------------------------------------------------------------------------
def dl_extras():
    step("Fonts (simfang, 百度源必下) + LaMa + Arial（github 墙外，支持手动放置）")
    # simfang：百度 bcebos 源，国内可达，正常能下成功
    _fetch_or_keep(
        PADDLEX / "fonts" / "simfang.ttf",
        "https://paddle-model-ecology.bj.bcebos.com/paddlex/PaddleX3.0/fonts/simfang.ttf",
    )
    # LaMa：github releases（墙外）。容器需设 LAMA_MODEL=/root/.cache/watermark_models/big-lama.pt
    _fetch_or_keep(
        WMARK / "big-lama.pt",
        "https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt",
    )
    # Arial.ttf：ultralytics（github，墙外）。水印 YOLO 绘制时需要，容器挂到 /root/.config/Ultralytics/
    _fetch_or_keep(
        ULTRA / "Arial.ttf",
        "https://github.com/ultralytics/assets/releases/download/v0.0.0/Arial.ttf",
    )


# ------------------------------------------------------------------------------
# 6. 按需更新外置目录中的 Pro VLM
# ------------------------------------------------------------------------------
def dl_mineru_pro():
    from offline_models import main as prepare

    if prepare(str(ROOT), selected_models="mineru_vlm_pro", strict=True, vlm_model=ACTIVE_VLM):
        raise RuntimeError("Pro VLM 模型准备失败")


# ------------------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------------------
TASKS = [
    ("MinerU", dl_mineru),
    ("Audio", dl_audio),
    ("YOLO", dl_yolo),
    ("Fonts/LaMa", dl_extras),
]


def main(argv=None):
    global ACTIVE_VLM
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default="/data/modelfiles")
    parser.add_argument(
        "--only",
        choices=["mineru-vlm-pro"],
        help="Download only the additional Pro model; leave audio/Paddle caches intact",
    )
    parser.add_argument(
        "--vlm-model", choices=["mineru_vlm", "mineru_vlm_pro"], help="Explicitly activate a verified VLM"
    )
    args = parser.parse_args(argv)
    configure_root(args.root)
    ACTIVE_VLM = args.vlm_model
    _ok.clear()
    _fail.clear()
    _manual.clear()
    tasks = [("MinerU Pro VLM", dl_mineru_pro)] if args.only else TASKS
    print(f"📁 模型根目录: {ROOT}")
    print(f"   MODELSCOPE_CACHE = {MSCOPE}")
    print(f"   PADDLEX_HOME     = {PADDLEX}")
    for name, fn in tasks:
        try:
            fn()
            _ok.append(name)
        except Exception as e:
            _fail.append(name)
            print(f"   ❌ {name} 失败: {e}")
            traceback.print_exc()

    # ---- 汇总 + 验证提示 ----
    print("\n" + "#" * 70)
    print(f"✅ 成功: {_ok}")
    if _fail:
        print(f"❌ 失败(需单独处理): {_fail}")
    print("#" * 70)

    if _manual:
        print("\n" + "!" * 70)
        print("⚠️ 以下文件下载失败（多为 HF/github 墙外源不可达），需在能上网的机器下好后手动放置：")
        for dst, url in _manual:
            print(f"   • 目标: {dst}")
            print(f"     来源: {url}")
        print("   放好后无需重跑下载，再次执行本脚本会把它们校验为 ✓ 已就位。")
        print("   ⚠️ YOLO 文件名必须是 yolo11x_watermark.pt（引擎认死）。")
        print("   ⚠️ LaMa github 直链国内可加代理前缀: https://mirror.ghproxy.com/")
        print("!" * 70)

    print(f"外置模型目录：{ROOT}。设置 OFFLINE_MODELS_PATH 后叠加 docker-compose.external-models.yml。")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
