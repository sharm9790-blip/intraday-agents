"""Health alerts, so the system never fails silently.

Called by watch.py on every run (market days). Sends, at most once each:
  🟢 SYSTEM READY   first run from 09:15 (pinger, cloud trigger, holiday list)
  ⚠️ WATCHER GAP    no watcher run for 20+ min during market hours
  ⚠️ PINGER?        run started by GitHub's backup schedule instead of the pinger
  ⚠️ SCAN FAILED    the routine API trigger was refused (401/403/429…)
  ⚠️ NO VERDICT     a debate was fired but nothing arrived within 25 min
  ⚠️ X OFFLINE      the cloud run could not use INDmoney / InvestSights
  ✅ X BACK         the next run after that could use it again
  (Groww is not alerted on: trades and live prices run on INDmoney now, and the
   debate carries on without Groww, so its sign-in state is not worth a message.)
"""
import os
from datetime import datetime


def _m(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


NAMES = {"groww": "Groww", "indmoney": "INDmoney", "investsights": "InvestSights"}
SILENT = {"groww"}  # never alerted on (see docstring)
FIX = "https://claude.ai/customize/connectors"


def connectors(doc, today, flags, send):
    """doc: plan.json or positions.json with "connectors": {"groww": true/false, ...}."""
    if doc.get("date") != today:
        return
    for key, ok in (doc.get("connectors") or {}).items():
        if key in SILENT:
            continue
        name = NAMES.get(key, key)
        down = "down_" + key
        if ok is False and not flags.get(down):
            flags[down] = True
            send(f"⚠️ <b>{name.upper()} OFFLINE</b>\nThe cloud run couldn't use {name}\n"
                 f"(sign-in probably expired). Scans go on\nwith the other connectors.\n"
                 f"👉 Reconnect: {FIX}")
        elif ok is True and flags.get(down):
            flags[down] = False
            send(f"✅ <b>{name} back</b> — the cloud run can use it again.")


def check(now, watch, plan_doc, holidays, send):
    """Mutates watch (flags), calls send(text) for each alert."""
    mins = now.hour * 60 + now.minute
    today = now.strftime("%Y-%m-%d")
    flags = watch.setdefault("health", {})
    market = _m("09:15") <= mins <= _m("15:30")
    event = os.environ.get("GITHUB_EVENT_NAME", "")

    # Gap since the last run (the pinger should start us every 5-15 min).
    last = watch.get("last_run")
    if market and last and mins - _m(last) >= 20 and last >= "09:15":
        key = "gap_" + last
        if key not in flags:
            flags[key] = True
            send(f"⚠️ <b>WATCHER GAP</b>\nNo check between {last} and {now:%H:%M} IST.\n"
                 "Price alerts may have been late.\n👉 Check the cron-job.org job is enabled.")
    watch["last_run"] = now.strftime("%H:%M")

    if market and event == "schedule" and "pinger" not in flags:
        flags["pinger"] = True
        send("⚠️ <b>PINGER?</b>\nThis check was started by GitHub's 30-min backup,\n"
             "not by your cron-job.org pinger.\n👉 Check the job's History on cron-job.org.")

    if mins >= _m("09:15") and "ready" not in flags:
        flags["ready"] = True
        fire_ok = bool(os.environ.get("CLAUDE_FIRE_URL") and os.environ.get("CLAUDE_FIRE_TOKEN"))
        lines = [f"🟢 <b>SYSTEM READY · {now:%a %d %b}</b>", "",
                 "✅ Watcher running" + (" (pinger)" if event == "workflow_dispatch" else ""),
                 ("✅" if fire_ok else "❌") + " Cloud scan trigger",
                 "🌅 Premarket list 08:45 · pre-open 09:08",
                 "🔎 Scans 09:45, then every 15 min"]
        if now.month == 12 and not any(h.startswith(str(now.year + 1)) for h in holidays):
            lines.append(f"⚠️ Add {now.year + 1} NSE holidays (watch.py)")
        send("\n".join(lines))

    err = watch.get("fire_error")
    if err and f"fire_{err}" not in flags:
        flags[f"fire_{err}"] = True
        hint = {401: "token wrong or regenerated → update CLAUDE_FIRE_TOKEN",
                403: "access refused → check the routine on claude.ai",
                429: "too many runs / usage limit → scans paused for now"}.get(err, "see GitHub Actions log")
        send(f"⚠️ <b>SCAN FAILED</b> ({err})\nThe cloud debate could not be started.\n👉 {hint}")

    connectors(plan_doc, today, flags, send)

    fired = watch.get("fired_at")
    if fired and mins - _m(fired) >= 25:
        vid = plan_doc.get("verdict_id") or ""
        import risk
        vt = risk.vid_hm(vid) if plan_doc.get("date") == today else None
        got = bool(vt and vt >= fired)
        key = "noverdict_" + fired
        if not got and key not in flags:
            flags[key] = True
            send(f"⚠️ <b>NO VERDICT</b>\nScan fired at {fired} but no result after 25 min.\n"
                 "👉 Open the routine's runs on claude.ai to see why.")
        if got or key in flags:
            watch["fired_at"] = None
