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
import ind
import opening
import news
import premarket
import prescan
import recheck
import risk
import scan
import shorts
import tables as T

PAUSED = set()  # chats that sent PAUSE today (broadcasts skip them)

IST = timezone(timedelta(hours=5, minutes=30))
FOOTER = "<i>Analysis only, not advice. You place orders yourself.</i>"
OPEN_STATES = ("waiting", "in_zone", "t1_hit")
EXIT_BY = "15:00"  # out before the broker's intraday auto square-off (and its fee)
WARN_AT = "14:50"
MAX_LIVE = 3        # plans followed at the same time
MAX_TRADES = 3      # trades open at the same time: held or BUY order pending (count_trades)
MAX_DAY = 6         # safety cap: stocks bought / ordered per day in total
RULES_VERSION = "2026-11-16"  # bump on any rule change; expectancy is split per version
MAX_HEAT = 2000     # ₹: total risk to SL of plans waiting / bought at once (2% of ₹1 lakh)
EVENT_RR = 2.0      # net R:R needed on event days (RBI, CPI, big results…)
VIX_SPIKE = 10.0    # % India VIX up on the day → no new entries
WEAK_ABOVE = 35     # % of stocks above VWAP below which the market counts as weak for longs
WEAK_CONF = 6       # min Judge confidence (/10) for a long on a weak day
WEAK_RS = 1.0       # stock must be ≥ this % better than Nifty on a weak day
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


IND_ALLOW = set()   # NSE symbols that may use INDmoney candles this run (live plans, premarket, new GO)
IND_CACHE = {}      # symbol → 5-day 5-min bars from INDmoney, fetched once per run in one batch


def candles(yahoo_symbol, days="1d", fast=True):
    """Today's 5-min candles as (time_ist, open, high, low, close, volume), last one may be forming.
    days="5d" also returns the previous sessions (premarket gap / volume checks).
    fast: NSE stocks come from INDmoney through the worker (WORKER_URL + WORKER_KEY,
    read-only market data) when set, else / on any failure from Yahoo. The scanner
    passes fast=False (234 stocks: Yahoo, so the INDmoney limits stay free for alerts)."""
    if (fast and yahoo_symbol.endswith(".NS") and yahoo_symbol[:-3] in IND_ALLOW
            and os.environ.get("WORKER_URL") and os.environ.get("WORKER_KEY")):
        try:
            return ind_candles(yahoo_symbol[:-3], days)
        except Exception as e:
            print("INDmoney candles failed, using Yahoo:", yahoo_symbol, e)
    return yahoo_candles(yahoo_symbol, days)


def ind_batch(symbols):
    """One worker call for up to 20 symbols (the worker asks INDmoney 5 at a time, with
    back-off on HTTP 429). Fills IND_CACHE; 13 Nov: one call per symbol hit the limit."""
    want = sorted(s for s in symbols if s not in IND_CACHE)[:20]
    if not want:
        return
    url = (os.environ["WORKER_URL"].rstrip("/") + "/?candles="
           + urllib.parse.quote(",".join(want)) + "&days=5")
    req = urllib.request.Request(url, headers={"x-worker-key": os.environ["WORKER_KEY"],
                                               "User-Agent": "intraday-watcher"})
    with urllib.request.urlopen(req, timeout=40) as r:
        doc = json.load(r)
    if not doc.get("ok"):
        for s in want:
            IND_CACHE[s] = None            # don't retry this run; Yahoo serves it
        raise RuntimeError(doc.get("error") or "no candles")
    for s in want:
        rows = (doc.get("candles") or {}).get(s)
        IND_CACHE[s] = [(datetime.fromtimestamp(ts, IST), o, h, l, c, v)
                        for ts, o, h, l, c, v in rows] if rows else None


def ind_candles(sym, days="1d"):
    if sym not in IND_CACHE:
        ind_batch(IND_ALLOW | {sym})
    out = IND_CACHE.get(sym)
    if not out:
        raise RuntimeError("no INDmoney candles")
    if days == "1d":
        out = [b for b in out if b[0].date() == datetime.now(IST).date()]
    if not out:
        raise RuntimeError("no candles today")
    return out, out[-1][4]


def yahoo_candles(yahoo_symbol, days="1d"):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(yahoo_symbol)
        + "?interval=5m&range=" + days
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


