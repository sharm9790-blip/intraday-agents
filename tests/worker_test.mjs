// Offline test of the worker's pure logic. Run on macOS without Node:
//   /System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc -m tests/worker_test.mjs
import { decide, watchList, istNow, base32, indCodes, indParse, posSnapshot, openBuys,
  openSells, deadMan, candleParse, quoteParse, chainTrim, expiryGuess } from "../cloudflare/worker.js";

let fails = 0;
const ok = (c, msg) => { if (!c) { fails++; print("FAIL " + msg); } };

// IST clock: 2026-11-09 03:45 UTC = 09:15 IST, Monday
const t = istNow(Date.UTC(2026, 10, 9, 3, 45));
ok(t.date === "2026-11-09" && t.hm === "09:15" && t.mins === 555 && t.weekday === 1, "istNow " + JSON.stringify(t));
ok(istNow(Date.UTC(2026, 10, 9, 18, 40)).date === "2026-11-10", "istNow rolls to next IST day");

// base32: RFC 4648 example "JBSWY3DPEHPK3PXP" = "Hello!" + DE AD BE EF
const b = base32("JBSWY3DPEHPK3PXP");
ok(String.fromCharCode(...b.slice(0, 6)) === "Hello!" && b[6] === 0xde && b[9] === 0xef, "base32");

const plan = { zone_low: 142.45, zone_high: 142.55, max_entry: 142.55, sl: 141.95, t1: 143.8, t2: 144.4,
               cancel_if_high_reaches: 143.8, cancel_if_close_below: 142.2 };
const watch = { date: "2026-11-09", live: [
  { symbol: "BANKINDIA", vid: "v1", status: "waiting", plan },
  { symbol: "SBIN", vid: "v2", status: "waiting", plan, user_sold: "13:10" },   // sold → skip
  { symbol: "PFC", vid: "v3", status: "cancelled", plan },                       // closed → skip
] };
const mt = { date: "2026-11-09", paused: ["6338805963"], trades: { "1322737291": [
  { symbol: "IRFC", qty: 1223, left: 1223, entry: 82.09, sl: 81.2, t1: 83.2, t2: 84.6, opened: "11:48", flags: {}, realized: 0 },
  { symbol: "BANKBARODA", qty: 396, left: 0, entry: 252.86, sl: 252.86, t1: 254.6, t2: 256.5, opened: "10:36", flags: {} },
] } };

const list = watchList(watch, mt, "2026-11-09");
ok(JSON.stringify(list.symbols) === '["BANKINDIA","IRFC"]', "watch list " + JSON.stringify(list.symbols));
ok(watchList(watch, mt, "2026-11-10").symbols.length === 0, "other day → nothing");

// price above the zone: nothing yet
let sent = {};
ok(decide(list, { BANKINDIA: 143.1, IRFC: 82.5 }, sent, "14:21", mt).length === 0, "no alert between levels");

// BANKINDIA dips into the zone, IRFC hits T1
let a = decide(list, { BANKINDIA: 142.5, IRFC: 83.25 }, sent, "14:22", mt);
ok(a.length === 2, "two alerts, got " + a.length);
const zone = a.find((x) => x.key === "plan:v1:zone");
ok(zone && zone.text.includes("IN BUY ZONE — BANKINDIA"), "zone alert");
ok(zone && zone.to.length === 1 && zone.to[0] === "1322737291", "paused chat gets no plan alert");
const t1 = a.find((x) => x.key.endsWith(":t1"));
ok(t1 && t1.text.includes("Sell <b>611</b>") && t1.to[0] === "1322737291", "T1 alert to owner, half qty");
ok(t1 && t1.text.includes("+₹1,419"), "T1 P&L " + (t1 && t1.text));
ok(t1 && t1.text.includes("Edit SL-M: qty <b>612</b>"), "T1 tells you to cut SL-M qty");

// same prices again → no repeats
ok(decide(list, { BANKINDIA: 142.5, IRFC: 83.25 }, sent, "14:23", mt).length === 0, "no repeat");

