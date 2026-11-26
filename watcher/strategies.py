"""Strategy lab: three more code-only PAPER engines next to the opening engine. No orders,
no AI, no buy/sell alerts: they only answer "would this rule have made money?".

  vwap     VWAP mean reversion (stocks, long and short). 10:00–14:00, a 5-min close
           ≥ 1.5% AND ≥ 2.5 × ATR away from VWAP, then a turning candle (green under
           VWAP / red over it). Entry next 5-min open, SL beyond the last 3 candles'
           extreme + 0.2 ATR (0.4–1.5% of price), target = VWAP at the signal, needs
           ≥ 1.2R. All out at 14:45. Not against a big Nifty day (|Nifty| ≥ 1%).
  gapfade  Gap fade (stocks). Gap of 3–8% at the open; 09:30–10:30 a 5-min close back
           through the 15-min opening range (below its low for a gap up / above its high
           for a gap down) and on the far side of VWAP. Entry next open, SL just past
           the opening range (0.5–2% of price), T1 = 1.5R (half, SL to cost), T2 = the
           gap fill (yesterday's close) capped at 3R. All out at 15:00.
  fly      Nifty iron fly (hedged option selling). 09:20–09:40, once a day: sell the ATM
           CE + PE of the nearest weekly expiry, buy wings WING points away. Tracked
           live on the INDmoney option chain every watcher run: out at −₹STOP or
           +₹TARGET (after costs), else at 15:15. "hold" also records what simply holding
           to 15:15 gave. Skipped on expiry day, event days, a VIX spike or a gap
           ≥ 1% in Nifty.

Event days (watcher/events.json + the premarket run's events): the fly sits out; the
stock engines still trade on paper but each trade is tagged, so the scorecard shows
normal days and event days apart.

Stock results in R (1R = planned risk) and ₹ at ₹1,000 risk a trade, after ~0.12% round
trip costs + slippage (same as opening.py / replay.py). Fly results in ₹ for 1 lot.
Nothing here is ever a signal unless the user decides so after ≥ 30 paper trades.
"""
import math
from datetime import date, timedelta

COST = 0.0012
PER_DAY = 2           # paper trades a day per stock engine

# VWAP reversion
V_FROM, V_TO, V_EXIT = "10:00", "14:00", "14:45"
V_STRETCH = 1.5       # % from VWAP
V_ATR = 2.5           # and ≥ this many 5-min ATRs
V_MIN_RISK, V_MAX_RISK = 0.4, 1.5
V_MIN_RR = 1.2
V_NIFTY_MAX = 1.0     # skip the side that fights a ≥ 1% Nifty day

# Gap fade
G_MIN, G_MAX = 3.0, 8.0
G_FROM, G_TO, G_EXIT = "09:30", "10:30", "15:00"
G_MIN_RISK, G_MAX_RISK = 0.5, 2.0

# Nifty iron fly
LOT = 65              # NIFTY lot size (NSE, from Jan 2026 contracts) — check if NSE changes it
STEP = 50
WING = 200            # points from ATM to each bought wing (4 strikes)
STOP = 3000           # ₹ per lot, after costs
TARGET = 2000         # ₹ per lot, after costs
F_FROM, F_TO, F_EXIT = "09:20", "09:40", "15:15"
F_GAP_MAX = 1.0       # % Nifty gap → no fly (trend days hurt short straddles)
F_VIX_SPIKE = 10.0    # % India VIX up on the day → no fly


def _m(t):
    return t.hour * 60 + t.minute


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _vwap(tb):
    v = sum(b[5] for b in tb)
    return sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in tb) / v if v else None


def _atr(tb, n=6):
    x = tb[-n:]
    return sum(b[2] - b[3] for b in x) / len(x) if x else 0


def _flip(bars):
    """A short is a long on the negated price (high ↔ low)."""
    return [(b[0], -b[1], -b[3], -b[2], -b[4], b[5]) for b in bars]


