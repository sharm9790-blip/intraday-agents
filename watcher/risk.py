"""Hard risk limits and position sizing for a Judge plan, done by code, not the AI.

    python3 watcher/risk.py state/plan.json           # print PASS / BLOCK
    python3 watcher/risk.py state/plan.json --write   # also fix qty, or set plan null on BLOCK

The cloud debate runs this before writing the Telegram verdict; the watcher
runs it again on every new GO as a backstop. Idea adapted from the MIT-licensed
trader-risk / trader-budget skills (github.com/Jakeschincariol/trader-skill).
"""
import json
import math
import sys

CAPITAL = 100000        # ₹
RISK_PCT = 1.0          # % of capital at risk per trade
FIXED_COST = 40         # ₹ brokerage per round trip (₹20 buy + ₹20 sell)
PCT_COST = 0.0004       # STT, exchange, GST, stamp as a fraction of price
MIN_RR = 1.5            # net reward:risk to T1
MAX_POSITION_X = 1.0    # position value cap = capital × this (1.0 = no leverage)
LAST_ENTRY = "14:30"    # no fresh entries after this
MIN_STOP_ATR = 0.7      # SL must be ≥ this × the 5-min ATR (`atr5` in the plan) below entry


def _hm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def size(entry, sl, capital=CAPITAL, risk_pct=RISK_PCT):
    """Shares so that loss at SL incl. all costs ≤ risk budget and value ≤ cap."""
    per_share = entry - sl + entry * PCT_COST
    if per_share <= 0 or entry - sl <= 0:
        return 0
    by_risk = math.floor((capital * risk_pct / 100 - FIXED_COST) / per_share)
    by_value = math.floor(capital * MAX_POSITION_X / entry)
    return max(0, min(by_risk, by_value))

MAX_TRADES = 3  # trades open at once (same as watch.MAX_TRADES)


def check(plan, verdict_time=None, trades_today=0, capital=CAPITAL, risk_pct=RISK_PCT):
    """Return {"status": PASS|BLOCK, "reasons": [...], "qty", "risk", "rr", "value"}."""
    reasons = []
    e, sl, t1, t2 = plan["entry"], plan["sl"], plan["t1"], plan["t2"]
    if plan.get("side", "long") != "long":
        reasons.append("only long trades allowed")
    if not sl < e < t1 <= t2:
        reasons.append("levels out of order (need SL < entry < T1 ≤ T2)")
    if not plan.get("zone_low", e) <= e <= plan.get("max_entry", e):
        reasons.append("entry outside its own buy zone")
    qty = size(e, sl, capital, risk_pct)
    if qty <= 0:
        reasons.append("quantity works out to 0")
    q = max(qty, 1)
    cost = FIXED_COST + PCT_COST * e * q
    loss = q * (e - sl) + cost
    gain = q * (t1 - e) - cost
    rr = gain / loss if loss > 0 else 0
    if rr < MIN_RR:
        reasons.append(f"net R:R {rr:.2f} below {MIN_RR}")
    if plan.get("deadline") and _hm(plan["deadline"]) > _hm(LAST_ENTRY):
        reasons.append(f"entry deadline after {LAST_ENTRY}")
    if verdict_time and _hm(verdict_time) >= _hm(LAST_ENTRY):
        reasons.append(f"plan given after {LAST_ENTRY}")
    if trades_today >= MAX_TRADES:
        reasons.append(f"{MAX_TRADES} trades already open")
    atr = plan.get("atr5")
    if atr and e - sl < MIN_STOP_ATR * atr:
        reasons.append(f"stop {e - sl:.2f} below entry is inside normal 5-min noise "
                       f"(ATR {atr:g}; need ≥ {MIN_STOP_ATR * atr:.2f}): widen SL, smaller qty")
    return {
        "status": "BLOCK" if reasons else "PASS",
        "reasons": reasons,
        "qty": qty,
        "risk": round(loss) if qty else 0,
        "rr": round(rr, 2),
        "value": round(qty * e),
    }


def main(path, write=False):
    with open(path) as f:
        doc = json.load(f)
    plan = doc.get("plan")
    if not plan:
        print("no plan")
        return 0
    vid = doc.get("verdict_id", "")
    vt = vid.split("_")[-1] if "_" in vid else None
    res = check(plan, f"{vt[:2]}:{vt[2:]}" if vt else None)
    print(json.dumps(res))
    if write:
        if res["status"] == "BLOCK":
            doc["blocked"] = {"symbol": plan["symbol"], "reasons": res["reasons"]}
            doc["plan"] = None
            doc["go_count"] = max(0, doc.get("go_count", 1) - 1)
        else:
            plan["qty"] = res["qty"]
        with open(path, "w") as f:
            json.dump(doc, f, indent=1)
    return 0 if res["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], "--write" in sys.argv))
