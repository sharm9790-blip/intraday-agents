---
name: nifty-options-debate
description: PAPER-trade Nifty 50 weekly options (buy CE / buy PE only) with a Bull → Bear → Judge debate, code risk checks and a paper log in state/. Use for "/nifty-options-debate", "run the nifty debate", "nifty options paper trade", "nifty paper report".
---

# Nifty options debate — PAPER MODE

This is a **paper test**. No real orders, no draft orders, no "place this" wording. Its only job is
to build an honest record (`state/nifty_paper.json`) to see if Nifty option buying beats the stock
routine **after costs** over 3–4 weeks. It runs beside `intraday-debate` and never touches its
files (`plan.json`, `watch.json`, `rejected.json` …).

Arguments (optional): `report` → only step 3 + the report message; `PREMARKET` → premarket mode.

## Steps
1. **Branch.** `git fetch origin claude/intraday-state && git checkout claude/intraday-state && git pull --rebase`.
2. **Clock.** `TZ=Asia/Kolkata date`; holidays = `NSE_HOLIDAYS` in `watcher/watch.py`. Not a trading day → stop.
   - 08:30–09:10 or text `PREMARKET` → **Premarket mode** (below), then stop.
   - 09:30–14:30 → the debate, steps 3–7.
   - 15:05–15:40 → step 3 only, then the **day report** (below).
   - Any other time → stop.
3. **Settle paper trades first.** For every trade in `state/nifty_paper.json` with status `pending` or `open`:
   fetch that option's 5-min candles from the trade's `id` time to now with INDmoney
   `get_indian_stocks_ohlc` (its `ind_key`, interval 5minute), save them as
   `[["<ISO time +05:30>", o, h, l, c], ...]` to `/tmp/c_<id>.json` (closed candles only), and run
   `python3 watcher/nifty_paper.py replay state/nifty_paper.json <id> /tmp/c_<id>.json`.
   The code decides fill / SL / T1 / T2 / time stop — never decide it yourself. If candles can't be
   fetched, leave it and say so in the report.
4. **Gather** (the data the agents share — fetch once, pass to all three):
   - `lookup_ind_keys` for the Nifty 50 index and, with `filter_type` IN_STOCKS_FNO, the current
     weekly Nifty options. Read `lot_size` and `expiry` from the contract details; never assume them.
   - Nifty spot: `get_indian_stocks_ohlc` 5-min (today + yesterday) and 1-day (last 20 days);
     `get_indian_stocks_details` for LTP.
   - `get_indian_stocks_option_chain` for the nearest weekly expiry: PCR, max pain, IV, OI and OI
     change per strike, highest call OI (resistance) and put OI (support), greeks and bid/ask.
   - `state/cues.json` (India VIX, Asia, US, crude) and `state/pack.json` → market breadth if fresh.
   - Today's events (RBI, CPI, Fed, budget, big results) via InvestSights `get_market_digest` /
     `get_earnings_calendar` if available.
   - Paper record: `python3 watcher/nifty_paper.py report state/nifty_paper.json --days 20`.
5. **Bull → Bear → Judge.** Launch `nifty-bull-agent` (with the data above), then `nifty-bear-agent`
   with the Bull's output, then `nifty-judge-agent` with both plus the paper record. One after
   another, never in parallel. Tell the Judge whether a paper trade is already pending/open today.
6. **Write files** (IST, HHMM):
   - `reports/YYYY-MM-DD_HHMM_nifty.md` — all three outputs + the disclaimer.
   - `state/nifty_plan.json` — always overwrite:
     ```json
     {"date": "YYYY-MM-DD", "verdict_id": "YYYY-MM-DD_HHMM", "mode": "paper", "plan": null}
     ```
     or, on GO, `"plan"`:
     ```json
     {"side": "CE", "contract": "NIFTY 18 NOV 24500 CE", "ind_key": "<from lookup>",
      "strike": 24500, "expiry": "YYYY-MM-DD", "lot_size": <from contract>, "delta": 0.55,
      "spot_trigger": 24480, "spot_sl": 24420, "spot_t1": 24560, "spot_t2": 24620,
      "premium_entry": 140, "premium_max": 144, "premium_sl": 105, "premium_t1": 190,
      "premium_t2": 220, "deadline": "HH:MM", "confidence": 6, "why": "one line"}
     ```
     Numbers only. For a PE the spot levels run the other way (`spot_sl` > `spot_trigger` > `spot_t1`).
   - **Code check (mandatory on GO):** `python3 watcher/nifty_paper.py check state/nifty_plan.json --write`.
     Exit code 2 = BLOCK: the plan is now null; the message says "⛔ blocked by risk check" + reasons.
     On PASS use the script's `lots`, `qty`, `risk` and `rr` in the message — never the Judge's.
   - `state/nifty_verdict.txt` — Telegram HTML following
     `.claude/skills/nifty-options-debate/telegram-format.md` exactly. Every message starts with
     `🧪 <b>PAPER</b>`.
7. **Commit and push:** `git add -A && git commit -m "nifty YYYY-MM-DD_HHMM" && git push origin claude/intraday-state`
   (conflict: `git pull --rebase`, push again). The watcher forwards `nifty_verdict.txt` to Telegram.

## Premarket mode (~08:45)
No trade. Bull and Bear only (short): gap estimate from `state/cues.json`, India VIX, yesterday's
Nifty high/low/close, prior-day option chain walls (highest call / put OI), PCR, max pain, today's
events. Write `state/nifty_plan.json` with `"plan": null`, `"verdict_id"` `YYYY-MM-DD_HHMM_pre`, and
`state/nifty_verdict.txt` with the premarket template (levels to watch, bias, event warning).
Commit `nifty premarket …` and push.

## Day report (15:05–15:40)
After step 3, run `report --days 1` and `report --days 20`. Write `state/nifty_plan.json`
(`"plan": null`, `verdict_id` `YYYY-MM-DD_HHMM_eod`) and `state/nifty_verdict.txt` with the report
template: today's paper trades and result, then the 20-day record (trades, win rate, net P&L after
costs, profit factor). Commit `nifty eod …` and push.

## Rules
- Option **buying** only (CE or PE). No selling, no spreads, no futures. ATM or 1 strike ITM.
- Nifty 50 only, nearest weekly expiry; on expiry day itself → NO TRADE (code also blocks it).
- At most one GO per run, at most 2 paper trades per day; NO TRADE is the default.
- No order, portfolio, holdings, positions, family, networth, watchlist or save_analysis tools.
- Disclaimer (end of every report and message): `Paper trade for testing only, not SEBI-registered investment advice. Options trading is high risk.`
