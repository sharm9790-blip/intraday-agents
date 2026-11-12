"""My Trade tracker: two-way Telegram bot for the orders you really place.

You tell the bot what you did in Groww:
    BOUGHT TATASTEEL                      (qty and SL/T1/T2 from today's plan, price = now)
    BOUGHT TATASTEEL 544 183.55           (your real qty and price)
    BOUGHT SBIN 100 812 SL 806 T1 824     (a stock that is not today's plan)
    SOLD TATASTEEL                        (everything left, price = now)
    SOLD TATASTEEL 272 185.9              (part of it)
    MYTRADES                              (status)   ·   HELP

INDmoney trades need no message: the Cloudflare worker reads your INDmoney
intraday positions every minute (read-only) and starts this workflow with them
(env IND_POSITIONS); apply_ind() opens, updates and closes the owner's trade from
INDmoney's own buy / sell quantities, averages and realised P&L.

For the account owner (first TELEGRAM_CHAT_ID), a plain BOUGHT / SOLD also asks
the cloud routine (payload SYNC_POSITIONS) to read today's Groww positions,
read-only; the real qty and average prices then replace the provisional ones.

Every 5 min (workflow mytrade.yml) it reads new messages, replies, and watches
each open trade against YOUR price: SL, T1 (sell half, SL to cost), T2,
14:50 warning and 15:00 exit, P&L. Each person's trades and messages stay in
their own chat. Commands are accepted only from TELEGRAM_CHAT_ID chats.
It never places, changes or cancels orders.

Usage: python mytrade.py <state_dir>            run (read messages + track)
       python mytrade.py --needed <state_dir>   exit 0 if a run is needed
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import watch as w

HELP = "\n".join([
    "🤖 <b>Bot commands</b>  (tap / or Menu)",
    "",
    "<b>Your trades</b>",
    "<code>BOUGHT TATASTEEL</code> — real qty/price from Groww",
    "<code>BOUGHT TATASTEEL 544 183.55</code> — your numbers",
    "<code>BOUGHT SBIN 100 812 SL 806 T1 824</code> — any stock",
    "INDmoney trades: <b>nothing to send</b>, I see your buys and sells in ~1 min",
    "<code>BOUGHT TATASTEEL 544 183.55 SL 182 IND</code> — set SL/T1 for an INDmoney stock",
    "<code>SOLD TATASTEEL</code> — all left · <code>SOLD TATASTEEL 272 185.9</code> — part",
    "<code>MYTRADES</code> — your open trades",
    "<code>ORDER GMK39038…</code> — check a Groww order by its id",
    "",
    "<b>Plan &amp; results</b>",
    "<code>PLAN</code> — resend today's latest verdict",
    "<code>SCORE</code> — scorecard + paper account so far",
    "",
    "<b>Settings for today</b>",
    "<code>RISK 0.5</code> — half size (0.25–1) · <code>RISK 1</code> — normal",
    "<code>PAUSE</code> — stop plan alerts for you today",
    "<code>RESUME</code> — start them again",
    "<code>HELP</code> — this list",
    "",
    "Replies in ~1–2 min, any time. I never place orders.",
])
BOT_COMMANDS = [  # shown in Telegram's "/" menu
    ("bought", "I bought — e.g. /bought TATASTEEL"),
    ("sold", "I sold — e.g. /sold TATASTEEL"),
    ("mytrades", "My open trades and P&L"),
    ("order", "Check a Groww order — /order ORDER_ID"),
    ("plan", "Resend today's latest verdict"),
    ("score", "Scorecard and paper account so far"),
    ("risk", "Size for today — /risk 0.5 or /risk 1"),
    ("pause", "Stop plan alerts for me today"),
    ("resume", "Start plan alerts again"),
    ("help", "All commands"),
]

CMD = re.compile(
    r"^/?(bought|buy|sold|sell)\s+([A-Za-z0-9&\-]+)"
    r"(?:\s+(\d+))?(?:\s+@?\s*(\d+(?:\.\d+)?))?"
    r"(?:\s+sl\s*(\d+(?:\.\d+)?))?(?:\s+t1\s*(\d+(?:\.\d+)?))?(?:\s+t2\s*(\d+(?:\.\d+)?))?(?:\s+(ind|indmoney|groww))?\s*$",
    re.I)


def chat_ids():
    return [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]


def allowed_chats():
    return set(chat_ids())


def owner_chat():
    """The Groww account holder: first chat in TELEGRAM_CHAT_ID."""
    ids = chat_ids()
    return ids[0] if ids else None


def fire_sync():
    """Ask the cloud routine to read today's Groww positions (read-only)."""
    url, token = os.environ.get("CLAUDE_FIRE_URL"), os.environ.get("CLAUDE_FIRE_TOKEN")
    if not url or not token:
        return False
    if os.environ.get("DRY_RUN"):
        print("FIRE SYNC_POSITIONS")
        return True
    req = urllib.request.Request(url, data=json.dumps({"text": "SYNC_POSITIONS"}).encode(), headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "experimental-cc-routine-2026-04-01",
        "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as e:
        print("sync fire failed", e)
        return False


def reconnect_hint(err):
    """Extra line when the cloud run says Groww itself is not connected."""
    e = (err or "").lower()
    if any(k in e for k in ("auth", "connect", "missing", "not available", "sign")):
        return "\n👉 Groww seems disconnected — reconnect at https://claude.ai/customize/connectors"
    return ""


def apply_sync(st, state_dir, today):
    """Replace the owner's provisional numbers with Groww's real fills."""
    req = st.get("sync_request")
    pos = w.load(os.path.join(state_dir, "positions.json"), {})
    if not req or pos.get("date") != today or (pos.get("synced_at") or "") < req:
        return
    chat = owner_chat()
    rows = {p["symbol"].upper(): p for p in pos.get("positions", [])}
    st["sync_request"] = None
    st["sync_fired"] = None
    for tr in st["trades"].get(chat, []):
        if tr.get("sync") != "pending":
            continue
        tr["sync"] = "done"
        p = rows.get(tr["symbol"])
        if pos.get("error"):
            w.telegram(f"⚠️ Groww sync failed ({w.esc(pos['error'][:60])}). "
                       f"Using my estimate for <b>{w.esc(tr['symbol'])}</b>; send "
                       f"<code>BOUGHT {w.esc(tr['symbol'])} QTY PRICE</code> to correct it."
                       + reconnect_hint(pos["error"]), to=chat)
            continue
        if not p or not p.get("buy_qty"):
            w.telegram(f"🔄 Groww shows no <b>{w.esc(tr['symbol'])}</b> buy yet — order not filled? "
                       "I'll keep my estimate; send BOUGHT again once it fills.", to=chat)
            continue
        bq, ba = int(p["buy_qty"]), float(p["buy_avg"])
        sq, sa = int(p.get("sell_qty") or 0), float(p.get("sell_avg") or 0)
        tr.update(qty=bq, entry=ba, left=max(bq - sq, 0),
                  realized=sq * (sa - ba) if sq else 0.0,
                  sells=[{"qty": sq, "price": sa, "at": pos["synced_at"]}] if sq else [])
        lines = ["", f"Bought <b>{bq}</b> @ <b>{w.rs(ba)}</b> (Groww avg)"]
        if sq:
            lines.append(f"Sold <b>{sq}</b> @ <b>{w.rs(sa)}</b> · P&amp;L {fmt_pnl(tr['realized'])}")
        lines.append(f"Open now: <b>{tr['left']}</b>")
        if tr["left"] and tr["sl"]:
            risk = tr["left"] * (ba - tr["sl"])
            lines.append(f"Real risk to SL {w.rs(tr['sl'])}: ₹{w.lakh(max(risk, 0))}"
                         + (" ✅" if risk <= 1000 else " ⚠️ over ₹1,000"))
        if not tr["left"]:
            lines += ["", f"✅ <b>Trade closed: {fmt_pnl(tr['realized'])}</b> (before costs)"]
        w.telegram(w.card("🔄", f"SYNCED FROM GROWW — {w.esc(tr['symbol'])}", lines, footer=False), to=chat)
        big = size_warn(tr["left"], ba, tr["sl"], tr.get("plan_qty")) if tr["left"] else []
        if big:
            w.telegram(w.card("⚠️", f"TOO BIG — {w.esc(tr['symbol'])}", ["", *big, "",
                              "One bad candle here costs more than the whole plan allows."],
                              footer=False), to=chat)


IND_SYM = re.compile(r"^[A-Z0-9&-]{1,20}$")


def ind_positions(state_dir, today, now):
    """INDmoney positions for today. A new snapshot comes in env IND_POSITIONS (from
    the worker) and is saved to state/positions_ind.json; it is data only, checked
    field by field. The snapshot is {"p": positions, "o": open BUY orders or null}
    (an older worker sent the positions list alone). Returns [{s, bq, ba, sq, sa, nq, rp}];
    the open orders [{s, q, p}] are saved for the watcher's trade count."""
    path = os.path.join(state_dir, "positions_ind.json")
    raw = os.environ.get("IND_POSITIONS") or ""
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        rows, orders = (data, None) if isinstance(data, list) else (
            (data.get("p"), data.get("o")) if isinstance(data, dict) else (None, None))
        if isinstance(rows, list):
            clean = []
            for r in rows[:200]:
                if not isinstance(r, dict) or not IND_SYM.match(str(r.get("s", "")).upper()):
                    continue
                try:
                    clean.append({"s": str(r["s"]).upper(), "bq": max(int(r.get("bq") or 0), 0),
                                  "ba": float(r.get("ba") or 0), "sq": max(int(r.get("sq") or 0), 0),
                                  "sa": float(r.get("sa") or 0), "nq": int(r.get("nq") or 0),
                                  "rp": float(r.get("rp") or 0)})
                except (TypeError, ValueError):
                    continue
            old = w.load(path, {})
            keep = old.get("orders", []) if old.get("date") == today else []
            if isinstance(orders, list):
                keep = []
                for o in orders[:200]:
                    if not isinstance(o, dict) or not IND_SYM.match(str(o.get("s", "")).upper()):
                        continue
                    try:
                        q, px = int(o.get("q") or 0), float(o.get("p") or 0)
                    except (TypeError, ValueError):
                        continue
                    if q > 0:
                        keep.append({"s": str(o["s"]).upper(), "q": q, "p": px})
            w.save(path, {"date": today, "at": now.strftime("%H:%M"), "positions": clean,
                          "orders": keep})
    doc = w.load(path, {})
    return doc.get("positions", []) if doc.get("date") == today else []


def apply_ind(st, state_dir, today, now):
    """Open / update / close the owner's INDmoney trades from INDmoney's positions.
    Uses the day's running totals (buy qty + avg, sell qty + avg, realised P&L), so it
    is safe to run again on the same data. Groww trades are never touched."""
    chat = owner_chat()
    seen = st.setdefault("ind_seen", {})
    for p in ind_positions(state_dir, today, now):
        sym, bq, sq = p["s"], p["bq"], p["sq"]
        if bq <= 0 or seen.get(sym) == [bq, sq]:
            continue                       # short-first trades are not followed
        before = seen.get(sym, [0, 0])
        seen[sym] = [bq, sq]
        mine = st["trades"].setdefault(chat, [])
        tr = next((x for x in reversed(mine) if x["symbol"] == sym and x.get("broker") == "INDmoney"), None)
        left = max(bq - sq, 0)
        realized = p["rp"] if p["rp"] else (sq * (p["sa"] - p["ba"]) if sq else 0.0)
        at = now.strftime("%H:%M")
        if not tr:
            plan = today_plan(state_dir, today, sym)
            tr = {"symbol": sym, "yahoo": sym + ".NS", "qty": bq, "left": left, "entry": p["ba"],
                  "sl": plan["sl"] if plan else None, "t1": plan["t1"] if plan else None,
                  "t2": plan["t2"] if plan else None, "opened": at, "checked": at,
                  "realized": realized, "sells": [], "flags": {}, "plan": bool(plan),
                  "plan_qty": plan["qty"] if plan else None, "broker": "INDmoney", "auto_ind": True}
            mine.append(tr)
            lines = ["", f"Bought <b>{bq}</b> @ <b>{w.rs(p['ba'])}</b> (INDmoney avg)"]
            if plan:
                lines.append(f"Plan entry {w.rs(plan['entry'])}")
            if tr["sl"]:
                risk = bq * (p["ba"] - tr["sl"])
                lines += [f"Real risk to SL {w.rs(tr['sl'])}: ₹{w.lakh(max(risk, 0))} "
                          + ("✅" if risk <= MAX_RISK else "⚠️ over ₹1,000 — reduce qty"),
                          *size_warn(bq, p["ba"], tr["sl"], tr["plan_qty"]), "",
                          f"👉 Place <b>SL-M {left} @ {w.rs(tr['sl'])}</b> now"]
                if tr["t1"]:
                    lines.append(f"👉 Sell <b>{bq // 2} @ {w.rs(tr['t1'])}</b> (T1)")
            else:
                lines += ["", "⚠️ Not today's plan, so I don't know your SL.",
                          f"Send <code>BOUGHT {w.esc(sym)} {bq} {p['ba']:g} SL PRICE IND</code> to add it."]
            if sq:
                lines.append(f"Sold <b>{sq}</b> @ <b>{w.rs(p['sa'])}</b> · P&amp;L {fmt_pnl(realized)}")
            lines += ["", "📲 Picked up from INDmoney by itself.",
                      "No need to send BOUGHT / SOLD: I see your INDmoney sells too."]
            title = "MY TRADE STARTED" if left else "TRADE CLOSED"
            if not left:
                lines += ["", f"✅ <b>Trade closed: {fmt_pnl(realized)}</b> (INDmoney, before costs)"]
            w.telegram(w.card("🧾", f"{title} — {w.esc(sym)}", lines, footer=False), to=chat)
            continue
        adopted = not tr.get("auto_ind")
        tr.update(qty=bq, entry=p["ba"], left=left, realized=realized, auto_ind=True)
        if sq > before[1]:
            tr["sells"].append({"qty": sq - before[1], "price": p["sa"], "at": at})
        lines = [""]
        if adopted:
            lines.append("🔄 Real numbers from INDmoney replace yours:")
        if bq > before[0] and before[0]:
            lines.append(f"➕ Bought more: now <b>{bq}</b> @ <b>{w.rs(p['ba'])}</b> avg")
        else:
            lines.append(f"Bought <b>{bq}</b> @ <b>{w.rs(p['ba'])}</b> (INDmoney avg)")
        if sq:
            lines.append(f"Sold <b>{sq}</b> @ <b>{w.rs(p['sa'])}</b> avg · P&amp;L {fmt_pnl(realized)}")
        if left:
            lines.append(f"Open now: <b>{left}</b>")
            if sq > before[1] and (tr["flags"].get("t1") or sq >= bq // 2):
                tr["sl"] = tr["entry"]
                lines.append(f"👉 Edit SL-M: qty <b>{left}</b>, trigger cost <b>{w.rs(tr['entry'])}</b>")
            big = size_warn(left, p["ba"], tr["sl"], tr.get("plan_qty")) if tr["sl"] else []
            lines += big
            title, icon = ("PART SOLD" if sq > before[1] else "SYNCED FROM INDMONEY"), "🧾"
        else:
            lines += ["", f"✅ <b>Trade closed: {fmt_pnl(realized)}</b> (INDmoney, before costs)",
                      "👉 Cancel any leftover SL / target orders"]
            title, icon = "TRADE CLOSED", "🧾"
        w.telegram(w.card(icon, f"{title} — {w.esc(sym)}", lines, footer=False), to=chat)


def get_updates(offset):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    url = (f"https://api.telegram.org/bot{token}/getUpdates?timeout=0&offset={offset}"
           "&allowed_updates=" + urllib.parse.quote('["message"]'))
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r).get("result", [])


def load_state(state_dir, today):
    st = w.load(os.path.join(state_dir, "mytrades.json"), {})
    if st.get("date") != today:
        st = {"offset": st.get("offset", 0), "date": today, "trades": {}, "summary_sent": False,
              "boards": {}, "menu": st.get("menu")}
    return st


def open_trades(st):
    return [t for ts in st["trades"].values() for t in ts if t["left"] > 0]


def price_at(yahoo, when):
    """Close of the 5-min candle around `when`, else the live price; plus all candles."""
    bars, ltp = w.candles(yahoo)
    before = [b for b in bars if b[0] <= when]
    px = before[-1][4] if before else (ltp if ltp is not None else (bars[-1][4] if bars else None))
    return px, bars, ltp


def today_plan(state_dir, today, sym=None):
    """Today's plan for sym (any of the live plans), else the newest one."""
    wt = w.load(os.path.join(state_dir, "watch.json"), {})
    plans = [e["plan"] for e in wt.get("live", [])] if wt.get("date") == today else []
    if not plans:
        doc = w.load(os.path.join(state_dir, "plan.json"), {})
        p = doc.get("plan") if doc.get("date") == today else None
        plans = [{**p, "qty": wt.get("qty_fix") or p["qty"]}] if p else []
    if sym:
        plans = [p for p in plans if p["symbol"].upper() == sym.upper()]
    return plans[-1] if plans else None


MAX_RISK = 1000          # ₹ to the stop-loss per trade
MAX_VALUE = 100000 * 5   # ₹1L capital at ~5x intraday margin
STALE_MIN = 75           # minutes with no real move before the "not moving" nudge


def size_warn(qty, entry, sl, plan_qty=None):
    """Lines warning that a position is bigger than the plan allows (empty if fine)."""
    out = []
    risk = qty * (entry - sl) if sl and entry > sl else 0
    if risk > MAX_RISK:
        keep = int(MAX_RISK // (entry - sl))
        out.append(f"🛑 Risk to SL is <b>₹{w.lakh(risk)}</b>, over the ₹{w.lakh(MAX_RISK)} limit.")
        out.append(f"👉 Sell <b>{qty - keep}</b> now and keep <b>{keep}</b>.")
    if qty * entry > MAX_VALUE:
        out.append(f"💰 Position ₹{w.lakh(qty * entry)} needs more than your ₹1L margin.")
    if plan_qty and qty > plan_qty * 1.2:
        out.append(f"📦 Plan qty was <b>{plan_qty}</b>, you hold <b>{qty}</b> ({qty / plan_qty:.1f}×).")
    return out


def fmt_pnl(x):
    return w.inr(round(x))


PLAN_STATE = {"waiting": "⏳ waiting", "in_zone": "🟢 bought (paper)", "t1_hit": "🎯 T1 done",
              "closed": "🏁 closed", "cancelled": "❌ cancelled"}


def today_lines(state_dir, chat=None):
    """Live part of SCORE: today's plans (paper) and your real trades so far."""
    today = datetime.now(w.IST).strftime("%Y-%m-%d")
    wt = w.load(os.path.join(state_dir, "watch.json"), {})
    lines = [f"<b>Today so far · {datetime.now(w.IST):%H:%M}</b>"]
    if wt.get("date") == today:
        plans = wt.get("live", [])
        lines.append(f"Scans {wt.get('verdicts', 0)} · GO plans {len(plans)} · "
                     f"paper buys {wt.get('trades', 0)}")
        for e in plans:
            st = PLAN_STATE.get(e.get("status"), e.get("status"))
            res = e.get("paper_result")
            lines.append(f"• {w.esc(e['symbol'])} {e.get('since', '')} {st}"
                         + (f" {w.inr(res)}" if res is not None else ""))
        lines.append(f"📈 Paper P&amp;L today {w.inr(wt.get('day_pnl', 0))}")
    else:
        lines.append("No scans yet today.")
    mt = w.load(os.path.join(state_dir, "mytrades.json"), {})
    mine = mt.get("trades", {}).get(chat, []) if mt.get("date") == today and chat else []
    if mine:
        lines.append("")
        for tr in mine:
            lines.append(f"🧾 {w.esc(tr['symbol'])} {tr['qty']} @ {w.rs(tr['entry'])}"
                         + (f" · {tr['left']} open" if tr["left"] else " · closed")
                         + f" {fmt_pnl(tr['realized'])}")
        lines.append(f"🧾 <b>Your real P&amp;L today {fmt_pnl(sum(t['realized'] for t in mine))}</b>"
                     " (booked, before costs)")
    return lines


def score_lines(state_dir, chat=None):
    hist = w.load(os.path.join(state_dir, "scorecard.json"), [])
    acc = w.load(os.path.join(state_dir, "paper.json"), {})
    lines = today_lines(state_dir, chat) + [""]
    if not hist:
        return lines + ["No full-day scorecard yet — the first one comes at 15:45."]
    run = {k: sum(d["totals"].get(k, 0) for d in hist) for k in ("rules", "limit", "market", "targets")}
    last = hist[-1]
    lines += [f"<b>Finished days: {len(hist)}</b> (last {last['date']})", "",
             f"Plan rules  {w.inr(run['rules'])}", f"Limit only  {w.inr(run['limit'])}",
             f"Bought at once  {w.inr(run['market'])}", f"Targets reached  +₹{w.lakh(run['targets'])}"]
    if acc:
        lines += ["", f"📈 Paper balance <b>₹{w.lakh(acc.get('balance', acc.get('start_balance', 0)))}</b>"
                  f" (since {acc.get('start_date')})"]
    real = w.load(os.path.join(state_dir, "realtrades.json"), {}).get("days", {})
    mine = [d[chat] for d in real.values() if chat and chat in d]
    if mine:
        lines.append(f"🧾 Your real P&amp;L {w.inr(sum(x['pnl'] for x in mine))} "
                     f"({sum(x['trades'] for x in mine)} trades, {len(mine)} days)")
    try:
        import expectancy
        lines += ["", "<b>🎯 Expectancy</b>"] + expectancy.lines(state_dir, chat=chat, short=True)
    except Exception as e:
        print("expectancy failed", e)
    return lines


ORDER_ID = re.compile(r"^/?order(?:\s+([A-Za-z0-9-]{6,40}))?\s*$", re.I)
FINAL = ("executed", "cancelled", "rejected", "notfound")
RECHECK_MIN = 10
MAX_CHECKS = 12  # ~2 hours of re-checks for an order still open


def save_order_requests(st, state_dir, today):
    """The ids the cloud should look up (read-only get_order_details)."""
    ids = [i for i, o in st.get("orders", {}).items() if o.get("state") not in FINAL]
    w.save(os.path.join(state_dir, "order_requests.json"), {"date": today, "ids": ids[-5:]})


def ask_order(st, oid, state_dir, today):
    o = st.setdefault("orders", {}).setdefault(oid, {"state": None, "checks": 0})
    o["req"] = datetime.now(w.IST).strftime("%H:%M")
    o["checks"] = o.get("checks", 0) + 1
    st["sync_request"] = o["req"]
    save_order_requests(st, state_dir, today)


def apply_orders(st, state_dir, today, now):
    """Report Groww order status; start/close tracking when an order is executed."""
    doc = w.load(os.path.join(state_dir, "orders.json"), {})
    chat = owner_chat()
    if doc.get("date") == today:
        found = {str(o.get("id")): o for o in doc.get("orders", [])}
        for oid, o in st.get("orders", {}).items():
            if not o.get("req") or o.get("seen") == o["req"] or (doc.get("synced_at") or "") < o["req"]:
                continue
            o["seen"] = o["req"]
            r = found.get(oid)
            if doc.get("error") or not r:
                o["state"] = "notfound"
                why = doc.get("error") or "Groww didn't find this order id"
                w.telegram(f"⚠️ Order <code>{w.esc(oid)}</code>: {w.esc(why[:80])}.\n"
                           "Check the id in Groww and send ORDER again."
                           + reconnect_hint(doc.get("error")), to=chat)
                continue
            state = (r.get("state") or "").lower()
            changed = state != o.get("state")
            o["state"] = state
            sym, side = (r.get("symbol") or "").upper(), (r.get("side") or "").upper()
            qty, filled = int(r.get("qty") or 0), int(r.get("filled_qty") or 0)
            px, avg = r.get("price"), r.get("avg_price")
            icon = {"executed": "✅", "partial": "🟠", "open": "🟡",
                    "cancelled": "⚪", "rejected": "❌"}.get(state, "❔")
            lines = ["", f"{w.esc(side)} <b>{qty}</b> {w.esc(sym)} · {w.esc(r.get('type') or '')}"
                     + (f" @ {w.rs(float(px))}" if px else ""),
                     f"Filled <b>{filled}</b>/{qty}" + (f" @ <b>{w.rs(float(avg))}</b>" if avg and filled else "")]
            if r.get("reason"):
                lines.append(f"Reason: {w.esc(str(r['reason'])[:80])}")
            if state in ("open", "partial"):
                lines.append(f"I'll check again in {RECHECK_MIN} min.")
            if changed or state not in ("open", "partial"):
                w.telegram(w.card(icon, f"ORDER {state.upper() or '?'} — {w.esc(sym)}", lines, footer=False), to=chat)
            if state == "executed" and sym and filled and avg:
                mine = [x for x in st["trades"].get(chat, []) if x["symbol"] == sym and x["left"] > 0]
                cmd = None
                if side == "BUY" and not mine:
                    cmd = f"BOUGHT {sym} {filled} {avg}"
                elif side == "SELL" and mine:
                    cmd = f"SOLD {sym} {min(filled, mine[-1]['left'])} {avg}"
                if cmd and market_open(now):
                    handle(st, chat, cmd, now, state_dir, today)
    # orders still open: look again every RECHECK_MIN minutes during market hours
    for oid, o in st.get("orders", {}).items():
        if o.get("state") in ("open", "partial") and o.get("seen") == o.get("req") \
                and market_open(now) and o.get("checks", 0) < MAX_CHECKS \
                and now.hour * 60 + now.minute - w.hhmm(o["req"]) >= RECHECK_MIN:
            ask_order(st, oid, state_dir, today)
    if st.get("orders"):
        save_order_requests(st, state_dir, today)  # drop orders that are done


def trading_day(d):
    return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in w.NSE_HOLIDAYS


def market_open(t):
    return trading_day(t) and w.hhmm("09:15") <= t.hour * 60 + t.minute <= w.hhmm("15:30")


def handle(st, chat, text, when, state_dir, today):
    reply = lambda msg: w.telegram(msg, to=chat)
    t = text.strip()
    t = re.sub(r"^/(\w+)@\w+", r"/\1", t)  # "/plan@sharmi_intraday_bot" -> "/plan"
    if re.fullmatch(r"/?(help|start|commands|menu)", t, re.I):
        return reply(HELP)
    if re.fullmatch(r"/?plan", t, re.I):
        try:
            with open(os.path.join(state_dir, "latest_verdict.txt")) as f:
                doc = w.load(os.path.join(state_dir, "plan.json"), {})
                if doc.get("date") != today:
                    if not trading_day(datetime.now(w.IST)):
                        return reply("📋 Market closed today. Next scan: next trading day 09:45 IST.")
                    return reply("📋 No verdict yet today. First scan 09:45.")
                return reply(w.check_groww_links(f.read()))
        except FileNotFoundError:
            return reply("📋 No verdict yet today.")
    if re.fullmatch(r"/?score", t, re.I):
        return reply(w.card("📒", "SCORE SO FAR", [""] + score_lines(state_dir, chat), footer=False))
    mo = ORDER_ID.match(t)
    if mo:
        if chat != owner_chat():
            return reply("🔒 Order checks work only for the Groww account holder.")
        if not mo.group(1):
            return reply("🔎 Send the Groww order id, e.g. <code>ORDER GMK39038RDT490CCVRO</code>\n"
                         "Groww → Orders → tap the order → copy the Order ID.")
        oid = mo.group(1).upper()
        ask_order(st, oid, state_dir, today)
        return reply(f"🔎 Checking order <code>{w.esc(oid)}</code> in Groww (read-only, 2–5 min)…")
    if re.fullmatch(r"/?pause", t, re.I):
        p = st.setdefault("paused", [])
        if chat not in p:
            p.append(chat)
        return reply("🔕 Plan alerts paused for you today. Your own trade alerts still come.\n"
                     "Send <code>RESUME</code> to turn them back on.")
    if re.fullmatch(r"/?resume", t, re.I):
        st["paused"] = [c for c in st.get("paused", []) if c != chat]
        return reply("🔔 Plan alerts back on.")
    mr = re.fullmatch(r"/?risk(?:\s+(\d*\.?\d+))?", t, re.I)
    if mr:
        if not mr.group(1):
            cur = st.get("risk", {}).get(chat, 1)
            return reply(f"⚖️ Your size today: <b>{cur}×</b>. Send <code>RISK 0.5</code> for half, "
                         "<code>RISK 1</code> for normal.")
        x = max(0.25, min(1.0, float(mr.group(1))))
        st.setdefault("risk", {})[chat] = x
        plan = today_plan(state_dir, today)
        extra = f"\nToday's plan qty for you: <b>{int(plan['qty'] * x)}</b> {w.esc(plan['symbol'])}" if plan else ""
        return reply(f"⚖️ Size set to <b>{x}×</b> for today (max risk ≈ ₹{int(1000 * x):,}).{extra}\n"
                     "Used when you send BOUGHT without a quantity.")
    if re.fullmatch(r"/?(bought|buy|sold|sell)", t, re.I):
        return reply("✍️ Add the stock, e.g. <code>BOUGHT TATASTEEL</code> or <code>SOLD TATASTEEL</code>")
    if re.fullmatch(r"/?(mytrades|status|trades)", t, re.I):
        mine = [x for x in st["trades"].get(chat, []) if x["left"] > 0]
        if not mine:
            return reply("🧾 No open trades. Send <code>BOUGHT SYMBOL</code> after you buy.")
        lines = []
        for x in mine:
            px, _, ltp = price_at(x["yahoo"], datetime.now(w.IST))
            un = x["left"] * ((ltp or px) - x["entry"])
            lines += [f"<b>{w.esc(x['symbol'])}</b> {x['left']} @ {w.rs(x['entry'])}",
                      f"Now {w.rs(ltp or px)} · P&amp;L {fmt_pnl(un + x['realized'])}", ""]
        return reply(w.card("🧾", "MY OPEN TRADES", [""] + lines, footer=False))
    m = CMD.match(t)
    if not m:
        return reply("🤔 I didn't get that.\n\n" + HELP)
    act, sym, qty, px, sl, t1, t2, broker = m.groups()
    on_ind = (broker or "").lower() in ("ind", "indmoney")
    if not market_open(when):
        return reply(f"🔒 Market is closed ({when:%a %H:%M} IST).\n"
                     "BOUGHT / SOLD work Mon–Fri 9:15–15:30 IST. HELP, PLAN, SCORE work any time.")
    sym = sym.upper()
    yahoo = sym + ".NS"
    try:
        mkt, bars, _ = price_at(yahoo, when)
    except Exception:
        return reply(f"❓ Couldn't find NSE stock <b>{w.esc(sym)}</b>. Check the symbol.")
    if mkt is None:
        return reply(f"❓ No price yet for <b>{w.esc(sym)}</b>.")
    price = float(px) if px else round(mkt, 2)
    mine = st["trades"].setdefault(chat, [])
    if act.lower() in ("bought", "buy"):
        auto = next((x for x in reversed(mine) if x["symbol"] == sym and x.get("auto_ind")
                     and x["left"] > 0), None) if on_ind else None
        if auto:                       # INDmoney already gave the fill: only add the levels
            auto.update({k: float(v) for k, v in (("sl", sl), ("t1", t1), ("t2", t2)) if v})
            return reply(w.card("🧾", f"LEVELS SET — {w.esc(sym)}", [
                "", f"{auto['left']} @ {w.rs(auto['entry'])} (from INDmoney)",
                f"🛑 SL {w.rs(auto['sl']) if auto['sl'] else '—'} · 🎯 T1 {w.rs(auto['t1']) if auto['t1'] else '—'}"
                f" · T2 {w.rs(auto['t2']) if auto['t2'] else '—'}"], footer=False))
        plan = today_plan(state_dir, today, sym)
        q = int(qty) if qty else (int(plan["qty"] * st.get("risk", {}).get(chat, 1)) if plan else None)
        if not q:
            return reply(f"📦 How many shares? Send e.g. <code>BOUGHT {w.esc(sym)} 100 {price}</code>")
        tr = {
            "symbol": sym, "yahoo": yahoo, "qty": q, "left": q, "entry": price,
            "sl": float(sl) if sl else (plan["sl"] if plan else None),
            "t1": float(t1) if t1 else (plan["t1"] if plan else None),
            "t2": float(t2) if t2 else (plan["t2"] if plan else None),
            "opened": when.strftime("%H:%M"), "checked": when.strftime("%H:%M"),
            "realized": 0.0, "sells": [], "flags": {}, "plan": bool(plan),
            "plan_qty": plan["qty"] if plan else None,
            "broker": "INDmoney" if on_ind else "Groww",
        }
        mine.append(tr)
        lines = ["", f"Bought <b>{q}</b> @ <b>{w.rs(price)}</b>" + ("" if px else " (price now)")]
        if on_ind:
            lines.append("📲 On INDmoney — your real fill replaces these numbers within ~1 min.")
        elif chat == owner_chat() and not (qty and px):
            tr["sync"] = "pending"
            st["sync_request"] = datetime.now(w.IST).strftime("%H:%M")
            lines.append("🔄 Getting your real fill from Groww (2–5 min)…")
        if plan:
            lines.append(f"Plan entry {w.rs(plan['entry'])}")
        if tr["sl"]:
            risk = q * (price - tr["sl"])
            ok = "✅" if risk <= 1000 else "⚠️ over ₹1,000 — reduce qty"
            lines += [f"Real risk to SL {w.rs(tr['sl'])}: ₹{w.lakh(risk)} {ok}",
                      *size_warn(q, price, tr["sl"], tr["plan_qty"]), "",
                      f"👉 Place <b>SL-M {q} @ {w.rs(tr['sl'])}</b> now"]
        else:
            lines += ["", "⚠️ No SL known — I can't warn you on a fall.",
                      "Send it again with your stop loss, e.g.",
                      f"<code>BOUGHT {w.esc(sym)} {q} {price} SL {round(price * 0.99, 1)}</code>"]
            mine.pop()
        if tr["t1"]:
            lines.append(f"👉 Sell <b>{q // 2} @ {w.rs(tr['t1'])}</b> (T1)")
        lines += ["", "I'll watch it every 5 min.", f"When you sell: <code>SOLD {w.esc(sym)}</code>"]
        return reply(w.card("🧾", f"MY TRADE STARTED — {w.esc(sym)}", lines, footer=False))
    # SOLD
    tr = next((x for x in reversed(mine) if x["symbol"] == sym and x["left"] > 0), None)
    if not tr:
        return reply(f"🧾 No open <b>{w.esc(sym)}</b> trade. Send <code>BOUGHT {w.esc(sym)}</code> first.")
    q = min(int(qty), tr["left"]) if qty else tr["left"]
    pnl = q * (price - tr["entry"])
    tr["left"] -= q
    tr["realized"] += pnl
    tr["sells"].append({"qty": q, "price": price, "at": when.strftime("%H:%M")})
    if chat == owner_chat() and not (qty and px) and tr.get("broker") != "INDmoney":
        tr["sync"] = "pending"
        st["sync_request"] = datetime.now(w.IST).strftime("%H:%M")
    lines = ["", f"Sold <b>{q}</b> @ <b>{w.rs(price)}</b>" + ("" if px else " (price now)"),
             f"This sale {fmt_pnl(pnl)}"]
    if tr.get("sync") == "pending":
        lines.append("🔄 Checking the real sell price in Groww…")
    if tr["left"]:
        lines += [f"Still holding <b>{tr['left']}</b>", f"P&amp;L so far {fmt_pnl(tr['realized'])}"]
        if tr["flags"].get("t1") or q >= tr["qty"] // 2:
            tr["sl"] = tr["entry"]
            lines.append(f"👉 Move SL-M to cost <b>{w.rs(tr['entry'])}</b>")
        return reply(w.card("🧾", f"PART SOLD — {w.esc(sym)}", lines, footer=False))
    lines += ["", f"✅ <b>Trade closed: {fmt_pnl(tr['realized'])}</b> (before costs)",
              "👉 Cancel any leftover SL / target orders",
              f"➖ Remove {w.esc(sym)} from \"My Draft Basket\""]
    return reply(w.card("🧾", f"TRADE CLOSED — {w.esc(sym)}", lines, footer=False))


def tg(method, params):
    """Telegram Bot API call; returns the result or None."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data, timeout=20) as r:
            return json.load(r).get("result")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if "message is not modified" in body:
            return True
        print("telegram", method, e.code, body[:120])
        return None


def board(st, chat, text, close=False):
    """One live board message per chat per day, edited in place (silent).
    A new board is sent (silently) if there is none yet or the edit fails."""
    b = st.setdefault("boards", {}).get(chat)
    if os.environ.get("DRY_RUN"):
        print(f"BOARD {'edit' if b else 'new'} to {chat}:\n{text}\n")
        st["boards"][chat] = {"id": 1, "open": not close}
        return
    if b and b.get("id"):
        if tg("editMessageText", {"chat_id": chat, "message_id": b["id"], "text": text,
                                  "parse_mode": "HTML", "disable_web_page_preview": "true"}):
            b["open"] = not close
            return
    if close:
        return
    res = tg("sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                             "disable_notification": "true", "disable_web_page_preview": "true"})
    if isinstance(res, dict):
        st["boards"][chat] = {"id": res["message_id"], "open": True}
        tg("pinChatMessage", {"chat_id": chat, "message_id": res["message_id"],
                              "disable_notification": "true"})


def track(st, now):
    """Alerts for each open trade, on candles that closed since the last check."""
    mins = now.hour * 60 + now.minute
    boards, totals = {}, {}
    for chat, trades in st["trades"].items():
        for tr in trades:
            if tr["left"] <= 0:
                continue
            def say(title, icon, lines, tr=tr, chat=chat):
                if tr.get("auto_ind"):  # INDmoney sells are seen by themselves
                    lines = ["✅ No need to send SOLD: I see INDmoney sales myself"
                             if x.startswith("Then send <code>SOLD") else x for x in lines]
                    lines = [x.replace("(Groww squares off at 15:05 + ₹50 fee)",
                                       "(before the broker's auto square-off fee)") for x in lines]
                w.telegram(w.card(icon, f"{title} — {w.esc(tr['symbol'])}",
                                  [f"🕐 {now:%H:%M} IST", ""] + lines, footer=False), to=chat)
            try:
                bars, ltp = w.candles(tr["yahoo"])
            except Exception as e:
                print("price failed", tr["symbol"], e)
                continue
            new = [b for b in bars if b[0].strftime("%H:%M") >= tr["checked"]
                   and b[0] + timedelta(minutes=5) <= now]
            price = ltp if ltp is not None else (bars[-1][4] if bars else tr["entry"])
            hi = max([b[2] for b in new] + [price])
            lo = min([b[3] for b in new] + [price])
            pnl = tr["left"] * (price - tr["entry"]) + tr["realized"]
            now_line = f"💹 Now <b>{w.rs(price)}</b> · P&amp;L <b>{fmt_pnl(pnl)}</b>"
            f = tr["flags"]
            sent = False
            if tr["sl"] and lo <= tr["sl"] and not f.get("sl_" + str(tr["sl"])):
                f["sl_" + str(tr["sl"])] = True
                at_cost = tr["sl"] >= tr["entry"]
                say("BACK AT COST" if at_cost else "MY SL HIT", "↩️" if at_cost else "🔴", [
                    f"Price touched {w.rs(tr['sl'])}", now_line, "",
                    f"👉 Make sure your {tr['left']} shares are sold",
                    f"Then send <code>SOLD {w.esc(tr['symbol'])}</code>"])
                sent = True
            elif tr["t1"] and hi >= tr["t1"] and not f.get("t1"):
                f["t1"] = True
                half = min(tr["qty"] // 2, tr["left"])
                say("MY T1 REACHED", "🎯", [
                    f"T1 {w.rs(tr['t1'])} reached", now_line, "",
                    f"👉 Sell <b>{half}</b> (if your T1 order didn't fill)",
                    f"👉 Edit SL-M: qty <b>{tr['left'] - half}</b> (what's left), trigger cost <b>{w.rs(tr['entry'])}</b>",
                    f"Then send <code>SOLD {w.esc(tr['symbol'])} {half} {tr['t1']}</code>"])
                tr["sl"] = tr["entry"]
                sent = True
            elif tr["t2"] and f.get("t1") and hi >= tr["t2"] and not f.get("t2"):
                f["t2"] = True
                say("MY T2 REACHED", "🏁", [
                    f"T2 {w.rs(tr['t2'])} reached", now_line, "",
                    f"👉 Sell the remaining <b>{tr['left']}</b>",
                    f"Then send <code>SOLD {w.esc(tr['symbol'])}</code>"])
                sent = True
            held = mins - w.hhmm(tr["opened"])
            if (not sent and tr["t1"] and tr["t1"] > tr["entry"] and not f.get("t1") and not f.get("stale")
                    and held >= STALE_MIN and mins < w.hhmm(w.WARN_AT)
                    and price < tr["entry"] + 0.3 * (tr["t1"] - tr["entry"])):
                f["stale"] = True
                say("NOT MOVING", "😴", [
                    f"Held {held} min, still near your buy {w.rs(tr['entry'])}", now_line,
                    f"🎯 T1 {w.rs(tr['t1'])} is {(tr['t1'] / price - 1) * 100:+.1f}% away", "",
                    "The setup expected a move by now. Your choice:",
                    f"👉 Exit near cost and free the margin, <b>or</b>",
                    f"👉 Move SL-M up to cost <b>{w.rs(tr['entry'])}</b> so it can't turn into a loss",
                    f"Then send <code>SOLD {w.esc(tr['symbol'])}</code> if you exit"])
                sent = True
            if mins >= w.hhmm(w.EXIT_BY) and not f.get("exit"):
                f["exit"] = True
                say("EXIT NOW", "⏰", [f"You still hold <b>{tr['left']}</b>", now_line, "",
                                      f"👉 <b>Sell everything</b> — {w.EXIT_BY} rule (Groww squares off at 15:05 + ₹50 fee)",
                                      f"Then send <code>SOLD {w.esc(tr['symbol'])}</code>"])
            elif mins >= w.hhmm(w.WARN_AT) and not f.get("warn"):
                f["warn"] = True
                say("10 MIN TO EXIT", "⏳", [f"You still hold <b>{tr['left']}</b>", now_line,
                                            "", f"Plan to be out by {w.EXIT_BY}."])
            tr["checked"] = (now - timedelta(minutes=5)).strftime("%H:%M")
            nxt = (f"T1 {w.rs(tr['t1'])}" if tr["t1"] and not f.get("t1")
                   else f"T2 {w.rs(tr['t2'])}" if tr["t2"] and not f.get("t2") else f"exit by {w.EXIT_BY}")
            gap = lambda lvl: f"{(lvl / price - 1) * 100:+.1f}%" if lvl else ""
            boards.setdefault(chat, []).extend([
                f"<b>{w.esc(tr['symbol'])}</b> {tr['left']} @ {w.rs(tr['entry'])}",
                f"💹 {w.rs(price)} · P&amp;L <b>{fmt_pnl(pnl)}</b>",
                (f"🛑 SL {w.rs(tr['sl'])} ({gap(tr['sl'])})" if tr["sl"] else "🛑 no SL")
                + f" · 🎯 {nxt}",
                "",
            ])
            totals[chat] = totals.get(chat, 0) + pnl
    for chat, lines in boards.items():
        board(st, chat, w.card("📋", f"LIVE TRADES · {now:%H:%M}", [
            "", *lines, f"Open trades P&amp;L <b>{fmt_pnl(totals[chat])}</b>",
            "<i>Updates every 5 min in this message. Alerts come separately.</i>"], footer=False))
    for chat in list(st.get("boards", {})):
        if chat not in boards and st["boards"][chat].get("open"):
            board(st, chat, w.card("📋", f"LIVE TRADES · {now:%H:%M}",
                                   ["", "✅ No open trades now."], footer=False), close=True)


def summary(st, state_dir=None):
    if state_dir:
        path = os.path.join(state_dir, "realtrades.json")
        hist = w.load(path, {"days": {}})
        hist["days"][st["date"]] = {
            chat: {"pnl": round(sum(t["realized"] for t in trs)), "trades": len(trs),
                   "list": [{"symbol": t["symbol"], "qty": t["qty"], "entry": t["entry"],
                             "pnl": round(t["realized"]), "open": t["left"]} for t in trs]}
            for chat, trs in st["trades"].items() if trs}
        w.save(path, hist)
    for chat, trades in st["trades"].items():
        if not trades:
            continue
        lines = [""]
        total = 0.0
        for tr in trades:
            total += tr["realized"]
            sells = ", ".join(f"{s['qty']} @ {w.rs(s['price'])}" for s in tr["sells"])
            lines += [f"<b>{w.esc(tr['symbol'])}</b> bought {tr['qty']} @ {w.rs(tr['entry'])}",
                      f"Sold {sells}" if sells else "Not sold yet",
                      f"Real P&amp;L {fmt_pnl(tr['realized'])}"
                      + (f" · ⚠️ {tr['left']} still open?" if tr["left"] else ""), ""]
        lines += [f"<b>Today real P&amp;L {fmt_pnl(total)}</b> (before costs)",
                  "Compare with 📈 Paper account in the scorecard."]
        w.telegram(w.card("🧾", "MY TRADES TODAY", lines, footer=False), to=chat)


def needed(state_dir):
    now = datetime.now(w.IST)
    st = load_state(state_dir, now.strftime("%Y-%m-%d"))
    if open_trades(st) or st.get("sync_request"):
        return True
    if any(o.get("state") in ("open", "partial") for o in st.get("orders", {}).values()):
        return True
    if any(st["trades"].values()) and not st["summary_sent"]:
        return True
    try:
        return bool(get_updates(st["offset"]))
    except Exception:
        return False


def main(state_dir):
    now = datetime.now(w.IST)
    today = now.strftime("%Y-%m-%d")
    st = load_state(state_dir, today)
    ok = allowed_chats()
    try:
        updates = get_updates(st["offset"])
    except Exception as e:
        print("getUpdates failed", e)
        updates = []
    for u in updates:
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text") or ""
        if chat not in ok or not text:
            continue  # only the configured chats can give commands
        when = datetime.fromtimestamp(msg.get("date", now.timestamp()), w.IST)
        if (now - when).total_seconds() > 1800:  # sent off-hours: say it was queued
            w.telegram(f"📨 Got your message from {when:%a %H:%M} — sorry for the delay, "
                       "here is the reply:", to=chat)
        if when.strftime("%Y-%m-%d") != today:
            when = now
        try:
            handle(st, chat, text, when, state_dir, today)
        except Exception as e:
            print("command failed", text[:40], e)
            w.telegram("⚠️ Couldn't process that. Try again or send HELP.", to=chat)
    if st.get("sync_request") and st.get("sync_fired") != st["sync_request"]:
        if fire_sync():
            st["sync_fired"] = st["sync_request"]
    apply_sync(st, state_dir, today)
    apply_ind(st, state_dir, today, now)
    apply_orders(st, state_dir, today, now)
    if st.get("sync_request") and st.get("sync_fired") != st["sync_request"]:  # re-check asked above
        if fire_sync():
            st["sync_fired"] = st["sync_request"]
    if st.get("sync_fired") and not st.get("sync_warned") == st["sync_fired"] \
            and now.hour * 60 + now.minute - w.hhmm(st["sync_fired"]) >= 15:
        st["sync_warned"] = st["sync_fired"]
        w.telegram("⚠️ <b>Groww sync is late</b> (asked at " + st["sync_fired"] + ").\n"
                   "I'm using my estimate. Send <code>BOUGHT SYMBOL QTY PRICE</code> to set it exactly.",
                   to=owner_chat())
    if st.get("menu") != len(BOT_COMMANDS) and not os.environ.get("DRY_RUN"):
        if tg("setMyCommands", {"commands": json.dumps(
                [{"command": c, "description": d} for c, d in BOT_COMMANDS])}):
            st["menu"] = len(BOT_COMMANDS)
    if w.hhmm("09:15") <= now.hour * 60 + now.minute <= w.hhmm("15:30"):
        track(st, now)
    if now.hour * 60 + now.minute >= w.hhmm("15:45") and not st["summary_sent"] \
            and any(st["trades"].values()):
        summary(st, state_dir)
        st["summary_sent"] = True
    w.save(os.path.join(state_dir, "mytrades.json"), st)


if __name__ == "__main__":
    if sys.argv[1] == "--needed":
        sys.exit(0 if needed(sys.argv[2]) else 1)
    main(sys.argv[1])
