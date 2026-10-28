# Dropboxer (phototransfer, milestone 1)
New to the app? Read [HOWTO.md](HOWTO.md) for the step-by-step guide.

Copy photos and videos from an iPhone to Windows over USB. Resumable, SHA-256 verified, keeps original timestamps (modified and, on Windows, creation).
Uses the open-source [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) to talk to Apple's AFC file service.

## Requirements
- Windows with **Apple Mobile Device Support** (installed with iTunes or the Apple Devices app). Verified present on this PC.
- Python 3.10+
- An iPhone, USB cable, phone unlocked, and "Trust This Computer" accepted.

## Setup
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Use
```powershell
.\.venv\Scripts\python.exe -m phototransfer devices                 # is the phone detected?
.\.venv\Scripts\python.exe -m phototransfer list                    # files in /DCIM
.\.venv\Scripts\python.exe -m phototransfer copy D:\iPhonePhotos    # copy everything
.\.venv\Scripts\python.exe -m phototransfer copy D:\iPhonePhotos --limit 5   # try 5 first
.\.venv\Scripts\python.exe -m phototransfer copy D:\iPhonePhotos --by-date   # YYYY\YYYY-MM folders
.\.venv\Scripts\python.exe -m phototransfer gui                  # window: browse by month, select, copy
```
```powershell
.\.venv\Scripts\python.exe -m phototransfer send D:\ToPhone               # PC -> phone, into /PhotoTransfer
.\.venv\Scripts\python.exe -m phototransfer send D:\ToPhone --to /Books   # pick another folder on the phone
```
`send` uploads a file or folder, then reads each file back from the phone and checks SHA-256 (a bad one is deleted). Files already there with the same size are skipped. It keeps the folder layout but cannot set the phone-side timestamp. In the GUI, after Connect, use **Send files…** / **Send folder…** (the folder box sets the destination on the phone; `/DCIM` is refused).
**Limit:** files land in the phone's media storage over USB. They do **not** appear in the Photos app (Apple only allows that through its own sync), so use this to carry files, not to add to your camera roll.

**Wi-Fi (no cable, no app on the phone):**
```powershell
.\.venv\Scripts\python.exe -m phototransfer share D:\iPhonePhotos     # prints a link and a QR code
```
Open the link on a phone on the same Wi-Fi. It shows the photos in that folder (tap one to open it; press and hold, then **Save to Photos** puts it in the Photos app). The page also has an **Upload** button that sends photos/videos from the phone (including its Photos library) to `<folder>` itself (or `--inbox`). The GUI has the same thing under **Wi-Fi share…** (shares the "Copy to" folder).
**Delete:** in Select mode a red **Delete N** button asks to confirm, then moves the ticked files (media inside the shared folder only) to the PC's Recycle Bin.
Safety: the link contains a random token and only photos, videos and common documents (PDF, Word, Excel, PowerPoint, text, CSV, e-books such as EPUB, MOBI, AZW3, FB2, DJVU, CBZ), archives (ZIP, RAR, 7z, TAR, GZ, BZ2, XZ, ZST, ISO, DMG, EXE, MSI, APK, JAR) and audio (MP3, M4A, WAV, FLAC, OGG, WMA, AIFF, CAF, AMR, MIDI) are served or accepted, but traffic is plain HTTP, so use it on a network you trust. Windows may ask to allow Python through the firewall: choose Private networks. Uploads keep the photo's original date.

