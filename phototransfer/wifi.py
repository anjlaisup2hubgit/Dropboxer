"""Wi-Fi transfer: a small private web server. A phone on the same network opens its link in a browser to
view and save the shared photos, and to upload photos/videos to this PC. No app is needed on the phone.

Security: every URL starts with a random token, so only someone with the link (or QR code) gets in.
The traffic is plain HTTP on your local network, so use it on a network you trust, not public Wi-Fi.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import secrets
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, parse_qs

from .thumbs import CACHE_DIR, MAX_BYTES, local_thumb, make_thumb

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".tiff", ".webp"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".csv", ".md", ".odt", ".ods", ".odp",
            ".pages", ".numbers", ".key", ".epub", ".mobi", ".azw", ".azw3", ".fb2", ".djvu", ".cbz", ".cbr"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | DOC_EXTS
MAX_LISTED = 3000
CHUNK = 1 << 20
WEB_THUMB = 240
VIEW_MAX = 2400  # longest side of the browser-friendly copy of a HEIC
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".heic": "image/heic", ".gif": "image/gif",
        ".tiff": "image/tiff", ".webp": "image/webp", ".mov": "video/quicktime", ".mp4": "video/mp4", ".m4v": "video/x-m4v",
        ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8", ".md": "text/markdown; charset=utf-8",
        ".rtf": "application/rtf", ".epub": "application/epub+zip", ".mobi": "application/x-mobipocket-ebook", ".azw3": "application/vnd.amazon.ebook", ".djvu": "image/vnd.djvu", ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}


def move_to_trash(p: Path) -> None:
    """Send a file to the Windows Recycle Bin so a wrong tap can be undone; elsewhere, delete it."""
    if os.name != "nt":
        p.unlink()
        return
    import ctypes
    from ctypes import wintypes

    class FileOp(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("func", wintypes.UINT), ("src", wintypes.LPCWSTR),
                    ("dst", wintypes.LPCWSTR), ("flags", ctypes.c_ushort), ("aborted", wintypes.BOOL),
                    ("mappings", ctypes.c_void_p), ("title", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = FileOp(None, FO_DELETE, str(p) + "\0", None,
                FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) != 0 or op.aborted:
        raise OSError(f"could not move {p.name} to the Recycle Bin")


def lan_ip() -> str:
    """This PC's address on the local network (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def tailscale_ip() -> str | None:
    """This PC's Tailscale address, if Tailscale is installed and connected. Phones on the same tailnet can
    use it from anywhere (mobile data, other Wi-Fi) without opening any port on the router."""
    import shutil
    import subprocess

    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    return out[0] if out and re.fullmatch(r"100\.\d+\.\d+\.\d+", out[0]) else None


