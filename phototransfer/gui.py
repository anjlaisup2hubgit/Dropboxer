"""Desktop window (tkinter, no extra dependencies): browse the camera roll, pick files, copy them."""
from __future__ import annotations

import asyncio
import queue
import threading
import tkinter as tk
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .cli import DEFAULT_ROOT, DEFAULT_UPLOAD_ROOT, _human
from .engine import copy_files, upload_files
from .sources.base import RemoteFile, Source
from .thumbs import THUMB, is_image, load_thumb

POLL_MS = 100
PAD = 6
CELL = THUMB + 2 * PAD  # one grid cell: the thumbnail box plus padding
HEAD_H = 34  # height of a day heading
SCROLL_UNIT = 40  # pixels per scroll unit
WHEEL_UNITS = 3  # units per mouse wheel notch (about 120 px)
TIP_DELAY_MS = 350
KINDS = {
    "All": None,
    "Photos": {".heic", ".jpg", ".jpeg", ".png", ".dng", ".gif", ".tiff"},
    "Videos": {".mov", ".mp4", ".m4v"},
}


class App:
    def __init__(self, root: tk.Tk, source_dir: str | None, udid: str | None):
        self.root, self.source_dir, self.udid = root, source_dir, udid
        self.source: Source | None = None
        self.all_files: list[RemoteFile] = []
        self.shown: list[RemoteFile] = []  # after the Show and date filters, newest first
        self.groups: list[tuple[str, list[RemoteFile]]] = []  # (YYYY-MM-DD, files) in display order
        self.selected: set[str] = set()  # paths
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
        self.visible: list[RemoteFile] = []
        self.thumb_sem: asyncio.Semaphore | None = None  # created on the loop thread

        # One background thread owns the event loop (and so the device connection).
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

        root.title("Phone photo transfer")
        root.geometry("1100x860")
        self._build()
        root.protocol("WM_DELETE_WINDOW", self._quit)
        root.after(POLL_MS, self._poll)

    # ---- layout -------------------------------------------------------------------------------
    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        self.connect_btn = ttk.Button(top, text="Connect and load", command=self.connect)
        self.connect_btn.pack(side="left")
        ttk.Label(top, text="Show:").pack(side="left", padx=(12, 2))
        self.kind = tk.StringVar(value="All")
        kind = ttk.Combobox(top, textvariable=self.kind, values=list(KINDS), width=8, state="readonly")
        kind.pack(side="left")
        kind.bind("<<ComboboxSelected>>", lambda _e: self._fill())
        ttk.Button(top, text="Select all", command=self.select_all).pack(side="left", padx=(12, 2))
        ttk.Button(top, text="Clear", command=self.clear_selection).pack(side="left")
        self.count = ttk.Label(top, text="")
        self.count.pack(side="right")

        flt = ttk.Frame(self.root, padding=(8, 0, 8, 6))
        flt.pack(fill="x")
        ttk.Label(flt, text="Date from:").pack(side="left")
        self.date_from, self.date_to = tk.StringVar(), tk.StringVar()
        for var in (self.date_from, None, self.date_to):
            if var is None:
                ttk.Label(flt, text="to:").pack(side="left", padx=(8, 2))
                continue
            e = ttk.Entry(flt, textvariable=var, width=12)
            e.pack(side="left", padx=(2, 0))
            e.bind("<Return>", lambda _e: self._fill())
        ttk.Label(flt, text="(YYYY-MM-DD, either can be empty)").pack(side="left", padx=8)
        ttk.Button(flt, text="Apply", command=self._fill).pack(side="left")
        ttk.Button(flt, text="Clear dates", command=self.clear_dates).pack(side="left", padx=4)

        mid = ttk.Frame(self.root, padding=(8, 0))
        mid.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(mid, background="#f3f3f3", highlightthickness=0, yscrollincrement=SCROLL_UNIT)
        sb = ttk.Scrollbar(mid, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=lambda a, b: (sb.set(a, b), self._schedule_redraw()))
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-WHEEL_UNITS if e.delta > 0 else WHEEL_UNITS, "units"))
        self.canvas.bind("<Configure>", lambda _e: self.root.after_idle(self._relayout))
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self._hide_tip(reset=True))

        opts = ttk.Frame(self.root, padding=8)
        opts.pack(fill="x")
        ttk.Label(opts, text="Copy to:").pack(side="left")
        self.dest = tk.StringVar(value=str(Path.home() / "Pictures" / "iPhone"))
        ttk.Entry(opts, textvariable=self.dest).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(opts, text="Browse…", command=self.browse).pack(side="left")
        self.by_date = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Organize by date", variable=self.by_date).pack(side="left", padx=8)
        ttk.Label(opts, text="Parallel:").pack(side="left")
        self.jobs = tk.IntVar(value=1)
        ttk.Spinbox(opts, from_=1, to=4, width=3, textvariable=self.jobs).pack(side="left")

        up = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        up.pack(fill="x")
        ttk.Label(up, text="Send to phone folder:").pack(side="left")
        self.remote = tk.StringVar(value=DEFAULT_UPLOAD_ROOT)
        ttk.Entry(up, textvariable=self.remote, width=20).pack(side="left", padx=4)
        self.send_files_btn = ttk.Button(up, text="Send files…", command=self.send_files, state="disabled")
        self.send_files_btn.pack(side="left")
        self.send_folder_btn = ttk.Button(up, text="Send folder…", command=self.send_folder, state="disabled")
        self.send_folder_btn.pack(side="left", padx=4)
        ttk.Label(up, text="(not shown in Photos)").pack(side="left", padx=4)
        ttk.Button(up, text="Wi-Fi share…", command=self.wifi_share).pack(side="right")

        bot = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        bot.pack(fill="x")
        self.copy_btn = ttk.Button(bot, text="Copy selected", command=self.start_copy, state="disabled")
        self.copy_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bot, text="Cancel", command=self.cancel.set, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        self.bar = ttk.Progressbar(bot, maximum=100)
        self.bar.pack(side="left", fill="x", expand=True, padx=8)
        self.status = ttk.Label(self.root, text="Plug in the phone, unlock it, tap Trust, then Connect.", padding=(8, 0, 8, 8))
        self.status.pack(fill="x")

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
                              text=f"{mark}{day}   {len(b)} files, {_human(sum(f.size for f in b))}")
                self.hits.append((0, y, width, y + HEAD_H, "head", b))
                continue
            if y + CELL < top or y > bottom:
                continue
            f, x = b, a * CELL
            x0, y0 = x + PAD, y + PAD
            c.create_rectangle(x0, y0, x0 + THUMB, y0 + THUMB, fill="#dcdcdc", outline="")
            img = self.thumb_by_path.get(f.path)
            if img:
                c.create_image(x0 + THUMB // 2, y0 + THUMB // 2, image=img)
            else:
                c.create_text(x0 + THUMB // 2, y0 + THUMB // 2, fill="#777777", font=("Segoe UI", 12, "bold"),
                              text=Path(f.path).suffix.lstrip(".").upper())
            if f.path in self.selected:
                c.create_rectangle(x0, y0, x0 + THUMB, y0 + THUMB, outline="#0a64d6", width=4)
                c.create_oval(x0 + 6, y0 + 6, x0 + 28, y0 + 28, fill="#0a64d6", outline="white", width=2)
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
                self.selected |= {g.path for g in self.shown[i:j + 1]}
            else:
                self.selected ^= {f.path}
            self.last_clicked = f
        self._update_count()
        self._schedule_redraw()

    def _motion(self, e: tk.Event) -> None:
        h = self._hit(e)
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
        tk.Label(self.tip, text=f"{Path(f.path).name}\n{when}\n{_human(f.size)}", justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, padx=6, pady=3).pack()
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
        if not self.source or self.busy:
            return
        for f in self.visible:
            if f.path not in self.thumb_asked and is_image(f.path):
                self.thumb_asked.add(f.path)
                asyncio.run_coroutine_threadsafe(self._thumb(f), self.loop)

    async def _thumb(self, f: RemoteFile) -> None:
        if self.thumb_sem is None:
            self.thumb_sem = asyncio.Semaphore(2)
        png = None
        async with self.thumb_sem:
            try:
                png = await load_thumb(self.source, f)
            except Exception:
                pass  # the row just keeps its grey placeholder
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

    def _update_count(self) -> None:
        sel = self.selected_files()
        self.count.config(text=f"{len(sel)} selected, {_human(sum(f.size for f in sel))}   |   {len(self.shown)} shown")
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.copy_btn.config(state="normal" if (self.source and not self.busy and self.selected_files()) else "disabled")
        can_send = "normal" if (self.source and not self.busy) else "disabled"
        self.send_files_btn.config(state=can_send)
        self.send_folder_btn.config(state=can_send)
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
        self.status.config(text="Connecting and listing files…")
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
        self.status.config(text=f"Copying {len(files)} files…")
        self._submit(self._copy(files, Path(dest), jobs, self.by_date.get()))

    async def _copy(self, files, dest, jobs, by_date) -> None:
        def progress(path: str, done: int, total: int) -> None:
            self.events.put(("progress", path, done))

        summary = await copy_files(self.source, files, DEFAULT_ROOT, dest, progress, jobs, by_date, self.cancel.is_set)
        self.events.put(("finished", summary))

    def wifi_share(self) -> None:
        """Share the 'Copy to' folder with phones on this Wi-Fi: view photos there, upload from the phone."""
        if getattr(self, "wifi_win", None) and self.wifi_win.winfo_exists():
            self.wifi_win.lift()
            return
        from .wifi import WifiServer, qr_png

        folder = Path(self.dest.get().strip() or ".")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            server = WifiServer(folder, folder)
            url = server.start()
        except OSError as e:
            messagebox.showerror("Wi-Fi share", f"Could not start sharing: {e}")
            return
        win = self.wifi_win = tk.Toplevel(self.root)
        win.title("Wi-Fi share")
        self.qr_img = tk.PhotoImage(data=qr_png(url))  # keep a reference or tk drops it
        ttk.Label(win, image=self.qr_img).pack(padx=12, pady=(12, 4))
        ttk.Label(win, text="On your phone (same Wi-Fi), scan this or open:").pack()
        link = ttk.Entry(win, width=44)
        link.insert(0, url)
        link.config(state="readonly")
        link.pack(padx=12, pady=4)
        note = ttk.Label(win, justify="center", wraplength=340, padding=8,
                         text=f"Your phone can view the photos in {folder} (press and hold a photo, then Save to Photos). "
                              f"Photos you upload from the phone are saved in that same folder. "
                              f"Plain HTTP: use a network you trust. If Windows asks, allow Python on Private networks.")
        note.pack()
        count = ttk.Label(win, text="0 files received")
        count.pack()

        def tick() -> None:
            if win.winfo_exists():
                count.config(text=f"{len(server.received)} files received")
                win.after(1000, tick)

        def close() -> None:
            server.stop()
            win.destroy()

        ttk.Button(win, text="Stop sharing", command=close).pack(pady=(4, 12))
        win.protocol("WM_DELETE_WINDOW", close)
        self._wifi_server = server
        tick()

    def send_files(self) -> None:
        names = filedialog.askopenfilenames(title="Choose files to send to the phone")
        if names:
            paths = [Path(n) for n in names]
            self._start_send(paths, paths[0].parent)

    def send_folder(self) -> None:
        d = filedialog.askdirectory(title="Choose a folder to send to the phone")
        if d:
            root = Path(d)
            self._start_send([p for p in root.rglob("*") if p.is_file()], root)

    def _start_send(self, paths: list[Path], root: Path) -> None:
        remote = "/" + self.remote.get().strip().strip("/")
        if not paths:
            self.status.config(text="Nothing to send: no files found.")
            return
        if remote == "/DCIM" or remote.startswith("/DCIM/"):
            messagebox.showerror("Not allowed", "Sending into /DCIM can confuse the Photos app. Pick another folder.")
            return
        self.cancel.clear()
        self.bar.config(value=0)
        self.total = sum(p.stat().st_size for p in paths)
        self.done = {}
        self.status.config(text=f"Sending {len(paths)} files to {remote}…")
        self._submit(self._send(paths, root, remote))

    async def _send(self, paths, root, remote) -> None:
        def progress(path: str, done: int, total: int) -> None:
            self.events.put(("progress", path, done))

        summary = await upload_files(self.source, paths, root, remote, progress, self.cancel.is_set)
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
            self.all_files = ev[1]
            self.thumb_asked.clear()
            self.busy = False
            self._fill()
            span = ""
            if ev[1]:
                lo, hi = (datetime.fromtimestamp(t(f.mtime for f in ev[1])).strftime("%Y-%m-%d") for t in (min, max))
                span = f" from {lo} to {hi}"
            self.status.config(text=f"{len(ev[1])} files on the phone{span}. Click photos to select (Shift-click for a range), then Copy.")
        elif kind == "thumb":
            _, path, png = ev
            img = tk.PhotoImage(data=png)
            self.thumb_by_path[path] = img
            self._schedule_redraw()
        elif kind == "progress":
            _, path, done = ev
            self.done[path] = done  # bytes done per file; the bar sums them
            self.bar.config(value=100 * sum(self.done.values()) / max(1, self.total))
        elif kind == "finished":
            s = ev[1]
            self.busy = False
            self.bar.config(value=100 if not (s.aborted or self.cancel.is_set()) else self.bar["value"])
            verb, already = ev[2:] if len(ev) > 2 else ("Copied", "already done")
            msg = f"{verb} {len(s.copied)} ({_human(s.bytes_copied)}), skipped {len(s.skipped)} {already}, failed {len(s.failed)}."
            if self.cancel.is_set():
                msg += " Cancelled; run it again to continue."
            if s.aborted:
                msg += f" Stopped early: {s.aborted}. Reconnect, unlock the phone, then Connect and copy again to resume."
            self.status.config(text=msg)
            if s.failed:
                messagebox.showwarning("Some files failed", "\n".join(f"{r}: {e}" for r, e in s.failed[:10]))
            self._sync_buttons()
            self.root.after_idle(self._load_visible)
        elif kind == "error":
            e = ev[1]
            self.busy = False
            self.status.config(text=f"{type(e).__name__}: {e}")
            messagebox.showerror(
                "Could not complete",
                f"{type(e).__name__}: {e}\n\nCheck: cable plugged in, phone unlocked, 'Trust This Computer' accepted.",
            )
            self._sync_buttons()

    def _quit(self) -> None:
        self.cancel.set()
        if getattr(self, "_wifi_server", None):
            self._wifi_server.stop()
        if self.source:
            asyncio.run_coroutine_threadsafe(self.source.close(), self.loop).result(timeout=5)
        self.root.destroy()


def run(source_dir: str | None = None, udid: str | None = None) -> int:
    root = tk.Tk()
    App(root, source_dir, udid)
    root.mainloop()
    return 0
