---
name: positions-sync
description: Read the user's real intraday positions for today from the Groww connector (read-only) and save them to state/positions.json on branch claude/intraday-state, so the My Trade tracker can use the real quantity and average price. Used when the routine is fired with the SYNC_POSITIONS payload.
---

# Positions sync (cloud, read-only)

The user sent BOUGHT / SOLD to the Telegram bot. Get the real fills from Groww.

## Steps
1. `git fetch origin claude/intraday-state && git checkout claude/intraday-state && git pull --rebase`.
2. Load the Groww tool with ToolSearch (`get_my_trading_positions_today`) and call it once. If it is missing or fails, write `{"date": "<today>", "synced_at": "<HH:MM IST>", "error": "<short reason>", "positions": []}` and go to step 4.
3. Write `state/positions.json` (IST times, numbers only, no ₹):
   ```json
   {"date": "YYYY-MM-DD", "synced_at": "HH:MM",
    "positions": [{"symbol": "TATASTEEL", "product": "INTRADAY",
                   "buy_qty": 544, "buy_avg": 183.55,
                   "sell_qty": 272, "sell_avg": 185.9,
                   "net_qty": 272}]}
   ```
   One row per NSE stock in today's positions, intraday and delivery. Map Groww's own field names to these (buy quantity, average buy price, sell quantity, average sell price, net quantity). Leave out anything else, for example account ids and the P&L Groww reports.
4. `git add state/positions.json && git commit -m "positions sync HH:MM" && git push origin claude/intraday-state` (on conflict: `git pull --rebase`, push again).

## Rules
- **Read only.** Call no other Groww tool. Never place, modify or cancel an order. Never read holdings, orders, margin, family or portfolio data.
- Do not run the Bull / Bear / Judge debate in this mode.
- Finish with one line: how many positions were saved.
