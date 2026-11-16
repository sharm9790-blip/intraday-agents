"""Nifty weekly options — PAPER trading only. Risk checks, lot sizing and a paper log, done by code.

Nothing here places orders. The `nifty-options-debate` skill writes `state/nifty_plan.json`;
this script checks it, sizes it in lots, and records it in `state/nifty_paper.json`, then
replays the option's own 5-min candles to see what would really have happened.

    python3 watcher/nifty_paper.py check  state/nifty_plan.json [--write]
        PASS / BLOCK. --write: set `lots` (or plan null on BLOCK) and add the plan to the
        paper log as `pending`.
    python3 watcher/nifty_paper.py replay state/nifty_paper.json ID candles.json
        candles.json = [["2026-11-16T10:05:00+05:30", o, h, l, c], ...] — the OPTION's
        5-min candles from the plan time on (INDmoney get_indian_stocks_ohlc). Fills, SL,
        T1, T2 and the 15:00 time stop are decided here, never by the AI.
    python3 watcher/nifty_paper.py report state/nifty_paper.json [--days 30]
        Win rate, net P&L after costs, expectancy — the number that decides whether this
        ever goes live.

Rules (change the constants, not the AI prompts):
  * buy CE or PE only (no option selling), 1 lot by default, at most 2 paper trades a day
  * premium SL 15–35% below entry, net R:R to T1 ≥ 1.5 after costs
  * delta 0.40–0.75 (ATM or 1 strike ITM, no cheap far-OTM)
  * no entries after 14:30, nothing on expiry day, stop for the day after 2 losses or −DAILY_STOP
"""
import json
import math
import sys
from datetime import date, datetime, timedelta

RISK_PER_TRADE = 2500    # ₹ max loss per paper trade incl. costs
MAX_LOTS = 1             # start with one lot
MAX_TRADES_DAY = 2
MAX_LOSSES_DAY = 2
DAILY_STOP = 5000        # ₹ — stop for the day at this net loss
MIN_RR = 1.5
SL_MIN_PCT, SL_MAX_PCT = 0.15, 0.35
DELTA_MIN, DELTA_MAX = 0.40, 0.75
LAST_ENTRY = "14:30"
TIME_STOP = "15:00"
NO_EXPIRY_DAY = True

# Approximate charges for buying options (per side). Good enough for paper P&L.
BROKERAGE = 20.0         # ₹ per order
STT_SELL = 0.001         # 0.1% of sell premium
EXCH = 0.0003503         # NSE transaction charge on premium, both sides
GST = 0.18               # on brokerage + exchange
STAMP_BUY = 0.00003      # on buy premium
SEBI = 0.000001


def _hm(s):
    h, m = str(s)[:5].split(":")
    return int(h) * 60 + int(m)


def costs(buy_value, sell_value, orders=2):
    brok = BROKERAGE * orders
    exch = EXCH * (buy_value + sell_value)
    return round(brok + exch + GST * (brok + exch) + STT_SELL * sell_value
                 + STAMP_BUY * buy_value + SEBI * (buy_value + sell_value), 2)


def size(entry, sl, lot_size, risk=RISK_PER_TRADE, max_lots=MAX_LOTS):
    """Lots so that the loss at SL incl. costs ≤ risk. 0 = even one lot is too much."""
    lots = 0
    for n in range(1, max_lots + 1):
        q = n * lot_size
        loss = q * (entry - sl) + costs(q * entry, q * sl)
        if loss <= risk:
            lots = n
    return lots


def _today_trades(log, day):
    return [t for t in log.get("trades", []) if t.get("date") == day]


