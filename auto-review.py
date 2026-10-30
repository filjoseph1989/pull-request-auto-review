#!/usr/bin/env python3
"""Local auto-review: review PRs waiting on me, post inline findings, approve once they're addressed.

Plan: /Users/fil/Fil/value-chain-project/docs/auto-review-plan.md

Claude only ever reads and returns JSON; this script does all the posting, gated by `mode` in config.toml.
The code lives in the autoreview/ package; this file is the entry point run.sh and launchd call.

    auto-review.py                 one cycle (what launchd runs)
    auto-review.py --pr OWNER/REPO#N [--force]   one PR, ignoring the search and (with --force) the state
"""

import sys

from autoreview.cli import main

if __name__ == "__main__":
    sys.exit(main())
