"""5-minute price watcher for the intraday debate.

Reads state/plan.json (written by the hourly cloud debate), checks the live
price on Yahoo Finance, and sends Telegram messages when the plan's status
changes. Writes state/watch.json so the same alert is never sent twice.

Usage: python watch.py <state_dir>
Env:   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (optional DRY_RUN=1 to print only)
Analysis alerts only - it never places orders.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
DISCLAIMER = "Analysis only, not investment advice. You place and manage orders yourself."
OPEN_STATES = ("waiting", "in_zone", "t1_hit")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def telegram(text):
    if os.environ.get("DRY_RUN"):
        print("TELEGRAM:", text)
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("telegram not configured; message was:", text)
        return
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"}
    ).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20)


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
            telegram(f"New verdict {vid} (no text file found).")
        watch["verdict_id"] = vid
        watch["status"] = "waiting" if plan_doc.get("plan") else None
        watch["since"] = None
        save(watch_path, watch)

    plan = plan_doc.get("plan") if plan_doc.get("date") == today else None
    if not plan or watch.get("status") not in OPEN_STATES:
        print("no live plan")
        return
    if minutes < hhmm("09:15") or minutes > hhmm("15:30"):
        return

    sym = plan["symbol"]
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
    status = watch["status"]
    msg = None

    def note(text):
        watch["log"].append(f"{now:%H:%M} {text}")

    if status == "waiting":
        hit_zone = [b for b in new if b[3] <= plan["zone_high"]]
        trigger = [b for b in hit_zone if b[4] > b[1] and b[4] > plan["trigger_close_above"]
                   and b[4] <= plan["max_entry"]]
        if minutes >= hhmm(plan["deadline"]) and not trigger:
            status, msg = "cancelled", f"ALERT: {sym} plan cancelled — not triggered by {plan['deadline']}. Price ₹{price:.2f}."
        elif any(b[2] >= plan["cancel_if_high_reaches"] for b in new) and not hit_zone:
            status, msg = "cancelled", f"ALERT: {sym} plan cancelled — ran to ₹{plan['cancel_if_high_reaches']} without the pullback. Don't chase."
        elif any(b[4] < plan["cancel_if_close_below"] for b in new) and not trigger:
            status, msg = "cancelled", f"ALERT: {sym} plan cancelled — 5-min close below ₹{plan['cancel_if_close_below']}."
        elif trigger:
            status = "in_zone"
            msg = (f"ALERT: {sym} BUY TRIGGER — green 5-min close ₹{trigger[-1][4]:.2f} in zone. "
                   f"Plan: buy ~₹{plan['entry']} (max ₹{plan['max_entry']}), SL ₹{plan['sl']}, "
                   f"T1 ₹{plan['t1']}, T2 ₹{plan['t2']}, qty {plan['qty']}. Place SL-M right after the fill.")
    elif status == "in_zone":
        if any(b[3] <= plan["sl"] for b in new) or price <= plan["sl"]:
            status, msg = "closed", f"ALERT: {sym} STOP LOSS ₹{plan['sl']} hit — exit if still holding."
        elif any(b[2] >= plan["t1"] for b in new) or price >= plan["t1"]:
            status, msg = "t1_hit", f"ALERT: {sym} T1 ₹{plan['t1']} hit — book 50%, move SL to ₹{plan['entry']}."
        elif last and last[4] < plan["exit_if_close_below_after_entry"]:
            status, msg = "closed", f"ALERT: {sym} early exit — 5-min close ₹{last[4]:.2f} below ₹{plan['exit_if_close_below_after_entry']}."
    elif status == "t1_hit":
        if any(b[2] >= plan["t2"] for b in new) or price >= plan["t2"]:
            status, msg = "closed", f"ALERT: {sym} T2 ₹{plan['t2']} hit — exit the rest."
        elif price <= plan["entry"]:
            status, msg = "closed", f"ALERT: {sym} back at cost ₹{plan['entry']} — exit the rest."
    if status in ("in_zone", "t1_hit") and minutes >= hhmm(plan.get("time_stop", "15:10")):
        status, msg = "closed", f"ALERT: Time stop {plan.get('time_stop', '15:10')} — exit any open {sym} position."

    vw = f"{vwap:.2f}" if vwap else "n/a"
    note(f"{sym} ₹{price:.2f} VWAP {vw} status {status}")
    if msg:
        telegram(msg + "\n" + DISCLAIMER)
    elif now.minute % 15 < 5:
        telegram(f"{now:%H:%M} {sym} ₹{price:.2f} (VWAP {vw}) — {status}, zone ₹{plan['zone_low']}–{plan['zone_high']}, SL ₹{plan['sl']}, T1 ₹{plan['t1']}.")
    watch["status"] = status
    watch["log"] = watch["log"][-200:]
    save(watch_path, watch)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "state")
