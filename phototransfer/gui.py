"""Desktop window (tkinter, no extra dependencies): browse the camera roll, pick files, copy them."""
from __future__ import annotations

import asyncio
import calendar
import json
import os
import queue
import threading
import tkinter as tk
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk

from .cli import DEFAULT_ROOT, DEFAULT_UPLOAD_ROOT, _human
from .engine import copy_files, upload_files
from .sources.base import RemoteFile, Source
from .thumbs import THUMB, audio_to_wav, is_audio, is_image, is_video, is_wav, load_thumb, local_thumb

SETTINGS_FILE = Path.home() / ".phototransfer" / "settings.json"
POLL_MS = 100
PAD = 2  # half the gap between tiles; the web page uses a 4 px gap
CELL = THUMB + 2 * PAD  # one grid cell: the thumbnail box plus padding
HEAD_H = 34  # height of a day heading
SCROLL_UNIT = 40  # pixels per scroll unit
WHEEL_UNITS = 3  # units per mouse wheel notch (about 120 px)
TIP_DELAY_MS = 350
PREVIEW = 900  # longest side of the picture opened by a double-click
MAX_PREVIEW = 3000  # most files listed from one folder in PC to iPhone
AUDIO = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".webm", ".mka"}  # .webm: YouTube audio downloads
KINDS = {
    "All": None,
    "Photos": {".heic", ".jpg", ".jpeg", ".png", ".dng", ".gif", ".tiff", ".webp"},
    "Videos": {".mov", ".mp4", ".m4v"},
    "Music": AUDIO,
}
# PC to iPhone destinations. The Music app cannot be filled over USB, so music goes into the Documents
# folder of a player that allows file sharing; the first choice is a plain folder (what photos use).
SEND_TO = {
    "Folder on iPhone": None,
    "VLC (music player)": "org.videolan.vlc-ios",
    "Documents by Readdle": "com.readdle.ReaddleDocsIPad",
}
APP_NAME = "Dropboxer"
# Same palette as the Wi-Fi web page: white, thin pale lines, one green accent.
BG, HEAD, PANEL, SOFT, LINE = "#ffffff", "#f3f7ee", "#ffffff", "#f3f6ee", "#dfe3d9"
TEXT, MUTED, GREEN, GREEN_DARK, RED = "#1f2a1f", "#6b7568", "#5ea500", "#4d8a00", "#c0392b"


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


