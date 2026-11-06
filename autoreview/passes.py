"""The two things the job does to a PR: a first review, and a recheck after the author responds."""

from __future__ import annotations

import json

from . import claude, config, github
from .config import BLOCKING, CONFIG, SETTLED
from .gates import gates
from .payloads import approval, reply_body, review_payload, review_summary, split_findings
from .posting import apply
from .util import log, notify
from .workspace import checkout, drop_checkout, rubric, ticket_text, write_ctx


def checks_note() -> str:
    if CONFIG.get("wait_for_checks", True):
        return ", and all the checks pass."
    return ". Approved without waiting for the checks (tests) to finish."


def review_pass(key: str, pr: dict, entry: dict) -> None:
    repo, number = key.split("#")
    diff = github.pr_diff(repo, number)
    ctx = write_ctx(key, {
        "pr.json": json.dumps({k: pr[k] for k in ("title", "body", "headRefName", "baseRefName", "files")}, indent=2),
        "diff.patch": diff,
        "ticket.txt": ticket_text(pr),
        "rubric.md": rubric(),
    })
    work = checkout(repo, int(number), pr["headRefOid"])
    try:
        prompt = claude.prompt("review.md", repo=repo, number=number, title=pr["title"], author=pr["author"]["login"], head=pr["headRefOid"], ctx=ctx)
        out = claude.ask(prompt, "findings.json", work, ctx)
    finally:
        drop_checkout(repo, work)

    round_no = entry.get("round", 0) + 1
    inline, outside = split_findings(out["findings"], diff)
    summary = review_summary(out)
    draft: dict = {"action": "review", "findings": out["findings"]}
    if out["findings"]:
        draft["review"] = review_payload(pr, round_no, summary, inline, outside)
    else:
        verdict, reasons = gates(repo, pr, threads_blocked=False)
        if verdict == "pass":
            draft["approve"] = approval(pr, round_no, summary + "\n\nNo problems found" + checks_note())
        else:
            draft["review"] = review_payload(pr, round_no, summary, [], [], f"No problems found. Not approving yet because of: {', '.join(reasons)}.")
    apply(key, pr, draft)
    entry["round"] = round_no


def recheck_pass(key: str, pr: dict, entry: dict, last_sha: str, threads: list[dict]) -> None:
    repo, number = key.split("#")
    round_no = entry.get("round", 1) + 1
    if round_no > CONFIG["max_rounds"]:
        # Alert once per PR; later changes on it would otherwise repeat the same alert every cycle.
        if not entry.get("max_rounds_notified"):
            notify("needs you", f"{key} passed {CONFIG['max_rounds']} rounds; not acting")
            entry["max_rounds_notified"] = True
        return

    keyed = {f"T{i + 1}": t for i, t in enumerate(threads)}
    # A thread gets a reply only when the author had the last word in it.
    author_replied = {k: (t["comments"]["nodes"][-1]["author"] or {}).get("login") != config.ME for k, t in keyed.items()}
    rerequested = any((r.get("login") or "") == config.ME for r in pr.get("reviewRequests", []))
    threads_doc = {
        k: {
            "path": t["path"], "line": t["line"] or t["originalLine"], "outdated": t["isOutdated"],
            "resolved_by_author": t["isResolved"],
            "author_replied": author_replied[k],
            "comments": [{"author": (c["author"] or {}).get("login"), "at": c["createdAt"], "body": c["body"]} for c in t["comments"]["nodes"]],
        }
        for k, t in keyed.items()
    }
    diff = github.pr_diff(repo, number)
    compare = github.compare_patch(repo, last_sha, pr["headRefOid"]) if last_sha != pr["headRefOid"] else ""
    ctx = write_ctx(key, {
        "threads.json": json.dumps(threads_doc, indent=2),
        "compare.patch": compare,
        "diff.patch": diff,
        "rubric.md": rubric(),
    })
    work = checkout(repo, int(number), pr["headRefOid"])
    try:
        prompt = claude.prompt("recheck.md", repo=repo, number=number, title=pr["title"], author=pr["author"]["login"],
                               head=pr["headRefOid"], last_sha=last_sha, ctx=ctx)
        out = claude.ask(prompt, "recheck.json", work, ctx)
    finally:
        drop_checkout(repo, work)

    verdicts = {v["thread"]: v for v in out["threads"] if v["thread"] in keyed}
    missing = [k for k in keyed if k not in verdicts]
    draft: dict = {"action": "recheck", "verdicts": out["threads"], "replies": [], "resolve": [], "unresolve": []}
    for k, v in verdicts.items():
        thread = keyed[k]
        if author_replied[k] and v["reply"].strip():
            draft["replies"].append({"comment_id": thread["comments"]["nodes"][0]["databaseId"], "body": reply_body(v["verdict"], v["reply"])})
        if v["verdict"] in SETTLED and not thread["isResolved"]:
            draft["resolve"].append(thread["id"])
        elif v["verdict"] not in SETTLED and thread["isResolved"]:
            draft["unresolve"].append(thread["id"])

    human = [k for k, v in verdicts.items() if v["verdict"] == "needs_human"]
    open_threads = missing + [k for k, v in verdicts.items() if v["verdict"] not in SETTLED]
    blocking_new = [f for f in out["new_findings"] if f["severity"] in BLOCKING]
    verdict, reasons = gates(repo, pr, threads_blocked=bool(open_threads))
    if blocking_new:
        verdict, reasons = "block", reasons + [f"{len(blocking_new)} new finding(s)"]

    if verdict == "pass":
        draft["approve"] = approval(pr, round_no, f"{out['summary']}\n\nEvery comment has been handled" + checks_note())
        if out["new_findings"]:
            draft["review"] = review_payload(pr, round_no, "A few small notes on the latest changes. None of them block this PR:", *split_findings(out["new_findings"], diff))
    elif verdict == "wait" and not out["new_findings"]:
        log(f"{key}: threads settled, waiting on {', '.join(reasons)}")
        entry["pending_approval"] = True
    elif rerequested:
        # The author asked for another look: say where things stand.
        still = f"Still waiting on: {', '.join(reasons)}." if reasons else ""
        draft["review"] = review_payload(pr, round_no, out["summary"], *split_findings(out["new_findings"], diff), extra=still)
    elif out["new_findings"]:
        draft["review"] = review_payload(pr, round_no, "I found some new problems in the latest changes:", *split_findings(out["new_findings"], diff))
    if human:
        notify("needs you", f"{key}: {len(human)} thread(s) need your call")
    apply(key, pr, draft)
    entry["round"] = round_no
