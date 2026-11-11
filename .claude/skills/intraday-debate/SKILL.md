---
name: intraday-debate
description: Run the Bull → Bear → Judge intraday stock debate for NSE stocks and write the verdict, plan and report into this repo's state/ and reports/ folders for the price watcher. Use for "/intraday-debate", "run the debate", "find an intraday trade".
---

# Intraday debate (cloud / repo version)

Arguments (optional): symbols or `scan`; `capital=<₹>` (default 100000); `risk=<%>` (default 1).

All state lives on the git branch `claude/intraday-state`. Work on that branch.

## Steps
1. **Branch.** `git fetch origin claude/intraday-state && git checkout claude/intraday-state` (if it does not exist: `git checkout -b claude/intraday-state`). `git pull --rebase` before writing.
2. **Clock.** Call Groww `resolve_market_time_and_calendar` (if Groww is unavailable, use `TZ=Asia/Kolkata date` and the NSE holiday list in `watcher/watch.py`). Not a trading day, or outside 09:40–14:35 IST → write nothing and stop.
3. **Today's state.** Read `state/plan.json` and `state/watch.json` (the 5-min watcher writes `watch.json`).
   - Live plans are the entries in `watch.json` → `live` (today only) with status `waiting`, `in_zone` or `t1_hit`. Up to **3** plans may be live at once, so a live plan does NOT stop the debate.
   - Stop (write nothing) if 3 plans are live, or `trades` (plans that hit the buy trigger today) + live `waiting` plans is 3 or more, or `day_pnl` is −2000 or lower (daily loss stop). Plans cancelled before the buy trigger do NOT count; `go_count` in `plan.json` is just a tally of GO verdicts.
   - Tell the Bull to skip the live symbols, and tell the Judge which plans are live (symbol, sector, status) so it avoids a near-duplicate bet (same sector moving together). Each plan is sized on its own (₹1,000 risk, position ≤ ₹1,00,000; MIS margin ≈ 20%, so 3 plans fit in ₹1,00,000).
   - Collect today's rejected symbols from `state/rejected.json` (today only).
   - Read `state/recheck.json` (today only): rejected stocks that have since closed ≥ 0.2% above the Judge's "flips on" level or their day high at rejection time (the watcher checks every 5 min). These are **not** skipped — give them to the Bull as **priority candidates** with their breakout line (level, time, close), ahead of new scan names (still analyse the best up to 5 in total; if more than 5, take the strongest by move above the level and relative strength).
   - Tell the Judge about the **sector-rally exception** (judge-agent.md): a second stock from a live plan's sector is allowed only if the sector index is up ≥ 1.5% and above its VWAP, most of the sector is green, and the stock passes every rule on its own; max 2 live plans per sector.
4. **Learn from the record.** Run `python3 watcher/stats.py state` and paste its text into the Judge's prompt under "What recent plans did". The Judge must follow its "Lesson:" lines when setting the buy zone and targets (still net R:R ≥ 1.5).
5. **Bull → Bear → Judge.** Launch `bull-agent` (scan; skip rejected symbols EXCEPT those in `recheck.json`, which must be analysed first; use TODAY's live 5-min candles; scan ~10 relative-strength F&O stocks, analyse the best up to 5), then `bear-agent` with the Bull's full output, then `judge-agent` with both. Run them one after another, never in parallel.
6. **Write files** (IST time, HHMM):
   - `reports/YYYY-MM-DD_HHMM.md` — all three outputs in full + the disclaimer.
   - `state/rejected.json` — `{"date": "YYYY-MM-DD", "symbols": {"SYM": "HH:MM one-line reason"}}`, merged with today's. Every reason starts with the IST time (HH:MM) of this debate, and add the flip price as `flips on 5-min close >PRICE …` when there is one — the watcher re-checks a stock once it closes above that level (or its day high). A re-checked stock that is rejected again gets its reason overwritten with the new time, so it needs a fresh breakout to come back.
   - `state/plan.json` — always overwrite with (it holds only THIS verdict's new plan; the watcher keeps the list of live plans itself):
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
              "exit_if_close_below_after_entry": 763.5, "time_stop": "15:00"}
     ```
     Always add `"connectors": {"groww": true, "indmoney": true, "investsights": true}` — `false` for any connector whose tools were missing, needed sign-in, or failed on every call this run (the watcher sends a one-time "OFFLINE — reconnect" alert for INDmoney / InvestSights; Groww is not alerted on).
     Numbers only, no ₹ signs. `yahoo` = NSE symbol + `.NS` (M&M → `M&M.NS`). Also add `"name"`: the company name for news search (e.g. `"Tata Steel"`), and `"entry_type"`: `"now"` (the watcher alerts BUY NOW as soon as the price is between `zone_low` and `max_entry`) or `"dip"` (waits for a green 5-min close in the zone), as the Judge decided.
   - **Code checks before the verdict text (mandatory for every GO):**
     1. `python3 watcher/news.py "<company name>"` — if `level` is `high` (results, board meeting, SEBI/raid, F&O ban… in the last 24 h or today), the trade is **NO-GO** unless the event is already over and priced in; say which headline. The cloud sandbox blocks outside websites, so it normally prints `unknown`: then use InvestSights `get_stock_news` / `get_earnings_calendar` / `get_corporate_actions` for the same check.
     2. `python3 watcher/risk.py state/plan.json --write` — code re-sizes `qty` (1% risk incl. ₹40 brokerage + 0.04% charges, position ≤ capital) and enforces hard limits (net R:R ≥ 1.5, levels in order, entry inside zone, deadline ≤ 14:30). Exit code 2 = **BLOCK**: the script has set `plan` to null; write a NO TRADE verdict that says "⛔ blocked by risk check" with its reasons. On PASS use the script's `qty` (and its risk ₹ / R:R) in the verdict and the draft order — never the Judge's own qty.
   - `state/latest_verdict.txt` — the Telegram message, read on a PHONE. Follow `.claude/skills/intraday-debate/telegram-format.md` exactly: Telegram HTML (`<b>`, `<i>` only), short lines (≤ 34 characters where possible), emoji markers, blank lines between blocks, no tables, no markdown, `&` written as `&amp;` (e.g. `M&amp;M`), max 2,500 characters. For **every GO**, add the **Draft order block** from `telegram-format.md` (Groww stock link + tap-to-copy values; the watcher verifies the link). It is text for the user only — never call any Groww order tool.
7. **Commit and push**: `git add -A && git commit -m "verdict YYYY-MM-DD_HHMM" && git push origin claude/intraday-state` (on a push conflict: `git pull --rebase` then push again). The watcher sends `latest_verdict.txt` to Telegram on its next run.

## Rules
- No agent places, modifies or cancels orders, and neither do you. Never call order, portfolio, holdings, positions, family, networth, watchlist or save_analysis tools.
- If one connector (Groww / INDmoney / InvestSights) is unavailable, continue with the others and note it in the report. In `latest_verdict.txt`, never mention Groww being offline / not connected (the user trades on INDmoney; that line is noise). An INDmoney or InvestSights outage may get one short line.
- Disclaimer (end of every report and verdict): `Educational analysis from market data, not SEBI-registered investment advice. Intraday trading is high risk; you place and manage any order yourself.`
