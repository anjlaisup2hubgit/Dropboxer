import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from phototransfer.engine import MANIFEST_NAME, PART_SUFFIX, copy_files
from phototransfer.sources.local import LocalSource


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def device(tmp_path):
    base = tmp_path / "device"
    (base / "DCIM" / "100APPLE").mkdir(parents=True)
    data = {
        "IMG_0001.HEIC": os.urandom(3_000_000),
        "IMG_0002.MOV": os.urandom(5_500_000),
        "IMG_0003.JPG": b"tiny",
    }
    for i, (name, blob) in enumerate(data.items()):
        p = base / "DCIM" / "100APPLE" / name
        p.write_bytes(blob)
        os.utime(p, (1_600_000_000 + i * 1000, 1_600_000_000 + i * 1000))
    return base, data


def test_copy_verifies_and_preserves_mtime(tmp_path, device):
    base, data = device
    src, dest = LocalSource(base), tmp_path / "out"
    files = run(src.list_files("/DCIM"))
    s = run(copy_files(src, files, "/DCIM", dest))
    assert len(s.copied) == 3 and not s.failed
    for name, blob in data.items():
        out = dest / "100APPLE" / name
        assert hashlib.sha256(out.read_bytes()).digest() == hashlib.sha256(blob).digest()
        assert int(out.stat().st_mtime) == int((base / "DCIM" / "100APPLE" / name).stat().st_mtime)


def test_second_run_skips_everything(tmp_path, device):
    base, _ = device
    src, dest = LocalSource(base), tmp_path / "out"
    files = run(src.list_files("/DCIM"))
    run(copy_files(src, files, "/DCIM", dest))
    s = run(copy_files(src, files, "/DCIM", dest))
    assert len(s.skipped) == 3 and not s.copied


def test_skipped_files_count_as_done_in_progress(tmp_path, device):
    base, data = device
    src, dest = LocalSource(base), tmp_path / "out"
    files = run(src.list_files("/DCIM"))
    run(copy_files(src, files, "/DCIM", dest))
    done = {}
    run(copy_files(src, files, "/DCIM", dest, lambda path, d, total: done.__setitem__(path, d)))
    assert sum(done.values()) == sum(len(b) for b in data.values())


def test_missing_source_folder_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        run(LocalSource(tmp_path / "nope").list_files("/DCIM"))


def test_resume_from_partial_file(tmp_path, device):
    base, data = device
    src, dest = LocalSource(base), tmp_path / "out"
    name = "IMG_0002.MOV"
    (dest / "100APPLE").mkdir(parents=True)
    (dest / "100APPLE" / (name + PART_SUFFIX)).write_bytes(data[name][:2_000_000])  # interrupted copy
    files = run(src.list_files("/DCIM"))
    s = run(copy_files(src, files, "/DCIM", dest))
    assert not s.failed
    assert (dest / "100APPLE" / name).read_bytes() == data[name]
    assert not (dest / "100APPLE" / (name + PART_SUFFIX)).exists()


def test_corrupt_oversize_partial_restarts(tmp_path, device):
    base, data = device
    src, dest = LocalSource(base), tmp_path / "out"
    (dest / "100APPLE").mkdir(parents=True)
    (dest / "100APPLE" / ("IMG_0003.JPG" + PART_SUFFIX)).write_bytes(b"x" * 1000)
    files = run(src.list_files("/DCIM"))
    s = run(copy_files(src, files, "/DCIM", dest))
    assert not s.failed
    assert (dest / "100APPLE" / "IMG_0003.JPG").read_bytes() == b"tiny"


