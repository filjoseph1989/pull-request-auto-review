"""What Claude gets to read: the PR checkout, and the ctx/ directory of files written for it."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .config import CONFIG, HOME, TICKET_RE
from .util import run


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


def write_ctx(key: str, files: dict[str, str]) -> Path:
    ctx = HOME / "ctx" / key.replace("/", "-").replace("#", "-")
    shutil.rmtree(ctx, ignore_errors=True)
    ctx.mkdir(parents=True)
    for name, content in files.items():
        (ctx / name).write_text(content)
    return ctx


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
