"""Memory cards, cameras and phones in USB-storage mode: any removable drive that holds a /DCIM folder.

They show up in Windows as a drive letter, so this is a local folder with two differences: it is found
automatically, and it is read-only (a card is never written to or deleted from by this app).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .local import LocalSource

DRIVE_REMOVABLE = 2
SEM_FAILCRITICALERRORS = 0x0001  # an empty card-reader slot fails quietly instead of showing "Insert a disk"


@dataclass(frozen=True)
class Drive:
    root: str    # e.g. "E:\\"
    label: str   # volume name, e.g. "EOS_DIGITAL"; may be empty

    def __str__(self) -> str:
        return f"{self.root[:2]} {self.label}".strip()


# Cameras that keep clips outside DCIM (Sony PRIVATE/M4ROOT, AVCHD, MP_ROOT), and photos in plain folders.
MEDIA_FOLDERS = ("DCIM", "PRIVATE", "MP_ROOT", "AVCHD", "BDMV", "Pictures", "Movies", "Videos")
MARKERS = ("DCIM", "PRIVATE", "MP_ROOT", "AVCHD")  # a removable drive with one of these is offered as a card


class DriveSource(LocalSource):
    """A drive's files, listed and read like a phone's; writing and deleting are refused."""

    roots = tuple("/" + f for f in MEDIA_FOLDERS)

    def __init__(self, root: str | Path):
        root = str(root)
        if len(root) == 2 and root[1] == ":":  # "E:" alone means the current folder on E:, not its top
            root += "\\"
        super().__init__(root)

    async def write(self, path, chunks) -> None:
        raise PermissionError("Memory cards are opened read-only; nothing is written to them.")

    async def remove(self, path: str) -> None:
        raise PermissionError("Memory cards are opened read-only; nothing is deleted from them.")


def _label(root: str) -> str:
    import ctypes

    buf = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(root, buf, len(buf), None, None, None, None, 0)
    return buf.value if ok else ""


def list_drives(folders: tuple[str, ...] = MARKERS) -> list[Drive]:
    """Removable drives that contain one of `folders` at the top. Empty off Windows, or when nothing is plugged in."""
    if os.name != "nt":
        return []
    import ctypes
    import string

    kernel = ctypes.windll.kernel32
    old = kernel.SetErrorMode(SEM_FAILCRITICALERRORS)
    try:
        mask = kernel.GetLogicalDrives()
        out = []
        for i, letter in enumerate(string.ascii_uppercase):
            root = f"{letter}:\\"
            if mask & (1 << i) and kernel.GetDriveTypeW(root) == DRIVE_REMOVABLE and any(os.path.isdir(root + f) for f in folders):
                out.append(Drive(root, _label(root)))
        return out
    finally:
        kernel.SetErrorMode(old)
