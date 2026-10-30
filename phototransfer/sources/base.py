"""Source interface. Any transport (USB/AFC, Wi-Fi, Android, local dir) implements this."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Iterable


@dataclass(frozen=True)
class RemoteFile:
    path: str      # POSIX-style path on the source, e.g. "/DCIM/100APPLE/IMG_0001.HEIC"
    size: int
    mtime: float   # unix seconds


class Source(ABC):
    roots: tuple[str, ...] = ("/DCIM",)  # where this kind of device keeps its photos and videos

    @abstractmethod
    async def list_files(self, root: str) -> list[RemoteFile]:
        """Recursively list regular files under root."""

    async def list_many(self, roots: Iterable[str]) -> list[RemoteFile]:
        """list_files over several folders, merged, sorted and without repeats. A folder the device lacks is skipped."""
        seen: dict[str, RemoteFile] = {}
        failed: list[FileNotFoundError] = []
        roots = list(roots)
        for root in roots:
            try:
                for f in await self.list_files(root):
                    seen.setdefault(f.path, f)
            except FileNotFoundError as e:
                failed.append(e)
        if failed and len(failed) == len(roots):  # nothing could be read at all, e.g. the source folder is gone
            raise failed[0]
        return sorted(seen.values(), key=lambda f: f.path)

    async def icloud_photos_on(self) -> bool:
        """True when the device looks like it keeps photos in iCloud, so some originals may not be on it."""
        return False

    @abstractmethod
    def read(self, path: str, offset: int = 0, chunk_size: int = 1 << 20) -> AsyncIterator[bytes]:
        """Yield the file's bytes starting at offset."""

    async def stat(self, path: str) -> RemoteFile | None:
        """Return the file's info, or None if it does not exist. Needed for uploads."""
        raise NotImplementedError(f"{type(self).__name__} cannot be written to")

    async def write(self, path: str, chunks: Iterable[bytes]) -> None:
        """Write chunks to path (creating folders), via a temporary name, renamed into place when complete.

        On any error the temporary file is removed and the exception re-raised.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot be written to")

    async def remove(self, path: str) -> None:
        raise NotImplementedError(f"{type(self).__name__} cannot be written to")

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> "Source":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
