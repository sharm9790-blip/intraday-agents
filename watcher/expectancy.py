"""Expectancy tracker: is the system's edge real, and is the money (₹1 lakh) the limit?

Answers, from the trades so far:
  paper  every plan taken exactly by its rules (scorecard.json, costs included)
  real   your own INDmoney trades (realtrades.json, ~0.08% costs taken off; your fills
         already carry the slippage, paper adds ~0.04% for it in replay.py)

Per side: trades, win rate, average win / loss, payoff, expectancy per trade in ₹ and
in R (R = the plan's risk: qty × (entry − SL)), profit factor, max drawdown, worst losing
streak. Then paper vs real trade by trade (same symbol, same day), plans you skipped,
trades you took that weren't a plan, and a verdict once there are MIN_TRADES trades:

  scale one step   expectancy ≥ 0.2R, profit factor ≥ 1.3, drawdown < 6% → risk 1% → 1.25%
  hold             expectancy > 0 but thinner → stay at ₹1 lakh / 1%
  fix first        expectancy ≤ 0 → don't add money; fix entries / filters

    python3 watcher/expectancy.py state [chat_id]
"""
import json
import tables as T
import os
import sys

CAPITAL = 100000
COST = 0.0008
HALVES_FROM = 20  # walk-forward check: first half vs second half once there are this many
MIN_TRADES = 30


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def paper_trades(hist, key="rules"):
    """Bought plans from scorecard.json → [{date, symbol, pnl, risk}]. key: rules | market."""
    out = []
    for d in hist:
        for p in d.get("plans", []):
            r = p.get(key) or {}
            if key == "rules" and not r.get("bought"):
                continue
            if key != "rules" and r.get("steps", [""])[0] in ("no data",):
                continue
            risk = p.get("qty", 0) * (p.get("entry", 0) - p.get("sl", 0))
            out.append({"date": d["date"], "symbol": p["symbol"], "pnl": r.get("pnl", 0),
                        "risk": round(risk) if risk > 0 else None, "v": p.get("rules_v") or "before 13 Nov"})
    return out


def real_trades(real, hist, chat):
    """realtrades.json for one chat → trades with costs off and R from that day's plan."""
    plans = {(d["date"], p["symbol"]): p for d in hist for p in d.get("plans", [])}
    out = []
    for day in sorted(real):
        for t in (real[day].get(str(chat)) or {}).get("list", []):
            if t.get("open"):
                continue
            p = plans.get((day, t["symbol"]))
            risk = t["qty"] * (p["entry"] - p["sl"]) if p and p["entry"] > p["sl"] else None
            out.append({"date": day, "symbol": t["symbol"], "plan": bool(p),
                        "pnl": round(t["pnl"] - COST * t["entry"] * t["qty"]),
                        "risk": round(risk) if risk else None, "slip": t.get("slip")})
    return out


def metrics(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    pnl = [t["pnl"] for t in trades]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]
    rk = [t for t in trades if t.get("risk")]  # R = total P&L ÷ total planned risk (robust to odd qty)
    bal = peak = CAPITAL
    dd = streak = worst = 0
    for x in pnl:
        bal += x
        peak = max(peak, bal)
        dd = max(dd, peak - bal)
        streak = streak + 1 if x < 0 else 0
        worst = max(worst, streak)
    gross_loss = -sum(losses)
    return {
        "n": n, "total": sum(pnl), "wins": len(wins), "losses": len(losses),
        "win_rate": round(100 * len(wins) / n),
        "avg_win": round(sum(wins) / len(wins)) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses)) if losses else 0,
        "payoff": round((sum(wins) / len(wins)) / (gross_loss / len(losses)), 2) if wins and losses else None,
        "exp": round(sum(pnl) / n),
        "exp_r": round(sum(t["pnl"] for t in rk) / sum(t["risk"] for t in rk), 2) if rk else None,
        "pf": round(sum(wins) / gross_loss, 2) if gross_loss else None,
        "dd": round(dd), "dd_pct": round(100 * dd / CAPITAL, 1), "streak": worst,
        "month": round(sum(pnl) / n * 2 * 21),  # at ~2 trades a day, 21 trading days
    }