def test_source_error_is_reported_not_fatal(tmp_path, device):
    base, data = device

    class Flaky(LocalSource):
        async def read(self, path, offset=0, chunk_size=1 << 20):
            if path.endswith("IMG_0002.MOV"):
                yield data["IMG_0002.MOV"][:1000]
                raise IOError("cable unplugged")
            async for c in super().read(path, offset, chunk_size):
                yield c

    src, dest = Flaky(base), tmp_path / "out"
    files = run(src.list_files("/DCIM"))
    s = run(copy_files(src, files, "/DCIM", dest))
    assert len(s.failed) == 1 and len(s.copied) == 2
    assert (dest / "100APPLE" / ("IMG_0002.MOV" + PART_SUFFIX)).exists()  # kept for resume
    # Re-run with a healthy source resumes and completes.
    s2 = run(copy_files(LocalSource(base), files, "/DCIM", dest))
    assert len(s2.copied) == 1 and not s2.failed
    assert (dest / "100APPLE" / "IMG_0002.MOV").read_bytes() == data["IMG_0002.MOV"]
    assert (dest / MANIFEST_NAME).exists()


def test_partial_not_matching_device_restarts(tmp_path, device):
    base, data = device
    src, dest = LocalSource(base), tmp_path / "out"
    name = "IMG_0002.MOV"
    (dest / "100APPLE").mkdir(parents=True)
    (dest / "100APPLE" / (name + PART_SUFFIX)).write_bytes(os.urandom(2_000_000))  # wrong bytes
    files = run(src.list_files("/DCIM"))
    s = run(copy_files(src, files, "/DCIM", dest))
    assert not s.failed
    assert (dest / "100APPLE" / name).read_bytes() == data[name]


def test_parallel_copy(tmp_path, device):
    base, data = device
    src, dest = LocalSource(base), tmp_path / "out"
    files = run(src.list_files("/DCIM"))
    s = run(copy_files(src, files, "/DCIM", dest, jobs=3))
    assert len(s.copied) == 3 and not s.failed
    for name, blob in data.items():
        assert (dest / "100APPLE" / name).read_bytes() == blob


def test_live_photo_pairing_recorded(tmp_path):
    import json

    base = tmp_path / "device" / "DCIM" / "100APPLE"
    base.mkdir(parents=True)
    (base / "IMG_0010.HEIC").write_bytes(b"still")
    (base / "IMG_0010.MOV").write_bytes(b"clip")
    (base / "IMG_0011.MOV").write_bytes(b"plain video")
    src, dest = LocalSource(tmp_path / "device"), tmp_path / "out"
    files = run(src.list_files("/DCIM"))
    run(copy_files(src, files, "/DCIM", dest))
    m = json.loads((dest / MANIFEST_NAME).read_text())
    assert m["100APPLE/IMG_0010.MOV"]["live_photo_of"] == "100APPLE/IMG_0010.HEIC"
    assert "live_photo_of" not in m["100APPLE/IMG_0011.MOV"]


@pytest.mark.skipif(os.name != "nt", reason="creation time is set on Windows only")
def test_windows_creation_time_set(tmp_path, device):
    base, _ = device
    src, dest = LocalSource(base), tmp_path / "out"
    files = run(src.list_files("/DCIM"))
    run(copy_files(src, files, "/DCIM", dest))
    out = dest / "100APPLE" / "IMG_0001.HEIC"
    assert int(out.stat().st_birthtime) == int(out.stat().st_mtime) == 1_600_000_000


def test_stops_after_repeated_failures_and_cleans_empty_parts(tmp_path, device):
    base, _ = device
    for i in range(10):
        (base / "DCIM" / "100APPLE" / f"X_{i}.JPG").write_bytes(b"data")

    class Dead(LocalSource):
        async def read(self, path, offset=0, chunk_size=1 << 20):
            raise IOError("cable unplugged")
            yield b""

    src, dest = Dead(base), tmp_path / "out"
    files = run(src.list_files("/DCIM"))
    s = run(copy_files(src, files, "/DCIM", dest))
    assert len(s.failed) == 3 and s.aborted
    assert not list(dest.rglob("*" + PART_SUFFIX))


def _mk(path, mtime):
    from phototransfer.sources.base import RemoteFile

    return RemoteFile(path, 4, mtime)


