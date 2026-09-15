#!/usr/bin/env python3
"""Additive deployment entry point, adapted from upstream setup/pipeline sizing.

The default action only prints a plan. Existing deployment scripts remain usable.
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def hardware():
    ram_gb = 0
    try:
        if Path("/proc/meminfo").exists():
            ram_gb = int(Path("/proc/meminfo").read_text().split("MemTotal:")[1].split()[0]) / 1024**2
        else:
            ram_gb = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)) / 1024**3
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    vram = []
    try:
        data = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        vram = [int(line) / 1024 for line in data.splitlines() if line.strip()]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return ram_gb, vram


def budget(ram_gb, vram, overrides):
    """Upstream 8 GiB/worker and 25% host share, bounded for small machines."""
    gpu_count = int(overrides.get("GPU_COUNT", len(vram) or 1))
    if gpu_count < 1:
        raise ValueError("GPU_COUNT must be positive")
    automatic = min(4, max(1, int(min(vram) / 8))) if vram else 1
    if ram_gb:
        automatic = min(automatic, max(1, int(ram_gb * 0.25 / (8 * gpu_count))))
    concurrency = int(overrides.get("MAX_CONCURRENT_TASKS", automatic))
    if gpu_count < 1 or concurrency < 1:
        raise ValueError("GPU_COUNT and MAX_CONCURRENT_TASKS must be positive")
    result = {"MAX_CONCURRENT_TASKS": str(concurrency)}
    if ram_gb:
        cap = max(min(16, ram_gb * 0.75), ram_gb * 0.25)
        limit = max(1, int(min(max(16, 8 * gpu_count * concurrency), cap)))
        result.update(WORKER_MEMORY_LIMIT=f"{limit}G", WORKER_MEMORY_RESERVATION=f"{max(1, limit // 4)}G")
    if vram:
        result["MINERU_VIRTUAL_VRAM_SIZE"] = str(max(1, int(min(vram) / concurrency)))
    return {key: overrides.get(key, value) for key, value in result.items()}


def read_env(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["full", "pipeline", "cpu", "offline"], default="full")
    parser.add_argument(
        "--action", choices=["plan", "config", "build", "up", "down", "status", "check"], default="plan"
    )
    parser.add_argument("--auto-budget", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--run-as-user", action="store_true", help="Opt-in host UID runtime (base/pipeline only)")
    args = parser.parse_args(argv)
    env_file = args.env_file or ROOT / (".env.cpu" if args.mode == "cpu" else ".env")
    env = os.environ.copy()
    values = read_env(env_file) | env
    if args.auto_budget:
        ram, vram = hardware()
        env.update(budget(ram, vram, values))
    files = ["docker-compose.yml"] if args.mode in ("full", "pipeline") else [f"docker-compose.{args.mode}.yml"]
    if args.mode == "pipeline":
        files.append("docker-compose.pipeline.yml")
    if args.run_as_user:
        if args.mode not in ("full", "pipeline"):
            parser.error("--run-as-user supports full/pipeline; existing offline/CPU commands are unchanged")
        env.setdefault("TIANSHU_UID", str(os.getuid()))
        env.setdefault("TIANSHU_GID", str(os.getgid()))
        if Path("/var/run/docker.sock").exists():
            env.setdefault("DOCKER_GID", str(Path("/var/run/docker.sock").stat().st_gid))
        files.append("docker-compose.user.yml")
    command = ["docker", "compose"]
    if env_file.exists():
        command += ["--env-file", str(env_file)]
    for file in files:
        command += ["-f", str(ROOT / file)]
    action = {
        "config": ["config", "--quiet"],
        "build": ["build"],
        "up": ["up", "-d"],
        "down": ["down"],
        "status": ["ps"],
        "check": ["ps"],
    }.get(args.action, ["up", "-d"])
    if args.action == "plan":
        print(
            json.dumps(
                {
                    "command": shlex.join(command + action),
                    "budget": {
                        k: env[k]
                        for k in (
                            "MAX_CONCURRENT_TASKS",
                            "WORKER_MEMORY_LIMIT",
                            "WORKER_MEMORY_RESERVATION",
                            "MINERU_VIRTUAL_VRAM_SIZE",
                        )
                        if k in env
                    },
                    "run_as_user": args.run_as_user,
                },
                indent=2,
            )
        )
        return 0
    if args.action == "up":
        if not values.get("JWT_SECRET_KEY") or len(values["JWT_SECRET_KEY"]) < 32:
            parser.error("Set a strong JWT_SECRET_KEY in the selected env file before starting")
        for directory in [
            "models",
            "models/huggingface_cache",
            "models/modelscope_cache",
            "models/paddlex_cache",
            "models/torch_cache",
            "models/paddleocr_cache",
            "models/ultralytics_cfg",
            "data/runtime-home",
            "data/db",
            "data/uploads",
            "data/output",
            "logs",
        ]:
            path = ROOT / directory
            path.mkdir(parents=True, exist_ok=True)
            if not os.access(path, os.W_OK):
                parser.error(
                    f"{path} must be writable by the configured runtime UID; existing permissions were not changed"
                )
    subprocess.run(command + action, cwd=ROOT, env=env, check=True)
    if args.action == "check":
        import urllib.request

        # Check both direct API and the existing frontend proxy, as upstream does.
        for port in [values.get("API_PORT", "8000"), values.get("FRONTEND_PORT", "80")]:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=10) as response:
                if response.status != 200:
                    raise RuntimeError("Health check failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
