# ============================================================================
# MinerU Server - Enterprise AI Data Preprocessing (CUDA 13.0 Edition)
# Optimized for: MinerU 4.2.3 (VLM Support)
# Fixes: Missing botocore, s3transfer, reportlab, and other sub-dependencies
# ============================================================================

# ============================================================================
# 镜像源配置（构建参数）：
#   - 默认使用国内镜像（阿里云 apt / 阿里云 PyPI）
#   - 海外网络: docker buildx build --build-arg APT_MIRROR= --build-arg PIP_MIRROR=https://pypi.org/simple ...
# ============================================================================
ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_MIRROR=https://mirrors.aliyun.com/pypi/simple/

FROM nvidia/cuda:13.0.2-cudnn-runtime-ubuntu24.04 AS base
ARG APT_MIRROR
ARG PIP_MIRROR

# 1. [网络优化] apt 源（APT_MIRROR 为空则保留官方源）+ 抗干扰配置
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s/archive.ubuntu.com/${APT_MIRROR}/g" /etc/apt/sources.list.d/ubuntu.sources && \
        sed -i "s/security.ubuntu.com/${APT_MIRROR}/g" /etc/apt/sources.list.d/ubuntu.sources; \
    fi && \
    echo 'Acquire::Retries "20"; Acquire::http::Timeout "300";' > /etc/apt/apt.conf.d/99custom

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CUDA_HOME=/usr/local/cuda \
    PATH=/usr/local/cuda/bin:$PATH \
    LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH \
    PIP_CACHE_DIR=/root/.cache/pip

# 2. [系统修复] 底层库 + 中文字体 + LibreOffice
# 合并为单层：原先拆成两个 RUN 会执行两次 apt-get update（各拉一遍索引）。
# LibreOffice 只装实际用到的三个组件，元包 libreoffice 会额外带上 Base / Draw / Math，
# 本项目一个都不用（Dockerfile.cpu 与 Dockerfile.offline 本来就是这么写的）。
# build-essential 提供 triton 运行期所需的 gcc。
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3.12-dev python3-pip python3.12-venv build-essential \
    git wget curl ffmpeg antiword pandoc poppler-utils \
    libgomp1 libglib2.0-0 libsm6 libxext6 libxrender-dev \
    libgl1 libglx-mesa0 libgstreamer1.0-0 \
    fonts-noto-core fonts-noto-cjk fonts-noto-cjk-extra \
    fonts-wqy-zenhei fonts-wqy-microhei fontconfig \
    libreoffice-writer libreoffice-calc libreoffice-impress \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# 4. Python 环境配置
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.12 1

RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m pip install --upgrade --ignore-installed pip "setuptools>=75.0.0" wheel \
    -i ${PIP_MIRROR} --break-system-packages

# -----------------------------------------------------------------------------
# Stage 2: Dependencies Installation
# -----------------------------------------------------------------------------
FROM base AS dependencies
WORKDIR /tmp
COPY backend/requirements.txt /tmp/requirements.txt
ARG PIP_MIRROR

# 保持 FlashAttention 和 CUDA 13 镜像所使用的 Torch ABI。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch==2.10.0+cu130 torchvision==0.25.0+cu130 torchaudio==2.10.0+cu130 \
    --index-url https://download.pytorch.org/whl/cu130 --break-system-packages

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements.txt torch==2.10.0+cu130 torchvision==0.25.0+cu130 torchaudio==2.10.0+cu130 \
    -i "${PIP_MIRROR}" --break-system-packages

# Step 6: [扩展] FlashAttention (必须在 torch 版本确认后最后安装)
# 该 wheel 有 242MB 且只在 GitHub Releases 上，国内直连经常几十 KB/s 且反复断连。
# 因此优先使用预先下载好的本地 wheel；backend/wheels/ 为空时才回退到直连下载。
#   wget -c -t 0 --timeout=30 -O backend/wheels/flash_attn-2.8.3+cu130torch2.10-cp312-cp312-linux_x86_64.whl \
#     https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/v0.7.16/flash_attn-2.8.3%2Bcu130torch2.10-cp312-cp312-linux_x86_64.whl
COPY backend/wheels/ /tmp/wheels/
RUN --mount=type=cache,target=/root/.cache/pip \
    if ls /tmp/wheels/flash_attn-*.whl > /dev/null 2>&1; then \
        echo "📦 Installing FlashAttention from pre-downloaded wheel" && \
        pip install /tmp/wheels/flash_attn-*.whl --break-system-packages; \
    else \
        echo "🌐 No local wheel found, downloading FlashAttention from GitHub" && \
        pip install https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/v0.7.16/flash_attn-2.8.3%2Bcu130torch2.10-cp312-cp312-linux_x86_64.whl --break-system-packages; \
    fi