// runs up to cancel level → one RAN UP alert; IRFC falls to SL
a = decide(list, { BANKINDIA: 143.9, IRFC: 81.1 }, sent, "14:24", mt);
ok(a.some((x) => x.key === "plan:v1:runup" && x.text.includes("Don't chase")), "run-up alert");
ok(a.some((x) => x.key.includes(":sl81.2") && x.text.includes("SL LEVEL HIT — IRFC")), "SL alert");

// T2 only after T1 is done
const t2list = watchList({}, { date: "2026-11-09", trades: { "1": [
  { symbol: "X", qty: 10, left: 5, entry: 100, sl: 100, t1: 102, t2: 104, opened: "10:00", flags: { t1: true }, realized: 10 },
] } }, "2026-11-09");
a = decide(t2list, { X: 104.2 }, {}, "11:00", {});
ok(a.length === 1 && a[0].text.includes("T2 REACHED — X") && a[0].text.includes("+₹31"), "T2 alert " + (a[0] && a[0].text));

// HTML escaping of symbols
const e = watchList({ date: "d", live: [{ symbol: "A&B", vid: "z", status: "waiting", plan }] }, {}, "d");
a = decide(e, { "A&B": 142.5 }, {}, "10:00", {});
ok(a[0].text.includes("A&amp;B") && !a[0].text.includes("A&B"), "escape");

// INDmoney: symbols → NSE_<id>, response back to symbols; unknown symbol skipped
const ic = indCodes(["SBIN", "M&M", "NEWCO"], { SBIN: 3045, "M&M": 2031 });
ok(JSON.stringify(Object.keys(ic.code)) === '["NSE_3045","NSE_2031"]' && ic.missing[0] === "NEWCO", "indCodes");
ok(indCodes(["NIFTYFUT"], { NIFTYFUT: "NFO_58875" }).code.NFO_58875 === "NIFTYFUT", "indCodes futures pass-through");
const ip = indParse({ status: "success", data: { NSE_3045: { live_price: 792.5 }, NSE_2031: { live_price: 3100 }, NSE_9: { live_price: 1 } } }, ic.code);
ok(ip.SBIN === 792.5 && ip["M&M"] === 3100 && Object.keys(ip).length === 2, "indParse " + JSON.stringify(ip));

// INDmoney positions → stable snapshot; bad symbols and empty rows dropped
const snap = posSnapshot([
  { symbol: "SBIN", buy_qty: 100, buy_avg: 800.5, sell_qty: 50, sell_avg: 805, net_qty: 50, realized_profit: 225 },
  { symbol: "bankindia-EQ", buy_qty: "701", buy_avg: "142.5", sell_qty: 0, net_qty: 701 },
  { symbol: "<b>X</b>", buy_qty: 1 }, { symbol: "IRFC", buy_qty: 0, sell_qty: 0 }]);
ok(snap.length === 2 && snap[0].s === "BANKINDIA" && snap[0].bq === 701 && snap[1].rp === 225, "posSnapshot " + JSON.stringify(snap));

// INDmoney order book → open BUY orders by symbol (filled / cancelled / sell / F&O dropped)
const ob = openBuys([
  { txn_type: "BUY", segment: "EQUITY", status: "O-PENDING", security_id: "3045", requested_qty: 10, traded_qty: 0, requested_price: "800.5" },
  { txn_type: "BUY", segment: "EQUITY", status: "PARTIALLY FILLED", security_id: "2031", requested_qty: 10, traded_qty: 4, requested_price: "3100" },
  { txn_type: "BUY", segment: "EQUITY", status: "SUCCESS", security_id: "2885", requested_qty: 5, traded_qty: 5 },
  { txn_type: "BUY", segment: "EQUITY", status: "CANCELLED", security_id: "2885", requested_qty: 5, traded_qty: 0 },
  { txn_type: "SELL", segment: "EQUITY", status: "O-PENDING", security_id: "3045", requested_qty: 10, traded_qty: 0 },
  { txn_type: "BUY", segment: "DERIVATIVE", status: "O-PENDING", security_id: "3045", requested_qty: 75, traded_qty: 0 },
  { txn_type: "BUY", segment: "EQUITY", status: "O-PENDING", security_id: "999999", requested_qty: 1, traded_qty: 0 }],
  { SBIN: 3045, "M&M": 2031, RELIANCE: 2885 });
