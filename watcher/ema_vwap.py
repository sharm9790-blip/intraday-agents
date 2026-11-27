"""EMA + VWAP trend-pullback engine on NIFTY (code only), PAPER TEST ONLY.

Nifty only, no stocks. The Nifty index has no volume, so VWAP and the volume check use
NIFTYBEES 5-min candles (the Nifty 50 ETF, it moves with the index); levels in the Telegram
card are converted to Nifty points with the live Nifty / NIFTYBEES ratio.

Checked every 5 min 09:45–14:00:
  LONG   trend      EMA 9 > EMA 21, both rising, price above VWAP
         pullback   the last candle dipped to EMA 9 (low within 0.05% of it, or below)
                    but closed back above EMA 9 and green
         volume     last candle volume ≥ 1.2 × today's average 5-min volume
         not late   Nifty +0.1% … +2% on the day
  SHORT  the mirror image (EMA 9 < EMA 21, both falling, below VWAP, bounce to EMA 9 that
         closes back below it red, −0.1% … −2% on the day).

  plan   entry = next 5-min open; SL = beyond the pullback candle and VWAP (+0.03%),
         risk 0.1% … 0.6% of price (≈ 25–150 Nifty points; skip if wider);
         T1 = 1.5R (half booked, SL to cost); the rest exits on a 5-min close back across
         EMA 21, at T2 = 3R, or at 15:10.
  limits 2 trades a day, never two open at once.

Results in R (1R = the plan's risk; at ₹1,000 risk per trade, +1R = +₹1,000), after
0.04% round-trip costs (Nifty futures / options-equivalent, incl. slippage).
Same code for the live paper test and the backtest.
"""
SLOTS = tuple(f"{h:02d}:{m:02d}" for h in range(9, 15) for m in range(0, 60, 5)
              if "09:45" <= f"{h:02d}:{m:02d}" <= "14:00")
FAST, SLOW = 9, 21
TOUCH = 0.0005
VOL_X = 1.2
PCT_MIN, PCT_MAX = 0.1, 2.0
MIN_RISK_PCT, MAX_RISK_PCT = 0.1, 0.6
EXIT_BY = "15:10"
PER_DAY = 2
COST = 0.0004
SHORTS = True
SYMBOL = "NIFTYBEES"


def _m(t):
    return t.hour * 60 + t.minute


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def ema(xs, n):
    out, k, e = [], 2 / (n + 1), None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def vwap_series(bars):
    out, pv, v = [], 0.0, 0.0
    for b in bars:
        pv += (b[2] + b[3] + b[4]) / 3 * (b[5] or 0)
        v += b[5] or 0
        out.append(pv / v if v else b[4])
    return out


def split(bars, now):
    """(previous days' bars, today's closed bars up to now)."""
    from datetime import timedelta
    prev = [b for b in bars if b[0].date() < now.date()]
    tb = [b for b in bars if b[0].date() == now.date() and _m(b[0]) >= 555
          and b[0] + timedelta(minutes=5) <= now]
    return prev, tb


def check(bars, now):
    """Signal at this slot or None. EMAs are warmed up on the previous days' bars so they
    are valid from the first candle; VWAP is today's."""
    prev, tb = split(bars, now)
    if len(tb) < 6 or not prev:
        return None
    pc = prev[-1][4]
    closes = [b[4] for b in prev[-60:]] + [b[4] for b in tb]
    e9, e21 = ema(closes, FAST)[-len(tb):], ema(closes, SLOW)[-len(tb):]
    vw = vwap_series(tb)
    last = tb[-1]
    pct = (last[4] / pc - 1) * 100
    vols = [b[5] or 0 for b in tb]
    avg = sum(vols) / len(vols)
    if not avg or vols[-1] < VOL_X * avg:
        return None
    up = e9[-1] > e21[-1] and e9[-1] > e9[-3] and e21[-1] > e21[-3] and last[4] > vw[-1]
    dn = e9[-1] < e21[-1] and e9[-1] < e9[-3] and e21[-1] < e21[-3] and last[4] < vw[-1]
    side = None
    if up and PCT_MIN <= pct <= PCT_MAX:
        if last[3] <= e9[-1] * (1 + TOUCH) and last[4] > e9[-1] and last[4] > last[1]:
            side = 1
    elif SHORTS and dn and -PCT_MAX <= pct <= -PCT_MIN:
        if last[2] >= e9[-1] * (1 - TOUCH) and last[4] < e9[-1] and last[4] < last[1]:
            side = -1
    if not side:
        return None
    return {"side": side, "price": last[4], "pct": round(pct, 2), "vwap": round(vw[-1], 3),
            "bar_low": last[3], "bar_high": last[2], "vol_x": round(vols[-1] / avg, 2)}


