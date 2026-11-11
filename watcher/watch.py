"""5-minute price watcher for the intraday debate.

Reads state/plan.json (written by the hourly cloud debate), checks the live
price on Yahoo Finance, and sends Telegram messages when the plan's status
changes. Writes state/watch.json so the same alert is never sent twice.
Messages are Telegram HTML, laid out for a phone screen.

Usage: python watch.py <state_dir>
Env:   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional DRY_RUN=1 to print only)
Analysis alerts only - it never places orders.
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import health
import news
import recheck
import risk

PAUSED = set()  # chats that sent PAUSE today (broadcasts skip them)

IST = timezone(timedelta(hours=5, minutes=30))
FOOTER = "<i>Analysis only, not advice. You place orders yourself.</i>"
OPEN_STATES = ("waiting", "in_zone", "t1_hit")
EXIT_BY = "15:00"  # Groww auto-squares-off MIS at 15:05 (+₹50 fee), so be out first
WARN_AT = "14:50"
MAX_LIVE = 3        # plans followed at the same time
MAX_TRADES = 3      # trades you may actually buy per day (see count_trades)
MISS_GRACE = 15     # min after a buy trigger to see your buy before the plan's slot is freed
DAY_LOSS_STOP = 2000  # no new plans once today's paper loss reaches this (₹)
# NSE equity trading holidays on weekdays (add next year's list each December).
NSE_HOLIDAYS = {"2026-11-10", "2026-11-24", "2026-12-25"}
STATUS_LABEL = {
    "waiting": "⏳ Waiting for dip",
    "in_zone": "🟢 In trade zone",
    "t1_hit": "🎯 T1 done, trailing",
}


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def rs(x):
    """₹ amount with Indian grouping, no trailing .0 (e.g. ₹1,234.5)."""
    s = f"{x:,.2f}".rstrip("0").rstrip(".")
    return "₹" + s


def esc(s):
    return html.escape(str(s), quote=False)


def telegram(text, to=None):
    """Send Telegram HTML; if the markup is rejected, resend as plain text.
    to: one chat id (e.g. a personal My Trade reply); default = every chat."""
    if os.environ.get("DRY_RUN"):
        print(f"TELEGRAM{' to ' + str(to) if to else ''}:\n" + text + "\n")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = str(to) if to else os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("telegram not configured; message was:", text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def post(params):
        data = urllib.parse.urlencode(params).encode()
        urllib.request.urlopen(url, data, timeout=20)

    # TELEGRAM_CHAT_ID may list several chats: "id1,id2". One failing chat
    # (e.g. someone blocked the bot) must not stop the others.
    for chat_id in [c.strip() for c in chat.split(",") if c.strip()]:
        if not to and chat_id in PAUSED:
            continue
        base = {"chat_id": chat_id, "disable_web_page_preview": "true"}
        try:
            try:
                post({**base, "text": text[:4000], "parse_mode": "HTML"})
            except urllib.error.HTTPError as e:
                if e.code != 400:
                    raise
                plain = html.unescape(re.sub(r"<[^>]+>", "", text))
                post({**base, "text": plain[:4000]})
        except urllib.error.HTTPError as e:
            print("telegram failed for one chat:", e.code)


def card(icon, title, lines, footer=True):
    out = [f"{icon} <b>{title}</b>", *lines]
    if footer:
        out += ["", FOOTER]
    return "\n".join(out)


def candles(yahoo_symbol):
    """Today's 5-min candles as (time_ist, open, high, low, close, volume), last one may be forming."""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(yahoo_symbol)
        + "?interval=5m&range=1d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        out.append((datetime.fromtimestamp(ts, IST), o, h, l, c, v or 0))
    return out, res["meta"].get("regularMarketPrice")


GROWW_DOWN = re.compile(r"groww\b[^\n]{0,40}\b(offline|not connected|disconnected|unavailable|reconnect)",
                        re.I)


