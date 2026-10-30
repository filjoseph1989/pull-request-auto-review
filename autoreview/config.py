"""Paths, constants, and the config and identity every other module reads."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
STATE_FILE = HOME / "state.json"
LOCK_DIR = HOME / "lock"
PAUSE_FILE = HOME / "PAUSE"
MARKER_RE = re.compile(r"<!-- auto-review sha=(\w+) round=(\d+) -->")
TICKET_RE = re.compile(r"\bGEEK-\d+\b", re.IGNORECASE)
BLOCKING = {"must-fix", "should-fix"}
SETTLED = {"addressed", "pushback_accepted"}

# Filled by cli.main(). CONFIG is updated in place so `from .config import CONFIG` stays valid;
# ME is rebound, so read it as `config.ME`.
CONFIG: dict = {}
ME = ""


def load() -> None:
    CONFIG.clear()
    CONFIG.update(tomllib.loads((HOME / "config.toml").read_text()))
