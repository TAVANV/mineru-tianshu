#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
pip_index="${PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
torch_index="$pip_index"
torch_version="2.7.1"
vision_version="0.22.1"
audio_version="2.7.1"

if [ "$(uname -s)" = "Linux" ]; then
    if command -v apt-get > /dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y libgomp1 ffmpeg
    fi
    if command -v nvidia-smi > /dev/null 2>&1 && nvidia-smi > /dev/null 2>&1; then
        torch_index="https://download.pytorch.org/whl/cu126"
        torch_version="2.10.0+cu126"
        vision_version="0.25.0+cu126"
        audio_version="2.10.0+cu126"
    else
        torch_index="https://download.pytorch.org/whl/cpu"
    fi
elif ! command -v ffmpeg > /dev/null 2>&1; then
    echo "未找到 ffmpeg。使用音视频解析前请先安装。"
fi

torch_packages=("torch==$torch_version" "torchvision==$vision_version" "torchaudio==$audio_version")
echo "安装 PyTorch $torch_version"
python3 -m pip install --index-url "$torch_index" --default-timeout=600 --retries 5 "${torch_packages[@]}"

echo "安装 MinerU 4.2.3 与后端依赖"
python3 -m pip install -r requirements.txt --index-url "$pip_index" --default-timeout=600 --retries 5 "${torch_packages[@]}"
python3 -m pip check

python3 - <<'PY'
import torch
import transformers
from mineru.parser import parse
from mineru.render import render_content_list, render_content_list_v2

assert all(callable(function) for function in (parse, render_content_list, render_content_list_v2))
print(f"PyTorch {torch.__version__}（CUDA {'可用' if torch.cuda.is_available() else '不可用'}）")
print(f"Transformers {transformers.__version__}")
print("MinerU 公开解析与渲染入口验证通过")
PY