def drop_groww_offline(text):
    """Remove the cloud's 'Groww offline / not connected' notes from a verdict: the
    debate runs fine on INDmoney + InvestSights, so the line is just noise."""
    out = [ln for ln in text.split("\n") if not GROWW_DOWN.search(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


GROWW_LINK = re.compile(r"(?:https://)?groww\.in/stocks/", re.I)


def check_groww_links(text, symbol=None):
    """Drop any Groww stock-page line the cloud still writes: orders go through
    INDmoney now, so GO cards carry no Groww link."""
    return "\n".join(ln for ln in text.split("\n") if not GROWW_LINK.search(ln))


def stock_name(yahoo_symbol):
    """Company name from Yahoo's chart meta, for the news search."""
    try:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               + urllib.parse.quote(yahoo_symbol) + "?interval=1d&range=1d")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            meta = json.load(r)["chart"]["result"][0]["meta"]
        return meta.get("longName") or meta.get("shortName")
    except Exception:
        return None


def hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def plan_lines(plan):
    return [
        f"🛑 Stop loss  <b>{rs(plan['sl'])}</b>",
        f"🎯 Target 1  <b>{rs(plan['t1'])}</b>  (sell half)",
        f"🎯 Target 2  <b>{rs(plan['t2'])}</b>",
        f"📦 Qty  <b>{plan['qty']}</b> shares",
    ]


def order_block(plan, qty=None):
    """Tap-to-copy order values (the user places the order in INDmoney; nothing is sent)."""
    q = qty or plan["qty"]
    buy = plan["entry"] if plan.get("entry_type") != "now" else plan["max_entry"]
    return [
        "📝 <b>Your order</b> (tap a value to copy)",
        f"📲 INDmoney › search <code>{esc(plan['symbol'])}</code> › Buy (tracked by itself)",
        "Intraday · Limit",
        f"Qty  <code>{q}</code>",
        f"Limit price  <code>{buy:g}</code>",
        "Then right after the fill:",
        f"SL-M sell {q} · trigger  <code>{plan['sl']:g}</code>",
        f"Limit sell {q // 2} · price  <code>{plan['t1']:g}</code>",
        f"⚠️ When that T1 sell fills: edit the SL-M to qty <code>{q - q // 2}</code>, "
        f"trigger <code>{plan['entry']:g}</code> (cost), or it sells too many and leaves you short.",
    ]


def lakh(x):
    """Indian grouping: 100000 -> 1,00,000."""
    s = str(int(round(abs(x))))
    head, tail = s[:-3], s[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    return ("-" if x < 0 else "") + (head + "," + tail if head else tail)


def inr(x):
    """Signed whole-rupee amount, e.g. +₹1,564 / −₹420."""
    if round(x) == 0:
        return "₹0"
    return ("+" if x > 0 else "−") + "₹" + f"{abs(x):,.0f}"


PAPER_START = 100000  # ₹ paper account opening balance


def paper_account(state_dir, today, rows):
    """Paper account that takes every plan exactly by its rules (the scorecard's
    "Plan rules" result, costs included). Rewrites today's trades, keeps history
    in state/paper.json, returns Telegram lines."""
    path = os.path.join(state_dir, "paper.json")
    acc = load(path, {"start_balance": PAPER_START, "start_date": today, "days": {}})
    acc["days"][today] = [
        {"symbol": r["symbol"], "start": r["start"], "pnl": r["rules"]["pnl"]}
        for r in rows if r["rules"].get("bought")
    ]
    bal = peak = acc["start_balance"]
    dd = 0.0
    trades = []
    for day in sorted(acc["days"]):
        for tr in acc["days"][day]:
            trades.append(tr["pnl"])
            bal += tr["pnl"]
            peak = max(peak, bal)
            dd = min(dd, (bal - peak) / peak)
    acc["balance"] = round(bal)
    save(path, acc)
    wins = sum(1 for p in trades if p > 0)
    losses = sum(1 for p in trades if p < 0)
    chg = (bal / acc["start_balance"] - 1) * 100
    today_pnl = sum(t["pnl"] for t in acc["days"][today])
    lines = [
        f"<b>📈 Paper account</b> (plan rules, since {acc['start_date']})",
        f"Balance <b>₹{lakh(bal)}</b>  ({chg:+.2f}%)",
        f"Today {inr(today_pnl)} · {len(acc['days'][today])} trade{'s' if len(acc['days'][today]) != 1 else ''}",
    ]
    if trades:
        lines += [
            f"Trades {len(trades)} · Wins {wins} · Losses {losses} · Win rate {wins * 100 // len(trades)}%",
            f"Biggest loss {inr(min(min(trades), 0))} · Max drawdown {dd * 100:.1f}%",
        ]
    else:
        lines += ["No trades yet — no plan has hit its buy trigger."]
    return lines


def send_scorecard(state_dir, today, watch):
    """Replay today's plans, send a Telegram scorecard, append to state/scorecard.json."""
    from replay import replay
    rows = []
    for p in watch.get("plans", []):
        try:
            bars, _ = candles(p["yahoo"])
            rows.append(replay(p, bars))
        except Exception as e:  # one bad symbol must not block the rest
            print("replay failed", p.get("symbol"), e)
    hist_path = os.path.join(state_dir, "scorecard.json")
    hist = [d for d in load(hist_path, []) if d.get("date") != today]
    tot = {k: sum(r.get(k, {}).get("pnl", 0) for r in rows) for k in ("rules", "limit", "market", "lock")}
    tot["targets"] = sum(r["targets"]["pf1"] * r["targets"]["t1_hit"]
                         + r["targets"]["pf2"] * r["targets"]["t2_hit"] for r in rows)
    hist.append({"date": today, "plans": rows, "totals": tot})
    save(hist_path, hist[-250:])
    run = {k: sum(d["totals"].get(k, 0) for d in hist) for k in ("rules", "limit", "market", "lock", "targets")}
    tgt = sum(r["targets"]["total"] for r in rows if "targets" in r)
    lines = [""]
    if not rows:
        n = watch.get("verdicts", 0)
        lines += [f"No GO plans today ({n} scan{'s' if n != 1 else ''}, all NO TRADE).", ""]
    for r in rows:
        ru = r["rules"]
        mark = "✅" if ru["pnl"] > 0 else ("❌" if ru["pnl"] < 0 else "⚪")
        lines += [
            f"{mark} <b>{esc(r['symbol'])}</b> · {r['start']}",
        ]
        tg = r.get("targets")
        if tg:
            yn = lambda ok: "✅" if ok else "❌"
            lines += [
                f"Cash ₹{lakh(tg['cash'])} · Qty <b>{tg['qty']}</b>",
                f"Entry {rs(r['entry'])} {yn(tg['entry_hit'])}",
                f"T1 {rs(r['t1'])} · 50% ({tg['half']}) {yn(tg['t1_hit'])}  +₹{lakh(tg['pf1'])}",
                f"T2 {rs(r['t2'])} · rest ({tg['rest']}) {yn(tg['t2_hit'])}  +₹{lakh(tg['pf2'])}",
                f"Target profit <b>+₹{lakh(tg['total'])}</b>",
                f"High {rs(tg['high'])} · Close {rs(r['close'])}",
            ]
        lines += [
            f"Plan rules {inr(ru['pnl'])} · Limit {inr(r['limit']['pnl'])} · At once {inr(r['market']['pnl'])}",
            f"At once + lock T1 {inr(r.get('lock', r['market'])['pnl'])}",
            "",
        ]
    lines += paper_account(state_dir, today, rows) + [""]
    lines += [
        "<b>Today</b>",
        f"If all targets hit  +₹{lakh(tgt)}",
        f"Targets reached (✅ only)  +₹{lakh(tot['targets'])}",
        f"Plan rules  {inr(tot['rules'])}",
        f"Limit only  {inr(tot['limit'])}",
        f"Bought at once  {inr(tot['market'])}",
        f"At once + lock T1  {inr(tot['lock'])}",
        "",
        f"<b>Running total · {len(hist)} day{'s' if len(hist) != 1 else ''}</b>",
        f"Targets reached  +₹{lakh(run['targets'])}",
        f"Plan rules  {inr(run['rules'])}",
        f"Limit only  {inr(run['limit'])}",
        f"Bought at once  {inr(run['market'])}",
        f"At once + lock T1  {inr(run['lock'])}",
        "",
        "Lock T1 = once price is halfway from T1 to T2, SL on the rest moves to T1.",
        "✅/❌ = price reached that level after the plan was given. Target profit is before costs.",
        "<i>Replay on 5-min candles, ~0.06% costs, SL assumed first when SL and target share a candle. Paper results, not real trades.</i>",
    ]
    telegram(card("📒", f"SCORECARD · {today}", lines))


def weekly_report(state_dir, now):
    """Friday (or the last trading day of the week) ≥ 15:50: the week in one card."""
    import stats
    wk = now.strftime("%G-W%V")
    path = os.path.join(state_dir, "weekly.json")
    if load(path, {}).get("last") == wk:
        return
    nxt = [(now + timedelta(days=i)) for i in range(1, 4)]
    last_day = now.weekday() == 4 or all(
        d.weekday() > 4 or d.strftime("%Y-%m-%d") in NSE_HOLIDAYS for d in nxt
        if d.isocalendar()[1] == now.isocalendar()[1])
    if not last_day:
        return
    hist = [d for d in load(os.path.join(state_dir, "scorecard.json"), [])
            if datetime.strptime(d["date"], "%Y-%m-%d").strftime("%G-W%V") == wk]
    s = stats.summarize(hist)
    acc = load(os.path.join(state_dir, "paper.json"), {})
    real = load(os.path.join(state_dir, "realtrades.json"), {}).get("days", {})
    t = s["totals"]
    lines = ["", f"Days {s['days']} · GO plans {s['plans']}"]
    if s["plans"]:
        n = s.get("with_targets") or 1
        lines += [f"Bought by rules {s['bought']} · ✅ {s['wins']} · ❌ {s['losses']}",
                  f"Entry reached {s['entry_fill']}/{n} · T1 {s['t1_hit']}/{n} · T2 {s['t2_hit']}/{n}"]
    lines += ["", "<b>Week P&amp;L</b>",
              f"Plan rules  {inr(t['rules'])}", f"Limit only  {inr(t['limit'])}",
              f"Bought at once  {inr(t['market'])}", f"At once + lock T1  {inr(t['lock'])}",
              f"Targets reached  +₹{lakh(t['targets'])}"]
    if acc:
        lines += ["", f"📈 Paper balance <b>₹{lakh(acc.get('balance', 0))}</b> (since {acc.get('start_date')})"]
    week_real = {}
    for day, chats in real.items():
        if datetime.strptime(day, "%Y-%m-%d").strftime("%G-W%V") == wk:
            for c, v in chats.items():
                week_real[c] = week_real.get(c, 0) + v["pnl"]
    if s.get("cancels"):
        lines += ["", "<b>Why plans weren't bought</b>"] + [f"• {esc(k)} ×{v}" for k, v in s["cancels"].items()]
    lines += ["", "<i>Paper results from 5-min candles. One week is not enough to judge — keep collecting.</i>"]
    telegram(card("🗓", f"WEEKLY REPORT · {wk}", lines))
    for c, pnl in week_real.items():
        telegram(f"🧾 <b>Your real trades this week: {inr(pnl)}</b> (before costs)", to=c)
    save(path, {"last": wk})


def fire_debate(now, trades, last_verdict_id, watch):
    """Start the cloud debate through the routine's API trigger every quarter hour,
    09:45–14:15 IST (last slot 14:15: a debate takes ~10-15 min and GOs must be in by
    14:30, so a 14:30 scan could only ever say NO TRADE, and often started after the
    routine's 14:35 cut-off and wrote nothing, setting off a false NO VERDICT alarm). The routine has no schedule of its own, so this is the only
    thing that starts debates. Called while fewer than MAX_LIVE plans are live. Returns True if it fired.
    """
    url = os.environ.get("CLAUDE_FIRE_URL")
    token = os.environ.get("CLAUDE_FIRE_TOKEN")
    minutes = now.hour * 60 + now.minute
    quarter = now.minute // 15 * 15
    slot = f"{now.hour:02d}:{quarter:02d}"
    if not url or not token:
        return False
    if not hhmm("09:45") <= minutes <= hhmm("14:29") or trades >= MAX_TRADES:
        return False
    if watch.get("last_fire") == slot:
        return False
    if last_verdict_id:  # a debate finished in the last 10 min: wait for the next slot
        v = last_verdict_id.split("_")[-1]
        if 0 <= minutes - (int(v[:2]) * 60 + int(v[2:])) < 10:
            return False
    if os.environ.get("DRY_RUN"):
        print("FIRE debate for slot", slot)
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps({"text": f"15-min scan, slot {slot} IST"}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "experimental-cc-routine-2026-04-01",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            print("fire failed:", e.code, e.read()[:300])
            watch["fire_error"] = e.code
            return False
        except Exception as e:
            print("fire failed:", e)
            watch["fire_error"] = "network"
            return False
        print("fired debate for slot", slot)
    watch["fire_error"] = None
    watch["last_fire"] = slot
    watch["fired_at"] = now.strftime("%H:%M")
    return True


def main(state_dir):
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    if today in NSE_HOLIDAYS:
        print("NSE holiday", today)
        return
    minutes = now.hour * 60 + now.minute
    plan_doc = load(os.path.join(state_dir, "plan.json"), {})
    watch_path = os.path.join(state_dir, "watch.json")
    watch = load(watch_path, {})
    if watch.get("date") != today:
        watch = {"date": today, "verdict_id": None, "live": [], "log": []}
    mt = load(os.path.join(state_dir, "mytrades.json"), {})
    PAUSED.clear()
    if mt.get("date") == today:
        PAUSED.update(mt.get("paused", []))
    health.check(now, watch, plan_doc, NSE_HOLIDAYS, telegram)
    health.connectors(load(os.path.join(state_dir, "positions.json"), {}), today,
                      watch.setdefault("health", {}), telegram)
    save(watch_path, watch)

    # 0) Live plans now live in watch["live"] (up to MAX_LIVE at once). Carry over a
    #    plan that an older watcher version was following with top-level fields.
    if "live" not in watch:
        watch["live"] = []
        p = plan_doc.get("plan") if plan_doc.get("date") == today else None
        if p and watch.get("status") in OPEN_STATES:
            v = (watch.get("verdict_id") or "_0000").split("_")[-1]
            watch["live"].append({
                "symbol": p["symbol"], "vid": watch.get("verdict_id"),
                "plan": {**p, "qty": watch.get("qty_fix") or p["qty"]},
                "status": watch["status"], "since": watch.get("since") or f"{v[:2]}:{v[2:]}",
                "paper_booked": watch.get("paper_booked", 0)})
        for k in ("status", "since", "qty_fix", "paper_booked"):
            watch.pop(k, None)

    mt_today = mt if mt.get("date") == today else {}
    ind = load(os.path.join(state_dir, "positions_ind.json"), {})
    ordered = {o["s"] for o in ind.get("orders") or [] if o.get("s")} if ind.get("date") == today else set()
    if count_trades(watch, mt_today, now, ordered):
        save(watch_path, watch)

    # 1) Forward a new verdict from the cloud debate.
    vid = plan_doc.get("verdict_id")
    if plan_doc.get("date") == today and vid and vid != watch.get("verdict_id"):
        try:
            with open(os.path.join(state_dir, "latest_verdict.txt")) as f:
                gp0 = plan_doc.get("plan") or {}
                telegram(check_groww_links(drop_groww_offline(f.read()), gp0.get("symbol")))
        except FileNotFoundError:
            telegram(card("📋", f"New verdict {esc(vid)}", ["(verdict text missing)"]))
        watch["verdict_id"] = vid
        watch["verdicts"] = watch.get("verdicts", 0) + 1
        if plan_doc.get("plan"):
            v = vid.split("_")[-1]
            start = f"{v[:2]}:{v[2:]}"
            gp = plan_doc["plan"]
            rk = risk.check(gp, start, watch.get("trades", 0))
            nw = news.check(gp.get("name") or stock_name(gp["yahoo"]) or gp["symbol"])
            lines = [""]
            reasons = list(rk["reasons"])
            if any(e["symbol"] == gp["symbol"] for e in open_plans(watch)):
                reasons.append(f"{gp['symbol']} already has a live plan")
            if len(slots(watch)) >= MAX_LIVE:
                reasons.append(f"{MAX_LIVE} plans already live")
            if reasons:
                lines += ["⛔ <b>BLOCKED — do not trade</b>", *["• " + esc(r) for r in reasons]]
            else:
                lines += [f"✅ Risk check passed · R:R {rk['rr']}",
                          f"Risk ₹{rk['risk']:,} · Value ₹{rk['value']:,}"]
                if abs(rk["qty"] - gp["qty"]) > 0.02 * max(gp["qty"], 1):
                    lines += [f"🧮 <b>Use qty {rk['qty']}</b> (not {gp['qty']})"]
                lines += ["", *order_block(gp, rk["qty"])]
                p = {**gp, "qty": rk["qty"], "start": start}
                watch.setdefault("plans", []).append(p)
                watch["live"].append({"symbol": gp["symbol"], "vid": vid, "plan": p,
                                      "status": "waiting", "since": start, "paper_booked": 0})
                n = len(slots(watch))
                if n > 1:
                    lines += [f"📚 Live plans now: <b>{n}/{MAX_LIVE}</b> "
                              f"({', '.join(esc(e['symbol']) for e in slots(watch))})"]
            lines += ["", *news.lines(nw)]
            if nw["level"] == "high" and not reasons:
                lines += ["👉 Results/event day: skip, or half size"]
            telegram(card("🛡", f"CHECKS — {esc(gp['symbol'])}", lines, footer=False))
        save(watch_path, watch)

    # 1b) End of day (15:45 IST): replay today's plans and send the scorecard once.
    # Needs at least one verdict today, so holidays and dead days stay silent.
    if (minutes >= hhmm("15:45") and (watch.get("plans") or watch.get("verdicts"))
            and not watch.get("scorecard_sent")):
        send_scorecard(state_dir, today, watch)
        watch["scorecard_sent"] = True
        save(watch_path, watch)
    if minutes >= hhmm("15:50") and watch.get("scorecard_sent"):
        weekly_report(state_dir, now)

    # 1c) You sold the stock of a live plan → that plan ends and its slot is free.
    if free_sold(watch, mt_today,
                 load(os.path.join(state_dir, "positions.json"), {}), today, now):
        save(watch_path, watch)

    # 2) Follow every live plan.
    if hhmm("09:15") <= minutes <= hhmm("15:30"):
        for e in open_plans(watch):
            track(e, now, watch)
        save(watch_path, watch)

    # 2b) Rejected stocks that broke out since → the next debate re-checks them.
    if hhmm("09:45") <= minutes <= hhmm("14:30"):
        try:
            new = recheck.update(state_dir, today, now, candles, load, save,
                                 skip={e["symbol"] for e in open_plans(watch)})
        except Exception as e:  # never let this stop the watcher
            print("recheck failed", e)
            new = []
        if new:
            rc = load(os.path.join(state_dir, "recheck.json"), {}).get("symbols", {})
            telegram(card("🔁", "RE-CHECK NEXT SCAN", [""] + [
                f"• <b>{esc(s)}</b> closed {rs(rc[s]['close'])} above its {rc[s]['kind']} "
                f"{rs(rc[s]['level'])} at {rc[s]['at']} (rejected {rc[s]['rejected_at']})" for s in new
            ] + ["", "It was rejected earlier; the next scan looks at it again."], footer=False))

    # 3) Keep scanning while there is room for another plan.
    count_trades(watch, mt_today, now, ordered)
    save(watch_path, watch)
    live = slots(watch)
    trades = watch.get("trades", 0)  # bought / ordered today (+ just-triggered plans)
    taken = set(watch.get("taken", []))
    waiting = sum(1 for e in live if e["status"] == "waiting" and e["symbol"] not in taken)
    loss_stop = watch.get("day_pnl", 0) <= -DAY_LOSS_STOP
    full = trades + waiting >= MAX_TRADES or len(live) >= MAX_LIVE
    late = minutes >= hhmm("14:40")  # last debate (14:15 slot) has reported
    if not live:
        print("no live plan")
        if ((trades >= MAX_TRADES or late or loss_stop) and not watch.get("done_sent")
                and minutes <= hhmm("15:30")):
            why = ([f"Paper loss today {inr(watch['day_pnl'])}", "(daily loss stop), none live now."]
                   if loss_stop else
                   [f"{MAX_TRADES} trades bought today", "(daily limit), none live now."]
                   if trades >= MAX_TRADES else ["No new entries after 14:30.", "No plan live."])
            telegram(card("🏁", f"DONE FOR TODAY · {now:%H:%M}", [
                "", *why, f"Trades bought: {trades}", "",
                "🔕 No more scans today.", "🔎 Next scan: Mon–Fri 09:45 IST",
            ]))
            watch["done_sent"] = True
            save(watch_path, watch)
    else:
        print("live:", ", ".join(f"{e['symbol']} {e['status']}" for e in live))
    if not full and not loss_stop:
        if fire_debate(now, trades + waiting, vid if plan_doc.get("date") == today else None, watch):
            save(watch_path, watch)


def free_sold(watch, mt, pos, today, now):
    """End each live plan whose stock you bought and have now fully sold (your
    SOLD message in mytrades.json, or Groww positions showing it closed), so the
    scans can look for a new trade. Returns True if any plan was freed."""
    freed = []
    rows = {r.get("symbol"): r for r in (pos.get("positions") or [])} if pos.get("date") == today else {}
    for e in slots(watch):
        mine = [t for ts in (mt.get("trades") or {}).values() for t in ts
                if t.get("symbol") == e["symbol"] and (t.get("opened") or "") >= e["since"]]
        r = rows.get(e["symbol"])
        sold = ((mine and all(t.get("left", 1) <= 0 for t in mine))
                or (r and r.get("buy_qty", 0) > 0 and r.get("sell_qty", 0) > 0 and r.get("net_qty", 1) == 0))
        if sold and not any(t.get("left", 0) > 0 for t in mine):
            e["user_sold"] = now.strftime("%H:%M")  # paper tracking goes on, slot is free
            freed.append(e["symbol"])
    if freed:
        left = slots(watch)
        telegram(card("🔓", f"SLOT FREED — {esc(', '.join(freed))}", [
            "", "You sold it, so its slot is free for a new trade.",
            "(I keep paper-tracking the plan quietly for the scorecard.)",
            f"📚 Live plans now: <b>{len(left)}/{MAX_LIVE}</b>"
            + (f" ({', '.join(esc(e['symbol']) for e in left)})" if left else ""),
            "🔎 Scans for a new trade resume (until 14:30, if the daily limits allow).",
        ], footer=False))
    return bool(freed)


def bought_symbols(mt):
    """{symbol: earliest open time} of every real trade today (your BOUGHT messages,
    INDmoney auto-tracking, Groww sync), any chat."""
    out = {}
    for ts in (mt.get("trades") or {}).values():
        for t in ts:
            sym, at = t.get("symbol"), t.get("opened") or "00:00"
            if sym and (sym not in out or at < out[sym]):
                out[sym] = at
    return out


def count_trades(watch, mt, now, ordered=frozenset()):
    """Set watch["trades"] (the daily limit counter, also read by the cloud debate) to
    the trades you really bought, not plans that merely hit their buy trigger:
      * every stock you bought today counts once (bot plan or not), and so does
        every stock with a BUY order placed in INDmoney but not filled yet (`ordered`);
      * a triggered plan you haven't bought counts only for MISS_GRACE minutes
        (your fill can take a minute or two to show up). After that it is marked
        missed: its slot is freed and it is paper-tracked quietly. If you buy it
        later after all, it counts again and its alerts come back.
    Returns True if anything changed."""
    t = now.strftime("%H:%M")
    mins = now.hour * 60 + now.minute
    bought = bought_symbols(mt)
    old = (watch.get("trades"), watch.get("taken"), [(e.get("missed"), e.get("triggered")) for e in watch.get("live", [])])
    pending, freed = 0, []
    for e in watch.get("live", []):
        if e.get("status") not in ("in_zone", "t1_hit", "closed") or e.get("user_sold"):
            continue
        if e.get("status") == "closed" and not e.get("triggered"):
            continue  # closed before this rule existed: leave as is
        trig = e.setdefault("triggered", t)
        mine = (e["symbol"] in ordered
                or (e["symbol"] in bought and bought[e["symbol"]] >= e.get("since", "00:00")))
        if mine:
            if e.pop("missed", None):
                print("missed plan bought after all", e["symbol"])
        elif not e.get("missed"):
            if mins - hhmm(trig) >= MISS_GRACE:
                e["missed"] = t
                if e.get("status") in OPEN_STATES:
                    freed.append(e["symbol"])
            else:
                pending += 1
    taken = set(bought) | set(ordered)
    watch["taken"] = sorted(taken)
    watch["trades"] = len(taken) + pending
    if freed:
        left = slots(watch)
        telegram(card("🔓", f"SLOT FREED — {esc(', '.join(freed))}", [
            "", f"Buy trigger hit, but no buy or open order of it in {MISS_GRACE} min,",
            "so it doesn't count as a trade and its slot is free.",
            "(Bought it after all? It counts again by itself,",
            "and SL / target alerts for it still come.)",
            f"📚 Trades bought today: <b>{watch['trades']}/{MAX_TRADES}</b>",
            f"📚 Live plans now: <b>{len(left)}/{MAX_LIVE}</b>"
            + (f" ({', '.join(esc(e['symbol']) for e in left)})" if left else ""),
        ], footer=False))
    return freed or old != (watch.get("trades"), watch.get("taken"), [(e.get("missed"), e.get("triggered")) for e in watch.get("live", [])])


def open_plans(watch):
    """Every plan still being tracked (alerts, paper result)."""
    return [e for e in watch.get("live", []) if e.get("status") in OPEN_STATES]


def slots(watch):
    """Open plans that still take a slot: not ones you already sold or never bought."""
    return [e for e in open_plans(watch) if not e.get("user_sold") and not e.get("missed")]


def track(entry, now, watch):
    """One 5-min check of one live plan: send alerts, update entry in place."""
    plan = entry["plan"]
    minutes = now.hour * 60 + now.minute
    sym = esc(plan["symbol"])
    bars, ltp = candles(plan["yahoo"])
    if not bars:
        print("no candles yet", plan["symbol"])
        return
    # Candles that closed after the verdict (ignore the one still forming).
    closed = [b for b in bars if b[0] + timedelta(minutes=5) <= now]
    vwap_num = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in bars)
    vol = sum(b[5] for b in bars)
    vwap = vwap_num / vol if vol else None
    last = closed[-1] if closed else None
    since = entry.get("since") or "00:00"
    new = [b for b in closed if b[0].hour * 60 + b[0].minute >= hhmm(since)]
    price = ltp if ltp is not None else bars[-1][4]
    vw = rs(vwap) if vwap else "n/a"
    price_line = f"💹 Now <b>{rs(price)}</b>  ·  VWAP {vw}"
    t = f"{now:%H:%M}"
    status = entry["status"]
    old_status = status
    exit_px = None  # price the paper position is closed at, when it closes
    msg = None

    if status == "waiting":
        buy_now = plan.get("entry_type") == "now"
        if buy_now:  # BUY NOW plan: buy at once while the price is inside the zone
            ok = plan["zone_low"] <= price <= plan["max_entry"]
            hit_zone = trigger = [(now, price, price, price, price, 0)] if ok else []
        else:
            hit_zone = [b for b in new if b[3] <= plan["zone_high"]]
            trigger = [b for b in hit_zone if b[4] > b[1] and b[4] > plan["trigger_close_above"]
                       and b[4] <= plan["max_entry"]]
        cancel = None
        if minutes >= hhmm(plan["deadline"]) and not trigger:
            cancel = f"Not triggered by {plan['deadline']}"
        elif any(b[2] >= plan["cancel_if_high_reaches"] for b in new) and not hit_zone:
            cancel = f"Ran to {rs(plan['cancel_if_high_reaches'])} without the dip"
        elif any(b[4] < plan["cancel_if_close_below"] for b in new) and not trigger:
            cancel = f"5-min close below {rs(plan['cancel_if_close_below'])}"
        if cancel:
            status = "cancelled"
            msg = card("❌", f"CANCELLED — {sym}", [
                f"🕐 {t} IST", "",
                f"Reason: {cancel}", price_line, "",
                "👉 <b>Do not buy.</b> Delete the Groww price alert.",
                "🔎 Next cloud scan will look for a new setup.",
            ])
        elif trigger and watch.get("trades", 0) >= MAX_TRADES:
            status = "cancelled"
            msg = card("❌", f"SKIP — {sym}", [
                f"🕐 {t} IST", "", f"{MAX_TRADES} trades already bought today.", price_line, "",
                "👉 <b>Do not buy.</b> Delete the Groww price alert.",
            ])
        elif trigger:
            status = "in_zone"
            entry["triggered"] = t
            watch["trades"] = watch.get("trades", 0) + 1  # until count_trades sees your buy
            msg = card("🔔", f"{'BUY NOW' if buy_now else 'BUY TRIGGER'} — {sym}", [
                f"🕐 {t} IST", "",
                (f"✅ Price {rs(price)} is in the buy-now zone" if buy_now
                 else f"✅ Green 5-min close {rs(trigger[-1][4])}"),
                price_line, "",
                f"📥 Buy  <b>~{rs(plan['entry'])}</b>  (max {rs(plan['max_entry'])})",
                *plan_lines(plan), "", *order_block(plan), "",
                *news.lines(news.check(plan.get("name") or stock_name(plan["yahoo"]) or plan["symbol"], 6)),
                "",
                "👉 Place SL-M right after the fill.",
            ])
    elif status == "in_zone":
        if any(b[3] <= plan["sl"] for b in new) or price <= plan["sl"]:
            status = "closed"
            exit_px = plan["sl"]
            msg = card("🔴", f"STOP LOSS HIT — {sym}", [
                f"🕐 {t} IST", "", f"SL {rs(plan['sl'])} touched", price_line, "",
                "👉 <b>Exit now</b> if still holding.",
            ])
        elif any(b[2] >= plan["t1"] for b in new) or price >= plan["t1"]:
            status = "t1_hit"
            msg = card("🎯", f"TARGET 1 HIT — {sym}", [
                f"🕐 {t} IST", "", f"T1 {rs(plan['t1'])} reached", price_line, "",
                "👉 <b>Sell half</b> now.",
                f"👉 Edit SL-M: qty <b>{plan['qty'] - plan['qty'] // 2}</b> (what's left), "
                f"trigger cost <b>{rs(plan['entry'])}</b>.",
                f"🎯 Next: T2 {rs(plan['t2'])}",
            ])
        elif last and last[4] < plan["exit_if_close_below_after_entry"]:
            status = "closed"
            exit_px = last[4]
            msg = card("⚠️", f"EARLY EXIT — {sym}", [
                f"🕐 {t} IST", "",
                f"5-min close {rs(last[4])} below {rs(plan['exit_if_close_below_after_entry'])}",
                price_line, "", "👉 <b>Exit</b> before the stop is hit.",
            ])
    elif status == "t1_hit":
        if any(b[2] >= plan["t2"] for b in new) or price >= plan["t2"]:
            status = "closed"
            exit_px = plan["t2"]
            msg = card("🏁", f"TARGET 2 HIT — {sym}", [
                f"🕐 {t} IST", "", f"T2 {rs(plan['t2'])} reached", price_line, "",
                "👉 <b>Sell the rest.</b> Trade complete.",
            ])
        elif price <= plan["entry"]:
            status = "closed"
            exit_px = plan["entry"]
            msg = card("↩️", f"BACK AT COST — {sym}", [
                f"🕐 {t} IST", "", price_line, "",
                f"👉 <b>Exit the rest</b> at {rs(plan['entry'])}. Half was booked at T1.",
            ])
    ts = min(plan.get("time_stop", EXIT_BY), EXIT_BY)
    if status in ("in_zone", "t1_hit") and minutes >= hhmm(ts):
        old_status = status
        status = "closed"
        exit_px = price
        msg = card("⏰", f"TIME STOP — {sym}", [
            f"🕐 {t} IST", "", price_line, "",
            f"👉 <b>Exit everything</b> — {ts} rule.",
        ])

    # Live paper trade: the plan's qty, bought at entry, half at T1, rest at the exit.
    if msg and status != old_status and old_status in ("in_zone", "t1_hit"):
        q, e = plan["qty"], plan["entry"]
        half = q // 2
        if status == "t1_hit":
            entry["paper_booked"] = round(half * (plan["t1"] - e))
            note = f"📈 Paper: {inr(entry['paper_booked'])} booked on {half} shares"
        else:
            booked = entry.get("paper_booked", 0) if old_status == "t1_hit" else 0
            left = q - half if old_status == "t1_hit" else q
            res = round(booked + left * (exit_px - e))
            entry["paper_result"] = res
            watch["day_pnl"] = watch.get("day_pnl", 0) + res
            note = f"📈 Paper trade result: <b>{inr(res)}</b> (before costs)"
        msg = msg.replace("\n\n" + FOOTER, "\n" + note + "\n\n" + FOOTER)
    if msg and status in ("closed", "cancelled"):
        msg = msg.replace("\n\n" + FOOTER,
                          f"\n➖ Remove {sym} from \"My Draft Basket\"\n\n" + FOOTER)
    watch["log"].append(f"{t} {plan['symbol']} {price:.2f} VWAP {vw} status {status}")
    if entry.get("user_sold"):
        pass  # you already sold: track quietly for the paper result, no alerts
    elif msg and entry.get("missed"):
        # No buy seen, but still send SL / target / exit cards: if your buy just
        # didn't show up (sync down, other broker), these are the ones that matter.
        telegram(msg.replace("\n\n" + FOOTER, "\nℹ️ No buy of this seen. Not holding it? Ignore.\n\n" + FOOTER))
    elif msg:
        telegram(msg)
    elif entry.get("missed"):
        pass  # no 15-min status board for a plan you didn't buy
    elif now.minute % 15 < 5:
        n = len(slots(watch))
        telegram(card("📊", f"{sym}  ·  {t}" + (f"  ·  {n} live" if n > 1 else ""), [
            price_line,
            f"Status: {STATUS_LABEL.get(status, status)}",
            "",
            f"📥 Zone  {rs(plan['zone_low'])}–{rs(plan['zone_high'])}",
            f"🛑 SL {rs(plan['sl'])}  ·  🎯 T1 {rs(plan['t1'])}",
            f"⌛ Cancel by {plan['deadline']}" if status == "waiting" else f"⏰ Exit by {ts}",
        ], footer=False))
    entry["status"] = status
    watch["log"] = watch["log"][-200:]


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "state")
