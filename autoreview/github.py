"""GitHub reads and writes through the gh CLI."""

from __future__ import annotations

import json
import re
import subprocess

from . import config
from .config import CONFIG
from .util import run


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


def my_login() -> str:
    return run(["gh", "api", "user", "-q", ".login"]).strip()


def candidates() -> list[str]:
    fields = "number,repository,isDraft,author"
    seen: dict[str, dict] = {}
    for flag in ("--review-requested=@me", "--reviewed-by=@me"):
        for pr in gh_json(["search", "prs", flag, "--state=open", "--json", fields, "--limit", "100"]) or []:
            seen[f"{pr['repository']['nameWithOwner']}#{pr['number']}"] = pr
    return [
        key for key, pr in seen.items()
        if key.split("#")[0] in CONFIG["repos"] and not pr["isDraft"] and pr["author"]["login"] != config.ME
    ]


def pr_view(repo: str, number: int) -> dict:
    fields = "number,title,body,author,headRefName,headRefOid,baseRefName,isDraft,mergeable,additions,deletions,files,reviewRequests,url"
    return gh_json(["pr", "view", str(number), "-R", repo, "--json", fields])


def pr_diff(repo: str, number: int | str) -> str:
    return run(["gh", "pr", "diff", str(number), "-R", repo])


def compare_patch(repo: str, base: str, head: str) -> str:
    """The base...head changes, as a diff-like text built from the compare API's per-file patches."""
    cmp = gh_api(f"repos/{repo}/compare/{base}...{head}")
    return "\n".join(f"diff --git a/{f['filename']} b/{f['filename']}\n+++ b/{f['filename']}\n{f.get('patch', '(binary or too large)')}" for f in cmp["files"])


def my_reviews(repo: str, number: int) -> list[dict]:
    reviews = gh_api(f"repos/{repo}/pulls/{number}/reviews", paginate=True) or []
    return [r for r in reviews if r["user"]["login"] == config.ME and r["state"] != "PENDING"]


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
        if not comments or (comments[0]["author"] or {}).get("login") != config.ME:
            continue
        # An author resolving my thread is a claim, not a verdict: check it unless I've had the last word since.
        resolved_by_me = (node["resolvedBy"] or {}).get("login") == config.ME
        last_is_mine = (comments[-1]["author"] or {}).get("login") == config.ME
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


def post_reply(repo: str, number: int | str, comment_id: int, body: str) -> None:
    gh_api(f"repos/{repo}/pulls/{number}/comments/{comment_id}/replies", method="POST", body={"body": body})


def post_review(repo: str, number: int | str, review: dict) -> str:
    return gh_api(f"repos/{repo}/pulls/{number}/reviews", method="POST", body=review)["html_url"]


def resolve_thread(thread_id: str) -> None:
    graphql("mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id } } }", id=thread_id)


def unresolve_thread(thread_id: str) -> None:
    graphql("mutation($id: ID!) { unresolveReviewThread(input: {threadId: $id}) { thread { id } } }", id=thread_id)
