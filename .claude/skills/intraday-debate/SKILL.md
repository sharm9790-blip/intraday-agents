---
name: intraday-debate
description: Run the Bull → Bear → Judge intraday stock debate for NSE stocks and write the verdict, plan and report into this repo's state/ and reports/ folders for the price watcher. Use for "/intraday-debate", "run the debate", "find an intraday trade".
---

# Intraday debate (cloud / repo version)

Arguments (optional): symbols or `scan`; `capital=<₹>` (default 100000); `risk=<%>` (default 1).

All state lives on the git branch `claude/intraday-state`. Work on that branch.

## Steps
1. **Branch.** `git fetch origin claude/intraday-state && git checkout claude/intraday-state` (if it does not exist: `git checkout -b claude/intraday-state`). `git pull --rebase` before writing.
2. **Clock.** Call Groww `resolve_market_time_and_calendar`. Not a trading day, or outside 09:40–14:35 IST → write nothing and stop.
3. **Today's state.** Read `state/plan.json` and `state/watch.json` (the 5-min watcher writes `watch.json`).
   - If `plan.json` is for today and `watch.json` shows that plan with status `waiting`, `in_zone` or `t1_hit` → a plan is live; do NOT debate. Stop.
   - If today's GO count (`plan.json` → `go_count`, today only) is 2 → stop.
   - Collect today's rejected symbols from `state/rejected.json` (today only).
4. **Bull → Bear → Judge.** Launch `bull-agent` (scan; skip rejected symbols unless data clearly changed; use TODAY's live 5-min candles; scan ~10 relative-strength F&O stocks, analyse the best up to 5), then `bear-agent` with the Bull's full output, then `judge-agent` with both. Run them one after another, never in parallel.
5. **Write files** (IST time, HHMM):
   - `reports/YYYY-MM-DD_HHMM.md` — all three outputs in full + the disclaimer.
   - `state/rejected.json` — `{"date": "YYYY-MM-DD", "symbols": {"SYM": "one-line reason"}}`, merged with today's.
   - `state/plan.json` — always overwrite with:
     ```json
     {"date": "YYYY-MM-DD", "verdict_id": "YYYY-MM-DD_HHMM", "go_count": 0,
      "plan": null}
     ```
     or, if the Judge gave GO / GO WITH CONDITIONS (top pick only), `go_count` + 1 and
     ```json
     "plan": {"symbol": "HDFCBANK", "yahoo": "HDFCBANK.NS", "side": "long",
              "zone_low": 766.0, "zone_high": 767.5, "entry": 766.5, "max_entry": 768.0,
              "trigger_close_above": 766.0, "sl": 761.8, "t1": 777.0, "t2": 784.0, "qty": 185,
              "deadline": "10:45", "cancel_if_high_reaches": 777.0, "cancel_if_close_below": 764.0,
              "exit_if_close_below_after_entry": 763.5, "time_stop": "15:10"}
     ```
     Numbers only, no ₹ signs. `yahoo` = NSE symbol + `.NS` (M&M → `M&M.NS`).
   - `state/latest_verdict.txt` — the Telegram message, read on a PHONE. Follow `.claude/skills/intraday-debate/telegram-format.md` exactly: Telegram HTML (`<b>`, `<i>` only), short lines (≤ 34 characters where possible), emoji markers, blank lines between blocks, no tables, no markdown, `&` written as `&amp;` (e.g. `M&amp;M`), max 2,500 characters.
6. **Commit and push**: `git add -A && git commit -m "verdict YYYY-MM-DD_HHMM" && git push origin claude/intraday-state` (on a push conflict: `git pull --rebase` then push again). The watcher sends `latest_verdict.txt` to Telegram on its next run.

## Rules
- No agent places, modifies or cancels orders, and neither do you. Never call order, portfolio, holdings, positions, family, networth, watchlist or save_analysis tools.
- If one connector (Groww / INDmoney / InvestSights) is unavailable, continue with the others and say so in the report and verdict.
- Disclaimer (end of every report and verdict): `Educational analysis from market data, not SEBI-registered investment advice. Intraday trading is high risk; you place and manage any order yourself.`
