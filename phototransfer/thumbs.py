"""Thumbnails for the GUI: read a photo from the source, shrink it, cache the result as a small PNG."""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tempfile
from pathlib import Path, PurePosixPath

from .sources.base import RemoteFile, Source

THUMB = 72
MAX_BYTES = 40 * 1024 * 1024  # skip huge originals (ProRAW etc.); the row keeps its placeholder
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".tiff", ".dng", ".webp"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
VIDEO_MAX_BYTES = 150 * 1024 * 1024  # a video is copied off the phone whole to find its first frame; skip longer ones
CACHE_DIR = Path.home() / ".phototransfer" / "thumbs"

try:  # HEIC is what iPhones shoot by default
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass


def is_image(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in IMAGE_EXTS


def is_video(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in VIDEO_EXTS


AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".webm", ".mka"}


def is_wav(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() == ".wav"


def is_audio(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in AUDIO_EXTS


def _decode_pcm(path: str | Path, rate: int, layout: str):
    """Yield raw 16-bit PCM byte blocks of any audio file (mp3, m4a, webm, ...), resampled to `rate`."""
    import av

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout=layout, rate=rate)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                yield bytes(out.planes[0])[: out.samples * out.format.bytes * len(out.layout.channels)]
        for out in resampler.resample(None):
            yield bytes(out.planes[0])[: out.samples * out.format.bytes * len(out.layout.channels)]


def audio_to_wav(path: str | Path, dest: str | Path) -> None:
    """Write any audio file as a CD-quality .wav so Windows can play it."""
    import wave

    with wave.open(str(dest), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        for block in _decode_pcm(path, 44100, "stereo"):
            w.writeframes(block)


def _wave_png(peaks: list[float], size: int, crop: bool) -> bytes:
    from PIL import Image, ImageDraw

    w_px, h_px = (size, size) if crop else (size, size // 2)
    im = Image.new("RGB", (w_px, h_px), "#e3eaf2")
    d, mid = ImageDraw.Draw(im), h_px / 2
    for x in range(w_px):
        p = peaks[x * len(peaks) // w_px] if peaks else 0
        d.line([(x, mid - max(1, p * mid * 0.9)), (x, mid + max(1, p * mid * 0.9))], fill="#2b6cb0")
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def make_audio_thumb(path: str | Path, size: int = THUMB, crop: bool = False) -> bytes | None:
    """Waveform PNG of any audio file, or None if it cannot be decoded."""
    try:
        import array

        samples = array.array("h")
        for block in _decode_pcm(path, 4000, "mono"):
            samples.extend(array.array("h", block[: len(block) - len(block) % 2]))
        if not samples:
            return None
        n = min(len(samples), max(size, 300))
        step = len(samples) / n
        peaks = [max(map(abs, samples[int(i * step): max(int(i * step) + 1, int((i + 1) * step))])) / 32768 for i in range(n)]
        return _wave_png(peaks, size, crop)
    except Exception:
        return None


def make_wave_thumb(path: str | Path, size: int = THUMB, crop: bool = False) -> bytes | None:
    """PNG picture of a .wav file's loudness over time (a mirrored bar per slice), or None if it cannot be read."""
    try:
        import array
        import wave

        from PIL import Image, ImageDraw

        with wave.open(str(path), "rb") as w:
            width, chans, frames = w.getsampwidth(), w.getnchannels(), w.getnframes()
            if width not in (1, 2) or not frames:  # 8 and 16 bit only; others keep the placeholder
                return None
            w_px, h_px = (size, size) if crop else (size, size // 2)
            peaks = []
            per = max(1, frames // w_px)
            for _ in range(w_px):
                data = w.readframes(per)
                if not data:
                    break
                if width == 2:
                    s = array.array("h", data[: len(data) - len(data) % 2])
                    top = max(map(abs, s[::chans]), default=0) / 32768
                else:
                    top = max((abs(b - 128) for b in data[::chans]), default=0) / 128
                peaks.append(top)
        im = Image.new("RGB", (w_px, h_px), "#e3eaf2")
        d, mid = ImageDraw.Draw(im), h_px / 2
        for x, p in enumerate(peaks):
            d.line([(x, mid - max(1, p * mid * 0.9)), (x, mid + max(1, p * mid * 0.9))], fill="#2b6cb0")
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()
    except Exception:
        return None


def _to_png(im, size: int, crop: bool) -> bytes:
    from PIL import ImageOps

    if crop:
        im = ImageOps.fit(im, (size, size))
    else:
        im.thumbnail((size, size))
    out = io.BytesIO()
    im.convert("RGB").save(out, "PNG")
    return out.getvalue()


def make_thumb(data: bytes, size: int = THUMB, crop: bool = False) -> bytes | None:
    """Return a PNG no larger than size x size (exactly size x size when crop fills the square from the
    centre), or None if the bytes are not an image we can decode."""
    try:
        from PIL import Image, ImageOps

        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (size * 2, size * 2))  # JPEG: decode at reduced scale, much faster
        return _to_png(ImageOps.exif_transpose(im), size, crop)
    except Exception:
        return None


def make_video_thumb(path: str | Path, size: int = THUMB, crop: bool = False) -> bytes | None:
    """PNG of a frame near the start of a .mov/.mp4 file on disk, or None if it cannot be decoded."""
    try:
        import av

        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            if container.duration:  # skip a possible black first frame, but stay within short clips
                container.seek(min(container.duration // 10, 1_000_000), stream=stream)
            for frame in container.decode(stream):
                return _to_png(frame.to_image(), size, crop)
    except Exception:
        pass
    return None


def local_thumb(path: str | Path, size: int = THUMB, crop: bool = False) -> bytes | None:
    """Thumbnail of a file on this PC (photo or video), or None."""
    p = Path(path)
    try:
        if is_video(p.name):
            return make_video_thumb(p, size, crop)
        if is_audio(p.name):
            # a video frame or cover art if the file has one (YouTube .webm, mp3 with art), else the waveform
            return make_video_thumb(p, size, crop) or make_wave_thumb(p, size, crop) or make_audio_thumb(p, size, crop)
        if is_image(p.name) and p.stat().st_size <= MAX_BYTES:
            return make_thumb(p.read_bytes(), size, crop)
    except OSError:
        pass
    return None


def _cache_file(f: RemoteFile, cache_dir: Path, size: int = THUMB, crop: bool = False) -> Path:
    key = hashlib.sha1(f"{f.path}|{f.size}|{f.mtime}|{size}|{'sq' if crop else ''}".encode()).hexdigest()
    return cache_dir / f"{key}.png"


async def load_thumb(source: Source, f: RemoteFile, cache_dir: Path = CACHE_DIR, size: int = THUMB,
                     crop: bool = False) -> bytes | None:
    """PNG thumbnail for f, from the cache if we have one, else read from the source. None if not possible."""
    video = is_video(f.path)
    if not (is_image(f.path) and f.size <= MAX_BYTES) and not (video and f.size <= VIDEO_MAX_BYTES):
        return None
    cached = _cache_file(f, cache_dir, size, crop)
    try:
        return cached.read_bytes()
    except OSError:
        pass
    loop = asyncio.get_running_loop()
    if video:  # decoders need a seekable file (the index of an iPhone video is at its end), so stage it on disk
        fd, tmp = tempfile.mkstemp(suffix=Path(f.path).suffix)
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in source.read(f.path):
                    out.write(chunk)
            png = await loop.run_in_executor(None, make_video_thumb, tmp, size, crop)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    else:
        buf = bytearray()
        async for chunk in source.read(f.path):
            buf += chunk
        png = await loop.run_in_executor(None, make_thumb, bytes(buf), size, crop)
    if png:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(png)
        except OSError:
            pass  # cache is an optimization only
    return png
