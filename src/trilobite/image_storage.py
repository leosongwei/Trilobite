from __future__ import annotations

import hashlib
import io
import mimetypes
from datetime import datetime
from pathlib import Path

from PIL import Image as PILImage

from src.trilobite.messages import Image

# Raster formats the completions API accepts as image_url content parts.
_API_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

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


def _rasterize_svg(data: bytes) -> bytes | None:
    """Render an SVG to PNG bytes (resvg, bundled in the wheel)."""
    try:
        import resvg_py

        png = bytes(resvg_py.svg_to_bytes(svg_string=data.decode("utf-8", errors="replace")))
        return png or None
    except Exception:
        return None


def _has_alpha(img: PILImage.Image) -> bool:
    if img.mode in ("RGBA", "LA", "PA"):
        return True
    return img.mode == "P" and "transparency" in img.info


def _encode_compressed(img: PILImage.Image) -> tuple[bytes, str]:
    """Encode as the smaller of quality-85 JPEG and PNG.

    Images with transparency keep their alpha channel as PNG; opaque images
    pick whichever of JPEG and PNG compresses better (JPEG for photos, PNG
    for flat-color screenshots).
    """
    if _has_alpha(img):
        buf = io.BytesIO()
        img.convert("RGBA").save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    png = buf.getvalue()
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
    jpeg = buf.getvalue()
    if len(jpeg) < len(png):
        return jpeg, "image/jpeg"
    return png, "image/png"


def normalize_image(data: bytes, mime_type: str) -> tuple[bytes, str] | None:
    """Convert image bytes into a format the completions API accepts.

    ``image_url`` content parts only take PNG / JPEG / GIF / WebP. Anything
    else (SVG, BMP, TIFF, ...) is re-encoded here: SVG via the bundled resvg
    renderer, other formats via Pillow. Returns ``(bytes, mime)`` or
    ``None`` when the image cannot be converted (broken data, unsupported
    and undecodable) -- callers must not store such images as image parts.
    """
    mime = mime_type.lower().split(";")[0].strip()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime in _API_IMAGE_MIMES:
        return data, mime
    if mime == "image/svg+xml":
        png = _rasterize_svg(data)
        if png is None:
            return None
        data = png
    try:
        img = PILImage.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    return _encode_compressed(img)


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
) -> Image | None:
    """Store image bytes under ``session_dir/images/<hash>.ext`` and return Image metadata.

    The image is first normalized to an API-supported format (``normalize_image``);
    ``None`` is returned when that fails -- such an image must never reach an
    ``image_url`` content part.

    With ``low_quality`` the image is re-encoded as quality-92 JPEG first
    (when that shrinks it); the untouched original is kept alongside as
    ``<hash>.orig<ext>`` purely as a disk backup -- it is never referenced by
    history and never served.
    """
    normalized = normalize_image(data, mime_type)
    if normalized is None:
        return None
    data, mime_type = normalized
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
