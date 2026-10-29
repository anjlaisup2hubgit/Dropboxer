"""Desktop window (tkinter, no extra dependencies): browse the camera roll, pick files, copy them."""
from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import threading
import tkinter as tk
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk

from .cli import DEFAULT_ROOT, DEFAULT_UPLOAD_ROOT, _human
from .engine import copy_files, upload_files
from .sources.base import RemoteFile, Source
from .lang import LANGUAGES, T, language, set_language
from .thumbs import THUMB, audio_to_wav, is_audio, is_image, is_video, is_wav, load_thumb, local_thumb

SETTINGS_FILE = Path.home() / ".phototransfer" / "settings.json"
POLL_MS = 100
PAD = 0  # half the gap between tiles; the web page uses a 4 px gap
CELL = THUMB + 2 * PAD  # one grid cell: the thumbnail box plus padding
HEAD_H = 34  # height of a day heading
SCROLL_UNIT = 40  # pixels per scroll unit
WHEEL_UNITS = 3  # units per mouse wheel notch (about 120 px)
TIP_DELAY_MS = 350
PREVIEW = 900  # longest side of the picture opened by a double-click
MAX_PREVIEW = 3000  # most files listed from one folder in PC to iPhone
AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".webm", ".mka",
         ".aiff", ".aif", ".caf", ".amr", ".mid", ".midi"}  # .webm: YouTube audio downloads
DOCS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".csv", ".md", ".odt", ".ods", ".odp",
        ".pages", ".numbers", ".key", ".epub", ".mobi", ".azw", ".azw3", ".fb2", ".djvu", ".cbz", ".cbr"}
ARCHIVES = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".lz4", ".tbz2", ".txz", ".cab", ".iso", ".dmg", ".exe", ".msi", ".apk", ".jar"}
PHOTOS = {".heic", ".jpg", ".jpeg", ".png", ".dng", ".gif", ".tiff", ".webp"}
VIDEOS = {".mov", ".mp4", ".m4v"}
KINDS = {
    "Gallery": PHOTOS | VIDEOS,
    "Music": AUDIO,
    "Documents": DOCS,
    "Archives": ARCHIVES,
    "All": None,
}
MANUAL = [
    ("h", "Selecting"),
    ("p", "Click a picture to select it; click it again to unselect."),
    ("p", "Shift-click selects everything between your last click and this one (if the last click unselected, the range unselects)."),
    ("p", "Click a day's heading to select all of that day; click it again to unselect the whole day."),
    ("p", "Hold the left button and drag a box to select every picture it touches. Drag a box over pictures that are already selected (or start it on one) to unselect them."),
    ("p", "Drag past the top or bottom of the list to scroll while boxing."),
    ("p", "Double-click a picture to open a larger preview; photos, videos and music play there."),
    ("p", "Ctrl+A selects all shown files, Esc clears the selection. Untick Select all above the list to clear it too."),
    ("h", "File types"),
    ("p", "Gallery shows photos and videos together; Music, Documents and Archives show those kinds; All shows everything."),
    ("h", "Icons on pictures"),
    ("p", "A small badge in the corner of a tile tells you what kind of file it is. Photos have no badge."),
    ("i", "video", "Video: double-click to play it."),
    ("i", "music", "Music: double-click to play it."),
    ("i", "doc", "Document (PDF, Word, text, e-books…)."),
    ("i", "archive", "Archive or installer (ZIP, RAR, EXE…)."),
    ("h", "PC to iPhone"),
    ("p", "Open Wings, double-click a folder in the left list to show its files (or a file to add it), select, then press Send."),
    ("p", "Send to: Music cannot be filled over USB. To send music, choose VLC or Documents by Readdle and find the files in that app."),
    ("p", "The Wi-Fi window shares the Save in folder: the phone sees the photos there, and uploads from the phone land there too. Change it in the Wi-Fi window or above."),
    ("p", "Sent files go into the folder named on the iPhone, so they do not appear in the Photos app. Sending into /DCIM is refused because it can confuse Photos."),
    ("h", "iPhone to PC"),
    ("s", "With a USB cable:"),
    ("p", "1. Plug the iPhone into the PC with the cable."),
    ("p", "2. Unlock the iPhone."),
    ("p", "3. Tap Trust on the iPhone."),
    ("p", "4. Click USB-C cable in the middle of the window; the files are listed."),
    ("p", "5. Select files, check Save in, then click Copy. Copied files are sorted into date folders under the folder in Save in."),
    ("s", "With Wi-Fi (no cable):"),
    ("p", "1. Put the phone and the PC on the same Wi-Fi."),
    ("p", "2. Click Wi-Fi share (in the middle of the window, or above the list once files are shown)."),
    ("p", "3. Scan the code with the phone's camera and open the link in Safari."),
    ("p", "4. Only if you ticked the HTTPS box: the first time, open the \"First time only\" link in the window on the phone to set it up."),
    ("p", "5. Choose files on the page. The Safari page must stay open and in front while it uploads."),
    ("s", "With Tailscale (no Wi-Fi, no cable):"),
    ("p", "1. Install Tailscale on the PC and on the iPhone, and sign in to the same account on both."),
    ("p", "2. Turn Tailscale on for both; the PC must stay on."),
    ("p", "3. Click Wi-Fi share on the PC."),
    ("p", "4. Copy the link in the Away from home (needs Tailscale) box and open it in Safari on the iPhone (type or send it to yourself)."),
    ("p", "5. Choose files on the page. Keep Safari open and in front while it uploads. Save to Photos is not available on this link; use Select and ZIP."),
    ("h", "Accepted file types"),
    ("p", "Anything else (for example .bin or .dll) is not accepted by the Wi-Fi page. Installers are only stored, never run. Kindle and other e-book formats need a reader app on the phone."),
]
# PC to iPhone destinations. The Music app cannot be filled over USB, so music goes into the Documents
# folder of a player that allows file sharing; the first choice is a plain folder (what photos use).
SEND_TO = {
    "Folder on iPhone": None,
    "VLC (music player)": "org.videolan.vlc-ios",
    "Documents by Readdle": "com.readdle.ReaddleDocsIPad",
}
APP_NAME = "Dropboxer"
BTN_W = 14  # every plain button has the same width (fits "28 Oct 2026 ▾" and the Vietnamese labels)
# Warm map-paper background, thin lines, one grass-green accent.
PAPER = "#f8fafd"  # inputs, buttons and pop-ups: a touch lighter than the window
BG, HEAD, PANEL, SOFT, LINE = "#eef3f8", "#e1e9f2", PAPER, "#e3eaf2", "#c9d5e2"
TEXT, MUTED, GREEN, GREEN_DARK, RED = "#1f2933", "#66727f", "#2b6cb0", "#1f5290", "#b5352b"  # GREEN is the blue accent (name is historical)
TICK_BLUE = "#2b6cb0"


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_settings(settings: dict) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings), encoding="utf-8")
    except OSError:
        pass  # remembering the folder is a convenience; never block closing the window


