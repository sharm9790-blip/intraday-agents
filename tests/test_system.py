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
import replay  # noqa: E402
import risk  # noqa: E402
import stats  # noqa: E402
import watch as w  # noqa: E402

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

        def fake_candles(sym):
            now = CLOCK[0]
            b = [x for x in bars_from(self.start, self.rows_by.get(sym, self.rows)) if x[0] <= now]
            return b, (b[-1][4] if b else None)
        w.candles = fake_candles

    def tearDown(self):
        w.telegram = self._tg
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
        self.assertGreater(pnl, 1500)


class TestFire(unittest.TestCase):
    def test_window_and_dedupe(self):
        st = {}
        self.assertFalse(w.fire_debate(dt.datetime(2026, 11, 9, 9, 40, tzinfo=IST), 0, None, st))
        self.assertTrue(w.fire_debate(dt.datetime(2026, 11, 9, 9, 46, tzinfo=IST), 0, None, st))
        self.assertFalse(w.fire_debate(dt.datetime(2026, 11, 9, 9, 51, tzinfo=IST), 0, None, st))
        self.assertFalse(w.fire_debate(dt.datetime(2026, 11, 9, 14, 36, tzinfo=IST), 0, None, {}))
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
        self.assertEqual(self.get("watch.json")["trades"], 1)

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

    def test_scorecard_and_paper(self):
        self.go()
        self.rows = [(183.45, 183.7, 183.3, 183.6), (183.6, 186.0, 183.5, 185.9)] + \
                    [(185.9, 186.2, 185.5, 186)] * 20
        self.at(13, 46)
        w.main(self.dir)
        self.at(15, 46)
        w.main(self.dir)
        self.assertIn("SCORECARD", self.texts())
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
            self.assertNotIn("BUY TRIGGER — TATASTEEL", t)
            self.at(14, 1); w.main(self.dir)
            self.assertIn("TARGET 1 HIT — SBIN", self.texts())
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
            self.assertGreater(len(fired), k)
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
        doc = {"date": "2026-11-09", "connectors": {"groww": False, "indmoney": True}}
        health.connectors(doc, "2026-11-09", flags, out.append)
        health.connectors(doc, "2026-11-09", flags, out.append)
        self.assertEqual(len(out), 1, "warn once")
        self.assertIn("GROWW OFFLINE", out[0])
        self.assertIn("claude.ai/customize/connectors", out[0])
        doc["connectors"]["groww"] = True
        health.connectors(doc, "2026-11-09", flags, out.append)
        health.connectors(doc, "2026-11-09", flags, out.append)
        self.assertEqual(len(out), 2)
        self.assertIn("Groww back", out[1])
        health.connectors({"date": "2026-11-08", "connectors": {"groww": False}}, "2026-11-09", flags, out.append)
        self.assertEqual(len(out), 2, "old files ignored")
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
        self.assertEqual(pnl_l, round(50 * 2 + 50 * 2 - 0.0006 * 100 * 100))   # rest sold at T1
        self.assertLess(pnl_m, pnl_l)                                         # plain rule rode it back down


class TestStats(unittest.TestCase):
    def test_lesson(self):
        day = {"date": "d", "totals": {"rules": 0, "limit": 770, "market": 3333, "targets": 7594},
               "plans": [{"symbol": "A", "rules": {"pnl": 0, "steps": ["ran up without the dip"]},
                          "limit": {"pnl": 0}, "market": {"pnl": 100},
                          "targets": {"entry_hit": False, "t1_hit": True, "t2_hit": False}}]}
        s = stats.summarize([day, day, day])
        self.assertIn("Lesson: entries are too far", stats.text(s))


if __name__ == "__main__":
    unittest.main()
