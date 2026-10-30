"""Whether a PR may be approved now, later, or not at all."""

from __future__ import annotations

from . import config
from .config import CONFIG
from .github import checks_state


def gates(repo: str, pr: dict, threads_blocked: bool) -> tuple[str, list[str]]:
    """'pass', 'wait' (try again next cycle) or 'block', with the reasons."""
    reasons, waiting = [], False
    if pr["isDraft"]:
        reasons.append("PR is a draft")
    if pr["author"]["login"] == config.ME:
        reasons.append("I'm the author")
    if pr["additions"] + pr["deletions"] > CONFIG["max_changed_lines"]:
        reasons.append(f"over {CONFIG['max_changed_lines']} changed lines")
    if threads_blocked:
        reasons.append("open review threads")
    if pr["mergeable"] == "CONFLICTING":
        reasons.append("merge conflicts")
    elif pr["mergeable"] == "UNKNOWN":
        waiting = True
    checks = checks_state(repo, pr["number"])
    if checks == "fail":
        reasons.append("failing checks")
    elif checks in ("pending", "unknown"):
        waiting = True
    if reasons:
        return "block", reasons
    return ("wait", ["checks or mergeability pending"]) if waiting else ("pass", [])
