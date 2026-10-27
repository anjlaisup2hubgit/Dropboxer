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
    @abstractmethod
    async def list_files(self, root: str) -> list[RemoteFile]:
        """Recursively list regular files under root."""

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
