# auto-review

A local launchd job that reviews GitHub PRs waiting on me, posts inline findings, re-checks them after the author responds, and approves once everything is addressed.

Claude only reads code and returns JSON. `auto-review.py` does all the posting, gated by `mode` in `config.toml`.

## How it works

Every 10 minutes launchd runs `run.sh`, which loads `env` and runs one cycle of `auto-review.py`:

1. **Find candidates.** Open PRs where my review is requested or that I've already reviewed, limited to `repos` in `config.toml`. Drafts and my own PRs are skipped.
2. **Skip unchanged PRs.** A PR is only processed when its head commit, the latest author reply in my threads, the review-request status or the mode has changed since the last run (tracked in `state.json`).
3. **Review pass** (no review from me yet). Checks out the PR head as a worktree under `work/`, writes the diff, PR metadata, Jira ticket and rubric to `ctx/`, and runs `claude -p` with `prompts/review.md`. Findings come back in the shape of `schemas/findings.json` and are posted as one review with inline comments.
4. **Recheck pass** (I've reviewed before). Skipped without running Claude when there's no new commit, no author reply and no new review request. Otherwise runs `prompts/recheck.md` against my open threads and the changes since my last review. Claude reads the author's replies first (some changes are intentional), then gives each thread a verdict (`addressed`, `pushback_accepted`, `not_addressed`, `needs_human`), and the thread is resolved or left open. It stays quiet unless something calls for a response:
   - A thread gets a reply only when the author replied last and the reply needs an answer (a question, a disagreement, an intentional choice, or a claim that doesn't hold). "Done" or "thanks" gets no reply.
   - The "Nth round of review" summary is posted only when the author has requested my review again.
   - New problems in the changes are always posted as new findings.
5. **Approve** when no blocking (`must-fix`/`should-fix`) threads remain and the gates pass: not a draft, under `max_changed_lines`, and no merge conflicts. With `wait_for_checks = true` the CI checks must be green too; with `false` (the current setting) it approves as soon as every concern is addressed, without waiting for tests. Pending mergeability (or pending checks, when waiting for them) means it tries again next cycle.

After `max_rounds` rounds the job stops acting on a PR and sends one macOS notification (only once per PR, however often the PR changes afterwards). It also notifies on errors, on `needs_human` threads, and when an approval is ready (in `comment-only` mode) or posted.

Claude runs with read-only tools (`Read`, `Grep`, `Glob`, and `git log/show/diff/blame`). PR content is treated as untrusted input in both prompts.

## How comments read

Everything posted is written in everyday English: short sentences, common words, and any technical term explained the first time. The prompts ask for this and `autoreview/payloads.py` formats it.

- **Review body:** `**First round of review (with AI help):**` (then Second, Third, …), a plain summary with a numbered list of issues to fix, **Checking the requirements:** (one line per acceptance criterion: Done / Mostly done / Not done yet / Can't check here), and **Before we can release this:** when deploys or other teams are involved.
- **Inline comments:** a plain headline plus `Must fix`, `Should fix` or `Small thing, optional`, and `was already there before this PR` when it predates the PR.
- **Thread replies:** `Fixed`, `Not fixed yet`, `OK, no change needed` or `Needs a person to decide`, marked `(checked with AI help)`.

The "(with AI help)" text comes from `review_label` in `config.toml`.

## Checking on it

```sh
review-status          # or: ~/.auto-review/auto-review.py status
review-status --all    # also list PRs that are merged, closed or no longer waiting on me
```

Read-only and takes a few seconds: it never posts anything and doesn't run Claude. It reads `state.json`, the newest draft per PR in `drafts/`, today's log, and does one GitHub search to see which PRs are still waiting on me. The first line shows the mode, whether the job is idle, running or paused, and when the last cycle ran. Then each PR is listed with its round, when the job last acted, and a note, grouped as:

| Group | Meaning |
| --- | --- |
| Needs you | A thread is `needs_human`, or the PR passed `max_rounds` |
| Errors | The last attempt on this PR failed (from today's log) |
| Approved | An approval was posted |
| Ready to approve (not posted) | Approved in `dry-run`/`comment-only` mode; only a draft exists |
| Queued for the next run | Waiting on me but not reviewed yet |
| Waiting on the author | Comments posted or still not fixed |
| Concerns addressed, waiting on GitHub | Nothing open, approval held back by a gate (checks, mergeability) |
| Seen, nothing to do | Known to the job but it never posted (e.g. reviewed before the job existed) |

If GitHub can't be reached it says so and shows the local data only.

## Files

| Path | Purpose |
| --- | --- |
| `auto-review.py` | Entry point; calls `autoreview.cli.main` |
| `autoreview/cli.py` | One cycle: candidates, change detection, dispatch to a pass |
| `autoreview/passes.py` | The review pass and the recheck pass |
| `autoreview/report.py` | The read-only `status` overview |
| `autoreview/gates.py` | Whether a PR may be approved now, later, or not at all |
| `autoreview/payloads.py` | Review/approval bodies and inline-comment placement (pure, no I/O) |
| `autoreview/posting.py` | Saves drafts and posts what `mode` allows |
| `autoreview/github.py` | All `gh` calls: search, PR data, threads, checks, posting |
| `autoreview/claude.py` | Prompt loading and the read-only `claude -p` run |
| `autoreview/workspace.py` | PR checkouts, the `ctx/` files, rubric and Jira ticket |
| `autoreview/state.py` | `state.json` and the run lock |
| `autoreview/config.py` | Paths, constants, `config.toml`, my GitHub login |
| `autoreview/util.py` | Logging, subprocesses, notifications |
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

Requirements: `python3` 3.11+, `gh` (logged in), `claude`, `jira` CLI, `git`, and optionally `terminal-notifier`.

Clickable alerts: `brew install terminal-notifier`, then allow it in System Settings → Notifications → terminal-notifier (set the style to Alerts or Banners). With it, clicking an alert opens the PR, and a newer alert about the same PR replaces the older one. Without it (or before notifications are allowed), alerts fall back to `osascript` and clicking them does nothing useful.

```sh
# Secrets for the Jira CLI
echo 'export JIRA_API_TOKEN=...' > ~/.auto-review/env

# Install and start the launchd agent
cp ~/.auto-review/com.fil.auto-review.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.fil.auto-review.plist

# The review-status command (not in git; recreate it on a new machine)
mkdir -p ~/.local/bin
printf '#!/bin/sh\nexec python3 ~/.auto-review/auto-review.py status "$@"\n' > ~/.local/bin/review-status
chmod +x ~/.local/bin/review-status
```

`~/.local/bin` must be on `PATH`.

`gh` must be logged in as the account that reviews the PRs (the active account). If you're signed in to several accounts, `gh auth status` shows which one is active; the job posts as that one.

The review rubric is read fresh on every run from `prompt_files` in `config.toml`, so edits to those files take effect immediately.

## Settings

Main knobs in `config.toml`:

| Key | What it does |
| --- | --- |
| `mode` | `dry-run`, `comment-only` or `live` (below) |
| `repos` | Only PRs in these repos are reviewed |
| `wait_for_checks` | `true`: hold approval until CI checks pass. `false`: approve once every concern is addressed |
| `max_rounds` | Past this many rounds the job stops and notifies once |
| `max_changed_lines` | Bigger PRs are reviewed but never approved |
| `max_prs_per_run` | PRs processed per 10-minute cycle |
| `review_label` | The "(with AI help)" text in review headers and replies |
| `retention_days` | Drafts and logs older than this are deleted |

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
- **What's going on:** `review-status`.
- **Logs:** `tail -f ~/.auto-review/logs/$(date +%F).log`.

Only one run at a time: a `lock/` directory guards the cycle and is taken over if it's older than 30 minutes.