def plan(sig, entry):
    s = sig["side"]
    if s > 0:
        stop = min(sig["bar_low"], sig["vwap"]) * (1 - 0.0003)
        risk = max(entry - stop, entry * MIN_RISK_PCT / 100)
    else:
        stop = max(sig["bar_high"], sig["vwap"]) * (1 + 0.0003)
        risk = max(stop - entry, entry * MIN_RISK_PCT / 100)
    if risk <= 0 or risk > entry * MAX_RISK_PCT / 100:
        return None
    return {"side": s, "entry": round(entry, 3), "sl": round(entry - s * risk, 3),
            "t1": round(entry + s * 1.5 * risk, 3), "t2": round(entry + s * 3 * risk, 3),
            "risk": round(risk, 3), "risk_pct": round(risk / entry * 100, 3)}


def manage(day_bars, i0, p, warm=()):
    """day_bars: today's bars; entered at the open of day_bars[i0]. warm: earlier closes for
    the EMA 21 trail. Returns (R after costs, steps, exit index or None if still open)."""
    s, e, r = p["side"], p["entry"], p["risk"]
    sl, half, got, steps = p["sl"], False, 0.0, []
    closes = list(warm) + [b[4] for b in day_bars]
    e21 = ema(closes, SLOW)[len(warm):]
    cost = COST * e / r

    def done(x, why, i):
        steps.append(f"{day_bars[i][0]:%H:%M} {why}")
        return round(got + x * (0.5 if half else 1) - cost, 2), steps, i

    for i in range(i0, len(day_bars)):
        b = day_bars[i]
        if _m(b[0]) >= _hm(EXIT_BY):
            return done(s * (b[1] - e) / r, "time exit", i)
        if ((b[3] if s > 0 else b[2]) - sl) * s <= 0:
            return done(s * (sl - e) / r, "SL" if not half else "back at cost", i)
        fav = b[2] if s > 0 else b[3]
        if not half and (fav - p["t1"]) * s >= 0:
            half, got, sl = True, 0.75, e
            steps.append(f"{b[0]:%H:%M} T1")
        if half:
            if (fav - p["t2"]) * s >= 0:
                steps.append(f"{b[0]:%H:%M} T2")
                return round(got + 1.5 - cost, 2), steps, i
            if (b[4] - e21[i]) * s < 0:      # 5-min close back across EMA 21
                return done(s * (b[4] - e) / r, "EMA21 exit", i)
    b = day_bars[-1]
    steps.append(f"open {b[4]:.2f}")
    return round(got + s * (b[4] - e) / r * (0.5 if half else 1) - cost, 2), steps, None


def run_day(bars, day, until=None):
    """All of a day's trades, in order, never two open at once, max PER_DAY.
    bars: several days of 5-min candles. until: live mode, only candles closed by then."""
    import datetime as dt
    tz = bars[-1][0].tzinfo
    day_b = [b for b in bars if b[0].date() == day and _m(b[0]) >= 555
             and (until is None or b[0] + dt.timedelta(minutes=5) <= until)]
    warm = [b[4] for b in bars if b[0].date() < day][-60:]
    out, free_at = [], None          # free_at: time the last trade closed; "open" if still on
    for slot in SLOTS:
        h, m = map(int, slot.split(":"))
        now = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
        if (until and now > until) or len(out) >= PER_DAY or free_at == "open":
            break
        if free_at is not None and now <= free_at:
            continue
        sig = check(bars, now)
        if not sig:
            continue
        i0 = next((i for i, b in enumerate(day_b) if b[0] >= now), None)
        if i0 is None:                                   # live: entry candle not closed yet
            p = plan(sig, sig["price"])
            if p:
                out.append({"at": slot, "side": p["side"], "pct": sig["pct"], "vol_x": sig["vol_x"], **p,
                            "r": 0.0, "steps": ["entry at next open"], "closed": False})
            break
        p = plan(sig, day_b[i0][1])
        if not p:
            continue
        res, steps, xi = manage(day_b, i0, p, warm)
        free_at = day_b[xi][0] if xi is not None else "open"
        out.append({"at": slot, "side": p["side"], "pct": sig["pct"], "vol_x": sig["vol_x"], **p,
                    "r": res, "steps": steps, "closed": xi is not None})
    return out


def lines(doc, res, inr):
    hist = [x for d in sorted(doc.get("days", {})) for x in doc["days"][d]]
    if not res and not hist:
        return []
    import tables as T
    out = ["<b>📈 Nifty EMA + VWAP · paper test</b> (not a signal)"]
    if res:
        out.append(T.table(["Time", "L/S", "R", "Pts", "₹*"],
                           [[t["at"], "L" if t["side"] > 0 else "S", T.r(t["r"]),
                             T.num(t["r"] * t.get("risk_pts", 0)), T.num(t["r"] * 1000)] for t in res]))
        out.append("<i>*at ₹1,000 risk per trade · Pts = Nifty points</i>")
    else:
        out.append("No Nifty EMA + VWAP signals today.")
    if hist:
        tot = sum(x["r"] for x in hist)
        out.append(f"So far {len(hist)} paper trades · {tot:+.1f}R ({inr(tot * 1000)}) · "
                   f"win {100 * sum(1 for x in hist if x['r'] > 0) // len(hist)}%")
    return out
