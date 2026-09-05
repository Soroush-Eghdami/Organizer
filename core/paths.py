from __future__ import annotations

import sys
from pathlib import Path


def get_app_dir() -> Path:
    """Base dir for config, DB and logs. Works in dev and PyInstaller."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent  # next to exe (writable)
    return Path(__file__).resolve().parent.parent  # organizer/
