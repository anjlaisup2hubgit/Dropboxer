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
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, parse_qs

from .thumbs import CACHE_DIR, MAX_BYTES, make_thumb

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".gif", ".tiff", ".webp"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
MAX_LISTED = 3000
CHUNK = 1 << 20
WEB_THUMB = 240
VIEW_MAX = 2400  # longest side of the browser-friendly copy of a HEIC
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".heic": "image/heic", ".gif": "image/gif",
        ".tiff": "image/tiff", ".webp": "image/webp", ".mov": "video/quicktime", ".mp4": "video/mp4", ".m4v": "video/x-m4v"}


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


def safe_name(name: str) -> str | None:
    """A plain file name for an upload, or None if it is not a photo/video we accept."""
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


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Photo transfer</title><style>
body{{font-family:system-ui,sans-serif;margin:0;padding:12px;background:#111;color:#eee}}
h2{{font-size:1.05rem;margin:18px 0 8px}} .up{{background:#222;border-radius:10px;padding:12px}}
button,input[type=file]{{font-size:1rem}} button{{padding:8px 14px;border-radius:8px;border:0;background:#2a7fff;color:#fff}}
#log div{{font-size:.85rem;margin:4px 0;color:#aaa}} .ok{{color:#6c6!important}} .bad{{color:#f66!important}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:4px}}
.grid a{{display:block;aspect-ratio:1;background:#222;overflow:hidden;position:relative;color:#eee;text-decoration:none}}
.grid img,.grid video{{width:100%;height:100%;object-fit:cover;pointer-events:none}}
.play{{position:absolute;right:6px;bottom:4px;font-size:.8rem;text-shadow:0 0 3px #000}}
small{{color:#888}} #bar{{margin:8px 0}} #bar button{{margin-right:6px}} button:disabled{{opacity:.4}}
.grid a.on::after{{content:"✓";position:absolute;inset:0;background:rgba(42,127,255,.45);font-size:2rem;display:flex;align-items:center;justify-content:center}}
</style></head><body>
<h2>Send to this PC</h2><div class="up">
Photos: <input id="f" type="file" multiple accept="image/*,video/*"><br><br>
Long videos (if the tick will not press): save them to Files first, then pick here<br>
<input id="f2" type="file" multiple> <br><br><button onclick="up()">Upload</button>
<div id="log"></div></div>
<h2>On this PC ({count} shown)</h2>
<small>Tap a photo to open it. To keep many at once: Select, tick them, Download ZIP, open the ZIP in the Files app,
select all, Share, Save.</small>
<div id="bar"><button id="selbtn" onclick="toggleSel()">Select</button><button id="alln" onclick="selAll()" hidden>All</button><button id="zipbtn" onclick="zip()" hidden>Download ZIP</button></div>
<div class="grid">{items}</div>
<script>
const T={token};
const io=new IntersectionObserver(es=>es.forEach(e=>{{if(e.isIntersecting){{e.target.src=e.target.dataset.src;io.unobserve(e.target);}}}}),{{rootMargin:'200px'}});
document.querySelectorAll('video[data-src]').forEach(v=>io.observe(v));
let sel=false;const chosen=new Set();
const $=id=>document.getElementById(id);
function upd(){{$('selbtn').textContent=sel?'Cancel':'Select';$('alln').hidden=$('zipbtn').hidden=!sel;
  $('zipbtn').textContent='Download '+chosen.size+' as ZIP';$('zipbtn').disabled=!chosen.size;}}
function toggleSel(){{sel=!sel;if(!sel){{chosen.clear();document.querySelectorAll('.grid a.on').forEach(a=>a.classList.remove('on'));}}upd();}}
function selAll(){{document.querySelectorAll('.grid a').forEach(a=>{{chosen.add(a.dataset.rel);a.classList.add('on');}});upd();}}
document.querySelector('.grid').addEventListener('click',e=>{{
  const a=e.target.closest('a');if(!sel||!a)return;e.preventDefault();
  const r=a.dataset.rel;if(chosen.has(r)){{chosen.delete(r);a.classList.remove('on');}}else{{chosen.add(r);a.classList.add('on');}}upd();}});
function zip(){{const f=document.createElement('form');f.method='POST';f.action='/'+T+'/zip';
  chosen.forEach(r=>{{const i=document.createElement('input');i.type='hidden';i.name='f';i.value=r;f.appendChild(i);}});
  document.body.appendChild(f);f.submit();f.remove();}}
async function up(){{
  const log=document.getElementById('log');
  for(const f of [...$('f').files,...$('f2').files]){{
    const d=document.createElement('div');d.textContent=f.name+' …';log.appendChild(d);
    try{{
      const r=await fetch('/'+T+'/put?name='+encodeURIComponent(f.name),{{method:'PUT',body:f,headers:{{'X-Last-Modified':String(f.lastModified)}}}});
      d.textContent=f.name+(r.ok?' ✓ sent':' ✗ '+await r.text());d.className=r.ok?'ok':'bad';
    }}catch(e){{d.textContent=f.name+' ✗ '+e;d.className='bad';}}
  }}
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
            else:  # the #t fragment makes iOS show the first frame as a preview
                inner = f'<video data-src="/{tok}/file/{rel}#t=0.1" preload="metadata" muted playsinline></video><span class="play">▶</span>'
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
        if p.suffix.lower() not in IMAGE_EXTS or p.stat().st_size > MAX_BYTES:
            return self._send(HTTPStatus.NOT_FOUND, b"")
        st = p.stat()
        cache = CACHE_DIR / "web" / (hashlib.sha1(f"{p}|{st.st_size}|{st.st_mtime}".encode()).hexdigest() + ".png")
        try:
            png = cache.read_bytes()
        except OSError:
            png = make_thumb(p.read_bytes(), WEB_THUMB)
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
                '<body style="margin:0;background:#000;color:#aaa;font-family:system-ui,sans-serif;text-align:center">'
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
        if route is None or route[0] != "zip":
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
        self.send_response(HTTPStatus.OK)  # no Content-Length: the connection closing ends the body (HTTP/1.0)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="photos.zip"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                used: set[str] = set()
                for p in files:
                    name = unique_path_name(used, p.name)
                    st = p.stat()
                    info = zipfile.ZipInfo(name, time.localtime(max(st.st_mtime, 315532800))[:6])
                    info.compress_type = zipfile.ZIP_STORED
                    with open(p, "rb") as src, zf.open(info, "w", force_zip64=True) as dst:
                        while block := src.read(CHUNK):
                            dst.write(block)
        except (ConnectionError, OSError):
            pass  # phone cancelled

    # ---- PUT (phone -> PC) ----
    def do_PUT(self) -> None:
        route = self._route()
        if route is None or route[0] != "put":
            return self._send(HTTPStatus.NOT_FOUND, b"not found")
        name = safe_name(parse_qs(urlsplit(self.path).query).get("name", [""])[0])
        if name is None:
            return self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, b"only photos and videos are accepted")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(HTTPStatus.LENGTH_REQUIRED, b"length required")
        inbox: Path = self.server.inbox
        inbox.mkdir(parents=True, exist_ok=True)
        part = inbox / f".{secrets.token_hex(6)}.part"
        got = 0
        try:
            with open(part, "wb") as out:
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
            part.unlink(missing_ok=True)
            return self._send(HTTPStatus.BAD_REQUEST, f"{type(e).__name__}: {e}".encode())
        self.server.received.append(target.name)
        self._send(HTTPStatus.OK, b"ok")


_Handler.do_HEAD = _Handler.do_GET


class WifiServer:
    def __init__(self, share_dir: str | Path, inbox: str | Path, port: int = 8765, token: str | None = None):
        self.share_dir, self.inbox = Path(share_dir).resolve(), Path(inbox).resolve()
        self.token = token or secrets.token_urlsafe(12)
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self.received: list[str] = []

    def start(self) -> str:
        httpd = ThreadingHTTPServer(("0.0.0.0", self.port), _Handler)
        httpd.daemon_threads = True
        httpd.root, httpd.inbox, httpd.token = self.share_dir, self.inbox, self.token
        httpd.lock, httpd.received = threading.Lock(), self.received
        self._httpd = httpd
        self.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return self.url

    @property
    def url(self) -> str:
        return f"http://{lan_ip()}:{self.port}/{self.token}/"

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def qr_png(text: str, scale: int = 5) -> bytes:
    import segno

    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="png", scale=scale, border=2)
    return out.getvalue()
