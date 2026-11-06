# intraday-agents

Bull → Bear → Judge intraday analysis for NSE stocks, with Telegram alerts. Analysis only — nothing here places orders.

| Part | Where it runs | How often | What it does |
|---|---|---|---|
| Debate (option 1) | Claude cloud routine | hourly, 09:45–14:45 IST, Mon–Fri | Runs `.claude/agents/bull-agent`, `bear-agent`, `judge-agent` via the `intraday-debate` skill; writes `reports/`, `state/plan.json`, `state/latest_verdict.txt` to branch `claude/intraday-state`. The verdict tells you which Groww alerts / SL / target orders to set. |
| Watcher (option 2) | GitHub Actions (`.github/workflows/watch.yml`) | every 5 min, 09:15–15:30 IST | `watcher/watch.py` forwards each new verdict to Telegram, checks the live price (Yahoo Finance 5-min candles), and alerts on buy trigger / T1 / T2 / SL / cancel / time stop. Writes `state/watch.json`. |

Secrets (repo → Settings → Secrets and variables → Actions): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

Test the watcher locally: `DRY_RUN=1 python3 watcher/watch.py path/to/state` (on python.org Python for macOS add `SSL_CERT_FILE=/etc/ssl/cert.pem`).

Educational analysis from market data, not SEBI-registered investment advice. Intraday trading is high risk; you place and manage any order yourself.