class App:
    def __init__(self, root: tk.Tk, source_dir: str | None, udid: str | None):
        self.root, self.source_dir, self.udid = root, source_dir, udid
        self.source: Source | None = None
        self.all_files: list[RemoteFile] = []
        self.shown: list[RemoteFile] = []  # after the Show and date filters, newest first
        self.groups: list[tuple[str, list[RemoteFile]]] = []  # (YYYY-MM-DD, files) in display order
        self.selected: set[str] = set()  # paths
        # The file list and ticks of the mode that is not on screen: (all_files, selected) per mode.
        self._stash: dict[str, tuple[list[RemoteFile], set[str]]] = {"receive": ([], set()), "send": ([], set())}
        self.layout: list[tuple] = []  # ("head", y, day, files) or ("cell", y, column, file)
        self.hits: list[tuple] = []  # what is drawn right now: (x0, y0, x1, y1, kind, payload)
        self.cols = 1
        self.last_clicked: RemoteFile | None = None
        self.band_from: tuple[float, float] | None = None  # where the left button went down, for drag-to-select
        self.band: tuple[float, float, float, float] | None = None  # the box being dragged, in canvas coordinates
        self.band_base: set[str] = set()
        self.band_erase = False  # the drag started on a selected tile: the box unselects
        self.redraw_pending = False
        self.tip: tk.Toplevel | None = None
        self.tip_after: str | None = None
        self.tip_path: str | None = None
        self.tip_xy = (0, 0)
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.thumb_by_path: dict[str, tk.PhotoImage] = {}  # kept here so tk does not garbage-collect them
        self.thumb_asked: set[str] = set()
        self.thumb_fails: dict[str, int] = {}
        self.visible: list[RemoteFile] = []
        self.thumb_sem: asyncio.Semaphore | None = None  # created on the loop thread
        self.failed = False  # the last connection or transfer broke: the cable watcher reconnects
        self.verb, self.nfiles, self.sizes = "Copying", 0, {}  # for the "3 of 80" progress text
        self.wings = False  # side panes open: the source on the left, the destination on the right
        self.send_scanned = False  # the Pictures folder is previewed the first time PC to iPhone opens

        # One background thread owns the event loop (and so the device connection).
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        root.title(APP_NAME)
        k = root.winfo_fpixels("1i") / 96
        # Fit the work area (the screen minus the taskbar) and open centred in it, so nothing starts hidden.
        left, top, right, bottom = 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - 48
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            area = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0):  # SPI_GETWORKAREA
                left, top, right, bottom = area.left, area.top, area.right, area.bottom
        h = min(round(860 * k), bottom - top - 50, right - left - 20)  # 50: the title bar and window borders
        w = h  # opens as a square; the wings add width later
        root.geometry(f"{w}x{h}+{left + (right - left - w) // 2}+{top + (bottom - top - h - 40) // 2}")
        set_language(_load_settings().get("lang", "en"))
        self._build()
        root.update_idletasks()
        need = min(root.winfo_reqwidth(), right - left - 20)  # a square, unless the header needs more room than that
        if need > w:
            w = need
            root.geometry(f"{w}x{h}+{left + (right - left - w) // 2}+{top + (bottom - top - h - 40) // 2}")
        self._mode_changed()  # PC to iPhone is the starting mode
        root.protocol("WM_DELETE_WINDOW", self._quit)
        root.bind("<Control-a>", lambda e: self._key(e, self.select_all))
        root.bind("<Escape>", lambda e: self._key(e, self.clear_selection))
        root.after(POLL_MS, self._poll)
        self.scanning = self.auto = False  # auto: the running connect was started by the cable watcher, so it fails quietly
        root.after(500, self._watch)
        root.after(300, self._share_start)

    # ---- layout -------------------------------------------------------------------------------
    def _style(self) -> None:
        """Light theme matching the Wi-Fi page."""
        self.root.configure(background=BG)
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, fieldbackground=BG, bordercolor=LINE,
                    lightcolor=BG, darkcolor=BG, focuscolor=BG, troughcolor=SOFT, font=("Segoe UI", 10))
        for name, bg in (("Head", HEAD), ("Panel", PANEL)):
            s.configure(f"{name}.TFrame", background=bg)
            s.configure(f"{name}.TLabel", background=bg)
            s.configure(f"{name}.TCheckbutton", background=bg, indicatorforeground="white")
            s.map(f"{name}.TCheckbutton", background=[("active", bg)],
                  indicatorbackground=[("selected", GREEN), ("!selected", PAPER)])
        s.configure("Card.TFrame", background=PANEL, bordercolor=LINE, borderwidth=1, relief="solid")
        s.configure("Link.TButton", background=HEAD, foreground=MUTED, borderwidth=0, relief="flat", bordercolor=HEAD,
                    lightcolor=HEAD, darkcolor=HEAD)
        s.map("Link.TButton", foreground=[("active", GREEN_DARK)], background=[("active", HEAD)], bordercolor=[("active", HEAD)])
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED)
        s.configure("Title.TLabel", background=HEAD, foreground=GREEN_DARK, font=("Segoe UI", 17, "bold"))
        s.configure("Sub.TLabel", background=HEAD, foreground=MUTED)
        # Outlined buttons with a thin line; the main action is solid green.
        s.configure("TButton", background=PAPER, foreground=TEXT, bordercolor=LINE, lightcolor=PAPER,
                    darkcolor=PAPER, borderwidth=1, padding=(14, 6), relief="solid")
        s.map("TButton", background=[("disabled", PAPER), ("active", SOFT)], foreground=[("disabled", "#aab4bf"), ("active", GREEN_DARK)],
              bordercolor=[("disabled", LINE), ("active", GREEN)])
        s.layout("TButton", [("Button.border", {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})])  # no focus outline
        s.configure("Narrow.TButton", padding=(6, 6))
        for name, bg, hover, off, fg, fill in (("Primary", GREEN, GREEN_DARK, "#b5cde8", "white", True),
                                                ("Danger", PAPER, "#fbeceb", PAPER, RED, False)):
            s.configure(f"{name}.TButton", background=bg, foreground=fg, font=("Segoe UI", 10, "bold"),
                        bordercolor=bg if fill else "#e3c4c0", lightcolor=bg, darkcolor=bg)
            s.map(f"{name}.TButton", background=[("disabled", off), ("active", hover)],
                  foreground=[("disabled", "white" if fill else "#e3c3bf")],
                  bordercolor=[("disabled", off if fill else LINE), ("active", hover if fill else RED)])
        s.configure("Seg.Toolbutton", background=PAPER, foreground=TEXT, bordercolor=LINE,
                    lightcolor=PAPER, darkcolor=PAPER, padding=(14, 6), borderwidth=1, relief="solid")
        plain = [("Button.border", {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})]
        for name in ("TButton", "Narrow.TButton", "Primary.TButton", "Danger.TButton"):
            s.layout(name, plain)  # no dotted focus outline on any button
        s.layout("Seg.Toolbutton", [("Button.border", {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})])  # no dotted focus outline
        s.map("Seg.Toolbutton", background=[("selected", GREEN), ("active", SOFT)],
              foreground=[("selected", "white")], bordercolor=[("selected", GREEN), ("active", GREEN)],
              lightcolor=[("selected", GREEN), ("active", SOFT)], darkcolor=[("selected", GREEN), ("active", SOFT)])  # no pale rim inside the blue
        s.configure("TEntry", fieldbackground=PAPER, foreground=TEXT, insertcolor=TEXT, padding=6)
        s.map("TEntry", fieldbackground=[("readonly", SOFT)], bordercolor=[("focus", GREEN)])
        s.configure("TCombobox", fieldbackground=PAPER, background=PAPER, foreground=TEXT, arrowcolor=TEXT, padding=5,
                    selectbackground=PAPER, selectforeground=TEXT)  # readonly boxes looked greyed out and turned dark when focused
        s.map("TCombobox", fieldbackground=[("readonly", PAPER)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", PAPER)], selectforeground=[("readonly", TEXT)], bordercolor=[("focus", GREEN)])
        self.root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 10))
        self.root.option_add("*TCombobox*Listbox.background", PAPER)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", GREEN)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")
        s.configure("TSpinbox", fieldbackground=PAPER, foreground=TEXT, insertcolor=TEXT, arrowcolor=TEXT, padding=4)
        s.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorforeground="white")
        s.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", GREEN), ("!selected", PAPER)])
        s.configure("Horizontal.TProgressbar", troughcolor=SOFT, background=GREEN, bordercolor=LINE,
                    lightcolor=GREEN, darkcolor=GREEN, thickness=8)
        s.configure("Vertical.TScrollbar", troughcolor=BG, background=LINE, bordercolor=BG, arrowcolor=BG,
                    lightcolor=LINE, darkcolor=LINE)
        s.map("Vertical.TScrollbar", background=[("active", "#b5c4d5")])
        s.configure("Treeview", background=BG, fieldbackground=BG, foreground=TEXT, borderwidth=0, rowheight=24)
        s.map("Treeview", background=[("selected", GREEN)], foreground=[("selected", "white")])
        s.configure("Treeview.Heading", background=HEAD, foreground=TEXT, relief="flat", padding=4)
        s.map("Treeview.Heading", background=[("active", SOFT)])

    def _zigzag(self) -> None:
        """The strip under the title; it doubles as the progress bar, filling left to right."""
        c, width = self.banner, self.banner.winfo_width()
        cut = width * self.progress / 100
        c.delete("all")
        c.create_rectangle(0, 0, cut, 18, fill=GREEN_DARK, outline="")
        for x in range(0, width + 36, 36):
            tri = [(x, 0), (x + 36, 0), (x + 18, 18)]
            c.create_polygon(tri, fill="#bcd1e8", outline="")
            part = []  # the triangle clipped to x <= cut
            for (ax, ay), (bx, by) in zip(tri, tri[1:] + tri[:1]):
                if ax <= cut:
                    part.append((ax, ay))
                if (ax <= cut) != (bx <= cut):
                    part.append((cut, ay + (by - ay) * (cut - ax) / (bx - ax)))
            if len(part) >= 3:
                c.create_polygon(part, fill=GREEN, outline="")

    def _set_progress(self, pct: float) -> None:
        self.progress = pct
        self._zigzag()

    def _build(self) -> None:
        self._style()
        self.mode = tk.StringVar(value="send")  # "receive" = iPhone to PC, "send" = PC to iPhone
        head = ttk.Frame(self.root, style="Head.TFrame", padding=(16, 12))
        head.pack(fill="x")
        tk.Frame(self.root, height=1, background=LINE).pack(fill="x")  # thin line under the header
        self.progress = 0.0
        self.banner = tk.Canvas(self.root, height=18, highlightthickness=0, background="#a9c3e0")  # zigzag strip, like the card banners
        self.banner.pack(fill="x")
        self.banner.bind("<Configure>", lambda _e: self._zigzag())
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left")
        lang = ttk.Combobox(head, values=list(LANGUAGES.values()), state="readonly", width=12)
        lang.set(LANGUAGES[language()])
        lang.pack(side="right")
        lang.bind("<<ComboboxSelected>>", lambda _e: self._set_language(lang.current()))
        ttk.Button(head, text=T("How to use"), style="Narrow.TButton", width=BTN_W, command=self.show_manual).pack(side="right", padx=(0, 8))
        self.wings_btn = ttk.Button(head, text=T("Wings"), width=BTN_W, command=self.toggle_wings)
        self.wings_btn.pack(side="right", padx=(0, 8))

        # The window in three parts: left wing | the usual window | right wing. The wings are 0 wide until Wings opens them.
        shell = tk.Frame(self.root, background=BG)
        shell.pack(fill="both", expand=True)
        self.wing_l, self.wing_r = (tk.Frame(shell, background=BG, width=0) for _ in range(2))
        for wing, edge in ((self.wing_l, "left"), (self.wing_r, "right")):
            wing.pack(side=edge, fill="y")
            wing.pack_propagate(False)
        tk.Frame(self.wing_l, width=1, background=LINE).pack(side="right", fill="y")  # thin edges where the wings meet the window
        tk.Frame(self.wing_r, width=1, background=LINE).pack(side="left", fill="y")
        body = tk.Frame(shell, background=BG)
        body.pack(side="left", fill="both", expand=True)
        self._wings_built = False

        self.top = top = ttk.Frame(body, padding=(16, 12, 16, 6))
        top.pack(fill="x")
        self.seg = seg = ttk.Frame(top)
        seg.pack(side="left")
        for text, value in (("PC to iPhone", "send"), ("iPhone to PC", "receive")):
            ttk.Radiobutton(seg, text=T(text), value=value, variable=self.mode, style="Seg.Toolbutton", takefocus=False,
                            command=self._mode_changed).pack(side="left", padx=(0, 2))
        self.copy_btn = ttk.Button(top, text=T("Copy"), style="Primary.TButton", command=self.primary_action, state="disabled")
        self.copy_btn.pack(side="left", padx=(16, 0))
        self.cancel_btn = ttk.Button(top, text=T("Stop"), style="Danger.TButton", command=self.cancel.set)
        self.count = ttk.Label(top, text="", style="Muted.TLabel")
        self.count.pack(side="right")

        # Where the files go.
        self.rows = ttk.Frame(body, padding=(16, 0, 16, 8))
        self.rows.pack(fill="x")
        self.dest_row = row = ttk.Frame(self.rows)
        ttk.Label(row, text=T("Save in")).pack(side="left")
        self.dest = tk.StringVar(value=_load_settings().get("dest") or str(Path.home() / "Pictures" / "iPhone"))
        ttk.Entry(row, textvariable=self.dest).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text=T("Change"), style="Narrow.TButton", width=BTN_W, command=self.browse).pack(side="left")
        self.remote_row = row = ttk.Frame(self.rows)  # PC to iPhone: where on the phone
        ttk.Label(row, text=T("Send to")).pack(side="left")
        self.send_names = {T(k): k for k in SEND_TO}  # what the list shows -> its key in SEND_TO
        self.send_to = tk.StringVar(value=T("Folder on iPhone"))
        pick = ttk.Combobox(row, textvariable=self.send_to, values=list(self.send_names), state="readonly", width=20)
        pick.pack(side="left", padx=8)
        pick.bind("<<ComboboxSelected>>", lambda _e: self._send_to_changed())
        self.remote = tk.StringVar(value=DEFAULT_UPLOAD_ROOT)
        self.remote_label = ttk.Label(row, text=_cap(T("folder")))
        self.remote_label.pack(side="left")
        ttk.Entry(row, textvariable=self.remote, width=16).pack(side="left", padx=8)
        self.send_note = ttk.Label(row, text="⚠ " + T("Won't show in Photos."), style="Muted.TLabel", foreground=RED)
        self.send_note.pack(side="left")

        # Filters and select-all sit in view, because they are used all the time.
        bar = ttk.Frame(body, padding=(16, 0, 16, 8))
        bar.pack(fill="x")
        self.kind = tk.StringVar(value="Gallery")
        self.kind_seg = ttk.Frame(bar)  # always shown, so the middle looks the same with and without Wings
        self.kind_seg.pack(side="left")
        for name in KINDS:
            ttk.Radiobutton(self.kind_seg, text=T(name), value=name, variable=self.kind, style="Seg.Toolbutton", takefocus=False,
                            command=self._fill).pack(side="left", padx=(0, 2))
        self.all_ticked = tk.BooleanVar(value=False)
        self.select_all_cb = ttk.Checkbutton(bar, text=T("Select all"), variable=self.all_ticked, command=self._toggle_all)
        self.select_all_cb.pack(side="left", padx=(16, 0))

        mid = ttk.Frame(body, padding=(16, 0, 0, 0))
        mid.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(mid, background=BG, highlightthickness=0, yscrollincrement=SCROLL_UNIT)
        sb = ttk.Scrollbar(mid, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=lambda a, b: (sb.set(a, b), self._schedule_redraw()))
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-WHEEL_UNITS if e.delta > 0 else WHEEL_UNITS, "units"))
        self.canvas.bind("<Configure>", lambda _e: self.root.after_idle(self._relayout))
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Double-Button-1>", lambda e: (self._press(e), self._open_preview(e)))  # the two clicks cancel out
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self._hide_tip(reset=True))
        self.empty = tk.Frame(self.canvas, background=BG)  # iPhone to PC with nothing listed yet: how to get photos
        self.share_qr = ttk.Label(self.empty)
        self.share_qr.pack()
        ttk.Label(self.empty, text=T("Scan with your phone (same Wi-Fi)")).pack()
        for attr, label in (("share_wifi", "Wi-Fi"), ("share_away", "Tailscale")):
            row = ttk.Frame(self.empty)
            ttk.Label(row, text=label, width=10).pack(side="left")
            entry = ttk.Entry(row, width=44)
            entry.pack(side="left")
            setattr(self, attr + "_row", row)
            setattr(self, attr, entry)

        self.dest_row.pack(fill="x")  # both rows are as tall as the taller one, so the filters below never move between tabs
        self.remote_row.pack(fill="x")
        self.root.update_idletasks()
        self.rows.configure(height=max(self.dest_row.winfo_reqheight(), self.remote_row.winfo_reqheight()) + 8)
        self.rows.pack_propagate(False)
        self.dest_row.pack_forget()
        self.remote_row.pack_forget()

        self.status = ttk.Label(body, text="",
                                style="Muted.TLabel", padding=(16, 4, 16, 12))
        self.status.pack(side="bottom", fill="x", before=mid)  # a short window shrinks the grid, not the status
        self._wings_state()
        self.dest.trace_add("write", lambda *_: self._share_changed())
        if getattr(self, "_wifi_server", None):
            self._share_show()
        if self.wings:  # rebuilt while open (a language change): the wings stay out
            self._open_wings_now()

    def _set_language(self, index: int) -> None:
        """Rebuild the window in the chosen language; the files, ticks, dates and folders stay as they are."""
        set_language(list(LANGUAGES)[index])
        _save_settings({**_load_settings(), "lang": language(), "dest": self.dest.get().strip()})
        self._rebuild()

    def _wing_sizes(self) -> tuple[int, int, int]:
        """Widths of the left wing, the right wing and the middle: full size, or less when the screen is too narrow for all three."""
        k = self.root.winfo_fpixels("1i") / 96
        wing, mid = round(480 * k), self.root.winfo_width()
        avail = self.root.winfo_screenwidth() - 20
        if mid + 2 * wing > avail:  # both wings are always the same width, so the window opens evenly
            wing = max(round(240 * k), (avail - mid) // 2)
        return wing, wing, mid

    def _ensure_wings(self) -> None:
        """Build the wings the first time they open. Left: where the files are now. Right: where they are going, with the routes below."""
        if self._wings_built:
            return
        self._wings_built = True
        self.left_slot = tk.Frame(self.wing_l, background=BG)
        self.left_slot.pack(side="right", fill="both", expand=True)
        right = tk.Frame(self.wing_r, background=BG)
        right.pack(side="left", fill="both", expand=True)
        self.right_slot = tk.Frame(right, background=BG)
        self.right_slot.pack(fill="both", expand=True)
        self._fill_wings()

    def _fill_wings(self) -> None:
        """What the wings show follows the direction: iPhone on the left and the PC folder on the right, or the other way round."""
        if not self._wings_built:
            return
        for slot in (self.left_slot, self.right_slot):
            for w in slot.winfo_children():
                w.destroy()
        if self.mode.get() == "send":
            self._build_explorer(self.left_slot, source=True)
            self._build_iphone_dest(self.right_slot)
        else:
            self._build_albums(self.left_slot)
            self._build_explorer(self.right_slot, source=False)

    def _wings_state(self) -> None:
        """The Wings button shows it is open, and the wings already hold Save in / Send to."""
        on = self.wings
        self.wings_btn.config(style="Primary.TButton" if on else "Narrow.TButton")
        if on:  # the wings already show Save in / Send to
            self.rows.pack_forget()
        elif not self.rows.winfo_ismapped():
            self.rows.pack(fill="x", after=self.top)

    def _open_wings_now(self) -> None:
        self._ensure_wings()
        left, right = self._wing_w
        self.wing_l.config(width=left)
        self.wing_r.config(width=right)

    def toggle_wings(self) -> None:
        """Wings: the window spreads its wings (source on the left, destination on the right) and folds them again."""
        if getattr(self, "_flying", False):
            return
        _save_settings({**_load_settings(), "dest": self.dest.get().strip()})
        opening = not self.wings
        self.wings = opening
        if opening:
            self._ensure_wings()
        self._wings_state()
        self._fly(opening)

    def _fly(self, opening: bool) -> None:
        """Animate the wings out (or in): the window grows sideways while the middle stays where it is."""
        import re

        root = self.root
        w, h, x, y = (int(v) for v in re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", root.geometry()).groups())
        if opening:
            left, right, mid1 = self._wing_sizes()
            self._wing_w, self._mid_before = (left, right), w
            mid0 = w
        else:
            left, right = self.wing_l.winfo_width(), self.wing_r.winfo_width()
            mid0, mid1 = w - left - right, getattr(self, "_mid_before", w - left - right)
        edge = x + (0 if opening else left)  # where the middle's left edge is, and stays
        screen, self._flying, steps = root.winfo_screenwidth(), True, 16

        def frame(i: int) -> None:
            e = 1 - (1 - i / steps) ** 3  # fast at first, settling at the end, like a wing stroke
            f = e if opening else 1 - e
            wl, wr = round(left * f), round(right * f)
            mid = round(mid0 + (mid1 - mid0) * e)
            total = mid + wl + wr
            self.wing_l.config(width=wl)
            self.wing_r.config(width=wr)
            root.geometry(f"{total}x{h}+{max(0, min(edge - wl, screen - total))}+{y}")
            if i < steps:
                root.after(18, frame, i + 1)
            else:
                self._flying = False

        frame(0)

    def _rebuild(self) -> None:
        mode, kind = self.mode.get(), self.kind.get()
        for w in self.root.winfo_children():
            w.destroy()
        self._build()
        self.mode.set(mode)
        self.kind.set(kind)
        self._mode_changed(keep=True)

    def _mode_changed(self, keep: bool = False) -> None:
        """Switch between copying from the iPhone and sending to it."""
        sending = self.mode.get() == "send"
        if not keep:
            self._stash["receive" if sending else "send"] = (self.all_files, self.selected)
            self.all_files, self.selected = self._stash["send" if sending else "receive"]
        if sending:
            self.dest_row.pack_forget()
            self.remote_row.pack(fill="x")
            if not self.all_files and not self.send_scanned:  # first visit: preview the Pictures folder
                self.send_scanned = True
                self._show_folder(Path.home() / "Pictures")
                self._show_folder(Path.home() / "Music")
            self.status.config(text="" if self.all_files else T("Looking for files…"))
        else:
            self.remote_row.pack_forget()
            self.dest_row.pack(fill="x")
            self.status.config(text="" if self.all_files else T("Unlock your iPhone and tap Trust."))
        self._wings_state()
        self._fill_wings()
        self._fill()

    def primary_action(self) -> None:
        if self.mode.get() == "send":
            self.send_selected()
        else:
            self.start_copy()

    # ---- list ---------------------------------------------------------------------------------
    def _fill(self) -> None:
        exts = KINDS[self.kind.get()]
        self.shown = sorted(
            (f for f in self.all_files
             if (exts is None or Path(f.path).suffix.lower() in exts)
             ),
            key=lambda f: f.mtime, reverse=True)
        days: dict[str, list[RemoteFile]] = defaultdict(list)  # insertion order = newest day first
        for f in self.shown:
            days[datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d")].append(f)
        self.groups = list(days.items())
        self.selected &= {f.path for f in self.shown}  # a filtered-out file is no longer selected
        self.canvas.yview_moveto(0)
        self._update_count()
        self._relayout()

    # ---- grid ---------------------------------------------------------------------------------
    def _relayout(self) -> None:
        """Place day headings and thumbnail cells; the column count follows the window width."""
        width = max(self.canvas.winfo_width(), CELL)
        self.cols = max(1, width // CELL)
        self.layout, y = [], 0
        for day, fs in self.groups:
            self.layout.append(("head", y, day, fs))
            y += HEAD_H
            for i, f in enumerate(fs):
                self.layout.append(("cell", y + (i // self.cols) * CELL, i % self.cols, f))
            y += -(-len(fs) // self.cols) * CELL
        self.canvas.configure(scrollregion=(0, 0, width, max(y, 1)))
        self._schedule_redraw()

    def _schedule_redraw(self) -> None:
        if not self.redraw_pending:
            self.redraw_pending = True
            self.root.after_idle(self._redraw)

    def _redraw(self) -> None:
        """Draw only what is on screen: a library can be thousands of files."""
        self.redraw_pending = False
        self._hide_tip(reset=True)
        c = self.canvas
        c.delete("all")
        self.hits, self.visible = [], []
        if not self.layout:
            mid_x, mid_y = c.winfo_width() // 2, c.winfo_height() // 2
            if self.mode.get() == "receive" and not self.all_files:
                c.create_window(mid_x, mid_y, window=self.empty)
            else:
                empty = T("Nothing here yet.")
                c.create_text(mid_x, mid_y, text=T("No files match these filters.") if self.all_files else empty,
                              fill=MUTED, font=("Segoe UI", 13))
            return
        top = c.canvasy(0)
        bottom = top + c.winfo_height()
        width = max(c.winfo_width(), CELL)
        for kind, y, a, b in self.layout:
            if kind == "head":
                if y + HEAD_H < top or y > bottom:
                    continue
                day = datetime.strptime(a, "%Y-%m-%d").strftime("%a %Y-%m-%d")
                mark = "✓ " if all(f.path in self.selected for f in b) else ""
                c.create_text(PAD + 2, y + HEAD_H // 2, anchor="w", font=("Segoe UI", 11, "bold"),
                                      fill=TEXT, text=f"{mark}{day}")
                self.hits.append((0, y, width, y + HEAD_H, "head", b))
                continue
            if y + CELL < top or y > bottom:
                continue
            f, x = b, a * CELL
            x0, y0 = x + PAD, y + PAD
            c.create_rectangle(x0, y0, x0 + THUMB, y0 + THUMB, fill=SOFT, outline=LINE)
            img = self.thumb_by_path.get(f.path)
            if img:
                c.create_image(x0 + THUMB // 2, y0 + THUMB // 2, image=img)
            else:
                c.create_text(x0 + THUMB // 2, y0 + THUMB // 2, fill=MUTED, font=("Segoe UI", 12, "bold"),
                              text=Path(f.path).suffix.lstrip(".").upper())
            ext = Path(f.path).suffix.lower()
            kind = "video" if ext in VIDEOS else "music" if ext in AUDIO else "doc" if ext in DOCS else "archive" if ext in ARCHIVES else ""
            if kind:
                self._kind_badge(x0 + 5, y0 + THUMB - 5 - 18, kind)
            if f.path in self.selected:
                c.create_rectangle(x0, y0, x0 + THUMB, y0 + THUMB, fill=TICK_BLUE, stipple="gray50", outline="")  # blue filter
                c.create_rectangle(x0, y0, x0 + THUMB, y0 + THUMB, outline=TICK_BLUE, width=3)
                r = 13 * THUMB / 200  # blue disc with a white tick, bottom right, like the iPhone's
                cx, cy = x0 + THUMB - 8 - r, y0 + THUMB - 8 - r
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=TICK_BLUE, outline="white", width=2)
                c.create_line(cx - r * .45, cy + r * .02, cx - r * .1, cy + r * .38, cx + r * .5, cy - r * .35,
                              fill="white", width=max(2, int(r / 4)), capstyle="round", joinstyle="round")
            self.hits.append((x0, y0, x0 + THUMB, y0 + THUMB, "cell", f))
            self.visible.append(f)
        if self.band:  # the box being dragged
            l, t, rt, b = self.band
            c.create_rectangle(l, t, rt, b, outline=GREEN, fill=GREEN, stipple="gray12", width=1)
            w, h, gap = rt - l, b - t, 10
            for k in range(-int(h) // gap * gap, int(w), gap):  # diagonal stripes, clipped to the box
                t0, t1 = max(0, -k), min(h, w - k)
                if t0 < t1:
                    c.create_line(l + k + t0, t + t0, l + k + t1, t + t1, fill=GREEN)
        self._load_visible()

    def _kind_badge(self, x: float, y: float, kind: str, c: tk.Canvas | None = None) -> None:
        """A small dark badge (24x18, top-left at x, y) with a white symbol: video, music, document or archive."""
        c, w = c or self.canvas, "white"
        c.create_rectangle(x, y, x + 24, y + 18, fill="#2b2f36", outline="")
        if kind == "video":  # play triangle
            c.create_polygon(x + 9, y + 4, x + 9, y + 14, x + 17, y + 9, fill=w, outline=w)
        elif kind == "music":  # eighth note
            c.create_oval(x + 7, y + 11, x + 12, y + 15, fill=w, outline=w)
            c.create_line(x + 11.5, y + 13, x + 11.5, y + 4, x + 17, y + 6, fill=w, width=2)
        elif kind == "doc":  # page with a folded corner and two text lines
            c.create_polygon(x + 7, y + 3, x + 14, y + 3, x + 17, y + 6, x + 17, y + 15, x + 7, y + 15, outline=w, fill="")
            c.create_line(x + 9, y + 9, x + 15, y + 9, fill=w)
            c.create_line(x + 9, y + 12, x + 15, y + 12, fill=w)
        else:  # archive: a box with a lid and a handle slot
            c.create_rectangle(x + 6, y + 4, x + 18, y + 7, outline=w, fill="")
            c.create_rectangle(x + 7, y + 7, x + 17, y + 15, outline=w, fill="")
            c.create_line(x + 10, y + 10, x + 14, y + 10, fill=w, width=2)

    def _hit(self, e: tk.Event) -> tuple | None:
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        return next((h for h in self.hits if h[0] <= x < h[2] and h[1] <= y < h[3]), None)

    def _press(self, e: tk.Event) -> None:
        self.band_from = (self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        self.band = None
        self.band_base = set(self.selected)
        h = self._hit(e)  # a drag that starts on a selected tile unselects what it touches instead
        self.band_erase = bool(h and h[4] == "cell" and h[5].path in self.selected)

    def _drag(self, e: tk.Event) -> None:
        """Hold the left button and drag: every tile the box touches is selected (on top of what was already);
        start on a selected tile, or box only selected ones, and it unselects them instead."""
        if not self.band_from:
            return
        c = self.canvas
        if e.y < 0 or e.y > c.winfo_height():  # dragging past the top or bottom edge scrolls
            c.yview_scroll(-1 if e.y < 0 else 1, "units")
        x0, y0 = self.band_from
        x1, y1 = c.canvasx(e.x), c.canvasy(e.y)
        if not self.band and abs(x1 - x0) < 5 and abs(y1 - y0) < 5:
            return  # still a click
        self.band = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        l, t, rt, b = self.band
        hit = {h[5].path for h in self.hits if h[4] == "cell" and h[0] < rt and h[2] > l and h[1] < b and h[3] > t}
        # starting on a selected tile, or boxing only tiles that are already selected, unselects them
        erase = self.band_erase or (bool(hit) and hit <= self.band_base)
        self.selected = self.band_base - hit if erase else self.band_base | hit
        self._update_count()
        self._schedule_redraw()

    def _release(self, e: tk.Event) -> None:
        dragged = self.band is not None
        self.band = self.band_from = None
        if dragged:
            self._schedule_redraw()
        else:
            self._click(e)

    def _click(self, e: tk.Event) -> None:
        h = self._hit(e)
        if not h:
            return
        if h[4] == "head":  # select the whole day, or unselect it if it is already all selected
            paths = {f.path for f in h[5]}
            self.selected = self.selected - paths if paths <= self.selected else self.selected | paths
        else:
            f = h[5]
            if e.state & 0x1 and self.last_clicked in self.shown:  # Shift: everything between the two clicks
                i, j = sorted((self.shown.index(self.last_clicked), self.shown.index(f)))
                span = {g.path for g in self.shown[i:j + 1]}  # the range follows the first click: it ticked, or unticked
                self.selected = self.selected | span if self.last_clicked.path in self.selected else self.selected - span
            else:
                self.selected ^= {f.path}
            self.last_clicked = f
        self._update_count()
        self._schedule_redraw()

    def _open_preview(self, e: tk.Event) -> None:
        """Double-click a tile: a larger picture (the first frame for a video), from the phone or from this PC."""
        h = self._hit(e)
        if not h or h[4] != "cell":
            return
        f, local = h[5], self.mode.get() == "send"
        if not local and not self.source:
            return
        win = tk.Toplevel(self.root)
        win.title(Path(f.path).name)
        win.configure(background=BG)
        label = tk.Label(win, text=T("Loading…"), background=BG, foreground=TEXT, padx=60, pady=40)
        label.pack()
        win.bind("<Escape>", lambda _e: win.destroy())
        label.bind("<Button-1>", lambda _e: win.destroy())
        if local and is_audio(f.path) and os.name == "nt":  # audio on this PC can be played right here
            import winsound

            def play() -> None:
                winsound.PlaySound(f.path, winsound.SND_FILENAME | winsound.SND_ASYNC)

            bar = ttk.Frame(win)
            bar.pack(pady=(0, 10))
            ttk.Button(bar, text="▶ Play", style="Primary.TButton", command=play).pack(side="left", padx=4)
            ttk.Button(bar, text="■ Stop", command=lambda: winsound.PlaySound(None, 0)).pack(side="left", padx=4)
            win.bind("<Destroy>", lambda e: winsound.PlaySound(None, 0) if e.widget is win else None)
        win.focus_set()
        asyncio.run_coroutine_threadsafe(self._preview(f, local, win, label), self.loop)

    async def _preview(self, f: RemoteFile, local: bool, win: tk.Toplevel, label: tk.Label) -> None:
        try:
            if local:
                png = await asyncio.get_running_loop().run_in_executor(None, local_thumb, f.path, PREVIEW, False)
            else:
                png = await load_thumb(self.source, f, size=PREVIEW)
        except Exception:
            png = None
        self.events.put(("preview", png, win, label))

    def _motion(self, e: tk.Event) -> None:
        h = self._hit(e)
        self.canvas.config(cursor="hand2" if h else "")
        f = h[5] if h and h[4] == "cell" else None
        self.tip_xy = (e.x_root, e.y_root)
        if (f.path if f else None) == self.tip_path:
            return
        self._hide_tip(reset=True)
        if f:
            self.tip_path = f.path
            self.tip_after = self.root.after(TIP_DELAY_MS, lambda: self._show_tip(f))

    def _show_tip(self, f: RemoteFile) -> None:
        when = datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d %H:%M")
        self.tip = tk.Toplevel(self.root)
        self.tip.wm_overrideredirect(True)
        tk.Label(self.tip, text=f"{Path(f.path).name}\n{when}\n{_human(f.size)}", justify="left", background=PAPER,
                 foreground=TEXT, relief="solid", borderwidth=1, padx=8, pady=5).pack()
        self.tip.wm_geometry(f"+{self.tip_xy[0] + 16}+{self.tip_xy[1] + 16}")

    def _hide_tip(self, reset: bool = False) -> None:
        if self.tip_after:
            self.root.after_cancel(self.tip_after)
            self.tip_after = None
        if self.tip:
            self.tip.destroy()
            self.tip = None
        if reset:
            self.tip_path = None

    def _load_visible(self) -> None:
        """Ask for thumbnails of the cells currently on screen (not the whole library)."""
        local = self.mode.get() == "send"  # the list holds files on this PC, so no phone is needed
        if self.busy or not (local or self.source):
            return
        for f in self.visible:
            if f.path not in self.thumb_asked and (is_image(f.path) or is_video(f.path) or (local and is_audio(f.path))):
                self.thumb_asked.add(f.path)
                asyncio.run_coroutine_threadsafe(self._thumb(f, local), self.loop)

    async def _thumb(self, f: RemoteFile, local: bool = False) -> None:
        if self.thumb_sem is None:
            self.thumb_sem = asyncio.Semaphore(2)
        png = None
        async with self.thumb_sem:
            try:
                if local:
                    png = await asyncio.get_running_loop().run_in_executor(None, local_thumb, f.path, THUMB, True)
                else:
                    png = await load_thumb(self.source, f, size=THUMB, crop=True)  # square, like the web page
            except Exception:
                self.events.put(("thumbfail", f.path))  # busy or dropped connection: ask again later
                return
        if png:
            self.events.put(("thumb", f.path, png))

    def selected_files(self) -> list[RemoteFile]:
        """Selected files among those currently shown."""
        return sorted((f for f in self.shown if f.path in self.selected), key=lambda f: f.path)

    def select_all(self) -> None:
        self.selected = {f.path for f in self.shown}
        self._update_count()
        self._schedule_redraw()

    def clear_selection(self) -> None:
        self.selected.clear()
        self._update_count()
        self._schedule_redraw()

    def _toggle_all(self) -> None:
        self.select_all() if self.all_ticked.get() else self.clear_selection()

    def _key(self, e: tk.Event, action) -> None:
        if not isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry, ttk.Spinbox)):  # leave text boxes alone
            action()

    def _update_count(self) -> None:
        sel = self.selected_files()
        self.all_ticked.set(bool(self.shown) and len(sel) == len(self.shown))
        self.count.config(text=f"{T('{n} selected', n=len(sel))}  ·  {_human(sum(f.size for f in sel))}" if sel else "")
        self._sync_buttons()
        self._update_albums()

    def _sync_buttons(self) -> None:
        n = len(self.selected_files())
        ready = bool(self.source) and not self.busy
        if self.mode.get() == "send":
            self.copy_btn.config(text=T("Send {n}", n=n) if n else T("Send"), state="normal" if ready and n else "disabled")
        else:
            self.copy_btn.config(text=T("Copy {n}", n=n) if n else T("Copy"), state="normal" if ready and n else "disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0), after=self.copy_btn) if self.busy else self.cancel_btn.pack_forget()

    def browse(self) -> None:
        d = filedialog.askdirectory(initialdir=self.dest.get() if Path(self.dest.get()).exists() else None)
        if d:
            self.dest.set(d)

    # ---- background work ----------------------------------------------------------------------
    def _submit(self, coro) -> None:
        self.busy = True
        self._sync_buttons()
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(lambda f: self.events.put(("error", f.exception())) if f.exception() else None)

    def _drop_source(self) -> None:
        """The connection broke: let go of it so the cable watcher connects again."""
        if self.source:
            asyncio.run_coroutine_threadsafe(self.source.close(), self.loop)
        self.source = None

    def _watch(self) -> None:
        """Look for a cable every couple of seconds: connect when an iPhone appears, let go when it is unplugged."""
        if not self.scanning:
            self.scanning = True
            asyncio.run_coroutine_threadsafe(self._scan(), self.loop)
        self.root.after(2500, self._watch)

    async def _scan(self) -> None:
        try:
            from .sources.iphone import list_devices

            n = len(await list_devices())
        except Exception:
            n = 0
        self.events.put(("devices", n))

    def connect(self) -> None:
        self.auto = False
        self.failed = False
        self.status.config(text=T("Connecting…"))
        self._submit(self._connect())

    async def _connect(self) -> None:
        if self.source:
            await self.source.close()
            self.source = None
        if self.source_dir:
            from .sources.local import LocalSource

            self.source = LocalSource(self.source_dir)
        else:
            from .sources.iphone import IPhoneSource

            self.source = await IPhoneSource.connect(self.udid)
        files = await self.source.list_files(DEFAULT_ROOT)
        self.events.put(("listed", files))

    def start_copy(self) -> None:
        files, dest = self.selected_files(), self.dest.get().strip()
        if not files or not dest:
            return
        self.cancel.clear()
        self._set_progress(0)
        self.total = sum(f.size for f in files)
        self.done: dict[str, int] = {}
        self.verb, self.nfiles, self.sizes = "Copying", len(files), {}
        self.status.config(text=T("Copying {n} files…", n=len(files)))
        self._submit(self._copy(files, Path(dest)))

    async def _copy(self, files, dest) -> None:
        def progress(path: str, done: int, total: int) -> None:
            self.events.put(("progress", path, done, total))

        summary = await copy_files(self.source, files, DEFAULT_ROOT, dest, progress, 1, True, self.cancel.is_set)  # one at a time, sorted into date folders
        self.events.put(("finished", summary))

    def _share_start(self) -> None:
        """Share the Save in folder over Wi-Fi (and Tailscale) so a phone can scan the QR code; restarted when the folder changes."""
        from .wifi import WifiServer, persistent_token

        old = getattr(self, "_wifi_server", None)
        if old:
            old.stop()
        self._wifi_server = None
        folder = Path(self.dest.get().strip() or ".")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for port in (8765, 0):  # 0: another copy of the app already holds the usual port
                try:
                    server = WifiServer(folder, folder, port=port, token=persistent_token())
                    server.start()
                    self._wifi_server = server
                    break
                except OSError:
                    continue
        except OSError:
            pass
        self._share_show()

    def _share_changed(self) -> None:
        if getattr(self, "_share_after", None):
            self.root.after_cancel(self._share_after)
        self._share_after = self.root.after(800, self._share_start)  # wait until the folder box stops changing

    def _share_show(self) -> None:
        """Fill the QR code and the two links in the empty iPhone list."""
        from .wifi import qr_png

        server = getattr(self, "_wifi_server", None)
        for entry, url in ((self.share_wifi, server and server.url), (self.share_away, server and server.remote_url)):
            entry.config(state="normal")
            entry.delete(0, "end")
            entry.insert(0, url or "")
            entry.config(state="readonly")
        for row in (self.share_wifi_row, self.share_away_row):
            row.pack_forget()
        if server:
            self.qr_img = tk.PhotoImage(data=qr_png(server.url))  # keep a reference or tk drops it
            self.share_qr.config(image=self.qr_img)
            self.share_wifi_row.pack(fill="x", pady=(8, 0))
            if server.remote_url:
                self.share_away_row.pack(fill="x", pady=(4, 0))
        else:
            self.share_qr.config(image="")

    def _send_to_changed(self) -> None:
        app = SEND_TO[self.send_names[self.send_to.get()]]
        self.remote.set(DEFAULT_UPLOAD_ROOT if app is None else "")
        self.remote_label.config(text=_cap(T("folder") if app is None else T("subfolder (optional)")))
        self.send_note.config(text="⚠ " + T("Won't show in Photos.") if app is None else
                              T("Find them in {app}.", app=self.send_to.get().split(" (")[0]),
                              foreground=RED if app is None else MUTED)

    def show_manual(self) -> None:
        """The instruction manual: the things the window does not make obvious, and what files are accepted."""
        win = tk.Toplevel(self.root)
        win.title(T("How to use"))
        win.configure(background=BG)
        win.geometry("640x560")
        win.transient(self.root)
        text = tk.Text(win, wrap="word", background=PAPER, foreground=TEXT, relief="flat", padx=18, pady=14,
                       font=("Segoe UI", 10), spacing1=2, spacing3=3, highlightthickness=1, highlightbackground=LINE)
        text.tag_configure("h", font=("Segoe UI", 12, "bold"), foreground=GREEN_DARK, spacing1=12, spacing3=4)
        text.tag_configure("s", font=("Segoe UI", 10, "bold"), spacing1=8)
        scroll = ttk.Scrollbar(win, command=text.yview)
        text.config(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        for kind, *rest in MANUAL:
            if kind == "i":  # an icon, drawn the same way as on the tiles, then what it means
                badge = tk.Canvas(text, width=24, height=18, background=PAPER, highlightthickness=0)
                self._kind_badge(0, 0, rest[0], badge)
                text.window_create("end", window=badge, padx=2, pady=2)
                text.insert("end", "  " + T(rest[1]) + "\n")
                continue
            text.insert("end", T(rest[0]) + "\n", kind if kind in ("h", "s") else ())
        text.insert("end", "\n")
        for name, exts in (("Gallery", PHOTOS | VIDEOS), ("Music", AUDIO), ("Documents", DOCS), ("Archives", ARCHIVES)):
            text.insert("end", T(name) + ": ", "h")
            text.insert("end", " ".join(sorted(exts)) + "\n")
        text.config(state="disabled")
        win.bind("<Escape>", lambda _e: win.destroy())
        win.focus_set()

    # ---- Wings panes --------------------------------------------------------------------------
    def _build_albums(self, parent: tk.Frame) -> None:
        """Left pane: the file types as a list with counts (the same filter as the buttons in the normal layout)."""
        tk.Label(parent, text=T("iPhone"), background=BG, foreground=MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w", padx=14, pady=8).pack(fill="x")
        self.album_list = box = tk.Listbox(parent, background=BG, foreground=TEXT, selectbackground=GREEN, selectforeground="white",
                                           highlightthickness=0, borderwidth=0, activestyle="none", exportselection=False,
                                           font=("Segoe UI", 11))
        box.pack(fill="both", expand=True)
        box.bind("<<ListboxSelect>>", lambda _e: self._pick_album(box.curselection()))
        self._update_albums()

    def _update_albums(self) -> None:
        box = getattr(self, "album_list", None)
        if box is None or not box.winfo_exists():
            return
        box.delete(0, "end")
        for name, exts in KINDS.items():
            n = sum(1 for f in self.all_files if exts is None or Path(f.path).suffix.lower() in exts)
            box.insert("end", f"   {T(name)} ({n})")
        box.selection_set(list(KINDS).index(self.kind.get()))

    def _pick_album(self, picked) -> None:
        if picked:
            self.kind.set(list(KINDS)[picked[0]])
            self._fill()

    def _build_explorer(self, parent: tk.Frame, source: bool) -> None:
        """This PC's folders. As the destination (iPhone to PC) picking a folder sets Save in; as the source (PC to iPhone)
        double-clicking a folder lists its files in the middle, and double-clicking a file adds it."""
        tk.Label(parent, text=T("Explorer") if source else T("Save in"), background=BG, foreground=MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w", padx=14, pady=8).pack(fill="x")
        foot = ttk.Label(parent, style="Muted.TLabel", padding=(8, 6))
        foot.pack(side="bottom", fill="x")
        tree = ttk.Treeview(parent, show="tree", selectmode="browse")
        sb = ttk.Scrollbar(parent, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        paths: dict[str, Path] = {}

        sendable = PHOTOS | VIDEOS | AUDIO | DOCS | ARCHIVES

        def add(parent_id: str, path: Path, label: str | None = None) -> str:
            folder = path.is_dir()
            iid = tree.insert(parent_id, "end", text="  " + ("" if folder else "▫ ") + (label or path.name or str(path)), open=False)
            paths[iid] = path
            if folder:
                tree.insert(iid, "end", text="…")  # a placeholder so the arrow shows; replaced on first open
            return iid

        def opened(_e=None) -> None:
            iid = tree.focus()
            kids = tree.get_children(iid)
            if iid not in paths or not (len(kids) == 1 and tree.item(kids[0], "text") == "…"):
                return
            tree.delete(kids[0])
            try:
                items = sorted(paths[iid].iterdir(), key=lambda d: d.name.lower())
                subs = [d for d in items if d.is_dir()] + ([d for d in items if d.is_file() and d.suffix.lower() in sendable][:500] if source else [])
            except OSError:
                subs = []
            for d in subs:
                add(iid, d)

        def picked(_e=None) -> None:
            sel = tree.selection()
            path = paths.get(sel[0]) if sel else None
            if path is None:
                return
            if path.is_dir() and not source:
                self.dest.set(str(path))  # where files from the iPhone are saved
            try:
                use = shutil.disk_usage(path)
                foot.config(text=f"{T('Free')}: {_human(use.free)}  {T('Total')}: {_human(use.total)}")
            except OSError:
                foot.config(text=str(path))

        def listing(_e=None) -> None:
            sel = tree.selection()
            path = paths.get(sel[0]) if sel and source else None
            if path is None:
                return
            if path.is_file():
                self._add_to_send([path])
            else:
                self._show_folder(path, only=True)

        pc =tree.insert("", "end", text="  " + T("This PC"), open=True)
        home = Path.home()
        for name in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"):
            if (home / name).is_dir():
                add(pc, home / name)
        drives = [Path(f"{c}:/") for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{c}:/").exists()] if os.name == "nt" else [Path("/")]
        for d in drives:
            add(pc, d, str(d))
        tree.bind("<<TreeviewOpen>>", opened)
        tree.bind("<<TreeviewSelect>>", picked)
        tree.bind("<Double-Button-1>", listing)
        if not source:  # start on the folder files are saved in
            tree.selection_set(pc)

    def _build_iphone_dest(self, parent: tk.Frame) -> None:
        """PC to iPhone, right wing: where on the iPhone the files go (the same choice as Send to in the middle)."""
        tk.Label(parent, text=T("iPhone"), background=BG, foreground=MUTED,
                 font=("Segoe UI", 9, "bold"), anchor="w", padx=14, pady=8).pack(fill="x")
        row = tk.Frame(parent, background=BG)
        row.pack(fill="x", padx=14)
        pick = ttk.Combobox(row, textvariable=self.send_to, values=list(self.send_names), state="readonly")
        pick.pack(fill="x", pady=(0, 8))
        pick.bind("<<ComboboxSelected>>", lambda _e: self._send_to_changed())
        ttk.Label(row, text=_cap(T("folder"))).pack(anchor="w")
        ttk.Entry(row, textvariable=self.remote).pack(fill="x", pady=(2, 8))
        ttk.Label(row, text="⚠ " + T("Won't show in Photos."), style="Muted.TLabel", foreground=RED).pack(anchor="w")

    def _show_folder(self, folder: Path, only: bool = False) -> None:
        """List the photos, videos, music and documents inside a folder (nothing ticked yet) so they can be previewed and picked.
        only: show just the files directly in that folder, replacing the list (the Explorer acts as a filter)."""
        exts = PHOTOS | VIDEOS | AUDIO | DOCS | ARCHIVES
        self.status.config(text=T("Looking in {folder}…", folder=folder))

        def scan() -> None:
            found = []
            try:
                for p in (folder.glob("*") if only else folder.rglob("*")):
                    if p.suffix.lower() in exts and p.is_file():
                        found.append(p)
                        if len(found) >= MAX_PREVIEW:
                            break
            except OSError:
                pass
            self.events.put(("folder", found, folder, only))

        threading.Thread(target=scan, daemon=True).start()

    def _add_to_send(self, paths: list[Path], tick: bool = True) -> None:
        """Put files on the PC to iPhone list (ticked or not), so they can be checked before sending."""
        if self.mode.get() != "send":
            self.mode.set("send")
            self._mode_changed()
        known = {f.path for f in self.all_files}
        added = 0
        for p in paths:
            try:
                st = p.stat()
            except OSError:
                continue
            if str(p) not in known:
                self.all_files.append(RemoteFile(str(p), st.st_size, st.st_mtime))
                if tick:
                    self.selected.add(str(p))
                added += 1
        self._fill()
        self.status.config(text="")

    def send_selected(self) -> None:
        paths = [Path(f.path) for f in self.selected_files()]
        try:
            root = Path(os.path.commonpath([str(p.parent) for p in paths]))
        except ValueError:  # different drives
            messagebox.showerror(T("Different drives"), T("Send files from one drive at a time: untick the others."))
            return
        self._start_send(paths, root)

    def _start_send(self, paths: list[Path], root: Path) -> None:
        remote = "/" + self.remote.get().strip().strip("/")
        if not paths:
            self.status.config(text=T("Nothing to send."))
            return
        if remote == "/DCIM" or remote.startswith("/DCIM/"):
            messagebox.showerror(T("Not allowed"), T("Sending into /DCIM can confuse the Photos app. Pick another folder."))
            return
        self.cancel.clear()
        self._set_progress(0)
        self.total = sum(p.stat().st_size for p in paths)
        self.done = {}
        self.verb, self.nfiles, self.sizes = "Sending", len(paths), {}
        self.status.config(text=T("Sending {n} files to {remote}…", n=len(paths), remote=remote))
        self._submit(self._send(paths, root, remote))

    async def _send(self, paths, root, remote) -> None:
        def progress(path: str, done: int, total: int) -> None:
            self.events.put(("progress", path, done, total))

        app = SEND_TO[self.send_names[self.send_to.get()]]
        dest = await self.source.open_app(app) if app else self.source
        try:
            summary = await upload_files(dest, paths, root, "" if remote == "/" and app else remote, progress, self.cancel.is_set)
        finally:
            if app:
                await dest.close()
        self.events.put(("finished", summary, "Sent", "already there"))

    # ---- UI thread: drain events --------------------------------------------------------------
    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll)

    def _handle(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "listed":
            self.auto = False
            if self.mode.get() == "send":  # the PC to iPhone list on screen is not replaced
                self._stash["receive"] = (ev[1], set())
                self.busy = False
                self.status.config(text="")
                self._sync_buttons()
                return
            self.all_files = ev[1]
            self.thumb_asked.clear()
            self.busy = False
            self._fill()
            span = None
            if ev[1]:
                lo, hi = (datetime.fromtimestamp(t(f.mtime for f in ev[1])).strftime("%Y-%m-%d") for t in (min, max))
                span = (lo, hi)
            self.status.config(text=(T("{n} found from {lo} to {hi}", n=len(ev[1]), lo=span[0], hi=span[1]) if span
                                     else T("{n} found", n=len(ev[1])))
                               if self.mode.get() == "receive" else "")
            self._sync_buttons()
        elif kind == "devices":
            self.scanning = False
            plugged = ev[1] or bool(self.source_dir)
            if plugged and not self.source and not self.busy:
                self.connect()
                self.auto = True
            elif not plugged and self.source and not self.busy:
                asyncio.run_coroutine_threadsafe(self.source.close(), self.loop)
                self.source, self.failed = None, False
                self._stash["receive"] = ([], set())
                if self.mode.get() == "receive":
                    self.all_files, self.selected = [], set()
                    self._fill()
                self.status.config(text=T("Plug in your iPhone with the cable."))
                self._sync_buttons()
        elif kind == "folder":
            _, files, folder, only = ev
            if only and self.mode.get() == "send":
                self.all_files = []
                self.selected = {s for s in self.selected if Path(s).parent == folder}
            self._add_to_send(files, tick=False)
            if not files:
                self.status.config(text=T("Nothing found in {folder}.", folder=folder))
        elif kind == "preview":
            _, png, win, label = ev
            if win.winfo_exists():
                if png:
                    label.image = tk.PhotoImage(data=png)  # kept on the label so tk does not drop it
                    label.config(image=label.image, text="", padx=0, pady=0)
                else:
                    label.config(text=T("No preview for this file."))
        elif kind == "thumb":
            _, path, png = ev
            img = tk.PhotoImage(data=png)
            self.thumb_by_path[path] = img
            self._schedule_redraw()
        elif kind == "thumbfail":
            self.thumb_fails[ev[1]] = self.thumb_fails.get(ev[1], 0) + 1
            if self.thumb_fails[ev[1]] < 3:  # three tries, then it keeps its grey box
                self.thumb_asked.discard(ev[1])
                self.root.after(1500, self._load_visible)
        elif kind == "progress":
            _, path, done, total = ev
            self.done[path] = done  # bytes done per file; the bar sums them
            self.sizes[path] = total
            pct = 100 * sum(self.done.values()) / max(1, self.total)
            self._set_progress(pct)
            finished = sum(1 for q, d in self.done.items() if d >= self.sizes.get(q, 0))
            name = PurePosixPath(path.replace("\\", "/")).name
            self.status.config(text=T("{verb} {i} of {n}: {name}  ({pct:.0f}%)", verb=T(self.verb), i=min(finished + 1, self.nfiles), n=self.nfiles, name=name, pct=pct))
        elif kind == "finished":
            s = ev[1]
            self.busy = False
            self._set_progress(100 if not (s.aborted or self.cancel.is_set()) else self.progress)
            verb, already = ev[2:] if len(ev) > 2 else ("Copied", "already done")
            msg = T("{verb} {n} ({size}), skipped {skipped} {already}, failed {failed}.", verb=T(verb), n=len(s.copied),
                    size=_human(s.bytes_copied), skipped=len(s.skipped), already=T(already), failed=len(s.failed))
            if self.cancel.is_set():
                msg += T(" Cancelled; run it again to continue.")
            if s.aborted:
                self._drop_source()
                msg += T(" Stopped early: {why}. Unlock the phone; it reconnects by itself, then copy again to resume.", why=s.aborted)
            self.status.config(text=msg)
            if s.failed:
                messagebox.showwarning(T("Some files failed"), "\n".join(f"{r}: {e}" for r, e in s.failed[:10]))
            self._sync_buttons()
            self.root.after_idle(self._load_visible)
        elif kind == "error":
            e = ev[1]
            self.busy = False
            if self.auto:  # the watcher tries again on its next look; no pop-up for a phone that is still locked
                self.auto, self.failed = False, False
                self.status.config(text=T("Unlock your iPhone and tap Trust."))
                self._sync_buttons()
                return
            self._drop_source()
            self.status.config(text=T("{err}. Check the cable, unlock the phone; it reconnects by itself.", err=f"{type(e).__name__}: {e}"))
            messagebox.showerror(
                T("Could not complete"),
                T("{err}\n\nCheck: cable plugged in, phone unlocked, 'Trust This Computer' accepted.", err=f"{type(e).__name__}: {e}"),
            )
            self._sync_buttons()

    def _quit(self) -> None:
        _save_settings({**_load_settings(), "dest": self.dest.get().strip()})
        self.cancel.set()
        if getattr(self, "_wifi_server", None):
            self._wifi_server.stop()
        if self.source:
            asyncio.run_coroutine_threadsafe(self.source.close(), self.loop).result(timeout=5)
        self.root.destroy()


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _apply_scale(scale: float) -> None:
    """Grow the grid's pixel sizes on scaled displays (call before App is built)."""
    global THUMB, PAD, CELL, HEAD_H
    THUMB, PAD, HEAD_H = round(72 * scale), round(2 * scale), round(30 * scale)  # PAD: a gap between tiles, so neighbours stay apart
    CELL = THUMB + 2 * PAD


def run(source_dir: str | None = None, udid: str | None = None) -> int:
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    root = tk.Tk()
    try:
        dpi = root.winfo_fpixels("1i")
        root.tk.call("tk", "scaling", dpi / 72.0)
        _apply_scale(dpi / 96.0)
    except tk.TclError:
        pass
    App(root, source_dir, udid)
    root.mainloop()
    return 0