def safe_name(name: str) -> str | None:
    """A plain file name for an upload, or None if it is not a photo, video or document we accept."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name.replace("\\", "/")).name).strip(" .")
    if not name or Path(name).suffix.lower() not in MEDIA_EXTS:
        return None
    return name


def unique_path(folder: Path, name: str) -> Path:
    p = folder / name
    n = 1
    while p.exists():
        p = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    return p


def unique_path_name(used: set[str], name: str) -> str:
    """`name`, or `name (1)` etc. if already used in this ZIP."""
    cand, n = name, 1
    while cand.lower() in used:
        cand = f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    used.add(cand.lower())
    return cand


_LOCAL_FIXED, _DESC, _CENTRAL_FIXED = 30 + 20, 24, 46 + 28  # header bytes beside the name; always ZIP64


def zip_length(entries: list[tuple[bytes, int]]) -> int:
    """Exact size of the stored ZIP that `zip_stream` produces for (name, size) entries."""
    body = sum(_LOCAL_FIXED + len(n) + s + _DESC + _CENTRAL_FIXED + len(n) for n, s in entries)
    return body + 56 + 20 + 22


def zip_stream(entries):
    """A stored (uncompressed) ZIP64, streamed in pieces. entries: (path, utf-8 name, size, mtime).
    Every entry uses ZIP64 fields, so the total length is a plain sum (`zip_length`) and can be sent as Content-Length."""
    import struct
    import zlib

    pos, central = 0, []
    for path, name, size, mtime in entries:
        t = time.localtime(max(mtime, 315532800))
        dt, dd = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2), ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
        head = struct.pack("<IHHHHHIIIHH", 0x04034B50, 45, 0x0808, 0, dt, dd, 0, 0xFFFFFFFF, 0xFFFFFFFF, len(name), 20)
        head += name + struct.pack("<HHQQ", 1, 16, 0, 0)
        yield head
        crc, left = 0, size
        with open(path, "rb") as f:
            while left:
                block = f.read(min(CHUNK, left))
                if not block:
                    raise IOError(f"{path.name} shrank while sending")
                crc = zlib.crc32(block, crc)
                left -= len(block)
                yield block
        yield struct.pack("<IIQQ", 0x08074B50, crc, size, size)
        central.append((name, dt, dd, crc, size, pos))
        pos += len(head) + size + _DESC
    cd_start = pos
    for name, dt, dd, crc, size, off in central:
        rec = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 45, 45, 0x0808, 0, dt, dd, crc, 0xFFFFFFFF, 0xFFFFFFFF,
                          len(name), 28, 0, 0, 0, 0, 0xFFFFFFFF)
        rec += name + struct.pack("<HHQQQ", 1, 24, size, size, off)
        yield rec
        pos += len(rec)
    cd_size = pos - cd_start
    n = len(central)
    yield struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, n, n, cd_size, cd_start)
    yield struct.pack("<IIQI", 0x07064B50, 0, pos, 1)
    yield struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)


def to_jpeg(data: bytes, max_side: int = VIEW_MAX) -> bytes | None:
    try:
        from PIL import Image, ImageOps

        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception:
        return None


def app_icon(size: int) -> bytes:
    """The home-screen icon: a blue square with a white up/down arrow pair."""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (size, size), "#5ea500")
    d, u = ImageDraw.Draw(im), size / 100
    for x, up in ((35, True), (65, False)):  # one arrow up, one down
        tip, base = (18, 82) if up else (82, 18)
        head = tip + (22 if up else -22)
        d.line([(x * u, base * u), (x * u, head * u)], fill="white", width=max(2, int(7 * u)))
        d.polygon([(x * u, tip * u), ((x - 13) * u, head * u), ((x + 13) * u, head * u)], fill="white")
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


# Pass-through worker: lets the browser treat the page as an installable app. Nothing is cached, so the
# gallery and the token never sit in a cache. (iOS only runs service workers on HTTPS pages.)
SERVICE_WORKER = "self.addEventListener('install',()=>self.skipWaiting());\n" \
                 "self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));\n" \
                 "self.addEventListener('fetch',e=>e.respondWith(fetch(e.request)));\n"


def manifest(token: str) -> bytes:
    base = f"/{quote(token)}/"
    return json.dumps({
        "name": "Dropboxer", "short_name": "Dropboxer", "start_url": base, "scope": base, "display": "standalone",
        "background_color": "#ffffff", "theme_color": "#ffffff",
        "icons": [{"src": f"{base}icon/192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": f"{base}icon/512.png", "sizes": "512x512", "type": "image/png"}],
    }).encode()


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="manifest" href="manifest.webmanifest"><link rel="apple-touch-icon" href="icon/180.png"><meta name="theme-color" content="#ffffff">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Dropboxer">
<title>Dropboxer</title><style>
body{{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#fff;color:#1f2a1f}}
h2{{font-size:1.05rem;font-weight:600;margin:20px 0 8px}}
.up{{background:#fff;border:1px solid #dfe3d9;border-radius:12px;padding:14px;margin-bottom:12px}}
button,input[type=file]{{font-size:1rem}}
button{{padding:8px 14px;border-radius:8px;border:1px solid #dfe3d9;background:#fff;color:#1f2a1f;cursor:pointer}}
button:hover{{border-color:#5ea500;color:#4d8a00}}
button.pri{{background:#5ea500;border-color:#5ea500;color:#fff}} button.pri:hover{{background:#4d8a00;color:#fff}}
button.danger{{color:#c0392b;border-color:#e8b9b3}} button.danger:hover{{border-color:#c0392b;color:#c0392b}}
button:disabled{{opacity:.4}}
small,.muted{{color:#6b7568}} .ok{{color:#4d8a00}} .bad{{color:#c0392b}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:6px}}
.grid a{{display:block;aspect-ratio:1;background:#f3f6ee;border:1px solid #dfe3d9;border-radius:8px;overflow:hidden;position:relative;color:#1f2a1f;text-decoration:none}}
.grid img,.grid video{{width:100%;height:100%;object-fit:cover}}
#log div{{font-size:.85rem;margin:4px 0;color:#6b7568}} #log div.ok{{color:#4d8a00}} #log div.bad{{color:#c0392b}}
.grid img,.grid video{{pointer-events:none}}
.doc{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:6px;text-align:center;font-size:.7rem;word-break:break-all;overflow:hidden;color:#6b7568}}
.doc b{{font-size:1.1rem;color:#4d8a00;margin-bottom:4px}}
.play{{position:absolute;right:6px;bottom:4px;font-size:.8rem;color:#fff;text-shadow:0 0 3px #000}}
#bar{{margin:8px 0}} #bar button{{margin-right:6px}}
.grid a.on{{border-color:#5ea500}}
.grid a.on::after{{content:"✓";position:absolute;inset:0;background:rgba(94,165,0,.4);color:#fff;font-size:2rem;display:flex;align-items:center;justify-content:center}}
</style></head><body>
<h2>Send to this PC</h2><div class="up">
Photos: <input id="f" type="file" multiple accept="image/*,video/*"><br><br>
Documents (PDF, Word, Excel, PowerPoint, text…) and long videos (if the tick will not press: save them to Files first): pick here<br>
<input id="f2" type="file" multiple> <br><br><button id="upbtn" class="pri" onclick="up()">Upload</button> <button id="stopbtn" onclick="stop()" hidden class="danger">Stop</button>
<div id="total"></div><div id="log"></div></div>
<h2>On this PC ({count} shown)</h2>
<small>Tap a photo to open it. To keep many at once: Select, tick them, Download ZIP, open the ZIP in the Files app,
select all, Share, Save.</small>
<div id="bar"><button id="selbtn" onclick="toggleSel()">Select</button><button id="alln" onclick="selAll()" hidden>All</button><button id="zipbtn" class="pri" onclick="zip()" hidden>Download ZIP</button><button id="delbtn" onclick="del()" hidden class="danger">Delete</button><button id="savebtn" class="pri" onclick="save()" hidden>Save to Photos</button></div><div id="savelog"></div>
<div class="grid">{items}</div>
<script>
const T={token},PIECE=8*1024*1024;
if('serviceWorker' in navigator)navigator.serviceWorker.register('sw.js').catch(()=>{{}});
let sel=false;const chosen=new Set();
const $=id=>document.getElementById(id);
function upd(){{$('selbtn').textContent=sel?'Cancel':'Select';$('alln').hidden=$('zipbtn').hidden=$('delbtn').hidden=!sel;
  $('delbtn').textContent='Delete '+chosen.size;$('delbtn').disabled=!chosen.size;
  $('zipbtn').textContent='Download '+chosen.size+' as ZIP';$('zipbtn').disabled=!chosen.size;
  $('savebtn').hidden=!sel||!window.isSecureContext||!navigator.canShare;$('savebtn').disabled=!chosen.size;}}
async function save(){{const L=$('savelog');
  try{{L.textContent='Loading '+chosen.size+' file(s)…';
    const files=[];for(const r of chosen){{const res=await fetch('/'+T+'/file/'+r.split('/').map(encodeURIComponent).join('/'));
      files.push(new File([await res.blob()],r.split('/').pop(),{{type:res.headers.get('Content-Type')||''}}));}}
    L.textContent='';await navigator.share({{files}});
  }}catch(e){{L.textContent=e.name==='AbortError'?'':'Could not open the share sheet: '+e;}}}}
function toggleSel(){{sel=!sel;if(!sel){{chosen.clear();document.querySelectorAll('.grid a.on').forEach(a=>a.classList.remove('on'));}}upd();}}
function selAll(){{document.querySelectorAll('.grid a').forEach(a=>{{chosen.add(a.dataset.rel);a.classList.add('on');}});upd();}}
document.querySelector('.grid').addEventListener('click',e=>{{
  const a=e.target.closest('a');if(!sel||!a)return;e.preventDefault();
  const r=a.dataset.rel;if(chosen.has(r)){{chosen.delete(r);a.classList.remove('on');}}else{{chosen.add(r);a.classList.add('on');}}upd();}});
async function del(){{const n=chosen.size;
  if(!n||!confirm('Delete '+n+' file(s) from the PC? They go to the PC\'s Recycle Bin.'))return;
  const body=new URLSearchParams();chosen.forEach(r=>body.append('f',r));
  try{{const res=await fetch('/'+T+'/delete',{{method:'POST',body}});
    if(!res.ok)throw new Error(await res.text());location.reload();
  }}catch(e){{$('savelog').textContent='Could not delete: '+e.message;}}}}
function zip(){{const f=document.createElement('form');f.method='POST';f.action='/'+T+'/zip';
  chosen.forEach(r=>{{const i=document.createElement('input');i.type='hidden';i.name='f';i.value=r;f.appendChild(i);}});
  document.body.appendChild(f);f.submit();f.remove();}}
let cancel=false,ctl=null;
const sleep=ms=>new Promise(s=>setTimeout(s,ms));
function store(k,v){{try{{v==null?localStorage.removeItem(k):localStorage.setItem(k,v);}}catch(e){{}}}}
function load(k){{try{{return localStorage.getItem(k);}}catch(e){{return null;}}}}
function stop(){{cancel=true;if(ctl)ctl.abort();}}
async function up(){{
  const log=$('log'),files=[...$('f').files,...$('f2').files];
  if(!files.length)return;
  cancel=false;$('upbtn').disabled=true;$('stopbtn').hidden=false;
  const all=files.reduce((n,f)=>n+f.size,0);let done=0;
  const tot=$('total');
  for(const f of files){{
    const d=document.createElement('div');d.textContent=f.name+' …';log.appendChild(d);
    const key='up|'+f.name+'|'+f.size+'|'+f.lastModified;  // same file picked again after a reload resumes
    let id=load(key);
    if(!id){{id=[...crypto.getRandomValues(new Uint8Array(12))].map(b=>b.toString(16).padStart(2,'0')).join('');store(key,id);}}
    const base='/'+T+'/put?name='+encodeURIComponent(f.name)+'&id='+id+'&total='+f.size+'&offset=';
    let off=0,fails=0,r=null,sent=0;
    try{{
      while(off<f.size&&!cancel){{
        const end=Math.min(off+PIECE,f.size);
        ctl=new AbortController();
        try{{
          r=await fetch(base+off,{{method:'PUT',body:f.slice(off,end),signal:ctl.signal,headers:{{'X-Last-Modified':String(f.lastModified)}}}});
        }}catch(e){{if(cancel)break;if(++fails>20)throw e;d.textContent=f.name+' … retrying';await sleep(2000);continue;}}
        if(r.status==409){{off=parseInt(await r.text());sent=off;continue;}}  // the PC says where it really is
        if(!r.ok)break;
        fails=0;off=end;sent=off;
        d.textContent=f.name+' … '+Math.floor(100*off/f.size)+'%';
        tot.textContent='Total: '+Math.floor(100*(done+sent)/Math.max(1,all))+'%';
      }}
      if(cancel){{d.textContent=f.name+' stopped (pick it again later to continue)';d.className='bad';break;}}
      const ok=r&&r.ok;if(ok)store(key,null);
      d.textContent=f.name+(ok?' ✓ sent':' ✗ '+(r?await r.text():'no answer'));d.className=ok?'ok':'bad';
    }}catch(e){{d.textContent=f.name+' ✗ '+e;d.className='bad';}}
    done+=f.size;tot.textContent='Total: '+Math.floor(100*done/Math.max(1,all))+'%';
  }}
  $('upbtn').disabled=false;$('stopbtn').hidden=true;
}}
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "phototransfer"

    def log_message(self, *args) -> None:  # quiet
        pass

    # ---- helpers ----
    def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _route(self) -> tuple[str, str] | None:
        """(action, rest) if the URL starts with the right token, else None."""
        parts = [unquote(p) for p in urlsplit(self.path).path.split("/")]
        if len(parts) < 2 or not secrets.compare_digest(parts[1].encode(), self.server.token.encode()):
            return None
        return (parts[2] if len(parts) > 2 else ""), "/".join(parts[3:])

    def _media(self, rel: str) -> Path | None:
        root = self.server.root
        try:
            p = (root / rel).resolve()
            p.relative_to(root)
        except (ValueError, OSError):
            return None
        return p if p.is_file() and p.suffix.lower() in MEDIA_EXTS else None

    # ---- GET ----
    def do_GET(self) -> None:
        route = self._route()
        if route is None:
            return self._send(HTTPStatus.NOT_FOUND, b"not found")
        action, rest = route
        if action == "":
            return self._index()
        if action == "manifest.webmanifest":
            return self._send(HTTPStatus.OK, manifest(self.server.token), "application/manifest+json")
        if action == "sw.js":
            return self._send(HTTPStatus.OK, SERVICE_WORKER.encode(), "text/javascript; charset=utf-8")
        if action == "icon":
            m = re.fullmatch(r"(180|192|512)\.png", rest)
            if m is None:
                return self._send(HTTPStatus.NOT_FOUND, b"not found")
            return self._send(HTTPStatus.OK, app_icon(int(m[1])), "image/png", {"Cache-Control": "max-age=86400"})
        p = self._media(rest)
        if p is None:
            return self._send(HTTPStatus.NOT_FOUND, b"not found")
        if action == "file":
            return self._file(p)
        if action == "thumb":
            return self._thumb(p)
        if action == "view":
            return self._view_page(rest)
        if action == "img":
            return self._view(p)
        self._send(HTTPStatus.NOT_FOUND, b"not found")

    def _index(self) -> None:
        root = self.server.root
        files = sorted((p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXTS),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_LISTED]
        tok, items = quote(self.server.token), []
        for p in files:
            rel = quote(p.relative_to(root).as_posix())
            if p.suffix.lower() in IMAGE_EXTS:
                inner = f'<img loading="lazy" src="/{tok}/thumb/{rel}" alt="">'
                href = f"/{tok}/view/{rel}"
            elif p.suffix.lower() in DOC_EXTS:  # no preview: a tile with the type and name; tapping downloads/opens it
                inner = f'<span class="doc"><b>{html.escape(p.suffix[1:].upper())}</b>{html.escape(p.name)}</span>'
                href = f"/{tok}/file/{rel}"
            else:  # a still from the video; the tile falls back to its grey box if none can be made
                inner = f'<img loading="lazy" src="/{tok}/thumb/{rel}" alt="" onerror="this.remove()"><span class="play">▶</span>'
                href = f"/{tok}/file/{rel}"
            items.append(f'<a href="{href}" data-rel="{html.escape(p.relative_to(root).as_posix(), quote=True)}">{inner}</a>')
        page = PAGE.format(count=len(files), items="".join(items), token=json.dumps(self.server.token))
        self._send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")

    def _file(self, p: Path) -> None:
        """Send a file, honouring Range so videos can play and seek on iOS."""
        size, start, end, status = p.stat().st_size, 0, p.stat().st_size - 1, HTTPStatus.OK
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m[1] or m[2]):
            if m[1]:
                start = int(m[1])
                end = int(m[2]) if m[2] else end
            else:
                start = max(0, size - int(m[2]))
            end = min(end, size - 1)
            if start > end:
                return self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", extra={"Content-Range": f"bytes */{size}"})
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", MIME.get(p.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(p, "rb") as f:
                f.seek(start)
                while length > 0:
                    block = f.read(min(CHUNK, length))
                    if not block:
                        break
                    self.wfile.write(block)
                    length -= len(block)
        except (ConnectionError, OSError):
            pass  # phone stopped listening (scrolled away)

    def _thumb(self, p: Path) -> None:
        if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size > MAX_BYTES:
            return self._send(HTTPStatus.NOT_FOUND, b"")
        st = p.stat()
        cache = CACHE_DIR / "web" / (hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime}".encode()).hexdigest() + ".png")
        try:
            png = cache.read_bytes()
        except OSError:
            png = make_thumb(p.read_bytes(), WEB_THUMB) if p.suffix.lower() in IMAGE_EXTS else local_thumb(p, WEB_THUMB)
            if png is None:
                return self._send(HTTPStatus.NOT_FOUND, b"")
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(png)
            except OSError:
                pass
        self._send(HTTPStatus.OK, png, "image/png", {"Cache-Control": "max-age=3600"})

    def _view_page(self, rel: str) -> None:
        """A page holding the photo as a plain image, so press and hold offers Save to Photos."""
        src = f"/{quote(self.server.token)}/img/{quote(rel)}"
        page = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<body style="margin:0;background:#fff;color:#6b7568;font-family:system-ui,sans-serif;text-align:center">'
                f'<p style="margin:10px">Press and hold the photo, then choose Save to Photos.</p><img src="{src}" style="max-width:100%">')
        self._send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")

    def _view(self, p: Path) -> None:
        """Photos open as-is, except HEIC, which many browsers cannot show: those get a JPEG copy."""
        if p.suffix.lower() == ".heic" and p.stat().st_size <= MAX_BYTES:
            jpg = to_jpeg(p.read_bytes())
            if jpg:
                return self._send(HTTPStatus.OK, jpg, "image/jpeg")
        self._file(p)

    # ---- POST: selected files as one ZIP (stored, not compressed; streamed, never held in memory) ----
    def do_POST(self) -> None:
        route = self._route()
        if route is None or route[0] not in ("zip", "delete"):
            return self._send(HTTPStatus.NOT_FOUND, b"not found")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(HTTPStatus.LENGTH_REQUIRED, b"length required")
        if n > 4 << 20:
            return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"too many files")
        rels = parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("f", [])[:MAX_LISTED]
        files = [p for p in (self._media(r) for r in rels) if p]
        if not files:
            return self._send(HTTPStatus.BAD_REQUEST, b"nothing selected")
        if route[0] == "delete":
            return self._delete(files)
        used: set[str] = set()
        entries = []
        for p in files:
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append((p, unique_path_name(used, p.name).encode("utf-8"), st.st_size, st.st_mtime))
        # The exact length is known up front, so iOS can show progress and does not treat the download as broken.
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(zip_length([(n, s) for _, n, s, _ in entries])))
        self.send_header("Content-Disposition", 'attachment; filename="photos.zip"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for block in zip_stream(entries):
                self.wfile.write(block)
        except (ConnectionError, OSError):
            pass  # phone cancelled

    def _delete(self, files: list[Path]) -> None:
        """Move the selected photos/videos (media inside the shared folder only) to the Recycle Bin."""
        failed = 0
        for p in files:
            try:
                move_to_trash(p)
            except OSError:
                failed += 1
        if failed:
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR, f"{failed} file(s) could not be deleted".encode())
        self._send(HTTPStatus.OK, str(len(files)).encode())

    # ---- PUT (phone -> PC) ----
    def do_PUT(self) -> None:
        route = self._route()
        if route is None or route[0] != "put":
            return self._send(HTTPStatus.NOT_FOUND, b"not found")
        name = safe_name(parse_qs(urlsplit(self.path).query).get("name", [""])[0])
        if name is None:
            return self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, b"only photos, videos and documents are accepted")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(HTTPStatus.LENGTH_REQUIRED, b"length required")
        inbox: Path = self.server.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        q = parse_qs(urlsplit(self.path).query)
        chunked = "id" in q  # big files arrive in pieces (id, offset, total) so a dropped connection loses one piece
        if chunked:
            try:
                uid, offset, total = q["id"][0], int(q["offset"][0]), int(q["total"][0])
            except (KeyError, ValueError):
                return self._send(HTTPStatus.BAD_REQUEST, b"bad chunk")
            if not re.fullmatch(r"[0-9a-f]{8,32}", uid) or offset < 0 or total < 1 or offset + length > total:
                return self._send(HTTPStatus.BAD_REQUEST, b"bad chunk")
            for old in inbox.glob(".up-*.part"):  # abandoned uploads older than a week
                try:
                    if time.time() - old.stat().st_mtime > 7 * 86400:
                        old.unlink()
                except OSError:
                    pass
            part = inbox / f".up-{uid}.part"
            have = part.stat().st_size if part.exists() else 0
            if have != offset:  # client resumes from what the PC really has
                return self._send(HTTPStatus.CONFLICT, str(have).encode())
        else:
            part = inbox / f".{secrets.token_hex(6)}.part"
        got = 0
        try:
            with open(part, "ab" if chunked else "wb") as out:
                while got < length:
                    block = self.rfile.read(min(CHUNK, length - got))
                    if not block:
                        break
                    out.write(block)
                    got += len(block)
                out.flush()
                os.fsync(out.fileno())
            if got != length:
                raise IOError(f"connection ended after {got} of {length} bytes")
            if chunked and offset + length < total:
                return self._send(HTTPStatus.OK, b"more")
            with self.server.lock:  # two uploads of the same name must not pick the same target
                target = unique_path(inbox, name)
                os.replace(part, target)
            try:
                ms = float(self.headers.get("X-Last-Modified", ""))
                if 0 < ms < 4e12:
                    os.utime(target, (ms / 1000, ms / 1000))
            except ValueError:
                pass
        except Exception as e:
            if chunked and offset + length < total:
                with open(part, "r+b") as out:  # drop the half-received piece, keep what came before
                    out.truncate(offset)
            else:
                part.unlink(missing_ok=True)
            return self._send(HTTPStatus.BAD_REQUEST, f"{type(e).__name__}: {e}".encode())
        self.server.received.append(target.name)
        self._send(HTTPStatus.OK, b"ok")


_Handler.do_HEAD = _Handler.do_GET


CERT_DIR = CACHE_DIR.parent / "tls"
CERT_DAYS = 365  # iOS rejects server certificates valid for more than 398 days


def ensure_cert(ip: str, cert_dir: Path | None = None) -> tuple[Path, Path]:
    """A self-signed certificate for this PC's LAN address; reused until the IP changes or it nears expiry."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    cert_dir = cert_dir or CERT_DIR
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = cert_dir / "cert.pem", cert_dir / "key.pem"
    addr = ipaddress.ip_address(ip)
    now = datetime.datetime.now(datetime.timezone.utc)
    if cert_path.exists() and key_path.exists():
        try:
            old = x509.load_pem_x509_certificate(cert_path.read_bytes())
            sans = old.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if addr in sans.get_values_for_type(x509.IPAddress) and old.not_valid_after_utc > now + datetime.timedelta(days=30):
                return cert_path, key_path
        except Exception:
            pass
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "phototransfer")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(addr)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