class DateField(ttk.Frame):
    """A date button that opens a month calendar (Mo-Su grid, month arrows, Clear and Today).

    `var` holds the date as YYYY-MM-DD, or "" for no date; `on_change` runs after a pick or Clear.
    """

    def __init__(self, parent, var: tk.StringVar, placeholder: str, on_change) -> None:
        super().__init__(parent)
        self.var, self.placeholder, self.on_change = var, placeholder, on_change
        self.pop: tk.Toplevel | None = None
        self.btn = ttk.Button(self, command=self.toggle, width=16)
        self.btn.pack()
        var.trace_add("write", lambda *_: self._label())
        self._label()

    def _label(self) -> None:
        text = self.var.get()
        self.btn.config(text=(datetime.strptime(text, "%Y-%m-%d").strftime("%d %b %Y") if text else self.placeholder) + "   ▾")

    def toggle(self) -> None:
        if self.pop:
            self.close()
            return
        try:
            shown = datetime.strptime(self.var.get(), "%Y-%m-%d")
        except ValueError:
            shown = datetime.now()
        self.year, self.month = shown.year, shown.month
        self.pop = pop = tk.Toplevel(self)
        pop.overrideredirect(True)
        pop.configure(background=LINE, padx=1, pady=1)
        self.body = tk.Frame(pop, background="white", padx=10, pady=8)
        self.body.pack()
        self._draw()
        pop.update_idletasks()
        x, y = self.winfo_rootx(), self.winfo_rooty() + self.winfo_height() + 2
        pop.geometry(f"+{max(0, min(x, self.winfo_screenwidth() - pop.winfo_reqwidth()))}+{y}")
        pop.bind("<Escape>", lambda _e: self.close())
        pop.bind("<Button-1>", self._click_away)
        pop.focus_set()
        pop.grab_set()

    def _click_away(self, e) -> None:
        p = self.pop
        if p and not (p.winfo_rootx() <= e.x_root < p.winfo_rootx() + p.winfo_width()
                      and p.winfo_rooty() <= e.y_root < p.winfo_rooty() + p.winfo_height()):
            self.close()

    def close(self) -> None:
        if self.pop:
            self.pop.grab_release()
            self.pop.destroy()
            self.pop = None

    def _shift(self, months: int) -> None:
        n = self.year * 12 + self.month - 1 + months
        self.year, self.month = n // 12, n % 12 + 1
        self._draw()

    def _pick(self, value: str) -> None:
        self.close()
        self.var.set(value)
        self.on_change()

    def _draw(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        font = ("Segoe UI", 10)
        head = tk.Frame(self.body, background="white")
        head.pack(fill="x", pady=(0, 6))
        tk.Label(head, text=datetime(self.year, self.month, 1).strftime("%B %Y"), background="white", foreground=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        for arrow, step in (("↓", 1), ("↑", -1)):
            b = tk.Label(head, text=arrow, background="white", foreground=TEXT, font=("Segoe UI", 13), cursor="hand2", padx=8)
            b.pack(side="right")
            b.bind("<Button-1>", lambda _e, s=step: self._shift(s))
        grid = tk.Frame(self.body, background="white")
        grid.pack()
        for c, name in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
            tk.Label(grid, text=name, width=4, background="white", foreground=TEXT, font=("Segoe UI", 10, "bold")).grid(row=0, column=c)
        today, chosen = datetime.now().date(), self.var.get()
        for i, d in enumerate(calendar.Calendar(0).itermonthdates(self.year, self.month)):
            inside, sel = d.month == self.month, d.isoformat() == chosen
            cell = tk.Label(grid, text=str(d.day), width=4, font=font, cursor="hand2",
                            background=GREEN if sel else "white", foreground="white" if sel else TEXT if inside else "#b3baac",
                            highlightthickness=1, highlightbackground=GREEN_DARK if d == today and not sel else cell_bg(sel))
            cell.grid(row=1 + i // 7, column=i % 7, pady=1)
            cell.bind("<Button-1>", lambda _e, v=d.isoformat(): self._pick(v))
            if not sel:
                cell.bind("<Enter>", lambda _e, w=cell: w.config(background=SOFT))
                cell.bind("<Leave>", lambda _e, w=cell: w.config(background="white"))
        foot = tk.Frame(self.body, background="white")
        foot.pack(fill="x", pady=(8, 0))
        for text, side, value in (("Clear", "left", ""), ("Today", "right", today.isoformat())):
            b = tk.Label(foot, text=text, background="white", foreground=GREEN_DARK, font=font, cursor="hand2")
            b.pack(side=side)
            b.bind("<Button-1>", lambda _e, v=value: self._pick(v))


def cell_bg(selected: bool) -> str:
    return GREEN if selected else "white"


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
        self.failed = False  # the last connection or transfer broke: the button offers Reconnect
        self.verb, self.nfiles, self.sizes = "Copying", 0, {}  # for the "3 of 80" progress text
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
        w = min(round(1100 * k), right - left - 20)
        h = min(round(860 * k), bottom - top - 50)  # 50: the title bar and window borders
        root.geometry(f"{w}x{h}+{left + (right - left - w) // 2}+{top + (bottom - top - h - 40) // 2}")
        self._build()
        self._mode_changed()  # PC to iPhone is the starting mode
        root.protocol("WM_DELETE_WINDOW", self._quit)
        root.bind("<Control-a>", lambda e: self._key(e, self.select_all))
        root.bind("<Escape>", lambda e: self._key(e, self.clear_selection))
        root.after(POLL_MS, self._poll)

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
                  indicatorbackground=[("selected", GREEN), ("!selected", "white")])
        s.configure("Card.TFrame", background=PANEL, bordercolor=LINE, borderwidth=1, relief="solid")
        s.configure("Link.TButton", background=HEAD, foreground=MUTED, borderwidth=0, relief="flat", bordercolor=HEAD,
                    lightcolor=HEAD, darkcolor=HEAD)
        s.map("Link.TButton", foreground=[("active", GREEN_DARK)], background=[("active", HEAD)], bordercolor=[("active", HEAD)])
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED)
        s.configure("Title.TLabel", background=HEAD, foreground=GREEN_DARK, font=("Segoe UI", 17, "bold"))
        s.configure("Sub.TLabel", background=HEAD, foreground=MUTED)
        # Outlined buttons with a thin line; the main action is solid green.
        s.configure("TButton", background="white", foreground=TEXT, bordercolor=LINE, lightcolor="white",
                    darkcolor="white", borderwidth=1, padding=(14, 6), relief="solid")
        s.map("TButton", background=[("disabled", "#fafbf8"), ("active", SOFT)], foreground=[("disabled", "#b3baac"), ("active", GREEN_DARK)],
              bordercolor=[("disabled", LINE), ("active", GREEN)])
        for name, bg, hover, off, fg, fill in (("Primary", GREEN, GREEN_DARK, "#cfe3b3", "white", True),
                                                ("Danger", "white", "#fdf1f0", "white", RED, False)):
            s.configure(f"{name}.TButton", background=bg, foreground=fg, font=("Segoe UI", 10, "bold"),
                        bordercolor=bg if fill else "#e8b9b3", lightcolor=bg, darkcolor=bg)
            s.map(f"{name}.TButton", background=[("disabled", off), ("active", hover)],
                  foreground=[("disabled", "white" if fill else "#e3c3bf")],
                  bordercolor=[("disabled", off if fill else LINE), ("active", hover if fill else RED)])
        s.configure("Seg.Toolbutton", background="white", foreground=TEXT, bordercolor=LINE,
                    lightcolor="white", darkcolor="white", padding=(14, 6), borderwidth=1, relief="solid")
        s.map("Seg.Toolbutton", background=[("selected", GREEN), ("active", SOFT)],
              foreground=[("selected", "white")], bordercolor=[("selected", GREEN), ("active", GREEN)])
        s.configure("TEntry", fieldbackground="white", foreground=TEXT, insertcolor=TEXT, padding=6)
        s.map("TEntry", fieldbackground=[("readonly", SOFT)], bordercolor=[("focus", GREEN)])
        s.configure("TSpinbox", fieldbackground="white", foreground=TEXT, insertcolor=TEXT, arrowcolor=TEXT, padding=4)
        s.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorforeground="white")
        s.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", GREEN), ("!selected", "white")])
        s.configure("Horizontal.TProgressbar", troughcolor=SOFT, background=GREEN, bordercolor=LINE,
                    lightcolor=GREEN, darkcolor=GREEN, thickness=8)
        s.configure("Vertical.TScrollbar", troughcolor=BG, background="#d4d9cd", bordercolor=BG, arrowcolor=BG,
                    lightcolor="#d4d9cd", darkcolor="#d4d9cd")
        s.map("Vertical.TScrollbar", background=[("active", "#bfc6b6")])

    def _build(self) -> None:
        self._style()
        self.mode = tk.StringVar(value="send")  # "receive" = iPhone to PC, "send" = PC to iPhone
        head = ttk.Frame(self.root, style="Head.TFrame", padding=(16, 12))
        head.pack(fill="x")
        tk.Frame(self.root, height=1, background=LINE).pack(fill="x")  # thin line under the header
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Button(head, text="Settings", style="Link.TButton", command=self.toggle_settings).pack(side="right", padx=(0, 8))

        self.top = top = ttk.Frame(self.root, padding=(16, 12, 16, 6))
        top.pack(fill="x")
        seg = ttk.Frame(top)
        seg.pack(side="left")
        for text, value in (("PC to iPhone", "send"), ("iPhone to PC", "receive")):
            ttk.Radiobutton(seg, text=text, value=value, variable=self.mode, style="Seg.Toolbutton",
                            command=self._mode_changed).pack(side="left", padx=(0, 2))
        self.connect_btn = ttk.Button(top, text="Connect iPhone", style="Primary.TButton", command=self.connect)
        self.connect_btn.pack(side="left", padx=(16, 0))
        # iPhone to PC: Wi-Fi is the easy way, so it sits right next to the mode switch.
        self.wifi_btn = ttk.Button(top, text="Send over Wi-Fi", style="Primary.TButton", command=self.wifi_share)
        self.count = ttk.Label(top, text="", style="Muted.TLabel")
        self.count.pack(side="right")

        # Filters and select-all sit in view, because they are used all the time.
        bar = ttk.Frame(self.root, padding=(16, 0, 16, 2))
        bar.pack(fill="x")
        ttk.Label(bar, text="Pick", font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 10))
        self.kind = tk.StringVar(value="All")
        for name in KINDS:
            ttk.Radiobutton(bar, text=name, value=name, variable=self.kind, style="Seg.Toolbutton",
                            command=self._fill).pack(side="left", padx=(0, 2))
        self.all_ticked = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Select all", variable=self.all_ticked, command=self._toggle_all).pack(side="left", padx=(16, 0))
        self.date_from, self.date_to = tk.StringVar(), tk.StringVar()
        ttk.Button(bar, text="Reset", command=self.clear_dates).pack(side="right")
        for var, text, hint in ((self.date_to, "to", "Any date"), (self.date_from, "Taken from", "Any date")):
            DateField(bar, var, hint, self._fill).pack(side="right", padx=(4, 0))
            ttk.Label(bar, text=text, style="Muted.TLabel").pack(side="right", padx=(10, 0))
        ttk.Label(self.root, style="Muted.TLabel", padding=(16, 2, 16, 6),
                  text="Click to select  ·  Shift-click for a range  ·  double-click to enlarge").pack(fill="x")

        # Everything else lives in Settings, which starts hidden.
        self.settings = ttk.Frame(self.root, style="Card.TFrame", padding=14)
        row = ttk.Frame(self.settings, style="Panel.TFrame")
        row.pack(fill="x", pady=(10, 0))
        self.by_date = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Sort into folders by date", variable=self.by_date, style="Panel.TCheckbutton").pack(side="left")
        ttk.Label(row, text="Copy at once", style="PanelMuted.TLabel").pack(side="left", padx=(20, 6))
        self.jobs = tk.IntVar(value=1)
        ttk.Spinbox(row, from_=1, to=4, width=3, textvariable=self.jobs).pack(side="left")

        mid = ttk.Frame(self.root, padding=(16, 0))
        mid.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(mid, background=BG, highlightthickness=0, yscrollincrement=SCROLL_UNIT)
        sb = ttk.Scrollbar(mid, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=lambda a, b: (sb.set(a, b), self._schedule_redraw()))
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-WHEEL_UNITS if e.delta > 0 else WHEEL_UNITS, "units"))
        self.canvas.bind("<Configure>", lambda _e: self.root.after_idle(self._relayout))
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Double-Button-1>", self._open_preview)  # the two clicks cancel out, the tick is unchanged
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self._hide_tip(reset=True))

        panel = ttk.Frame(self.root, style="Card.TFrame", padding=14)
        panel.pack(fill="x", padx=16, pady=(10, 4))
        self.dest_row = row = ttk.Frame(panel, style="Panel.TFrame")
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="Save photos in", style="Panel.TLabel").pack(side="left")
        self.dest = tk.StringVar(value=_load_settings().get("dest") or str(Path.home() / "Pictures" / "iPhone"))
        ttk.Entry(row, textvariable=self.dest).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="Change folder…", command=self.browse).pack(side="left")
        self.remote_row = row = ttk.Frame(panel, style="Panel.TFrame")  # PC to iPhone: where on the phone
        ttk.Label(row, text="Send to", style="Panel.TLabel").pack(side="left")
        self.send_to = tk.StringVar(value="Folder on iPhone")
        pick = ttk.Combobox(row, textvariable=self.send_to, values=list(SEND_TO), state="readonly", width=20)
        pick.pack(side="left", padx=8)
        pick.bind("<<ComboboxSelected>>", lambda _e: self._send_to_changed())
        self.remote = tk.StringVar(value=DEFAULT_UPLOAD_ROOT)
        self.remote_label = ttk.Label(row, text="folder", style="Panel.TLabel")
        self.remote_label.pack(side="left")
        ttk.Entry(row, textvariable=self.remote, width=16).pack(side="left", padx=8)
        self.send_note = ttk.Label(row, text="Won't show in Photos.", style="PanelMuted.TLabel")
        self.send_note.pack(side="left")
        row = ttk.Frame(panel, style="Panel.TFrame")
        row.pack(fill="x")
        self.copy_btn = ttk.Button(row, text="Copy to PC", style="Primary.TButton", command=self.primary_action, state="disabled")
        self.copy_btn.pack(side="left")
        self.add_btn = ttk.Button(row, text="Add files…", command=self.choose_files)  # shown in PC to iPhone mode only
        self.folder_btn = ttk.Button(row, text="Show a folder…", command=self.send_folder)
        self.cancel_btn = ttk.Button(row, text="Stop", style="Danger.TButton", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.bar = ttk.Progressbar(row, maximum=100)
        self.bar.pack(side="left", fill="x", expand=True, padx=8)

        self.status = ttk.Label(self.root, text="Plug in and unlock your iPhone, then click Connect.",
                                style="Muted.TLabel", padding=(16, 4, 16, 12))
        # Pin the action panel and status line to the bottom so a short window shrinks the picture grid, not them.
        self.status.pack(side="bottom", fill="x", before=mid)
        panel.pack(side="bottom", fill="x", padx=16, pady=(10, 4), before=mid)

    def toggle_settings(self) -> None:
        if self.settings.winfo_manager():
            self.settings.pack_forget()
        else:
            self.settings.pack(fill="x", padx=16, pady=(0, 6), after=self.top)

    def _mode_changed(self) -> None:
        """Switch between copying from the iPhone and sending to it."""
        sending = self.mode.get() == "send"
        self._stash["receive" if sending else "send"] = (self.all_files, self.selected)
        self.all_files, self.selected = self._stash["send" if sending else "receive"]
        if sending:
            self.dest_row.pack_forget()
            self.wifi_btn.pack_forget()
            self.remote_row.pack(fill="x", pady=(0, 12), before=self.copy_btn.master)
            self.add_btn.pack(side="left", padx=(8, 0), after=self.copy_btn)
            self.folder_btn.pack(side="left", padx=(8, 0), after=self.add_btn)
            if not self.all_files and not self.send_scanned:  # first visit: preview the Pictures folder
                self.send_scanned = True
                self._show_folder(Path.home() / "Pictures")
                self._show_folder(Path.home() / "Music")
            self.status.config(text="Pick files, then click Send." if self.all_files else "Looking for files…")
        else:
            self.remote_row.pack_forget()
            self.wifi_btn.pack(side="left", padx=(8, 0), after=self.connect_btn)
            self.dest_row.pack(fill="x", pady=(0, 12), before=self.copy_btn.master)
            self.add_btn.pack_forget()
            self.folder_btn.pack_forget()
            self.status.config(text="Pick photos, then click Copy to PC.")
        self._fill()

    def primary_action(self) -> None:
        if self.mode.get() == "send":
            self.send_selected() if self.selected_files() else self.choose_files()
        else:
            self.start_copy()

    # ---- list ---------------------------------------------------------------------------------
    def _date_bounds(self) -> tuple[float | None, float | None]:
        """(from, to) as timestamps; 'to' is the start of the day after, so the last day is included."""
        def parse(text: str) -> datetime | None:
            return datetime.strptime(text.strip(), "%Y-%m-%d") if text.strip() else None

        lo, hi = parse(self.date_from.get()), parse(self.date_to.get())
        return (lo.timestamp() if lo else None), ((hi + timedelta(days=1)).timestamp() if hi else None)

    def clear_dates(self) -> None:
        self.date_from.set("")
        self.date_to.set("")
        self._fill()

    def _fill(self) -> None:
        try:
            lo, hi = self._date_bounds()
        except ValueError:
            self.status.config(text="Dates must look like 2026-10-26.")
            return
        exts = KINDS[self.kind.get()]
        self.shown = sorted(
            (f for f in self.all_files
             if (exts is None or Path(f.path).suffix.lower() in exts)
             and (lo is None or f.mtime >= lo) and (hi is None or f.mtime < hi)),
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
            msg = ("Send files from this PC to your iPhone.\nClick Show a folder… or Add files… to see what you can send."
                   if self.mode.get() == "send" and not self.all_files else
                   "No files match these filters." if self.all_files else
                   "Plug in your iPhone, unlock it,\ntap Trust, then click Connect iPhone.")
            c.create_text(c.winfo_width() // 2, c.winfo_height() // 2, text=msg, fill=MUTED, justify="center",
                          font=("Segoe UI", 13))
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
                title = c.create_text(PAD + 2, y + HEAD_H // 2, anchor="w", font=("Segoe UI", 11, "bold"),
                                      fill=TEXT, text=f"{mark}{day}")
                c.create_text(c.bbox(title)[2] + 12, y + HEAD_H // 2, anchor="w", font=("Segoe UI", 10), fill=MUTED,
                              text=f"{len(b)} files, {_human(sum(f.size for f in b))}   (click to select the day)")
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
            if is_video(f.path):
                c.create_text(x0 + THUMB - 6, y0 + THUMB - 6, anchor="se", text="▶", fill="white", font=("Segoe UI", 10))
            if f.path in self.selected:
                c.create_rectangle(x0, y0, x0 + THUMB, y0 + THUMB, outline=GREEN, width=3)
                c.create_oval(x0 + 6, y0 + 6, x0 + 28, y0 + 28, fill=GREEN, outline="white", width=2)
                c.create_text(x0 + 17, y0 + 17, text="✓", fill="white", font=("Segoe UI", 11, "bold"))
            self.hits.append((x0, y0, x0 + THUMB, y0 + THUMB, "cell", f))
            self.visible.append(f)
        self._load_visible()

    def _hit(self, e: tk.Event) -> tuple | None:
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        return next((h for h in self.hits if h[0] <= x < h[2] and h[1] <= y < h[3]), None)

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
        label = tk.Label(win, text="Loading…", background=BG, foreground=TEXT, padx=60, pady=40)
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
        tk.Label(self.tip, text=f"{Path(f.path).name}\n{when}\n{_human(f.size)}", justify="left", background="white",
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
        self.count.config(text=f"{len(sel)} selected ({_human(sum(f.size for f in sel))})  ·  {len(self.shown)} shown")
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        n = len(self.selected_files())
        ready = bool(self.source) and not self.busy
        if self.mode.get() == "send":
            self.copy_btn.config(text=f"Send {n} to iPhone" if n else "Choose files…",
                                 state=("normal" if ready else "disabled") if n else ("disabled" if self.busy else "normal"))
            self.add_btn.config(state="disabled" if self.busy else "normal")
        else:
            self.copy_btn.config(text=f"Copy {n} to PC" if n else "Copy to PC", state="normal" if ready and n else "disabled")
        self.connect_btn.config(text="Reconnect iPhone" if self.failed else "Reload photos" if self.source else "Connect iPhone")
        self.cancel_btn.config(state="normal" if self.busy else "disabled")
        self.connect_btn.config(state="disabled" if self.busy else "normal")

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

    def connect(self) -> None:
        self.failed = False
        self.status.config(text="Connecting…")
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
        try:
            jobs = min(4, max(1, int(self.jobs.get())))
        except tk.TclError:  # empty or non-numeric Parallel box
            jobs = 1
            self.jobs.set(1)
        self.cancel.clear()
        self.bar.config(value=0)
        self.total = sum(f.size for f in files)
        self.done: dict[str, int] = {}
        self.verb, self.nfiles, self.sizes = "Copying", len(files), {}
        self.status.config(text=f"Copying {len(files)} files…")
        self._submit(self._copy(files, Path(dest), jobs, self.by_date.get()))

    async def _copy(self, files, dest, jobs, by_date) -> None:
        def progress(path: str, done: int, total: int) -> None:
            self.events.put(("progress", path, done, total))

        summary = await copy_files(self.source, files, DEFAULT_ROOT, dest, progress, jobs, by_date, self.cancel.is_set)
        self.events.put(("finished", summary))

    def wifi_share(self) -> None:
        """Share the 'Copy to' folder with phones on this Wi-Fi: view photos there, upload from the phone."""
        if getattr(self, "wifi_win", None) and self.wifi_win.winfo_exists():
            self.wifi_win.lift()
            return
        from .wifi import WifiServer, persistent_token, qr_png

        folder = Path(self.dest.get().strip() or ".")
        token = persistent_token()

        def start(tls: bool) -> WifiServer:
            server = WifiServer(folder, folder, token=token, tls=tls)
            server.start()
            return server

        try:
            folder.mkdir(parents=True, exist_ok=True)
            server = start(False)
        except OSError as e:
            messagebox.showerror("Wi-Fi share", f"Could not start sharing: {e}")
            return
        win = self.wifi_win = tk.Toplevel(self.root)
        win.title(f"{APP_NAME} - Wi-Fi share")
        win.configure(background=BG)
        qr = ttk.Label(win)
        qr.pack(padx=12, pady=(12, 4))
        ttk.Label(win, text="Scan with your phone (same Wi-Fi)").pack()
        link = ttk.Entry(win, width=44)
        link.pack(padx=12, pady=4)
        away_label = ttk.Label(win, text="Away from home:")
        away = ttk.Entry(win, width=44)
        https = tk.BooleanVar(value=False)
        note = ttk.Label(win, justify="center", wraplength=340, padding=8)
        count = ttk.Label(win, text="0 files received")

        def show() -> None:
            self.qr_img = tk.PhotoImage(data=qr_png(server.url))  # keep a reference or tk drops it
            qr.config(image=self.qr_img)
            link.config(state="normal")
            link.delete(0, "end")
            link.insert(0, server.url)
            link.config(state="readonly")
            text = f"Saves to: {folder}"
            if server.tls:
                text += f"\nFirst time, trust: {server.cert_url}"
            away.config(state="normal")
            away.delete(0, "end")
            if server.remote_url:
                away.insert(0, server.remote_url)
                away_label.pack(after=link)
                away.pack(after=away_label, padx=12, pady=4)
            else:
                away_label.pack_forget()
                away.pack_forget()
            away.config(state="readonly")
            note.config(text=text)

        def toggle() -> None:
            nonlocal server
            server.stop()
            try:
                server = start(https.get())
            except OSError as e:
                messagebox.showerror("Wi-Fi share", f"Could not restart sharing: {e}")
                https.set(False)
                server = start(False)
            self._wifi_server = server
            count.config(text="0 files received")
            show()

        ttk.Checkbutton(win, text="HTTPS (save many photos at once)", variable=https,
                        command=toggle).pack()
        note.pack()
        count.pack()

        def tick() -> None:
            if win.winfo_exists():
                count.config(text=f"{len(server.received)} files received")
                win.after(1000, tick)

        def close() -> None:
            server.stop()
            win.destroy()

        ttk.Button(win, text="Stop sharing", style="Danger.TButton", command=close).pack(pady=(4, 14))
        win.protocol("WM_DELETE_WINDOW", close)
        self._wifi_server = server
        show()
        tick()

    def _send_to_changed(self) -> None:
        app = SEND_TO[self.send_to.get()]
        self.remote.set(DEFAULT_UPLOAD_ROOT if app is None else "")
        self.remote_label.config(text="folder" if app is None else "subfolder (optional)")
        self.send_note.config(text="Won't show in Photos." if app is None else
                              f"Find them in {self.send_to.get().split(' (')[0]}.")

    def choose_files(self) -> None:
        names = filedialog.askopenfilenames(title="Choose files to send to the phone", filetypes=[
            ("All files", "*.*"), ("Music", " ".join(f"*{e}" for e in sorted(AUDIO))),
            ("Photos and videos", " ".join(f"*{e}" for e in sorted(KINDS["Photos"] | KINDS["Videos"])))])
        if names:
            self._add_to_send([Path(n) for n in names])

    def send_folder(self) -> None:
        d = filedialog.askdirectory(title="Choose a folder to send to the phone")
        if d:
            self._show_folder(Path(d))

    def _show_folder(self, folder: Path) -> None:
        """List the photos and videos inside a folder (nothing ticked yet) so they can be previewed and picked."""
        exts = KINDS["Photos"] | KINDS["Videos"] | AUDIO
        self.status.config(text=f"Looking in {folder}…")

        def scan() -> None:
            found = []
            try:
                for p in folder.rglob("*"):
                    if p.suffix.lower() in exts and p.is_file():
                        found.append(p)
                        if len(found) >= MAX_PREVIEW:
                            break
            except OSError:
                pass
            self.events.put(("folder", found, folder))

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
        self.status.config(text=f"{added} files added, {len(self.selected_files())} ticked. "
                                "Click a picture to tick or untick it, then click Send.")

    def send_selected(self) -> None:
        paths = [Path(f.path) for f in self.selected_files()]
        try:
            root = Path(os.path.commonpath([str(p.parent) for p in paths]))
        except ValueError:  # different drives
            messagebox.showerror("Different drives", "Send files from one drive at a time: untick the others.")
            return
        self._start_send(paths, root)

    def _start_send(self, paths: list[Path], root: Path) -> None:
        remote = "/" + self.remote.get().strip().strip("/")
        if not paths:
            self.status.config(text="Nothing to send.")
            return
        if remote == "/DCIM" or remote.startswith("/DCIM/"):
            messagebox.showerror("Not allowed", "Sending into /DCIM can confuse the Photos app. Pick another folder.")
            return
        self.cancel.clear()
        self.bar.config(value=0)
        self.total = sum(p.stat().st_size for p in paths)
        self.done = {}
        self.verb, self.nfiles, self.sizes = "Sending", len(paths), {}
        self.status.config(text=f"Sending {len(paths)} files to {remote}…")
        self._submit(self._send(paths, root, remote))

    async def _send(self, paths, root, remote) -> None:
        def progress(path: str, done: int, total: int) -> None:
            self.events.put(("progress", path, done, total))

        app = SEND_TO[self.send_to.get()]
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
            if self.mode.get() == "send":  # the PC to iPhone list on screen is not replaced
                self._stash["receive"] = (ev[1], set())
                self.busy = False
                self.status.config(text="iPhone connected.")
                self._sync_buttons()
                return
            self.all_files = ev[1]
            self.thumb_asked.clear()
            self.busy = False
            self._fill()
            span = ""
            if ev[1]:
                lo, hi = (datetime.fromtimestamp(t(f.mtime for f in ev[1])).strftime("%Y-%m-%d") for t in (min, max))
                span = f" from {lo} to {hi}"
            self.status.config(text=f"{len(ev[1])} found{span}. Pick photos, then click Copy to PC."
                               if self.mode.get() == "receive" else "iPhone connected.")
            self._sync_buttons()
        elif kind == "folder":
            _, files, folder = ev
            self._add_to_send(files, tick=False)
            if not files:
                self.status.config(text=f"Nothing found in {folder}.")
        elif kind == "preview":
            _, png, win, label = ev
            if win.winfo_exists():
                if png:
                    label.image = tk.PhotoImage(data=png)  # kept on the label so tk does not drop it
                    label.config(image=label.image, text="", padx=0, pady=0)
                else:
                    label.config(text="No preview for this file.")
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
            self.bar.config(value=pct)
            finished = sum(1 for q, d in self.done.items() if d >= self.sizes.get(q, 0))
            name = PurePosixPath(path.replace("\\", "/")).name
            self.status.config(text=f"{self.verb} {min(finished + 1, self.nfiles)} of {self.nfiles}: {name}  ({pct:.0f}%)")
        elif kind == "finished":
            s = ev[1]
            self.busy = False
            self.bar.config(value=100 if not (s.aborted or self.cancel.is_set()) else self.bar["value"])
            verb, already = ev[2:] if len(ev) > 2 else ("Copied", "already done")
            msg = f"{verb} {len(s.copied)} ({_human(s.bytes_copied)}), skipped {len(s.skipped)} {already}, failed {len(s.failed)}."
            if self.cancel.is_set():
                msg += " Cancelled; run it again to continue."
            if s.aborted:
                self.failed = True
                msg += f" Stopped early: {s.aborted}. Reconnect, unlock the phone, then Connect and copy again to resume."
            self.status.config(text=msg)
            if s.failed:
                messagebox.showwarning("Some files failed", "\n".join(f"{r}: {e}" for r, e in s.failed[:10]))
            self._sync_buttons()
            self.root.after_idle(self._load_visible)
        elif kind == "error":
            e = ev[1]
            self.busy = False
            self.failed = True
            self.status.config(text=f"{type(e).__name__}: {e}. Check the cable, unlock the phone, click Reconnect.")
            messagebox.showerror(
                "Could not complete",
                f"{type(e).__name__}: {e}\n\nCheck: cable plugged in, phone unlocked, 'Trust This Computer' accepted.",
            )
            self._sync_buttons()

    def _quit(self) -> None:
        _save_settings({"dest": self.dest.get().strip()})
        self.cancel.set()
        if getattr(self, "_wifi_server", None):
            self._wifi_server.stop()
        if self.source:
            asyncio.run_coroutine_threadsafe(self.source.close(), self.loop).result(timeout=5)
        self.root.destroy()


def _apply_scale(scale: float) -> None:
    """Grow the grid's pixel sizes on scaled displays (call before App is built)."""
    global THUMB, PAD, CELL, HEAD_H
    THUMB, PAD, HEAD_H = round(100 * scale), max(1, round(2 * scale)), round(34 * scale)
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
