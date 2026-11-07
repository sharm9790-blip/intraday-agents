#!/bin/zsh
# Quick health check of the whole setup (run on your Mac).
cd "$(dirname "$0")/.."
G=$(command -v gh || echo /opt/homebrew/bin/gh)
ok() { echo "✅ $1"; }; bad() { echo "❌ $1"; }
for s in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID CLAUDE_FIRE_URL CLAUDE_FIRE_TOKEN; do
  $G secret list | grep -q "^$s" && ok "secret $s" || bad "secret $s missing"
done
python3 -m unittest discover -s tests >/dev/null 2>&1 && ok "tests pass" || bad "tests FAIL"
git fetch -q origin
git merge-base --is-ancestor origin/main origin/claude/intraday-state && ok "state branch has latest code" \
  || bad "state branch is behind main → run scripts/deploy.sh"
last=$($G run list --workflow watch.yml --limit 1 --json createdAt,event,conclusion -q '.[0] | "\(.createdAt) \(.event) \(.conclusion)"')
echo "ℹ️  last watcher run: $last (UTC)"
y=$(( $(date +%Y) + 1 ))
grep -q "\"$y-" watcher/watch.py && ok "NSE holidays for $y listed" || echo "ℹ️  add $y NSE holidays to watcher/watch.py (each December)"
echo "ℹ️  routine: https://claude.ai/code/routines/trig_014p6yZgKx8HWEs16mh19mgn"
echo "ℹ️  pinger: check History on cron-job.org shows 204"
