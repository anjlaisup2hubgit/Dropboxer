"""iPhone source: reads the camera roll (/DCIM) over USB using Apple's AFC service via pymobiledevice3."""
from __future__ import annotations

import posixpath
from typing import AsyncIterator, Iterable

from .base import RemoteFile, Source

READ_CHUNK = 4 * 1024 * 1024  # AFC max read size


class IPhoneSource(Source):
    def __init__(self, lockdown, afc, owns_lockdown: bool = True):
        self._lockdown = lockdown
        self._afc = afc
        self._owns_lockdown = owns_lockdown

    async def open_app(self, bundle_id: str) -> "IPhoneSource":
        """The Documents folder of an app that allows file sharing (VLC, Documents, ...), over the same connection."""
        from pymobiledevice3.services.house_arrest import HouseArrestService

        afc = await HouseArrestService.create(self._lockdown, bundle_id, documents_only=True)
        return IPhoneSource(self._lockdown, afc, owns_lockdown=False)

    @classmethod
    async def connect(cls, udid: str | None = None) -> "IPhoneSource":
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        # autopair=True asks the phone to show the "Trust This Computer?" prompt.
        lockdown = await create_using_usbmux(serial=udid, autopair=True, pair_timeout=60)
        afc = AfcService(lockdown)
        await afc.__aenter__()
        return cls(lockdown, afc)

    async def list_files(self, root: str = "/DCIM") -> list[RemoteFile]:
        out: list[RemoteFile] = []
        stack = [root.rstrip("/") or "/"]
        while stack:
            d = stack.pop()
            for name in await self._afc.listdir(d):
                p = f"{d.rstrip('/')}/{name}"
                st = await self._afc.stat(p)
                kind = st.get("st_ifmt")
                if kind == "S_IFDIR":
                    stack.append(p)
                elif kind == "S_IFREG":
                    mtime = st["st_mtime"]
                    ts = mtime.timestamp() if hasattr(mtime, "timestamp") else float(mtime)
                    out.append(RemoteFile(p, int(st["st_size"]), ts))
        return sorted(out, key=lambda f: f.path)

    async def read(self, path: str, offset: int = 0, chunk_size: int = READ_CHUNK) -> AsyncIterator[bytes]:
        size = int((await self._afc.stat(path))["st_size"])
        handle = await self._afc.fopen(path, "r")
        try:
            if offset:
                await self._afc.fseek(handle, offset)
            remaining = size - offset
            while remaining > 0:
                chunk = await self._afc.fread(handle, min(chunk_size, remaining))
                if not chunk:
                    raise IOError(f"Unexpected end of data reading {path}")
                remaining -= len(chunk)
                yield chunk
        finally:
            await self._afc.fclose(handle)

    async def stat(self, path: str) -> RemoteFile | None:
        from pymobiledevice3.exceptions import AfcFileNotFoundError

        try:
            st = await self._afc.stat(path)
        except AfcFileNotFoundError:
            return None
        if st.get("st_ifmt") != "S_IFREG":
            return None
        mtime = st["st_mtime"]
        ts = mtime.timestamp() if hasattr(mtime, "timestamp") else float(mtime)
        return RemoteFile(path, int(st["st_size"]), ts)

    async def write(self, path: str, chunks: Iterable[bytes]) -> None:
        part = path + ".part"
        folder = posixpath.dirname(path)
        if folder not in ("", "/") and not await self._afc.exists(folder):  # MAKE_DIR is refused on a folder that is already there
            await self._afc.makedirs(folder)
        handle = await self._afc.fopen(part, "w")
        try:
            for chunk in chunks:
                await self._afc.fwrite(handle, chunk)
        except BaseException:
            # A pause lands here mid-write. A failing clean-up must not hide it or leave the session half-open.
            for undo in (lambda: self._afc.fclose(handle), lambda: self._afc.rm_single(part, force=True)):
                try:
                    await undo()
                except Exception:
                    pass
            raise
        await self._afc.fclose(handle)
        await self._afc.rm_single(path, force=True)  # rename will not overwrite
        await self._afc.rename(part, path)

    async def remove(self, path: str) -> None:
        await self._afc.rm_single(path, force=True)

    async def close(self) -> None:
        await self._afc.aclose()
        if not self._owns_lockdown:
            return
        close =getattr(self._lockdown, "close", None)
        if close:
            res = close()
            if hasattr(res, "__await__"):
                await res


async def list_devices() -> list[dict]:
    """Return USB-attached iOS devices known to usbmuxd (Apple Mobile Device Service)."""
    from pymobiledevice3.usbmux import list_devices as _list

    return [
        {"udid": d.serial, "connection": d.connection_type}
        for d in await _list()
    ]