def order_block(plan, qty=None, confirmed=False):
    """Tap-to-copy order values (the user places the order in INDmoney; nothing is sent).
    A dip plan is not bought until the 🔔 BUY TRIGGER (green 5-min close back above the
    trigger): a buy limit placed in advance fills while the price is still falling
    (13 Nov: WAAREEENER and IIFLCAPS filled on the way down, −₹1,182; the plan itself
    never triggered). So before the trigger the card says wait; at the trigger it gives
    the order with the limit at max entry."""
    q = qty or plan["qty"]
    dip = plan.get("entry_type") != "now"
    if dip and not confirmed:
        return [
            "📝 <b>Your order</b>",
            "⏳ <b>Don't place the buy yet.</b> No limit order in advance:",
            "it fills while the price is still falling.",
            f"Wait for 🔔 <b>BUY TRIGGER</b> (green 5-min close back above "
            f"<code>{plan.get('trigger_close_above', plan['entry']):g}</code>), then buy:",
            f"Qty <code>{q}</code> · Limit <code>{plan['max_entry']:g}</code> · "
            f"SL-M <code>{plan['sl']:g}</code> · T1 sell {q // 2} @ <code>{plan['t1']:g}</code>",
            f"📲 INDmoney › search <code>{esc(plan['symbol'])}</code>",
        ]
    buy = plan["max_entry"]
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
    rows_ = [["Balance", f"{lakh(bal)}", f"{chg:+.2f}%"],
             ["Today", T.num(today_pnl), f"{len(acc['days'][today])} trades"]]
    if trades:
        rows_ += [["All trades", str(len(trades)), f"{wins * 100 // len(trades)}% win"],
                  ["Worst trade", T.num(min(min(trades), 0)), f"DD {dd * 100:.1f}%"]]
    lines = [f"<b>📈 Paper account</b> (plan rules, since {acc['start_date']})",
             T.table(["", "₹", ""], rows_, right={1, 2})]
    if not trades:
        lines.append("No trades yet — no plan has hit its buy trigger.")
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
    tot = {k: sum(r.get(k, {}).get("pnl", 0) for r in rows) for k in ("rules", "limit", "market", "lock", "trail")}
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
    else:
        yn = lambda ok: "Y" if ok else "-"
        lines += ["<b>Plans</b> (₹, after costs)", T.table(
            ["Plan", "Time", "Rules", "AtOnce"],
            [[T.short(x["symbol"]), x["start"], T.num(x["rules"]["pnl"]), T.num(x["market"]["pnl"])] for x in rows],
            total=["Total", "", T.num(tot["rules"]), T.num(tot["market"])]),
            "<b>Price reached</b> (after the plan)", T.table(
            ["Plan", "Entry", "T1", "T2", "High"],
            [[T.short(x["symbol"]), yn(x["targets"]["entry_hit"]), yn(x["targets"]["t1_hit"]),
              yn(x["targets"]["t2_hit"]), f"{x['targets']['high']:g}"] for x in rows if "targets" in x]), ""]
    lines += ["<b>Ways to take the plans</b>", T.table(
        ["Way", "Today", f"{len(hist)} days"],
        [["Plan rules", T.num(tot["rules"]), T.num(run["rules"])],
         ["Limit only", T.num(tot["limit"]), T.num(run["limit"])],
         ["At once", T.num(tot["market"]), T.num(run["market"])],
         ["Lock T1", T.num(tot["lock"]), T.num(run["lock"])],
         ["Trail T1", T.num(tot["trail"]), T.num(sum(d["totals"].get("trail", 0) for d in hist))],
         ["Targets*", T.num(tot["targets"]), T.num(run["targets"])]]), ""]
    lines += paper_account(state_dir, today, rows) + [""]
    extra = []
    try:
        op = os.path.join(state_dir, "opening.json")
        od = load(op, {})
        ores = od.setdefault("days", {}).get(today, [])
        if od.get("date") == today and od.get("open"):
            ores = []
            for t in od["open"]:
                try:
                    ores.append(opening.replay(t, candles(t["yahoo"])[0]))
                except Exception as e:
                    print("opening replay failed", t["symbol"], e)
            od["days"][today], od["open"] = ores, []
            save(op, od)
        ol = opening.lines(od, ores, inr)
        if ol:
            lines += ol + [""]
        extra.append(("opening", ores))
    except Exception as e:  # never block the scorecard
        print("opening lines failed", e)
    try:
        sp = os.path.join(state_dir, "shorts.json")
        sd = load(sp, {})
        res = shorts.close_day(sd, today, candles)
        save(sp, sd)
        sl = shorts.lines(sd, today, res, inr)
        if sl:
            lines += sl + [""]
        extra.append(("shorts", res))
    except Exception as e:  # never block the scorecard
        print("shorts failed", e)
    try:
        import expectancy
        lines += ["<b>🎯 Expectancy</b> (all days so far)"] + expectancy.lines(state_dir, short=True) + [""]
    except Exception as e:  # never block the scorecard
        print("expectancy failed", e)
    lines += scorecard_insights(rows, tot, dict(extra), load(os.path.join(state_dir, "candidates.json"), {}), today)
    lines += [
        "",
        "<i>At once = bought at market when the plan came. Lock T1 = SL to T1 halfway to T2. "
        "Trail T1 = after T1 the rest trails 1× ATR. *Targets = levels the price really reached, before costs. "
        "Replay on 5-min candles, ~0.12% costs + slippage. Paper results, not real trades.</i>",
    ]
    telegram(card("📒", f"SCORECARD · {today}", lines))


