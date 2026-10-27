"""Thumbnails for the GUI: read a photo from the source, shrink it, cache the result as a small PNG."""
from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path, PurePosixPath

from .sources.base import RemoteFile, Source

THUMB = 200
MAX_BYTES = 40 * 1024 * 1024  # skip huge originals (ProRAW etc.); the row keeps its placeholder
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".tiff", ".dng"}
CACHE_DIR = Path.home() / ".phototransfer" / "thumbs"

try:  # HEIC is what iPhones shoot by default
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass


def is_image(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in IMAGE_EXTS


def make_thumb(data: bytes, size: int = THUMB) -> bytes | None:
    """Return a PNG no larger than size x size, or None if the bytes are not an image we can decode."""
    try:
        from PIL import Image, ImageOps

        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (size * 2, size * 2))  # JPEG: decode at reduced scale, much faster
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size))
        out = io.BytesIO()
        im.convert("RGB").save(out, "PNG")
        return out.getvalue()
    except Exception:
        return None


def _cache_file(f: RemoteFile, cache_dir: Path, size: int = THUMB) -> Path:
    key = hashlib.sha1(f"{f.path}|{f.size}|{f.mtime}|{size}".encode()).hexdigest()
    return cache_dir / f"{key}.png"


async def load_thumb(source: Source, f: RemoteFile, cache_dir: Path = CACHE_DIR, size: int = THUMB) -> bytes | None:
    """PNG thumbnail for f, from the cache if we have one, else read from the source. None if not possible."""
    if not is_image(f.path) or f.size > MAX_BYTES:
        return None
    cached = _cache_file(f, cache_dir, size)
    try:
        return cached.read_bytes()
    except OSError:
        pass
    buf = bytearray()
    async for chunk in source.read(f.path):
        buf += chunk
    png = await asyncio.get_running_loop().run_in_executor(None, make_thumb, bytes(buf), size)
    if png:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(png)
        except OSError:
            pass  # cache is an optimization only
    return png
