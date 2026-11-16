"""Offline tests for the Nifty options paper engine (watcher/nifty_paper.py)."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "watcher"))
import nifty_paper as nf  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PLAN = {"side": "CE", "contract": "NIFTY 24500 CE", "strike": 24500, "expiry": "2026-11-18",
        "lot_size": 65, "delta": 0.55, "spot_trigger": 24480, "spot_sl": 24420, "spot_t1": 24560,
        "premium_entry": 140, "premium_max": 144, "premium_sl": 110, "premium_t1": 190,
        "premium_t2": 220, "deadline": "10:45"}


def bars(start_hm, rows):
    h, m = start_hm
    t0 = dt.datetime(2026, 11, 16, h, m, tzinfo=IST)
    return [(t0 + dt.timedelta(minutes=5 * i), *r) for i, r in enumerate(rows)]


class TestCheck(unittest.TestCase):
    def test_pass(self):
        r = nf.check(PLAN, "10:15", {"trades": []}, "2026-11-16")
        self.assertEqual(r["status"], "PASS", r["reasons"])
        self.assertEqual(r["lots"], 1)
        self.assertLessEqual(r["risk"], nf.RISK_PER_TRADE)

    def test_blocks(self):
        bad = {**PLAN, "premium_sl": 135, "delta": 0.2, "expiry": "2026-11-16", "deadline": "14:45"}
        r = nf.check(bad, "14:35", {"trades": []}, "2026-11-16")
        self.assertEqual(r["status"], "BLOCK")
        txt = " ".join(r["reasons"])
        for w in ("noise", "delta", "expiry", "deadline", "after 14:30"):
            self.assertIn(w, txt)

    def test_pe_spot_direction(self):
        r = nf.check({**PLAN, "side": "PE"}, "10:15", {}, "2026-11-16")
        self.assertIn("spot levels", " ".join(r["reasons"]))

    def test_day_limits(self):
        log = {"trades": [{"date": "2026-11-16", "status": "closed", "pnl": -2400},
                          {"date": "2026-11-16", "status": "closed", "pnl": -2700}]}
        r = nf.check(PLAN, "11:15", log, "2026-11-16")
        txt = " ".join(r["reasons"])
        self.assertIn("already today", txt)
        self.assertIn("stopped for the day", txt)
        self.assertIn("daily stop", txt)

    def test_too_expensive_for_one_lot(self):
        r = nf.check({**PLAN, "premium_entry": 400, "premium_max": 405, "premium_sl": 300,
                      "premium_t1": 560, "premium_t2": 600}, "10:15", {}, "2026-11-16")
        self.assertIn("1 lot risks more", " ".join(r["reasons"]))


class TestManage(unittest.TestCase):
    def trade(self, **kw):
        return {**PLAN, "qty": 65, "lots": 1, "status": "pending", **kw}

    def test_fill_then_t1_one_lot_exits_all(self):
        b = bars((10, 20), [(136, 139, 135, 138), (139, 145, 138, 143), (143, 192, 142, 188)])
        t = nf.manage(b, self.trade())
        self.assertEqual(t["status"], "closed")
        self.assertEqual(t["fill"]["price"], 140)
        self.assertEqual(t["result"], "T1")
        self.assertGreater(t["pnl"], 0)

    def test_sl_first_in_same_candle(self):
        b = bars((10, 20), [(141, 195, 105, 150)])
        t = nf.manage(b, self.trade())
        self.assertEqual(t["result"], "SL")
        self.assertLess(t["pnl"], 0)

    def test_gap_over_max_not_filled_after_deadline(self):
        b = bars((10, 35), [(150, 160, 146, 155), (155, 158, 150, 152), (152, 156, 149, 150)])
        t = nf.manage(b, self.trade())
        self.assertEqual(t["status"], "not_filled")

    def test_two_lots_half_then_cost_stop(self):
        b = bars((10, 20), [(140, 142, 139, 141), (141, 191, 140, 185), (185, 186, 139, 140)])
        t = nf.manage(b, self.trade(qty=130, lots=2))
        self.assertEqual(t["result"], "T1+SL at cost")
        self.assertGreater(t["pnl"], 0)

    def test_time_stop(self):
        b = bars((14, 50), [(140, 141, 139, 140), (140, 150, 138, 148), (148, 152, 146, 150)])
        t = nf.manage(b, self.trade(deadline="14:55"))
        self.assertEqual(t["result"], "time stop")


class TestCli(unittest.TestCase):
    def test_check_write_replay_report(self):
        d = tempfile.mkdtemp()
        plan_p = os.path.join(d, "nifty_plan.json")
        with open(plan_p, "w") as f:
            json.dump({"date": "2026-11-16", "verdict_id": "2026-11-16_1015", "plan": dict(PLAN)}, f)
        self.assertEqual(nf.main(["x", "check", plan_p, "--write"]), 0)
        log_p = os.path.join(d, "nifty_paper.json")
        log = json.load(open(log_p))
        self.assertEqual(log["trades"][0]["status"], "pending")
        c_p = os.path.join(d, "c.json")
        with open(c_p, "w") as f:
            json.dump([["2026-11-16T10:20:00+05:30", 139, 141, 138, 140],
                       ["2026-11-16T10:25:00+05:30", 140, 191, 139, 189]], f)
        self.assertEqual(nf.main(["x", "replay", log_p, "2026-11-16_1015", c_p]), 0)
        rep = nf.report(json.load(open(log_p)), 30, dt.date(2026, 11, 16))
        self.assertEqual(rep["wins"], 1)
        self.assertGreater(rep["net_pnl"], 0)


class TestForward(unittest.TestCase):
    def test_forward_once(self):
        os.environ["DRY_RUN"] = "1"
        import watch as w
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "nifty_plan.json"), "w") as f:
            json.dump({"date": "2026-11-16", "verdict_id": "2026-11-16_1015", "plan": None}, f)
        with open(os.path.join(d, "nifty_verdict.txt"), "w") as f:
            f.write("🧪 PAPER test")
        sent = []
        real = w.telegram
        w.telegram = lambda text, to=None: sent.append(text)
        try:
            watch = {}
            self.assertTrue(w.nifty_step(d, "2026-11-16", watch))
            self.assertFalse(w.nifty_step(d, "2026-11-16", watch))
            self.assertFalse(w.nifty_step(d, "2026-11-17", {}))
        finally:
            w.telegram = real
        self.assertEqual(sent, ["🧪 PAPER test"])


if __name__ == "__main__":
    unittest.main()