def check(plan, verdict_time=None, log=None, today=None):
    """Return {"status": PASS|BLOCK, "reasons": [...], "lots", "qty", "risk", "rr"}."""
    log = log or {}
    today = today or plan.get("date") or date.today().isoformat()
    reasons = []
    side = plan.get("side")
    e, sl = plan.get("premium_entry"), plan.get("premium_sl")
    t1, t2 = plan.get("premium_t1"), plan.get("premium_t2", plan.get("premium_t1"))
    lot = int(plan.get("lot_size") or 0)
    if side not in ("CE", "PE"):
        reasons.append("side must be CE or PE (option buying only)")
    if lot <= 0:
        reasons.append("lot_size missing (take it from the contract details)")
    if None in (e, sl, t1) or not sl < e < t1 <= t2:
        reasons.append("premium levels out of order (need SL < entry < T1 ≤ T2)")
        return {"status": "BLOCK", "reasons": reasons, "lots": 0, "qty": 0, "risk": 0, "rr": 0}
    if plan.get("premium_max", e) < e:
        reasons.append("premium_max below premium_entry")
    drop = (e - sl) / e
    if drop < SL_MIN_PCT:
        reasons.append(f"premium SL {drop:.0%} below entry is inside normal noise (need ≥ {SL_MIN_PCT:.0%})")
    if drop > SL_MAX_PCT:
        reasons.append(f"premium SL {drop:.0%} below entry is too wide (max {SL_MAX_PCT:.0%})")
    # spot levels must point the same way as the option
    ss, st, s1 = plan.get("spot_sl"), plan.get("spot_trigger"), plan.get("spot_t1")
    if None not in (ss, st, s1):
        ok = ss < st < s1 if side == "CE" else ss > st > s1
        if not ok:
            reasons.append(f"spot levels don't match a {side} (SL, trigger, T1 order)")
    d = plan.get("delta")
    if d is not None and not DELTA_MIN <= abs(d) <= DELTA_MAX:
        reasons.append(f"delta {abs(d):.2f} outside {DELTA_MIN}–{DELTA_MAX} (use ATM / 1 ITM)")
    if NO_EXPIRY_DAY and plan.get("expiry") == today:
        reasons.append("expiry day: no paper trades (premium decay too fast)")
    if plan.get("deadline") and _hm(plan["deadline"]) > _hm(LAST_ENTRY):
        reasons.append(f"entry deadline after {LAST_ENTRY}")
    if verdict_time and _hm(verdict_time) >= _hm(LAST_ENTRY):
        reasons.append(f"plan given after {LAST_ENTRY}")
    day = _today_trades(log, today)
    taken = [t for t in day if t.get("status") != "blocked"]
    if len(taken) >= MAX_TRADES_DAY:
        reasons.append(f"{MAX_TRADES_DAY} paper trades already today")
    if any(t.get("status") in ("pending", "open") for t in day):
        reasons.append("a paper trade is still pending/open")
    losses = sum(1 for t in day if (t.get("pnl") or 0) < 0)
    pnl = sum(t.get("pnl") or 0 for t in day)
    if losses >= MAX_LOSSES_DAY:
        reasons.append(f"{losses} losses today: stopped for the day")
    if pnl <= -DAILY_STOP:
        reasons.append(f"day P&L ₹{pnl:,.0f}: daily stop hit")
    lots = size(e, sl, lot) if lot > 0 else 0
    if lot > 0 and lots == 0:
        reasons.append(f"1 lot risks more than ₹{RISK_PER_TRADE:,} at this SL: pick a cheaper strike or tighter level")
    n = max(lots, 1) * max(lot, 1)
    loss = n * (e - sl) + costs(n * e, n * sl)
    gain = n * (t1 - e) - costs(n * e, n * t1)
    rr = gain / loss if loss > 0 else 0
    if rr < MIN_RR:
        reasons.append(f"net R:R {rr:.2f} below {MIN_RR}")
    return {"status": "BLOCK" if reasons else "PASS", "reasons": reasons,
            "lots": lots, "qty": lots * lot, "risk": round(loss) if lots else 0,
            "rr": round(rr, 2), "premium_value": round(lots * lot * e)}