def test_plan_paths_keeps_device_layout_by_default():
    from phototransfer.engine import plan_paths

    files = [_mk("/DCIM/100APPLE/IMG_1.JPG", 1_600_000_000)]
    assert plan_paths(files, "/DCIM") == {"/DCIM/100APPLE/IMG_1.JPG": "100APPLE/IMG_1.JPG"}


def test_plan_paths_by_date_and_duplicate_names():
    from datetime import datetime

    from phototransfer.engine import plan_paths

    t = 1_600_000_000
    d = datetime.fromtimestamp(t)
    files = [
        _mk("/DCIM/100APPLE/IMG_1.JPG", t),
        _mk("/DCIM/101APPLE/IMG_1.JPG", t),  # same name, same month
        _mk("/DCIM/100APPLE/IMG_2.JPG", t),
    ]
    plan = plan_paths(files, "/DCIM", by_date=True)
    prefix = f"{d:%Y}/{d:%Y-%m}"
    assert plan["/DCIM/100APPLE/IMG_2.JPG"] == f"{prefix}/IMG_2.JPG"
    assert plan["/DCIM/100APPLE/IMG_1.JPG"] == f"{prefix}/100APPLE_IMG_1.JPG"
    assert plan["/DCIM/101APPLE/IMG_1.JPG"] == f"{prefix}/101APPLE_IMG_1.JPG"


def test_by_date_manifest_keys_and_live_photo_link(tmp_path):
    import json
    from datetime import datetime

    base = tmp_path / "device" / "DCIM" / "100APPLE"
    base.mkdir(parents=True)
    for name in ("IMG_0010.HEIC", "IMG_0010.MOV"):
        p = base / name
        p.write_bytes(b"data")
        os.utime(p, (1_600_000_000, 1_600_000_000))
    src, dest = LocalSource(tmp_path / "device"), tmp_path / "out"
    files = run(src.list_files("/DCIM"))
    s = run(copy_files(src, files, "/DCIM", dest, by_date=True))
    d = datetime.fromtimestamp(1_600_000_000)
    folder = f"{d:%Y}/{d:%Y-%m}"
    assert not s.failed and (dest / folder / "IMG_0010.HEIC").exists()
    m = json.loads((dest / MANIFEST_NAME).read_text())
    assert m[f"{folder}/IMG_0010.MOV"]["live_photo_of"] == f"{folder}/IMG_0010.HEIC"


def test_cancel_stops_and_rerun_resumes(tmp_path, device):
    base, data = device
    src, dest = LocalSource(base), tmp_path / "out"
    files = run(src.list_files("/DCIM"))
    seen = {"n": 0}

    def cancel():
        seen["n"] += 1
        return seen["n"] > 3  # let the first file start, then ask to stop

    s = run(copy_files(src, files, "/DCIM", dest, jobs=1, cancel=cancel))
    assert len(s.copied) < 3 and not s.failed
    s2 = run(copy_files(src, files, "/DCIM", dest))
    assert not s2.failed and len(s2.copied) + len(s.copied) == 3
    for name, blob in data.items():
        assert (dest / "100APPLE" / name).read_bytes() == blob
    assert not list(dest.rglob("*" + PART_SUFFIX))


def test_upload_verifies_and_skips_on_rerun(tmp_path):
    from phototransfer.engine import upload_files

    pc = tmp_path / "pc"
    (pc / "sub").mkdir(parents=True)
    blobs = {"a.jpg": os.urandom(2_500_000), "sub/b.mov": os.urandom(100)}
    for name, blob in blobs.items():
        (pc / name).write_bytes(blob)
    phone = tmp_path / "phone"
    dst = LocalSource(phone)
    files = [p for p in pc.rglob("*") if p.is_file()]

    s = run(upload_files(dst, files, pc, "/PhotoTransfer"))
    assert len(s.copied) == 2 and not s.failed
    for name, blob in blobs.items():
        assert (phone / "PhotoTransfer" / name).read_bytes() == blob
    assert not list(phone.rglob("*.part"))

    s = run(upload_files(dst, files, pc, "/PhotoTransfer"))
    assert len(s.skipped) == 2 and not s.copied


