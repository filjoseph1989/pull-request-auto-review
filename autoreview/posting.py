"""Saving every draft and posting as much of it as `mode` allows."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import github
from .config import CONFIG, HOME
from .util import log, notify


def save_draft(key: str, head: str, draft: dict) -> Path:
    drafts = HOME / "drafts"
    drafts.mkdir(exist_ok=True)
    path = drafts / f"{key.replace('/', '-').replace('#', '-')}-{head[:8]}-{int(time.time())}.json"
    path.write_text(json.dumps(draft, indent=2))
    return path


def apply(key: str, pr: dict, draft: dict) -> None:
    """Post what `draft` describes, as far as the mode allows; record the rest as a draft."""
    repo, number = key.split("#")
    mode = CONFIG["mode"]
    path = save_draft(key, pr["headRefOid"], {"mode": mode, "pr": pr["url"], **draft})
    if mode == "dry-run":
        log(f"{key}: dry-run, draft at {path}")
        return

    for reply in draft.get("replies", []):
        github.post_reply(repo, number, reply["comment_id"], reply["body"])
    for thread_id in draft.get("resolve", []):
        github.resolve_thread(thread_id)
    for thread_id in draft.get("unresolve", []):
        github.unresolve_thread(thread_id)
    if draft.get("review"):
        url = github.post_review(repo, number, draft["review"])
        log(f"{key}: posted review {url}")
    if draft.get("approve"):
        if mode == "live":
            url = github.post_review(repo, number, draft["approve"])
            notify("approved", f"{key} {url}")
        else:
            notify("ready to approve", f"{key} (draft {path.name})")
