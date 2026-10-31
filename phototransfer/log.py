"""A plain log file (~/.phototransfer/boxer.log) so a failed send to the phone can be read afterwards."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = Path.home() / ".phototransfer" / "boxer.log"
log = logging.getLogger("boxer")


def setup() -> None:
    if log.handlers:
        return
    log.setLevel(logging.DEBUG)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(LOG_FILE, maxBytes=500_000, backupCount=1, encoding="utf-8")
    except OSError:
        h = logging.NullHandler()  # logging is a convenience; never block the app
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(h)
