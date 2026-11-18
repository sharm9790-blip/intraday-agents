"""2-year backtest of the opening momentum strategy (watcher/opening.py rules, unchanged)
on a fixed basket of liquid F&O stocks, using INDmoney 5-min candles saved by bt_fetch.py.

    python3 watcher/backtest.py btdata [out.json]

Same entry / exit code as the live paper engine (opening.check / plan / manage /
day_signals), so the backtest and the paper test can never drift apart. NIFTYBEES stands in
for Nifty (the "market not falling" filter). Volume vs a normal day = the stock's own
average daily volume over the previous 20 sessions, built from the same 5-min data.

Reports: totals, year by year, month by month, up vs down market days, exits, drawdown and
losing runs, the 11:15 early exit vs holding, the late-chase comparison (buying 09:55
stocks already up ≥ 4.5%), and a sensitivity check of nearby settings (no tuning).
Results in R (1R = the planned risk; at ₹1,000 risk per trade, +1R ≈ +₹1,000).
"""
import json
import os
import statistics as stx
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import opening
import scan

IST = timezone(timedelta(hours=5, minutes=30))


def load(folder):
    meta = json.load(open(os.path.join(folder, "meta.json")))
    data = {}
    for s in meta["symbols"]:
        p = os.path.join(folder, s + ".json")
        if not os.path.exists(p):
            continue
        rows = json.load(open(p))
        bars = [(datetime.fromtimestamp(r[0], IST), r[1], r[2], r[3], r[4], r[5] or 0) for r in rows]
        bars = [b for b in bars if 555 <= b[0].hour * 60 + b[0].minute <= 925]   # 09:15–15:25
        if bars:
            data[s] = bars
    return data


def index_by_day(bars):
    out = defaultdict(list)
    for b in bars:
        out[b[0].date()].append(b)
    return out


def run(data, params=None, days=None, early=True):
    params = params or {}
    old = {k: getattr(opening, k) for k in params}
    for k, v in params.items():
        setattr(opening, k, v)
    try:
        nifty = data["NIFTYBEES"]
        stocks = {s: b for s, b in data.items() if s != "NIFTYBEES"}
        byday = {s: index_by_day(b) for s, b in stocks.items()}
        nday = index_by_day(nifty)
        all_days = sorted(nday)
        days = days or all_days
        u = scan.load_universe(".")
        groups = {s: scan.group(s, u) for s in stocks}
        vol = {s: {d: sum(x[5] for x in v) for d, v in bd.items()} for s, bd in byday.items()}
        trades = []
        for i, d in enumerate(all_days):
            if d not in days or i < 11:
                continue
            prev_days = all_days[max(0, i - 21):i]
            win = {}
            avg = {}
            for s, bd in byday.items():
                if d not in bd:
                    continue
                pv = [x for x in prev_days if x in bd]
                if len(pv) < 10:
                    continue
                win[s] = [b for x in pv[-2:] for b in bd[x]] + bd[d]
                avg[s] = sum(vol[s][x] for x in pv[-20:]) / len(pv[-20:])
            if not win:
                continue
            nb = [b for x in all_days[i - 1:i + 1] for b in nday[x]]
            res = opening.day_signals(d, win, avg, nb, groups)
            for t in res:
                t["day"] = d.isoformat()
                if not early:
                    t["r"] = t["r_no_early_exit"]
                trades.append(t)
        return trades
    finally:
        for k, v in old.items():
            setattr(opening, k, v)


def late_chase(data):
    """The user's old pain: at 09:55 buy the 2 basket stocks already up ≥ 4.5%, hold."""
    stocks = {s: b for s, b in data.items() if s != "NIFTYBEES"}
    byday = {s: index_by_day(b) for s, b in stocks.items()}
    days = sorted({d for bd in byday.values() for d in bd})
    out = []
    for i, d in enumerate(days[1:], 1):
        cands = []
        for s, bd in byday.items():
            if d not in bd or days[i - 1] not in bd:
                continue
            db, pc = bd[d], bd[days[i - 1]][-1][4]
            i0 = next((k for k, b in enumerate(db) if b[0].hour == 9 and b[0].minute == 55), None)
            if not i0:
                continue
            pct = (db[i0 - 1][4] / pc - 1) * 100
            if pct >= 4.5:
                cands.append((pct, s, db, i0))
        for pct, s, db, i0 in sorted(cands, reverse=True)[:2]:
            e = db[i0][1]
            vol = sum(b[5] for b in db[:i0]) or 1
            vw = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in db[:i0]) / vol
            risk = min(max(e - vw * 0.997, e * 0.004), e * 0.03)
            p = {"entry": e, "sl": e - risk, "t1": e + 1.5 * risk, "t2": e + 3 * risk, "risk": risk,
                 "atr5": sum(b[2] - b[3] for b in db[max(0, i0 - 6):i0]) / max(1, min(6, i0))}
            r, _ = opening.manage(db, i0, p, early_exit=None)
            out.append({"day": d.isoformat(), "symbol": s, "r": r})
    return out


