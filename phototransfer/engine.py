"""Transport-independent copy engine: resume, SHA-256 verification, timestamp preservation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable

from .sources.base import RemoteFile, Source

MANIFEST_NAME = ".phototransfer.json"
PART_SUFFIX = ".part"
HASH_BLOCK = 1 << 20
MAX_CONSECUTIVE_FAILURES = 3

Progress = Callable[[str, int, int], None]  # (path, bytes_done, bytes_total)


class Cancelled(Exception):
    """Raised from the progress hook to stop one file when the caller asks to cancel."""


@dataclass
class Summary:
    copied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    bytes_copied: int = 0
    aborted: str | None = None  # set when copying stopped early (e.g. device unplugged)


def rel_path(file: RemoteFile, root: str) -> PurePosixPath:
    return PurePosixPath(file.path).relative_to(PurePosixPath(root))


def sha256_file(path: Path, limit: int | None = None) -> "hashlib._Hash":
    h = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as f:
        while True:
            n = HASH_BLOCK if remaining is None else min(HASH_BLOCK, remaining)
            if n == 0:
                break
            block = f.read(n)
            if not block:
                break
            h.update(block)
            if remaining is not None:
                remaining -= len(block)
    return h


def set_times(path: Path, mtime: float) -> None:
    """Set modified time everywhere, and the creation time too on Windows."""
    os.utime(path, (mtime, mtime))
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    ticks = int((mtime + 11644473600) * 10_000_000)  # unix seconds -> 100ns since 1601
    ft = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
    FILE_WRITE_ATTRIBUTES, SHARE_ALL, OPEN_EXISTING = 0x100, 7, 3
    handle = k32.CreateFileW(str(path), FILE_WRITE_ATTRIBUTES, SHARE_ALL, None, OPEN_EXISTING, 0x02000000, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not k32.SetFileTime(handle, ctypes.byref(ft), None, ctypes.byref(ft)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        k32.CloseHandle(handle)


async def _read_range(source: Source, path: str, offset: int, length: int) -> bytes:
    buf = bytearray()
    async for chunk in source.read(path, offset):
        buf += chunk
        if len(buf) >= length:
            break
    return bytes(buf[:length])


async def _partial_matches_source(source: Source, f: RemoteFile, part: Path, offset: int) -> bool:
    """Spot-check an existing .part against the device: first and last block must match."""
    n = min(HASH_BLOCK, offset)
    spans = [0] if offset <= n else [0, offset - n]
    with open(part, "rb") as fh:
        for start in spans:
            fh.seek(start)
            if fh.read(n) != await _read_range(source, f.path, start, n):
                return False
    return True


def pair_live_photos(files: list[RemoteFile]) -> dict[str, str]:
    """Map each Live Photo's .MOV to its .HEIC/.JPG still (same folder, same stem)."""
    stills = {}
    for f in files:
        p = PurePosixPath(f.path)
        if p.suffix.lower() in (".heic", ".jpg", ".jpeg"):
            stills[(p.parent, p.stem.lower())] = f.path
    pairs = {}
    for f in files:
        p = PurePosixPath(f.path)
        if p.suffix.lower() == ".mov" and (p.parent, p.stem.lower()) in stills:
            pairs[f.path] = stills[(p.parent, p.stem.lower())]
    return pairs


def _load_manifest(dest: Path) -> dict:
    try:
        return json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save_manifest(dest: Path, manifest: dict) -> None:
    tmp = dest / (MANIFEST_NAME + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, dest / MANIFEST_NAME)


def _already_done(entry: dict | None, target: Path, f: RemoteFile) -> bool:
    return bool(
        entry
        and target.exists()
        and entry.get("size") == f.size
        and abs(entry.get("mtime", -1) - f.mtime) < 1
        and target.stat().st_size == f.size
    )