def verdict(m):
    if m["n"] < MIN_TRADES:
        return ("collecting", f"{m['n']}/{MIN_TRADES} trades — too few to judge yet")
    er = m["exp_r"] if m["exp_r"] is not None else (m["exp"] / (CAPITAL * 0.01))
    if er <= 0:
        return ("fix", "expectancy ≤ 0: don't add money — fix entries/filters first")
    if er >= 0.2 and (m["pf"] or 0) >= 1.3 and m["dd_pct"] < 6:
        return ("scale", "edge looks real: OK to scale one step (risk 1% → 1.25%)")
    return ("hold", "small positive edge: stay at ₹1 lakh / 1% risk and keep collecting")


def walk_forward(trades):
    """First half vs second half (time order): does the edge hold up out of sample?"""
    if len(trades) < HALVES_FROM:
        return None
    h = len(trades) // 2
    a, b = metrics(trades[:h]), metrics(trades[h:])
    note = ("⚠️ edge fading: second half lost money" if a["exp"] > 0 >= b["exp"] else
            "✅ edge holding in both halves" if a["exp"] > 0 and b["exp"] > 0 else "")
    return f"Walk-forward: first {a['n']} {inr(a['exp'])}/trade · last {b['n']} {inr(b['exp'])}/trade" + (
        f" — {note}" if note else "")


def by_version(trades):
    vs = {}
    for t in trades:
        vs.setdefault(t.get("v"), []).append(t)
    if len(vs) < 2:
        return []
    return ["Per rules version: " + " · ".join(
        f"{v}: {metrics(ts)['n']} trades {inr(metrics(ts)['exp'])}/trade" for v, ts in vs.items())]


def slip_summary(real):
    s = [t["slip"] for t in real if t.get("slip")]
    if not s:
        return []
    d = [x["delay"] for x in s if x.get("delay") is not None]
    return [f"Your fills vs alert ({len(s)}): avg {inr(round(sum(x['rs'] for x in s) / len(s)))} paid above plan"
            + (f" · avg {round(sum(d) / len(d))} min after the alert" if d else "")]


def compare(paper, real):
    """Same symbol, same day: paper vs real. Plus skipped plans and off-plan trades."""
    rmap = {}
    for t in real:
        rmap.setdefault((t["date"], t["symbol"]), []).append(t)
    pairs, skipped = [], []
    for p in paper:
        r = rmap.pop((p["date"], p["symbol"]), None)
        if r:
            pairs.append({"date": p["date"], "symbol": p["symbol"], "paper": p["pnl"],
                          "real": sum(x["pnl"] for x in r)})
        else:
            skipped.append(p)
    extra = [t for ts in rmap.values() for t in ts]
    return {"pairs": pairs, "gap": sum(x["real"] - x["paper"] for x in pairs),
            "skipped": skipped, "skipped_pnl": sum(p["pnl"] for p in skipped),
            "extra": extra, "extra_pnl": sum(t["pnl"] for t in extra)}


def inr(x):
    return ("+" if x > 0 else "−" if x < 0 else "") + "₹" + f"{abs(x):,.0f}"


def _cols(m):
    """One table column of metrics for a side (paper / you / a test)."""
    if not m or not m.get("n"):
        return ["0", "–", "–", "–", "–", "–", "–", "–", "–"]
    return [str(m["n"]), f"{m['win_rate']}%", T.num(m["avg_win"]), T.num(m["avg_loss"]),
            T.num(m["exp"]), T.r(m["exp_r"]) if m["exp_r"] is not None else "–",
            f"{m['pf']:g}" if m["pf"] else "–", f"{m['dd_pct']}%", T.num(m["month"])]


ROWS = ["Trades", "Win rate", "Avg win", "Avg loss", "₹/trade", "R/trade", "Profit f.", "Max DD", "≈/month"]


KEY = [0, 1, 4, 5, 7]   # trades, win rate, ₹/trade, R/trade, max DD: the daily scorecard's short table


def side_by_side(named, rows=None):
    """named: [(title, metrics)] → a metrics × sides table (≤ 3 sides fit a phone)."""
    cols = [_cols(m) for _, m in named]
    idx = rows or range(len(ROWS))
    return T.table(["", *[t for t, _ in named]], [[ROWS[i], *[c[i] for c in cols]] for i in idx])


