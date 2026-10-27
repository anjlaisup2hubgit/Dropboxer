# phototransfer (milestone 1)

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
Safety: the link contains a random token and only photos/videos are served or accepted, but traffic is plain HTTP, so use it on a network you trust. Windows may ask to allow Python through the firewall: choose Private networks. Uploads keep the photo's original date.

Re-running the same `copy` command skips finished files and resumes interrupted ones (`*.part`).
`--by-date` files by modified date as `YYYY/YYYY-MM/name`; a name used twice gets its device folder as a prefix.
`gui` opens a window (tkinter, no extra dependencies): the camera roll as a grid of 200 px previews grouped by day (newest first), a Photos/Videos filter, a From/To date filter, a destination picker, Organize by date, a Parallel setting, a progress bar and Cancel. Cancel keeps the `.part` file, so a re-run resumes.
Click a photo to select it (Shift-click selects a range; click a day heading to select that whole day). Hover a photo to see its name, date and size.
Previews load only for the photos on screen and are cached in `~/.phototransfer/thumbs`, so reopening is fast. HEIC needs `pillow-heif` (installed with the app). Videos and very large originals (>40 MB) show a grey box with the file type instead of a preview.
`--source-dir <folder>` swaps the phone for a local folder, so you can try everything without a device.

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