ok(JSON.stringify(ob) === '[{"s":"M&M","q":6,"p":3100},{"s":"SBIN","q":10,"p":800.5}]', "openBuys " + JSON.stringify(ob));

// open SELL orders (SL pending) per symbol
const sells = openSells([
  { txn_type: "SELL", segment: "EQUITY", status: "SL-PENDING", security_id: 1, requested_qty: 10, traded_qty: 0 },
  { txn_type: "SELL", segment: "EQUITY", status: "COMPLETE", security_id: 2, requested_qty: 5, traded_qty: 5 },
  { txn_type: "BUY", segment: "EQUITY", status: "PENDING", security_id: 2, requested_qty: 5, traded_qty: 0 },
], { SBIN: 1, TCS: 2 });
ok(JSON.stringify(sells) === '[{"s":"SBIN","q":10}]', "openSells " + JSON.stringify(sells));

// dead-man: 11:00 IST Monday, last run 10:40 → alert; 10:50 → fine; holiday → none
const t11 = istNow(Date.UTC(2026, 10, 9, 5, 30));
ok(deadMan({ date: "2026-11-09", last_run: "10:40" }, t11).key === "dead:2026-11-09:10:40", "dead-man gap");
ok(deadMan({ date: "2026-11-09", last_run: "10:50" }, t11) === null, "dead-man ok");
ok(deadMan({ date: "2026-11-09", holiday: true }, t11) === null, "dead-man holiday");
ok(deadMan({ date: "2026-11-06", last_run: "15:25" }, t11).key.endsWith(":none"), "dead-man no run today");
ok(deadMan({}, istNow(Date.UTC(2026, 10, 9, 3, 50))) === null, "dead-man before 09:25");

// candles: seconds and ms timestamps, sorted, unknown codes skipped
const cp = candleParse({ success: true, data: { NSE_1: { candles: [
  { ts: 1762660800000, o: 2, h: 3, l: 1, c: 2.5, v: 9 }, { ts: 1762660500, o: 1, h: 2, l: 1, c: 2, v: 5 }] },
  NSE_9: { candles: [{ ts: 1, o: 1, h: 1, l: 1, c: 1, v: 1 }] } } }, { NSE_1: "SBIN" });
ok(JSON.stringify(Object.keys(cp)) === '["SBIN"]' && cp.SBIN[0][0] === 1762660500 && cp.SBIN[1][0] === 1762660800,
   "candleParse " + JSON.stringify(cp));


// full quotes, chain trim, expiry guess
const qp = quoteParse({ data: { NSE_1: { live_price: 100, day_open: 99, day_high: 101, day_low: 98, prev_close: 97,
  volume: 5, upper_circuit: 116, lower_circuit: 78, "52week_high": 120, "52week_low": 60 } } }, { NSE_1: "SBIN" });
ok(qp.SBIN.ltp === 100 && qp.SBIN.upper_circuit === 116 && qp.SBIN.high_52w === 120, "quoteParse " + JSON.stringify(qp));
const ct = chainTrim({ data: { underlying_ltp: 100, strikes: { "100": { ce: { oi: 5, previous_oi: 3, iv: 20, greeks: {} }, pe: null } } } });
ok(ct.strikes["100"].ce.oi === 5 && ct.strikes["100"].pe === null && !("greeks" in ct.strikes["100"].ce), "chainTrim");
ok(JSON.stringify(expiryGuess("2026-11-16")) === '["2026-11-24","2026-11-26","2026-12-29"]', "expiryGuess " + JSON.stringify(expiryGuess("2026-11-16")));

const ca = chainTrim({ data: { strikes: [{ strike_price: 1080, ce: { oi: 9 }, pe: { oi: 4 } }] } });
ok(ca.strikes["1080"].ce.oi === 9 && ca.strikes["1080"].pe.oi === 4, "chainTrim array " + JSON.stringify(ca));

print(fails ? `${fails} FAILED` : "worker tests OK");
