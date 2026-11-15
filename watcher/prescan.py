"""Premarket momentum scan over the WHOLE liquid universe, by code, before the open.

Before this, the 08:40 premarket debate let the AI pick its own ~5 candidates from the
connectors (most-active list + a few price histories). On 13 Nov it found five stretched
stocks (RSI 73–80) and none qualified. Now code ranks all ~234 stocks on yesterday's daily
candles and the debate starts from the best 15:

  trend      close > EMA 20 > EMA 50 (daily)
  momentum   5-day and 20-day return vs Nifty, close in the top of yesterday's range,
             volume vs the 20-day average
  setup      close within 3% below the 20-day high (room to break out) or a fresh
             20-day-high close on volume (breakout day 1)
  not stretched  RSI 14 ≤ 72 and 5-day return ≤ 12% (13 Nov: RSI 73–80 picks all failed)
  moves enough   daily ATR ≥ 1.2% of price (an intraday trade needs range)

Writes state/premarket_candidates.json {date, built_at, based_on, nifty_5d, items: [...]},
each item with the levels the Judge needs: pdh / pdl / pdc, 20-day high, daily ATR.

    python3 watcher/prescan.py state     # run by hand
"""
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import scan

IST = timezone(timedelta(hours=5, minutes=30))
TOP = 15
MAX_RSI = 72
MAX_5D = 12.0
MIN_ATR_PCT = 1.2
NEAR_HIGH = 3.0


def daily(sym, days="6mo"):
    """Yahoo daily candles [(date, o, h, l, c, v)] (completed sessions + today's if trading)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym)
           + "?interval=1d&range=" + days)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                timeout=20) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        out.append((datetime.fromtimestamp(ts, IST).date(), o, h, l, c, v or 0))
    return out


def _ema(xs, n):
    if len(xs) < n:
        return None
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def _rsi(xs, n=14):
    if len(xs) <= n:
        return None
    ag = sum(max(b - a, 0) for a, b in zip(xs[:n], xs[1:n + 1])) / n
    al = sum(max(a - b, 0) for a, b in zip(xs[:n], xs[1:n + 1])) / n
    for a, b in zip(xs[n:], xs[n + 1:]):
        ag = (ag * (n - 1) + max(b - a, 0)) / n
        al = (al * (n - 1) + max(a - b, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def features(rows, asof):
    """rows: daily candles; uses sessions strictly before `asof` (the trading day ahead)."""
    rows = [r for r in rows if r[0] < asof]
    if len(rows) < 60:
        return None
    c = [r[4] for r in rows]
    d = rows[-1]
    h20 = max(r[2] for r in rows[-21:-1])
    v20 = sum(r[5] for r in rows[-21:-1]) / 20
    trs = [max(r[2], p[4]) - min(r[3], p[4]) for p, r in zip(rows[-15:-1], rows[-14:])]
    atr = sum(trs) / len(trs)
    rng = d[2] - d[3]
    return {
        "date": d[0].isoformat(), "pdo": d[1], "pdh": d[2], "pdl": d[3], "pdc": d[4],
        "close_pos": round((d[4] - d[3]) / rng, 2) if rng else 0.5,
        "vol_x20": round(d[5] / v20, 2) if v20 else None,
        "h20": round(h20, 2), "to_h20_pct": round((h20 / d[4] - 1) * 100, 2),
        "new_high": d[4] >= h20,
        "ema20": _ema(c, 20), "ema50": _ema(c, 50), "rsi14": _rsi(c),
        "ret5": (c[-1] / c[-6] - 1) * 100, "ret20": (c[-1] / c[-21] - 1) * 100,
        "atr_pct": atr / d[4] * 100, "atr": atr, "v20": v20,
    }


def keep(f):
    if not f or f["ema20"] is None or f["ema50"] is None or f["rsi14"] is None or not f["vol_x20"]:
        return False
    setup = f["to_h20_pct"] <= NEAR_HIGH or (f["new_high"] and f["vol_x20"] >= 1.3)
    return (f["pdc"] > f["ema20"] > f["ema50"] and f["rsi14"] <= MAX_RSI and f["ret5"] <= MAX_5D
            and f["atr_pct"] >= MIN_ATR_PCT and f["close_pos"] >= 0.5 and setup)


def score(f, n5, n20):
    return round(min(f["ret5"] - n5, 8) * 0.5 + min(f["ret20"] - n20, 15) * 0.15
                 + 2 * f["close_pos"] + 1.5 * math.log2(max(f["vol_x20"], 0.5))
                 + (1.0 if f["new_high"] else max(0, 1 - f["to_h20_pct"] / NEAR_HIGH))
                 - max(0, f["rsi14"] - 66) * 0.25, 2)


def run(state_dir, asof, fetch=daily, universe=None, workers=16):
    u = universe or scan.load_universe(state_dir)
    stocks = u.get("stocks", {})

    def one(sym):
        try:
            return sym, features(fetch(sym if sym.startswith("^") else sym + ".NS"), asof)
        except Exception:
            return sym, None

    with ThreadPoolExecutor(workers) as ex:
        res = dict(ex.map(one, ["^NSEI", *stocks]))
    nf = res.pop("^NSEI") or {"ret5": 0, "ret20": 0}
    items = []
    for s, f in res.items():
        if keep(f):
            items.append({"symbol": s, "yahoo": s + ".NS", "fo": bool(stocks[s].get("fo")),
                          "sector": scan.group(s, u) or stocks[s].get("ind", ""),
                          "score": score(f, nf["ret5"], nf["ret20"]),
                          **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in f.items()
                             if k not in ("ema20", "ema50")},
                          "above_ema20_pct": round((f["pdc"] / f["ema20"] - 1) * 100, 2)})
    items.sort(key=lambda x: -x["score"])
    # one sector should not take the whole list: at most 3 per sector group in the top
    out, per = [], {}
    for it in items:
        g = it["sector"] if it["sector"] not in ("", "Other") else None
        if g and per.get(g, 0) >= 3:
            continue
        if g:
            per[g] = per.get(g, 0) + 1
        out.append(it)
    return {"date": asof.isoformat(), "built_at": datetime.now(IST).strftime("%H:%M"),
            "based_on": max((f["date"] for f in res.values() if f), default=None),
            "nifty_5d": round(nf["ret5"], 2), "nifty_20d": round(nf["ret20"], 2),
            "scanned": sum(1 for f in res.values() if f), "passed": len(items), "items": out[:TOP],
            # 20-day average daily volume of every stock: the opening engine's "volume so far vs a day"
            "avg_vol": {s: round(f["v20"]) for s, f in res.items() if f and f.get("v20")}}


if __name__ == "__main__":
    sd = sys.argv[1] if len(sys.argv) > 1 else "state"
    doc = run(sd, datetime.now(IST).date())
    with open(os.path.join(sd, "premarket_candidates.json"), "w") as f:
        json.dump(doc, f, indent=1)
    print(doc["scanned"], "scanned,", doc["passed"], "passed:", [i["symbol"] for i in doc["items"]])
