# Intraday Bull / Bear / Judge — setup and operations guide

How the whole system is wired, where every piece lives, how to change it, and how to fix it.
Last updated 2026-11-06.

> Analysis only. Nothing in this system places, modifies or cancels orders. You place every trade yourself in Groww.
> Educational analysis from market data, not SEBI-registered investment advice. Intraday trading is high risk.

---

## 1. What it does

Every 15 minutes during market hours, three Claude agents debate NSE intraday long trades:

| Agent | File | Job |
|---|---|---|
| 🐂 Bull | `.claude/agents/bull-agent.md` | Scans ~10 relative-strength F&O stocks, analyses the best (up to 5), lists every indicator that supports a buy. |
| 🐻 Bear | `.claude/agents/bear-agent.md` | Re-checks the Bull's numbers with its own data, runs a 10-point risk checklist, marks each idea REJECT / WEAK / ACCEPTABLE. |
| ⚖️ Judge | `.claude/agents/judge-agent.md` | Gives GO / GO WITH CONDITIONS / NO-GO with entry, SL, T1, T2, quantity and exit plan. |

A Python watcher then follows the chosen plan's live price and sends Telegram alerts (buy trigger, targets, stop loss, cancel, time stop).

Data sources (claude.ai connectors): **Groww**, **INDmoney**, **InvestSights**. The watcher uses Yahoo Finance 5-min candles (no key needed).

---

## 2. Architecture

```
cron-job.org  ──every 15 min──▶  GitHub Actions: watch.yml  (watcher/watch.py)
                                   │  • forwards new verdicts to Telegram
                                   │  • checks live price of the plan, sends alerts
                                   │  • no live plan → POST routine API trigger (:00/:15/:30/:45)
                                   ▼
Claude cloud routine  (no schedule of its own — only API fires)
  (runs .claude/skills/intraday-debate: Bull → Bear → Judge)
                                   │
                                   ▼
git branch claude/intraday-state   state/plan.json, latest_verdict.txt, rejected.json,
                                   watch.json, reports/YYYY-MM-DD_HHMM.md
                                   │
                                   ▼
Telegram bot  @sharmi_intraday_bot  ──▶  your phone
```

### Daily timeline (IST, Mon–Fri)

| Time | What happens |
|---|---|
| 09:15 | Watcher starts checking (exits by itself outside 09:15–15:30). |
| 09:45 | First debate (fired by the watcher). |
| 09:45 – 14:30 | Debate every 15 min, all fired by the watcher through the routine's API trigger. |
| 14:30 | No new entries after this (Judge rule); last fires. |
| 15:10 | Time stop — exit any open position. |
| 15:30 | Watcher stops. |

---

## 3. Components and where they live

### 3.1 Repo (this one)

`github.com/sharm9790-blip/intraday-agents` (private). Local copy: `~/Downloads/YouTube/intraday-agents`.

| Path | What |
|---|---|
| `.claude/agents/*.md` | The three agent definitions. |
| `.claude/skills/intraday-debate/SKILL.md` | Cloud run steps (branch, clock, state checks, debate, write files, push). |
| `.claude/skills/intraday-debate/telegram-format.md` | Mobile Telegram templates (GO / NO TRADE / connector problem). |
| `watcher/watch.py` | 5/15-min price watcher, Telegram sender, debate firer. |
| `.github/workflows/watch.yml` | Runs the watcher (workflow_dispatch from the pinger + 30-min backup schedule). |
| `docs/SETUP.md` | This guide. |

**Branches**
- `main` — code. GitHub Actions runs the watcher from `main`.
- `claude/intraday-state` — code (merged from main) **plus** `state/` and `reports/`. The cloud routine works on this branch.

> When you change agents, skill or watcher: commit to `main`, then merge `main` into `claude/intraday-state` and push both.

### 3.2 Claude cloud routine

- Page: https://claude.ai/code/routines/trig_014p6yZgKx8HWEs16mh19mgn
- Name: "⚡ Intraday Bull/Bear/Judge — hourly (IST 9:45–14:45)"
- Schedule trigger: **none** (removed). Debates start only through the API trigger, fired by the watcher. Don't add a schedule back, or scans run twice.
- API trigger: Fire URL `https://api.anthropic.com/v1/claude_code/routines/trig_014p6yZgKx8HWEs16mh19mgn/fire` + bearer token (stored only in GitHub secret `CLAUDE_FIRE_TOKEN`)
- Model: `claude-sonnet-5-5`; environment: Default
- Repo: this one; connectors: Groww, INDMONEY, investsights_in only
- Allowed tools: Bash, Read, Write, Edit, Glob, Grep, Agent, Skill, ToolSearch
- The prompt tells it to read and follow `.claude/skills/intraday-debate/SKILL.md` with `scan capital=100000 risk=1`.

