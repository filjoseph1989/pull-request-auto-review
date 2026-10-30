"""Per-PR state in state.json and the single-run lock."""

from __future__ import annotations

import json
import shutil
import time

from .config import LOCK_DIR, STATE_FILE
from .util import log


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def acquire_lock() -> bool:
    try:
        LOCK_DIR.mkdir()
        return True
    except FileExistsError:
        if time.time() - LOCK_DIR.stat().st_mtime > 1800:
            log("stale lock (>30 min), taking it over")
            LOCK_DIR.touch()
            return True
        return False


def release_lock() -> None:
    shutil.rmtree(LOCK_DIR, ignore_errors=True)
