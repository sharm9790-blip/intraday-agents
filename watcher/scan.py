"""Code scanner: ranks every liquid NSE cash stock each quarter hour for the Bull.

The AI debate only has time to look at a handful of stocks, so it used to scan
~10 F&O names itself and missed most movers (6–12 Nov: 45 of the 80 top
gainers were never looked at). This ranks the whole liquid universe by code:

  universe   watcher/universe.json (or a fresher state/universe.json): Nifty 500,
             EQ series only (no T2T), 20-day turnover ≥ ₹100 cr, price ≥ ₹100.
             F&O is NOT required (you buy cash shares); `fo` is kept as a tag.
  keep       above VWAP, ≤ 1.5% above VWAP (not run yet), up ≥ 0.3%, volume so far
             ≥ 1.2× usual for this time, and up ≤ 3% unless it pulled back to VWAP
             and bounced (no chasing). Non-F&O stocks up > 8% are skipped (they
             have 5/10/20% price bands; near a band you may not be able to exit).
  score      relative strength vs Nifty + volume surge + last-30-min momentum.
  mode       "trend" (Nifty ≥ +0.3% and ≥ 55% of the universe above VWAP or ≥ 65% up),
             "weak" (Nifty ≤ −0.3% and ≤ 40% above VWAP) or "choppy".

Writes state/candidates.json: {date, at, mode, nifty_pct, above_vwap_pct, up_pct, items: [...]}.

    python3 watcher/scan.py --build-universe watcher/universe.json   # weekly refresh (needs NSE + Yahoo)
"""
import json
import math
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = 12
MAX_VWAP_DIST = 1.5   # % above VWAP
NO_CHASE = 3.0        # % up on the day
MIN_VOL_X = 1.2
NONFO_MAX = 8.0       # % up: non-F&O price-band safety


def load_universe(state_dir):
    for p in (os.path.join(state_dir, "universe.json"), os.path.join(HERE, "universe.json")):
        try:
            with open(p) as f:
                u = json.load(f)
            if u.get("stocks"):
                return u
        except (OSError, ValueError):
            pass
    return {"stocks": {}}


def stats(bars, now):
    """bars: 5-min (time, o, h, l, c, v) over several days. None if not enough data."""
    today = now.date()
    tb = [b for b in bars if b[0].date() == today and b[0] + timedelta(minutes=5) <= now]
    prev = [b for b in bars if b[0].date() < today]
    if len(tb) < 3 or not prev:
        return None
    pc = prev[-1][4]
    price = tb[-1][4]
    vol = sum(b[5] for b in tb)
    vwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in tb) / vol if vol else None
    cut = tb[-1][0].time()
    days = {}
    for b in prev:
        if b[0].time() <= cut and b[0].time() >= tb[0][0].time():
            days[b[0].date()] = days.get(b[0].date(), 0) + b[5]
    usual = sum(days.values()) / len(days) if days else None
    last = tb[-12:]
    return {
        "price": round(price, 2),
        "pct": round((price / pc - 1) * 100, 2),
        "vwap": round(vwap, 2) if vwap else None,
        "vwap_dist": round((price / vwap - 1) * 100, 2) if vwap else None,
        "vol_x": round(vol / usual, 2) if usual else None,
        "last30": round((price / tb[-7][4] - 1) * 100, 2) if len(tb) >= 7 else 0.0,
        "atr5": round(sum(b[2] - b[3] for b in last) / len(last), 2),
        "bounced": bool(vwap and min(b[3] for b in tb[-6:]) <= vwap * 1.003 and price >= vwap),
        "day_high": round(max(b[2] for b in tb), 2),
    }


def keep(st, fo):
    if not st or st["vwap_dist"] is None or st["vol_x"] is None:
        return False
    if not fo and st["pct"] > NONFO_MAX:
        return False
    return (0 <= st["vwap_dist"] <= MAX_VWAP_DIST and st["pct"] >= 0.3 and st["vol_x"] >= MIN_VOL_X
            and (st["pct"] <= NO_CHASE or st["bounced"]))


def score(st, nifty_pct):
    return round(min(st["pct"] - nifty_pct, 4) + 1.5 * math.log2(max(st["vol_x"], 0.5))
                 + 3 * st["last30"], 2)


def mode_of(nifty_pct, above, up=0):
    if nifty_pct is None:
        return "choppy"
    if nifty_pct >= 0.3 and (above >= 55 or up >= 65):
        return "trend"
    if nifty_pct <= -0.3 and above <= 40:
        return "weak"
    return "choppy"


