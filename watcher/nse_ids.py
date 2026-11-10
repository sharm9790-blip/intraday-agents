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


def build():
    req = urllib.request.Request(MASTER, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode("utf-8", "replace")
    ids = {}
    for row in csv.reader(io.StringIO(text)):
        # EXCH, SEGMENT, SECURITY_ID, INSTRUMENT_NAME, ..., TRADING_SYMBOL (5), ..., SERIES (14)
        if len(row) > 14 and row[0] == "NSE" and row[1] == "E" and row[3] == "EQUITY" \
                and row[14] in ("EQ", "BE") and row[2].isdigit():
            ids.setdefault(row[5], int(row[2]))
    if len(ids) < 1000 or ids.get("RELIANCE") != 2885 or ids.get("SBIN") != 3045:
        raise ValueError(f"scrip master looks wrong ({len(ids)} stocks)")
    return ids


def refresh(state_dir, save, load, now=None):
    """Rebuild state/nse_ids.json if missing or older than a week. Returns True if rebuilt."""
    path = os.path.join(state_dir, "nse_ids.json")
    old = load(path, {})
    now = now or datetime.now(timezone.utc)
    try:
        built = datetime.fromisoformat(old.get("built", ""))
        if now - built < timedelta(days=MAX_AGE_DAYS) and old.get("ids"):
            return False
    except ValueError:
        pass
    save(path, {"built": now.isoformat(timespec="seconds"), "ids": build()})
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
