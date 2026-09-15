"""
输出结果规范化器基类
"""

from pathlib import Path
from typing import Dict, Any, Optional
from loguru import logger
import json
import os


class BaseOutputNormalizer:
    """
    输出结果规范化器基类
    定义了规范化的基本流程和公共方法（如 RustFS 上传）
    """

    STANDARD_MARKDOWN_NAME = "result.md"
    STANDARD_JSON_NAME = "result.json"
    STANDARD_IMAGE_DIR = "images"

    def __init__(self):
        """
        初始化规范化器
        """
        self._rustfs_client = None

    def normalize(
        self, output_dir: Path, use_rustfs: Optional[bool] = None, enable_caption: bool = True
    ) -> Dict[str, Any]:
        """
        规范化输出目录（模板方法）

        Args:
            output_dir: 输出目录（引擎的原始输出目录）
            use_rustfs: 任务级 RustFS 开关。
                        None  → 由 RUSTFS_ENABLED 环境变量决定（向后兼容）
                        True  → 强制上传到 RustFS
                        False → 不上传，保留本地图片路径

        Returns:
            规范化后的文件信息
        """
        output_dir = Path(output_dir)
        if not output_dir.exists():
            raise ValueError(f"Output directory does not exist: {output_dir}")

        logger.info(f"🔧 Normalizing output directory: {output_dir}")

        # 1. 执行本地文件规范化（由子类实现）
        result = self._normalize_local_files(output_dir)

        # 确保基本字段存在
        result.setdefault("markdown_file", None)
        result.setdefault("json_file", None)
        result.setdefault("image_dir", None)
        result.setdefault("image_count", 0)
        result.setdefault("rustfs_enabled", False)
        result.setdefault("images_uploaded", False)

        if enable_caption:
            try:
                from image_caption import ImageCaptionConfig, process_output_dir

                config = ImageCaptionConfig.load()
                if config:
                    result["image_caption"] = process_output_dir(output_dir, config)
            except Exception as e:
                logger.warning(f"Image caption skipped: {type(e).__name__}")

        # 2. 自动上传图片到 RustFS 并替换 URL（基础功能，始终启用）
        if result["image_dir"] and result["image_count"] > 0:
            self._process_rustfs_upload(result, use_rustfs=use_rustfs)
        else:
            logger.debug("ℹ️  No images to upload")
            result["rustfs_enabled"] = False
            result["images_uploaded"] = False

        logger.info("✅ Normalization complete:")
        logger.info(f"   Markdown: {result['markdown_file']}")
        logger.info(f"   Images: {result['image_count']} files in {result['image_dir']}")
        logger.info(f"   JSON: {result['json_file']}")
        logger.info(f"   RustFS: {result['rustfs_enabled']} (uploaded: {result['images_uploaded']})")

        return result

    def _normalize_local_files(self, output_dir: Path) -> Dict[str, Any]:
        """
        规范化本地文件（子类必须实现）

        Args:
            output_dir: 输出目录

        Returns:
            Dict containing:
            - markdown_file: Optional[Path]
            - json_file: Optional[Path]
            - image_dir: Optional[Path]
            - image_count: int
        """
        raise NotImplementedError

    def _process_rustfs_upload(self, result: Dict[str, Any], use_rustfs: Optional[bool] = None):
        """处理 RustFS 上传和 URL 替换

        Args:
            result: 规范化结果字典
            use_rustfs: 任务级 RustFS 开关。
                        None  → 读取 RUSTFS_ENABLED 环境变量（向后兼容）
                        True/False → 按任务强制开启/关闭
        """

        # 检查是否启用 RustFS：任务级开关优先，未指定时回退到环境变量
        if use_rustfs is None:
            rustfs_enabled = os.getenv("RUSTFS_ENABLED", "true").lower() in ("true", "1", "yes")
        else:
            rustfs_enabled = use_rustfs

        if not rustfs_enabled:
            logger.info("ℹ️  RustFS upload disabled for this task, using local file service")
            result["rustfs_enabled"] = False
            result["images_uploaded"] = False
            return

        try:
            logger.info(f"📤 Uploading {result['image_count']} images to RustFS...")
            url_mapping = self._upload_images_to_rustfs(result["image_dir"])

            if url_mapping:
                # 替换 Markdown 中的图片路径
                if result["markdown_file"]:
                    self._replace_markdown_urls(result["markdown_file"], url_mapping)

                # 替换 JSON 中的图片路径
                if result["json_file"]:
                    self._replace_json_urls(result["json_file"], url_mapping)

                result["rustfs_enabled"] = True
                result["images_uploaded"] = True
                logger.info(f"✅ Images uploaded to RustFS: {len(url_mapping)}/{result['image_count']}")
            else:
                logger.warning("⚠️  No images uploaded (url_mapping empty)")
                result["rustfs_enabled"] = False
                result["images_uploaded"] = False
        except Exception as e:
            logger.error(f"❌ Failed to upload images to RustFS: {e}")
            logger.error(f"   Error details: {type(e).__name__}: {str(e)}")
            result["rustfs_enabled"] = False
            result["images_uploaded"] = False
            # RustFS 上传失败不应中断主流程，继续使用本地路径
            logger.warning("⚠️  Continuing with local image paths (RustFS upload failed)")

    def _upload_images_to_rustfs(self, image_dir: Path) -> Dict[str, str]:
        """
        上传图片到 RustFS 对象存储

        Args:
            image_dir: 图片目录

        Returns:
            {本地文件名: RustFS URL} 的映射字典
        """
        # 延迟导入，避免在不需要时初始化
        try:
            from storage import RustFSClient

            if self._rustfs_client is None:
                self._rustfs_client = RustFSClient()

            # 直接上传，使用日期前缀 (YYYYMMDD/短uuid.ext)
            logger.info(f"📤 Uploading images to RustFS: {image_dir}")
            url_mapping = self._rustfs_client.upload_directory(
                str(image_dir),
                prefix=None,  # 不使用额外前缀，直接用日期分组
            )

            return url_mapping

        except Exception as e:
            logger.error(f"❌ Failed to initialize RustFS client: {e}")
            raise

    def _replace_markdown_urls(self, md_file: Path, url_mapping: Dict[str, str]):
        from utils.image_references import replace_image_references

        content = md_file.read_text(encoding="utf-8")
        updated = replace_image_references(content, url_mapping, markdown_to_html=True)
        if updated != content:
            md_file.write_text(updated, encoding="utf-8")

    def _replace_json_urls(self, json_file: Path, url_mapping: Dict[str, str]):
        from utils.image_references import replace_json_image_references

        data = json.loads(json_file.read_text(encoding="utf-8"))
        json_file.write_text(
            json.dumps(replace_json_image_references(data, url_mapping), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