def stats(rs):
    if not rs:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    eq = peak = dd = 0.0
    run_ = worst = 0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        run_ = run_ + 1 if r <= 0 else 0
        worst = max(worst, run_)
    gl = -sum(r for r in rs if r <= 0)
    return {"n": len(rs), "win": round(100 * len(wins) / len(rs)), "avg": round(stx.mean(rs), 3),
            "total": round(sum(rs), 1), "pf": round(sum(wins) / gl, 2) if gl else None,
            "dd": round(dd, 1), "streak": worst,
            "avg_win": round(stx.mean(wins), 2) if wins else 0,
            "avg_loss": round(stx.mean([r for r in rs if r <= 0]), 2) if len(wins) < len(rs) else 0}


def main(folder, out_path=None):
    data = load(folder)
    nifty = index_by_day(data["NIFTYBEES"])
    nd = sorted(nifty)
    nchg = {d: (nifty[d][-1][4] / nifty[nd[i - 1]][-1][4] - 1) * 100 for i, d in enumerate(nd) if i}
    trades = run(data)
    rs = [t["r"] for t in trades]
    rep = {"span": [nd[0].isoformat(), nd[-1].isoformat()], "sessions": len(nd),
           "stocks": len(data) - 1, "overall": stats(rs)}
    by = defaultdict(list)
    for t in trades:
        by[t["day"][:4]].append(t["r"])
    rep["by_year"] = {k: stats(v) for k, v in sorted(by.items())}
    half = len(nd) // 2
    first = {d.isoformat() for d in nd[:half]}
    rep["walk_forward"] = {"first_half": stats([t["r"] for t in trades if t["day"] in first]),
                           "second_half": stats([t["r"] for t in trades if t["day"] not in first])}
    bm = defaultdict(list)
    for t in trades:
        bm[t["day"][:7]].append(t["r"])
    rep["by_month"] = {k: {"n": len(v), "total": round(sum(v), 1)} for k, v in sorted(bm.items())}
    rep["months_positive"] = f"{sum(1 for v in bm.values() if sum(v) > 0)}/{len(bm)}"
    up = [t["r"] for t in trades if nchg.get(datetime.fromisoformat(t["day"]).date(), 0) > 0.3]
    dn = [t["r"] for t in trades if nchg.get(datetime.fromisoformat(t["day"]).date(), 0) < -0.3]
    fl = [t["r"] for t in trades if -0.3 <= nchg.get(datetime.fromisoformat(t["day"]).date(), 0) <= 0.3]
    rep["by_market_day"] = {"nifty_up": stats(up), "nifty_flat": stats(fl), "nifty_down": stats(dn)}
    ex = defaultdict(list)
    for t in trades:
        last = " ".join(t["steps"])
        k = ("T2" if "T2" in last else "T1 then trail/cost" if "T1" in last else
             "early exit 11:15" if "early exit" in last else "SL" if "SL" in last else "other")
        ex[k].append(t["r"])
    rep["exits"] = {k: {"n": len(v), "avg": round(stx.mean(v), 2)} for k, v in ex.items()}
    rep["hold_no_early_exit"] = stats([t["r_no_early_exit"] for t in trades])
    rep["late_chase"] = stats([t["r"] for t in late_chase(data)])
    sens = {}
    for name, p in [("volume 15%", {"VOL_FRAC": 0.15}), ("volume 30%", {"VOL_FRAC": 0.30}),
                    ("up to +5%", {"PCT_MAX": 5.0}), ("gap up to 4%", {"GAP_MAX": 4.0}),
                    ("any Nifty", {"NIFTY_MIN": -9.0}), ("3 a day", {"PER_DAY": 3}),
                    ("1 a day", {"PER_DAY": 1})]:
        sens[name] = stats([t["r"] for t in run(data, p)])
    rep["sensitivity"] = sens
    eq, curve = 0.0, []
    for t in trades:
        eq += t["r"]
        curve.append([t["day"], round(eq, 2)])
    rep["equity_curve"] = curve
    rep["trades"] = [{k: t[k] for k in ("day", "at", "symbol", "pct", "gap", "vol_frac", "entry", "sl", "t1", "r")}
                     | {"exit": t["steps"][-1]} for t in trades]
    if out_path:
        json.dump(rep, open(out_path, "w"), indent=1, default=str)
    return rep


if __name__ == "__main__":
    r = main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps({k: v for k, v in r.items() if k not in ("equity_curve", "trades", "by_month")}, indent=1))