# Step 6.5: [ONNX GPU] 表格模型（结构识别 / 分类）跑在 ONNX Runtime 上。MinerU 依赖的是纯 CPU 版
# onnxruntime，装上后表格模型只能用 CPU，多进程下成为 CPU 瓶颈。换成 onnxruntime-gpu：
# - 1.27 起 PyPI 上的 onnxruntime-gpu 默认按 CUDA 13.0 + cuDNN 9 构建，与本镜像一致
# - 两个包共用 onnxruntime 模块目录，必须先卸 CPU 版，否则文件互相覆盖
# - 必须是最后一个 pip 步骤：之后再装依赖 mineru 的包，会把 CPU 版装回来
RUN --mount=type=cache,target=/root/.cache/pip \
    pip uninstall -y onnxruntime --break-system-packages && \
    pip install onnxruntime-gpu==1.30.0 -i ${PIP_MIRROR} --break-system-packages

# Step 7: [最终验证]
# 在构建阶段验证实际调用的公开入口。
RUN python -c "import torch; import pypdf; import boto3; import botocore; import openai; print(chr(9989) + ' 三方依赖验证通过')" \
    && python -c "import flash_attn; print(chr(9989) + ' flash_attn 可用')" \
    && python -c "import onnxruntime as o; from importlib.metadata import distributions; assert not any(d.metadata['Name'].lower() == 'onnxruntime' for d in distributions()); p = o.get_available_providers(); assert 'CUDAExecutionProvider' in p, p; print(chr(9989) + ' onnxruntime-gpu ' + o.__version__ + ' ' + str(p))" \
    && python -c "from mineru.parser import parse; from mineru.render import render_content_list, render_content_list_v2; print(chr(9989) + ' MinerU 公开解析与渲染入口可用')" \
    && pip show mineru flash-attn | grep -E "^(Name|Version)"

# -----------------------------------------------------------------------------
# Stage 3: Application Runtime
# -----------------------------------------------------------------------------
FROM dependencies AS application
WORKDIR /app
COPY backend/ /app/backend/
COPY pyproject.toml /app/
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY scripts/init-models.sh /usr/local/bin/init-models.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/init-models.sh && \
    mkdir -p /app/models /app/data/uploads /app/data/output /app/data/db /app/logs

# 非 root 运行用户（worker 服务因挂载 /var/run/docker.sock，在 compose 中显式覆盖为 root）
RUN groupadd -g 10001 tianshu && \
    useradd -u 10001 -g tianshu -m -s /bin/bash tianshu && \
    chown -R tianshu:tianshu /app /home/tianshu

# USER 指令不会自动设置 HOME，显式指定以便运行时解析用户缓存路径
ENV HOME=/home/tianshu

WORKDIR /app/backend

RUN python -c "import ffmpeg, soundfile, kaldi_native_fbank, imagehash, pywt; from funasr import AutoModel; from audio_engines.sensevoice_engine import SenseVoiceEngine; from video_engines.keyframe_extractor import KeyframeExtractor, VideoOCREngine; from video_engines.video_engine import VideoProcessingEngine; assert VideoProcessingEngine.check_ffmpeg(); print(chr(9989) + ' 音频与视频关键帧入口可用')"

RUN python -c "from ultralytics import YOLO; from remove_watermark.lama_inpainter import SimpleLama; from remove_watermark.watermark_remover import WatermarkRemover, LAMA_AVAILABLE, ULTRALYTICS_AVAILABLE; assert LAMA_AVAILABLE and ULTRALYTICS_AVAILABLE; print(chr(9989) + ' YOLO 与离线 LaMa 入口可用')"

EXPOSE 8000 8001 8002

USER tianshu

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["api", "python", "api_server.py"]
