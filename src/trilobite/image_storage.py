from __future__ import annotations

import hashlib
import io
import mimetypes
from datetime import datetime
from pathlib import Path

from PIL import Image as PILImage

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


def transcode_image(data: bytes, mime_type: str) -> tuple[bytes, str] | None:
    """Re-encode an image as quality-92 JPEG (low-quality mode).

    Returns ``(jpeg_bytes, "image/jpeg")`` when the re-encode succeeded and
    is strictly smaller than the original; ``None`` otherwise so the caller
    keeps the original bytes. SVG (not raster-decodable) and animated images
    (a JPEG re-encode would drop all but the first frame) are left untouched.
    """
    if mime_type.lower() in ("image/svg+xml",):
        return None
    try:
        img = PILImage.open(io.BytesIO(data))
        img.load()
        if getattr(img, "is_animated", False):
            return None
        if img.mode in ("RGBA", "LA", "P", "PA"):
            # Flatten alpha onto white so transparent regions stay visible.
            rgba = img.convert("RGBA")
            flattened = PILImage.new("RGB", rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.split()[-1])
            img = flattened
        else:
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        out = buf.getvalue()
    except Exception:
        return None
    if len(out) >= len(data):
        return None
    return out, "image/jpeg"


def save_image(
    session_dir: Path,
    data: bytes,
    mime_type: str,
    original_name: str = "",
    date: str = "",
    low_quality: bool = False,
) -> Image:
    """Store image bytes under ``session_dir/images/<hash>.ext`` and return Image metadata.

    With ``low_quality`` the image is re-encoded as quality-92 JPEG first
    (when that shrinks it); the untouched original is kept alongside as
    ``<hash>.orig<ext>`` purely as a disk backup -- it is never referenced by
    history and never served.
    """
    images_dir = session_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    stored, stored_mime = data, mime_type
    if low_quality:
        transcoded = transcode_image(data, mime_type)
        if transcoded is not None:
            stored, stored_mime = transcoded
            orig_hash = hashlib.sha256(data).hexdigest()[:12]
            backup = images_dir / f"{orig_hash}.orig{image_ext(mime_type)}"
            if not backup.exists():
                backup.write_bytes(data)
    ext = image_ext(stored_mime)
    filename = f"{hashlib.sha256(stored).hexdigest()[:12]}{ext}"
    (images_dir / filename).write_bytes(stored)
    return Image(
        filename=filename,
        mime_type=stored_mime,
        original_name=original_name or filename,
        date=date,
    )