def manage(day, i0, side, entry, sl, t1, t2, exit_by, half=True):
    """Replay from candle i0 (bought / sold at its open = entry). SL checked first in each
    candle (conservative). half=True: half at T1, SL to cost, rest at T2; else all at T1.
    Returns (R after costs, steps)."""
    bars = _flip(day) if side == "short" else day
    k = -1 if side == "short" else 1
    e, sl, t1, t2 = k * entry, k * sl, k * t1, k * t2
    r = e - sl
    cost = COST * abs(entry) / r
    got, half_done, steps = 0.0, False, []
    for b in bars[i0:]:
        hm = f"{b[0]:%H:%M}"
        if _m(b[0]) >= _hm(exit_by):
            res = (b[1] - e) / r
            steps.append(f"{hm} time stop")
            return round(got + res * (0.5 if half_done else 1) - cost, 2), steps
        if b[3] <= sl:
            res = (sl - e) / r
            steps.append(f"{hm} " + ("SL" if not half_done else "back at cost"))
            return round(got + res * (0.5 if half_done else 1) - cost, 2), steps
        if not half_done and b[2] >= t1:
            if not half:
                steps.append(f"{hm} target")
                return round((t1 - e) / r - cost, 2), steps
            half_done, got, sl = True, 0.5 * (t1 - e) / r, e
            steps.append(f"{hm} T1")
            if b[2] >= t2:
                steps.append(f"{hm} T2")
                return round(got + 0.5 * (t2 - e) / r - cost, 2), steps
            continue
        if half_done and b[2] >= t2:
            steps.append(f"{hm} T2")
            return round(got + 0.5 * (t2 - e) / r - cost, 2), steps
    b = bars[-1]
    steps.append("close")
    return round(got + (b[4] - e) / r * (0.5 if half_done else 1) - cost, 2), steps


# ---------- signals (only candles up to the signal candle's close are used) ----------
def vwap_signal(tb, nifty_pct):
    """tb: today's candles up to and including the signal candle."""
    if len(tb) < 4 or nifty_pct is None:
        return None
    vw, atr, b, pb = _vwap(tb), _atr(tb), tb[-1], tb[-2]
    if not vw or not atr:
        return None
    c = b[4]
    dev = (c / vw - 1) * 100
    if dev <= -V_STRETCH and vw - c >= V_ATR * atr and c > b[1] and c > pb[4] and nifty_pct > -V_NIFTY_MAX:
        side, ext = "long", min(x[3] for x in tb[-3:]) - 0.2 * atr
    elif dev >= V_STRETCH and c - vw >= V_ATR * atr and c < b[1] and c < pb[4] and nifty_pct < V_NIFTY_MAX:
        side, ext = "short", max(x[2] for x in tb[-3:]) + 0.2 * atr
    else:
        return None
    return {"side": side, "stop_at": ext, "target": vw, "dev": round(dev, 2), "score": abs(dev)}


def vwap_plan(sig, entry):
    k = 1 if sig["side"] == "long" else -1
    risk = max(k * (entry - sig["stop_at"]), entry * V_MIN_RISK / 100)
    if risk > entry * V_MAX_RISK / 100:
        return None
    reward = k * (sig["target"] - entry)
    if reward < V_MIN_RR * risk:
        return None
    return {"entry": round(entry, 2), "sl": round(entry - k * risk, 2), "t1": round(sig["target"], 2),
            "t2": round(sig["target"], 2), "risk": round(risk, 2)}


def gap_signal(tb, prev_close):
    """Gap fade at the close of the last candle in tb."""
    if len(tb) < 4 or not prev_close:
        return None
    gap = (tb[0][1] / prev_close - 1) * 100
    if not G_MIN <= abs(gap) <= G_MAX:
        return None
    orh, orl = max(b[2] for b in tb[:3]), min(b[3] for b in tb[:3])
    vw, c = _vwap(tb), tb[-1][4]
    if not vw:
        return None
    if gap > 0 and c < orl and c < vw:
        return {"side": "short", "stop_at": orh * 1.001, "fill": prev_close, "gap": round(gap, 2), "score": gap}
    if gap < 0 and c > orh and c > vw:
        return {"side": "long", "stop_at": orl * 0.999, "fill": prev_close, "gap": round(gap, 2), "score": -gap}
    return None


def gap_plan(sig, entry):
    k = 1 if sig["side"] == "long" else -1
    risk = max(k * (entry - sig["stop_at"]), entry * G_MIN_RISK / 100)
    if risk > entry * G_MAX_RISK / 100:
        return None
    to_fill = k * (sig["fill"] - entry)
    if to_fill < 1.5 * risk:      # the gap is nearly filled already: nothing left to fade
        return None
    return {"entry": round(entry, 2), "sl": round(entry - k * risk, 2), "t1": round(entry + k * 1.5 * risk, 2),
            "t2": round(entry + k * min(to_fill, 3 * risk), 2), "risk": round(risk, 2)}


ENGINES = {
    "vwap": {"name": "VWAP reversion", "from": V_FROM, "to": V_TO, "exit": V_EXIT, "half": False},
    "gapfade": {"name": "Gap fade", "from": G_FROM, "to": G_TO, "exit": G_EXIT, "half": True},
}


