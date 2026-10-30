#!/usr/bin/env python3
"""Local auto-review: review PRs waiting on me, post inline findings, approve once they're addressed.

Plan: /Users/fil/Fil/value-chain-project/docs/auto-review-plan.md

Claude only ever reads and returns JSON; this script does all the posting, gated by `mode` in config.toml.

    auto-review.py                 one cycle (what launchd runs)
    auto-review.py --pr OWNER/REPO#N [--force]   one PR, ignoring the search and (with --force) the state
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

HOME = Path(__file__).resolve().parent
STATE_FILE = HOME / "state.json"
LOCK_DIR = HOME / "lock"
MARKER_RE = re.compile(r"<!-- auto-review sha=(\w+) round=(\d+) -->")
TICKET_RE = re.compile(r"\bGEEK-\d+\b", re.IGNORECASE)
BLOCKING = {"must-fix", "should-fix"}
SETTLED = {"addressed", "pushback_accepted"}

CONFIG: dict = {}
ME = ""


# ---------------------------------------------------------------- plumbing

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


def gh_json(args: list[str], **kw):
    out = run(["gh", *args], **kw)
    return json.loads(out) if out.strip() else None


def gh_api(path: str, *, method: str = "GET", body: dict | None = None, paginate: bool = False):
    args = ["api", "-X", method, path]
    if paginate:
        args.insert(1, "--paginate")
    if body is not None:
        args += ["--input", "-"]
    out = run(["gh", *args], input=json.dumps(body) if body is not None else None)
    if paginate and out.strip().startswith("["):
        # --paginate prints one JSON array per page back to back.
        return [item for page in re.split(r"(?<=\])\s*(?=\[)", out.strip()) for item in json.loads(page)]
    return json.loads(out) if out.strip() else None


def graphql(query: str, **variables):
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    return gh_json(args)["data"]


def notify(title: str, message: str) -> None:
    log(f"NOTIFY {title}: {message}")
    script = f"display notification {json.dumps(message)} with title {json.dumps('Auto-review: ' + title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


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


# ---------------------------------------------------------------- GitHub reads

def candidates() -> list[str]:
    fields = "number,repository,isDraft,author"
    seen: dict[str, dict] = {}
    for flag in ("--review-requested=@me", "--reviewed-by=@me"):
        for pr in gh_json(["search", "prs", flag, "--state=open", "--json", fields, "--limit", "100"]) or []:
            seen[f"{pr['repository']['nameWithOwner']}#{pr['number']}"] = pr
    return [
        key for key, pr in seen.items()
        if key.split("#")[0] in CONFIG["repos"] and not pr["isDraft"] and pr["author"]["login"] != ME
    ]


def pr_view(repo: str, number: int) -> dict:
    fields = "number,title,body,author,headRefName,headRefOid,baseRefName,isDraft,mergeable,additions,deletions,files,reviewRequests,url"
    return gh_json(["pr", "view", str(number), "-R", repo, "--json", fields])


def my_reviews(repo: str, number: int) -> list[dict]:
    reviews = gh_api(f"repos/{repo}/pulls/{number}/reviews", paginate=True) or []
    return [r for r in reviews if r["user"]["login"] == ME and r["state"] != "PENDING"]


def my_open_threads(repo: str, number: int) -> list[dict]:
    owner, name = repo.split("/")
    query = """
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          reviewThreads(first: 100) {
            nodes {
              id isResolved isOutdated path line originalLine resolvedBy { login }
              comments(first: 50) { nodes { databaseId author { login } body createdAt } }
            }
          }
        }
      }
    }"""
    nodes = graphql(query, owner=owner, name=name, number=number)["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    threads = []
    for node in nodes:
        comments = node["comments"]["nodes"]
        if not comments or (comments[0]["author"] or {}).get("login") != ME:
            continue
        # An author resolving my thread is a claim, not a verdict: check it unless I've had the last word since.
        resolved_by_me = (node["resolvedBy"] or {}).get("login") == ME
        last_is_mine = (comments[-1]["author"] or {}).get("login") == ME
        if node["isResolved"] and (resolved_by_me or last_is_mine):
            continue
        threads.append(node)
    return threads


def checks_state(repo: str, number: int) -> str:
    proc = subprocess.run(["gh", "pr", "checks", str(number), "-R", repo, "--json", "bucket,name"], capture_output=True, text=True)
    if proc.returncode not in (0, 8) or not proc.stdout.strip():
        return "none" if "no checks" in proc.stderr.lower() else "unknown"
    buckets = {c["bucket"] for c in json.loads(proc.stdout)}
    if buckets & {"fail", "cancel"}:
        return "fail"
    if "pending" in buckets:
        return "pending"
    return "pass"


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """RIGHT-side lines GitHub accepts inline comments on: added or context lines inside a hunk."""
    lines: dict[str, set[int]] = {}
    path, new = None, 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            path = None
        elif raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else None
        elif m := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw):
            new = int(m.group(1))
        elif path and raw[:1] in (" ", "+"):
            lines.setdefault(path, set()).add(new)
            new += 1
    return lines


# ---------------------------------------------------------------- context + Claude

def rubric() -> str:
    parts = []
    for file in CONFIG["prompt_files"]:
        p = Path(file)
        if p.exists():
            parts.append(f"<!-- {p.name} -->\n{p.read_text()}")
    return "\n\n".join(parts)


def ticket_text(pr: dict) -> str:
    match = TICKET_RE.search(" ".join([pr["title"], pr["body"] or "", pr["headRefName"]]))
    if not match:
        return "No Jira key found in the PR title, body or branch."
    key = match.group(0).upper()
    proc = subprocess.run(["jira", "issue", "view", key, "--plain"], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return f"{key}: couldn't load the ticket ({proc.stderr.strip()[:200]})."
    return re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)


def checkout(repo: str, number: int, head: str) -> Path:
    clone = HOME / "repos" / repo
    if not clone.exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        run(["gh", "repo", "clone", repo, str(clone), "--", "--filter=blob:none", "--quiet"], timeout=900)
    run(["git", "-C", str(clone), "fetch", "--quiet", "origin", f"+refs/pull/{number}/head:refs/auto-review/pr-{number}"], timeout=900)
    work = HOME / "work" / f"{repo.replace('/', '-')}-{number}"
    if work.exists():
        run(["git", "-C", str(clone), "worktree", "remove", "--force", str(work)], check=False)
        shutil.rmtree(work, ignore_errors=True)
    run(["git", "-C", str(clone), "worktree", "prune"])
    run(["git", "-C", str(clone), "worktree", "add", "--detach", "--quiet", str(work), head], timeout=600)
    return work


def drop_checkout(repo: str, work: Path) -> None:
    clone = HOME / "repos" / repo
    run(["git", "-C", str(clone), "worktree", "remove", "--force", str(work)], check=False)
    shutil.rmtree(work, ignore_errors=True)


def claude(prompt: str, schema_file: str, work: Path, ctx: Path) -> dict:
    schema = (HOME / "schemas" / schema_file).read_text()
    args = [
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", schema,
        "--model", CONFIG["model"],
        "--max-budget-usd", str(CONFIG["max_budget_usd"]),
        "--permission-mode", "dontAsk",
        "--allowedTools", "Read", "Grep", "Glob",
        "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git blame:*)",
        "--add-dir", str(ctx),
        "--setting-sources", "project,local",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    last_error = ""
    for attempt in (1, 2):
        proc = subprocess.run(args, cwd=work, input=prompt, capture_output=True, text=True, timeout=CONFIG["claude_timeout_s"])
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            last_error = f"no JSON (exit {proc.returncode}): {proc.stderr.strip()[:300]}"
            continue
        log(f"claude attempt {attempt}: {result.get('subtype')} cost=${result.get('total_cost_usd', 0):.2f} turns={result.get('num_turns')}")
        if not result.get("is_error") and isinstance(result.get("structured_output"), dict):
            return result["structured_output"]
        last_error = str(result.get("result") or result.get("subtype"))[:300]
    raise RuntimeError(f"Claude returned no usable output: {last_error}")


def write_ctx(key: str, files: dict[str, str]) -> Path:
    ctx = HOME / "ctx" / key.replace("/", "-").replace("#", "-")
    shutil.rmtree(ctx, ignore_errors=True)
    ctx.mkdir(parents=True)
    for name, content in files.items():
        (ctx / name).write_text(content)
    return ctx


# ---------------------------------------------------------------- posting (mode-gated)

def comment_body(finding: dict) -> str:
    tag = finding["severity"] + (", pre-existing" if finding.get("pre_existing") else "")
    return f"**{finding['title']}** _({tag})_\n\n{finding['body']}"


def split_findings(findings: list[dict], diff: str) -> tuple[list[dict], list[dict]]:
    allowed = commentable_lines(diff)
    inline, outside = [], []
    for f in findings:
        (inline if f["line"] in allowed.get(f["path"], set()) else outside).append(f)
    return inline, outside


def review_payload(pr: dict, round_no: int, summary: str, inline: list[dict], outside: list[dict], extra: str = "") -> dict:
    body = [f"**{CONFIG['review_prefix']}** (round {round_no})", "", summary]
    if extra:
        body += ["", extra]
    if outside:
        body += ["", "**Not on a changed line:**"]
        body += [f"- `{f['path']}:{f['line']}`: {comment_body(f)}" for f in outside]
    body += ["", f"<!-- auto-review sha={pr['headRefOid']} round={round_no} -->"]
    return {
        "commit_id": pr["headRefOid"],
        "event": "COMMENT",
        "body": "\n".join(body),
        "comments": [{"path": f["path"], "line": f["line"], "side": "RIGHT", "body": comment_body(f)} for f in inline],
    }


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
        gh_api(f"repos/{repo}/pulls/{number}/comments/{reply['comment_id']}/replies", method="POST", body={"body": reply["body"]})
    for thread_id in draft.get("resolve", []):
        graphql("mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id } } }", id=thread_id)
    for thread_id in draft.get("unresolve", []):
        graphql("mutation($id: ID!) { unresolveReviewThread(input: {threadId: $id}) { thread { id } } }", id=thread_id)
    if draft.get("review"):
        url = gh_api(f"repos/{repo}/pulls/{number}/reviews", method="POST", body=draft["review"])["html_url"]
        log(f"{key}: posted review {url}")
    if draft.get("approve"):
        if mode == "live":
            url = gh_api(f"repos/{repo}/pulls/{number}/reviews", method="POST", body=draft["approve"])["html_url"]
            notify("approved", f"{key} {url}")
        else:
            notify("ready to approve", f"{key} (draft {path.name})")


def approval(pr: dict, round_no: int, summary: str) -> dict:
    body = f"**{CONFIG['review_prefix']}** (round {round_no})\n\n{summary}\n\n<!-- auto-review sha={pr['headRefOid']} round={round_no} -->"
    return {"commit_id": pr["headRefOid"], "event": "APPROVE", "body": body}


def gates(repo: str, pr: dict, threads_blocked: bool) -> tuple[str, list[str]]:
    """'pass', 'wait' (try again next cycle) or 'block', with the reasons."""
    reasons, waiting = [], False
    if pr["isDraft"]:
        reasons.append("PR is a draft")
    if pr["author"]["login"] == ME:
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


# ---------------------------------------------------------------- passes

def review_pass(key: str, pr: dict, entry: dict) -> None:
    repo, number = key.split("#")
    diff = run(["gh", "pr", "diff", number, "-R", repo])
    ctx = write_ctx(key, {
        "pr.json": json.dumps({k: pr[k] for k in ("title", "body", "headRefName", "baseRefName", "files")}, indent=2),
        "diff.patch": diff,
        "ticket.txt": ticket_text(pr),
        "rubric.md": rubric(),
    })
    work = checkout(repo, int(number), pr["headRefOid"])
    try:
        prompt = (HOME / "prompts" / "review.md").read_text().format(
            repo=repo, number=number, title=pr["title"], author=pr["author"]["login"], head=pr["headRefOid"], ctx=ctx)
        out = claude(prompt, "findings.json", work, ctx)
    finally:
        drop_checkout(repo, work)

    round_no = entry.get("round", 0) + 1
    inline, outside = split_findings(out["findings"], diff)
    summary = f"{out['summary']}\n\n**Ticket coverage:** {out['ticket_coverage']}"
    draft: dict = {"action": "review", "findings": out["findings"]}
    if out["findings"]:
        draft["review"] = review_payload(pr, round_no, summary, inline, outside)
    else:
        verdict, reasons = gates(repo, pr, threads_blocked=False)
        if verdict == "pass":
            draft["approve"] = approval(pr, round_no, summary + "\n\nNo findings; checks pass.")
        else:
            draft["review"] = review_payload(pr, round_no, summary, [], [], f"No findings. Not approving yet: {', '.join(reasons)}.")
    apply(key, pr, draft)
    entry["round"] = round_no


def recheck_pass(key: str, pr: dict, entry: dict, last_sha: str, threads: list[dict]) -> None:
    repo, number = key.split("#")
    round_no = entry.get("round", 1) + 1
    if round_no > CONFIG["max_rounds"]:
        notify("needs you", f"{key} passed {CONFIG['max_rounds']} rounds; not acting")
        return

    keyed = {f"T{i + 1}": t for i, t in enumerate(threads)}
    threads_doc = {
        k: {
            "path": t["path"], "line": t["line"] or t["originalLine"], "outdated": t["isOutdated"],
            "resolved_by_author": t["isResolved"],
            "comments": [{"author": (c["author"] or {}).get("login"), "at": c["createdAt"], "body": c["body"]} for c in t["comments"]["nodes"]],
        }
        for k, t in keyed.items()
    }
    diff = run(["gh", "pr", "diff", number, "-R", repo])
    compare = ""
    if last_sha != pr["headRefOid"]:
        cmp = gh_api(f"repos/{repo}/compare/{last_sha}...{pr['headRefOid']}")
        compare = "\n".join(f"diff --git a/{f['filename']} b/{f['filename']}\n+++ b/{f['filename']}\n{f.get('patch', '(binary or too large)')}" for f in cmp["files"])
    ctx = write_ctx(key, {
        "threads.json": json.dumps(threads_doc, indent=2),
        "compare.patch": compare,
        "diff.patch": diff,
        "rubric.md": rubric(),
    })
    work = checkout(repo, int(number), pr["headRefOid"])
    try:
        prompt = (HOME / "prompts" / "recheck.md").read_text().format(
            repo=repo, number=number, title=pr["title"], author=pr["author"]["login"],
            head=pr["headRefOid"], last_sha=last_sha, ctx=ctx)
        out = claude(prompt, "recheck.json", work, ctx)
    finally:
        drop_checkout(repo, work)

    verdicts = {v["thread"]: v for v in out["threads"] if v["thread"] in keyed}
    missing = [k for k in keyed if k not in verdicts]
    draft: dict = {"action": "recheck", "verdicts": out["threads"], "replies": [], "resolve": [], "unresolve": []}
    for k, v in verdicts.items():
        thread = keyed[k]
        draft["replies"].append({"comment_id": thread["comments"]["nodes"][0]["databaseId"], "body": f"{CONFIG['review_prefix']}: **{v['verdict'].replace('_', ' ')}**. {v['reply']}"})
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
        draft["approve"] = approval(pr, round_no, f"{out['summary']}\n\nEvery review thread is addressed and the checks pass.")
        if out["new_findings"]:
            draft["review"] = review_payload(pr, round_no, "Minor notes on the latest changes (not blocking):", *split_findings(out["new_findings"], diff))
    elif verdict == "wait" and not out["new_findings"]:
        log(f"{key}: threads settled, waiting on {', '.join(reasons)}")
        entry["pending_approval"] = True
    else:
        still = f"Still open: {', '.join(reasons)}." if reasons else ""
        draft["review"] = review_payload(pr, round_no, out["summary"], *split_findings(out["new_findings"], diff), extra=still)
    if human:
        notify("needs you", f"{key}: {len(human)} thread(s) need your call")
    apply(key, pr, draft)
    entry["round"] = round_no


# ---------------------------------------------------------------- driver

def signature(pr: dict, threads: list[dict], requested: bool) -> str:
    last_reply = max((c["createdAt"] for t in threads for c in t["comments"]["nodes"][1:] if (c["author"] or {}).get("login") != ME), default="")
    return f"{CONFIG['mode']}|{pr['headRefOid']}|{last_reply}|{int(requested)}"


def process(key: str, state: dict, force: bool) -> bool:
    repo, number = key.split("#")
    pr = pr_view(repo, int(number))
    if pr["isDraft"] or pr["author"]["login"] == ME:
        return False
    entry = state.setdefault(key, {})
    reviews = my_reviews(repo, int(number))
    threads = my_open_threads(repo, int(number)) if reviews else []
    requested = any((r.get("login") or "") == ME for r in pr.get("reviewRequests", []))
    sig = signature(pr, threads, requested)
    if not force and entry.get("signature") == sig and not entry.get("pending_approval"):
        return False

    entry.pop("pending_approval", None)
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
        log(f"{key}: RECHECK {last_sha[:8]} → {pr['headRefOid'][:8]}, {len(threads)} open thread(s)")
        recheck_pass(key, pr, entry, last_sha, threads)
    entry["signature"] = sig
    entry["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return True


def main() -> int:
    global CONFIG, ME
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", help="OWNER/REPO#N: process just this PR")
    parser.add_argument("--force", action="store_true", help="ignore the saved state for --pr")
    opts = parser.parse_args()

    CONFIG = tomllib.loads((HOME / "config.toml").read_text())
    if (HOME / "PAUSE").exists():
        log("paused (PAUSE file present)")
        return 0
    if not acquire_lock():
        log("another run holds the lock")
        return 0
    try:
        ME = run(["gh", "api", "user", "-q", ".login"]).strip()
        state = load_state()
        keys = [opts.pr] if opts.pr else candidates()
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
        shutil.rmtree(LOCK_DIR, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