def scorecard_insights(rows, tot, extra, cand, today):
    """💡 lines worked out from today's numbers, so the card says what they mean."""
    out = []
    names = {"rules": "plan rules", "limit": "a plain limit order", "market": "buying at once",
             "lock": "at once + lock T1", "trail": "trailing after T1"}
    if rows:
        best = max(names, key=lambda k: tot.get(k, 0))
        if best != "rules" and tot[best] - tot["rules"] >= 300:
            out.append(f"💡 Best way today: {names[best]} ({inr(tot[best])} vs plan rules {inr(tot['rules'])}).")
        ran = [x["symbol"] for x in rows if "without the dip" in " ".join(x["rules"]["steps"])]
        fell = [x["symbol"] for x in rows if "below cancel" in " ".join(x["rules"]["steps"])]
        if ran:
            hit = [x["symbol"] for x in rows if x["symbol"] in ran and x["targets"]["t1_hit"]]
            out.append(f"💡 {len(ran)} dip plan(s) ran up without the dip"
                       + (f"; {len(hit)} still reached T1 ({esc(', '.join(hit))})." if hit else "."))
        if fell:
            out.append(f"💡 {len(fell)} dip plan(s) kept falling ({esc(', '.join(fell))}): "
                       "buying before the trigger would have lost.")
        won = sum(1 for x in rows if x["rules"].get("bought") and x["rules"]["pnl"] > 0)
        lost = sum(1 for x in rows if x["rules"].get("bought") and x["rules"]["pnl"] < 0)
        if won or lost:
            out.append(f"💡 Bought by the rules: {won} won, {lost} lost, {inr(tot['rules'])}.")
    elif cand.get("date") == today:
        out.append(f"💡 No GO: market {cand.get('mode', '?')}, {cand.get('above_vwap_pct', '?')}% of stocks "
                   f"above VWAP at {cand.get('at', '?')}. Sitting out is fine.")
    o = extra.get("opening") or []
    if o:
        tr = sum(x["r"] for x in o)
        out.append(f"💡 Opening test: {len(o)} paper trade(s), {tr:+.2f}R ({inr(tr * 1000)} at ₹1,000 risk).")
    sh = extra.get("shorts") or []
    if sh:
        out.append(f"💡 Short test: {len(sh)} paper short(s), {inr(sum(x['pnl'] for x in sh))}.")
    return out


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
    lines += ["<b>Day by day</b> (paper, ₹)", T.table(
        ["Day", "Plans", "Rules", "AtOnce"],
        [[datetime.strptime(d["date"], "%Y-%m-%d").strftime("%a %d"), str(len(d.get("plans", []))),
          T.num(d["totals"].get("rules", 0)), T.num(d["totals"].get("market", 0))] for d in hist],
        total=["Week", str(s["plans"]), T.num(t["rules"]), T.num(t["market"])])]
    if s["plans"]:
        n = s.get("with_targets") or 1
        lines += ["<b>How the plans went</b>", T.table(
            ["", "Count", "of"],
            [["Bought (rules)", str(s["bought"]), str(s["plans"])], ["  won", str(s["wins"]), str(s["bought"])],
             ["  lost", str(s["losses"]), str(s["bought"])], ["Entry reached", str(s["entry_fill"]), str(n)],
             ["T1 reached", str(s["t1_hit"]), str(n)], ["T2 reached", str(s["t2_hit"]), str(n)]])]
    lines += ["<b>Week by way of taking the plans</b>", T.table(
        ["Way", "₹"], [["Plan rules", T.num(t["rules"])], ["Limit only", T.num(t["limit"])],
                       ["At once", T.num(t["market"])], ["Lock T1", T.num(t["lock"])],
                       ["Trail T1", T.num(t.get("trail", 0))], ["Targets*", T.num(t["targets"])]])]
    if acc:
        lines += [f"📈 Paper balance <b>₹{lakh(acc.get('balance', 0))}</b> (since {acc.get('start_date')})"]
    best = max(("rules", "limit", "market", "lock"), key=lambda k: t.get(k, 0))
    if s["plans"] and best != "rules" and t[best] - t["rules"] >= 500:
        lines.append(f"💡 This week {dict(limit='a plain limit order', market='buying at once', lock='at once + lock T1')[best]} "
                     f"beat the plan rules by {inr(t[best] - t['rules'])}.")
    week_real = {}
    for day, chats in real.items():
        if datetime.strptime(day, "%Y-%m-%d").strftime("%G-W%V") == wk:
            for c, v in chats.items():
                week_real[c] = week_real.get(c, 0) + v["pnl"]
    if s.get("cancels"):
        lines += ["", "<b>Why plans weren't bought</b>"] + [f"• {esc(k)} ×{v}" for k, v in s["cancels"].items()]
    lines += ["", "<i>Paper results from 5-min candles. One week is not enough to judge — keep collecting.</i>"]
    telegram(card("🗓", f"WEEKLY REPORT · {wk}", lines))
    import expectancy
    for c, pnl in week_real.items():
        telegram(f"🧾 <b>Your real trades this week: {inr(pnl)}</b> (before costs) · "
                 f"paper by the rules {inr(t['rules'])}", to=c)
    for c in {c for d in real.values() for c in d} or [None]:
        try:
            telegram(card("🎯", "EXPECTANCY · is the edge real?",
                          [""] + expectancy.lines(state_dir, chat=c), footer=False), to=c)
        except Exception as e:
            print("expectancy failed", c, e)
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
    if not post_fire(url, token, f"15-min scan, slot {slot} IST", watch):
        return False
    watch["last_fire"] = slot
    watch["fired_at"] = now.strftime("%H:%M")
    return True


# Before the open: (key, from, to, routine text). Fired once each per day.
PRE_FIRES = (("pre", "08:40", "08:59", "PREMARKET overnight scan"),
             ("open", "09:05", "09:13", "PREOPEN check"))


def fire_premarket(now, watch):
    """Start the two premarket cloud runs (overnight list ~08:45, pre-open check ~09:08).
    Needs the pinger to start the watcher from 08:40. Returns True if it fired."""
    url = os.environ.get("CLAUDE_FIRE_URL")
    token = os.environ.get("CLAUDE_FIRE_TOKEN")
    if not url or not token or now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    done = watch.setdefault("pre_fired", [])
    for key, a, b, text in PRE_FIRES:
        if hhmm(a) <= minutes <= hhmm(b) and key not in done:
            if post_fire(url, token, f"{text}, {now:%H:%M} IST", watch):
                done.append(key)
                return True
    return False


def fire_late_recheck(now, state_dir, today, watch):
    """A rejected stock that broke out after the last scan (14:15) used to be lost
    (PAYTM 12 Nov, 14:20). One short re-check run, 14:16–14:22, only for those."""
    url = os.environ.get("CLAUDE_FIRE_URL")
    token = os.environ.get("CLAUDE_FIRE_TOKEN")
    minutes = now.hour * 60 + now.minute
    if not url or not token or not hhmm("14:16") <= minutes <= hhmm("14:22") or watch.get("late_fired"):
        return False
    rc = load(os.path.join(state_dir, "recheck.json"), {})
    syms = [s for s, v in (rc.get("symbols") or {}).items()
            if rc.get("date") == today and v.get("at", "") >= "14:10"]
    if not syms:
        return False
    if not post_fire(url, token, f"RECHECK late breakout: {' '.join(syms)}, {now:%H:%M} IST", watch):
        return False
    watch["late_fired"] = now.strftime("%H:%M")
    watch["fired_at"] = now.strftime("%H:%M")
    return True


def post_fire(url, token, text, watch):
    """POST to the routine's API trigger. False (and watch['fire_error']) on failure."""
    if os.environ.get("DRY_RUN"):
        print("FIRE", text)
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps({"text": text}).encode(),
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
        print("fired:", text)
    watch["fire_error"] = None
    return True


