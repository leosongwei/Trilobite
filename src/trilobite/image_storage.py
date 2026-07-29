from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime
from pathlib import Path

from src.trilobite.messages import Image

# Explicit overrides for common image types so the extension is predictable
# regardless of platform quirks in ``mimetypes.guess_extension``.
_IMAGE_MIMES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}

_REVERSE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


def image_ext(mime_type: str) -> str:
    mime = mime_type.lower()
    ext = _IMAGE_MIMES.get(mime)
    if ext:
        return ext
    guessed = mimetypes.guess_extension(mime)
    return guessed or ".bin"


def ext_to_mime(ext: str) -> str:
    ext = ext.lower()
    mime = _REVERSE_MIMES.get(ext)
    if mime:
        return mime
    guessed, _ = mimetypes.guess_type(f"file{ext}")
    return guessed or "application/octet-stream"


def format_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")


def save_image(
    session_dir: Path,
    data: bytes,
    mime_type: str,
    original_name: str = "",
    date: str = "",
) -> Image:
    """Store image bytes under ``session_dir/images/<hash>.ext`` and return Image metadata."""
    images_dir = session_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    ext = image_ext(mime_type)
    filename = f"{hashlib.sha256(data).hexdigest()[:12]}{ext}"
    (images_dir / filename).write_bytes(data)
    return Image(
        filename=filename,
        mime_type=mime_type,
        original_name=original_name or filename,
        date=date,
    )
