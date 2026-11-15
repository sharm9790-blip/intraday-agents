"""Opening momentum engine (code only, no AI delay): catch the 09:15–09:45 movers early.

Problem (user, 15 Nov): the first live scan is 09:40 and the AI debate adds 10–15 min, so
plans arrive ~09:55 when the day's gainers are already +5%; buying there, the 11–12
selling leaves a loss. This finds them at +1% to +3.5% instead, every 5 min 09:20–09:40:

  gap        open ≤ +2.5% over yesterday's close (bigger gaps tend to fade), ≥ −1%
  move       now +1.0% … +3.5% on the day
  breakout   last 5-min candle green and closed at a new high for the day
  strength   price above VWAP; volume so far ≥ 20% of a normal FULL day (heavy early buying)
  market     Nifty not below −0.3%
  plan       entry = next 5-min open; SL = higher of today's low and VWAP − 0.3%, but at
             least 0.4% of price below entry (and skipped if more than 1.5%);
             T1 = +1.5R (half), T2 = +3R; the rest trails 1 × 5-min ATR under the high close.
  exit       not at T1 by 11:15 → out (the 11–12 selling); everything out by 15:00.
  limits     2 a day, one per sector group, first signal per stock only.

PAPER TEST ONLY for now (state/opening.json, scorecard section). Results in R (1R = the
plan's risk; at the usual ₹1,000 risk per trade, +1R = +₹1,000 before costs).
"""
from datetime import timedelta

GAP_MAX = 2.5
GAP_MIN = -1.0
PCT_MIN, PCT_MAX = 1.0, 3.5
VOL_FRAC = 0.20
NIFTY_MIN = -0.3
SLOTS = ("09:20", "09:25", "09:30", "09:35", "09:40")
EARLY_EXIT = "11:15"
EXIT_BY = "15:00"
MIN_RISK_PCT, MAX_RISK_PCT = 0.4, 1.5
PER_DAY = 2
COST = 0.0012   # round trip incl. slippage (same as replay.py)


def _m(t):
    return t.hour * 60 + t.minute


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def today_bars(bars, now):
    return [b for b in bars if b[0].date() == now.date() and _m(b[0]) >= 555
            and b[0] + timedelta(minutes=5) <= now]


def check(bars, now, avg_vol, nifty_pct):
    """One stock at one slot. bars: several days of 5-min candles. Returns signal dict or None."""
    tb = today_bars(bars, now)
    prev = [b for b in bars if b[0].date() < now.date()]
    if len(tb) < 1 or not prev or not avg_vol or nifty_pct is None or nifty_pct < NIFTY_MIN:
        return None
    pc = prev[-1][4]
    gap = (tb[0][1] / pc - 1) * 100
    last = tb[-1]
    pct = (last[4] / pc - 1) * 100
    vol = sum(b[5] for b in tb)
    vwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in tb) / vol if vol else None
    hi_before = max([b[2] for b in tb[:-1]] or [tb[0][1]])
    if not (GAP_MIN <= gap <= GAP_MAX and PCT_MIN <= pct <= PCT_MAX):
        return None
    if not (vwap and last[4] > vwap and last[4] > last[1] and last[4] >= hi_before):
        return None
    vf = vol / avg_vol
    if vf < VOL_FRAC:
        return None
    return {"price": last[4], "pct": round(pct, 2), "gap": round(gap, 2), "vwap": round(vwap, 2),
            "vol_frac": round(vf, 2), "day_low": min(b[3] for b in tb),
            "atr5": sum(b[2] - b[3] for b in tb[-6:]) / len(tb[-6:]),
            "score": round(vf * 2 + pct - abs(gap) * 0.5, 2)}


def plan(sig, entry):
    sl = max(sig["day_low"], sig["vwap"] * 0.997)
    risk = max(entry - sl, entry * MIN_RISK_PCT / 100, 0.7 * sig["atr5"])
    if risk > entry * MAX_RISK_PCT / 100:
        return None
    return {"entry": round(entry, 2), "sl": round(entry - risk, 2), "t1": round(entry + 1.5 * risk, 2),
            "t2": round(entry + 3 * risk, 2), "risk": round(risk, 2), "atr5": sig["atr5"]}


def manage(bars, i0, p, early_exit=EARLY_EXIT, trail=True):
    """Bars from i0 (entry candle, bought at its open). Returns (R multiple after costs, steps)."""
    e, r = p["entry"], p["risk"]
    sl, half_done, hc = p["sl"], False, 0.0
    got = 0.0          # R already booked on the first half
    steps = []
    for b in bars[i0:]:
        t = _m(b[0])
        if t >= _hm(EXIT_BY) or (not half_done and early_exit and t >= _hm(early_exit)):
            res = (b[1] - e) / r
            steps.append(f"{b[0]:%H:%M} {'time stop' if half_done else 'early exit'} {b[1]:.2f}")
            return round((got + res * (0.5 if half_done else 1)) - COST * e / r, 2), steps
        if b[3] <= sl:
            res = (sl - e) / r
            steps.append(f"{b[0]:%H:%M} " + ("SL" if not half_done else "trail stop" if sl > e else "back at cost"))
            return round((got + res * (0.5 if half_done else 1)) - COST * e / r, 2), steps
        if not half_done and b[2] >= p["t1"]:
            half_done, got, sl = True, 0.75, e      # half at +1.5R = +0.75R, SL to cost
            steps.append(f"{b[0]:%H:%M} T1")
        if half_done:
            if b[2] >= p["t2"]:
                steps.append(f"{b[0]:%H:%M} T2")
                return round(got + 1.5 - COST * e / r, 2), steps
            if trail:
                hc = max(hc, b[4])
                sl = max(sl, hc - p["atr5"])
    b = bars[-1]
    res = (b[4] - e) / r
    steps.append(f"close {b[4]:.2f}")
    return round((got + res * (0.5 if half_done else 1)) - COST * e / r, 2), steps