def test_upload_detects_corruption_and_removes_file(tmp_path):
    from phototransfer.engine import upload_files

    pc = tmp_path / "pc"
    pc.mkdir()
    (pc / "a.jpg").write_bytes(b"A" * 1000)  # fixed bytes: random data could already end in the byte we corrupt it to

    class Corrupting(LocalSource):
        async def write(self, path, chunks):
            await super().write(path, (c[:-1] + b"X" for c in chunks))

    dst = Corrupting(tmp_path / "phone")
    s = run(upload_files(dst, [pc / "a.jpg"], pc, "/PhotoTransfer"))
    assert len(s.failed) == 1 and not s.copied
    assert not (tmp_path / "phone" / "PhotoTransfer" / "a.jpg").exists()


def test_thumbnail_is_small_png_and_cached(tmp_path):
    import io

    from PIL import Image

    from phototransfer.engine import RemoteFile
    from phototransfer.thumbs import THUMB, load_thumb

    base = tmp_path / "phone" / "DCIM"
    base.mkdir(parents=True)
    Image.new("RGB", (400, 300), "red").save(base / "IMG_1.JPG")
    (base / "IMG_2.MOV").write_bytes(b"not an image")
    src, cache = LocalSource(tmp_path / "phone"), tmp_path / "cache"
    f = run(src.stat("/DCIM/IMG_1.JPG"))
    png = run(load_thumb(src, f, cache))
    assert Image.open(io.BytesIO(png)).size == (THUMB, THUMB * 3 // 4)
    assert len(list(cache.glob("*.png"))) == 1
    (base / "IMG_1.JPG").unlink()  # second call must come from the cache
    assert run(load_thumb(src, f, cache)) == png
    assert run(load_thumb(src, RemoteFile("/DCIM/IMG_2.MOV", 12, 0.0), cache)) is None


def _wifi(tmp_path):
    from PIL import Image

    from phototransfer.wifi import WifiServer

    share = tmp_path / "share"
    (share / "trip").mkdir(parents=True)
    Image.new("RGB", (400, 300), "red").save(share / "trip" / "a.jpg")
    (share / "clip.mov").write_bytes(bytes(range(100)))
    (share / "secret.bin").write_text("no")
    (tmp_path / "outside.jpg").write_bytes(b"x")
    srv = WifiServer(share, tmp_path / "inbox", port=0)
    srv.start()
    return srv, f"http://127.0.0.1:{srv.port}/{srv.token}"


def _get(url, headers=None):
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def test_wifi_needs_token_and_stays_in_folder(tmp_path):
    srv, base = _wifi(tmp_path)
    try:
        root = base.rsplit("/", 1)[0]
        assert _get(root + "/wrongtoken/")[0] == 404
        assert _get(root + "/")[0] == 404
        s, body, _ = _get(base + "/")
        assert s == 200 and b"a.jpg" in body and b"secret.bin" not in body
        assert _get(base + "/file/secret.bin")[0] == 404          # not a media type
        assert _get(base + "/file/..%2foutside.jpg")[0] == 404    # outside the folder
        assert _get(base + "/file/../outside.jpg")[0] == 404
        s, png, _ = _get(base + "/thumb/trip/a.jpg")
        assert s == 200 and png[:4] == b"\x89PNG"
    finally:
        srv.stop()


def test_wifi_page_is_an_installable_app(tmp_path):
    import json

    srv, base = _wifi(tmp_path)
    try:
        root = base.rsplit("/", 1)[0]
        s, body, _ = _get(base + "/")
        assert b'rel="manifest"' in body and b"apple-touch-icon" in body
        s, body, h = _get(base + "/manifest.webmanifest")
        m = json.loads(body)
        assert s == 200 and m["start_url"] == m["scope"] == f"/{srv.token}/" and m["display"] == "standalone"
        for icon in m["icons"]:
            s, png, _ = _get(root + icon["src"])
            assert s == 200 and png[:4] == b"\x89PNG"
        assert _get(base + "/icon/180.png")[0] == 200
        assert _get(base + "/icon/999.png")[0] == 404
        assert b"addEventListener" in _get(base + "/sw.js")[1]
        assert _get(root + "/wrong/manifest.webmanifest")[0] == 404   # still needs the token
    finally:
        srv.stop()


def test_wifi_token_persists_between_runs(tmp_path, monkeypatch):
    from phototransfer import wifi

    monkeypatch.setattr(wifi, "TOKEN_FILE", tmp_path / "sub" / "share_token")
    first = wifi.persistent_token()
    assert wifi.persistent_token() == first
    (tmp_path / "sub" / "share_token").write_text("bad")      # damaged file: replaced
    second = wifi.persistent_token()
    assert second != "bad" and wifi.persistent_token() == second
    assert wifi.persistent_token(renew=True) != second


def test_wifi_https_with_self_signed_cert(tmp_path, monkeypatch):
    import ssl
    import urllib.request

    from phototransfer import wifi

    monkeypatch.setattr(wifi, "CERT_DIR", tmp_path / "tls")
    monkeypatch.setattr(wifi, "lan_ip", lambda: "127.0.0.1")
    (tmp_path / "a.jpg").write_bytes(b"x")
    srv = wifi.WifiServer(tmp_path, tmp_path, port=0, tls=True)
    try:
        url = srv.start()
        assert url.startswith("https://")
        cert_path = tmp_path / "tls" / "cert.pem"
        ctx = ssl.create_default_context(cafile=str(cert_path))  # trusts only our cert
        assert b"a.jpg" in urllib.request.urlopen(url, context=ctx).read()
        pem = urllib.request.urlopen(srv.cert_url).read()
        assert pem == cert_path.read_bytes()
        bad = srv.cert_url.rsplit("/", 2)[0] + "/nope/cert"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad)
        # unchanged IP: the same certificate is reused
        assert wifi.ensure_cert("127.0.0.1")[0].read_bytes() == pem
    finally:
        srv.stop()


