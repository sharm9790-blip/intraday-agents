"""Market data pack for the cloud debate, built by code from INDmoney's own API.

The Bull / Bear / Judge used to get every number through claude.ai MCP connectors,
which need a fresh sign-in now and then (INDmoney's expired on 12 Nov). This module
computes the same evidence in code and the watcher writes it to the repo, so the debate
reads files instead of depending on a connector:

  state/cues.json   ~08:30 and ~09:00: US close, Asia live, crude, USD/INR, India VIX (Yahoo)
  state/pack.json   every scanner slot: market breadth + sectors, and for the top scanner
                    stocks and live plans: INDmoney full quote (incl. circuit limits),
                    5-min indicators (VWAP, EMA 9/21/50, RSI 14, MACD, ATR, opening range,
                    prior-day high/low/close, pivots, volume vs usual) and, for F&O stocks,
                    the option chain (PCR, call wall, put floor, OI change, build-up).

All reads are market data only (the worker's read-only ?quotes= / ?chain= / ?candles=).
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
PACK_TOP = 8      # scanner stocks with full detail
CHAIN_TOP = 4     # of those, F&O stocks that also get the option chain

CUES = [("S&P 500", "^GSPC", "US close"), ("Nasdaq", "^IXIC", "US close"), ("Dow", "^DJI", "US close"),
        ("Nikkei", "^N225", "Asia live"), ("Hang Seng", "^HSI", "Asia live"), ("Kospi", "^KS11", "Asia live"),
        ("Brent", "BZ=F", "crude"), ("WTI", "CL=F", "crude"), ("USD/INR", "INR=X", "currency"),
        ("India VIX", "^INDIAVIX", "prev close"), ("Nifty", "^NSEI", "prev close")]


# ---------- indicators (pure) ----------
def ema(xs, n):
    if len(xs) < n:
        return None
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def rsi(xs, n=14):
    if len(xs) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(xs[:n], xs[1:n + 1]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(xs[n:], xs[n + 1:]):
        d = b - a
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def macd_hist(xs):
    if len(xs) < 35:
        return None
    line = []
    for i in range(26, len(xs) + 1):
        a, b = ema(xs[:i], 12), ema(xs[:i], 26)
        line.append(a - b)
    sig = ema(line, 9)
    return line[-1] - sig if sig is not None else None


def indicators(bars, now):
    """bars: several days of 5-min (t, o, h, l, c, v). Uses only closed candles."""
    done = [b for b in bars if b[0] + timedelta(minutes=5) <= now]
    tb = [b for b in done if b[0].date() == now.date()]
    prev = [b for b in done if b[0].date() < now.date()]
    if not tb:
        return None
    closes = [b[4] for b in done]
    vol = sum(b[5] for b in tb)
    vwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in tb) / vol if vol else None
    out = {"price": tb[-1][4], "day_high": max(b[2] for b in tb), "day_low": min(b[3] for b in tb),
           "day_open": tb[0][1], "vwap": vwap,
           "ema9": ema(closes, 9), "ema21": ema(closes, 21), "ema50": ema(closes, 50),
           "rsi14": rsi(closes), "macd_hist": macd_hist(closes),
           "atr5": sum(b[2] - b[3] for b in tb[-12:]) / len(tb[-12:]),
           "orb15_high": max(b[2] for b in tb[:3]), "orb15_low": min(b[3] for b in tb[:3])}
    if prev:
        pday = prev[-1][0].date()
        pb = [b for b in prev if b[0].date() == pday]
        h, l, c = max(b[2] for b in pb), min(b[3] for b in pb), pb[-1][4]
        p = (h + l + c) / 3
        out.update(pdh=h, pdl=l, pdc=c, pct=(out["price"] / c - 1) * 100,
                   pivot=p, r1=2 * p - l, s1=2 * p - h, r2=p + (h - l), s2=p - (h - l))
        cut = tb[-1][0].time()
        days = {}
        for b in prev:
            if tb[0][0].time() <= b[0].time() <= cut:
                days[b[0].date()] = days.get(b[0].date(), 0) + b[5]
        if days:
            out["vol_x"] = vol / (sum(days.values()) / len(days))
    if len(tb) >= 7:
        out["last30_pct"] = (out["price"] / tb[-7][4] - 1) * 100
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in out.items() if v is not None}


def chain_stats(doc, price):
    """INDmoney /market/option-chain answer → PCR, call wall above, put floor below, OI change."""
    strikes = (doc or {}).get("strikes") or {}
    rows = []
    for k, v in strikes.items():
        try:
            s = float(k)
        except ValueError:
            continue
        ce, pe = (v or {}).get("ce") or {}, (v or {}).get("pe") or {}
        rows.append((s, ce.get("oi") or 0, ce.get("previous_oi") or 0, pe.get("oi") or 0,
                     pe.get("previous_oi") or 0, ce.get("iv"), pe.get("iv")))
    if not rows:
        return None
    ce_oi = sum(r[1] for r in rows)
    pe_oi = sum(r[3] for r in rows)
    above = [r for r in rows if r[0] >= price]
    below = [r for r in rows if r[0] <= price]
    wall = max(above, key=lambda r: r[1]) if above else None
    floor = max(below, key=lambda r: r[3]) if below else None
    ce_chg = sum(r[1] - r[2] for r in rows)
    pe_chg = sum(r[3] - r[4] for r in rows)
    atm = min(rows, key=lambda r: abs(r[0] - price))
    return {"expiry": doc.get("expiry"), "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None,
            "call_wall": wall and wall[0], "call_wall_oi": wall and wall[1],
            "put_floor": floor and floor[0], "put_floor_oi": floor and floor[3],
            "ce_oi_change": ce_chg, "pe_oi_change": pe_chg,
            "read": ("put writing > call writing (supportive)" if pe_chg > ce_chg > 0 or (pe_chg > 0 >= ce_chg)
                     else "call writing > put writing (capping)" if ce_chg > pe_chg else "mixed"),
            "atm_iv": atm[5] or atm[6]}


def expiries(today, holidays=()):
    """Likely monthly stock-option expiries to try: last Tuesday of the month (NSE since
    Sep 2025), moved back over holidays; then last Thursday (older rule) as a fallback."""
    out = []
    for m in range(2):
        y, mo = today.year + (today.month + m - 1) // 12, (today.month + m - 1) % 12 + 1
        last = (date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1))
        for wd in (1, 3):
            d = last - timedelta(days=(last.weekday() - wd) % 7)
            while d.isoformat() in holidays or d.weekday() > 4:
                d -= timedelta(days=1)
            if d >= today:
                out.append(d.isoformat())
    return sorted(set(out), key=out.index)[:3]


# ---------- fetching ----------
def _worker(query, timeout=40):
    url = os.environ["WORKER_URL"].rstrip("/") + "/?" + query
    req = urllib.request.Request(url, headers={"x-worker-key": os.environ["WORKER_KEY"],
                                               "User-Agent": "intraday-watcher"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def worker_on():
    return bool(os.environ.get("WORKER_URL") and os.environ.get("WORKER_KEY"))


def quotes(symbols):
    if not worker_on() or not symbols:
        return {}
    d = _worker("quotes=" + urllib.parse.quote(",".join(symbols[:100])))
    return d.get("quotes") or {} if d.get("ok") else {}


def chain(sym, exp_list):
    if not worker_on():
        return None
    d = _worker(f"chain={urllib.parse.quote(sym)}&expiry={','.join(exp_list)}&strikes=8")
    return d.get("chain") if d.get("ok") else None


def yahoo_quote(sym):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym)
           + "?interval=1d&range=5d")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                timeout=15) as r:
        res = json.load(r)["chart"]["result"][0]
    m = res["meta"]
    closes = [c for c in (res["indicators"]["quote"][0].get("close") or []) if c]
    # last daily bar = latest session (live or closed); the one before = previous close
    px = m.get("regularMarketPrice") or (closes[-1] if closes else None)
    pc = closes[-2] if len(closes) >= 2 else None
    return {"price": px, "pct": round((px / pc - 1) * 100, 2) if px and pc else None,
            "time": datetime.fromtimestamp(m.get("regularMarketTime", 0), IST).strftime("%d %b %H:%M")}


def build_cues(now, fetch=yahoo_quote):
    items, parts = {}, []
    for name, sym, kind in CUES:
        try:
            q = fetch(sym)
        except Exception as e:
            print("cue failed", sym, e)
            continue
        items[name] = {**q, "kind": kind}
        if q.get("pct") is not None:
            parts.append(f"{name} {q['pct']:+.1f}%")
    return {"date": now.strftime("%Y-%m-%d"), "at": now.strftime("%H:%M"), "items": items,
            "line": " · ".join(parts) or "no cues fetched",
            "note": "GIFT Nifty is not available from this source; read the gap from Asia live + US close, "
                    "then the 09:08 pre-open price."}


def build_pack(now, cand, live_syms, candles, holidays=(), fo=None):
    """cand = candidates.json; candles(yahoo_symbol, "5d") → (bars, ltp)."""
    fo = fo or {}
    top = [i["symbol"] for i in (cand.get("items") or [])[:PACK_TOP]]
    syms = list(dict.fromkeys(list(live_syms) + top))
    try:
        qs = quotes(syms)
    except Exception as e:
        print("pack quotes failed", e)
        qs = {}
    exps = expiries(now.date(), holidays)
    stocks, chains = [], 0
    for s in syms:
        row = {"symbol": s, "live_plan": s in live_syms, "fo": bool(fo.get(s))}
        if s in qs:
            row["quote"] = qs[s]
        try:
            bars, _ = candles(s + ".NS", "5d")
            row["ind"] = indicators(bars, now)
        except Exception as e:
            row["ind_error"] = str(e)[:80]
        if row["fo"] and chains < CHAIN_TOP:
            try:
                px = (row.get("ind") or {}).get("price") or (row.get("quote") or {}).get("ltp")
                c = chain(s, exps)
                if c and px:
                    row["chain"] = chain_stats(c, px)
                chains += 1
                time.sleep(0.4)
            except Exception as e:
                row["chain_error"] = str(e)[:80]
        stocks.append(row)
    return {"date": now.strftime("%Y-%m-%d"), "at": now.strftime("%H:%M"), "source": "INDmoney API via worker + code",
            "market": {k: cand.get(k) for k in ("mode", "nifty_pct", "above_vwap_pct", "up_pct", "scanned", "sectors")},
            "stocks": stocks}
