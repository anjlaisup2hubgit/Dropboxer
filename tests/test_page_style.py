"""The Wi-Fi page (phototransfer/wifi.py) and the website (web/index.html) must look the same on a phone."""
import re
from pathlib import Path

from phototransfer.wifi import PAGE

WEB = (Path(__file__).parent.parent / "web" / "index.html").read_text(encoding="utf-8")

# CSS rules both pages must carry word for word (the page template doubles its braces).
SHARED = [
    "html{-webkit-text-size-adjust:100%;text-size-adjust:100%}",
    'body{font-family:"Segoe UI",-apple-system,system-ui,sans-serif;margin:0;background:#eef3f8;color:#1f2933}',
    "header{background:#e1e9f2;border-bottom:1px solid #c9d5e2;padding:12px 16px}",
    "main{margin:0 auto;padding:0 16px 24px;max-width:760px}",
    "h2{font-size:1.05rem;font-weight:700;margin:24px 0 10px}",
    "[hidden]{display:none!important}",
    "input[type=file]{color:#66727f;font-size:1rem;max-width:100%}",
    "button.pri{background:#2b6cb0;border-color:#2b6cb0;color:#fff} button.pri:hover{background:#1f5290;color:#fff}",
    "button:disabled{opacity:.4}",
    ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:0}",
    "min-height:44px;border-radius:0;",  # square buttons, like the desktop window
    ".names .item{display:flex;align-items:center;gap:10px}.names .item+.item{margin-top:-9px}",
]


def test_shared_css_matches():
    page = PAGE.replace("{{", "{").replace("}}", "}")
    for rule in SHARED:
        assert rule in WEB, f"website lost: {rule}"
        assert rule in page, f"Wi-Fi page lost: {rule}"


def test_same_colours():
    page = PAGE.replace("{{", "{").replace("}}", "}")
    colours = lambda s: set(re.findall(r"#[0-9a-f]{6}\b", s))
    assert colours(page) <= colours(WEB), colours(page) - colours(WEB)
