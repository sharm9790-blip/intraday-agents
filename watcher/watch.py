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

IST = timezone(timedelta(hours=5, minutes=30))
FOOTER = "<i>Analysis only, not advice. You place orders yourself.</i>"
OPEN_STATES = ("waiting", "in_zone", "t1_hit")
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


def telegram(text):
    """Send Telegram HTML; if the markup is rejected, resend as plain text."""
    if os.environ.get("DRY_RUN"):
        print("TELEGRAM:\n" + text + "\n")
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("telegram not configured; message was:", text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def post(params):
        data = urllib.parse.urlencode(params).encode()
        urllib.request.urlopen(url, data, timeout=20)

    base = {"chat_id": chat, "disable_web_page_preview": "true"}
    try:
        post({**base, "text": text[:4000], "parse_mode": "HTML"})
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        post({**base, "text": plain[:4000]})


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


def fire_debate(now, trades, last_verdict_id, watch):
    """Start the cloud debate through the routine's API trigger at :00/:15/:30.

    The routine's own hourly schedule covers :45, so together they give a scan
    every 15 min. Only called when no plan is live. Returns True if it fired.
    """
    url = os.environ.get("CLAUDE_FIRE_URL")
    token = os.environ.get("CLAUDE_FIRE_TOKEN")
    minutes = now.hour * 60 + now.minute
    quarter = now.minute // 15 * 15
    slot = f"{now.hour:02d}:{quarter:02d}"
    if not url or not token:
        return False
    if not hhmm("10:00") <= minutes <= hhmm("14:30") or quarter == 45 or trades >= 2:
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
            return False
        print("fired debate for slot", slot)
    watch["last_fire"] = slot
    return True


def main(state_dir):
    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    minutes = now.hour * 60 + now.minute
    plan_doc = load(os.path.join(state_dir, "plan.json"), {})
    watch_path = os.path.join(state_dir, "watch.json")
    watch = load(watch_path, {})
    if watch.get("date") != today:
        watch = {"date": today, "verdict_id": None, "status": None, "since": None, "log": []}

    # 1) Forward a new verdict from the cloud debate (option 1 alert).
    vid = plan_doc.get("verdict_id")
    if plan_doc.get("date") == today and vid and vid != watch.get("verdict_id"):
        try:
            with open(os.path.join(state_dir, "latest_verdict.txt")) as f:
                telegram(f.read())
        except FileNotFoundError:
            telegram(card("📋", f"New verdict {esc(vid)}", ["(verdict text missing)"]))
        watch["verdict_id"] = vid
        watch["status"] = "waiting" if plan_doc.get("plan") else None
        watch["since"] = None
        save(watch_path, watch)

    plan = plan_doc.get("plan") if plan_doc.get("date") == today else None
    if not plan or watch.get("status") not in OPEN_STATES:
        print("no live plan")
        trades = watch.get("trades", 0)  # plans that hit the buy trigger today
        late = minutes >= hhmm("14:40")  # last debate (14:30 slot) has reported
        if (trades >= 2 or late) and not watch.get("done_sent") and minutes <= hhmm("15:30"):
            why = (["2 trades triggered today", "(daily limit), none live now."] if trades >= 2
                   else ["No new entries after 14:30.", "No plan live."])
            telegram(card("🏁", f"DONE FOR TODAY · {now:%H:%M}", [
                "",
                *why,
                f"Trades triggered: {trades}",
                "",
                "🔕 No more scans today.",
                "🔎 Next scan: Mon–Fri 09:45 IST",
            ]))
            watch["done_sent"] = True
            save(watch_path, watch)
        if fire_debate(now, trades, vid if plan_doc.get("date") == today else None, watch):
            save(watch_path, watch)
        return
    if minutes < hhmm("09:15") or minutes > hhmm("15:30"):
        return

    sym = esc(plan["symbol"])
    bars, ltp = candles(plan["yahoo"])
    if not bars:
        print("no candles yet")
        return
    # Candles that closed after the verdict (ignore the one still forming).
    closed = [b for b in bars if b[0] + timedelta(minutes=5) <= now]
    vwap_num = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in bars)
    vol = sum(b[5] for b in bars)
    vwap = vwap_num / vol if vol else None
    last = closed[-1] if closed else None
    since = watch.get("since")  # IST HH:MM after which we evaluate candles
    if since is None:
        since = (vid or "_0000").split("_")[-1]
        since = f"{since[:2]}:{since[2:]}"
        watch["since"] = since
    new = [b for b in closed if b[0].hour * 60 + b[0].minute >= hhmm(since)]
    price = ltp if ltp is not None else bars[-1][4]
    vw = rs(vwap) if vwap else "n/a"
    price_line = f"💹 Now <b>{rs(price)}</b>  ·  VWAP {vw}"
    t = f"{now:%H:%M}"
    status = watch["status"]
    msg = None

    if status == "waiting":
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
                "👉 <b>Do not buy.</b> Delete the Groww alert.",
                "🔎 Next cloud scan will look for a new setup.",
            ])
        elif trigger:
            status = "in_zone"
            watch["trades"] = watch.get("trades", 0) + 1
            msg = card("🔔", f"BUY TRIGGER — {sym}", [
                f"🕐 {t} IST", "",
                f"✅ Green 5-min close {rs(trigger[-1][4])}",
                price_line, "",
                f"📥 Buy  <b>~{rs(plan['entry'])}</b>  (max {rs(plan['max_entry'])})",
                *plan_lines(plan), "",
                "👉 Place SL-M right after the fill.",
            ])
    elif status == "in_zone":
        if any(b[3] <= plan["sl"] for b in new) or price <= plan["sl"]:
            status = "closed"
            msg = card("🔴", f"STOP LOSS HIT — {sym}", [
                f"🕐 {t} IST", "", f"SL {rs(plan['sl'])} touched", price_line, "",
                "👉 <b>Exit now</b> if still holding.",
            ])
        elif any(b[2] >= plan["t1"] for b in new) or price >= plan["t1"]:
            status = "t1_hit"
            msg = card("🎯", f"TARGET 1 HIT — {sym}", [
                f"🕐 {t} IST", "", f"T1 {rs(plan['t1'])} reached", price_line, "",
                "👉 <b>Sell half</b> now.",
                f"👉 Move SL to cost <b>{rs(plan['entry'])}</b>.",
                f"🎯 Next: T2 {rs(plan['t2'])}",
            ])
        elif last and last[4] < plan["exit_if_close_below_after_entry"]:
            status = "closed"
            msg = card("⚠️", f"EARLY EXIT — {sym}", [
                f"🕐 {t} IST", "",
                f"5-min close {rs(last[4])} below {rs(plan['exit_if_close_below_after_entry'])}",
                price_line, "", "👉 <b>Exit</b> before the stop is hit.",
            ])
    elif status == "t1_hit":
        if any(b[2] >= plan["t2"] for b in new) or price >= plan["t2"]:
            status = "closed"
            msg = card("🏁", f"TARGET 2 HIT — {sym}", [
                f"🕐 {t} IST", "", f"T2 {rs(plan['t2'])} reached", price_line, "",
                "👉 <b>Sell the rest.</b> Trade complete.",
            ])
        elif price <= plan["entry"]:
            status = "closed"
            msg = card("↩️", f"BACK AT COST — {sym}", [
                f"🕐 {t} IST", "", price_line, "",
                f"👉 <b>Exit the rest</b> at {rs(plan['entry'])}. Half was booked at T1.",
            ])
    ts = plan.get("time_stop", "15:10")
    if status in ("in_zone", "t1_hit") and minutes >= hhmm(ts):
        status = "closed"
        msg = card("⏰", f"TIME STOP — {sym}", [
            f"🕐 {t} IST", "", price_line, "",
            f"👉 <b>Exit everything</b> — {ts} rule.",
        ])

    watch["log"].append(f"{t} {plan['symbol']} {price:.2f} VWAP {vw} status {status}")
    if msg:
        telegram(msg)
    elif now.minute % 15 < 5:
        telegram(card("📊", f"{sym}  ·  {t}", [
            price_line,
            f"Status: {STATUS_LABEL.get(status, status)}",
            "",
            f"📥 Zone  {rs(plan['zone_low'])}–{rs(plan['zone_high'])}",
            f"🛑 SL {rs(plan['sl'])}  ·  🎯 T1 {rs(plan['t1'])}",
            f"⌛ Cancel by {plan['deadline']}" if status == "waiting" else f"⏰ Exit by {ts}",
        ], footer=False))
    watch["status"] = status
    watch["log"] = watch["log"][-200:]
    save(watch_path, watch)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "state")