def day_trades(engine, day, data, nifty, groups=None):
    """Replay one day for one stock engine. data: {sym: 5-min candles over several days},
    nifty: Nifty 5-min candles. Walks the day candle by candle so a signal only sees the
    past; at most PER_DAY trades, one per stock and per sector group."""
    cfg, groups = ENGINES[engine], groups or {}
    days = {}
    for s, bars in data.items():
        tb = [b for b in bars if b[0].date() == day and _m(b[0]) >= 555]
        prev = [b for b in bars if b[0].date() < day]
        if len(tb) >= 6 and prev:
            days[s] = (tb, prev[-1][4])
    nb = [b for b in nifty if b[0].date() == day]
    npv = [b for b in nifty if b[0].date() < day]
    times = sorted({b[0] for tb, _ in days.values() for b in tb if _hm(cfg["from"]) <= _m(b[0]) + 5 <= _hm(cfg["to"])})
    out, taken, used = [], set(), set()
    for t in times:                       # t = signal candle start; it closes at t + 5 min
        if len(out) >= PER_DAY:
            break
        nn = [b for b in nb if b[0] <= t]
        npct = (nn[-1][4] / npv[-1][4] - 1) * 100 if nn and npv else None
        cands = []
        for s, (tb, pc) in days.items():
            if s in taken:
                continue
            upto = [b for b in tb if b[0] <= t]
            if not upto or upto[-1][0] != t:
                continue
            sig = vwap_signal(upto, npct) if engine == "vwap" else gap_signal(upto, pc)
            if sig:
                cands.append((sig["score"], s, sig, tb, len(upto)))
        for _, s, sig, tb, i0 in sorted(cands, key=lambda x: -x[0]):
            if len(out) >= PER_DAY:
                break
            g = groups.get(s)
            if (g and g in used) or i0 >= len(tb):
                continue
            entry = tb[i0][1]
            p = (vwap_plan if engine == "vwap" else gap_plan)(sig, entry)
            if not p:
                continue
            r, steps = manage(tb, i0, sig["side"], p["entry"], p["sl"], p["t1"], p["t2"], cfg["exit"], cfg["half"])
            out.append({"symbol": s, "side": sig["side"], "at": f"{tb[i0][0]:%H:%M}", "r": r,
                        "steps": steps, **p, **{k: sig[k] for k in ("dev", "gap") if k in sig}})
            taken.add(s)
            if g:
                used.add(g)
    return out


def record_stock_day(doc, today, results, event):
    """results: {engine: [trades]} → doc["days"][today][engine], each trade tagged."""
    d = doc.setdefault("days", {}).setdefault(today, {})
    for eng, res in results.items():
        d[eng] = [{**t, "event": bool(event)} for t in res]
    return d


# ---------- Nifty iron fly (live, on the option chain) ----------
def next_expiries(today, n=3):
    """Nifty weekly expiries are Tuesdays (NSE, since Sep 2025). The worker tries the
    real expiry list first, these are only the fallback guesses."""
    d = today + timedelta(days=(1 - today.weekday()) % 7)
    return [(d + timedelta(days=7 * i)).isoformat() for i in range(n)]


def _px(chain, strike, kind):
    for k, v in (chain.get("strikes") or {}).items():
        try:
            if abs(float(k) - strike) < 0.01:
                leg = (v or {}).get(kind) or {}
                p = leg.get("last_price")
                return float(p) if p else None
        except (TypeError, ValueError):
            continue
    return None


def leg_costs(open_px, close_px, qty, short):
    """Approximate F&O option charges for one leg, open + close (see nifty_paper.costs)."""
    import nifty_paper as nf
    buy, sell = (close_px, open_px) if short else (open_px, close_px)
    return nf.costs(qty * buy, qty * sell)


def fly_skip(today, chain, nifty_gap, vix_pct, events):
    why = []
    if chain and chain.get("expiry") == today:
        why.append("expiry day")
    if events:
        why.append("event day: " + "; ".join(events)[:60])
    if vix_pct is not None and vix_pct >= F_VIX_SPIKE:
        why.append(f"India VIX up {vix_pct:g}%")
    if nifty_gap is not None and abs(nifty_gap) >= F_GAP_MAX:
        why.append(f"Nifty gap {nifty_gap:+.1f}%")
    return why


def fly_open(chain, now, lots=1):
    """Build the paper iron fly from a chain snapshot. Returns (trade, None) or (None, why)."""
    spot = chain.get("underlying_ltp")
    if not spot:
        return None, "no Nifty price in the option chain"
    atm = round(float(spot) / STEP) * STEP
    legs = [("sell", atm, "ce"), ("sell", atm, "pe"), ("buy", atm + WING, "ce"), ("buy", atm - WING, "pe")]
    px = [_px(chain, k, t) for _, k, t in legs]
    if None in px:
        return None, "missing premiums for " + ", ".join(f"{k}{t.upper()}" for (_, k, t), p in zip(legs, px) if p is None)
    credit = px[0] + px[1] - px[2] - px[3]
    if credit <= 0 or credit >= WING:
        return None, f"odd credit {credit:.1f}"
    qty = lots * LOT
    return {"at": f"{now:%H:%M}", "expiry": chain.get("expiry"), "spot": float(spot), "atm": atm,
            "legs": [{"side": s, "strike": k, "type": t.upper(), "open": p} for (s, k, t), p in zip(legs, px)],
            "credit": round(credit, 2), "qty": qty, "max_loss": round((WING - credit) * qty),
            "status": "open", "path": []}, None