def run(state_dir, now, candles, universe=None, workers=16):
    u = universe or load_universe(state_dir)
    stocks = u.get("stocks", {})

    def one(sym):
        try:
            bars, _ = candles(sym + ".NS" if sym != "^NSEI" else sym, "5d")
            return sym, stats(bars, now)
        except Exception:
            return sym, None

    with ThreadPoolExecutor(workers) as ex:
        res = dict(ex.map(one, ["^NSEI", *stocks]))
    nifty = res.pop("^NSEI")
    nifty_pct = nifty["pct"] if nifty else None
    got = {s: st for s, st in res.items() if st and st["vwap"]}
    above = round(100 * sum(1 for st in got.values() if st["price"] > st["vwap"]) / len(got)) if got else 0
    up = round(100 * sum(1 for st in got.values() if st["pct"] > 0) / len(got)) if got else 0
    items = []
    for s, st in got.items():
        info = stocks[s]
        if keep(st, info.get("fo")):
            items.append({"symbol": s, "yahoo": s + ".NS", "fo": bool(info.get("fo")),
                          "sector": info.get("ind", ""), "turnover_cr": info.get("tv"),
                          "score": score(st, nifty_pct or 0), **st})
    items.sort(key=lambda x: -x["score"])
    return {"date": now.strftime("%Y-%m-%d"), "at": now.strftime("%H:%M"),
            "mode": mode_of(nifty_pct, above, up), "nifty_pct": nifty_pct,
            "above_vwap_pct": above, "up_pct": up, "scanned": len(got), "items": items[:TOP]}


def build_universe(path, min_tv=100, min_px=100):
    """Weekly: Nifty 500 (EQ series) + F&O list from NSE, 20-day turnover from Yahoo."""
    import csv
    import io
    hdr = {"User-Agent": "Mozilla/5.0"}

    def get(url):
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=30) as r:
            return r.read().decode("utf-8", "replace")

    n500 = list(csv.DictReader(io.StringIO(get(
        "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"))))
    fo = {r.split(",")[1].strip() for r in get(
        "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv").splitlines()[1:] if "," in r}

    def daily(sym):
        try:
            d = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/"
                               f"{urllib.parse.quote(sym)}.NS?interval=1d&range=1mo"))["chart"]["result"][0]
            q = d["indicators"]["quote"][0]
            rows = [(c, v) for c, v in zip(q["close"], q["volume"]) if c and v][-20:]
            return sym, (sum(c * v for c, v in rows) / len(rows) / 1e7, rows[-1][0]) if rows else None
        except Exception:
            return sym, None

    import urllib.parse
    eq = [r for r in n500 if r.get("Series") == "EQ"]
    with ThreadPoolExecutor(16) as ex:
        tv = dict(ex.map(daily, [r["Symbol"] for r in eq]))
    out = {}
    for r in eq:
        t = tv.get(r["Symbol"])
        if t and t[0] >= min_tv and t[1] >= min_px:
            out[r["Symbol"]] = {"tv": round(t[0]), "fo": r["Symbol"] in fo, "ind": r["Industry"]}
    if len(out) < 100:
        raise SystemExit(f"universe too small ({len(out)}), keeping the old one")
    from datetime import date
    with open(path, "w") as f:
        json.dump({"built": date.today().isoformat(), "min_turnover_cr": min_tv, "min_price": min_px,
                   "stocks": dict(sorted(out.items()))}, f, indent=0)
    print("universe:", len(out), "stocks")


def refresh(state_dir, days=7):
    """Rebuild state/universe.json when it is over `days` old (failures keep the old one)."""
    from datetime import date
    u = load_universe(state_dir)
    try:
        age = (date.today() - date.fromisoformat(u.get("built", "2000-01-01"))).days
    except ValueError:
        age = 999
    if age < days:
        print("universe fresh,", age, "days old")
        return
    build_universe(os.path.join(state_dir, "universe.json"))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--build-universe":
        build_universe(sys.argv[2])
    elif len(sys.argv) > 2 and sys.argv[1] == "--refresh":
        refresh(sys.argv[2])


def stock_trend(bars, now):
    """Today's last 6 closed 5-min candles all closed above VWAP with rising lows."""
    tb = [b for b in bars if b[0].date() == now.date() and b[0] + timedelta(minutes=5) <= now]
    if len(tb) < 8:
        return False
    vol = sum(b[5] for b in tb)
    if not vol:
        return False
    vwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in tb) / vol
    last = tb[-6:]
    return all(b[4] > vwap for b in last) and min(b[3] for b in last[3:]) >= min(b[3] for b in last[:3])


def atr5(bars, now, n=12):
    """Average 5-min range of today's last n closed candles (None if under 4)."""
    tb = [b for b in bars if b[0].date() == now.date() and b[0] + timedelta(minutes=5) <= now][-n:]
    return round(sum(b[2] - b[3] for b in tb) / len(tb), 2) if len(tb) >= 4 else None