def test_wifi_range_requests_for_video(tmp_path):
    srv, base = _wifi(tmp_path)
    try:
        s, body, h = _get(base + "/file/clip.mov", {"Range": "bytes=10-19"})
        assert s == 206 and body == bytes(range(10, 20)) and h["Content-Range"] == "bytes 10-19/100"
        s, body, _ = _get(base + "/file/clip.mov", {"Range": "bytes=-5"})
        assert s == 206 and body == bytes(range(95, 100))
    finally:
        srv.stop()


def test_wifi_zip_of_selected_files(tmp_path):
    import io
    import urllib.parse
    import urllib.request
    import zipfile

    srv, base = _wifi(tmp_path)
    try:
        body = urllib.parse.urlencode([("f", "trip/a.jpg"), ("f", "clip.mov"), ("f", "secret.bin"), ("f", "../outside.jpg")]).encode()
        with urllib.request.urlopen(urllib.request.Request(base + "/zip", data=body, method="POST")) as r:
            data = r.read()
        zf = zipfile.ZipFile(io.BytesIO(data))
        assert sorted(zf.namelist()) == ["a.jpg", "clip.mov"]         # non-media and outside files left out
        assert zf.read("clip.mov") == bytes(range(100)) and zf.testzip() is None
    finally:
        srv.stop()