class _CertHandler(BaseHTTPRequestHandler):
    """Plain-HTTP side door that only hands out the public certificate, so the phone can install it once."""

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        parts = urlsplit(self.path).path.strip("/").split("/")
        if len(parts) == 2 and parts[1] == "cert" and secrets.compare_digest(parts[0].encode(), self.server.token.encode()):
            body, status, ctype = self.server.cert_pem, 200, "application/x-x509-ca-cert"
        else:
            body, status, ctype = b"not found", 404, "text/plain"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


TOKEN_FILE = Path.home() / ".phototransfer" / "share_token"


def persistent_token(renew: bool = False) -> str:
    """The link token kept between runs, so a home-screen icon or bookmark keeps working. `renew` makes a new
    one (use it if the link leaked); the old link then stops working."""
    try:
        tok = "" if renew else TOKEN_FILE.read_text().strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{16}", tok):
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(12)
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(tok)
    except OSError:
        pass  # still works for this run
    return tok


class WifiServer:
    def __init__(self, share_dir: str | Path, inbox: str | Path, port: int = 8765, token: str | None = None, tls: bool = False):
        self.share_dir, self.inbox = Path(share_dir).resolve(), Path(inbox).resolve()
        self.token = token or secrets.token_urlsafe(12)
        self.port = port
        self.tls = tls
        self._httpd: ThreadingHTTPServer | None = None
        self._cert_httpd: ThreadingHTTPServer | None = None
        self.cert_port = 0
        self.received: list[str] = []

    def start(self) -> str:
        httpd = ThreadingHTTPServer(("0.0.0.0", self.port), _Handler)
        httpd.daemon_threads = True
        httpd.root, httpd.inbox, httpd.token = self.share_dir, self.inbox, self.token
        httpd.lock, httpd.received = threading.Lock(), self.received
        if self.tls:
            import ssl

            cert_path, key_path = ensure_cert(lan_ip())
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert_path, key_path)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            side = ThreadingHTTPServer(("0.0.0.0", 0), _CertHandler)
            side.daemon_threads = True
            side.token, side.cert_pem = self.token, cert_path.read_bytes()
            self._cert_httpd, self.cert_port = side, side.server_address[1]
            threading.Thread(target=side.serve_forever, daemon=True).start()
        self._httpd = httpd
        self.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return self.url

    @property
    def url(self) -> str:
        return f"{'https' if self.tls else 'http'}://{lan_ip()}:{self.port}/{self.token}/"

    @property
    def remote_url(self) -> str | None:
        """The same link over Tailscale, for use away from home (plain HTTP; Tailscale itself encrypts it)."""
        ip = tailscale_ip()
        return f"http://{ip}:{self.port}/{self.token}/" if ip and not self.tls else None

    @property
    def cert_url(self) -> str | None:
        """Where the phone downloads the certificate to trust (HTTPS mode only)."""
        return f"http://{lan_ip()}:{self.cert_port}/{self.token}/cert" if self.tls else None

    def stop(self) -> None:
        for attr in ("_httpd", "_cert_httpd"):
            s = getattr(self, attr)
            if s:
                s.shutdown()
                s.server_close()
                setattr(self, attr, None)


def qr_png(text: str, scale: int = 5) -> bytes:
    import segno

    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="png", scale=scale, border=2)
    return out.getvalue()
