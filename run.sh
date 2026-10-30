#!/bin/zsh
# launchd entry point. launchd doesn't read ~/.zshrc, so secrets the job needs
# (JIRA_API_TOKEN) go in ~/.auto-review/env, e.g.:  export JIRA_API_TOKEN=...
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin
[[ -f $HOME/.auto-review/env ]] && source $HOME/.auto-review/env
exec /usr/bin/env python3 $HOME/.auto-review/auto-review.py "$@"