def test_wifi_delete_selected_files(tmp_path, monkeypatch):
    import urllib.error
    import urllib.parse
    import urllib.request

    from phototransfer import wifi

    trashed = []
    monkeypatch.setattr(wifi, "move_to_trash", lambda p: (trashed.append(p.name), p.unlink()))
    srv, base = _wifi(tmp_path)
    try:
        (tmp_path / "secret.bin").write_text("keep")
        body = urllib.parse.urlencode([("f", "clip.mov"), ("f", "secret.bin"), ("f", "../outside.jpg")]).encode()
        with urllib.request.urlopen(urllib.request.Request(base + "/delete", data=body, method="POST")) as r:
            assert r.read() == b"1"
        assert trashed == ["clip.mov"] and not (tmp_path / "share" / "clip.mov").exists()
        assert (tmp_path / "secret.bin").exists() and (tmp_path / "share" / "trip" / "a.jpg").exists()
        with pytest.raises(urllib.error.HTTPError) as e:  # nothing valid selected
            urllib.request.urlopen(urllib.request.Request(base + "/delete", data=b"f=nope.jpg", method="POST"))
        assert e.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as e:  # wrong token
            urllib.request.urlopen(urllib.request.Request(base.rsplit("/", 1)[0] + "/bad/delete", data=body, method="POST"))
        assert e.value.code == 404
    finally:
        srv.stop()


def test_wifi_zip_has_exact_content_length(tmp_path):
    import io
    import urllib.parse
    import urllib.request
    import zipfile

    srv, base = _wifi(tmp_path)
    try:
        (tmp_path / "share" / "ünï.mp4").write_bytes(b"v" * 3_000_000)
        body = urllib.parse.urlencode([("f", "trip/a.jpg"), ("f", "clip.mov"), ("f", "ünï.mp4")]).encode()
        with urllib.request.urlopen(urllib.request.Request(base + "/zip", data=body, method="POST")) as r:
            data, declared = r.read(), int(r.headers["Content-Length"])
        assert declared == len(data)
        zf = zipfile.ZipFile(io.BytesIO(data))
        assert zf.testzip() is None and len(zf.read("ünï.mp4")) == 3_000_000
    finally:
        srv.stop()


def test_wifi_chunked_upload_resumes(tmp_path):
    import urllib.error
    import urllib.request

    srv, base = _wifi(tmp_path)
    try:
        def put(offset, data, uid="ab" * 8, total=10):
            req = urllib.request.Request(f"{base}/put?name=big.mov&id={uid}&total={total}&offset={offset}", data=data, method="PUT")
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        inbox = tmp_path / "inbox"
        assert put(0, b"01234") == (200, b"more")
        assert put(0, b"01234") == (409, b"5")                      # wrong offset: told where the PC is
        assert not (inbox / "big.mov").exists()
        assert put(5, b"56789") == (200, b"ok")
        assert (inbox / "big.mov").read_bytes() == b"0123456789" and not list(inbox.glob(".*.part"))
        assert put(0, b"x", uid="../../x")[0] == 400
    finally:
        srv.stop()


def test_wifi_upload_saves_safely(tmp_path):
    import urllib.error
    import urllib.request

    srv, base = _wifi(tmp_path)
    try:
        def put(name, data, ms="1600000000000"):
            req = urllib.request.Request(f"{base}/put?name={name}", data=data, method="PUT", headers={"X-Last-Modified": ms})
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code

        assert put("IMG_1.HEIC", b"abc") == 200
        assert put("IMG_1.HEIC", b"def") == 200                      # same name: not overwritten
        assert put("..%2f..%2fevil.jpg", b"z") == 200                # path stripped to a plain name
        assert put("run.bin", b"z") == 415                           # not a photo/video/document
        assert put("Notes.PDF", b"%PDF") == 200 and put("sheet.xlsx", b"PK") == 200  # documents are accepted
        inbox = tmp_path / "inbox"
        assert (inbox / "IMG_1.HEIC").read_bytes() == b"abc"
        assert (inbox / "IMG_1 (1).HEIC").read_bytes() == b"def"
        assert (inbox / "Notes.PDF").read_bytes() == b"%PDF"
        assert (inbox / "evil.jpg").exists() and not (tmp_path / "evil.jpg").exists()
        assert int((inbox / "IMG_1.HEIC").stat().st_mtime) == 1_600_000_000
        assert not list(inbox.glob(".*.part"))
        assert put("x.jpg", b"1") == 200 and len(srv.received) == 6
    finally:
        srv.stop()