def day_signals(day, data, avg_vol, nifty, groups):
    """Backtest / replay one day. data: {sym: bars over several days}; nifty: Nifty bars.
    Returns the (max PER_DAY) paper trades with results."""
    import datetime as dt
    taken, used_groups, out = set(), set(), []
    for slot in SLOTS:
        h, m = map(int, slot.split(":"))
        tz = next(iter(data.values()))[0][0].tzinfo
        now = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
        nb = today_bars(nifty, now)
        npv = [b for b in nifty if b[0].date() < day]
        npct = (nb[-1][4] / npv[-1][4] - 1) * 100 if nb and npv else None
        cands = []
        for s, bars in data.items():
            if s in taken:
                continue
            sig = check(bars, now, avg_vol.get(s), npct)
            if sig:
                cands.append((sig["score"], s, sig))
        for _, s, sig in sorted(cands, reverse=True):
            if len(out) >= PER_DAY:
                break
            g = groups.get(s)
            if g and g in used_groups:
                continue
            day_b = [b for b in data[s] if b[0].date() == day]
            i0 = next((i for i, b in enumerate(day_b) if b[0] >= now), None)
            if i0 is None:
                continue
            p = plan(sig, day_b[i0][1])
            if not p:
                continue
            res, steps = manage(day_b, i0, p)
            res15, _ = manage(day_b, i0, p, early_exit=None)
            out.append({"symbol": s, "at": slot, **{k: sig[k] for k in ("pct", "gap", "vol_frac")}, **p,
                        "r": res, "r_no_early_exit": res15, "steps": steps,
                        "day_high_after": round(max(b[2] for b in day_b[i0:]), 2)})
            taken.add(s)
            if g:
                used_groups.add(g)
        if len(out) >= PER_DAY:
            break
    return out


# ---------- live paper test (watcher) ----------
def live_slot(now, universe, avg_vol, candles, quotes=None, nifty_pct=None, taken=(), used_groups=(),
              groups=None, room=PER_DAY):
    """One 09:20–09:40 slot, live. quotes (INDmoney full quotes) pre-filter the universe to
    stocks already +1…+3.5% with a small gap, so only those need 5-min candles."""
    groups = groups or {}
    syms = [s for s in universe if s not in taken and avg_vol.get(s)]
    if quotes:
        def pre(q):
            pc, ltp, op = q.get("prev_close"), q.get("ltp"), q.get("open")
            if not (pc and ltp and op):
                return False
            return PCT_MIN <= (ltp / pc - 1) * 100 <= PCT_MAX and GAP_MIN <= (op / pc - 1) * 100 <= GAP_MAX
        syms = [s for s in syms if s in quotes and pre(quotes[s])]
    out = []
    for s in syms:
        try:
            bars, _ = candles(s + ".NS", "5d")
        except Exception:
            continue
        sig = check(bars, now, avg_vol.get(s), nifty_pct)
        if sig:
            out.append((sig["score"], s, sig))
    picks = []
    for _, s, sig in sorted(out, reverse=True):
        if len(picks) >= room:
            break
        g = groups.get(s)
        if g and g in used_groups:
            continue
        p = plan(sig, sig["price"])
        if p:
            picks.append({"symbol": s, "yahoo": s + ".NS", "at": now.strftime("%H:%M"), "group": g,
                          **{k: sig[k] for k in ("pct", "gap", "vol_frac")}, **p})
    return picks


def replay(t, bars):
    """15:45: the paper trade bought at the open of the first 5-min candle after the signal."""
    d = bars[-1][0].date() if bars else None
    day = [b for b in bars if b[0].date() == d and b[0].strftime("%H:%M") >= "09:15"]
    i0 = next((i for i, b in enumerate(day) if b[0].strftime("%H:%M") >= t["at"]), None)
    if i0 is None:
        return {**t, "r": 0.0, "steps": ["no candles after the signal"]}
    p = {**t, "entry": round(day[i0][1], 2), "sl": round(day[i0][1] - t["risk"], 2),
         "t1": round(day[i0][1] + 1.5 * t["risk"], 2), "t2": round(day[i0][1] + 3 * t["risk"], 2)}
    r, steps = manage(day, i0, p)
    return {**t, "entry": p["entry"], "r": r, "steps": [f"{day[i0][0]:%H:%M} bought {day[i0][1]:.2f}"] + steps}


def lines(doc, res, inr):
    hist = [x for d in sorted(doc.get("days", {})) for x in doc["days"][d]]
    if not res and not hist:
        return []
    import tables as T
    out = ["<b>👀 Opening engine · paper test</b> (not a signal)"]
    if res:
        out.append(T.table(["Stock", "Time", "R", "₹*", "Exit"],
                           [[T.short(t["symbol"], 9), t["at"], T.r(t["r"]), T.num(t["r"] * 1000),
                             (t["steps"][-1].split(" ", 1)[1] if " " in t["steps"][-1] else t["steps"][-1])[:10]]
                            for t in res], right={2, 3}))
        out.append("<i>*at ₹1,000 risk per trade</i>")
    else:
        out.append("No opening signals today.")
    if hist:
        tot = sum(x["r"] for x in hist)
        out.append(f"So far {len(hist)} paper trades · {tot:+.1f}R ({inr(tot * 1000)}) · "
                   f"win {100 * sum(1 for x in hist if x['r'] > 0) // len(hist)}%")
    return out
