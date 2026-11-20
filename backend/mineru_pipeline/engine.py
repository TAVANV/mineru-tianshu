"""通过 MinerU SDK 解析 PDF、图片和 Office 文件。"""

import gc
import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Lock

from loguru import logger

from utils import copy_file


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".jp2"}
VLM_MODEL_NAME = "MinerU-1.2B"


def _tier_for_backend(backend: str) -> str:
    if backend.startswith("vlm-"):
        return "standard"
    if backend.startswith("hybrid-"):
        return "advanced"
    return "basic"


def _page_range(options: dict) -> str:
    def page_number(key: str, default: int) -> int:
        value = options.get(key)
        if value is None:
            value = options.get(f"{key}_id")
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    start = max(0, page_number("start_page", 0))
    end = page_number("end_page", -1)
    if end < 0:
        return f"{start + 1}-r1" if start else ""
    return f"{start + 1}-{end + 1}"


class MinerUPipelineEngine:
    _instance = None
    _lock = Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, device: str = "cpu", vlm_api_base: str | None = None):
        with self._lock:
            if vlm_api_base:
                self.vlm_api_base = vlm_api_base
            if hasattr(self, "_last_active"):
                return
            self.vlm_api_base = vlm_api_base
            self._last_active = time.monotonic()
            self._cache_trimmed = True
            self._processing = 0
            threading.Thread(target=self._auto_sleep_monitor, daemon=True).start()
            logger.info(f"MinerU 解析引擎已初始化（设备 {device}）")

    def _auto_sleep_monitor(self):
        while True:
            time.sleep(10)
            with self._lock:
                if self._processing or self._cache_trimmed or time.monotonic() - self._last_active < 300:
                    continue
                self.cleanup()
                self._cache_trimmed = True

    def _wait_for_server(self, server_url: str, timeout: int = 60) -> None:
        health_url = f"{server_url.rstrip('/')}/v1/models"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(health_url, timeout=2) as response:
                    if response.status == 200:
                        return
            except urllib.error.URLError:
                pass
            time.sleep(1)
        logger.warning(f"等待 VLM 服务就绪超时（{timeout} 秒），将交由 MinerU 处理连接")

    @staticmethod
    def _flatten_content_blocks(json_content):
        """把 MinerU 的 content_list 拍平成块列表，兼容 v1 与 v2

        - v1 (*_content_list.json)    : 扁平数组 [block, block, ...]
        - v2 (*_content_list_v2.json) : 按页分组的嵌套数组 [[block, ...], [block, ...], ...]

        """
        if not isinstance(json_content, list):
            return []

        blocks = []
        for item in json_content:
            if isinstance(item, list):
                # v2：一页的块列表
                blocks.extend(b for b in item if isinstance(b, dict))
            elif isinstance(item, dict):
                # 按页分组的对象格式，或 v1 的扁平块
                page_blocks = item.get("parsing_res_list") or item.get("blocks")
                if isinstance(page_blocks, list):
                    blocks.extend(b for b in page_blocks if isinstance(b, dict))
                else:
                    blocks.append(item)
        return blocks

    @staticmethod
    def _extract_block_text(block):
        """从单个块里取纯文本，兼容 v1 的 text 字段与 v2 的 content 结构"""
        if not isinstance(block, dict):
            return ""

        # v1：直接带 text
        text = block.get("text")
        if isinstance(text, str) and text:
            return text

        content = block.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, dict):
            return ""

        # v2：content 下按块类型分不同的键，值形如 [{"content": "..."}, ...]
        for key in ("title_content", "paragraph_content", "table_content", "list_content"):
            items = content.get(key)
            if not isinstance(items, list):
                continue
            parts = []
            for item in items:
                if isinstance(item, dict):
                    piece = item.get("content")
                    if isinstance(piece, str):
                        parts.append(piece)
                elif isinstance(item, str):
                    parts.append(item)
            if parts:
                return "".join(parts)
        return ""

    @staticmethod
    def cleanup():
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except ImportError:
            pass
        except Exception as error:
            logger.warning(f"清理 GPU 缓存失败：{error}")

    def parse(self, file_path: str, output_path: str, options: dict | None = None) -> dict:
        with self._lock:
            self._processing += 1
            self._cache_trimmed = False
        try:
            return self._parse(Path(file_path), Path(output_path), options or {})
        finally:
            with self._lock:
                self._processing -= 1
                self._last_active = time.monotonic()

    def _parse(self, source: Path, output_dir: Path, options: dict) -> dict:
        from docvortex.export import materialize_middle, validate_materialized_assets
        from mineru.config import VlmConfig, get_config
        from mineru.parser import parse
        from mineru.render import render_content_list, render_content_list_v2, render_markdown

        backend = options.get("parse_mode") or "pipeline"
        tier = _tier_for_backend(backend)
        ocr_mode = "ocr" if options.get("force_ocr") else (options.get("method") or "auto")
        is_image = source.suffix.lower() in IMAGE_SUFFIXES
        is_pdf = source.suffix.lower() == ".pdf" or is_image
        page_range = _page_range(options) if is_pdf else ""

        if is_pdf and tier != "basic":
            server_url = options.get("server_url") or self.vlm_api_base
            vlm_config = (
                VlmConfig(server_url=server_url, model=VLM_MODEL_NAME) if server_url else get_config().model.vlm
            )
            if vlm_config.server_url:
                self._wait_for_server(vlm_config.server_url)
            elif backend.endswith("-http-client"):
                raise ValueError("MinerU HTTP 后端需要 server_url 或已配置的 VLM 服务地址")
        else:
            vlm_config = None

        unsupported = options.keys() & {
            "lang",
            "formula_enable",
            "table_enable",
            "draw_layout_bbox",
            "draw_span_bbox",
            "draw_layout",
            "draw_span",
            "dump_model_output",
        }
        if unsupported:
            logger.debug(f"MinerU 4.2.3 SDK 忽略无对应参数的选项：{', '.join(sorted(unsupported))}")

        with tempfile.TemporaryDirectory(prefix="mineru_") as temp_dir:
            # 图片转成 PDF 后，共用 OCR 页范围及源 PDF 预览。
            if is_image:
                import img2pdf

                sdk_source = Path(temp_dir) / "source.pdf"
                sdk_source.write_bytes(img2pdf.convert(source.read_bytes(), rotation=img2pdf.Rotation.ifvalid))
            else:
                sdk_source = Path(temp_dir) / f"source{source.suffix.lower()}"
                copy_file(source, sdk_source)

            result = parse(sdk_source, tier=tier, ocr_mode=ocr_mode, page_range=page_range, vlm_config=vlm_config)
            middle, assets = materialize_middle(result.middle_json)
            validate_materialized_assets(middle, assets)
            markdown = render_markdown(middle)
            content_v1 = render_content_list(middle) if options.get("dump_content_list", True) else None
            content_v2 = None
            if content_v1 is not None:
                content_v2 = []
                # V2 的外层数组下标须与用于预览的源 PDF 页号一致。
                for page, blocks in zip(middle.pages, render_content_list_v2(middle), strict=True):
                    content_v2.extend([] for _ in range(page.page_idx - len(content_v2)))
                    content_v2.append(blocks)

            if not markdown.strip():
                blocks = self._flatten_content_blocks(content_v2)
                markdown = "\n\n".join(text for block in blocks if (text := self._extract_block_text(block)))
                if not markdown.strip():
                    markdown = "\n\n".join(block["text"] for block in content_v1 or [] if block.get("text"))
                if not markdown.strip():
                    if not is_pdf:
                        raise ValueError("MinerU 未生成可用的解析内容")
                    markdown = "> 未提取到文本，请查看源文件预览。"

            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "images").mkdir(exist_ok=True)
            for path, payload in assets.items():
                (output_dir / path).write_bytes(payload)

            markdown_file = None
            if options.get("dump_markdown", True):
                markdown_file = output_dir / "result.md"
                markdown_file.write_text(markdown, encoding="utf-8")

            artifacts = output_dir / "mineru"
            json_file = None
            if content_v2 is not None:
                artifacts.mkdir(exist_ok=True)
                v1_json = json.dumps(content_v1, ensure_ascii=False, indent=2)
                # 根目录的 V1 供图片描述、RustFS 和分片合并按 img_path、page_idx 处理。
                (output_dir / "result.json").write_text(v1_json, encoding="utf-8")
                (artifacts / "result_content_list.json").write_text(v1_json, encoding="utf-8")
                json_file = artifacts / "result_content_list_v2.json"
                json_file.write_text(json.dumps(content_v2, ensure_ascii=False, indent=2), encoding="utf-8")

            if options.get("dump_middle_json", True):
                artifacts.mkdir(exist_ok=True)
                (artifacts / "result_middle.json").write_text(
                    json.dumps(middle.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
                )

            if options.get("dump_orig_pdf", True) or (is_image and options.get("draw_layout_bbox", True)):
                copy_file(sdk_source, output_dir / f"result_origin{sdk_source.suffix}")

        return {
            "markdown": markdown,
            "result_path": str(output_dir),
            "markdown_file": str(markdown_file) if markdown_file else None,
            "json_path": str(json_file) if json_file else None,
            "json_content": content_v2,
        }


def get_engine(vlm_api_base: str | None = None) -> MinerUPipelineEngine:
    return MinerUPipelineEngine(vlm_api_base=vlm_api_base)
