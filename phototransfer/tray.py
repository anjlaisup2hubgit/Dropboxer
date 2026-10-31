"""System-tray app: keeps the Wi-Fi share running in the background with no window. Right-click the tray
icon for the phone link, a QR code, the photos folder, the full window, and "Start with Windows"."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .wifi import WifiServer, persistent_token, qr_png

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "PhoneTransfer"


def launch_command() -> list[str]:
    """How to start this app again: the packaged .exe itself, or this Python with -m."""
    return [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "phototransfer"]


def autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except (ImportError, OSError):
        return False


def set_autostart(on: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(launch_command() + ["tray"]))
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass


def _copy(text: str) -> None:
    subprocess.run(["clip"], input=text.encode("utf-16"), check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def run_tray(folder: Path, port: int = 8765) -> int:
    import io

    import pystray
    from PIL import Image

    from .wifi import app_icon

    folder.mkdir(parents=True, exist_ok=True)
    server = WifiServer(folder, folder, port, persistent_token())
    try:
        server.start()
    except OSError as e:
        print(f"Could not start on port {port}: {e} (is it already running?)", file=sys.stderr)
        return 1

    def show_qr(icon, item):
        path = Path(tempfile.gettempdir()) / "phototransfer_qr.png"
        path.write_bytes(qr_png(server.url, 8))
        os.startfile(path)

    def toggle_autostart(icon, item):
        set_autostart(not autostart_enabled())

    def open_window(icon, item):
        subprocess.Popen(launch_command() + ["gui"])

    def quit_(icon, item):
        server.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Show QR code for the phone", show_qr, default=True),
        pystray.MenuItem("Copy phone link", lambda i, it: _copy(server.url)),
        pystray.MenuItem("Copy away-from-home link (Tailscale)", lambda i, it: _copy(server.remote_url or ""),
                         visible=lambda it: server.remote_url is not None),
        pystray.MenuItem("Open photos folder", lambda i, it: os.startfile(folder)),
        pystray.MenuItem("Open full window (USB copy)", open_window),
        pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda it: autostart_enabled()),
        pystray.MenuItem("Quit", quit_),
    )
    icon = pystray.Icon("phototransfer", Image.open(io.BytesIO(app_icon(64, True))), "Boxer", menu)

    def watch_received() -> None:
        import time

        seen = 0
        while icon.visible or seen == 0:
            time.sleep(1)
            if seen < len(server.received):
                n = len(server.received) - seen
                seen = len(server.received)
                try:
                    icon.notify(f"{n} file(s) received", "Boxer")
                except Exception:
                    pass
            if server._httpd is None:
                break

    import threading

    threading.Thread(target=watch_received, daemon=True).start()
    icon.run()
    return 0
