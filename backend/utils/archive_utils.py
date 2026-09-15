"""Bounded local ZIP/EPUB handling; no external-resource downloads."""

import hashlib
import json
import re
import shutil
import stat
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

SUPPORTED = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".docx",
    ".xlsx",
    ".pptx",
    ".doc",
    ".xls",
    ".ppt",
    ".md",
    ".txt",
    ".html",
    ".htm",
    ".csv",
    ".json",
    ".xml",
    ".epub",
    ".wav",
    ".mp3",
    ".flac",
    ".m4a",
    ".ogg",
    ".mp4",
    ".avi",
    ".mkv",
    ".mov",
    ".fasta",
    ".fa",
    ".fna",
    ".faa",
    ".gb",
    ".gbk",
    ".genbank",
}
MAX_FILES = 1000
MAX_BYTES = 512 * 1024 * 1024


def extract_archive(source, destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) > MAX_FILES or sum(m.file_size for m in members) > MAX_BYTES:
            raise ValueError("Archive exceeds file count or uncompressed size limit")
        seen = set()
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            mode = member.external_attr >> 16
            if (
                "\\" in name
                or ":" in name
                or path.is_absolute()
                or ".." in path.parts
                or stat.S_ISLNK(mode)
                or member.flag_bits & 1
            ):
                raise ValueError("Unsafe or encrypted archive entry")
            target = destination.joinpath(*path.parts)
            if not target.resolve().is_relative_to(destination):
                raise ValueError("Archive entry escapes destination")
            if name.casefold() in seen:
                raise ValueError("Duplicate archive path")
            seen.add(name.casefold())
            if member.file_size > max(1, member.compress_size) * 200:
                raise ValueError("Archive compression ratio exceeds limit")
        total = 0
        for member in members:
            target = destination / member.filename
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, target.open("xb") as dst:
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError("Archive exceeds extraction size limit")
                    dst.write(chunk)
    return sorted(p for p in destination.rglob("*") if p.is_file())


def archive_documents(source, destination):
    files = extract_archive(source, destination)
    candidates = [
        p
        for p in files
        if p.suffix.lower() in SUPPORTED
        and "__MACOSX" not in p.parts
        and not any(part.startswith(".") for part in p.relative_to(destination).parts)
    ]
    if not candidates:
        raise ValueError("Archive has no supported documents")
    result = []
    for index, path in enumerate(candidates):
        member = path.relative_to(destination).as_posix()
        # Worker output directories are based on stem; make every member globally unique.
        unique = path.with_name(f"{Path(destination).name}_{index}_{path.name}")
        shutil.copy2(path, unique)
        result.append({"file_name": member, "file_path": str(unique), "archive_index": index, "archive_member": member})
    return result


def convert_epub(source, output):
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    output = Path(output)
    images = output / "images"
    images.mkdir(parents=True, exist_ok=True)
    sections = []
    with tempfile.TemporaryDirectory(prefix="epub-") as temp:
        root = Path(temp).resolve()
        extract_archive(source, root)
        container = ET.parse(root / "META-INF/container.xml")
        book = next(iter(container.findall(".//{*}rootfile")), None)
        if book is None:
            raise ValueError("EPUB has no package document")
        package_path = (root / book.attrib["full-path"]).resolve()
        if not package_path.is_relative_to(root):
            raise ValueError("Invalid EPUB package path")
        package = ET.parse(package_path)
        manifest = {n.attrib["id"]: n.attrib for n in package.findall(".//{*}manifest/{*}item")}
        for item in package.findall(".//{*}spine/{*}itemref"):
            entry = manifest.get(item.attrib["idref"])
            if not entry:
                raise ValueError("EPUB spine references a missing item")
            chapter = (package_path.parent / unquote(entry["href"].split("#")[0])).resolve()
            if not chapter.is_relative_to(root):
                raise ValueError("Invalid EPUB chapter path")
            soup = BeautifulSoup(chapter.read_text(encoding="utf-8"), "html.parser")
            for tag in soup(["script", "style", "iframe", "object"]):
                tag.decompose()
            for tag in soup.find_all("img"):
                src = urlsplit(tag.get("src", ""))
                if src.scheme or src.netloc:
                    tag.decompose()
                    continue
                path = (chapter.parent / unquote(src.path)).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    tag.decompose()
                    continue
                name = hashlib.sha256(path.read_bytes()).hexdigest()[:20] + path.suffix.lower()
                shutil.copy2(path, images / name)
                tag["src"] = "images/" + name
            sections.append(markdownify(str(soup), heading_style="ATX"))
    (output / "result.md").write_text("\n\n".join(sections), encoding="utf-8")
    (output / "result.json").write_text(json.dumps({"chapters": sections}, ensure_ascii=False), encoding="utf-8")


def merge_archive_results(children, output):
    from utils.image_references import replace_image_references, replace_json_image_references

    output = Path(output)
    images = output / "images"
    images.mkdir(parents=True, exist_ok=True)
    documents, markdown = [], []
    ordered = sorted(children, key=lambda c: json.loads(c["options"]).get("archive_index", 0))
    for index, child in enumerate(ordered):
        result = Path(child["result_path"])
        source_md = result / "result.md"
        if not source_md.is_file():
            candidates = sorted(result.glob("*.md"))
            if not candidates:
                raise ValueError("Archive member has no Markdown result")
            source_md = candidates[0]
        text = source_md.read_text(encoding="utf-8")
        replacements = {}
        for image in sorted((result / "images").glob("*")):
            if image.is_file():
                name = f"{index}_{image.name}"
                shutil.copy2(image, images / name)
                replacements[image.name] = "images/" + name

        text = replace_image_references(text, replacements)
        data_file = result / "result.json"
        data = (
            replace_json_image_references(json.loads(data_file.read_text(encoding="utf-8")), replacements)
            if data_file.exists()
            else None
        )
        title = json.loads(child["options"]).get("archive_member", child["file_name"])
        title = re.sub(r"[\r\n<>]", "", title)
        markdown.append(f"# {title}\n\n{text}")
        documents.append({"file_name": title, "task_id": child["task_id"], "content": text, "data": data})
    (output / "result.md").write_text("\n\n---\n\n".join(markdown), encoding="utf-8")
    (output / "result.json").write_text(
        json.dumps({"documents": documents}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
