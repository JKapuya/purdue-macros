#!/bin/zsh
# Daily refresh: regenerate today's page, then push it so the cloud routine
# has something current to publish to the artifact.
#
# Run by launchd at 6:30 AM (com.jkapuya.purduemacros). The cloud routine
# publishes ~30 min later, which leaves room for a slow or asleep Mac.
set -u

REPO=/Users/jonathankapuya/purdue-macros
PYTHON=/usr/bin/python3
GIT=/opt/homebrew/bin/git

cd "$REPO" || exit 1

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
"$PYTHON" purdue_macros.py || { echo "generator failed, not pushing"; exit 1; }

# Nothing to push if the menu did not change (Purdue republishes the same
# page on closed days).
if [ -n "$("$GIT" status --porcelain today.html latest.json)" ]; then
  "$GIT" add today.html latest.json
  "$GIT" commit -q -m "menu $(date +%F)" && echo "committed"
  "$GIT" push -q origin main && echo "pushed" || echo "push failed"
else
  echo "no menu change, nothing to push"
fi
