"""Android phones, tablets and cameras in MTP/PTP mode ("File transfer"): they get no drive letter, so they are
reached through the Windows shell (the same view as Explorer's This PC) with pywin32.

The shell objects belong to one thread, so every call runs on a single worker thread. The shell cannot stream a file,
so a file is copied to a temporary folder when it is first read, and removed again once nothing needs it.
Read-only, like a memory card.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from ..log import log
from .base import RemoteFile, Source

THIS_PC = 17  # CSIDL_DRIVES
COPY_FLAGS = 4 | 16 | 512 | 1024  # no progress box, yes to all, no folder prompt, no error box
STALL_SECONDS = 60  # a copy that grows by nothing for this long has failed
ARCHIVES = (".zip", ".cab")  # the shell shows these as folders; in a phone they are files

_pool: ThreadPoolExecutor | None = None


def _com_thread() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        import pythoncom

        _pool = ThreadPoolExecutor(1, thread_name_prefix="mtp", initializer=pythoncom.CoInitialize)
    return _pool


async def _on_com(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_com_thread(), fn, *args)


@dataclass(frozen=True)
class MtpDevice:
    name: str   # e.g. "Pixel 7"
    path: str   # the shell's id for it

    def __str__(self) -> str:
        return self.name


def _shell():
    import win32com.client

    return win32com.client.Dispatch("Shell.Application")


def _children(folder_item) -> list:
    items = folder_item.GetFolder.Items()
    return [items.Item(i) for i in range(items.Count)]


def _name(item) -> str:
    """The real file name; Name alone leaves the extension off when Explorer is set to hide extensions."""
    try:
        return item.ExtendedProperty("System.FileName") or item.Name
    except Exception:
        return item.Name


def _is_folder(item) -> bool:
    return bool(item.IsFolder) and not _name(item).lower().endswith(ARCHIVES)


def _mtime(item) -> float:
    try:
        return item.ModifyDate.timestamp()
    except Exception:
        return time.time()


def _is_portable(item) -> bool:
    p = item.Path.lower()
    return bool(item.IsFolder) and p.startswith("::") and ("usb#" in p or "\\\\?\\" in p)


def _find_devices() -> list[MtpDevice]:
    items = _shell().NameSpace(THIS_PC).Items()
    return [MtpDevice(it.Name, it.Path) for it in (items.Item(i) for i in range(items.Count)) if _is_portable(it)]


async def list_mtp_devices() -> list[MtpDevice]:
    """Phones and cameras Windows shows under This PC without a drive letter. Empty off Windows or without pywin32."""
    try:
        return await _on_com(_find_devices)
    except Exception as e:
        log.info("list_mtp_devices: %r", e)
        return []


class MtpSource(Source):
    roots = ("/DCIM", "/Pictures", "/Movies", "/WhatsApp", "/Telegram")  # Android keeps screenshots, downloads and chat media outside DCIM

    def __init__(self, device: MtpDevice):
        self.device = device
        self._index: dict[str, object] = {}   # file path -> shell item, filled by list_files
        self._staged: dict[str, list] = {}    # path -> [local copy, readers using it]
        self._last: str | None = None
        self._tmp = Path(tempfile.mkdtemp(prefix="dropboxer-mtp-"))
        self._n = 0

    # ---- on the COM thread --------------------------------------------------------------------
    def _storages(self) -> list:
        shell = _shell()
        items = shell.NameSpace(THIS_PC).Items()
        dev = next((items.Item(i) for i in range(items.Count) if items.Item(i).Path == self.device.path), None)
        if dev is None:
            raise ConnectionError(f"{self.device.name} is no longer connected.")
        found = _children(dev)
        if not found:
            raise PermissionError(f"{self.device.name} shows no storage. Unlock it and choose File transfer in its USB notification.")
        return found

    def _list_many(self, roots) -> list[RemoteFile]:
        self._index = {}
        seen: dict[str, RemoteFile] = {}
        storages = self._storages()
        for root in roots:
            for f in self._list(root, storages):
                seen.setdefault(f.path, f)
        return sorted(seen.values(), key=lambda f: f.path)

    def _list(self, root: str, storages: list) -> list[RemoteFile]:
        root = "/" + root.strip("/")
        out: list[RemoteFile] = []
        for n, storage in enumerate(storages):
            top = storage
            for part in [p for p in root.split("/") if p]:  # go down to the root folder inside this storage
                top = next((c for c in _children(top) if _name(c).lower() == part.lower() and _is_folder(c)), None)
                if top is None:
                    break
            if top is None:
                continue
            vpath = root if n == 0 else f"{root}/{_name(storage)}"  # extra storages (an SD card) sit under the root
            stack = [(top, vpath)]
            while stack:
                folder, vp = stack.pop()
                for child in _children(folder):
                    name = _name(child)
                    if name.startswith("."):  # .thumbnails, .trashed-...
                        continue
                    if _is_folder(child):
                        stack.append((child, f"{vp}/{name}"))
                    else:
                        path = f"{vp}/{name}"
                        self._index[path] = child
                        out.append(RemoteFile(path, int(child.Size), _mtime(child)))
        return sorted(out, key=lambda f: f.path)

    def _copy_out(self, item, size: int) -> Path:
        """Copy one file out of the phone into a fresh temporary folder and wait for it to be whole."""
        self._n += 1
        dest = self._tmp / str(self._n)
        dest.mkdir()
        free = shutil.disk_usage(self._tmp).free
        if free < size + (64 << 20):
            raise IOError(f"Not enough room on the temp drive to copy {_name(item)} from the phone ({size >> 20} MB needed, {free >> 20} MB free).")
        _shell().NameSpace(str(dest)).CopyHere(item, COPY_FLAGS)
        import pythoncom

        last, moved = -1, time.monotonic()
        while True:
            pythoncom.PumpWaitingMessages()
            files = [p for p in dest.iterdir() if p.is_file()]
            got = files[0].stat().st_size if files else -1
            if files and got == size and got == last:
                return files[0]
            if got != last:
                last, moved = got, time.monotonic()
            elif time.monotonic() - moved > STALL_SECONDS:
                raise IOError(f"Copying {_name(item)} from the phone stalled ({got} of {size} bytes). Is it still unlocked?")
            time.sleep(0.2)

    def _stage_sync(self, path: str) -> Path:
        item = self._index.get(path)
        if item is None:
            raise FileNotFoundError(f"{path} was not listed on {self.device.name}")
        return self._copy_out(item, int(item.Size))

    # ---- Source interface ---------------------------------------------------------------------
    async def list_files(self, root: str) -> list[RemoteFile]:
        return await self.list_many([root])

    async def list_many(self, roots) -> list[RemoteFile]:
        return await _on_com(self._list_many, list(roots))

    async def read(self, path: str, offset: int = 0, chunk_size: int = 1 << 20) -> AsyncIterator[bytes]:
        entry = self._staged.get(path)
        if entry is None:
            entry = self._staged[path] = [await _on_com(self._stage_sync, path), 0]
        entry[1] += 1
        self._last = path
        try:
            with open(entry[0], "rb") as f:
                f.seek(offset)
                while chunk := f.read(chunk_size):
                    yield chunk
        finally:
            entry[1] -= 1
            self._evict()

    def _evict(self) -> None:
        """Drop copies nobody is reading, except the latest (a resume reads the same file again)."""
        for p, (local, users) in list(self._staged.items()):
            if users == 0 and p != self._last:
                shutil.rmtree(local.parent, ignore_errors=True)
                del self._staged[p]

    async def write(self, path, chunks) -> None:
        raise PermissionError("Phones opened this way are read-only; nothing is written to them.")

    async def remove(self, path: str) -> None:
        raise PermissionError("Phones opened this way are read-only; nothing is deleted from them.")

    async def close(self) -> None:
        self._staged.clear()
        self._index.clear()
        shutil.rmtree(self._tmp, ignore_errors=True)