Limits (research preview): API fires 30/hour per routine (we use ≤ 3). Every run uses your Claude subscription allowance — up to ~19 runs/day, fewer when a plan is live (no debate then).

### 3.3 GitHub Actions watcher

- Workflow: `.github/workflows/watch.yml`, job `watch`, timeout 5 min, `concurrency: intraday-watcher`.
- Checks out `claude/intraday-state` into `statebr/`, runs `python3 watcher/watch.py statebr/state`, commits `state/watch.json` back.
- Own schedule: `*/30 3-10 * * 1-5` UTC — **backup only** (GitHub delays free scheduled jobs by 15–75 min).
- Actions page: https://github.com/sharm9790-blip/intraday-agents/actions

### 3.4 cron-job.org pinger (the real cadence)

GitHub runs `workflow_dispatch` immediately, so an external pinger gives on-time runs.

| Setting | Value |
|---|---|
| URL | `https://api.github.com/repos/sharm9790-blip/intraday-agents/actions/workflows/watch.yml/dispatches` |
| Method | `POST` |
| Schedule | `*/15 9-15 * * 1-5` with timezone **Asia/Kolkata** (use `*/5` for faster alerts) |
| Header | `Authorization: Bearer github_pat_…` |
| Header | `Accept: application/vnd.github+json` |
| Header | `User-Agent: cron-job-org` |
| Body | `{"ref":"main"}` |
| Success | HTTP **204** in the job's History |

GitHub token for it: fine-grained PAT `intraday-pinger`, repository access **only `intraday-agents`**, permission **Actions: Read and write**, nothing else. Create at https://github.com/settings/personal-access-tokens/new. Renew before it expires (1 year) and paste the new one into the cron-job.org header.

### 3.5 Telegram

- Bot: `@sharmi_intraday_bot` (created with @BotFather). You sent `/start` so it can message you.
- GitHub secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Local copy for desktop runs: `~/.config/telegram-bot/env` (chmod 600), sender `~/stock-debates/bin/tg_send.sh`.
- Messages are Telegram HTML (`<b>`, `<i>` only), short lines for a phone, `&` written as `&amp;`. If Telegram rejects the markup, the sender resends as plain text.

### 3.6 GitHub secrets (repo → Settings → Secrets and variables → Actions)

| Secret | Used for |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Sending alerts |
| `TELEGRAM_CHAT_ID` | Your chat |
| `CLAUDE_FIRE_URL` | Routine API trigger URL |
| `CLAUDE_FIRE_TOKEN` | Routine API trigger token |

Without the two `CLAUDE_FIRE_*` secrets no debates run at all. Never put any token in code, chat or a commit.

### 3.7 Old desktop-app task (paused)

`intraday-debate-auto` in the Claude desktop app (Scheduled in the sidebar), skill at `~/.claude/scheduled-tasks/intraday-debate-auto/SKILL.md`, state in `~/stock-debates/state/`. Paused because the cloud setup replaces it; it only ran while the Mac and app were open. Re-enable only if the cloud setup is down, and pause it again after, or you get double alerts.

---

## 4. Trading rules built in

- Capital ₹1,00,000, risk 1% per trade (₹1,000). Qty = floor(risk ÷ (entry − SL)), capped by intraday margin.
- GO only if net R:R ≥ 1:1.5 to T1 after ~0.06% round-trip costs, SL at real structure, no high-severity Bear flag, before 14:30.
- Book 50% at T1, move SL to cost, trail the rest to T2. No averaging down, no widening the stop.
- Time stop 15:10. One live plan at a time.
- **Daily limit: 2 trades that actually hit the buy trigger.** Plans cancelled before entry don't count. After 2, the watcher sends 🏁 DONE FOR TODAY and stops firing debates.
- Rejected symbols are remembered for the day (`state/rejected.json`) and skipped unless data clearly changed.

Groww watchlist **"My Draft Basket"** (created 2026-11-07) holds today's planned stocks: each GO says ➕ add it, every cancel / exit alert says ➖ remove it. The watchlist only keeps the stock handy; quantities and prices stay in the Telegram message.

What you do on each GO message: set the Groww price alert; after buying, immediately place SL-M and the T1 sell order; exit everything by 15:10.

---

### Code checks (risk.py, news.py)