**HTTPS (lets the page save many photos straight to Photos):** add `--https`. The first time (and whenever the PC's IP changes, which makes a new certificate), trust the certificate on the iPhone once:
1. Run `share ... --https`; it prints a second link ending in `/cert`. Open that link in Safari on the phone and tap Allow to download the profile.
2. Settings > General > VPN & Device Management > the downloaded profile > Install.
3. Settings > General > About > Certificate Trust Settings > switch on full trust for "phototransfer".
Then open the main `https://` link. In **Select** mode a **Save to Photos** button appears: tick photos and it opens the iOS share sheet with **Save Images / Save Videos**. The files are loaded into the phone's memory first, so do a few hundred photos at a time, not a whole library. Remove the profile in the same Settings screen when you no longer need it.

**Add to Home Screen:** the page is installable (manifest, icon, standalone mode). In Safari tap Share > Add to Home Screen; it opens full-screen like an app. The link's token is saved in `~/.phototransfer/share_token` and port 8765 is the default, so the same link works after restarting `share` (CLI and GUI share it). `share --new-token` makes a new secret link if the old one leaked; icons and bookmarks must then be re-added. If the port is busy, pass the same `--port` as before. iOS runs the service worker only over `--https`; without it you still get the icon and full-screen mode. Transfers pause when the phone locks or you leave the app.

**Away from home (Tailscale):** nothing is opened on your router and nothing goes through a public server. Install [Tailscale](https://tailscale.com/download) on this PC and on the iPhone, sign in to the same account on both, and keep the PC on with `share` (or the GUI Wi-Fi window) running. `share` then prints a second link starting `http://100.…`; open that one on the phone on mobile data or any Wi-Fi. Tailscale encrypts the traffic, so plain HTTP is fine there. Limits: the page's "Save to Photos" button needs HTTPS and so does not show on this link (use Select + ZIP); if Windows blocks the connection, allow Python in the firewall for the Tailscale network (it may count as Public).

Re-running the same `copy` command skips finished files and resumes interrupted ones (`*.part`).
`--by-date` files by modified date as `YYYY/YYYY-MM/name`; a name used twice gets its device folder as a prefix.
`gui` opens a window (tkinter, no extra dependencies): the camera roll as a grid of 200 px previews grouped by day (newest first), a Photos/Videos filter, a From/To date filter, a destination picker, Organize by date, a Parallel setting, a progress bar and Cancel. Cancel keeps the `.part` file, so a re-run resumes.
Click a photo to select it (Shift-click selects a range; click a day heading to select that whole day). Hover a photo to see its name, date and size.
Previews load only for the photos on screen and are cached in `~/.phototransfer/thumbs`, so reopening is fast. HEIC needs `pillow-heif` (installed with the app). Videos and very large originals (>40 MB) show a grey box with the file type instead of a preview.
`--source-dir <folder>` swaps the phone for a local folder, so you can try everything without a device.

## Tray app (no window, no terminal)
`python -m phototransfer tray [folder]` (also what `PhoneTransfer.exe` does when started with no arguments) keeps the Wi-Fi share running from a tray icon: left-click shows the QR code, right-click has Copy link, the Tailscale link, Open photos folder, Open full window (USB copy), **Start with Windows**, and Quit. It shares the GUI's "Copy to" folder. A balloon says when files arrive.
Build the single file: `pip install -e .[dev]`, then
`pyinstaller --noconfirm --onefile --noconsole --name PhoneTransfer --collect-all pymobiledevice3 --collect-all pillow_heif --collect-all av --hidden-import pystray._win32 run_tray.py` → `dist\PhoneTransfer.exe` (~90 MB, first start takes ~15 s while it unpacks). Add one firewall rule for that .exe and it keeps working wherever you move it.

## Website version (`web/`)
A static page plus one tiny function, for Vercel (project root directory = `web`). The PC opens the site in Chrome or Edge, picks a folder, and shows a QR code; the phone scans it and sends photos **directly** to that folder over WebRTC. Nothing is stored on the server: `api/signal.js` only passes two short handshake messages, encrypted with a key that stays in the link's `#fragment`, and forgets them after 5 minutes.
Setup: create the Vercel project from `web/`, add an **Upstash Redis** store from the Vercel Marketplace (it sets `UPSTASH_REDIS_REST_URL` / `_TOKEN`), deploy. Limits: phone → PC only; the PC must keep the tab open; Chrome/Edge only on the PC (folder access); file dates are not kept; no TURN relay, so strict networks (some mobile carriers, office Wi-Fi) may not connect, and then the Wi-Fi/Tailscale server above is the fallback.

## How it works
- `phototransfer/sources/` — transports behind one small `Source` interface (`list_files`, `read`). `iphone.py` is USB/AFC, `local.py` is for tests. A Wi-Fi or Android source plugs in here later.
- `phototransfer/engine.py` — resume, hashing, verification, timestamps, manifest (`.phototransfer.json` in the destination). It never touches the transport.
- `phototransfer/cli.py` — `devices`, `list`, `copy`.

Verification: bytes are hashed while streaming, then the finished `.part` file is re-read from disk and must match
in size and SHA-256 before it is renamed into place. The modified time is set from the phone's value.

## Known limits
- Camera roll only (`/DCIM`). No albums, edits metadata, or iCloud-only originals that are not on the phone ("Optimize iPhone Storage").
- Resume spot-checks the first and last 1 MB of an existing `.part` against the phone; the middle is trusted. A mismatch restarts the file.
- Live Photos stay as separate `.HEIC` + `.MOV` files; the `.MOV` gets `live_photo_of` in the manifest.
- `--jobs N` copies files in parallel (default 1). Tried on a real iPhone with `--jobs 4`: 934 files (16.7 GB) copied with 0 failures.
- Copying stops after 3 consecutive failures (usually a disconnected or locked phone). Reconnect, unlock, and re-run; finished files are skipped and partial files resume.
- No HEIC to JPEG conversion yet.

## Test plan
Automated (no phone needed): `.\.venv\Scripts\python.exe -m pytest` — copy + hash + mtime, skip on re-run, resume from partial, bad partial restarts, source failure mid-file then recovery.

Manual with a real iPhone (not yet run, no phone was connected when this was built):
1. `devices` shows the phone. If not: replug, unlock, tap Trust, check `Get-Service "Apple Mobile Device Service"` is Running.
2. `list` shows `/DCIM/1xxAPPLE/...` files with plausible dates and sizes.
3. `copy <dir> --limit 5`, then open the files; compare dates in Explorer with the Photos app.
4. Unplug the cable during a large video copy. Re-run: it should resume, not restart, and finish with no `FAILED`.
5. Run `copy` again: everything reported as skipped.
6. Spot check: `Get-FileHash` on a copied file vs the `sha256` in `.phototransfer.json`.
7. Large run: 1000+ files and a video over 2 GB; check memory stays flat and no failures.
8. Locked phone / untrusted computer: error message is clear, no traceback.
