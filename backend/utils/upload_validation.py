"""Optional file-signature validation. Off by default for legacy upload clients."""

from pathlib import Path


def validate_signature(filename, prefix):
    extension = Path(filename).suffix.lower()
    signatures = {
        ".pdf": (b"%PDF-",),
        ".png": (b"\x89PNG\r\n\x1a\n",),
        ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",),
        ".gif": (b"GIF87a", b"GIF89a"),
        ".bmp": (b"BM",),
        ".tif": (b"II*\0", b"MM\0*"),
        ".tiff": (b"II*\0", b"MM\0*"),
        ".zip": (b"PK\x03\x04", b"PK\x05\x06"),
        ".epub": (b"PK\x03\x04",),
        ".docx": (b"PK\x03\x04",),
        ".xlsx": (b"PK\x03\x04",),
        ".pptx": (b"PK\x03\x04",),
        ".doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
        ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
        ".ppt": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    }
    if extension in signatures and not any(prefix.startswith(s) for s in signatures[extension]):
        raise ValueError("File content does not match the filename extension")
    if extension in {".webp", ".wav"}:
        expected = b"WEBP" if extension == ".webp" else b"WAVE"
        if prefix[:4] != b"RIFF" or prefix[8:12] != expected:
            raise ValueError("Invalid RIFF file signature")