def manage(bars, trade):
    """Replay a paper trade on the option's 5-min candles [(datetime, o, h, l, c), ...].

    Entry: first candle (before the deadline) that trades at premium_entry, filled at
    max(open, entry) if that is ≤ premium_max. Then per candle, SL is checked first
    (conservative). T1: with 1 lot exit all; with 2+ lots book half and move SL to entry.
    T2 exits the rest; anything left goes at the close of the 15:00 candle.
    Returns the updated trade dict."""
    t = dict(trade)
    e, mx = t["premium_entry"], t.get("premium_max", t["premium_entry"])
    sl, t1, t2 = t["premium_sl"], t["premium_t1"], t.get("premium_t2", t["premium_t1"])
    qty = t["qty"]
    left, booked, sells = qty, 0.0, []
    filled = t.get("fill")
    i = 0
    if not filled:
        for i, (ts, o, h, l, c) in enumerate(bars):
            if t.get("deadline") and _hm(f"{ts:%H:%M}") > _hm(t["deadline"]):
                break
            if h >= e and l <= mx:
                px = max(o, e)
                if px > mx:
                    continue
                filled = {"price": round(px, 2), "time": f"{ts:%H:%M}"}
                break
        else:
            i = len(bars)
        if not filled:
            if bars and t.get("deadline") and _hm(f"{bars[-1][0]:%H:%M}") >= _hm(t["deadline"]):
                t.update(status="not_filled", pnl=0)
            return t
        t["fill"] = filled
    else:
        while i < len(bars) and f"{bars[i][0]:%H:%M}" < filled["time"]:
            i += 1
    entry = filled["price"]
    half_done = False
    for ts, o, h, l, c in bars[i:]:
        hm = f"{ts:%H:%M}"
        if l <= sl:
            sells.append((left, min(sl, o), "SL" if not half_done else "SL at cost", hm))
            left = 0
        elif not half_done and h >= t1:
            n = left if qty // t["lot_size"] <= 1 else (qty // t["lot_size"] // 2) * t["lot_size"]
            sells.append((n, t1, "T1", hm))
            left -= n
            half_done = True
            sl = entry
            if left and h >= t2:
                sells.append((left, t2, "T2", hm))
                left = 0
        elif half_done and h >= t2:
            sells.append((left, t2, "T2", hm))
            left = 0
        if left and _hm(hm) >= _hm(TIME_STOP):
            sells.append((left, c, "time stop", hm))
            left = 0
        if not left:
            break
    if left:
        t["status"] = "open"
        return t
    sell_val = sum(n * p for n, p, _, _ in sells)
    gross = sell_val - qty * entry
    cost = costs(qty * entry, sell_val, orders=1 + len(sells))
    t.update(status="closed", exits=[{"qty": n, "price": round(p, 2), "why": w, "time": hm}
                                     for n, p, w, hm in sells],
             gross=round(gross, 2), costs=cost, pnl=round(gross - cost, 2),
             result=sells[-1][2] if len(sells) == 1 else "+".join(s[2] for s in sells))
    return t


def report(log, days=30, today=None):
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    tr = [t for t in log.get("trades", []) if t.get("date", "") >= since]
    closed = [t for t in tr if t.get("status") == "closed"]
    wins = [t for t in closed if t["pnl"] > 0]
    net = sum(t["pnl"] for t in closed)
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in closed if t["pnl"] <= 0)
    return {
        "days": days, "plans": len(tr), "filled": len(closed),
        "not_filled": sum(1 for t in tr if t.get("status") == "not_filled"),
        "open": sum(1 for t in tr if t.get("status") in ("pending", "open")),
        "wins": len(wins), "losses": len(closed) - len(wins),
        "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
        "net_pnl": round(net), "costs": round(sum(t.get("costs", 0) for t in closed)),
        "avg_per_trade": round(net / len(closed)) if closed else None,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
        "by_side": {s: round(sum(t["pnl"] for t in closed if t["side"] == s)) for s in ("CE", "PE")},
    }


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, doc):
    with open(path, "w") as f:
        json.dump(doc, f, indent=1)


def _bars(path):
    return [(datetime.fromisoformat(r[0]), *map(float, r[1:5])) for r in _load(path, [])]


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "check":
        path = argv[2]
        doc = _load(path, {})
        plan = doc.get("plan")
        if not plan:
            print("no plan")
            return 0
        log_path = path.replace("nifty_plan.json", "nifty_paper.json")
        log = _load(log_path, {"trades": []})
        vid = doc.get("verdict_id", "")
        vt = vid.split("_")[-1] if "_" in vid else None
        res = check(plan, f"{vt[:2]}:{vt[2:]}" if vt else None, log, doc.get("date"))
        print(json.dumps(res))
        if "--write" in argv:
            if res["status"] == "BLOCK":
                doc["blocked"] = {"contract": plan.get("contract"), "reasons": res["reasons"]}
                doc["plan"] = None
            else:
                plan.update(lots=res["lots"], qty=res["qty"], risk=res["risk"], rr=res["rr"])
                log.setdefault("trades", []).append({
                    "id": vid, "date": doc.get("date"), "status": "pending", **plan})
                _save(log_path, log)
            _save(path, doc)
        return 0 if res["status"] == "PASS" else 2
    if cmd == "replay":
        log_path, tid, candles = argv[2], argv[3], argv[4]
        log = _load(log_path, {"trades": []})
        for k, t in enumerate(log.get("trades", [])):
            if t["id"] == tid:
                log["trades"][k] = manage(_bars(candles), t)
                _save(log_path, log)
                print(json.dumps(log["trades"][k]))
                return 0
        print("no such trade", tid)
        return 1
    if cmd == "report":
        days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 30
        print(json.dumps(report(_load(argv[2], {"trades": []}), days), indent=1))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
