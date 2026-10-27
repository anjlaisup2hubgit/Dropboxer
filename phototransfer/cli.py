"""Command line: phototransfer devices | list | copy | send | share | gui"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from .engine import copy_files, upload_files
from .sources.base import Source

DEFAULT_ROOT = "/DCIM"
DEFAULT_UPLOAD_ROOT = "/PhotoTransfer"  # not /DCIM: writing there does not add to Photos and can confuse it


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


async def _open_source(args) -> Source:
    if args.source_dir:  # fake "device" for testing without a phone
        from .sources.local import LocalSource

        return LocalSource(args.source_dir)
    from .sources.iphone import IPhoneSource

    try:
        return await IPhoneSource.connect(args.udid)
    except Exception as e:
        sys.exit(
            f"Could not connect to an iPhone: {type(e).__name__}: {e}\n"
            "Check: cable plugged in, phone unlocked, 'Trust This Computer' accepted, "
            "Apple Mobile Device Service running."
        )


async def cmd_devices(args) -> int:
    from .sources.iphone import list_devices

    devs = await list_devices()
    if not devs:
        print("No iPhone found over USB. Plug it in, unlock it, and tap 'Trust'.")
        return 1
    for d in devs:
        print(f"{d['udid']}  ({d['connection']})")
    return 0


async def cmd_list(args) -> int:
    async with await _open_source(args) as src:
        files = await src.list_files(args.root)
    total = sum(f.size for f in files)
    for f in files:
        when = datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")
        print(f"{when}  {_human(f.size):>10}  {f.path}")
    print(f"\n{len(files)} files, {_human(total)}")
    return 0


async def cmd_copy(args) -> int:
    async with await _open_source(args) as src:
        files = await src.list_files(args.root)
        if args.limit:
            files = files[: args.limit]
        print(f"{len(files)} files, {_human(sum(f.size for f in files))} -> {args.dest}")

        last = {"path": None}

        def progress(path: str, done: int, total: int) -> None:
            pct = 100 * done // total if total else 100
            end = "\n" if done >= total else "\r"
            print(f"  {path}  {pct:3d}%  {_human(done)}/{_human(total)}   ", end=end, flush=True)

        summary = await copy_files(src, files, args.root, Path(args.dest), progress, jobs=args.jobs, by_date=args.by_date)

    print(
        f"\ncopied {len(summary.copied)} ({_human(summary.bytes_copied)}), "
        f"skipped {len(summary.skipped)} already done, failed {len(summary.failed)}"
    )
    for rel, err in summary.failed:
        print(f"  FAILED {rel}: {err}", file=sys.stderr)
    if summary.aborted:
        print(f"Stopped early: {summary.aborted}\nReconnect the phone, unlock it, and re-run the same command to resume.", file=sys.stderr)
    return 1 if summary.failed else 0


async def cmd_send(args) -> int:
    local = Path(args.path)
    if local.is_file():
        files, root = [local], local.parent
    elif local.is_dir():
        files, root = [p for p in local.rglob("*") if p.is_file()], local
    else:
        sys.exit(f"Not found: {local}")
    if args.limit:
        files = sorted(files)[: args.limit]
    async with await _open_source(args) as dst:
        print(f"{len(files)} files, {_human(sum(p.stat().st_size for p in files))} -> {args.to}")

        def progress(path: str, done: int, total: int) -> None:
            pct = 100 * done // total if total else 100
            end = "\n" if done >= total else "\r"
            print(f"  {path}  {pct:3d}%  {_human(done)}/{_human(total)}   ", end=end, flush=True)

        summary = await upload_files(dst, files, root, args.to, progress)

    print(
        f"\nsent {len(summary.copied)} ({_human(summary.bytes_copied)}), "
        f"skipped {len(summary.skipped)} already there, failed {len(summary.failed)}"
    )
    for rel, err in summary.failed:
        print(f"  FAILED {rel}: {err}", file=sys.stderr)
    if summary.aborted:
        print(f"Stopped early: {summary.aborted}\nReconnect the phone, unlock it, and re-run the same command.", file=sys.stderr)
    return 1 if summary.failed else 0


def cmd_share(args) -> int:
    import time

    from .wifi import WifiServer, persistent_token

    share = Path(args.folder)
    if not share.is_dir():
        sys.exit(f"Not a folder: {share}")
    inbox = Path(args.inbox) if args.inbox else share
    server = WifiServer(share, inbox, args.port, persistent_token(args.new_token), tls=args.https)
    try:
        url = server.start()
    except OSError as e:
        sys.exit(f"Could not start the server on port {args.port}: {e}")
    print(f"Sharing {share}\nUploads from the phone go to {inbox}\n\nOn your phone (same Wi-Fi), open:\n  {url}\n")
    if server.cert_url:
        print("First time with --https: install and trust the certificate on the phone (see README), from:\n"
              f"  {server.cert_url}\n")
    if server.remote_url:
        print(f"Away from home (phone has Tailscale on, same account):\n  {server.remote_url}\n")
    try:
        import segno

        segno.make(url, error="m").terminal(compact=True)
    except ImportError:
        pass
    print("\nPlain HTTP on your local network: use a network you trust. Press Ctrl+C to stop.")
    print("If Windows asks, allow Python through the firewall on Private networks.")
    seen = 0
    try:
        while True:
            time.sleep(0.5)
            while seen < len(server.received):
                print(f"  received {server.received[seen]}")
                seen += 1
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    print(f"\nStopped. {len(server.received)} files received.")
    return 0


def cmd_tray(args) -> int:
    from .gui import _load_settings
    from .tray import run_tray

    folder = Path(args.folder or _load_settings().get("dest") or Path.home() / "Pictures" / "iPhone")
    return run_tray(folder, args.port)


def cmd_gui(args) -> int:
    from .gui import run  # tkinter runs its own loop, so this is not async like the others

    return run(args.source_dir, args.udid)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="phototransfer", description="Copy photos/videos from an iPhone over USB.")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--udid", help="Device UDID (default: the connected device)")
        sp.add_argument("--root", default=DEFAULT_ROOT, help=f"Folder on the device (default {DEFAULT_ROOT})")
        sp.add_argument("--source-dir", help="Use a local folder instead of an iPhone (for testing)")

    sub.add_parser("devices", help="List USB-connected iPhones").set_defaults(fn=cmd_devices)
    sp = sub.add_parser("list", help="List camera-roll files")
    common(sp)
    sp.set_defaults(fn=cmd_list)
    sp = sub.add_parser("copy", help="Copy camera-roll files to a folder (resumable, SHA-256 verified)")
    common(sp)
    sp.add_argument("dest", help="Destination folder")
    sp.add_argument("--limit", type=int, help="Only copy the first N files")
    sp.add_argument("--jobs", type=int, default=1, help="Files to copy in parallel (default 1; try 4)")
    sp.add_argument("--by-date", action="store_true", help="Organize into YYYY/YYYY-MM folders instead of the device layout")
    sp.set_defaults(fn=cmd_copy)
    sp = sub.add_parser("send", help="Send a file or folder from this PC to the phone (SHA-256 verified)")
    sp.add_argument("--udid", help="Device UDID (default: the connected device)")
    sp.add_argument("--source-dir", help="Use a local folder as the 'phone' instead of an iPhone (for testing)")
    sp.add_argument("path", help="File or folder on this PC")
    sp.add_argument("--to", default=DEFAULT_UPLOAD_ROOT, help=f"Folder on the device (default {DEFAULT_UPLOAD_ROOT})")
    sp.add_argument("--limit", type=int, help="Only send the first N files")
    sp.set_defaults(fn=cmd_send)
    sp = sub.add_parser("share", help="Share a folder over Wi-Fi: view photos on a phone browser and upload from it")
    sp.add_argument("folder", help="Folder of photos to show on the phone")
    sp.add_argument("--inbox", help="Where uploads from the phone are saved (default: the shared folder itself)")
    sp.add_argument("--port", type=int, default=8765, help="Port to listen on (default 8765)")
    sp.add_argument("--https", action="store_true", help="Serve over HTTPS with a self-signed certificate (needed to save straight to Photos)")
    sp.add_argument("--new-token", action="store_true", help="Make a new secret link (old links, bookmarks and home-screen icons stop working)")
    sp.set_defaults(fn=cmd_share)
    sp = sub.add_parser("tray", help="Run the Wi-Fi share in the background from a system-tray icon (default when started with no command)")
    sp.add_argument("folder", nargs="?", help="Folder to share (default: the GUI's 'Copy to' folder)")
    sp.add_argument("--port", type=int, default=8765, help="Port to listen on (default 8765)")
    sp.set_defaults(fn=cmd_tray)
    sp = sub.add_parser("gui", help="Open the window: browse, select and copy")
    sp.add_argument("--udid", help="Device UDID (default: the connected device)")
    sp.add_argument("--source-dir", help="Use a local folder instead of an iPhone (for testing)")
    sp.set_defaults(fn=cmd_gui)

    args = p.parse_args(["tray"] if argv is None and len(sys.argv) == 1 else argv)
    try:
        if args.cmd in ("gui", "share", "tray"):  # gui runs its own loop; share is a blocking server
            return args.fn(args)
        return asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run the same command to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
