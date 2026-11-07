"""My Trade tracker: two-way Telegram bot for the orders you really place.

You tell the bot what you did in Groww:
    BOUGHT TATASTEEL                      (qty and SL/T1/T2 from today's plan, price = now)
    BOUGHT TATASTEEL 544 183.55           (your real qty and price)
    BOUGHT SBIN 100 812 SL 806 T1 824     (a stock that is not today's plan)
    SOLD TATASTEEL                        (everything left, price = now)
    SOLD TATASTEEL 272 185.9              (part of it)
    MYTRADES                              (status)   ·   HELP

For the account owner (first TELEGRAM_CHAT_ID), a plain BOUGHT / SOLD also asks
the cloud routine (payload SYNC_POSITIONS) to read today's Groww positions,
read-only; the real qty and average prices then replace the provisional ones.

Every 5 min (workflow mytrade.yml) it reads new messages, replies, and watches
each open trade against YOUR price: SL, T1 (sell half, SL to cost), T2,
15:00 warning and 15:10 exit, P&L. Each person's trades and messages stay in
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
    "🤖 <b>My Trade commands</b>",
    "",
    "<code>BOUGHT TATASTEEL</code>",
    "  I read your real qty and price",
    "  from Groww (owner) or use the plan",
    "<code>BOUGHT TATASTEEL 544 183.55</code>",
    "  your real qty and price",
    "<code>BOUGHT SBIN 100 812 SL 806 T1 824</code>",
    "  a stock outside today's plan",
    "<code>SOLD TATASTEEL</code>  all left (Groww price)",
    "<code>SOLD TATASTEEL 272 185.9</code>  part",
    "<code>MYTRADES</code>  your open trades",
    "",
    "I check every 5 min. I never place orders.",
])
CMD = re.compile(
    r"^/?(bought|buy|sold|sell)\s+([A-Za-z0-9&\-]+)"
    r"(?:\s+(\d+))?(?:\s+@?\s*(\d+(?:\.\d+)?))?"
    r"(?:\s+sl\s*(\d+(?:\.\d+)?))?(?:\s+t1\s*(\d+(?:\.\d+)?))?(?:\s+t2\s*(\d+(?:\.\d+)?))?\s*$",
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
                       f"<code>BOUGHT {w.esc(tr['symbol'])} QTY PRICE</code> to correct it.", to=chat)
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
              "boards": {}}
    return st


def open_trades(st):
    return [t for ts in st["trades"].values() for t in ts if t["left"] > 0]


def price_at(yahoo, when):
    """Close of the 5-min candle around `when`, else the live price; plus all candles."""
    bars, ltp = w.candles(yahoo)
    before = [b for b in bars if b[0] <= when]
    px = before[-1][4] if before else (ltp if ltp is not None else (bars[-1][4] if bars else None))
    return px, bars, ltp


def today_plan(state_dir, today):
    doc = w.load(os.path.join(state_dir, "plan.json"), {})
    wt = w.load(os.path.join(state_dir, "watch.json"), {})
    p = doc.get("plan") if doc.get("date") == today else None
    if p and wt.get("qty_fix"):
        p = {**p, "qty": wt["qty_fix"]}
    return p


def fmt_pnl(x):
    return w.inr(round(x))


def handle(st, chat, text, when, state_dir, today):
    reply = lambda msg: w.telegram(msg, to=chat)
    t = text.strip()
    if re.fullmatch(r"/?(help|start)", t, re.I):
        return reply(HELP)
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
    act, sym, qty, px, sl, t1, t2 = m.groups()
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
        plan = today_plan(state_dir, today)
        plan = plan if plan and plan["symbol"].upper() == sym else None
        q = int(qty) if qty else (plan["qty"] if plan else None)
        if not q:
            return reply(f"📦 How many shares? Send e.g. <code>BOUGHT {w.esc(sym)} 100 {price}</code>")
        tr = {
            "symbol": sym, "yahoo": yahoo, "qty": q, "left": q, "entry": price,
            "sl": float(sl) if sl else (plan["sl"] if plan else None),
            "t1": float(t1) if t1 else (plan["t1"] if plan else None),
            "t2": float(t2) if t2 else (plan["t2"] if plan else None),
            "opened": when.strftime("%H:%M"), "checked": when.strftime("%H:%M"),
            "realized": 0.0, "sells": [], "flags": {}, "plan": bool(plan),
        }
        mine.append(tr)
        lines = ["", f"Bought <b>{q}</b> @ <b>{w.rs(price)}</b>" + ("" if px else " (price now)")]
        if chat == owner_chat() and not (qty and px):
            tr["sync"] = "pending"
            st["sync_request"] = datetime.now(w.IST).strftime("%H:%M")
            lines.append("🔄 Getting your real fill from Groww (2–5 min)…")
        if plan:
            lines.append(f"Plan entry {w.rs(plan['entry'])}")
        if tr["sl"]:
            risk = q * (price - tr["sl"])
            ok = "✅" if risk <= 1000 else "⚠️ over ₹1,000 — reduce qty"
            lines += [f"Real risk to SL {w.rs(tr['sl'])}: ₹{w.lakh(risk)} {ok}", "",
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
    if chat == owner_chat() and not (qty and px):
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
            say = lambda title, icon, lines: w.telegram(w.card(icon, f"{title} — {w.esc(tr['symbol'])}",
                                                               [f"🕐 {now:%H:%M} IST", ""] + lines,
                                                               footer=False), to=chat)
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
                    f"👉 Move SL-M to cost <b>{w.rs(tr['entry'])}</b>",
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
            if mins >= w.hhmm("15:10") and not f.get("exit"):
                f["exit"] = True
                say("EXIT NOW", "⏰", [f"You still hold <b>{tr['left']}</b>", now_line, "",
                                      "👉 <b>Sell everything</b> — 15:10 rule",
                                      f"Then send <code>SOLD {w.esc(tr['symbol'])}</code>"])
            elif mins >= w.hhmm("15:00") and not f.get("warn"):
                f["warn"] = True
                say("10 MIN TO EXIT", "⏳", [f"You still hold <b>{tr['left']}</b>", now_line,
                                            "", "Plan to be out by 15:10."])
            tr["checked"] = (now - timedelta(minutes=5)).strftime("%H:%M")
            nxt = (f"T1 {w.rs(tr['t1'])}" if tr["t1"] and not f.get("t1")
                   else f"T2 {w.rs(tr['t2'])}" if tr["t2"] and not f.get("t2") else "exit by 15:10")
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


def summary(st):
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
    if w.hhmm("09:15") <= now.hour * 60 + now.minute <= w.hhmm("15:30"):
        track(st, now)
    if now.hour * 60 + now.minute >= w.hhmm("15:45") and not st["summary_sent"] \
            and any(st["trades"].values()):
        summary(st)
        st["summary_sent"] = True
    w.save(os.path.join(state_dir, "mytrades.json"), st)


if __name__ == "__main__":
    if sys.argv[1] == "--needed":
        sys.exit(0 if needed(sys.argv[2]) else 1)
    main(sys.argv[1])