def main(state_dir):
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    if today in NSE_HOLIDAYS:
        print("NSE holiday", today)
        wp = os.path.join(state_dir, "watch.json")
        if load(wp, {}).get("date") != today:   # tells the worker's dead-man alert to stay quiet
            save(wp, {"date": today, "holiday": True, "live": [], "log": []})
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

    IND_CACHE.clear()
    IND_ALLOW.clear()
    IND_ALLOW.update(e["symbol"] for e in open_plans(watch))
    pm = load(os.path.join(state_dir, "premarket.json"), {})
    if pm.get("date") == today and hhmm("09:15") <= minutes <= hhmm("09:45"):
        IND_ALLOW.update(s["symbol"] for s in pm.get("stocks") or [] if s.get("keep") is not False)
    mt_today = mt if mt.get("date") == today else {}
    ind = load(os.path.join(state_dir, "positions_ind.json"), {})
    ordered = {o["s"] for o in ind.get("orders") or [] if o.get("s")} if ind.get("date") == today else set()
    if count_trades(watch, mt_today, now, ordered):
        save(watch_path, watch)
    if sl_nudge(ind, mt_today, today, minutes, watch):
        save(watch_path, watch)
    pm_doc = load(os.path.join(state_dir, "premarket.json"), {})
    if pm_doc.get("connectors"):   # 08:45 run: catch an expired connector before the open
        health.connectors(pm_doc, today, watch.setdefault("health", {}), telegram)

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
            gp = dict(plan_doc["plan"])
            IND_ALLOW.add(gp["symbol"])
            conv = []
            try:
                bars, ltp = candles(gp["yahoo"])
            except Exception as e:
                print("candles failed", e)
                bars, ltp = [], None
            a = scan.atr5(bars, now)
            if a:
                gp["atr5"] = a
            # Trend day / stock trending and already above the dip zone: buy now instead
            # of waiting for a dip that rarely comes (6–12 Nov: 13 of 24 dips never filled).
            price = ltp if ltp is not None else (bars[-1][4] if bars else None)
            mode = load(os.path.join(state_dir, "candidates.json"), {})
            mode = mode.get("mode") if mode.get("date") == today else None
            if (gp.get("entry_type") == "dip" and price and price > gp["zone_high"]
                    and (mode == "trend" or scan.stock_trend(bars, now))):
                p2 = chase_plan(gp, price, now, bars, watch)
                if p2:
                    gp = p2
                    conv = ["📈 <b>Trend " + ("day" if mode == "trend" else "stock") + ": BUY NOW</b>",
                            f"Price {rs(price)} is above the dip zone;",
                            "buy here, same SL / targets (R:R still ≥ 1.5).", ""]
            rk = risk.check(gp, start, watch.get("trades", 0))
            if watch.get("day_trades", 0) >= MAX_DAY:
                rk["reasons"].append(f"{MAX_DAY} trades already taken today")
            nw = news.check(gp.get("name") or stock_name(gp["yahoo"]) or gp["symbol"])
            lines = [""]
            reasons = list(rk["reasons"])
            if any(e["symbol"] == gp["symbol"] for e in open_plans(watch)):
                reasons.append(f"{gp['symbol']} already has a live plan")
            if len(slots(watch)) >= MAX_LIVE:
                reasons.append(f"{MAX_LIVE} plans already live")
            cd = load(os.path.join(state_dir, "candidates.json"), {})
            if not reasons and cd.get("date") == today:
                reasons += weak_rule(gp, cd, bars, now)
            if not reasons:
                reasons += guard(watch, {**gp, "qty": rk["qty"]}, state_dir, today, rk["rr"])
                gp["sector"] = scan.group(gp["symbol"], scan.load_universe(state_dir))
                gp["rules_v"] = RULES_VERSION
            if reasons:
                lines += ["⛔ <b>BLOCKED — do not trade</b>", *["• " + esc(r) for r in reasons]]
            else:
                lines += [f"✅ Risk check passed · R:R {rk['rr']}",
                          f"Risk ₹{rk['risk']:,} · Value ₹{rk['value']:,}"]
                if abs(rk["qty"] - gp["qty"]) > 0.02 * max(gp["qty"], 1):
                    lines += [f"🧮 <b>Use qty {rk['qty']}</b> (not {gp['qty']})"]
                lines += ["", *conv, *order_block(gp, rk["qty"])]
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
    if free_sold(watch, mt_today, today, now):
        save(watch_path, watch)

    # 1d) Premarket watchlist: forward it, start the pre-open runs, then from 09:20
    #     turn a first-candle breakout into a live BUY NOW plan.
    if premarket_step(state_dir, today, now, watch):
        save(watch_path, watch)
    if nifty_step(state_dir, today, watch):   # Nifty options paper verdicts (no plans, no orders)
        save(watch_path, watch)
    if opening_step(state_dir, today, now, watch):
        save(watch_path, watch)
    cues_step(state_dir, today, minutes, watch)   # before the 08:40 premarket fire
    prescan_step(state_dir, today, minutes, watch)
    if fire_premarket(now, watch):
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

    # 2c) Code scanner: rank the whole liquid universe once per quarter hour, just
    #     before the debate fires, so the Bull starts from the best movers.
    quarter = f"{now.hour:02d}:{now.minute // 15 * 15:02d}"
    if hhmm("09:40") <= minutes <= hhmm("14:25") and watch.get("scan_slot") != quarter:
        try:
            cand = scan.run(state_dir, now, lambda s, d="1d": candles(s, d, False))
            save(os.path.join(state_dir, "candidates.json"), cand)
            watch["scan_slot"] = quarter
            print("scan:", cand["mode"], [i["symbol"] for i in cand["items"]])
            sp = os.path.join(state_dir, "shorts.json")
            sd = load(sp, {})
            if shorts.record(sd, cand, now, today):   # paper test only, nothing is sent
                save(sp, sd)
            if ind.worker_on():
                try:   # data pack for the cloud debate (INDmoney API, no connector needed)
                    live_syms = [e["symbol"] for e in open_plans(watch)]
                    IND_ALLOW.update(live_syms + [i["symbol"] for i in cand["items"][:ind.PACK_TOP]])
                    uni = scan.load_universe(state_dir).get("stocks", {})
                    pack = ind.build_pack(now, cand, live_syms, candles, NSE_HOLIDAYS,
                                          {s: v.get("fo") for s, v in uni.items()})
                    save(os.path.join(state_dir, "pack.json"), pack)
                    print("pack:", [x["symbol"] for x in pack["stocks"]])
                except Exception as e:
                    print("pack failed", e)
        except Exception as e:  # never let this stop the watcher
            print("scan failed", e)

    # 3) Keep scanning while there is room for another plan.
    count_trades(watch, mt_today, now, ordered)
    save(watch_path, watch)
    live = slots(watch)
    trades = watch.get("trades", 0)  # bought / ordered today (+ just-triggered plans)
    taken = set(watch.get("taken", []))
    waiting = sum(1 for e in live if e["status"] == "waiting" and e["symbol"] not in taken)
    loss_stop = watch.get("day_pnl", 0) <= -DAY_LOSS_STOP
    day_full = watch.get("day_trades", 0) >= MAX_DAY
    full = trades + waiting >= MAX_TRADES or len(live) >= MAX_LIVE or day_full
    late = minutes >= hhmm("14:40")  # last debate (14:15 slot) has reported
    if not live:
        print("no live plan")
        if ((day_full or late or loss_stop) and not watch.get("done_sent")
                and minutes <= hhmm("15:30")):
            why = ([f"Paper loss today {inr(watch['day_pnl'])}", "(daily loss stop), none live now."]
                   if loss_stop else
                   [f"{MAX_DAY} trades taken today", "(daily safety limit), none live now."]
                   if day_full else ["No new entries after 14:30.", "No plan live."])
            telegram(card("🏁", f"DONE FOR TODAY · {now:%H:%M}", [
                "", *why, f"Trades today: {watch.get('day_trades', 0)}", "",
                "🔕 No more scans today.", "🔎 Next scan: Mon–Fri 09:45 IST",
            ]))
            watch["done_sent"] = True
            save(watch_path, watch)
    else:
        print("live:", ", ".join(f"{e['symbol']} {e['status']}" for e in live))
    if not full and not loss_stop:
        if fire_debate(now, trades + waiting, vid if plan_doc.get("date") == today else None, watch):
            save(watch_path, watch)
        elif fire_late_recheck(now, state_dir, today, watch):
            save(watch_path, watch)