def lines(state_dir, chat=None, short=False):
    hist = load(os.path.join(state_dir, "scorecard.json"), [])
    paper = paper_trades(hist)
    mp = metrics(paper)
    named = [("Paper", mp)]
    rt, mr = [], None
    if chat:
        real = load(os.path.join(state_dir, "realtrades.json"), {}).get("days", {})
        rt = real_trades(real, hist, chat)
        if rt:
            mr = metrics(rt)
            named.append(("You", mr))
    if not short:
        mm = metrics(paper_trades(hist, "market"))
        if mm["n"] and len(named) < 3:
            named.append(("AtOnce", mm))
    out = ["Paper = every plan by its rules (costs incl.); You = your INDmoney trades (−0.08% costs)."
           if mr else "Paper = every plan by its rules, costs included.",
           side_by_side(named, KEY if short else None)]
    if not short:
        tr = [{**t, "pnl": p.get("trail", {}).get("pnl", t["pnl"])}
              for t, p in zip(paper, [p for d in hist for p in d.get("plans", []) if p.get("rules", {}).get("bought")])]
        if any("trail" in p for d in hist for p in d.get("plans", [])):
            out.append(f"Trail after T1 instead: {inr(metrics(tr)['exp'])}/trade")
        out += by_version(paper)
    wf = walk_forward(paper)
    if wf:
        out.append(wf)
    if rt:
        out += slip_summary(rt)
        c = compare(paper, rt)
        out += ["", "<b>Paper vs real</b>"]
        if c["pairs"]:
            out.append(f"Same trades ({len(c['pairs'])}): real {inr(sum(x['real'] for x in c['pairs']))}"
                       f" vs paper {inr(sum(x['paper'] for x in c['pairs']))} → gap <b>{inr(c['gap'])}</b>"
                       + (" (entries/exits cost you)" if c["gap"] < 0 else ""))
            if not short:
                out.append(T.table(["Day", "Stock", "Paper", "You"],
                                   [[x["date"][5:], T.short(x["symbol"], 9), T.num(x["paper"]), T.num(x["real"])]
                                    for x in c["pairs"][-8:]], right={2, 3}))
        if c["skipped"]:
            out.append(f"Plans you didn't take ({len(c['skipped'])}): paper {inr(c['skipped_pnl'])}")
        if c["extra"]:
            out.append(f"Trades that weren't a plan ({len(c['extra'])}): {inr(c['extra_pnl'])}")
    tests = []
    try:
        import shorts
        sh = shorts.history(load(os.path.join(state_dir, "shorts.json"), {}))
        if sh:
            ms = metrics(sh)
            tests.append(["Shorts", f"{ms['n']}/{MIN_TRADES}", f"{ms['win_rate']}%", T.num(ms["exp"])])
    except Exception as e:
        print("shorts expectancy failed", e)
    od = load(os.path.join(state_dir, "opening.json"), {})
    oh = [x["r"] for d in sorted(od.get("days", {})) for x in od["days"][d]]
    if oh:
        tests.append(["Opening", str(len(oh)), f"{100 * sum(1 for r in oh if r > 0) // len(oh)}%",
                      T.num(sum(oh) / len(oh) * 1000)])
    ed = load(os.path.join(state_dir, "ema_vwap.json"), {})
    eh = [x["r"] for d in sorted(ed.get("days", {})) for x in ed["days"][d]]
    if eh:
        tests.append(["Nifty EMA", str(len(eh)), f"{100 * sum(1 for r in eh if r > 0) // len(eh)}%",
                      T.num(sum(eh) / len(eh) * 1000)])
    nl = load(os.path.join(state_dir, "nifty_paper.json"), {})
    nc = [t for t in nl.get("trades", []) if t.get("status") == "closed"]
    if nc:
        tests.append(["Nifty opt", str(len(nc)), f"{100 * sum(1 for t in nc if t['pnl'] > 0) // len(nc)}%",
                      T.num(sum(t["pnl"] for t in nc) / len(nc))])
    if tests:
        out += ["", "<b>Paper tests</b> (not signals; opening backtest +₹750/trade, 76% win; "
                "Nifty options risk up to ₹2,500/trade; Nifty EMA+VWAP backtest −0.10R/trade)",
                T.table(["Test", "Trades", "Win", "₹/trade"], tests)]
    v = verdict(mp)
    out += ["", f"🎯 <b>Verdict (paper)</b>: {v[1]}"]
    if mr and mr["n"]:
        out.append(f"🎯 <b>Verdict (your trades)</b>: {verdict(mr)[1]}")
    return out


if __name__ == "__main__":
    import re
    txt = "\n".join(lines(sys.argv[1] if len(sys.argv) > 1 else "state",
                          sys.argv[2] if len(sys.argv) > 2 else None))
    print(re.sub(r"</?(b|pre|i)>", "", txt).replace("&amp;", "&"))
