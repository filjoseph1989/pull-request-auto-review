"""Logging, subprocesses and macOS notifications."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

from .config import HOME


def log(msg: str) -> None:
    line = f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    logs = HOME / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / f"{dt.date.today()}.log", "a") as fh:
        fh.write(line + "\n")


def run(args: list[str], *, cwd: Path | None = None, input: str | None = None, timeout: int = 300, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=cwd, input=input, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])}… exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    return proc.stdout


def notify(title: str, message: str) -> None:
    log(f"NOTIFY {title}: {message}")
    script = f"display notification {json.dumps(message)} with title {json.dumps('Auto-review: ' + title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)
