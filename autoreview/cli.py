"""One cycle: pick candidate PRs, skip the unchanged ones, and run a review or recheck pass on the rest."""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from . import config, github
from .cleanup import prune
from .config import CONFIG, MARKER_RE, PAUSE_FILE
from .passes import recheck_pass, review_pass
from .state import acquire_lock, load_state, release_lock, save_state
from .util import log, notify


def signature(pr: dict, threads: list[dict], requested: bool) -> str:
    last_reply = max((c["createdAt"] for t in threads for c in t["comments"]["nodes"][1:] if (c["author"] or {}).get("login") != config.ME), default="")
    return f"{CONFIG['mode']}|{pr['headRefOid']}|{last_reply}|{int(requested)}"


def process(key: str, state: dict, force: bool) -> bool:
    repo, number = key.split("#")
    pr = github.pr_view(repo, int(number))
    if pr["isDraft"] or pr["author"]["login"] == config.ME:
        return False
    entry = state.setdefault(key, {})
    reviews = github.my_reviews(repo, int(number))
    threads = github.my_open_threads(repo, int(number)) if reviews else []
    requested = any((r.get("login") or "") == config.ME for r in pr.get("reviewRequests", []))
    sig = signature(pr, threads, requested)
    if not force and entry.get("signature") == sig and not entry.get("pending_approval"):
        return False

    pending = entry.pop("pending_approval", None)
    if not reviews:
        log(f"{key}: REVIEW at {pr['headRefOid'][:8]}")
        review_pass(key, pr, entry)
    else:
        last = reviews[-1]
        marker = MARKER_RE.search(last.get("body") or "")
        entry.setdefault("round", int(marker.group(2)) if marker else 1)
        last_sha = last["commit_id"]
        if not threads and last_sha == pr["headRefOid"] and last["state"] == "APPROVED":
            entry["signature"] = sig
            return False
        # No new code, no author reply, no new review request: nothing to look at, so don't run Claude.
        author_replied = any((t["comments"]["nodes"][-1]["author"] or {}).get("login") != config.ME for t in threads)
        if last_sha == pr["headRefOid"] and not author_replied and not requested and not pending:
            entry["signature"] = sig
            return False
        log(f"{key}: RECHECK {last_sha[:8]} → {pr['headRefOid'][:8]}, {len(threads)} open thread(s)")
        recheck_pass(key, pr, entry, last_sha, threads)
    entry["signature"] = sig
    entry["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return True


def main() -> int:
    if sys.argv[1:2] == ["status"]:
        from .report import main as report
        return report()
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", help="OWNER/REPO#N: process just this PR")
    parser.add_argument("--force", action="store_true", help="ignore the saved state for --pr")
    opts = parser.parse_args()

    try:
        config.load()
    except Exception as exc:
        notify("error", f"setup failed: {exc}")
        return 1
    if PAUSE_FILE.exists():
        log("paused (PAUSE file present)")
        return 0
    if not acquire_lock():
        log("another run holds the lock")
        return 0
    try:
        config.ME = github.my_login()
        if CONFIG.get("github_account") and config.ME != CONFIG["github_account"]:
            notify("error", f"gh is logged in as {config.ME}, expected {CONFIG['github_account']}")
            return 1
        state = load_state()
        keys = [opts.pr] if opts.pr else github.candidates()
        try:
            prune(None if opts.pr else keys)
        except Exception as exc:  # housekeeping must never block reviews
            log(f"cleanup failed: {exc}")
        done = 0
        for key in keys:
            if done >= CONFIG["max_prs_per_run"]:
                break
            try:
                if process(key, state, opts.force):
                    done += 1
            except Exception as exc:  # one bad PR must not stop the others
                notify("error", f"{key}: {exc}")
            finally:
                save_state(state)
        log(f"cycle done: {done} PR(s) processed, {len(keys)} candidate(s), mode={CONFIG['mode']}")
        return 0
    finally:
        release_lock()