def nifty_step(state_dir, today, watch):
    """Forward a new Nifty options PAPER verdict (nifty-options-debate skill). Returns True
    if watch changed. Isolated on purpose: any error here never touches the stock plans."""
    try:
        doc = load(os.path.join(state_dir, "nifty_plan.json"), {})
        vid = doc.get("verdict_id")
        if doc.get("date") != today or not vid or vid == watch.get("nifty_id"):
            return False
        try:
            with open(os.path.join(state_dir, "nifty_verdict.txt")) as f:
                telegram(f.read())
        except FileNotFoundError:
            pass
        watch["nifty_id"] = vid
        return True
    except Exception as e:
        print("nifty_step failed", e)
        return False


def premarket_step(state_dir, today, now, watch):
    """Returns True if watch changed."""
    doc = load(os.path.join(state_dir, "premarket.json"), {})
    if doc.get("date") != today:
        return False
    changed = False
    pid = doc.get("id")
    if pid and pid != watch.get("pre_id"):
        try:
            with open(os.path.join(state_dir, "premarket_msg.txt")) as f:
                telegram(check_groww_links(drop_groww_offline(f.read())))
        except FileNotFoundError:
            pass
        watch["pre_id"] = pid
        changed = True
    minutes = now.hour * 60 + now.minute
    if not hhmm(premarket.START) <= minutes <= hhmm(premarket.END) + 10:
        return changed
    done = watch.setdefault("pre_done", {})
    t = f"{now:%H:%M}"
    for s in doc.get("stocks") or []:
        sym = s.get("symbol")
        if not sym or s.get("keep") is False or sym in done:
            continue
        try:
            bars, _ = candles(s["yahoo"], "5d")
        except Exception as e:
            print("premarket candles failed", sym, e)
            continue
        verdict, info = premarket.evaluate(s, bars, now)
        if verdict == "wait":
            continue
        changed = True
        if verdict == "drop":
            done[sym] = info
            continue
        why = (f"🌅 Premarket pick: 5-min close {rs(info['close'])} above {rs(s['buy_above'])}"
               + (f", volume {info['vol_x']}× usual" if info.get("vol_x") else ""))
        live = slots(watch)
        taken = set(watch.get("taken", []))
        waiting = sum(1 for e in live if e["status"] == "waiting" and e["symbol"] not in taken)
        reasons = []
        if any(e["symbol"] == sym for e in open_plans(watch)):
            reasons.append("already live")
        if len(live) >= MAX_LIVE or watch.get("trades", 0) + waiting >= MAX_TRADES:
            reasons.append(f"{MAX_TRADES} trades / plans already open")
        if watch.get("day_trades", 0) >= MAX_DAY:
            reasons.append(f"{MAX_DAY} trades already taken today")
        p = premarket.to_plan(s, info["close"], t)
        rk = risk.check(p, t, 0)
        reasons += rk["reasons"]
        if not reasons:
            reasons += guard(watch, {**p, "qty": rk["qty"]}, state_dir, today, rk["rr"])
            p["sector"] = scan.group(sym, scan.load_universe(state_dir))
            p["rules_v"] = RULES_VERSION
        if reasons:
            done[sym] = "broke out, skipped: " + "; ".join(reasons)
            telegram(card("🌅", f"PREMARKET SKIP — {esc(sym)}", [
                f"🕐 {t} IST", "", esc(why), *["• " + esc(r) for r in reasons], "",
                "👉 <b>Do not buy.</b>"]))
            continue
        p["qty"] = rk["qty"]
        p["start"] = t
        watch.setdefault("plans", []).append(p)
        watch["live"].append({"symbol": sym, "vid": f"pre_{pid}", "plan": p, "status": "waiting",
                              "since": t, "paper_booked": 0, "why": why})
        done[sym] = f"breakout {info['at']} → BUY NOW plan"
    if (minutes >= hhmm(premarket.END) and done and not watch.get("pre_summary")
            and all(x.get("symbol") in done for x in doc.get("stocks") or [] if x.get("keep") is not False)):
        watch["pre_summary"] = True
        changed = True
        telegram(card("🌅", f"PREMARKET LIST DONE · {t}", [""] + [
            f"• <b>{esc(k)}</b>: {esc(v)}" for k, v in done.items()] + [
            "", "🔎 Normal scans from 09:45."], footer=False))
    return changed


