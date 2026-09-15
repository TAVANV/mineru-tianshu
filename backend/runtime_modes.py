"""Opt-in pipeline deployment boundaries; full deployments keep every backend."""

import os
from pathlib import Path


def validate_pipeline_task(filename, backend):
    if os.getenv("TIANSHU_DEPLOY_MODE", "full") != "pipeline":
        return
    if backend not in {"auto", "pipeline"} or Path(filename).suffix.lower() in {
        ".wav",
        ".mp3",
        ".flac",
        ".m4a",
        ".ogg",
        ".mp4",
        ".avi",
        ".mkv",
        ".mov",
        ".webm",
        ".flv",
        ".wmv",
    }:
        raise ValueError("This deployment only enables document pipeline processing")
