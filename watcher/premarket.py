"""Premarket watchlist: built by the cloud before the open, triggered by code after it.

The cloud routine runs twice before the market opens (fired by watch.py):
  ~08:45 PREMARKET  overnight cues + yesterday's charts → 3–5 stocks with buy-above levels
  ~09:08 PREOPEN    pre-open gap check (drop gaps > 4%) → final list
and writes state/premarket.json + state/premarket_msg.txt (the Telegram watchlist).

From 09:20 to 09:40 the watcher checks each kept stock's 5-min candles (Yahoo):
a green candle closing above `buy_above` (but not above `max_entry`) on volume at least
VOL_X × that candle's usual volume → it becomes a live BUY NOW plan (risk-checked).

    python3 watcher/premarket.py state/premarket.json --write
        cloud step: risk-check every stock at its planned entry (just above buy_above), fix qty,
        set keep=false with the reason on a BLOCK. Exit 0 if any stock is kept.
"""
import json
import sys
from datetime import timedelta

import risk

MAX_GAP = 4.0      # % gap-up above which the stock is dropped (fade risk)
VOL_X = 1.5        # first-candle volume vs the same candle on earlier days
START, END = "09:20", "09:40"  # window in which a breakout counts


def _hm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def _bm(b):
    return b[0].hour * 60 + b[0].minute


def evaluate(stock, bars, now):
    """bars: 5-min (time, o, h, l, c, v) over the last few days (Yahoo range=5d).
    Returns ("buy", info) | ("drop", reason) | ("wait", None)."""
    today = now.date()
    mins = now.hour * 60 + now.minute
    tb = [b for b in bars if b[0].date() == today and _bm(b) >= _hm("09:15")]
    closed = [b for b in tb if b[0] + timedelta(minutes=5) <= now]
    prev = [b for b in bars if b[0].date() < today]
    if not closed:
        return ("drop", "no price data by 09:40") if mins >= _hm(END) else ("wait", None)
    prev_close = prev[-1][4] if prev else None
    gap = (tb[0][1] / prev_close - 1) * 100 if prev_close else None
    if gap is not None and gap > MAX_GAP:
        return "drop", f"gap-up {gap:.1f}% > {MAX_GAP:g}% (fade risk)"
    b = closed[-1]
    if b[4] < stock["sl"]:
        return "drop", f"fell below SL {stock['sl']:g}"
    if b[4] > stock["max_entry"] or tb[0][1] > stock["max_entry"]:
        return "drop", f"ran past max entry {stock['max_entry']:g}, don't chase"
    same = [x[5] for x in prev if x[0].time() == b[0].time() and x[5]]
    avg = sum(same) / len(same) if same else None
    vol_x = b[5] / avg if avg else None
    if b[4] > stock["buy_above"] and b[4] > b[1] and (vol_x is None or vol_x >= VOL_X):
        return "buy", {"close": b[4], "at": f"{b[0] + timedelta(minutes=5):%H:%M}",
                       "vol_x": round(vol_x, 1) if vol_x else None,
                       "gap": round(gap, 1) if gap is not None else None}
    if mins >= _hm(END):
        return "drop", f"no breakout above {stock['buy_above']:g} by {END}"
    return "wait", None


def to_plan(stock, close, t):
    """The live BUY NOW plan for a stock that broke out at `close` at time t (HH:MM)."""
    entry = round(min(max(close, stock.get("entry", close)), stock["max_entry"]), 2)
    dl = _hm(t) + 15
    keep = {k: v for k, v in stock.items() if k not in ("keep", "why", "preopen", "gap_pct")}
    return {**keep, "side": "long", "entry_type": "now", "entry": entry,
            "zone_low": stock["buy_above"], "zone_high": stock["max_entry"],
            "trigger_close_above": stock["buy_above"],
            "deadline": max(stock.get("deadline", "09:45"), f"{dl // 60:02d}:{dl % 60:02d}"),
            "cancel_if_high_reaches": stock.get("cancel_if_high_reaches", stock["t1"]),
            "cancel_if_close_below": stock.get("cancel_if_close_below", stock["sl"]),
            "exit_if_close_below_after_entry": stock.get("exit_if_close_below_after_entry", stock["sl"]),
            "time_stop": stock.get("time_stop", "15:00"), "source": "premarket"}


def main(path, write=False):
    with open(path) as f:
        doc = json.load(f)
    kept = 0
    for s in doc.get("stocks") or []:
        if s.get("keep") is False:
            continue
        # Checked at the planned entry (just above the trigger), not the worst case: at
        # max_entry CDSL failed on 13 Nov by 0.06 R. The 09:20 breakout is risk-checked
        # again at its real close before any BUY NOW goes out.
        plan = {**s, "entry": s.get("entry", s["buy_above"]), "zone_low": s["buy_above"], "deadline": "09:45"}
        res = risk.check(plan, "09:15")
        print(s["symbol"], json.dumps(res))
        if res["status"] == "BLOCK":
            s["keep"] = False
            s["why"] = "risk check: " + "; ".join(res["reasons"])
        else:
            s["keep"] = True
            s["qty"] = res["qty"]
            kept += 1
    if write:
        with open(path, "w") as f:
            json.dump(doc, f, indent=1)
    return 0 if kept else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], "--write" in sys.argv))
