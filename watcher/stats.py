"""What the scorecard history says, for the weekly report and for the Judge.

    python3 watcher/stats.py state        # last 10 trading days, plain text

The cloud debate runs this before the Bull/Bear/Judge and passes the text to
the Judge, so buy zones and targets follow evidence (e.g. "plans waiting for a
dip were cancelled 4 of 5 times while plain limit orders made money").
"""
import json
import os
import sys
from collections import Counter


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def summarize(days):
    """days: scorecard.json entries. Returns a dict of counts and totals."""
    plans = [p for d in days for p in d.get("plans", [])]
    out = {"days": len(days), "plans": len(plans),
           "totals": {k: sum(d["totals"].get(k, 0) for d in days)
                      for k in ("rules", "limit", "market", "targets")}}
    if not plans:
        return out
    bought = [p for p in plans if p["rules"].get("bought")]
    cancels = Counter(p["rules"]["steps"][0] for p in plans if not p["rules"].get("bought"))
    tg = [p["targets"] for p in plans if "targets" in p]
    out.update(
        bought=len(bought),
        wins=sum(1 for p in bought if p["rules"]["pnl"] > 0),
        losses=sum(1 for p in bought if p["rules"]["pnl"] < 0),
        cancels=dict(cancels.most_common()),
        entry_fill=sum(1 for t in tg if t["entry_hit"]),
        t1_hit=sum(1 for t in tg if t["t1_hit"]),
        t2_hit=sum(1 for t in tg if t["t2_hit"]),
        with_targets=len(tg),
        best=max(plans, key=lambda p: p["market"]["pnl"])["symbol"],
    )
    return out


def text(s):
    if not s["plans"]:
        return f"Scorecard history: {s['days']} day(s), no GO plans yet."
    t = s["totals"]
    lines = [
        f"Scorecard, last {s['days']} trading day(s): {s['plans']} GO plans.",
        f"By plan rules: {s['bought']} bought ({s['wins']} wins, {s['losses']} losses), "
        f"P&L Rs {t['rules']}.",
        f"Plain limit order at entry: Rs {t['limit']}. Bought at market at once: Rs {t['market']}.",
    ]
    if s.get("with_targets"):
        n = s["with_targets"]
        lines.append(f"Price reached the entry in {s['entry_fill']}/{n} plans, T1 in {s['t1_hit']}/{n}, "
                     f"T2 in {s['t2_hit']}/{n}.")
    if s.get("cancels"):
        lines.append("Why plans were not bought: "
                     + "; ".join(f"{k} x{v}" for k, v in s["cancels"].items()) + ".")
    missed = sum(v for k, v in s.get("cancels", {}).items()
                 if "without the dip" in k or "not triggered" in k)
    if s["plans"] >= 4 and missed >= s["plans"] / 2 and s.get("t1_hit", 0) >= s["plans"] / 2:
        lines.append(f"Lesson: {missed}/{s['plans']} plans never got their dip while T1 was reached in "
                     f"{s['t1_hit']}/{s.get('with_targets', s['plans'])} — prefer BUY NOW when the setup "
                     "is confirmed and net R:R at the live price is >= 1.5.")
    if s["days"] >= 3:
        if t["limit"] > t["rules"] and s["entry_fill"] < s["with_targets"] / 2:
            lines.append("Lesson: entries are too far below the price (often never reached) — "
                         "set the buy zone closer to the live price/VWAP while keeping net R:R >= 1.5.")
        if t["rules"] < 0 and s["losses"] > s["wins"]:
            lines.append("Lesson: bought plans lose more often than they win — be stricter on trend "
                         "and market filters.")
    return "\n".join(lines)


if __name__ == "__main__":
    state = sys.argv[1] if len(sys.argv) > 1 else "state"
    hist = load(os.path.join(state, "scorecard.json"), [])
    print(text(summarize(hist[-10:])))
