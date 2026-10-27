"""Local-directory source. Used for tests and as a reference implementation."""
from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncIterator, Iterable

from .base import RemoteFile, Source


class LocalSource(Source):
    def __init__(self, base: str | Path):
        self.base = Path(base)

    def _abs(self, path: str) -> Path:
        return self.base / path.lstrip("/")

    async def list_files(self, root: str) -> list[RemoteFile]:
        out = []
        top = self._abs(root)
        for dirpath, _, names in os.walk(top):
            for name in names:
                p = Path(dirpath) / name
                st = p.stat()
                rel = "/" + p.relative_to(self.base).as_posix()
                out.append(RemoteFile(rel, st.st_size, st.st_mtime))
        return sorted(out, key=lambda f: f.path)

    async def stat(self, path: str) -> RemoteFile | None:
        p = self._abs(path)
        if not p.is_file():
            return None
        st = p.stat()
        return RemoteFile(path, st.st_size, st.st_mtime)

    async def write(self, path: str, chunks: Iterable[bytes]) -> None:
        target = self._abs(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        try:
            with open(part, "wb") as f:
                for chunk in chunks:
                    f.write(chunk)
            os.replace(part, target)
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    async def remove(self, path: str) -> None:
        self._abs(path).unlink(missing_ok=True)

    async def read(self, path: str, offset: int = 0, chunk_size: int = 1 << 20) -> AsyncIterator[bytes]:
        with open(self._abs(path), "rb") as f:
            f.seek(offset)
            while chunk := f.read(chunk_size):
                yield chunk
