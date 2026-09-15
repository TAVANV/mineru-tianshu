"""Shared image-reference handling for captions, result URLs and image handoff."""

import html
import re
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

# Escaped brackets/backslashes in alt text must not terminate the image prematurely.
MARKDOWN_IMAGE = re.compile(
    r"!\[(?P<alt>(?:\\.|[^\]\\])*)\]\(\s*"
    r"(?P<url><[^>\n]+>|(?:\\.|[^\\()\s]|\([^()\n]*\))+)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)
HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
HTML_SOURCE = re.compile(r"\bsrc\s*=\s*(?P<quote>[\"'])(?P<url>.*?)(?P=quote)", re.IGNORECASE)


def decoded_reference(value):
    return html.unescape(re.sub(r"\\([\\\[\]()<> ])", r"\1", value.strip("<>")))


def image_filenames(content):
    references = [m.group("url") for m in MARKDOWN_IMAGE.finditer(content)]
    for tag in HTML_IMAGE.finditer(content):
        source = HTML_SOURCE.search(tag.group())
        if source:
            references.append(source.group("url"))
    return {PurePosixPath(unquote(urlsplit(decoded_reference(ref)).path)).name for ref in references}


def local_image_url(reference, mapping):
    value = decoded_reference(reference)
    url = urlsplit(value)
    if url.scheme or url.netloc:
        return None
    path = PurePosixPath(unquote(url.path).replace("\\", "/"))
    # Only complete image paths; never match an arbitrary string containing a filename.
    if len(path.parts) < 2 or path.parts[-2] != "images":
        return None
    return mapping.get(path.name)


def replace_image_references(content, mapping, *, markdown_to_html=False):
    def markdown(match):
        url = local_image_url(match.group("url"), mapping)
        if url is None:
            return match.group()
        if markdown_to_html:
            alt = html.unescape(re.sub(r"\\([\\\[\]()<> ])", r"\1", match.group("alt")))
            return f'<img src="{html.escape(url, quote=True)}" alt="{html.escape(alt, quote=True)}">'
        if match.group("url").startswith("<"):
            url = "<" + url + ">"
        else:
            # Bare Markdown destinations need spaces/parentheses escaped after a rename.
            url = quote(url, safe="/:#?&=@[]!$'*+,;%-._~")
        start, end = match.start("url") - match.start(), match.end("url") - match.start()
        return match.group()[:start] + url + match.group()[end:]

    def image_tag(match):
        tag = match.group()
        source = HTML_SOURCE.search(tag)
        if not source:
            return tag
        url = local_image_url(source.group("url"), mapping)
        if url is None:
            return tag
        return tag[: source.start("url")] + html.escape(url, quote=True) + tag[source.end("url") :]

    # Rewrite existing HTML before adding any new tags from Markdown.
    return MARKDOWN_IMAGE.sub(markdown, HTML_IMAGE.sub(image_tag, content))


def replace_json_image_references(value, mapping, field=None):
    """Replace exact image-path fields and inline Markdown/HTML, preserving all other text."""
    image_fields = {"img_path", "image_path", "image_url", "src", "path", "url", "images"}
    if isinstance(value, dict):
        return {key: replace_json_image_references(item, mapping, key) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_json_image_references(item, mapping, field) for item in value]
    if isinstance(value, str):
        if field in image_fields:
            url = local_image_url(value, mapping)
            if url is not None:
                return url
        return replace_image_references(value, mapping)
    return value
