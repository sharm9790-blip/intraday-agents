"""Offline tests for the watcher, scorecard, paper account, health and My Trade bot.

    python3 -m unittest discover -s tests -v

No network: candles, news and Telegram are replaced with fakes; the clock is fixed.
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "watcher"))
os.environ["DRY_RUN"] = "1"
os.environ["TELEGRAM_CHAT_ID"] = "111,222"
os.environ["CLAUDE_FIRE_URL"] = "x"
os.environ["CLAUDE_FIRE_TOKEN"] = "y"

import health  # noqa: E402
import mytrade as mt  # noqa: E402
import news  # noqa: E402
import nse_ids  # noqa: E402
import premarket  # noqa: E402
import recheck  # noqa: E402
import replay  # noqa: E402
import risk  # noqa: E402
import scan  # noqa: E402
import stats  # noqa: E402
import watch as w  # noqa: E402

REAL_CANDLES = w.candles

IST = w.IST
CLOCK = [dt.datetime(2026, 11, 9, 10, 0, tzinfo=IST)]


class FakeDT(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return CLOCK[0]


w.datetime = FakeDT
mt.datetime = FakeDT

PLAN = {"symbol": "TATASTEEL", "yahoo": "TATASTEEL.NS", "side": "long", "name": "Tata Steel",
        "zone_low": 183.2, "zone_high": 183.6, "entry": 183.5, "max_entry": 183.75,
        "trigger_close_above": 183.4, "sl": 182.6, "t1": 185.9, "t2": 187.0, "qty": 544,
        "deadline": "14:30", "cancel_if_high_reaches": 186.5, "cancel_if_close_below": 182.9,
        "exit_if_close_below_after_entry": 182.85, "time_stop": "15:10"}


def bars_from(start, rows):
    """rows: (open, high, low, close) per 5-min candle from start."""
    return [(start + dt.timedelta(minutes=5 * i), o, h, lo, c, 1000) for i, (o, h, lo, c) in enumerate(rows)]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sent = []
        self._tg = w.telegram
        w.telegram = lambda text, to=None: self.sent.append((to, text))
        news.check = lambda *a, **k: {"level": "none", "flagged": [], "count": 0}
        w.stock_name = lambda s: "Tata Steel"
        self.rows = []
        self.start = dt.datetime(2026, 11, 9, 13, 45, tzinfo=IST)

        self.rows_by = {}

        self.bars_by = {}  # full 5-day bar lists (premarket tests)
        self._uni = scan.load_universe
        scan.load_universe = lambda d: {"stocks": {}}  # scanner off unless a test turns it on

        def fake_candles(sym, days="1d", *a):
            now = CLOCK[0]
            if sym in self.bars_by:
                b = [x for x in self.bars_by[sym] if x[0] <= now
                     and (days == "5d" or x[0].date() == now.date())]
            else:
                b = [x for x in bars_from(self.start, self.rows_by.get(sym, self.rows)) if x[0] <= now]
            return b, (b[-1][4] if b else None)
        w.candles = fake_candles

    def tearDown(self):
        w.telegram = self._tg
        scan.load_universe = self._uni
        shutil.rmtree(self.dir)

    def put(self, name, data):
        with open(os.path.join(self.dir, name), "w") as f:
            json.dump(data, f)

    def get(self, name):
        with open(os.path.join(self.dir, name)) as f:
            return json.load(f)

    def at(self, h, m):
        CLOCK[0] = dt.datetime(2026, 11, 9, h, m, tzinfo=IST)

    def texts(self):
        return "\n".join(t for _, t in self.sent)


class TestRisk(unittest.TestCase):
    def test_pass_and_size(self):
        r = risk.check(PLAN, "13:45")
        self.assertEqual(r["status"], "PASS")
        self.assertLessEqual(r["risk"], 1000)
        self.assertLessEqual(r["value"], 100000)

    def test_block(self):
        bad = {**PLAN, "t1": 183.9, "deadline": "14:45"}
        r = risk.check(bad, "14:35", 3)
        self.assertEqual(r["status"], "BLOCK")
        self.assertEqual(len(r["reasons"]), 4)


class TestReplay(unittest.TestCase):
    def test_sl_first_in_same_candle(self):
        b = bars_from(dt.datetime(2026, 11, 9, 10, 0, tzinfo=IST), [(183.5, 186.0, 182.0, 184)])
        pnl, steps = replay.manage(b, 0, 183.5, PLAN)
        self.assertIn("SL", steps[0])
        self.assertLess(pnl, 0)

    def test_t1_then_t2(self):
        b = bars_from(dt.datetime(2026, 11, 9, 10, 0, tzinfo=IST),
                      [(183.5, 184, 183.3, 183.9), (183.9, 186, 183.8, 185.9), (185.9, 187.2, 185.8, 187)])
        pnl, steps = replay.manage(b, 0, 183.5, PLAN)
        self.assertEqual([s.split()[-1] for s in steps], ["T1", "T2"])
        self.assertGreater(pnl, 1400)


class TestNseIds(unittest.TestCase):
    def test_weekly_refresh(self):
        store, calls = {}, []
        orig = nse_ids.build
        nse_ids.build = lambda: calls.append(1) or {"SBIN": 3045}
        try:
            save = lambda p, d: store.__setitem__(p, d)
            load = lambda p, d: store.get(p, d)
            t0 = dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)
            self.assertTrue(nse_ids.refresh("s", save, load, t0))           # missing → build
            self.assertFalse(nse_ids.refresh("s", save, load, t0 + dt.timedelta(days=3)))
            self.assertTrue(nse_ids.refresh("s", save, load, t0 + dt.timedelta(days=8)))
            self.assertEqual(len(calls), 2)
        finally:
            nse_ids.build = orig


PRE = {"symbol": "KALYANKJIL", "yahoo": "KALYANKJIL.NS", "name": "Kalyan Jewellers",
       "buy_above": 101.5, "zone_low": 101.5, "zone_high": 102.2, "entry": 101.6, "max_entry": 102.2,
       "trigger_close_above": 101.5, "sl": 100.9, "t1": 104.5, "t2": 106.0, "qty": 900,
       "deadline": "09:45", "cancel_if_high_reaches": 104.5, "cancel_if_close_below": 100.9,
       "exit_if_close_below_after_entry": 101.0, "time_stop": "15:00", "keep": True}


def day_bars(day, rows, h=9, m=15):
    """rows: (open, high, low, close, volume) per 5-min candle from h:m on day."""
    t0 = dt.datetime(2026, 11, day, h, m, tzinfo=IST)
    return [(t0 + dt.timedelta(minutes=5 * i), *r) for i, r in enumerate(rows)]


class TestPremarket(Base):
    def setUp(self):
        super().setUp()
        prev = day_bars(6, [(99.5, 100, 99.4, 99.9, 1000), (99.9, 100.1, 99.8, 100.0, 800)])
        self.bars_by["KALYANKJIL.NS"] = prev + day_bars(9, [
            (101.0, 102.0, 100.9, 101.9, 2000), (101.9, 102.1, 101.7, 102.0, 1500)])
        self.bars_by["HINDCOPPER.NS"] = prev + day_bars(9, [(105.0, 106, 104.5, 105.5, 5000)])
        gap = {**PRE, "symbol": "HINDCOPPER", "yahoo": "HINDCOPPER.NS", "buy_above": 104,
               "zone_low": 104, "entry": 104.1, "max_entry": 106.5, "zone_high": 106.5,
               "sl": 103, "t1": 110, "t2": 112}
        self.put("premarket.json", {"date": "2026-11-09", "stage": "preopen", "id": "2026-11-09_0910",
                                    "stocks": [PRE, gap]})
        with open(os.path.join(self.dir, "premarket_msg.txt"), "w") as f:
            f.write("📋 <b>FINAL WATCHLIST</b>")

    def test_fires_before_open(self):
        st = {}
        self.assertTrue(w.fire_premarket(dt.datetime(2026, 11, 9, 8, 45, tzinfo=IST), st))
        self.assertFalse(w.fire_premarket(dt.datetime(2026, 11, 9, 8, 50, tzinfo=IST), st))
        self.assertFalse(w.fire_premarket(dt.datetime(2026, 11, 9, 9, 1, tzinfo=IST), st))
        self.assertTrue(w.fire_premarket(dt.datetime(2026, 11, 9, 9, 8, tzinfo=IST), st))
        self.assertEqual(st["pre_fired"], ["pre", "open"])
        self.assertFalse(w.fire_premarket(dt.datetime(2026, 11, 7, 8, 45, tzinfo=IST), {}))  # Saturday

    def test_breakout_becomes_buy_now_and_gap_dropped(self):
        self.at(9, 21)
        w.main(self.dir)
        t = self.texts()
        self.assertIn("FINAL WATCHLIST", t)
        self.assertIn("BUY NOW — KALYANKJIL", t)
        self.assertIn("Premarket pick: 5-min close ₹101.9 above ₹101.5, volume 2.0× usual", t)
        wt = self.get("watch.json")
        self.assertEqual([e["symbol"] for e in w.slots(wt)], ["KALYANKJIL"])
        self.assertIn("gap-up 5.0%", wt["pre_done"]["HINDCOPPER"])
        self.at(9, 26)
        w.main(self.dir)
        self.assertEqual(self.texts().count("FINAL WATCHLIST"), 1, "watchlist sent once")
        self.at(9, 41)
        w.main(self.dir)
        self.assertIn("PREMARKET LIST DONE", self.texts())

    def test_evaluate_rules(self):
        now = dt.datetime(2026, 11, 9, 9, 21, tzinfo=IST)
        prev = day_bars(6, [(99.5, 100, 99.4, 100.0, 1000)])
        weak = prev + day_bars(9, [(101.0, 102.0, 100.9, 101.9, 1100)])  # 1.1× volume only
        self.assertEqual(premarket.evaluate(PRE, weak, now)[0], "wait")
        self.assertEqual(premarket.evaluate(PRE, weak, now.replace(minute=40))[0], "drop")
        far = prev + day_bars(9, [(101.0, 103.0, 100.9, 102.9, 3000)])
        self.assertIn("don't chase", premarket.evaluate(PRE, far, now)[1])
        ok = prev + day_bars(9, [(101.0, 102.0, 100.9, 101.9, 3000)])
        self.assertEqual(premarket.evaluate(PRE, ok, now)[0], "buy")

    def test_risk_cli_drops_bad_rr(self):
        bad = {**PRE, "symbol": "X", "t1": 102.5}
        self.put("pm.json", {"stocks": [PRE, bad]})
        self.assertEqual(premarket.main(os.path.join(self.dir, "pm.json"), True), 0)
        st = self.get("pm.json")["stocks"]
        self.assertTrue(st[0]["keep"])
        self.assertFalse(st[1]["keep"])
        self.assertIn("R:R", st[1]["why"])


class TestChase(Base):
    def test_ran_up_dip_becomes_buy_now(self):
        plan = {**PLAN, "t1": 187.0, "t2": 188.0, "deadline": "13:55", "cancel_if_high_reaches": 187.5}
        self.put("plan.json", {"date": "2026-11-09", "verdict_id": "2026-11-09_1345", "go_count": 1,
                               "plan": plan})
        with open(os.path.join(self.dir, "latest_verdict.txt"), "w") as f:
            f.write("🟢 <b>GO — TATASTEEL</b>")
        self.rows = [(183.7, 183.9, 183.65, 183.85), (183.85, 184.0, 183.7, 183.9)]
        self.at(13, 46); w.main(self.dir)
        self.at(13, 56); w.main(self.dir)
        t = self.texts()
        self.assertNotIn("CANCELLED", t)
        self.assertIn("BUY NOW — TATASTEEL", t)
        self.assertIn("Ran up without the dip", t)

    def test_ran_up_too_far_is_cancelled(self):
        plan = {**PLAN, "deadline": "13:55"}  # T1 185.9: buying 184.4 gives R:R < 1.5
        self.put("plan.json", {"date": "2026-11-09", "verdict_id": "2026-11-09_1345", "go_count": 1,
                               "plan": plan})
        self.rows = [(183.7, 184.2, 183.65, 184.1), (184.1, 184.5, 184.0, 184.4)]
        self.at(13, 46); w.main(self.dir)
        self.at(13, 56); w.main(self.dir)
        self.assertIn("CANCELLED — TATASTEEL", self.texts())


def trend_bars(day, start_px, n, step=0.15, vol=1000, h=9, m=15):
    """n rising 5-min candles: each closes `step` higher, lows rising."""
    rows, px = [], start_px
    for _ in range(n):
        rows.append((px, px + step + 0.05, px - 0.05, px + step, vol))
        px += step
    return day_bars(day, rows, h, m)


class TestScanner(Base):
    def test_rank_mode_and_filters(self):
        uni = {"stocks": {"AAA": {"fo": True, "tv": 500, "ind": "Banks"},
                          "BBB": {"fo": False, "tv": 200, "ind": "FMCG"},
                          "CCC": {"fo": True, "tv": 300, "ind": "IT"}}}
        prev = lambda px: day_bars(6, [(px, px, px, px, 1000)] * 24)
        self.bars_by = {
            "^NSEI": prev(100) + trend_bars(9, 100.2, 24, 0.02),                 # Nifty +0.7%
            "AAA.NS": prev(100) + trend_bars(9, 100.5, 24, 0.05, vol=3000),       # +1.7%, 3× volume
            "BBB.NS": prev(100) + trend_bars(9, 108.0, 24, 0.05, vol=3000),       # non-F&O +9%: band risk
            "CCC.NS": prev(100) + trend_bars(9, 100.5, 24, 0.05, vol=900),        # no volume surge
        }
        self.at(11, 15)
        doc = scan.run(self.dir, CLOCK[0], w.candles, uni, workers=2)
        self.assertEqual([i["symbol"] for i in doc["items"]], ["AAA"])
        self.assertEqual(doc["mode"], "trend")
        self.assertEqual(doc["items"][0]["vol_x"], 3.0)
        self.assertTrue(scan.stock_trend(self.bars_by["AAA.NS"], CLOCK[0]))

    def test_atr_stop_block(self):
        r = risk.check({**PLAN, "atr5": 2.0}, "13:45")
        self.assertEqual(r["status"], "BLOCK")
        self.assertIn("inside normal 5-min noise", r["reasons"][0])
        self.assertEqual(risk.check({**PLAN, "atr5": 1.0}, "13:45")["status"], "PASS")

    def test_trend_day_turns_dip_into_buy_now(self):
        plan = {**PLAN, "entry_type": "dip", "zone_low": 182.9, "zone_high": 183.2, "entry": 183.0,
                "max_entry": 183.3, "trigger_close_above": 183.0, "sl": 182.4, "t1": 186.5, "t2": 187.5,
                "cancel_if_close_below": 182.6, "exit_if_close_below_after_entry": 182.5,
                "cancel_if_high_reaches": 187.0}
        self.bars_by["TATASTEEL.NS"] = trend_bars(9, 182.0, 24, 0.06)          # 182 → 183.4, rising
        self.put("candidates.json", {"date": "2026-11-09", "mode": "trend", "items": []})
        self.put("plan.json", {"date": "2026-11-09", "verdict_id": "2026-11-09_1115", "go_count": 1,
                               "plan": plan})
        with open(os.path.join(self.dir, "latest_verdict.txt"), "w") as f:
            f.write("🟢 <b>GO — TATASTEEL</b>")
        self.at(11, 16)
        w.main(self.dir)
        t = self.texts()
        self.assertIn("Trend day: BUY NOW", t)
        e = self.get("watch.json")["live"][0]
        self.assertEqual(e["plan"]["entry_type"], "now")
        self.assertIn("BUY NOW — TATASTEEL", t)

    def test_late_recheck_fires_once(self):
        self.put("recheck.json", {"date": "2026-11-09", "symbols": {
            "PAYTM": {"at": "14:20", "rejected_at": "13:30", "level": 1897, "kind": "flip level", "close": 1901}}})
        st = {}
        self.assertFalse(w.fire_late_recheck(dt.datetime(2026, 11, 9, 14, 12, tzinfo=IST), self.dir, "2026-11-09", st))
        self.assertTrue(w.fire_late_recheck(dt.datetime(2026, 11, 9, 14, 21, tzinfo=IST), self.dir, "2026-11-09", st))
        self.assertFalse(w.fire_late_recheck(dt.datetime(2026, 11, 9, 14, 22, tzinfo=IST), self.dir, "2026-11-09", st))


class TestFire(unittest.TestCase):
    def test_window_and_dedupe(self):
        st = {}
        self.assertFalse(w.fire_debate(dt.datetime(2026, 11, 9, 9, 40, tzinfo=IST), 0, None, st))
        self.assertTrue(w.fire_debate(dt.datetime(2026, 11, 9, 9, 46, tzinfo=IST), 0, None, st))
        self.assertFalse(w.fire_debate(dt.datetime(2026, 11, 9, 9, 51, tzinfo=IST), 0, None, st))
        self.assertFalse(w.fire_debate(dt.datetime(2026, 11, 9, 14, 36, tzinfo=IST), 0, None, {}))
        self.assertTrue(w.fire_debate(dt.datetime(2026, 11, 9, 14, 16, tzinfo=IST), 0, None, {}))
        self.assertFalse(w.fire_debate(dt.datetime(2026, 11, 9, 14, 30, tzinfo=IST), 0, None, {}))
        self.assertFalse(w.fire_debate(dt.datetime(2026, 11, 9, 11, 0, tzinfo=IST), 3, None, {}))


class TestWatcherFlow(Base):
    def go(self):
        self.put("plan.json", {"date": "2026-11-09", "verdict_id": "2026-11-09_1345", "go_count": 1, "plan": PLAN})
        with open(os.path.join(self.dir, "latest_verdict.txt"), "w") as f:
            f.write("🟢 <b>GO — TATASTEEL</b>")

    def test_trigger_t1_t2_with_paper(self):
        self.go()
        self.rows = [(183.45, 183.7, 183.3, 183.6), (183.6, 184.5, 183.5, 184.4),
                     (184.4, 186.0, 184.3, 185.9), (185.9, 187.2, 185.8, 187.1)]
        for h, m in [(13, 46), (13, 51), (14, 1), (14, 6)]:
            self.at(h, m)
            w.main(self.dir)
        t = self.texts()
        self.assertIn("GO — TATASTEEL", t)
        self.assertIn("CHECKS — TATASTEEL", t)
        self.assertIn("BUY TRIGGER", t)
        self.assertIn("TARGET 1 HIT", t)
        self.assertIn("+₹653 booked", t)
        self.assertIn("TARGET 2 HIT", t)
        self.assertIn("+₹1,605", t)
        self.assertIn('Remove TATASTEEL from "My Draft Basket"', t)
        # never bought: 15 min after the trigger it stops counting and frees its slot,
        # but the target / SL cards still come, with a note
        wt = self.get("watch.json")
        self.assertEqual(wt["trades"], 0)
        self.assertEqual(t.count("SLOT FREED — TATASTEEL"), 1)
        self.assertIn("No buy of this seen", t[t.index("TARGET 2 HIT"):])
        self.assertEqual(w.slots(wt), [])

    def test_bought_trigger_keeps_counting(self):
        self.go()
        self.rows = [(183.45, 183.7, 183.3, 183.6), (183.6, 184.5, 183.5, 184.4),
                     (184.4, 185.0, 184.3, 184.8), (184.8, 185.0, 184.5, 184.9)]
        self.at(13, 46); w.main(self.dir)
        self.at(13, 51); w.main(self.dir)
        self.assertEqual(self.get("watch.json")["trades"], 1, "counts while your fill shows up")
        self.put("mytrades.json", {"date": "2026-11-09", "trades": {"1": [
            {"symbol": "TATASTEEL", "opened": "13:53", "qty": 544, "left": 544},
            {"symbol": "KALYANKJIL", "opened": "10:41", "qty": 10, "left": 0}]}})
        self.at(14, 6); w.main(self.dir)
        self.at(14, 11); w.main(self.dir)
        wt = self.get("watch.json")
        self.assertEqual(wt["trades"], 1, "only TATASTEEL is still open")
        self.assertEqual(wt["day_trades"], 2, "both real buys count today, plan or not")
        self.assertNotIn("SLOT FREED", self.texts())
        self.assertEqual([e["symbol"] for e in w.slots(wt)], ["TATASTEEL"])

    def test_three_open_then_sell_frees(self):
        fired = []
        orig = w.fire_debate
        w.fire_debate = lambda now, trades, vid, watch: fired.append(1) or False
        try:
            self.rows = [(185, 185.5, 184.8, 185.2)] * 40
            hold = lambda *left: {"date": "2026-11-09", "trades": {"1": [
                {"symbol": s, "opened": "10:00", "qty": 10, "left": l}
                for s, l in zip(["A", "B", "C"], left)]}}
            self.put("mytrades.json", hold(10, 10, 10))
            self.at(11, 0); w.main(self.dir)
            self.assertEqual(self.get("watch.json")["trades"], 3)
            self.assertEqual(fired, [], "3 open → no scan")
            self.put("mytrades.json", hold(0, 10, 10))   # first one filled and closed
            self.at(11, 5); w.main(self.dir)
            wt = self.get("watch.json")
            self.assertEqual((wt["trades"], wt["day_trades"]), (2, 3))
            self.assertEqual(len(fired), 1, "a slot is free → scan")
            # daily safety cap: 6 stocks today, even with nothing open
            self.put("mytrades.json", {"date": "2026-11-09", "trades": {"1": [
                {"symbol": s, "opened": "10:00", "qty": 1, "left": 0} for s in "ABCDEF"]}})
            self.at(11, 10); w.main(self.dir)
            self.assertEqual(len(fired), 1, "6 today → no more scans")
            self.assertIn("6 trades taken today", self.texts())
        finally:
            w.fire_debate = orig

    def test_open_order_counts_and_keeps_slot(self):
        self.go()
        self.rows = [(183.45, 183.7, 183.3, 183.6), (183.6, 184.5, 183.5, 184.4),
                     (184.4, 185.0, 184.3, 184.8), (184.8, 185.0, 184.5, 184.9)]
        # limit buy placed in INDmoney but not filled, plus an order for another stock
        self.put("positions_ind.json", {"date": "2026-11-09", "positions": [], "orders": [
            {"s": "TATASTEEL", "q": 544, "p": 183.5}, {"s": "PFC", "q": 10, "p": 400}]})
        self.at(13, 46); w.main(self.dir)
        self.at(13, 51); w.main(self.dir)
        self.at(14, 6); w.main(self.dir)
        wt = self.get("watch.json")
        self.assertNotIn("SLOT FREED", self.texts(), "an open order is not a missed trade")
        self.assertEqual(wt["trades"], 2)
        self.assertEqual(wt["taken"], ["PFC", "TATASTEEL"])
        # order cancelled, nothing bought → 15 min after the trigger it frees the slot
        self.put("positions_ind.json", {"date": "2026-11-09", "positions": [], "orders": []})
        self.at(14, 11); w.main(self.dir)
        self.assertIn("SLOT FREED — TATASTEEL", self.texts())
        self.assertEqual(self.get("watch.json")["trades"], 0)

    def test_missed_then_bought_counts_again(self):
        self.go()
        self.rows = [(183.45, 183.7, 183.3, 183.6), (183.6, 184.5, 183.5, 184.4),
                     (184.4, 185.0, 184.3, 184.8), (184.8, 185.0, 184.5, 184.9)]
        self.at(13, 46); w.main(self.dir)
        self.at(13, 51); w.main(self.dir)
        self.at(14, 6); w.main(self.dir)
        self.assertEqual(self.get("watch.json")["trades"], 0)
        self.put("mytrades.json", {"date": "2026-11-09", "trades": {"1": [
            {"symbol": "TATASTEEL", "opened": "14:08", "qty": 544, "left": 544}]}})
        self.at(14, 11); w.main(self.dir)
        wt = self.get("watch.json")
        self.assertEqual(wt["trades"], 1)
        self.assertEqual([e["symbol"] for e in w.slots(wt)], ["TATASTEEL"])

    def test_buy_now(self):
        plan = dict(PLAN, entry_type="now", zone_low=183.4, zone_high=183.8, entry=183.6, max_entry=183.9)
        self.put("plan.json", {"date": "2026-11-09", "verdict_id": "2026-11-09_1345", "go_count": 1, "plan": plan})
        with open(os.path.join(self.dir, "latest_verdict.txt"), "w") as f:
            f.write("🟢 <b>GO — TATASTEEL</b>")
        self.rows = [(183.45, 183.7, 183.3, 183.6), (183.6, 184.5, 183.5, 184.4),
                     (184.4, 186.0, 184.3, 185.9)]
        self.at(13, 46)
        w.main(self.dir)
        self.assertIn("BUY NOW — TATASTEEL", self.texts())   # no wait for a green dip candle
        self.assertEqual(self.get("watch.json")["trades"], 1)
        bars = bars_from(self.start, self.rows)
        r = replay.replay(dict(plan, start="13:45"), bars)
        self.assertTrue(r["rules"]["bought"])
        self.assertEqual(r["rules"]["steps"][0], "bought now 183.45")

    def test_cancel_at_deadline(self):
        self.go()
        self.rows = [(185, 185.5, 184.8, 185.2)] * 12
        self.at(13, 46)
        w.main(self.dir)
        self.at(14, 32)
        w.main(self.dir)
        self.assertIn("CANCELLED — TATASTEEL", self.texts())
        self.assertIn("Not triggered by 14:30", self.texts())

    def test_holiday_silent(self):
        CLOCK[0] = dt.datetime(2026, 11, 10, 10, 0, tzinfo=IST)
        w.main(self.dir)
        self.assertEqual(self.sent, [])
        self.assertTrue(self.get("watch.json")["holiday"])   # worker dead-man stays quiet

    def test_scorecard_and_paper(self):
        self.go()
        self.rows = [(183.45, 183.7, 183.3, 183.6), (183.6, 186.0, 183.5, 185.9)] + \
                    [(185.9, 186.2, 185.5, 186)] * 20
        self.at(13, 46)
        w.main(self.dir)
        self.at(15, 46)
        w.main(self.dir)
        self.assertIn("SCORECARD", self.texts())
        self.assertIn("🎯 Expectancy", self.texts())
        self.assertIn("too few to judge yet", self.texts())
        acc = self.get("paper.json")
        self.assertEqual(len(acc["days"]["2026-11-09"]), 1)
        self.assertGreater(acc["balance"], 100000)


class TestMultiPlan(Base):
    """Up to 3 plans live at once; scans keep running while there is room."""
    def verdict(self, vid, plan):
        self.put("plan.json", {"date": "2026-11-09", "verdict_id": vid, "go_count": 1, "plan": plan})
        with open(os.path.join(self.dir, "latest_verdict.txt"), "w") as f:
            f.write(f"🟢 <b>GO — {plan['symbol'] if plan else 'none'}</b>")

    def test_two_live_plans_and_scan_keeps_running(self):
        fired = []
        orig = w.fire_debate
        w.fire_debate = lambda now, trades, vid, watch: fired.append(f"{now:%H:%M}") or False
        try:
            other = dict(PLAN, symbol="SBIN", yahoo="SBIN.NS", name="SBI")
            self.rows = [(185, 185.5, 184.8, 185.2)] * 30          # TATASTEEL above its zone
            self.rows_by["SBIN.NS"] = [(183.45, 183.7, 183.3, 183.6)] * 2 + \
                                      [(183.6, 186.0, 183.5, 185.95)] * 28
            self.verdict("2026-11-09_1345", PLAN)
            self.at(13, 46); w.main(self.dir)
            self.assertIn("13:46", fired)  # 1 live → still scans
            self.verdict("2026-11-09_1350", other)
            self.at(13, 52); w.main(self.dir)
            wt = self.get("watch.json")
            self.assertEqual([e["symbol"] for e in wt["live"]], ["TATASTEEL", "SBIN"])
            self.assertIn("Live plans now: <b>2/3</b>", self.texts())
            self.at(13, 56); w.main(self.dir)
            t = self.texts()
            self.assertIn("BUY TRIGGER — SBIN", t)
            self.assertIn("Limit price  <code>183.5</code>", t)     # copy-ready order values
            self.assertIn("SL-M sell", t)
            self.assertNotIn("groww", t.lower().replace("groww squares off", ""))
            self.assertIn("📲 INDmoney › search <code>SBIN</code>", t)
            self.assertIn("When that T1 sell fills: edit the SL-M to qty", t)  # no short after T1
            self.assertNotIn("BUY TRIGGER — TATASTEEL", t)
            self.at(14, 1); w.main(self.dir)
            self.assertIn("TARGET 1 HIT — SBIN", self.texts())
            self.assertIn("Edit SL-M: qty", self.texts())
            # same stock again → blocked, not a second SBIN plan
            self.verdict("2026-11-09_1405", other)
            self.at(14, 6); w.main(self.dir)
            self.assertIn("SBIN already has a live plan", self.texts())
            self.assertEqual(len(w.open_plans(self.get("watch.json"))), 2)
        finally:
            w.fire_debate = orig

    def test_full_at_three_and_migration(self):
        fired = []
        orig = w.fire_debate
        w.fire_debate = lambda now, trades, vid, watch: fired.append(1) or False
        try:
            # old single-plan watch.json (written by the previous watcher version)
            self.put("plan.json", {"date": "2026-11-09", "verdict_id": "2026-11-09_1345", "plan": PLAN})
            self.put("watch.json", {"date": "2026-11-09", "verdict_id": "2026-11-09_1345",
                                    "status": "waiting", "since": "13:45", "qty_fix": 500, "log": []})
            self.rows = [(185, 185.5, 184.8, 185.2)] * 30
            self.at(13, 50); w.main(self.dir)
            wt = self.get("watch.json")
            self.assertEqual(wt["live"][0]["symbol"], "TATASTEEL")
            self.assertEqual(wt["live"][0]["plan"]["qty"], 500)
            self.assertNotIn("status", wt)
            self.assertNotIn("GO —", self.texts(), "carry-over must not re-send the verdict")
            n = len(fired)
            for i, sym in enumerate(["SBIN", "PFC"]):
                self.verdict(f"2026-11-09_14{i}0", dict(PLAN, symbol=sym, yahoo=sym + ".NS"))
                self.at(14, i * 10 + 1); w.main(self.dir)
            self.assertEqual(len(w.open_plans(self.get("watch.json"))), 3)
            k = len(fired)
            self.at(14, 16); w.main(self.dir)
            self.assertEqual(len(fired), k, "3 live → no more scans")
            self.assertGreater(k, n)
        finally:
            w.fire_debate = orig


    def test_sold_frees_slot(self):
        fired = []
        orig = w.fire_debate
        w.fire_debate = lambda now, trades, vid, watch: fired.append(1) or False
        try:
            self.rows = [(185, 185.5, 184.8, 185.2)] * 40
            for i, sym in enumerate(["TATASTEEL", "SBIN", "PFC"]):
                self.verdict(f"2026-11-09_134{i}", dict(PLAN, symbol=sym, yahoo=sym + ".NS"))
                self.at(13, 46 + i); w.main(self.dir)
            self.assertEqual(len(w.open_plans(self.get("watch.json"))), 3)
            k = len(fired)
            # still holding SBIN → slot stays taken
            self.put("mytrades.json", {"date": "2026-11-09", "trades": {"1": [
                {"symbol": "SBIN", "opened": "13:55", "qty": 10, "left": 10}]}})
            self.at(14, 0); w.main(self.dir)
            self.assertEqual(len(fired), k)
            # sold everything → plan ends, scan runs again
            self.put("mytrades.json", {"date": "2026-11-09", "trades": {"1": [
                {"symbol": "SBIN", "opened": "13:55", "qty": 10, "left": 0}]}})
            self.at(14, 5); w.main(self.dir)
            self.assertIn("SLOT FREED — SBIN", self.texts())
            wt = self.get("watch.json")
            self.assertEqual([e["symbol"] for e in w.slots(wt)], ["TATASTEEL", "PFC"])
            self.assertIn("SBIN", [e["symbol"] for e in w.open_plans(wt)], "paper tracking goes on")
            self.assertEqual((wt["trades"], wt["day_trades"]), (0, 1), "sold → not open, still 1 today")
            self.assertGreater(len(fired), k, "0 open + 2 waiting → scan again")
            # Groww positions closed for PFC (no SOLD message) also frees it, once
            self.put("positions.json", {"date": "2026-11-09", "positions": [
                {"symbol": "PFC", "buy_qty": 5, "sell_qty": 5, "net_qty": 0}]})
            self.at(14, 10); w.main(self.dir)
            self.at(14, 15); w.main(self.dir)
            self.assertEqual(self.texts().count("SLOT FREED — PFC"), 1)
        finally:
            w.fire_debate = orig


class TestScore(Base):
    def test_score_shows_today_live(self):
        self.at(13, 0)
        self.put("watch.json", {"date": "2026-11-09", "verdicts": 4, "trades": 1, "day_pnl": -94,
                                "live": [{"symbol": "SBIN", "since": "10:25", "status": "closed",
                                          "paper_result": -94}]})
        self.put("mytrades.json", {"date": "2026-11-09", "trades": {"7": [
            {"symbol": "SBIN", "qty": 10, "left": 0, "entry": 100.0, "realized": 50.0}]}})
        t = "\n".join(mt.score_lines(self.dir, "7"))
        self.assertIn("Today so far · 13:00", t)
        self.assertIn("SBIN 10:25 🏁 closed −₹94", t)
        self.assertIn("Your real P&amp;L today +₹50", t)
        self.assertIn("first one comes at 15:45", t)


class TestRecheck(Base):
    def test_rejected_stock_breakout_is_flagged_once(self):
        self.start = dt.datetime(2026, 11, 9, 11, 0, tzinfo=IST)
        flat = (183.5, 183.6, 183.4, 183.5)
        self.rows_by["SBIN.NS"] = [(183.5, 184.0, 183.4, 183.6)] * 2 + [flat] * 4 + \
                                  [(183.6, 184.7, 183.6, 184.6)] + [flat] * 10      # 11:30 breaks 184 day high
        self.rows_by["PNB.NS"] = [(185.0, 185.9, 184.9, 185.9)] * 17                  # never > flip 186 + 0.2%
        self.rows_by["TINY.NS"] = [(100.0, 100.0, 99.9, 99.95)] * 2 + [(99.95, 100.1, 99.9, 100.1)] * 15  # +0.1% only
        self.put("rejected.json", {"date": "2026-11-09", "symbols": {
            "SBIN": "11:10 fading, no breakout",
            "PNB": "11:10 R:R 1.1; flips on 5-min close >186.0 on >1M vol",
            "TINY": "11:10 flat",
            "OLD": "no time in this reason"}})
        self.at(11, 25)
        self.assertEqual(recheck.update(self.dir, "2026-11-09", CLOCK[0], w.candles, w.load, w.save), [])
        self.at(11, 40)
        self.assertEqual(recheck.update(self.dir, "2026-11-09", CLOCK[0], w.candles, w.load, w.save), ["SBIN"])
        rc = self.get("recheck.json")["symbols"]["SBIN"]
        self.assertEqual((rc["at"], rc["level"], rc["kind"]), ("11:30", 184.0, "day high"))
        # already flagged → not new again; a fresh rejection needs a fresh breakout
        self.at(11, 45)
        self.assertEqual(recheck.update(self.dir, "2026-11-09", CLOCK[0], w.candles, w.load, w.save), [])
        self.put("rejected.json", {"date": "2026-11-09", "symbols": {"SBIN": "11:45 rejected again"}})
        self.at(11, 55)
        recheck.update(self.dir, "2026-11-09", CLOCK[0], w.candles, w.load, w.save)
        self.assertEqual(self.get("recheck.json")["symbols"], {})
        self.assertEqual(recheck.level_for("13:50 x flips on 15-min close >4,157 on vol", [], 0), (4157.0, "flip level"))

    def test_watcher_sends_recheck_card(self):
        self.start = dt.datetime(2026, 11, 9, 11, 0, tzinfo=IST)
        self.rows = [(183.5, 184.0, 183.4, 183.6)] * 2 + [(183.6, 184.7, 183.6, 184.6)] * 10
        self.put("rejected.json", {"date": "2026-11-09", "symbols": {"SBIN": "11:05 fading"}})
        orig = w.fire_debate
        w.fire_debate = lambda *a: False
        try:
            self.at(11, 30); w.main(self.dir)
            self.at(11, 35); w.main(self.dir)
        finally:
            w.fire_debate = orig
        self.assertEqual(self.texts().count("RE-CHECK NEXT SCAN"), 1)
        self.assertIn("SBIN", self.texts())


class TestHealth(Base):
    def test_ready_gap_fire_noverdict(self):
        watch = {}
        out = []
        health.check(dt.datetime(2026, 11, 9, 9, 16, tzinfo=IST), watch, {}, w.NSE_HOLIDAYS, out.append)
        self.assertIn("SYSTEM READY", out[-1])
        health.check(dt.datetime(2026, 11, 9, 9, 50, tzinfo=IST), watch, {}, w.NSE_HOLIDAYS, out.append)
        self.assertIn("WATCHER GAP", out[-1])
        watch["fire_error"] = 401
        watch["fired_at"] = "10:00"
        health.check(dt.datetime(2026, 11, 9, 10, 26, tzinfo=IST), watch, {}, w.NSE_HOLIDAYS, out.append)
        joined = "\n".join(out)
        self.assertIn("SCAN FAILED", joined)
        self.assertIn("NO VERDICT", joined)
        n = len(out)
        health.check(dt.datetime(2026, 11, 9, 10, 30, tzinfo=IST), watch, {}, w.NSE_HOLIDAYS, out.append)
        self.assertEqual(len(out), n, "alerts must not repeat")

    def test_connector_offline_and_back(self):
        flags, out = {}, []
        doc = {"date": "2026-11-09", "connectors": {"groww": False, "indmoney": False}}
        health.connectors(doc, "2026-11-09", flags, out.append)
        health.connectors(doc, "2026-11-09", flags, out.append)
        self.assertEqual(len(out), 1, "warn once, and never for Groww")
        self.assertIn("INDMONEY OFFLINE", out[0])
        self.assertNotIn("Groww", out[0])
        self.assertIn("claude.ai/customize/connectors", out[0])
        doc["connectors"].update(groww=True, indmoney=True)
        health.connectors(doc, "2026-11-09", flags, out.append)
        health.connectors(doc, "2026-11-09", flags, out.append)
        self.assertEqual(len(out), 2)
        self.assertIn("INDmoney back", out[1])
        health.connectors({"date": "2026-11-08", "connectors": {"indmoney": False}}, "2026-11-09", flags, out.append)
        self.assertEqual(len(out), 2, "old files ignored")

    def test_verdict_drops_groww_offline(self):
        txt = ("🟢 <b>GO</b>\n\n⚠️ Groww offline — reconnect\n\n"
               "<i>Groww offline; INDmoney and InvestSights used.</i>\n"
               "⚠️ Groww not connected\n📲 groww.in/stocks/trent-ltd\nOpen in Groww › Buy")
        got = w.drop_groww_offline(txt)
        self.assertNotIn("offline", got)
        self.assertNotIn("not connected", got)
        self.assertIn("Open in Groww", got)
        self.assertNotIn("groww.in", w.check_groww_links(got))
        self.assertNotIn("\n\n\n", got)
        self.assertIn("reconnect", mt.reconnect_hint("Groww connector needs authentication"))
        self.assertEqual(mt.reconnect_hint("order not found"), "")


class TestMyTrade(Base):
    def setUp(self):
        super().setUp()
        self.q = []
        mt.get_updates = lambda off: [u for u in self.q if u["update_id"] >= off]
        mt.fire_sync = lambda: True
        self.start = dt.datetime(2026, 11, 9, 9, 15, tzinfo=IST)
        self.rows = [(183.6 + 0.02 * i, 183.7 + 0.02 * i, 183.5 + 0.02 * i, 183.65 + 0.02 * i) for i in range(80)]
        self.put("plan.json", {"date": "2026-11-09", "verdict_id": "x", "plan": PLAN})

    def say(self, uid, chat, text, h, m):
        ts = int(dt.datetime(2026, 11, 9, h, m, tzinfo=IST).timestamp())
        self.q.append({"update_id": uid, "message": {"chat": {"id": chat}, "text": text, "date": ts}})

    def run_at(self, h, m):
        self.at(h, m)
        mt.main(self.dir)

    def ind(self, rows, h, m):
        os.environ["IND_POSITIONS"] = json.dumps(rows)
        try:
            self.run_at(h, m)
        finally:
            os.environ.pop("IND_POSITIONS", None)

    def test_ind_orders_saved(self):
        os.environ["IND_POSITIONS"] = json.dumps({"p": [], "o": [
            {"s": "SBIN", "q": 10, "p": 800.5}, {"s": "<b>", "q": 1}, {"s": "PFC", "q": 0}]})
        try:
            self.run_at(10, 1)
        finally:
            os.environ.pop("IND_POSITIONS", None)
        self.assertEqual(self.get("positions_ind.json")["orders"], [{"s": "SBIN", "q": 10, "p": 800.5}])
        # order book unreadable this time (null) → keep the last list
        os.environ["IND_POSITIONS"] = json.dumps({"p": [], "o": None})
        try:
            self.run_at(10, 2)
        finally:
            os.environ.pop("IND_POSITIONS", None)
        self.assertEqual(self.get("positions_ind.json")["orders"], [{"s": "SBIN", "q": 10, "p": 800.5}])

    def test_indmoney_auto(self):
        # INDmoney buy shows up → trade starts by itself with the plan's SL/T1 (owner chat)
        self.ind([{"s": "TATASTEEL", "bq": 544, "ba": 183.6, "sq": 0, "sa": 0, "nq": 544, "rp": 0}], 10, 1)
        tr = self.get("mytrades.json")["trades"]["111"][0]
        self.assertEqual((tr["qty"], tr["left"], tr["entry"], tr["broker"]), (544, 544, 183.6, "INDmoney"))
        self.assertIn("MY TRADE STARTED — TATASTEEL", self.texts())
        self.assertIn("Picked up from INDmoney", self.texts())
        n = len(self.sent)
        self.run_at(10, 5)                                   # same data again: no repeat card
        self.assertFalse(any("TATASTEEL" in t and "STARTED" in t for _, t in self.sent[n:]))
        # half sold at T1 → part sold, SL to cost, INDmoney's realised P&L
        self.ind([{"s": "TATASTEEL", "bq": 544, "ba": 183.6, "sq": 272, "sa": 185.0, "nq": 272, "rp": 380.8}], 10, 20)
        tr = self.get("mytrades.json")["trades"]["111"][0]
        self.assertEqual((tr["left"], tr["realized"], tr["sl"]), (272, 380.8, 183.6))
        self.assertIn("PART SOLD — TATASTEEL", self.texts())
        # BOUGHT … IND on the auto trade only sets levels, no duplicate
        self.say(1, 111, "BOUGHT TATASTEEL 272 183.6 SL 183 T1 185 IND", 10, 22)
        self.run_at(10, 25)
        trs = self.get("mytrades.json")["trades"]["111"]
        self.assertEqual(len(trs), 1)
        self.assertIn("LEVELS SET — TATASTEEL", self.texts())
        # rest sold → closed, slot logic sees left 0
        self.ind([{"s": "TATASTEEL", "bq": 544, "ba": 183.6, "sq": 544, "sa": 185.5, "nq": 0, "rp": 1033.6}], 11, 0)
        tr = self.get("mytrades.json")["trades"]["111"][0]
        self.assertEqual((tr["left"], tr["realized"]), (0, 1033.6))
        self.assertIn("TRADE CLOSED — TATASTEEL", self.texts())
        # junk in the input is ignored, Groww trades untouched
        self.ind([{"s": "<b>X</b>", "bq": 1}, "bad", {"s": "SBIN", "bq": "x"}], 11, 5)
        self.assertEqual(len(self.get("mytrades.json")["trades"]["111"]), 1)

    def test_commands(self):
        self.say(1, 111, "BOUGHT TATASTEEL", 10, 1)
        self.say(2, 999, "BOUGHT TATASTEEL", 10, 1)        # stranger: ignored
        self.say(3, 222, "RISK 0.5", 10, 2)
        self.say(4, 222, "BOUGHT TATASTEEL", 10, 3)
        self.say(5, 222, "PAUSE", 10, 3)
        self.say(6, 111, "HELP", 10, 4)
        self.run_at(10, 5)
        st = self.get("mytrades.json")
        self.assertNotIn("999", st["trades"])
        self.assertEqual(st["trades"]["222"][0]["qty"], 272)   # half size
        self.assertEqual(st["trades"]["111"][0]["sync"], "pending")
        self.assertIn("222", st["paused"])
        self.assertTrue(all(to in ("111", "222") for to, _ in self.sent))
        self.assertIn("Bot commands", self.texts())
        # Groww sync replaces the owner's estimate
        self.put("positions.json", {"date": "2026-11-09", "synced_at": "10:07", "positions": [
            {"symbol": "TATASTEEL", "buy_qty": 544, "buy_avg": 183.62, "sell_qty": 0, "sell_avg": 0}]})
        self.run_at(10, 10)
        st = self.get("mytrades.json")
        self.assertEqual(st["trades"]["111"][0]["entry"], 183.62)
        self.assertIn("SYNCED FROM GROWW", self.texts())
        self.say(7, 111, "SOLD TATASTEEL 544 185", 10, 12)
        self.run_at(10, 15)
        st = self.get("mytrades.json")
        self.assertEqual(st["trades"]["111"][0]["left"], 0)
        self.run_at(14, 50)
        self.assertIn("10 MIN TO EXIT", self.texts())
        self.assertNotIn("EXIT NOW", self.texts())
        self.run_at(15, 0)
        self.assertIn("EXIT NOW", self.texts())             # 222 still holds
        self.assertIn("15:00 rule", self.texts())
        self.run_at(15, 46)
        self.assertIn("MY TRADES TODAY", self.texts())
        self.assertIn("2026-11-09", self.get("realtrades.json")["days"])


    def test_too_big_and_not_moving(self):
        self.assertEqual(mt.size_warn(104, 958.5, 953.5, 104), [])
        big = "\n".join(mt.size_warn(728, 957.66, 953.5, 104))
        self.assertIn("Sell <b>488</b> now and keep <b>240</b>", big)
        self.assertIn("7.0×", big)
        # Groww shows far more shares than the plan → TOO BIG alert
        self.say(1, 111, "BOUGHT TATASTEEL", 10, 1)
        self.run_at(10, 5)
        self.put("positions.json", {"date": "2026-11-09", "synced_at": "10:07", "positions": [
            {"symbol": "TATASTEEL", "buy_qty": 4000, "buy_avg": 183.62, "sell_qty": 0, "sell_avg": 0}]})
        self.run_at(10, 10)
        t = self.texts()
        self.assertIn("TOO BIG — TATASTEEL", t)
        self.assertIn("needs more than your ₹1L margin", t)
        # held 75+ min with price barely above the buy → one NOT MOVING nudge
        self.assertNotIn("NOT MOVING", t)
        self.run_at(11, 20)
        self.run_at(11, 25)
        self.assertEqual(self.texts().count("NOT MOVING — TATASTEEL"), 1)

    def test_indmoney_trade(self):
        self.say(1, 111, "BOUGHT TATASTEEL 100 183.5 IND", 10, 1)
        self.run_at(10, 5)
        st = self.get("mytrades.json")
        tr = st["trades"]["111"][0]
        self.assertEqual((tr["broker"], tr["qty"], tr["entry"]), ("INDmoney", 100, 183.5))
        self.assertNotIn("sync", tr)                     # no Groww lookup for INDmoney
        self.assertFalse(st.get("sync_request"))
        self.say(2, 111, "SOLD TATASTEEL", 10, 7)
        self.run_at(10, 10)
        st = self.get("mytrades.json")
        self.assertEqual(st["trades"]["111"][0]["left"], 0)
        self.assertFalse(st.get("sync_request"))
        self.assertIn("On INDmoney", self.texts())

    def test_order_check(self):
        self.start = dt.datetime(2026, 11, 9, 10, 0, tzinfo=IST)
        self.rows = [(183.4, 183.7, 183.3, 183.5)] * 6
        self.say(1, 111, "ORDER gmk39038rdt", 10, 1)
        self.say(2, 222, "ORDER GMK39038RDT", 10, 1)          # not the Groww account holder
        self.run_at(10, 3)
        self.assertEqual(self.get("order_requests.json")["ids"], ["GMK39038RDT"])
        self.assertIn("only for the Groww account holder", self.texts())
        sync = lambda t, state, filled, avg: (
            self.put("positions.json", {"date": "2026-11-09", "synced_at": t, "positions": []}),
            self.put("orders.json", {"date": "2026-11-09", "synced_at": t, "orders": [
                {"id": "GMK39038RDT", "symbol": "TATASTEEL", "side": "BUY", "type": "LIMIT",
                 "qty": 100, "filled_qty": filled, "price": 183.5, "avg_price": avg, "state": state}]}))
        sync("10:05", "open", 0, None)
        self.run_at(10, 8)
        self.assertIn("ORDER OPEN — TATASTEEL", self.texts())
        self.run_at(10, 14)                                    # 10 min later: asks Groww again
        req = self.get("mytrades.json")["orders"]["GMK39038RDT"]["req"]
        self.assertEqual(req, "10:14")
        sync("10:16", "executed", 100, 183.52)
        self.run_at(10, 18)
        t = self.texts()
        self.assertIn("ORDER EXECUTED — TATASTEEL", t)
        self.assertIn("MY TRADE STARTED — TATASTEEL", t)       # tracking starts by itself
        tr = self.get("mytrades.json")["trades"]["111"][0]
        self.assertEqual((tr["qty"], tr["entry"]), (100, 183.52))
        self.assertEqual(self.get("order_requests.json")["ids"], [])

    def test_market_closed(self):
        CLOCK[0] = dt.datetime(2026, 11, 7, 18, 0, tzinfo=IST)   # Saturday
        ts = int(CLOCK[0].timestamp())
        for uid, text in ((1, "BOUGHT TATASTEEL"), (2, "PLAN"), (3, "HELP")):
            self.q.append({"update_id": uid, "message": {"chat": {"id": 111}, "text": text, "date": ts}})
        mt.main(self.dir)
        st = self.get("mytrades.json")
        self.assertFalse(st["trades"].get("111"))
        self.assertIn("Market is closed", self.texts())
        self.assertIn("Bot commands", self.texts())
        self.assertEqual(st["offset"], 4)


class TestLock(unittest.TestCase):
    def test_lock_t1_halfway(self):
        start = dt.datetime(2026, 11, 9, 10, 0, tzinfo=IST)
        bars = bars_from(start, [(100, 102.1, 99.8, 102), (102, 103.2, 101.9, 103),
                                 (103, 103.1, 101.5, 101.8), (101.8, 102, 101, 101.2)])
        p = {"qty": 100, "sl": 99.0, "t1": 102.0, "t2": 104.0, "time_stop": "15:10"}
        pnl_m, steps_m = replay.manage(bars, 0, 100.0, p)
        pnl_l, steps_l = replay.manage(bars, 0, 100.0, p, lock=True)
        self.assertIn("10:05 SL to T1", steps_l)
        self.assertIn("10:10 locked T1", steps_l)
        self.assertEqual(pnl_l, round(50 * 2 + 50 * 2 - replay.COST * 100 * 100))   # rest sold at T1
        self.assertLess(pnl_m, pnl_l)                                         # plain rule rode it back down


class TestStats(unittest.TestCase):
    def test_lesson(self):
        day = {"date": "d", "totals": {"rules": 0, "limit": 770, "market": 3333, "targets": 7594},
               "plans": [{"symbol": "A", "rules": {"pnl": 0, "steps": ["ran up without the dip"]},
                          "limit": {"pnl": 0}, "market": {"pnl": 100},
                          "targets": {"entry_hit": False, "t1_hit": True, "t2_hit": False}}]}
        s = stats.summarize([day, day, day])
        self.assertIn("Lesson: entries are too far", stats.text(s))


class TestExpectancy(unittest.TestCase):
    def setUp(self):
        import expectancy
        self.e = expectancy
        self.dir = tempfile.mkdtemp()

    def plan(self, sym, pnl, bought=True, entry=100.0, sl=99.0, qty=100):
        return {"symbol": sym, "entry": entry, "sl": sl, "qty": qty,
                "rules": {"pnl": pnl, "bought": bought, "steps": ["x"]},
                "market": {"pnl": pnl, "steps": ["bought"]}}

    def test_metrics_and_verdicts(self):
        tr = [{"pnl": 150, "risk": 100}, {"pnl": -100, "risk": 100}] * 15
        m = self.e.metrics(tr)
        self.assertEqual((m["n"], m["win_rate"], m["exp"], m["exp_r"], m["pf"]), (30, 50, 25, 0.25, 1.5))
        self.assertEqual(m["streak"], 1)
        self.assertEqual(self.e.verdict(m)[0], "scale")
        self.assertEqual(self.e.verdict(self.e.metrics(tr[:10]))[0], "collecting")
        self.assertEqual(self.e.verdict(self.e.metrics([{"pnl": -50, "risk": 100}] * 30))[0], "fix")
        self.assertEqual(self.e.verdict(self.e.metrics([{"pnl": 120, "risk": 100}, {"pnl": -100, "risk": 100}] * 15))[0], "hold")

    def test_paper_vs_real(self):
        hist = [{"date": "2026-11-09", "plans": [self.plan("SBIN", 300), self.plan("TCS", 200),
                                                 self.plan("INFY", 0, bought=False)],
                 "totals": {"rules": 500, "limit": 0, "market": 500, "lock": 0, "targets": 0}}]
        real = {"2026-11-09": {"7": {"pnl": 50, "trades": 2, "list": [
            {"symbol": "SBIN", "qty": 100, "entry": 100.0, "pnl": 100, "open": 0},
            {"symbol": "ITC", "qty": 10, "entry": 400.0, "pnl": -50, "open": 0}]}}}
        with open(os.path.join(self.dir, "scorecard.json"), "w") as f:
            json.dump(hist, f)
        with open(os.path.join(self.dir, "realtrades.json"), "w") as f:
            json.dump({"days": real}, f)
        paper = self.e.paper_trades(hist)
        self.assertEqual([p["symbol"] for p in paper], ["SBIN", "TCS"])
        c = self.e.compare(paper, self.e.real_trades(real, hist, "7"))
        self.assertEqual(c["gap"], 92 - 300)          # 100 − 0.08% costs (8) vs 300
        self.assertEqual([p["symbol"] for p in c["skipped"]], ["TCS"])
        self.assertEqual([t["symbol"] for t in c["extra"]], ["ITC"])
        t = "\n".join(self.e.lines(self.dir, chat="7"))
        self.assertIn("Plans you didn't take (1): paper +₹200", t)
        self.assertIn("Trades that weren't a plan (1)", t)
        self.assertIn("Verdict (your trades)", t)
        self.assertIn("Expectancy", "\n".join(mt.score_lines(self.dir, "7")))


class TestGuard(Base):
    def setUp(self):
        super().setUp()
        self._vix = w.vix_change
        w.vix_change = lambda: None

    def tearDown(self):
        w.vix_change = self._vix
        super().tearDown()

    def watch(self):
        return {"live": [{"symbol": "SBIN", "status": "waiting",
                          "plan": {"symbol": "SBIN", "qty": 450, "entry": 100.0, "sl": 98.0}}]}

    def test_sector_cap(self):
        r = w.guard(self.watch(), {"symbol": "KOTAKBANK", "qty": 100, "entry": 100.0, "sl": 99.0},
                    self.dir, "2026-11-09", 2.0)
        self.assertTrue(any("Banks already open (SBIN)" in x for x in r), r)
        p = {"symbol": "TCS", "qty": 450, "entry": 100.0, "sl": 98.0}
        self.assertEqual(w.guard(self.watch(), p, self.dir, "2026-11-09", 2.0), [])
        self.assertEqual(p["rules_v"], w.RULES_VERSION)

    def test_heat_cap(self):
        r = w.guard(self.watch(), {"symbol": "TCS", "qty": 600, "entry": 100.0, "sl": 98.0},
                    self.dir, "2026-11-09", 2.0)
        self.assertTrue(any("total risk to SL would be ₹2,100" in x for x in r), r)

    def test_event_day_and_vix(self):
        self.put("premarket.json", {"date": "2026-11-09", "events": ["RBI policy 10:00"]})
        p = {"symbol": "TCS", "qty": 100, "entry": 100.0, "sl": 99.0}
        self.assertTrue(any("event day (RBI policy" in x for x in w.guard({}, dict(p), self.dir, "2026-11-09", 1.8)))
        self.assertEqual(w.guard({}, dict(p), self.dir, "2026-11-09", 2.1), [])
        w.vix_change = lambda: 12.5
        self.assertTrue(any("VIX up 12.5%" in x for x in w.guard({}, dict(p), self.dir, "2026-11-09", 2.1)))


class TestSlNudge(Base):
    def test_nudge_once(self):
        ind = {"date": "2026-11-09", "sells": []}
        mtd = {"trades": {"7": [{"symbol": "SBIN", "broker": "INDmoney", "left": 10, "sl": 98.0, "opened": "10:00"}]}}
        wt = {}
        self.assertFalse(w.sl_nudge(ind, mtd, "2026-11-09", w.hhmm("10:02"), wt))
        self.assertTrue(w.sl_nudge(ind, mtd, "2026-11-09", w.hhmm("10:04"), wt))
        self.assertIn("NO SL ORDER", self.texts())
        self.assertEqual(self.sent[0][0], "7")
        self.assertFalse(w.sl_nudge(ind, mtd, "2026-11-09", w.hhmm("10:09"), wt))
        self.assertFalse(w.sl_nudge({"date": "2026-11-09", "sells": ["SBIN"]}, mtd, "2026-11-09", 610, {}))
        self.assertFalse(w.sl_nudge({"date": "2026-11-09", "sells": None}, mtd, "2026-11-09", 610, {}))


class TestTrail(unittest.TestCase):
    def test_trail_lets_winner_run(self):
        b = bars_from(dt.datetime(2026, 11, 9, 10, 0, tzinfo=IST),
                      [(100, 102.1, 99.8, 102), (102, 104.5, 101.9, 104.4),
                       (104.4, 106, 104, 105.8), (105.8, 106, 105, 105.5)])
        p = {"qty": 100, "sl": 99.0, "t1": 102.0, "t2": 104.0, "time_stop": "15:10", "atr5": 1.0}
        plain, _ = replay.manage(b, 0, 100.0, p)
        trail, steps = replay.manage(b, 0, 100.0, p, trail=True)
        self.assertEqual(plain, round(300 - replay.COST * 100 * 100))
        self.assertEqual(trail, round(375 - replay.COST * 100 * 100))
        self.assertEqual(steps[-1], "close 105.50")


class TestCandleSource(unittest.TestCase):
    def setUp(self):
        self.saved = (w.ind_candles, w.yahoo_candles, w.urllib.request.urlopen)
        os.environ.update(WORKER_URL="https://w.example", WORKER_KEY="k")

    def tearDown(self):
        w.ind_candles, w.yahoo_candles, w.urllib.request.urlopen = self.saved
        for k in ("WORKER_URL", "WORKER_KEY"):
            os.environ.pop(k, None)

    def test_indmoney_then_yahoo_fallback(self):
        calls = []
        w.yahoo_candles = lambda s, d="1d": calls.append(("y", s)) or ([], None)
        w.ind_candles = lambda s, d="1d": calls.append(("i", s)) or ([], None)
        REAL_CANDLES("SBIN.NS")
        REAL_CANDLES("SBIN.NS", "1d", False)     # scanner → Yahoo
        REAL_CANDLES("^INDIAVIX", "5d")          # index → Yahoo
        self.assertEqual(calls, [("i", "SBIN"), ("y", "SBIN.NS"), ("y", "^INDIAVIX")])

        def boom(s, d="1d"):
            raise RuntimeError("down")
        calls.clear()
        w.ind_candles = boom
        REAL_CANDLES("SBIN.NS")
        self.assertEqual(calls, [("y", "SBIN.NS")])

    def test_worker_parse(self):
        import io
        now = int(w.datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0).timestamp())
        body = json.dumps({"ok": True, "candles": {"SBIN": [[now - 86400, 1, 2, 0.5, 1.5, 9],
                                                            [now, 2, 3, 1, 2.5, 7]]}}).encode()
        seen = []

        def fake_open(req, timeout=0):
            seen.append(req)
            return io.BytesIO(body)
        w.urllib.request.urlopen = fake_open
        bars, ltp = self.saved[0]("SBIN", "1d")
        self.assertEqual(len(bars), 1)           # only today's candles for "1d"
        self.assertEqual(ltp, 2.5)
        self.assertEqual(seen[0].get_header("X-worker-key"), "k")
        bars, _ = self.saved[0]("SBIN", "5d")
        self.assertEqual(len(bars), 2)


class TestWalkForwardSlip(unittest.TestCase):
    def test_walk_forward(self):
        import expectancy
        tr = [{"pnl": 100, "risk": 100}] * 10 + [{"pnl": -50, "risk": 100}] * 10
        self.assertIn("edge fading", expectancy.walk_forward(tr))
        self.assertIsNone(expectancy.walk_forward(tr[:10]))

    def test_slip(self):
        s = mt.slip({"entry": 100.0, "start": "10:00", "buy_at": "10:05"}, 100.5, 100, "10:12")
        self.assertEqual(s, {"per_share": 0.5, "rs": 50, "delay": 7, "plan_at": "10:05"})
        self.assertIn("paid ₹0.5/share above plan", mt.slip_line(s))

if __name__ == "__main__":
    unittest.main()