async def copy_file(source: Source, f: RemoteFile, target: Path, progress: Progress | None = None) -> str:
    """Copy one file with resume. Returns its SHA-256 hex digest. Raises on any verification failure."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + PART_SUFFIX)

    offset = part.stat().st_size if part.exists() else 0
    if offset > f.size:
        offset = 0  # stale/garbage partial file: start over
    if offset and not await _partial_matches_source(source, f, part, offset):
        offset = 0  # partial does not match the device's file: start over
    # Resume: rehash what we already have so the final digest covers the whole file.
    h = sha256_file(part, limit=offset) if offset else hashlib.sha256()
    if offset == 0 and part.exists():
        part.unlink()

    done = offset
    with open(part, "ab" if offset else "wb") as out:
        if offset < f.size:
            async for chunk in source.read(f.path, offset):
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(f.path, done, f.size)
        out.flush()
        os.fsync(out.fileno())

    if part.stat().st_size != f.size:
        raise IOError(f"size mismatch: expected {f.size}, got {part.stat().st_size}")
    if sha256_file(part).hexdigest() != h.hexdigest():  # catches write/disk corruption
        part.unlink()
        raise IOError("hash mismatch after write; partial file discarded")

    os.replace(part, target)
    set_times(target, f.mtime)
    return h.hexdigest()


def plan_paths(files: list[RemoteFile], root: str, by_date: bool = False) -> dict[str, str]:
    """Map each file's device path to its relative destination path.

    By default the device layout under root is kept. With by_date, files go to YYYY/YYYY-MM/name;
    a name used twice (the same IMG number in two device folders) gets its device folder as a prefix.
    """
    if not by_date:
        return {f.path: str(rel_path(f, root)) for f in files}
    plan = {}
    for f in files:
        d = datetime.fromtimestamp(f.mtime)
        plan[f.path] = f"{d:%Y}/{d:%Y-%m}/{PurePosixPath(f.path).name}"
    counts = Counter(plan.values())
    for f in files:
        if counts[plan[f.path]] > 1:
            p = PurePosixPath(f.path)
            head, _, name = plan[f.path].rpartition("/")
            plan[f.path] = f"{head}/{p.parent.name}_{name}"
    return plan


async def copy_files(
    source: Source,
    files: list[RemoteFile],
    root: str,
    dest: Path,
    progress: Progress | None = None,
    jobs: int = 1,
    by_date: bool = False,
    cancel: Callable[[], bool] | None = None,
) -> Summary:
    dest.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(dest)
    summary = Summary()
    pairs = pair_live_photos(files)
    plan = plan_paths(files, root, by_date)
    sem = asyncio.Semaphore(max(1, jobs))
    streak = 0  # consecutive failures; a few in a row means the device is gone

    def checked_progress(path: str, done: int, total: int) -> None:
        if cancel and cancel():
            raise Cancelled()  # leaves the .part file in place so a re-run resumes it
        if progress:
            progress(path, done, total)

    async def one(f: RemoteFile) -> None:
        nonlocal streak
        rel = plan[f.path]
        target = dest / rel
        if _already_done(manifest.get(rel), target, f):
            summary.skipped.append(rel)
            if progress:  # count it as done so a resumed run's progress bar still reaches 100%
                progress(f.path, f.size, f.size)
            return
        async with sem:
            if summary.aborted or (cancel and cancel()):
                return
            try:
                digest = await copy_file(source, f, target, checked_progress)
            except Cancelled:
                return
            except Exception as e:  # keep going; report at the end
                summary.failed.append((rel, f"{type(e).__name__}: {e}"))
                part = target.with_name(target.name + PART_SUFFIX)
                if part.exists() and part.stat().st_size == 0:
                    part.unlink()
                streak += 1
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    summary.aborted = f"{streak} files failed in a row ({type(e).__name__}); device disconnected or locked?"
                return
            streak = 0
        entry = {"size": f.size, "mtime": f.mtime, "sha256": digest}
        if f.path in pairs:
            still = pairs[f.path]
            entry["live_photo_of"] = plan.get(still) or str(rel_path(RemoteFile(still, 0, 0), root))
        manifest[rel] = entry
        _save_manifest(dest, manifest)
        summary.copied.append(rel)
        summary.bytes_copied += f.size

    await asyncio.gather(*(one(f) for f in files))
    return summary


async def upload_file(dest: Source, local: Path, remote: str, progress: Progress | None = None) -> str:
    """Send one local file to the device, then read it back and check SHA-256. Returns the digest."""
    size = local.stat().st_size
    h = hashlib.sha256()

    def chunks():
        done = 0
        with open(local, "rb") as f:
            while block := f.read(HASH_BLOCK):
                h.update(block)
                done += len(block)
                if progress:
                    progress(remote, done, size)
                yield block

    await dest.write(remote, chunks())
    back = hashlib.sha256()
    async for block in dest.read(remote):
        back.update(block)
    if back.hexdigest() != h.hexdigest():
        await dest.remove(remote)
        raise IOError("hash mismatch after upload; file removed from the device")
    return h.hexdigest()


async def upload_files(
    dest: Source,
    paths: list[Path],
    local_root: Path,
    remote_root: str,
    progress: Progress | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Summary:
    """Send local files to remote_root on the device, keeping their layout under local_root.

    A file already on the device with the same size is skipped (the device does not let us set its time).
    """
    summary = Summary()
    streak = 0

    def checked_progress(path: str, done: int, total: int) -> None:
        if cancel and cancel():
            raise Cancelled()
        if progress:
            progress(path, done, total)

    for p in sorted(paths):
        rel = p.relative_to(local_root).as_posix()
        remote = f"{remote_root.rstrip('/')}/{rel}"
        if cancel and cancel():
            break
        existing = await dest.stat(remote)
        if existing and existing.size == p.stat().st_size:
            summary.skipped.append(rel)
            if progress:
                progress(remote, existing.size, existing.size)
            continue
        try:
            await upload_file(dest, p, remote, checked_progress)
        except Cancelled:
            break
        except Exception as e:
            summary.failed.append((rel, f"{type(e).__name__}: {e}"))
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES:
                summary.aborted = f"{streak} files failed in a row ({type(e).__name__}); device disconnected or locked?"
                break
            continue
        streak = 0
        summary.copied.append(rel)
        summary.bytes_copied += p.stat().st_size
    return summary
