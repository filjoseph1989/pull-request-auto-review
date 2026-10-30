"""Building review and approval bodies from findings. Pure functions, no I/O."""

from __future__ import annotations

import re

from .config import CONFIG


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


def approval(pr: dict, round_no: int, summary: str) -> dict:
    body = f"**{CONFIG['review_prefix']}** (round {round_no})\n\n{summary}\n\n<!-- auto-review sha={pr['headRefOid']} round={round_no} -->"
    return {"commit_id": pr["headRefOid"], "event": "APPROVE", "body": body}
