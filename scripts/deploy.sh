#!/bin/zsh
# Run tests, push main, and merge main into the state branch the cloud uses.
# Usage: scripts/deploy.sh "commit message"   (commits any staged/unstaged changes first)
set -e
cd "$(dirname "$0")/.."
G=$(command -v gh || echo /opt/homebrew/bin/gh)
python3 -m unittest discover -s tests >/dev/null 2>&1 || { echo "❌ tests failed — not deploying"; python3 -m unittest discover -s tests 2>&1 | tail -20; exit 1; }
echo "✅ tests passed"
if [[ -n "$1" && -n "$(git status --porcelain)" ]]; then
  git add -A && git commit -q -m "$1"
fi
git push -q origin main
WT=$(mktemp -d)/statebr
git fetch -q origin claude/intraday-state
git worktree add -q "$WT" origin/claude/intraday-state
( cd "$WT" && git checkout -q -B claude/intraday-state origin/claude/intraday-state \
  && git merge -q --no-edit origin/main && git push -q origin claude/intraday-state )
git worktree remove --force "$WT"
echo "✅ main pushed and merged into claude/intraday-state"
