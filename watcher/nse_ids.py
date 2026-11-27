"""NSE symbol → exchange security id map, for INDmoney's price API.

INDmoney's LTP call wants "NSE_<security id>" (e.g. SBIN → NSE_3045), not the
symbol. The ids are NSE's own scrip codes, so they come from a public scrip
master (no login needed). The Cloudflare worker can't parse a 28 MB CSV on the
free plan, so the watcher builds a small JSON (~2,000 EQ stocks) once a week into
state/nse_ids.json and the worker reads that.

Usage: python nse_ids.py <state_dir>   (watch.py calls refresh() itself)
"""
import csv
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

MASTER = "https://images.dhan.co/api-data/api-scrip-master.csv"
MAX_AGE_DAYS = 7
FUT = "NIFTYFUT"          # current-month NIFTY futures, stored as "NFO_<id>" (worker passes it through)
LAST_FUT_EXPIRY = None    # expiry date of the contract the last build() picked


def build(today=None):
    """{symbol: NSE id} for ~2,000 EQ stocks, plus NIFTYFUT: "NFO_<id>" for the nearest
    NIFTY futures contract (Tradetron's "Current Month") so the EMA + VWAP paper test can
    use futures VWAP and volume, like the Tradetron strategy."""
    global LAST_FUT_EXPIRY
    today = today or datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
    req = urllib.request.Request(MASTER, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8", "replace")
    ids, futs = {}, []
    for row in csv.reader(io.StringIO(text)):
        # NSE,D,58875,FUTIDX,0,NIFTY-Dec2026-FUT,65.0,NIFTY DEC FUT,2026-12-29 14:30:00,...
        if len(row) > 8 and row[0] == "NSE" and row[1] == "D" and row[3] == "FUTIDX" \
                and row[5].startswith("NIFTY-") and row[5].endswith("-FUT") and row[2].isdigit():
            if row[8][:10] >= today:
                futs.append((row[8][:10], int(row[2])))
            continue
        # EXCH, SEGMENT, SECURITY_ID, INSTRUMENT_NAME, ..., TRADING_SYMBOL (5), ..., SERIES (14)
        if len(row) > 14 and row[0] == "NSE" and row[1] == "E" and row[3] == "EQUITY" \
                and row[14] in ("EQ", "BE") and row[2].isdigit():
            ids.setdefault(row[5], int(row[2]))
    if len(ids) < 1000 or ids.get("RELIANCE") != 2885 or ids.get("SBIN") != 3045:
        raise ValueError(f"scrip master looks wrong ({len(ids)} stocks)")
    if futs:
        exp, sid = min(futs)
        ids[FUT], LAST_FUT_EXPIRY = f"NFO_{sid}", exp
    return ids


def refresh(state_dir, save, load, now=None):
    """Rebuild state/nse_ids.json if missing, older than a week, or its NIFTY futures
    contract has expired (roll to the next month). Returns True if rebuilt."""
    path = os.path.join(state_dir, "nse_ids.json")
    old = load(path, {})
    now = now or datetime.now(timezone.utc)
    try:
        built = datetime.fromisoformat(old.get("built", ""))
        today = now.astimezone(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
        if now - built < timedelta(days=MAX_AGE_DAYS) and old.get("ids") \
                and "fut_expiry" in old and (old["fut_expiry"] or "9999-12-31") >= today:
            return False
    except ValueError:
        pass
    ids = build()
    doc = {"built": now.isoformat(timespec="seconds"), "ids": ids,
           "fut_expiry": LAST_FUT_EXPIRY if FUT in ids else None}
    save(path, doc)
    return True


if __name__ == "__main__":
    def _load(p, d):
        try:
            with open(p) as f:
                return json.load(f)
        except (OSError, ValueError):
            return d

    def _save(p, data):
        with open(p, "w") as f:
            json.dump(data, f, separators=(",", ":"))

    refresh(sys.argv[1], _save, _load)
    print(len(_load(os.path.join(sys.argv[1], "nse_ids.json"), {}).get("ids", {})), "stocks")
