"""Window text in English or Vietnamese. English text is the key: a missing translation shows the English."""
from __future__ import annotations

LANGUAGES = {"en": "English", "vi": "Tiếng Việt"}
_lang = "en"

VI = {
    "PC to iPhone": "Máy tính → iPhone",
    "iPhone to PC": "iPhone → máy tính",
    "Copy": "Sao chép",
    "Copy {n}": "Sao chép {n}",
    "Send {n}": "Gửi {n}",
    "Add files…": "Thêm tệp…",
    "Folder…": "Thư mục…",
    "Stop": "Dừng",
    "Save in": "Lưu vào",
    "Change": "Đổi",
    "Send to": "Gửi đến",
    "folder": "thư mục",
    "subfolder (optional)": "thư mục con (tùy chọn)",
    "Folder on iPhone": "Thư mục trên iPhone",
    "VLC (music player)": "VLC (nghe nhạc)",
    "Won't show in Photos.": "Sẽ không hiện trong Ảnh.",
    "Find them in {app}.": "Tìm trong {app}.",
    "All": "Tất cả",
    "Photos": "Ảnh",
    "Videos": "Video",
    "Music": "Nhạc",
    "Documents": "Tài liệu",
    "Archives": "Tệp nén",
    "Select all": "Chọn tất cả",
    "Deselect all": "Bỏ chọn tất cả",
    "Reload": "Tải lại",
    "Reconnect": "Kết nối lại",
    "Wi-Fi": "Wi-Fi",
    "Reset": "Đặt lại",
    "From": "Từ",
    "to": "đến",
    "Any date": "Mọi ngày",
    "Clear": "Xóa",
    "Today": "Hôm nay",
    "Shift-click selects a range  ·  click a day to select all of it":
        "Shift + nhấp để chọn một dãy  ·  nhấp vào ngày để chọn cả ngày  ·  kéo chuột để quét chọn",
    "USB-C cable": "Cáp USB-C",
    "Wi-Fi share": "Chia sẻ Wi-Fi",
    "Unlock your iPhone and tap Trust.": "Mở khóa iPhone và nhấn Tin cậy.",
    "Looking for files…": "Đang tìm tệp…",
    "Dates must look like 2026-10-26.": "Ngày phải có dạng 2026-10-26.",
    "No files match these filters.": "Không có tệp phù hợp.",
    "Nothing here yet.": "Chưa có gì.",
    "Loading…": "Đang tải…",
    "No preview for this file.": "Không có bản xem trước.",
    "{n} selected": "Đã chọn {n}",
    "Connecting…": "Đang kết nối…",
    "Copying {n} files…": "Đang sao chép {n} tệp…",
    "Sending {n} files to {remote}…": "Đang gửi {n} tệp đến {remote}…",
    "Copying": "Đang sao chép",
    "Sending": "Đang gửi",
    "Copied": "Đã sao chép",
    "Sent": "Đã gửi",
    "already done": "đã có",
    "already there": "đã có",
    "{verb} {i} of {n}: {name}  ({pct:.0f}%)": "{verb} {i}/{n}: {name}  ({pct:.0f}%)",
    "{verb} {n} ({size}), skipped {skipped} {already}, failed {failed}.":
        "{verb} {n} ({size}), bỏ qua {skipped} ({already}), lỗi {failed}.",
    " Cancelled; run it again to continue.": " Đã dừng; chạy lại để tiếp tục.",
    " Stopped early: {why}. Reconnect, unlock the phone, then Connect and copy again to resume.":
        " Dừng giữa chừng: {why}. Kết nối lại, mở khóa điện thoại rồi sao chép lại để tiếp tục.",
    "Some files failed": "Một số tệp bị lỗi",
    "Nothing to send.": "Không có gì để gửi.",
    "{n} found": "Tìm thấy {n}",
    "{n} found from {lo} to {hi}": "Tìm thấy {n} từ {lo} đến {hi}",
    "Nothing found in {folder}.": "Không tìm thấy gì trong {folder}.",
    "Looking in {folder}…": "Đang tìm trong {folder}…",
    "{err}. Check the cable, unlock the phone, click Reconnect.":
        "{err}. Kiểm tra cáp, mở khóa điện thoại, nhấn Kết nối lại.",
    "Could not complete": "Không hoàn thành được",
    "{err}\n\nCheck: cable plugged in, phone unlocked, 'Trust This Computer' accepted.":
        "{err}\n\nKiểm tra: đã cắm cáp, đã mở khóa điện thoại, đã chọn 'Tin cậy máy tính này'.",
    "Different drives": "Ổ đĩa khác nhau",
    "Send files from one drive at a time: untick the others.": "Chỉ gửi tệp từ một ổ đĩa mỗi lần: bỏ chọn các tệp còn lại.",
    "Not allowed": "Không được phép",
    "Sending into /DCIM can confuse the Photos app. Pick another folder.":
        "Gửi vào /DCIM có thể làm ứng dụng Ảnh bị lỗi. Hãy chọn thư mục khác.",
    "Choose files to send to the phone": "Chọn tệp để gửi đến điện thoại",
    "Choose a folder to send to the phone": "Chọn thư mục để gửi đến điện thoại",
    "All files": "Mọi tệp",
    "Photos and videos": "Ảnh và video",
    "Wi-Fi share window": "Chia sẻ Wi-Fi",
    "Could not start sharing: {e}": "Không bắt đầu chia sẻ được: {e}",
    "Could not restart sharing: {e}": "Không khởi động lại chia sẻ được: {e}",
    "Scan with your phone (same Wi-Fi)": "Quét bằng điện thoại (cùng Wi-Fi)",
    "Away from home:": "Khi ở xa nhà:",
    "Saves to: {folder}": "Lưu vào: {folder}",
    "First time, trust: {url}": "Lần đầu, hãy tin cậy: {url}",
    "HTTPS (save many photos at once)": "HTTPS (lưu nhiều ảnh cùng lúc)",
    "{n} files received": "Đã nhận {n} tệp",
    "Stop sharing": "Dừng chia sẻ",
    "Language": "Ngôn ngữ",
    "Mo": "T2", "Tu": "T3", "We": "T4", "Th": "T5", "Fr": "T6", "Sa": "T7", "Su": "CN",
    "Month {m} {y}": "Tháng {m} {y}",
}


def set_language(code: str) -> None:
    global _lang
    _lang = code if code in LANGUAGES else "en"


def language() -> str:
    return _lang


def T(text: str, **kw) -> str:
    out = VI.get(text, text) if _lang == "vi" else text
    return out.format(**kw) if kw else out