def fly_mtm(t, chain):
    """₹ P&L after costs if the fly were closed now on this chain snapshot, or None."""
    now_px = [_px(chain, l["strike"], l["type"].lower()) for l in t["legs"]]
    if None in now_px:
        return None
    pnl = 0.0
    for l, p in zip(t["legs"], now_px):
        sh = l["side"] == "sell"
        pnl += (l["open"] - p if sh else p - l["open"]) * t["qty"] - leg_costs(l["open"], p, t["qty"], sh)
    return round(pnl)


def fly_update(t, mtm, now):
    """Feed one MTM reading. Closes the paper trade at STOP / TARGET / F_EXIT; keeps
    recording "hold" (what holding to 15:15 gives) after an early exit."""
    hm = f"{now:%H:%M}"
    t["path"].append([hm, mtm])
    t["worst"] = min(t.get("worst", mtm), mtm)
    t["best"] = max(t.get("best", mtm), mtm)
    end = _m(now) >= _hm(F_EXIT)
    if t["status"] == "open":
        why = "stop" if mtm <= -STOP else "target" if mtm >= TARGET else "15:15" if end else None
        if why:
            t.update(status="closed", pnl=mtm, exit=f"{hm} {why}")
    if end:
        t["hold"] = mtm
        t["done"] = True
    return t


# ---------- scorecard ----------
def _stock_hist(doc, eng):
    return [x for d in sorted(doc.get("days", {})) for x in doc["days"][d].get(eng, [])]


def summary(doc):
    """Per engine: n, wins, ₹ total (stocks at ₹1,000 risk), and event-day split."""
    out = {}
    for eng in ENGINES:
        h = _stock_hist(doc, eng)
        ev = [x for x in h if x.get("event")]
        out[eng] = {"n": len(h), "win": sum(1 for x in h if x["r"] > 0), "rs": round(sum(x["r"] for x in h) * 1000),
                    "ev_n": len(ev), "ev_rs": round(sum(x["r"] for x in ev) * 1000)}
    fl = [d["fly"] for d in doc.get("days", {}).values() if isinstance(d.get("fly"), dict) and "pnl" in d["fly"]]
    out["fly"] = {"n": len(fl), "win": sum(1 for x in fl if x["pnl"] > 0), "rs": sum(x["pnl"] for x in fl),
                  "hold": sum(x.get("hold", x["pnl"]) for x in fl)}
    return out


def lines(doc, today, inr):
    import tables as T
    d = doc.get("days", {}).get(today, {})
    s = summary(doc)
    if not d and not any(v["n"] for v in s.values()):
        return []
    out = ["<b>🧪 Strategy lab · paper test</b> (not a signal)"]
    rows = []
    for eng in ENGINES:
        for x in d.get(eng, []):
            rows.append([("V " if eng == "vwap" else "G ") + T.short(x["symbol"], 8), x["side"][0].upper(),
                         x["at"], T.num(x["r"] * 1000)])
    if rows:
        out.append(T.table(["Stock", "L/S", "Time", "₹*"], rows))
    fly = d.get("fly")
    if isinstance(fly, dict) and "pnl" in fly:
        out.append(f"Iron fly {fly['atm']:g} (±{WING}) credit {fly['credit']:g}: {inr(fly['pnl'])} "
                   f"({fly.get('exit', '')}) · hold to 15:15 {inr(fly.get('hold', fly['pnl']))}")
    elif isinstance(fly, dict) and fly.get("skipped"):
        out.append(f"Iron fly: sat out ({fly['skipped'][:70]})")
    if not rows and not (isinstance(fly, dict) and "pnl" in fly):
        out.append("No lab trades today.")
    tot = [[ENGINES[e]["name"][:14], s[e]["n"], f"{100 * s[e]['win'] // s[e]['n']}%" if s[e]["n"] else "–",
            T.num(s[e]["rs"])] for e in ENGINES]
    f = s["fly"]
    tot.append(["Iron fly", f["n"], f"{100 * f['win'] // f['n']}%" if f["n"] else "–", T.num(f["rs"])])
    out.append(T.table(["So far", "N", "Win", "₹"], tot))
    evs = [f"{ENGINES[e]['name']} {s[e]['ev_n']} on event days ({inr(s[e]['ev_rs'])})" for e in ENGINES if s[e]["ev_n"]]
    if evs:
        out.append("Of those: " + "; ".join(evs) + ".")
    out.append("<i>*stocks at ₹1,000 risk a trade, after costs; fly = 1 lot after costs. "
               "V = VWAP reversion, G = gap fade.</i>")
    return out