def free_sold(watch, mt, today, now):
    """End each live plan whose stock you bought and have now fully sold (mytrades.json:
    INDmoney sells picked up by themselves, or your SOLD message), so the scans can look
    for a new trade. Returns True if any plan was freed."""
    freed = []
    for e in slots(watch):
        mine = [t for ts in (mt.get("trades") or {}).values() for t in ts
                if t.get("symbol") == e["symbol"] and (t.get("opened") or "") >= e["since"]]
        if mine and all(t.get("left", 1) <= 0 for t in mine):
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
    INDmoney auto-tracking), any chat."""
    out = {}
    for ts in (mt.get("trades") or {}).values():
        for t in ts:
            sym, at = t.get("symbol"), t.get("opened") or "00:00"
            if sym and (sym not in out or at < out[sym]):
                out[sym] = at
    return out


def open_symbols(mt):
    """Stocks you still hold some of today (any chat)."""
    return {t["symbol"] for ts in (mt.get("trades") or {}).values() for t in ts
            if t.get("symbol") and (t.get("left") or 0) > 0}


def count_trades(watch, mt, now, ordered=frozenset()):
    """Set watch["trades"] (trades open now, max MAX_TRADES; also read by the cloud
    debate) and watch["day_trades"] (today's total, max MAX_DAY). Only real trades count,
    not plans that merely hit their buy trigger:
      * open now = stocks you still hold + stocks with a BUY order placed in INDmoney
        but not filled yet (`ordered`). Sell it all and its slot is free again;
      * today's total = every stock you bought or ordered today, sold or not;
      * a triggered plan you haven't bought counts only for MISS_GRACE minutes
        (your fill can take a minute or two to show up). After that it is marked
        missed: its slot is freed and it is paper-tracked quietly. If you buy it
        later after all, it counts again and its alerts come back.
    Returns True if anything changed."""
    t = now.strftime("%H:%M")
    mins = now.hour * 60 + now.minute
    bought = bought_symbols(mt)
    old = (watch.get("trades"), watch.get("day_trades"), watch.get("taken"), [(e.get("missed"), e.get("triggered")) for e in watch.get("live", [])])
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
    taken = open_symbols(mt) | set(ordered)
    watch["taken"] = sorted(taken)
    watch["trades"] = len(taken) + pending
    watch["day_trades"] = len(set(bought) | set(ordered)) + pending
    if freed:
        left = slots(watch)
        telegram(card("🔓", f"SLOT FREED — {esc(', '.join(freed))}", [
            "", f"Buy trigger hit, but no buy or open order of it in {MISS_GRACE} min,",
            "so it doesn't count as a trade and its slot is free.",
            "(Bought it after all? It counts again by itself,",
            "and SL / target alerts for it still come.)",
            f"📚 Trades open now: <b>{watch['trades']}/{MAX_TRADES}</b>",
            f"📚 Live plans now: <b>{len(left)}/{MAX_LIVE}</b>"
            + (f" ({', '.join(esc(e['symbol']) for e in left)})" if left else ""),
        ], footer=False))
    return freed or old != (watch.get("trades"), watch.get("day_trades"), watch.get("taken"), [(e.get("missed"), e.get("triggered")) for e in watch.get("live", [])])


def chase_plan(plan, price, now, bars, watch):
    """A dip plan whose price ran up without the dip: a BUY NOW plan at the live price
    with the same SL / targets, or None if that fails the risk check (net R:R ≥ 1.5),
    is too late, stretched (> 1.5% above VWAP) or already past max price."""
    t = f"{now:%H:%M}"
    if hhmm(t) >= hhmm(risk.LAST_ENTRY) or price >= plan["t1"]:
        return None
    vol = sum(b[5] for b in bars)
    vwap = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in bars) / vol if vol else None
    if vwap and price > vwap * 1.015:
        return None
    dl = min(hhmm(t) + 15, hhmm(risk.LAST_ENTRY))
    p2 = {**plan, "entry_type": "now", "entry": round(price, 2),
          "zone_low": round(price * 0.998, 2), "zone_high": round(price * 1.002, 2),
          "max_entry": round(price * 1.002, 2), "trigger_close_above": round(price * 0.998, 2),
          "deadline": f"{dl // 60:02d}:{dl % 60:02d}",
          "cancel_if_high_reaches": max(plan["cancel_if_high_reaches"], plan["t1"])}
    rk = risk.check(p2, t, 0)
    if rk["status"] != "PASS":
        return None
    p2["qty"] = rk["qty"]
    return p2


def open_plans(watch):
    """Every plan still being tracked (alerts, paper result)."""
    return [e for e in watch.get("live", []) if e.get("status") in OPEN_STATES]


def slots(watch):
    """Open plans that still take a slot: not ones you already sold or never bought."""
    return [e for e in open_plans(watch) if not e.get("user_sold") and not e.get("missed")]


SL_NUDGE_MIN = 3  # minutes after your buy with no SL order seen → one reminder


def sl_nudge(ind, mt, today, minutes, watch):
    """Your INDmoney buy has no open SELL (SL) order after SL_NUDGE_MIN minutes → one
    reminder per trade. Needs the worker's open-sell list (positions_ind.json "sells")."""
    if ind.get("date") != today or not isinstance(ind.get("sells"), list) or not MARKET_OPEN(minutes):
        return False
    sells = set(ind["sells"])
    done = watch.setdefault("sl_nudged", [])
    changed = False
    for chat, trs in (mt.get("trades") or {}).items():
        for tr in trs:
            if tr.get("broker") != "INDmoney" or tr.get("left", 0) <= 0 or not tr.get("sl"):
                continue
            key = f"{chat}:{tr['symbol']}:{tr.get('opened')}"
            if key in done or tr["symbol"] in sells or minutes - hhmm(tr.get("opened", "00:00")) < SL_NUDGE_MIN:
                continue
            done.append(key)
            changed = True
            telegram(card("🛑", f"NO SL ORDER? — {esc(tr['symbol'])}", [
                "", f"You hold <b>{tr['left']}</b> but INDmoney shows no open SELL / SL order.",
                f"👉 Place <b>SL-M {tr['left']} @ {rs(tr['sl'])}</b> now", "",
                "<i>If you just placed it, ignore this. One reminder per trade.</i>"], footer=False), to=chat)
    return changed


def MARKET_OPEN(minutes):
    return hhmm("09:15") <= minutes <= hhmm("15:30")


def prescan_step(state_dir, today, minutes, watch):
    """08:10–08:39: rank the whole universe for today's premarket list (code, Yahoo daily),
    so the 08:40 debate starts from the best momentum stocks, not ~5 it found itself."""
    if not hhmm("08:10") <= minutes < hhmm("08:40") or watch.get("prescan"):
        return
    try:
        doc = prescan.run(state_dir, datetime.now(IST).date())
        if doc["scanned"] >= 100:
            save(os.path.join(state_dir, "premarket_candidates.json"), doc)
            watch["prescan"] = doc["built_at"]
            print("prescan:", doc["passed"], "passed", [i["symbol"] for i in doc["items"]])
        else:
            print("prescan: only", doc["scanned"], "priced, retry next run")
    except Exception as e:
        print("prescan failed", e)


def opening_step(state_dir, today, now, watch):
    """09:20–09:40, every 5 min: the opening momentum engine, PAPER TEST ONLY (one week
    from 16 Nov). Each pick gets a 👀 card clearly marked "don't trade this"; 15:45 replays
    them into the scorecard. Backtest 15 Oct–13 Nov: 30 trades, 76% win, +0.75R a trade,
    while buying 09:55 stocks already up ≥ 4.5% lost −0.32R a trade."""
    slot = f"{now.hour:02d}:{now.minute // 5 * 5:02d}"
    done = watch.setdefault("open_slots", [])
    if slot not in opening.SLOTS or slot in done:
        return False
    done.append(slot)
    path = os.path.join(state_dir, "opening.json")
    doc = load(path, {})
    if doc.get("date") != today:
        doc["date"], doc["open"] = today, []
    if len(doc["open"]) >= opening.PER_DAY:
        return True
    pc = load(os.path.join(state_dir, "premarket_candidates.json"), {})
    avg = pc.get("avg_vol") if pc.get("date") == today else None
    if not avg:
        print("opening: no avg volumes from the 08:10 prescan")
        return True
    try:
        nb, _ = candles("^NSEI", "5d", False)
        prev = [b for b in nb if b[0].date() < now.date()]
        tb = opening.today_bars(nb, now)
        npct = (tb[-1][4] / prev[-1][4] - 1) * 100 if tb and prev else None
        uni = scan.load_universe(state_dir)
        syms = list(uni.get("stocks", {}))
        try:
            qs = ind.quotes(syms) if ind.worker_on() else None
        except Exception as e:
            print("opening quotes failed, scanning all on Yahoo:", e)
            qs = None
        picks = opening.live_slot(now, syms, avg, lambda s, d: candles(s, d, False), qs, npct,
                                  {t["symbol"] for t in doc["open"]}, {t.get("group") for t in doc["open"]},
                                  {s: scan.group(s, uni) for s in syms}, opening.PER_DAY - len(doc["open"]))
    except Exception as e:
        print("opening failed", e)
        return True
    for t in picks:
        doc["open"].append(t)
        telegram(card("👀", f"PAPER TEST · OPENING — {esc(t['symbol'])}", [
            f"🕐 {t['at']} IST · up {t['pct']:+.1f}% (gap {t['gap']:+.1f}%) · volume {t['vol_frac']:.0%} of a day", "",
            f"Paper buy ~{rs(t['entry'])} · SL {rs(t['sl'])} · T1 {rs(t['t1'])} · T2 {rs(t['t2'])}",
            "Out by 11:15 if T1 not reached; after T1 the rest trails.", "",
            "<b>Paper test only — don't trade this.</b>",
            "<i>Testing the opening engine for a week; results in the 15:45 scorecard.</i>"], footer=False))
    save(path, doc)
    return True


def cues_step(state_dir, today, minutes, watch):
    """Overnight cues to state/cues.json at ~08:05 (first, before the 08:10 momentum scan)
    and again ~09:00 (Yahoo; no connector)."""
    slot = "pre" if hhmm("08:05") <= minutes < hhmm("09:00") else (
        "open" if hhmm("09:00") <= minutes <= hhmm("09:14") else None)
    done = watch.setdefault("cues", [])
    if not slot or slot in done:
        return
    try:
        c = ind.build_cues(datetime.now(IST))
        save(os.path.join(state_dir, "cues.json"), c)
        print("cues:", c["line"])
    except Exception as e:
        print("cues failed", e)
    done.append(slot)


def weak_rule(plan, cand, bars, now):
    """Weak market (scanner mode "weak" or < WEAK_ABOVE% of stocks above VWAP): dips keep
    falling, so no DIP buys; a BUY NOW needs confidence ≥ WEAK_CONF, the stock ≥ WEAK_RS%
    stronger than Nifty and in its own uptrend. (13 Nov, 13–31% above VWAP: both dip plans
    that filled lost; the two that ran up without the dip reached T1.)"""
    above = cand.get("above_vwap_pct")
    if (cand.get("scanned") or 0) < 50:   # too few stocks priced to judge the market
        return []
    if not (cand.get("mode") == "weak" or (above is not None and above < WEAK_ABOVE)):
        return []
    tag = f"weak market ({above}% of stocks above VWAP)"
    if plan.get("entry_type") != "now":
        return [f"{tag}: no dip buys — dips keep falling on days like this"]
    out = []
    conf = plan.get("confidence")
    if conf is not None and conf < WEAK_CONF:
        out.append(f"{tag}: confidence {conf:g}/10 below {WEAK_CONF}")
    st = scan.stats(bars, now) if bars else None
    nifty = cand.get("nifty_pct") or 0
    if st and st["pct"] - nifty < WEAK_RS:
        out.append(f"{tag}: only {st['pct'] - nifty:+.1f}% vs Nifty (need +{WEAK_RS:g}%)")
    if bars and not scan.stock_trend(bars, now):
        out.append(f"{tag}: stock not in its own 5-min uptrend")
    return out


def events_today(state_dir, today):
    """Event-day notes: watcher/events.json {"YYYY-MM-DD": "RBI policy"} (edit by hand)
    plus "events" the premarket cloud run wrote in state/premarket.json."""
    ev = []
    fixed = load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.json"), {})
    if fixed.get(today):
        ev.append(fixed[today])
    pm = load(os.path.join(state_dir, "premarket.json"), {})
    if pm.get("date") == today:
        ev += [str(x)[:80] for x in pm.get("events") or [] if x]
    return ev


def vix_change():
    """India VIX % change on the day (Yahoo ^INDIAVIX), None if unknown."""
    try:
        bars, ltp = candles("^INDIAVIX", "5d", False)
        today = datetime.now(IST).date()
        prev = [b for b in bars if b[0].date() < today]
        now_px = ltp or (bars[-1][4] if bars else None)
        return round((now_px / prev[-1][4] - 1) * 100, 1) if prev and now_px else None
    except Exception as e:
        print("VIX failed", e)
        return None


def guard(watch, plan, state_dir, today, rr=None):
    """Portfolio rules on top of risk.py: one plan per sector group, total risk to SL
    of open plans ≤ MAX_HEAT, R:R ≥ EVENT_RR on event days, no entries on a VIX spike.
    Tags the plan with its sector and the rules version. Returns block reasons."""
    reasons = []
    uni = scan.load_universe(state_dir)
    g = scan.group(plan["symbol"], uni)
    plan["sector"] = g
    plan["rules_v"] = RULES_VERSION
    open_ = [e for e in slots(watch) if e["symbol"] != plan["symbol"]]
    if g:
        same = [e["symbol"] for e in open_ if (e["plan"].get("sector") or scan.group(e["symbol"], uni)) == g]
        if same:
            reasons.append(f"{g} already open ({', '.join(same)}): one per sector")
    heat = sum(e["plan"]["qty"] * max(e["plan"]["entry"] - e["plan"]["sl"], 0)
               for e in open_ if e["status"] in ("waiting", "in_zone"))
    new = plan.get("qty", 0) * max(plan["entry"] - plan["sl"], 0)
    if heat and heat + new > MAX_HEAT:
        reasons.append(f"total risk to SL would be ₹{heat + new:,.0f} (max ₹{MAX_HEAT:,}): "
                       "wait for a plan to reach T1 or close")
    ev = events_today(state_dir, today)
    if ev and rr is not None and rr < EVENT_RR:
        reasons.append(f"event day ({'; '.join(ev)[:80]}): needs R:R ≥ {EVENT_RR:g}, plan has {rr}")
    vx = vix_change()
    if vx is not None and vx >= VIX_SPIKE:
        reasons.append(f"India VIX up {vx:g}% today (fear spike): no new entries")
    return reasons


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
        rerouted = False
        if cancel and not buy_now and price > plan["zone_high"] and not cancel.startswith("5-min close below"):
            p2 = chase_plan(plan, price, now, bars, watch)
            if p2:  # ran up without the dip, but buying here still pays ≥ 1.5× the risk
                entry["plan"] = plan = p2
                entry["rerouted"] = t
                buy_now = rerouted = True
                cancel = None
                trigger = [(now, price, price, price, price, 0)]
        if cancel:
            status = "cancelled"
            msg = card("❌", f"CANCELLED — {sym}", [
                f"🕐 {t} IST", "",
                f"Reason: {cancel}", price_line, "",
                "👉 <b>Do not buy.</b> Cancel any buy order / price alert",
                "you set for it in INDmoney.",
                "🔎 Next cloud scan will look for a new setup.",
            ])
        elif trigger and plan["symbol"] not in watch.get("taken", []) and (
                watch.get("trades", 0) >= MAX_TRADES or watch.get("day_trades", 0) >= MAX_DAY):
            status = "cancelled"
            full_why = (f"{MAX_TRADES} trades already open." if watch.get("trades", 0) >= MAX_TRADES
                        else f"{MAX_DAY} trades already taken today.")
            msg = card("❌", f"SKIP — {sym}", [
                f"🕐 {t} IST", "", full_why, price_line, "",
                "👉 <b>Do not buy.</b> Cancel any buy order / price alert for it.",
            ])
        elif trigger:
            status = "in_zone"
            entry["triggered"] = t
            plan["buy_at"] = t
            if plan["symbol"] not in watch.get("taken", []):  # until count_trades sees your buy
                watch["trades"] = watch.get("trades", 0) + 1
                watch["day_trades"] = watch.get("day_trades", 0) + 1
            msg = card("🔔", f"{'BUY NOW' if buy_now else 'BUY TRIGGER'} — {sym}", [
                f"🕐 {t} IST", "",
                *([esc(entry["why"])] if entry.get("why") else []),
                *(["🔁 Ran up without the dip,", "but R:R from here is still ≥ 1.5"] if rerouted else []),
                (f"✅ Price {rs(price)} is in the buy-now zone" if buy_now
                 else f"✅ Green 5-min close {rs(trigger[-1][4])}"),
                price_line, "",
                f"📥 Buy  <b>~{rs(plan['entry'])}</b>  (max {rs(plan['max_entry'])})",
                *plan_lines(plan), "", *order_block(plan, confirmed=True), "",
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
