"""Offline tests for the strategy lab paper engines (watcher/strategies.py)."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "watcher"))
import strategies as S  # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = dt.date(2026, 11, 25)


def session(day, closes, vol=1000, start=(9, 15), spread=0.1):
    """5-min candles from a list of closes; open = previous close, ±spread% wicks."""
    t0 = dt.datetime(day.year, day.month, day.day, *start, tzinfo=IST)
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        h, l = max(o, c) * (1 + spread / 100), min(o, c) * (1 - spread / 100)
        out.append((t0 + dt.timedelta(minutes=5 * i), o, h, l, c, vol))
        prev = c
    return out


def prev_day(close):
    return session(DAY - dt.timedelta(days=1), [close] * 75)


def flat_nifty():
    return prev_day(24000) + session(DAY, [24000] * 75)


class TestManage(unittest.TestCase):
    def test_long_t1_then_t2(self):
        bars = session(DAY, [100, 100, 101, 102, 103.5])
        r, steps = S.manage(bars, 1, "long", 100, 99, 101.5, 103, "15:00")
        self.assertIn("T1", " ".join(steps))
        self.assertIn("T2", " ".join(steps))
        self.assertAlmostEqual(r, 0.5 * 1.5 + 0.5 * 3 - S.COST * 100, places=2)

    def test_short_sl(self):
        bars = session(DAY, [100, 100, 101, 102])
        r, steps = S.manage(bars, 1, "short", 100, 101, 98.5, 97, "15:00")
        self.assertEqual(steps[-1].split()[-1], "SL")
        self.assertLess(r, -1)

    def test_time_stop(self):
        bars = session(DAY, [100] * 75)
        r, steps = S.manage(bars, 1, "long", 100, 99, 102, 104, "14:45")
        self.assertTrue(steps[-1].startswith("14:45"))


class TestVwap(unittest.TestCase):
    def test_long_reversion(self):
        # flat at 100 until 10:30, slides to ~97.3, turns green, then drifts back to 100
        closes = [100] * 15 + [99.6, 99.2, 98.8, 98.4, 98.0, 97.6, 97.3, 97.5] + [97.8 + 0.3 * i for i in range(8)] + [100] * 45
        data = {"AAA": prev_day(100) + session(DAY, closes, spread=0.05)}
        res = S.day_trades("vwap", DAY, data, flat_nifty())
        self.assertEqual(len(res), 1, res)
        t = res[0]
        self.assertEqual(t["side"], "long")
        self.assertGreater(t["r"], 0)
        self.assertEqual(t["steps"][-1].split()[-1], "target")

    def test_no_trade_when_quiet(self):
        data = {"AAA": prev_day(100) + session(DAY, [100] * 75)}
        self.assertEqual(S.day_trades("vwap", DAY, data, flat_nifty()), [])


class TestGapFade(unittest.TestCase):
    def test_gap_up_fade(self):
        # prev close 100, opens 104, holds 3 candles, breaks the opening-range low, fills
        closes = [104, 104.2, 104.1, 103.0, 102.4, 101.8, 101.0, 100.4, 100.0] + [100] * 66
        bars = session(DAY, closes, spread=0.05)
        bars[0] = (bars[0][0], 104, 104.3, 103.9, 104, 1000)
        data = {"GAP": prev_day(100) + bars}
        res = S.day_trades("gapfade", DAY, data, flat_nifty())
        self.assertEqual(len(res), 1, res)
        self.assertEqual(res[0]["side"], "short")
        self.assertGreater(res[0]["r"], 0)
        self.assertIn("T1", " ".join(res[0]["steps"]))

    def test_small_gap_ignored(self):
        data = {"G": prev_day(100) + session(DAY, [101] * 75)}
        self.assertEqual(S.day_trades("gapfade", DAY, data, flat_nifty()), [])


def chain(spot, prices, expiry="2026-12-01"):
    return {"underlying_ltp": spot, "expiry": expiry,
            "strikes": {str(float(k)): {"ce": {"last_price": ce}, "pe": {"last_price": pe}}
                        for k, (ce, pe) in prices.items()}}


class TestFly(unittest.TestCase):
    OPEN = chain(24010, {24000: (100, 95), 24200: (30, 300), 23800: (290, 28)})

    def test_open_and_target(self):
        now = dt.datetime(2026, 11, 25, 9, 20, tzinfo=IST)
        t, err = S.fly_open(self.OPEN, now)
        self.assertIsNone(err)
        self.assertEqual(t["atm"], 24000)
        self.assertEqual(t["credit"], 100 + 95 - 30 - 28)
        # premiums decay: body worth 55 + 50, wings 12 + 10
        later = chain(24000, {24000: (55, 50), 24200: (12, 300), 23800: (290, 10)})
        mtm = S.fly_mtm(t, later)
        self.assertGreater(mtm, S.TARGET)
        S.fly_update(t, mtm, now.replace(hour=12))
        self.assertEqual(t["status"], "closed")
        self.assertIn("target", t["exit"])
        self.assertNotIn("done", t)          # keeps recording "hold" until 15:15
        S.fly_update(t, mtm - 500, now.replace(hour=15, minute=15))
        self.assertTrue(t["done"])
        self.assertEqual(t["hold"], mtm - 500)
        self.assertEqual(t["pnl"], mtm)

    def test_stop(self):
        now = dt.datetime(2026, 11, 25, 9, 20, tzinfo=IST)
        t, _ = S.fly_open(self.OPEN, now)
        crash = chain(23700, {24000: (8, 300), 24200: (2, 500), 23800: (240, 110)})
        mtm = S.fly_mtm(t, crash)
        S.fly_update(t, mtm, now.replace(hour=11))
        self.assertEqual(t["status"], "closed")
        self.assertIn("stop", t["exit"])
        self.assertLessEqual(-mtm, t["max_loss"] + 1000)   # wings cap the loss (plus costs)

    def test_missing_leg(self):
        t, err = S.fly_open(chain(24010, {24000: (120, 110)}), dt.datetime(2026, 11, 25, 9, 20, tzinfo=IST))
        self.assertIsNone(t)
        self.assertIn("missing", err)

    def test_skips(self):
        self.assertIn("expiry day", S.fly_skip("2026-12-01", {"expiry": "2026-12-01"}, 0.2, 1, [])[0])
        self.assertTrue(S.fly_skip("2026-11-25", {}, 0.1, 2, ["RBI policy"])[0].startswith("event day"))
        self.assertIn("gap", S.fly_skip("2026-11-25", {}, -1.4, 0, [])[0])
        self.assertIn("VIX", S.fly_skip("2026-11-25", {}, 0, 12, [])[0])
        self.assertEqual(S.fly_skip("2026-11-25", {"expiry": "2026-12-01"}, 0.3, 2, []), [])

    def test_expiries_are_tuesdays(self):
        e = S.next_expiries(dt.date(2026, 11, 26))
        self.assertEqual(e[0], "2026-12-01")
        self.assertTrue(all(dt.date.fromisoformat(x).weekday() == 1 for x in e))


class TestScorecard(unittest.TestCase):
    def test_lines_and_event_split(self):
        doc = {}
        S.record_stock_day(doc, "2026-11-24", {"vwap": [{"symbol": "AAA", "side": "long", "at": "11:05", "r": 0.8}],
                                               "gapfade": []}, ["RBI policy"])
        S.record_stock_day(doc, "2026-11-25", {"vwap": [], "gapfade": [
            {"symbol": "GAP", "side": "short", "at": "09:50", "r": -1.1}]}, [])
        doc["days"]["2026-11-25"]["fly"] = {"atm": 24000, "credit": 155, "pnl": 2100, "hold": 1500,
                                            "exit": "12:00 target", "legs": [], "done": True}
        s = S.summary(doc)
        self.assertEqual(s["vwap"]["ev_n"], 1)
        self.assertEqual(s["gapfade"]["rs"], -1100)
        self.assertEqual(s["fly"]["rs"], 2100)
        txt = "\n".join(S.lines(doc, "2026-11-25", lambda x: f"₹{x:,.0f}"))
        for w in ("Strategy lab", "GAP", "Iron fly 24000", "hold to 15:15", "event days"):
            self.assertIn(w, txt)

    def test_empty(self):
        self.assertEqual(S.lines({}, "2026-11-25", str), [])


if __name__ == "__main__":
    unittest.main()
