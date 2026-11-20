"""为每种镜像的手动发布独立预留整数版本。"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TAG_PREFIX = "refs/tags/image-build-version/"
REGISTRY_PREFIX = "registry.cn-chengdu.aliyuncs.com/tavan-ai"
IMAGE_NAMES = ("tianshu-backend", "tianshu-frontend")


def parse_reserved_versions(refs: list[dict], image_name: str) -> set[int]:
    if image_name not in IMAGE_NAMES:
        raise ValueError(f"不支持的镜像名称 {image_name}")
    versions = set()
    # 旧统一预留记录作为基线，镜像专属预留只影响对应镜像。
    pattern = re.escape(TAG_PREFIX) + rf"(?:{re.escape(image_name)}/)?([1-9][0-9]*)"
    for ref in refs:
        match = re.fullmatch(pattern, ref["ref"])
        if match and int(match[1]) >= 3:
            versions.add(int(match[1]))
    return versions


def registry_tag_exists(image: str) -> bool:
    result = subprocess.run(
        ["docker", "manifest", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    if result.returncode == 0:
        return True
    message = result.stderr.strip()
    if "manifest unknown" in message.lower() or "no such manifest" in message.lower():
        return False
    raise RuntimeError(f"无法确认镜像标签是否存在 {image}\n{message}")


def select_version(requested: str, reserved: set[int], tag_exists, image_name: str) -> str:
    if image_name not in IMAGE_NAMES:
        raise ValueError(f"不支持的镜像名称 {image_name}")
    requested = requested.strip()
    if requested:
        if len(requested) > 128 or not re.fullmatch(r"[0-9]+", requested):
            raise ValueError("版本必须为不超过 128 位的整数")
        version = int(requested)
        if version < 3:
            raise ValueError("版本从 3 开始，请填写大于等于 3 的整数")
        if version in reserved:
            raise ValueError(f"版本 {version} 已预留，请使用新版本")
    else:
        version = max({2} | reserved) + 1

    while True:
        if len(str(version)) > 128:
            raise ValueError("版本超过 Docker 标签长度限制")
        image = f"{REGISTRY_PREFIX}/{image_name}:{version}"
        if not tag_exists(image):
            return str(version)
        if requested:
            raise ValueError(f"镜像版本已存在 {image}")
        version += 1


def github_request(method: str, path: str, payload=None):
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        api_url + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "tianshu-image-publisher",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"GitHub API 返回 HTTP {error.code}，请检查仓库权限及版本预留标签") from error


def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["GITHUB_SHA"]
    image_name = os.environ["IMAGE_NAME"]
    refs = github_request("GET", f"/repos/{repository}/git/matching-refs/tags/image-build-version/")
    version = select_version(
        os.environ.get("REQUESTED_VERSION", ""),
        parse_reserved_versions(refs, image_name),
        registry_tag_exists,
        image_name,
    )

    # createRef 在标签已存在时失败，分配冲突不能进入镜像发布阶段。
    github_request(
        "POST",
        f"/repos/{repository}/git/refs",
        {"ref": f"{TAG_PREFIX}{image_name}/{version}", "sha": sha},
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"version={version}\n")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(f"已预留 `{image_name}` 版本 **{version}**，源码 `{sha}`。\n\n")
            summary.write(f"- `{REGISTRY_PREFIX}/{image_name}:{version}`\n")
            summary.write("\n失败或取消的构建也占用该镜像已预留的版本。发布以本工作流构建任务成功为准。\n")
    print(f"已预留 {image_name} 版本 {version}")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, RuntimeError, URLError, subprocess.TimeoutExpired) as error:
        print(f"版本分配失败 {error}", file=sys.stderr)
        raise SystemExit(1)
