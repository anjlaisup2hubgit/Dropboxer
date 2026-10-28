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
ARCHIVES = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"}
KINDS = {
    "All": None,
    "Photos": {".heic", ".jpg", ".jpeg", ".png", ".dng", ".gif", ".tiff", ".webp"},
    "Videos": {".mov", ".mp4", ".m4v"},
    "Music": AUDIO,
    "Documents": DOCS,
    "Archives": ARCHIVES,
}
# PC to iPhone destinations. The Music app cannot be filled over USB, so music goes into the Documents
# folder of a player that allows file sharing; the first choice is a plain folder (what photos use).
SEND_TO = {
    "Folder on iPhone": None,
    "VLC (music player)": "org.videolan.vlc-ios",
    "Documents by Readdle": "com.readdle.ReaddleDocsIPad",
}
APP_NAME = "Dropboxer"
# Warm map-paper background, thin lines, one grass-green accent.
PAPER = "#f8f6ee"  # inputs, buttons and pop-ups: a touch lighter than the window
BG, HEAD, PANEL, SOFT, LINE = "#f1eee4", "#e8e5d8", PAPER, "#e6e4d7", "#d3d0bf"
TEXT, MUTED, GREEN, GREEN_DARK, RED = "#2b3226", "#6c7262", "#6a9444", "#527a32", "#b5574d"


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
        self.body = tk.Frame(pop, background=PAPER, padx=10, pady=8)
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
        head = tk.Frame(self.body, background=PAPER)
        head.pack(fill="x", pady=(0, 6))
        tk.Label(head, text=datetime(self.year, self.month, 1).strftime("%B %Y") if language() == "en" else T("Month {m} {y}", m=self.month, y=self.year), background=PAPER, foreground=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        for arrow, step in (("↓", 1), ("↑", -1)):
            b = tk.Label(head, text=arrow, background=PAPER, foreground=TEXT, font=("Segoe UI", 13), cursor="hand2", padx=8)
            b.pack(side="right")
            b.bind("<Button-1>", lambda _e, s=step: self._shift(s))
        grid = tk.Frame(self.body, background=PAPER)
        grid.pack()
        for c, name in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
            tk.Label(grid, text=T(name), width=4, background=PAPER, foreground=TEXT, font=("Segoe UI", 10, "bold")).grid(row=0, column=c)
        today, chosen = datetime.now().date(), self.var.get()
        for i, d in enumerate(calendar.Calendar(0).itermonthdates(self.year, self.month)):
            inside, sel = d.month == self.month, d.isoformat() == chosen
            cell = tk.Label(grid, text=str(d.day), width=4, font=font, cursor="hand2",
                            background=GREEN if sel else PAPER, foreground="white" if sel else TEXT if inside else "#aeb2a2",
                            highlightthickness=1, highlightbackground=GREEN_DARK if d == today and not sel else cell_bg(sel))
            cell.grid(row=1 + i // 7, column=i % 7, pady=1)
            cell.bind("<Button-1>", lambda _e, v=d.isoformat(): self._pick(v))
            if not sel:
                cell.bind("<Enter>", lambda _e, w=cell: w.config(background=SOFT))
                cell.bind("<Leave>", lambda _e, w=cell: w.config(background=PAPER))
        foot = tk.Frame(self.body, background=PAPER)
        foot.pack(fill="x", pady=(8, 0))
        for text, side, value in (("Clear", "left", ""), ("Today", "right", today.isoformat())):
            b = tk.Label(foot, text=T(text), background=PAPER, foreground=GREEN_DARK, font=font, cursor="hand2")
            b.pack(side=side)
            b.bind("<Button-1>", lambda _e, v=value: self._pick(v))


def cell_bg(selected: bool) -> str:
    return GREEN if selected else PAPER


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
        set_language(_load_settings().get("lang", "en"))
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
        s.map("TButton", background=[("disabled", PAPER), ("active", SOFT)], foreground=[("disabled", "#aeb2a2"), ("active", GREEN_DARK)],
              bordercolor=[("disabled", LINE), ("active", GREEN)])
        for name, bg, hover, off, fg, fill in (("Primary", GREEN, GREEN_DARK, "#c8d5b6", "white", True),
                                                ("Danger", PAPER, "#f3e6e0", PAPER, RED, False)):
            s.configure(f"{name}.TButton", background=bg, foreground=fg, font=("Segoe UI", 10, "bold"),
                        bordercolor=bg if fill else "#e3c4c0", lightcolor=bg, darkcolor=bg)
            s.map(f"{name}.TButton", background=[("disabled", off), ("active", hover)],
                  foreground=[("disabled", "white" if fill else "#e3c3bf")],
                  bordercolor=[("disabled", off if fill else LINE), ("active", hover if fill else RED)])
        s.configure("Seg.Toolbutton", background=PAPER, foreground=TEXT, bordercolor=LINE,
                    lightcolor=PAPER, darkcolor=PAPER, padding=(14, 6), borderwidth=1, relief="solid")
        s.map("Seg.Toolbutton", background=[("selected", GREEN), ("active", SOFT)],
              foreground=[("selected", "white")], bordercolor=[("selected", GREEN), ("active", GREEN)])
        s.configure("TEntry", fieldbackground=PAPER, foreground=TEXT, insertcolor=TEXT, padding=6)
        s.map("TEntry", fieldbackground=[("readonly", SOFT)], bordercolor=[("focus", GREEN)])
        s.configure("TSpinbox", fieldbackground=PAPER, foreground=TEXT, insertcolor=TEXT, arrowcolor=TEXT, padding=4)
        s.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorforeground="white")
        s.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", GREEN), ("!selected", PAPER)])
        s.configure("Horizontal.TProgressbar", troughcolor=SOFT, background=GREEN, bordercolor=LINE,
                    lightcolor=GREEN, darkcolor=GREEN, thickness=8)
        s.configure("Vertical.TScrollbar", troughcolor=BG, background="#d3d0bf", bordercolor=BG, arrowcolor=BG,
                    lightcolor="#d3d0bf", darkcolor="#d3d0bf")
        s.map("Vertical.TScrollbar", background=[("active", "#c2bfae")])

    def _build(self) -> None:
        self._style()
        self.mode = tk.StringVar(value="send")  # "receive" = iPhone to PC, "send" = PC to iPhone
        head = ttk.Frame(self.root, style="Head.TFrame", padding=(16, 12))
        head.pack(fill="x")
        tk.Frame(self.root, height=1, background=LINE).pack(fill="x")  # thin line under the header
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").pack(side="left")
        lang = ttk.Combobox(head, values=list(LANGUAGES.values()), state="readonly", width=11)
        lang.set(LANGUAGES[language()])
        lang.pack(side="right")
        lang.bind("<<ComboboxSelected>>", lambda _e: self._set_language(lang.current()))

        self.top = top = ttk.Frame(self.root, padding=(16, 12, 16, 6))
        top.pack(fill="x")
        seg = ttk.Frame(top)
        seg.pack(side="left")
        for text, value in (("PC to iPhone", "send"), ("iPhone to PC", "receive")):
            ttk.Radiobutton(seg, text=T(text), value=value, variable=self.mode, style="Seg.Toolbutton",
                            command=self._mode_changed).pack(side="left", padx=(0, 2))
        self.copy_btn = ttk.Button(top, text=T("Copy"), style="Primary.TButton", command=self.primary_action, state="disabled")
        self.copy_btn.pack(side="left", padx=(16, 0))
        self.folder_btn = ttk.Button(top, text=T("Folder…"), command=self.send_folder)  # PC to iPhone only
        self.cancel_btn = ttk.Button(top, text=T("Stop"), style="Danger.TButton", command=self.cancel.set)
        self.count = ttk.Label(top, text="", style="Muted.TLabel")
        self.count.pack(side="right")
        self.bar = ttk.Progressbar(top, maximum=100)
        self.bar.pack(side="left", fill="x", expand=True, padx=12)

        # Where the files go.
        self.rows = ttk.Frame(self.root, padding=(16, 0, 16, 8))
        self.rows.pack(fill="x")
        self.dest_row = row = ttk.Frame(self.rows)
        ttk.Label(row, text=T("Save in")).pack(side="left")
        self.dest = tk.StringVar(value=_load_settings().get("dest") or str(Path.home() / "Pictures" / "iPhone"))
        ttk.Entry(row, textvariable=self.dest).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text=T("Change"), command=self.browse).pack(side="left")
        self.remote_row = row = ttk.Frame(self.rows)  # PC to iPhone: where on the phone
        ttk.Label(row, text=T("Send to")).pack(side="left")
        self.send_names = {T(k): k for k in SEND_TO}  # what the list shows -> its key in SEND_TO
        self.send_to = tk.StringVar(value=T("Folder on iPhone"))
        pick = ttk.Combobox(row, textvariable=self.send_to, values=list(self.send_names), state="readonly", width=20)
        pick.pack(side="left", padx=8)
        pick.bind("<<ComboboxSelected>>", lambda _e: self._send_to_changed())
        self.remote = tk.StringVar(value=DEFAULT_UPLOAD_ROOT)
        self.remote_label = ttk.Label(row, text=T("folder"))
        self.remote_label.pack(side="left")
        ttk.Entry(row, textvariable=self.remote, width=16).pack(side="left", padx=8)
        self.send_note = ttk.Label(row, text=T("Won't show in Photos."), style="Muted.TLabel")
        self.send_note.pack(side="left")

        # Filters and select-all sit in view, because they are used all the time.
        bar = ttk.Frame(self.root, padding=(16, 0, 16, 2))
        bar.pack(fill="x")
        self.kind = tk.StringVar(value="All")
        for name in KINDS:
            ttk.Radiobutton(bar, text=T(name), value=name, variable=self.kind, style="Seg.Toolbutton",
                            command=self._fill).pack(side="left", padx=(0, 2))
        self.all_ticked = tk.BooleanVar(value=False)
        self.select_all_cb = ttk.Checkbutton(bar, text=T("Select all"), variable=self.all_ticked, command=self._toggle_all)
        self.select_all_cb.pack(side="left", padx=(16, 0))
        self.deselect_btn = ttk.Button(bar, text=T("Deselect all"), command=self.clear_selection)  # shown while something is selected
        self.date_from, self.date_to = tk.StringVar(), tk.StringVar()
        self.connect_btn = ttk.Button(bar, text=T("Reload"), command=self.connect)  # iPhone to PC, once files are listed
        self.wifi_btn = ttk.Button(bar, text=T("Wi-Fi"), command=self.wifi_share)
        ttk.Button(bar, text=T("Reset"), command=self.clear_dates).pack(side="right")
        for var, text, hint in ((self.date_to, T("to"), T("Any date")), (self.date_from, T("From"), T("Any date"))):
            DateField(bar, var, hint, self._fill).pack(side="right", padx=(4, 0))
            ttk.Label(bar, text=text, style="Muted.TLabel").pack(side="right", padx=(10, 0))
        ttk.Label(self.root, style="Muted.TLabel", padding=(16, 2, 16, 6),
                  text=T("Shift-click selects a range  ·  click a day to select all of it")).pack(fill="x")

        self.by_date = tk.BooleanVar(value=True)  # copies are always sorted into date folders
        self.jobs = tk.IntVar(value=1)

        mid = ttk.Frame(self.root, padding=(16, 0))
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
        for text, command in (("USB-C cable", self.connect), ("Wi-Fi share", self.wifi_share)):
            ttk.Button(self.empty, text=T(text), style="Primary.TButton", command=command, width=16).pack(side="left", padx=8, ipady=8)

        self.status = ttk.Label(self.root, text=T("Unlock your iPhone and tap Trust."),
                                style="Muted.TLabel", padding=(16, 4, 16, 12))
        self.status.pack(side="bottom", fill="x", before=mid)  # a short window shrinks the grid, not the status

    def _set_language(self, index: int) -> None:
        """Rebuild the window in the chosen language; the files, ticks, dates and folders stay as they are."""
        set_language(list(LANGUAGES)[index])
        _save_settings({**_load_settings(), "lang": language(), "dest": self.dest.get().strip()})
        mode, kind = self.mode.get(), self.kind.get()
        dates = self.date_from.get(), self.date_to.get()
        for w in self.root.winfo_children():
            w.destroy()
        self._build()
        self.mode.set(mode)
        self.kind.set(kind)
        self.date_from.set(dates[0])
        self.date_to.set(dates[1])
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
            self.folder_btn.pack(side="left", padx=(8, 0), after=self.copy_btn)
            if not self.all_files and not self.send_scanned:  # first visit: preview the Pictures folder
                self.send_scanned = True
                self._show_folder(Path.home() / "Pictures")
                self._show_folder(Path.home() / "Music")
            self.status.config(text="" if self.all_files else T("Looking for files…"))
        else:
            self.remote_row.pack_forget()
            self.dest_row.pack(fill="x")
            self.folder_btn.pack_forget()
            self.status.config(text="")
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
            self.status.config(text=T("Dates must look like 2026-10-26."))
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
            mid_x, mid_y = c.winfo_width() // 2, c.winfo_height() // 2
            if self.mode.get() == "receive" and not self.all_files:
                c.create_window(mid_x, mid_y, window=self.empty)
            else:
                c.create_text(mid_x, mid_y, text=T("No files match these filters.") if self.all_files else T("Nothing here yet."),
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
            if is_video(f.path):
                c.create_text(x0 + THUMB - 6, y0 + THUMB - 6, anchor="se", text="▶", fill="white", font=("Segoe UI", 10))
            if f.path in self.selected:
                c.create_rectangle(x0, y0, x0 + THUMB, y0 + THUMB, outline=GREEN, width=3)
                c.create_oval(x0 + 6, y0 + 6, x0 + 28, y0 + 28, fill=GREEN, outline="white", width=2)
                c.create_text(x0 + 17, y0 + 17, text="✓", fill="white", font=("Segoe UI", 11, "bold"))
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

    def _hit(self, e: tk.Event) -> tuple | None:
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        return next((h for h in self.hits if h[0] <= x < h[2] and h[1] <= y < h[3]), None)

    def _press(self, e: tk.Event) -> None:
        self.band_from = (self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        self.band = None
        self.band_base = set(self.selected)

    def _drag(self, e: tk.Event) -> None:
        """Hold the left button and drag: every tile the box touches is selected (on top of what was already)."""
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
        self.selected = self.band_base | hit
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
        if sel:
            self.deselect_btn.pack(side="left", padx=(10, 0), after=self.select_all_cb)
        else:
            self.deselect_btn.pack_forget()
        self.count.config(text=f"{T('{n} selected', n=len(sel))}  ·  {_human(sum(f.size for f in sel))}" if sel else "")
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        n = len(self.selected_files())
        ready = bool(self.source) and not self.busy
        if self.mode.get() == "send":
            self.copy_btn.config(text=T("Send {n}", n=n) if n else T("Add files…"),
                                 state=("normal" if ready else "disabled") if n else ("disabled" if self.busy else "normal"))
        else:
            self.copy_btn.config(text=T("Copy {n}", n=n) if n else T("Copy"), state="normal" if ready and n else "disabled")
        self.connect_btn.config(text=T("Reconnect") if self.failed else T("Reload"))
        show = self.mode.get() == "receive" and bool(self.all_files)  # otherwise the middle of the list has them
        for b in (self.connect_btn, self.wifi_btn):
            b.pack(side="right", padx=(0, 6)) if show else b.pack_forget()
        self.cancel_btn.pack(side="left", padx=(8, 0), after=self.copy_btn) if self.busy else self.cancel_btn.pack_forget()
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
        self.status.config(text=T("Copying {n} files…", n=len(files)))
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
            messagebox.showerror(T("Wi-Fi share"), T("Could not start sharing: {e}", e=e))
            return
        win = self.wifi_win = tk.Toplevel(self.root)
        win.title(f"{APP_NAME} - {T('Wi-Fi share')}")
        win.configure(background=BG)
        qr = ttk.Label(win)
        qr.pack(padx=12, pady=(12, 4))
        ttk.Label(win, text=T("Scan with your phone (same Wi-Fi)")).pack()
        link = ttk.Entry(win, width=44)
        link.pack(padx=12, pady=4)
        away_label = ttk.Label(win, text=T("Away from home:"))
        away = ttk.Entry(win, width=44)
        https = tk.BooleanVar(value=False)
        note = ttk.Label(win, justify="center", wraplength=340, padding=8)
        count = ttk.Label(win, text=T("{n} files received", n=0))

        def show() -> None:
            self.qr_img = tk.PhotoImage(data=qr_png(server.url))  # keep a reference or tk drops it
            qr.config(image=self.qr_img)
            link.config(state="normal")
            link.delete(0, "end")
            link.insert(0, server.url)
            link.config(state="readonly")
            text = T("Saves to: {folder}", folder=folder)
            if server.tls:
                text += "\n" + T("First time, trust: {url}", url=server.cert_url)
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
                messagebox.showerror(T("Wi-Fi share"), T("Could not restart sharing: {e}", e=e))
                https.set(False)
                server = start(False)
            self._wifi_server = server
            count.config(text=T("{n} files received", n=0))
            show()

        ttk.Checkbutton(win, text=T("HTTPS (save many photos at once)"), variable=https,
                        command=toggle).pack()
        note.pack()
        count.pack()

        def tick() -> None:
            if win.winfo_exists():
                count.config(text=T("{n} files received", n=len(server.received)))
                win.after(1000, tick)

        def close() -> None:
            server.stop()
            win.destroy()

        ttk.Button(win, text=T("Stop sharing"), style="Danger.TButton", command=close).pack(pady=(4, 14))
        win.protocol("WM_DELETE_WINDOW", close)
        self._wifi_server = server
        show()
        tick()

    def _send_to_changed(self) -> None:
        app = SEND_TO[self.send_names[self.send_to.get()]]
        self.remote.set(DEFAULT_UPLOAD_ROOT if app is None else "")
        self.remote_label.config(text=T("folder") if app is None else T("subfolder (optional)"))
        self.send_note.config(text=T("Won't show in Photos.") if app is None else
                              T("Find them in {app}.", app=self.send_to.get().split(" (")[0]))

    def choose_files(self) -> None:
        names = filedialog.askopenfilenames(title=T("Choose files to send to the phone"), filetypes=[
            (T("All files"), "*.*"), (T("Music"), " ".join(f"*{e}" for e in sorted(AUDIO))),
            (T("Documents"), " ".join(f"*{e}" for e in sorted(DOCS))),
            (T("Archives"), " ".join(f"*{e}" for e in sorted(ARCHIVES))),
            (T("Photos and videos"), " ".join(f"*{e}" for e in sorted(KINDS["Photos"] | KINDS["Videos"])))])
        if names:
            self._add_to_send([Path(n) for n in names])

    def send_folder(self) -> None:
        d = filedialog.askdirectory(title=T("Choose a folder to send to the phone"))
        if d:
            self._show_folder(Path(d))

    def _show_folder(self, folder: Path) -> None:
        """List the photos, videos, music and documents inside a folder (nothing ticked yet) so they can be previewed and picked."""
        exts = KINDS["Photos"] | KINDS["Videos"] | AUDIO | DOCS | ARCHIVES
        self.status.config(text=T("Looking in {folder}…", folder=folder))

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
        self.bar.config(value=0)
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
        elif kind == "folder":
            _, files, folder = ev
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
            self.bar.config(value=pct)
            finished = sum(1 for q, d in self.done.items() if d >= self.sizes.get(q, 0))
            name = PurePosixPath(path.replace("\\", "/")).name
            self.status.config(text=T("{verb} {i} of {n}: {name}  ({pct:.0f}%)", verb=T(self.verb), i=min(finished + 1, self.nfiles), n=self.nfiles, name=name, pct=pct))
        elif kind == "finished":
            s = ev[1]
            self.busy = False
            self.bar.config(value=100 if not (s.aborted or self.cancel.is_set()) else self.bar["value"])
            verb, already = ev[2:] if len(ev) > 2 else ("Copied", "already done")
            msg = T("{verb} {n} ({size}), skipped {skipped} {already}, failed {failed}.", verb=T(verb), n=len(s.copied),
                    size=_human(s.bytes_copied), skipped=len(s.skipped), already=T(already), failed=len(s.failed))
            if self.cancel.is_set():
                msg += T(" Cancelled; run it again to continue.")
            if s.aborted:
                self.failed = True
                msg += T(" Stopped early: {why}. Reconnect, unlock the phone, then Connect and copy again to resume.", why=s.aborted)
            self.status.config(text=msg)
            if s.failed:
                messagebox.showwarning(T("Some files failed"), "\n".join(f"{r}: {e}" for r, e in s.failed[:10]))
            self._sync_buttons()
            self.root.after_idle(self._load_visible)
        elif kind == "error":
            e = ev[1]
            self.busy = False
            self.failed = True
            self.status.config(text=T("{err}. Check the cable, unlock the phone, click Reconnect.", err=f"{type(e).__name__}: {e}"))
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


def _apply_scale(scale: float) -> None:
    """Grow the grid's pixel sizes on scaled displays (call before App is built)."""
    global THUMB, PAD, CELL, HEAD_H
    THUMB, PAD, HEAD_H = round(72 * scale), 0, round(30 * scale)
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
