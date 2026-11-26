# intraday-agents

Bull → Bear → Judge intraday analysis for NSE stocks, with Telegram alerts. Analysis only — nothing here places orders.

📘 Full setup, operations and troubleshooting guide: [docs/SETUP.md](docs/SETUP.md)

| Part | Where it runs | How often | What it does |
|---|---|---|---|
| Debate (option 1) | Claude cloud routine | every 15 min, 09:45–14:45 IST, Mon–Fri (API trigger fired by the watcher at :00/:15/:30/:45) | Runs `.claude/agents/bull-agent`, `bear-agent`, `judge-agent` via the `intraday-debate` skill; writes `reports/`, `state/plan.json`, `state/latest_verdict.txt` to branch `claude/intraday-state`. The verdict tells you which Groww alerts / SL / target orders to set. |
| Watcher (option 2) | GitHub Actions (`.github/workflows/watch.yml`) | every 5 min, 09:15–15:30 IST (cron-job.org → `workflow_dispatch`; GitHub's own 30-min schedule is a backup) | `watcher/watch.py` forwards each new verdict to Telegram, checks the live price (Yahoo Finance 5-min candles), and alerts on buy trigger / T1 / T2 / SL / cancel / time stop. When no plan is live it also POSTs the routine's API trigger every quarter hour (09:45–14:30, while fewer than 2 plans have hit their buy trigger today; cancelled plans don't count). Writes `state/watch.json`. |

| **Strategy lab** paper test | GitHub Actions (inside the watcher) | Iron fly: 09:20 open, every run to 15:15 · stocks: 15:45 replay | `watcher/strategies.py`, code only, no AI: **VWAP mean reversion** and **gap fade** on the F&O stocks (replayed on the day's 5-min candles, signals only see the past), and a **Nifty iron fly** (sell ATM CE+PE, buy ±200 wings, out at −₹3,000 / +₹2,000 / 15:15) marked to market on the INDmoney option chain (needs `WORKER_URL` + `WORKER_KEY`). The fly sits out on expiry days, event days (`watcher/events.json`), a VIX spike or a ≥1% Nifty gap; stock trades on event days are tagged and shown apart. State `state/strategies.json`, results in the 15:45 scorecard. Paper only. |

| Nifty options **paper test** | Claude cloud routine (its own scheduled task) | ~08:45, hourly 09:30–14:30, ~15:10 IST | `.claude/skills/nifty-options-debate` runs `nifty-bull-agent` → `nifty-bear-agent` → `nifty-judge-agent` on Nifty 50 weekly options (buy CE / PE only). `watcher/nifty_paper.py` checks risk, sizes in lots and replays the option's own candles into `state/nifty_paper.json`; the watcher forwards `state/nifty_verdict.txt` (🧪 PAPER) to Telegram. No orders, separate from the stock plans. Record: `python3 watcher/nifty_paper.py report state/nifty_paper.json`. |

Secrets (repo → Settings → Secrets and variables → Actions): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and for the 15-min debate `CLAUDE_FIRE_URL` + `CLAUDE_FIRE_TOKEN` (routine → Edit → Trigger + → API → copy Fire URL, Generate token). Without them the debate stays hourly.

Test the watcher locally: `DRY_RUN=1 python3 watcher/watch.py path/to/state` (on python.org Python for macOS add `SSL_CERT_FILE=/etc/ssl/cert.pem`).

Educational analysis from market data, not SEBI-registered investment advice. Intraday trading is high risk; you place and manage any order yourself.

Risk sizing, hard-limit checks and the news check (`watcher/risk.py`, `watcher/news.py`) adapt ideas from [Jakeschincariol/trader-skill](https://github.com/Jakeschincariol/trader-skill) (MIT).
