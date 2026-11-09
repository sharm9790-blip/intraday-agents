"""Second chance for rejected stocks.

The cloud debate writes every stock it rejects to state/rejected.json as
{"SYM": "HH:MM reason ... flips on 5-min close >1050 ..."}. Later scans skip
those stocks, so a stock rejected at 11:10 that breaks out at 13:40 was never
looked at again (PNB / UNIONBANK / CANBK on 9 Nov 2026).

Every 5-min watcher run (09:45–14:30) this checks each rejected stock: if a
5-min candle that closed after the rejection time closes above its level, the
stock goes into state/recheck.json and the next debate must analyse it again.
Level = the "flips on … >X" price the Judge wrote, else the day high up to the
rejection time. A newer rejection (later time in rejected.json) needs a new
breakout, so a stock is not re-checked over and over.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor

FLIP = re.compile(r"flips?\s+(?:only\s+)?on[^>]*>\s*=?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.I)
TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})")
BUFFER = 0.002  # close must clear the level by 0.2%, not by a paisa


def _hm(b):
    return b[0].hour * 60 + b[0].minute


def level_for(reason, bars, rej_min):
    """(level, kind) a rejected stock must close above to be re-checked."""
    m = FLIP.search(reason or "")
    if m:
        return float(m.group(1).replace(",", "")), "flip level"
    before = [b[2] for b in bars if _hm(b) < rej_min]
    return (max(before), "day high") if before else (None, None)


def scan(rejected, candles, now, skip=()):
    """rejected: {SYM: reason}. candles(yahoo) -> (bars, ltp).
    Returns {SYM: {"at": HH:MM, "rejected_at": HH:MM, "level": x, "kind": ..., "close": c}}."""
    jobs = {}
    for sym, reason in rejected.items():
        t = TIME.match(reason or "")
        if not t or sym in skip:
            continue
        jobs[sym] = (reason, int(t.group(1)) * 60 + int(t.group(2)))

    def one(item):
        sym, (reason, rej_min) = item
        try:
            bars, _ = candles(sym + ".NS")
        except Exception as e:  # one bad symbol must not stop the rest
            print("recheck candles failed", sym, e)
            return sym, None
        closed = [b for b in bars if b[0].timestamp() + 300 <= now.timestamp()]
        lvl, kind = level_for(reason, closed, rej_min)
        if lvl is None:
            return sym, None
        brk = next((b for b in closed if _hm(b) >= rej_min and b[4] > lvl * (1 + BUFFER)), None)
        if not brk:
            return sym, None
        return sym, {"at": f"{brk[0]:%H:%M}", "rejected_at": f"{rej_min // 60:02d}:{rej_min % 60:02d}",
                     "level": round(lvl, 2), "kind": kind, "close": round(brk[4], 2)}

    with ThreadPoolExecutor(max_workers=8) as ex:
        return {s: r for s, r in ex.map(one, jobs.items()) if r}


def update(state_dir, today, now, candles, load, save, skip=()):
    """Write state/recheck.json; returns the new symbols flagged this run."""
    rej = load(os.path.join(state_dir, "rejected.json"), {})
    path = os.path.join(state_dir, "recheck.json")
    old = load(path, {})
    old = old.get("symbols", {}) if old.get("date") == today else {}
    if rej.get("date") != today:
        if old:
            save(path, {"date": today, "symbols": {}})
        return []
    found = scan(rej.get("symbols", {}), candles, now, skip)
    new = [s for s in found if s not in old or old[s].get("rejected_at") != found[s]["rejected_at"]]
    if found != old:
        save(path, {"date": today, "symbols": found})
    return new
