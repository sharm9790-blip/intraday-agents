"""Short side, PAPER TEST ONLY: no Telegram signal, no plan, nothing to act on.

On weak days (13 Nov: 156 of 234 stocks fell, only 13–31% above VWAP) the long-only
debate rightly found almost nothing, while stocks like TDPOWERSYS (−5.5% from 10:00),
SWIGGY, CAMS and HDFCAMC kept falling. Before any short signal is ever sent, this
measures whether a simple rule-based intraday short would have made money:

  record   each scanner slot on a weak market (mode weak, or < 35% of stocks above
           VWAP with ≥ 50 priced): take up to 2 new names from candidates.json "shorts"
           (max 3 a day, none after 14:30). Entry = next 5-min open, SL = 0.5 × ATR above
           max(VWAP, price) (≥ 0.7 × ATR away), T1 = 1.5R, T2 = 2.5R, qty for ₹1,000 risk.
  replay   15:45, on the day's 5-min candles with the same management as the long
           paper trades (half at T1, rest SL to cost, T2, 15:00 time stop, costs),
           mirrored for a short. History in state/shorts.json.

Shown as one paper section in the scorecard and in the expectancy card; it becomes a
real signal only if it earns it over ≥ 30 paper trades AND the user decides so.
"""
import math
import re
import os
from datetime import timedelta

import replay

PER_SLOT = 2
PER_DAY = 3
LAST = "14:30"
RISK = 1000
CAPITAL = 100000


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def weak(cand):
    above = cand.get("above_vwap_pct")
    return (cand.get("scanned") or 0) >= 50 and (cand.get("mode") == "weak" or (above is not None and above < 35))


def levels(x):
    """x: a candidates.json "shorts" item. Returns the paper trade or None."""
    price, vwap, atr = x["price"], x.get("vwap") or x["price"], x.get("atr5") or 0
    if not atr:
        return None
    sl = round(max(max(vwap, price) + 0.5 * atr, price + 0.7 * atr, price * 1.004), 2)
    r = sl - price
    qty = min(math.floor(RISK / r), math.floor(CAPITAL / price)) if r > 0 else 0
    if qty <= 0:
        return None
    return {"symbol": x["symbol"], "yahoo": x["yahoo"], "price": price, "sl": sl,
            "t1": round(price - 1.5 * r, 2), "t2": round(price - 2.5 * r, 2), "qty": qty,
            "sector": x.get("sector", "")}


def record(doc, cand, now, today):
    """Add new paper shorts from this scan. doc = state/shorts.json. Returns True if changed."""
    if doc.get("date") != today:
        doc["date"], doc["open"] = today, []
    if not weak(cand) or now.hour * 60 + now.minute > _hm(LAST) or len(doc["open"]) >= PER_DAY:
        return False
    have = {t["symbol"] for t in doc["open"]}
    added = 0
    for x in cand.get("shorts") or []:
        if x["symbol"] in have or added >= PER_SLOT or len(doc["open"]) >= PER_DAY:
            continue
        t = levels(x)
        if t:
            doc["open"].append({**t, "at": now.strftime("%H:%M")})
            added += 1
    return bool(added)


def _flip(bars):
    """A short is a long on the negated price: high ↔ low swap."""
    return [(b[0], -b[1], -b[3], -b[2], -b[4], b[5]) for b in bars]


def replay_one(t, bars):
    s = _hm(t["at"])
    i0 = next((i for i, b in enumerate(bars) if b[0].hour * 60 + b[0].minute >= s + 5), None)
    if i0 is None:
        return {**t, "pnl": 0, "steps": ["no candles after the signal"]}
    entry = bars[i0][1]
    p = {"qty": t["qty"], "sl": -t["sl"], "t1": -t["t1"], "t2": -t["t2"], "time_stop": "15:00"}
    if entry >= t["sl"]:
        return {**t, "entry": entry, "pnl": 0, "steps": ["opened above SL, skipped"]}
    pnl, steps = replay.manage(_flip(bars), i0, -entry, p)
    steps = [re.sub(r"-(\d)", r"\1", st) for st in steps]   # prices back to positive
    return {**t, "entry": round(entry, 2), "pnl": pnl,
            "steps": [f"{bars[i0][0]:%H:%M} sold short {entry:.2f}"] + steps}


def close_day(doc, today, candles):
    """15:45: replay today's paper shorts, move them to history. Returns today's results."""
    if doc.get("date") != today or not doc.get("open"):
        return doc.setdefault("days", {}).get(today, [])
    res = []
    for t in doc["open"]:
        try:
            bars, _ = candles(t["yahoo"])
            res.append(replay_one(t, bars))
        except Exception as e:
            print("short replay failed", t["symbol"], e)
    doc.setdefault("days", {})[today] = res
    doc["open"] = []
    return res


def history(doc):
    return [{"date": d, "symbol": t["symbol"], "pnl": t["pnl"],
             "risk": round(t["qty"] * (t["sl"] - t.get("entry", t["price"]))) or None}
            for d in sorted(doc.get("days", {})) for t in doc["days"][d] if "entry" in t]


def lines(doc, today, res, inr):
    hist = history(doc)
    if not res and not hist:
        return []
    out = ["<b>🔻 Short side · paper test only</b> (not a signal)"]
    for t in res:
        out.append(f"{t['symbol']} {t['at']} · {inr(t['pnl'])} · " + " → ".join(t["steps"][-2:]))
    if not res:
        out.append("No weak-market short setups today.")
    tot = sum(h["pnl"] for h in hist)
    out.append(f"So far {len(hist)}/30 paper shorts · total {inr(tot)}"
               + (f" · {inr(round(tot / len(hist)))}/trade" if hist else ""))
    return out
