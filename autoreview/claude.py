"""Running `claude -p` read-only against a checkout and getting schema-shaped JSON back."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import CONFIG, HOME
from .util import log


def prompt(name: str, **fields) -> str:
    return (HOME / "prompts" / name).read_text().format(**fields)


def ask(prompt: str, schema_file: str, work: Path, ctx: Path) -> dict:
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
