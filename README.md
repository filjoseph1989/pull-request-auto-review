# auto-review

A local launchd job that reviews GitHub PRs waiting on me, posts inline findings, re-checks them after the author responds, and approves once everything is addressed.

Claude only reads code and returns JSON. `auto-review.py` does all the posting, gated by `mode` in `config.toml`.

Design notes: `/Users/fil/Fil/value-chain-project/docs/auto-review-plan.md`.

## How it works

Every 10 minutes launchd runs `run.sh`, which loads `env` and runs one cycle of `auto-review.py`:

1. **Find candidates.** Open PRs where my review is requested or that I've already reviewed, limited to `repos` in `config.toml`. Drafts and my own PRs are skipped.
2. **Skip unchanged PRs.** A PR is only processed when its head commit, the latest author reply in my threads, the review-request status or the mode has changed since the last run (tracked in `state.json`).
3. **Review pass** (no review from me yet). Checks out the PR head as a worktree under `work/`, writes the diff, PR metadata, Jira ticket and rubric to `ctx/`, and runs `claude -p` with `prompts/review.md`. Findings come back in the shape of `schemas/findings.json` and are posted as one review with inline comments.
4. **Recheck pass** (I've reviewed before). Runs `prompts/recheck.md` against my open threads and the changes since my last review. Each thread gets a verdict (`addressed`, `pushback_accepted`, `not_addressed`, `needs_human`), a reply, and is resolved or left open. New problems in the changes are posted as new findings.
5. **Approve** when no blocking (`must-fix`/`should-fix`) threads remain and the gates pass: not a draft, under `max_changed_lines`, no merge conflicts, and checks green. Pending checks or mergeability mean it tries again next cycle.

After `max_rounds` rounds the job stops acting on a PR and sends a macOS notification. It also notifies on errors, on `needs_human` threads, and when an approval is ready (in `comment-only` mode) or posted.

Claude runs with read-only tools (`Read`, `Grep`, `Glob`, and `git log/show/diff/blame`). PR content is treated as untrusted input in both prompts.

## Files

| Path | Purpose |
| --- | --- |
| `auto-review.py` | The job |
| `run.sh` | launchd entry point; sets `PATH` and sources `env` |
| `com.fil.auto-review.plist` | launchd agent definition (every 600 s, and at load) |
| `config.toml` | Mode, repos, model, budgets and limits |
| `prompts/` | Review and recheck prompts sent to Claude |
| `schemas/` | JSON schemas Claude's output must match |

Not in git (see `.gitignore`):

| Path | Purpose |
| --- | --- |
| `env` | Secrets, e.g. `export JIRA_API_TOKEN=...` |
| `state.json` | Per-PR signature, round and last run |
| `drafts/` | Every review, reply and approval the job would post, written in all modes |
| `ctx/` | Context files handed to Claude for each PR |
| `repos/`, `work/` | Clones of the watched repos and per-PR worktrees |
| `logs/` | Daily logs and `launchd.log` |

## Setup

Requirements: `python3` 3.11+, `gh` (logged in), `claude`, `jira` CLI, `git`.

```sh
# Secrets for the Jira CLI
echo 'export JIRA_API_TOKEN=...' > ~/.auto-review/env

# Install and start the launchd agent
cp ~/.auto-review/com.fil.auto-review.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fil.auto-review.plist
```

The review rubric is read fresh on every run from `prompt_files` in `config.toml`, so edits to those files take effect immediately.

## Modes

Set `mode` in `config.toml`:

- `dry-run`: nothing is posted; everything goes to `drafts/`.
- `comment-only`: reviews, thread replies and resolves are posted; approvals go to `drafts/` with a notification.
- `live`: approvals are posted too.

## Usage

```sh
~/.auto-review/run.sh                                         # one cycle, as launchd runs it
~/.auto-review/run.sh --pr geekeeasia/value-chain-v2#100      # just this PR
~/.auto-review/run.sh --pr geekeeasia/value-chain-v2#100 --force   # ignore saved state
```

- **Pause:** `touch ~/.auto-review/PAUSE` (remove it to resume).
- **Stop:** `launchctl unload ~/Library/LaunchAgents/com.fil.auto-review.plist`.
- **Logs:** `tail -f ~/.auto-review/logs/$(date +%F).log`.

Only one run at a time: a `lock/` directory guards the cycle and is taken over if it's older than 30 minutes.