Adapted from the MIT-licensed [trader-skill](https://github.com/Jakeschincariol/trader-skill) ideas (trader-risk, trader-budget, trader-news), rewritten for NSE intraday.

- `watcher/risk.py` — sizing and hard limits by code, not the AI. Settings at the top: `CAPITAL` ₹1,00,000, `RISK_PCT` 1, `FIXED_COST` ₹40, `PCT_COST` 0.04%, `MIN_RR` 1.5, `MAX_POSITION_X` 1.0 (position ≤ capital; raise for MIS leverage), `LAST_ENTRY` 14:30. Run by the cloud debate before the verdict (`--write` fixes qty or blocks) and again by the watcher on each new GO.
- The cloud sandbox can't reach outside websites (Google News, Yahoo, groww.in — proxy 403, tested 2026-11-07). So in the cloud the news check uses InvestSights news/earnings tools; the GitHub watcher runs news.py and verifies Groww links.
- `watcher/news.py` — Google News RSS (no key), last 24 h, flags results / board meeting / SEBI / raids / F&O ban as high, dividends / deals / ratings as medium. Used by the cloud debate (high = NO-GO) and in the 🛡 CHECKS and 🔔 BUY TRIGGER messages.

### 🧾 My Trade tracker (two-way bot)

Tell the bot what you really did in Groww; it tracks **your** trade every 5 min, separately from the plan alerts.

| You send | Bot does |
|---|---|
| `BOUGHT TATASTEEL` | Owner (first `TELEGRAM_CHAT_ID`): starts tracking with today's plan qty and the market price, and fires the routine with payload `SYNC_POSITIONS` → one read-only Groww call (`get_my_trading_positions_today`) → `state/positions.json` → real qty and average price replace the estimate (🔄 SYNCED FROM GROWW, 2–5 min). Others: plan qty, market price. |
| `BOUGHT TATASTEEL 544 183.55` | Uses your numbers (no Groww call) |
| `BOUGHT SBIN 100 812 SL 806 T1 824` | Any stock outside the plan (SL required) |
| `SOLD TATASTEEL` / `SOLD TATASTEEL 272 185.9` | Books the sale (owner: real sell price synced from Groww) |
| `MYTRADES` · `HELP` | Status / commands |
| `PLAN` | Resend today's latest verdict |
| `SCORE` | Scorecard + paper account + your real P&L so far |
| `RISK 0.5` / `RISK 1` | Your size for today (0.25–1×); used when BOUGHT has no qty |
| `PAUSE` / `RESUME` | Stop / restart plan broadcasts for you today (your own trade alerts still come) |

All commands are in the bot's **/ Menu** (registered with `setMyCommands`; `BOT_COMMANDS` in `mytrade.py`).

**📋 Live board:** while any trade is open, each person gets ONE pinned message listing all their open trades (price, P&L, distance to SL, next target, total), edited every 5 min — edits are silent, so the phone doesn't buzz. When all trades are closed it shows "✅ No open trades now".

Loud alerts, only to the person who sent the trade: real risk check (≤ ₹1,000), 🔴 SL touched, 🎯 T1 (sell half, SL to cost), 🏁 T2, ↩️ back at cost, ⏳ 15:00, ⏰ 15:10 exit, 🧾 MY TRADES TODAY at 15:45.

Plumbing: `watcher/mytrade.py` + `.github/workflows/mytrade.yml`. `watch.yml` starts `mytrade.yml` (`gh workflow run`) on any pinger run that has an open trade, a pending Groww sync, a new Telegram message, or a due summary. State: `state/mytrades.json` (Telegram offset, trades per chat), `state/positions.json` (last Groww sync). Messages are read only from the chats in `TELEGRAM_CHAT_ID`; messages sent outside 09:00–15:55 are handled at the next morning's first run. Skill: `.claude/skills/positions-sync/SKILL.md`. Nothing places, changes or cancels orders.

### 🩺 Health alerts (`watcher/health.py`)

Each at most once: 🟢 **SYSTEM READY** at the first run from 09:15 (watcher, cloud trigger, December holiday reminder) · ⚠️ **WATCHER GAP** (no run for 20+ min) · ⚠️ **PINGER?** (run came from GitHub's backup schedule) · ⚠️ **SCAN FAILED** (routine trigger refused, with the fix for 401/403/429) · ⚠️ **NO VERDICT** (fired but no result in 25 min) · ⚠️ **Groww sync is late** (15 min, owner only).

### 🗓 Weekly report and learning

- Friday (or the week's last trading day) after the scorecard: 🗓 **WEEKLY REPORT** — plans, bought/wins/losses, entry/T1/T2 hit rates, week P&L four ways, paper balance, why plans weren't bought; each person also gets their real-trade total. State: `state/weekly.json`, `state/realtrades.json`.
- `watcher/stats.py state` summarises the last 10 scorecard days. The cloud debate runs it first and gives it to the Judge ("What recent plans did"), whose rules say to follow its **Lesson:** lines (e.g. buy zone nearer the live price when entries are rarely reached).

### 🧪 Tests and scripts

- `tests/test_system.py` — offline tests (fake candles, clock, Telegram): risk, replay, firing window, buy→T1→T2 with paper lines, cancel, holiday, scorecard + paper account, health alerts, My Trade commands + Groww sync + 15:10 + summary, stats lesson. Run `python3 -m unittest discover -s tests -v`. GitHub runs them on every push (`tests.yml`); a ❌ means don't trust the change.
- `scripts/deploy.sh "message"` — runs the tests, commits, pushes `main`, merges it into `claude/intraday-state`. Refuses to deploy if tests fail.
- `scripts/check.sh` — secrets present, tests pass, state branch up to date, last watcher run, next year's holidays.

### NSE holidays

`NSE_HOLIDAYS` at the top of `watcher/watch.py` lists weekday holidays (2026: 10 Nov Diwali Balipratipada, 24 Nov Guru Nanak Jayanti, 25 Dec Christmas). On those days the watcher does nothing, so no cloud runs are wasted. **Add the next year's NSE list every December.** Special sessions such as Diwali Muhurat trading (Sunday 8 Nov 2026) are not covered — the system runs Mon–Fri only.

## 5. State files (branch `claude/intraday-state`)

| File | Written by | Content |
|---|---|---|
| `state/plan.json` | Cloud debate | `date`, `verdict_id` (`YYYY-MM-DD_HHMM`), `go_count` (tally of GO verdicts, info only), `plan` (null or symbol, yahoo, zone_low/high, entry, max_entry, trigger_close_above, sl, t1, t2, qty, deadline, cancel rules, exit rule, time_stop — numbers only). |
| `state/latest_verdict.txt` | Cloud debate | Telegram message for that verdict (mobile format). |
| `state/rejected.json` | Cloud debate | `{"date", "symbols": {"SYM": "reason"}}` |
| `state/watch.json` | Watcher | `date`, `verdict_id` already forwarded, `status`, `since`, `trades` (bought today), `last_fire` (slot), `done_sent`, `log`. |
| `state/paper.json` | Watcher | Paper account: ₹1,00,000 start (2026-11-06), every plan taken exactly by its rules (costs included), trades per day and balance. |
| `state/scorecard.json` | Watcher | One entry per day: each plan's replay (rules / limit / market P&L and steps) and day totals. |
| `reports/YYYY-MM-DD_HHMM.md` | Cloud debate | Full Bull, Bear and Judge outputs. |

Plan status flow (watcher):

```
waiting ──green 5-min close in zone──▶ in_zone (🔔 BUY TRIGGER, trades+1)
waiting ──deadline / ran to T1 without dip / close below cancel level──▶ cancelled (❌)
in_zone ──SL touched──▶ closed (🔴)     in_zone ──early-exit close──▶ closed (⚠️)
in_zone ──T1──▶ t1_hit (🎯 sell half, SL to cost)
t1_hit ──T2──▶ closed (🏁)               t1_hit ──back to entry──▶ closed (↩️)
in_zone / t1_hit at 15:10 ──▶ closed (⏰ TIME STOP)
```

A 📊 status card is sent on runs in the first 5 minutes of each quarter hour while a plan is live.

---

## 6. Telegram messages you'll see

| Message | Meaning |
|---|---|
| 🟢 GO — SYMBOL | New plan: buy zone, SL, targets, qty, conditions, what to set in Groww |
| ⚡ HIGH CONFIDENCE · 📝 Draft order | Inside a GO with confidence above 7: tap-to-copy Qty / Buy / SL / T1 values and a Groww stock link. You place the order yourself. |
| ⚪ NO TRADE | Nothing passed; reasons and next scan time |
| ⚠️ NO ANALYSIS | A data connector was missing in the cloud run |
| 📊 SYMBOL · HH:MM | Status of the live plan |
| 🔔 BUY TRIGGER | Conditions met — buy now, then place SL-M |
| 🎯 TARGET 1 HIT | Sell half, move SL to cost |
| 🏁 TARGET 2 HIT | Sell the rest |
| 🔴 STOP LOSS HIT / ⚠️ EARLY EXIT / ↩️ BACK AT COST | Exit |
| 🛡 CHECKS — SYMBOL | Right after each GO: code risk check (✅ passed with R:R, risk ₹, value, or ⛔ BLOCKED with reasons; 🧮 corrected qty) and a 24 h news check (✅ / 📰 / ⚠️ EVENT RISK) |
| ❌ CANCELLED | Don't buy; delete the Groww alert |
| ⏰ TIME STOP | Exit everything (15:10) |
| 📈 Paper lines | In 🎯 T1 / 🔴 SL / 🏁 T2 / ↩️ / ⚠️ / ⏰ alerts: the paper result of the live trade (plan qty, before costs) |
| 📒 SCORECARD | 15:45 on market days (needs the pinger to run until 15:55): per stock cash ₹1,00,000 ÷ entry = qty, 50% at T1 and rest at T2 with ✅/❌ for whether price reached entry / T1 / T2, target profit (before costs), high and close; then each of today's plans replayed on 5-min candles — by the plan's rules, as a plain limit order, and bought at once — with today's and the running paper P&L (`state/scorecard.json`) |
| 🏁 DONE FOR TODAY | Sent once a day: when 2 bought trades are used, or at ~14:40 when scanning ends with no plan live |

---

## 7. Common tasks

**Run a debate now** — routine page → **Run now**, or locally `gh workflow run watch.yml` (fires only in a free quarter-hour slot, 09:45–14:30).

**Test the watcher locally**
```bash
DRY_RUN=1 SSL_CERT_FILE=/etc/ssl/cert.pem python3 watcher/watch.py path/to/state
```
`DRY_RUN=1` prints messages instead of sending and never fires the routine.

**Change capital or risk** — edit the routine prompt on claude.ai (`scan capital=… risk=…`).

**Change scan frequency** — debates: watcher fires every quarter hour 09:45–14:30 (see `fire_debate` in `watch.py`). Alerts: change the cron-job.org schedule.

**Change agent behaviour** — edit `.claude/agents/*.md` on `main`, merge into `claude/intraday-state`, push both. Also copy to `~/.claude/agents/` if you use the local `/intraday-debate` skill.

**Rotate a token** (both the Telegram bot token and the routine token were pasted in chat on 6 Nov — rotate them once)
- Telegram: @BotFather → `/revoke` → update `TELEGRAM_BOT_TOKEN` secret and `~/.config/telegram-bot/env`.
- Routine: routine page → Edit → API trigger → **Regenerate** → update `CLAUDE_FIRE_TOKEN`.
- Pinger: create a new PAT → paste into cron-job.org header → delete the old PAT.

Update a secret from the terminal (token read from your clipboard, never typed into chat):
```bash
pbpaste | gh secret set CLAUDE_FIRE_TOKEN -R sharm9790-blip/intraday-agents
```

**Pause everything** — pause the cron-job.org job, disable the routine on claude.ai, and disable the workflow under Actions.

---

## 8. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| No messages for a long time | `state/plan.json` `go_count`, `state/watch.json` `trades`, `status` | 2 trades bought → done for the day. Plan live → only status cards every 15 min. |
| Watcher runs far apart | Actions page: are there `workflow_dispatch` runs every 15 min? | cron-job.org History: 401 = bad PAT, 404 = wrong URL / PAT repo access, timezone must be Asia/Kolkata. |
| No debates at all | Watcher log line `fired debate for slot …` or `fire failed: 401` | Regenerate the routine token and update `CLAUDE_FIRE_TOKEN`. |
| Cloud run fails | Routine page → runs → open the session log | 401 `github_token_missing`: reconnect GitHub on claude.ai. 403 `repo_access_denied`: install the Claude GitHub app on the repo. |
| ⚠️ NO ANALYSIS | Routine connectors | Re-attach Groww / INDMONEY / investsights_in on the routine; reconnect them at claude.ai/customize/connectors. |
| Message arrives as plain text | Bad HTML (often a raw `&`) | Fix `latest_verdict.txt` formatting rules in `telegram-format.md`. |
| GitHub minutes warning | Billing → Actions usage (shared with `unread-autopilot`, 2,000 free min/month for private repos) | Use the 15-min pinger, or make this repo public (unlimited minutes; reports become visible). |
| Local Python SSL error | `CERTIFICATE_VERIFY_FAILED` | Prefix with `SSL_CERT_FILE=/etc/ssl/cert.pem`. |
| `gh: command not found` | Homebrew not on PATH | Use `/opt/homebrew/bin/gh` or add `eval "$(/opt/homebrew/bin/brew shellenv)"` to `~/.zprofile`. |

---

*Educational analysis from market data, not SEBI-registered investment advice. Intraday trading is high risk; you place and manage any order yourself.*
