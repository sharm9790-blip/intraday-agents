"""Download ~2 years of 5-min candles for a backtest basket from INDmoney, through the
worker's read-only ?candles=…&from=&to= endpoint (≤ 7 days per call, 20 symbols per call).
Runs in GitHub Actions (backtest-data.yml), where WORKER_URL / WORKER_KEY live.

    python3 watcher/bt_fetch.py out_dir [weeks=104] [n_stocks=40]

Writes out_dir/<SYMBOL>.json = [[ts_epoch_s, o, h, l, c, v], ...] and out_dir/meta.json.
Stops going further back once 4 weeks in a row come back empty (end of INDmoney's history).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))


def basket(n):
    u = json.load(open(os.path.join(HERE, "universe.json")))["stocks"]
    fo = sorted((s for s, v in u.items() if v.get("fo")), key=lambda s: -u[s]["tv"])
    return fo[:n] + ["NIFTYBEES"]


def get(syms, a, b):
    url = (os.environ["WORKER_URL"].rstrip("/") + "/?candles=" + urllib.parse.quote(",".join(syms))
           + f"&from={a}&to={b}")
    req = urllib.request.Request(url, headers={"x-worker-key": os.environ["WORKER_KEY"],
                                               "User-Agent": "intraday-backtest"})
    for k in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            if d.get("ok"):
                return d.get("candles") or {}
            print("  worker:", d.get("error"))
        except Exception as e:
            print("  retry", k, e)
        time.sleep(5 * (k + 1))
    return None


def main(out, weeks=104, n=40):
    os.makedirs(out, exist_ok=True)
    syms = basket(n)
    data = {s: {} for s in syms}
    end = date.today() - timedelta(days=1)
    empty, done = 0, 0
    for w in range(weeks):
        b = end - timedelta(days=7 * w)
        a = b - timedelta(days=6)
        got = 0
        for i in range(0, len(syms), 20):
            res = get(syms[i:i + 20], a.isoformat(), b.isoformat())
            if res is None:
                continue
            for s, rows in res.items():
                for r in rows:
                    data.setdefault(s, {})[r[0]] = r
                got += len(rows)
            time.sleep(1.0)
        done += 1
        print(f"week {w + 1:3d} {a}..{b}: {got} candles")
        empty = empty + 1 if got == 0 else 0
        if empty >= 4:
            print("4 empty weeks in a row: end of history")
            break
    for s, rows in data.items():
        with open(os.path.join(out, s + ".json"), "w") as f:
            json.dump(sorted(rows.values()), f, separators=(",", ":"))
    first = min((min(r) for r in data.values() if r), default=None)
    json.dump({"symbols": syms, "weeks_tried": done, "first_ts": first,
               "candles": {s: len(r) for s, r in data.items()}}, open(os.path.join(out, "meta.json"), "w"), indent=0)
    print("done:", sum(len(r) for r in data.values()), "candles")


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0], int(a[1]) if len(a) > 1 else 104, int(a[2]) if len(a) > 2 else 40)
