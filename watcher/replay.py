"""Replay a day's plans on 5-min candles: what each would have made.

Three ways of taking each plan, all managed the same way afterwards
(SL first if SL and target are in the same candle, 50% at T1 then SL to
cost, rest at T2, everything out at 15:00 (before the broker's auto square-off), ~0.12% round trip =
costs ~0.08% (brokerage, STT, exchange, GST, stamp) + ~0.04% slippage; 0.06% until 12 Nov 2026):

  rules  - the plan exactly as given (buy-trigger conditions and cancel rules)
  limit  - a plain limit buy at the entry price, no other conditions, until the deadline
  market - bought at market when the plan was given (chasing)
  lock   - same as market, plus one SL step: once price is halfway from T1 to T2,
           the SL on the rest moves up to T1 (tracked to see if it beats "market")
  trail  - plan rules entry, but after T1 the rest has no T2: its SL trails 1 × the
           5-min ATR under the highest close (tracked to see if winners should run)
"""
COST = 0.0012
EXIT_BY = "15:00"


def _m(b):
    return b[0].hour * 60 + b[0].minute


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _atr(bars, i0, p):
    if p.get("atr5"):
        return p["atr5"]
    prev = bars[max(0, i0 - 12):i0] or bars[i0:i0 + 1]
    return sum(b[2] - b[3] for b in prev) / len(prev) if prev else 0


def manage(bars, i0, entry, p, lock=False, trail=False):
    q = p["qty"]
    half = q // 2
    sl = p["sl"]
    left = q
    pnl = 0.0
    steps = []
    atr = _atr(bars, i0, p) if trail else 0
    hc = 0.0
    for b in bars[i0:]:
        if _m(b) >= min(_hm(p.get("time_stop", EXIT_BY)), _hm(EXIT_BY)):
            pnl += left * (b[1] - entry)
            steps.append(f"{b[0]:%H:%M} time stop {b[1]:.2f}")
            left = 0
            break
        if b[3] <= sl:
            pnl += left * (sl - entry)
            steps.append(f"{b[0]:%H:%M} " + ("SL" if sl < entry else "back at cost" if sl == entry else "locked T1"))
            left = 0
            break
        if left == q and b[2] >= p["t1"]:
            pnl += half * (p["t1"] - entry)
            left -= half
            sl = entry
            steps.append(f"{b[0]:%H:%M} T1")
        if trail and left < q:
            hc = max(hc, b[4])
            if atr and hc - atr > sl:
                sl = round(hc - atr, 2)
            continue
        if left < q and b[2] >= p["t2"]:
            pnl += left * (p["t2"] - entry)
            steps.append(f"{b[0]:%H:%M} T2")
            left = 0
            break
        if lock and left < q and sl < p["t1"] and b[2] >= (p["t1"] + p["t2"]) / 2:
            sl = p["t1"]
            steps.append(f"{b[0]:%H:%M} SL to T1")
    if left:
        pnl += left * (bars[-1][4] - entry)
        steps.append(f"close {bars[-1][4]:.2f}")
    return round(pnl - COST * abs(entry) * q), steps


def targets(p, bars, cash=100000):
    """Your sheet: cash ÷ entry = qty (whole shares), 50% sold at T1, rest at T2,
    before costs. Plus whether price really reached entry / T1 / T2 after the plan."""
    s, d = _hm(p["start"]), _hm(p["deadline"])
    e, t1, t2 = p["entry"], p["t1"], p["t2"]
    qty = int(cash // e)
    half = qty // 2
    after = [b for b in bars if s <= _m(b) < _hm(EXIT_BY)]
    hi = max((b[2] for b in after), default=0)
    filled = any(b[3] <= e for b in after if _m(b) < d)
    return {
        "cash": cash, "qty": qty, "half": half, "rest": qty - half,
        "pf1": round(half * (t1 - e)), "pf2": round((qty - half) * (t2 - e)),
        "total": round(half * (t1 - e) + (qty - half) * (t2 - e)),
        "t1_hit": hi >= t1, "t2_hit": hi >= t2, "entry_hit": filled, "high": round(hi, 2),
    }


def replay(p, bars):
    """p: a plan dict (plan.json format) plus "start" (HH:MM it was given)."""
    s, d = _hm(p["start"]), _hm(p["deadline"])
    res = {"symbol": p["symbol"], "start": p["start"], "rules_v": p.get("rules_v"),
           "sector": p.get("sector"), "entry": p["entry"], "qty": p["qty"],
           "sl": p["sl"], "t1": p["t1"], "t2": p["t2"], "close": round(bars[-1][4], 2)}
    rule = ("cancelled", "no trigger")
    buy_now = p.get("entry_type") == "now"
    for i, b in enumerate(bars):
        t = _m(b)
        if t < s:
            continue
        if t >= d:
            rule = ("cancelled", f"not triggered by {p['deadline']}")
            break
        if buy_now and p["zone_low"] <= b[1] <= p["max_entry"]:
            rule = ("bought", i, b[1])          # bought at this candle's open
            break
        if (not buy_now and b[3] <= p["zone_high"] and b[4] > b[1]
                and b[4] > p["trigger_close_above"] and b[4] <= p["max_entry"]):
            rule = ("bought", i + 1, p["entry"])
            break
        if b[2] >= p["cancel_if_high_reaches"]:
            rule = ("cancelled", "ran up without the dip")
            break
        if b[4] < p["cancel_if_close_below"]:
            rule = ("cancelled", "closed below cancel level")
            break
    if rule[0] == "bought" and rule[1] < len(bars):
        pnl, steps = manage(bars, rule[1], rule[2], p)
        res["rules"] = {"pnl": pnl, "steps": ([f"bought now {rule[2]:.2f}"] if buy_now else []) + steps,
                        "bought": True}
        pnl, steps = manage(bars, rule[1], rule[2], p, trail=True)
        res["trail"] = {"pnl": pnl, "steps": steps}
    else:
        res["rules"] = {"pnl": 0, "steps": [rule[1] if rule[0] == "cancelled" else "no fill"]}
        res["trail"] = {"pnl": 0, "steps": res["rules"]["steps"]}
    fill = next((i for i, b in enumerate(bars) if s <= _m(b) < d and b[3] <= p["entry"]), None)
    if fill is None:
        res["limit"] = {"pnl": 0, "steps": ["never filled"]}
    else:
        pnl, steps = manage(bars, fill, p["entry"], p)
        res["limit"] = {"pnl": pnl, "steps": [f"{bars[fill][0]:%H:%M} filled"] + steps}
    i0 = next((i for i, b in enumerate(bars) if _m(b) >= s), None)
    res["price_at_plan"] = round(bars[i0][1], 2) if i0 is not None else None
    res["targets"] = targets(p, bars)
    if i0 is None:
        res["market"] = {"pnl": 0, "steps": ["no data"]}
    else:
        pnl, steps = manage(bars, i0, bars[i0][1], p)
        res["market"] = {"pnl": pnl, "steps": [f"bought {bars[i0][1]:.2f}"] + steps}
        pnl, steps = manage(bars, i0, bars[i0][1], p, lock=True)
        res["lock"] = {"pnl": pnl, "steps": [f"bought {bars[i0][1]:.2f}"] + steps}
    return res
