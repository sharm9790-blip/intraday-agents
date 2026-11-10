// Cloudflare Worker (free plan), runs every minute (Cron Trigger "* * * * *").
//
// 1) Instant bot replies at any hour: asks Telegram whether anyone messaged the
//    bot. If an allowed chat did, it shows "typing…" and starts the GitHub
//    workflow mytrade.yml, which reads the message and replies (~30–60 s).
//    It never confirms updates (no `offset`), so mytrade.py still receives every
//    message exactly as before. KV key "last" remembers what was already handed
//    over, so each message starts the workflow once (retried if it was not
//    answered after 10 min, at most 3 times).
//
// 2) ⚡ Live price alerts (Mon–Fri 09:15–15:30 IST): reads today's live plans
//    (state/watch.json) and open trades (state/mytrades.json) from the state
//    branch, gets the last traded price for those stocks in ONE call to INDmoney's
//    free /market/quotes/ltp, and sends an alert the first time a price reaches a level (buy
//    zone, cancel level, SL, T1, T2). The 5-min watcher still applies the plan
//    rules (candle closes) and keeps the scorecard.
//    READ ONLY: the only broker calls are the login (access token) and the LTP
//    endpoint. Nothing here places, changes or cancels orders.
//
// Settings in the Cloudflare dashboard (Worker → Settings → Variables and Secrets):
//   Secret  TELEGRAM_BOT_TOKEN   the bot token
//   Secret  GITHUB_TOKEN         fine-grained PAT, repo intraday-agents only,
//                                Actions: Read and write, Contents: Read-only
//   KV binding  STATE            any KV namespace
//   Cron trigger  * * * * *
//   INDmoney (INDstocks) login — free live prices (all Secrets):
//     IND_CLIENT_ID + IND_MPIN + IND_TOTP_SECRET   (TOTP login, code made here; token 24 h)
//     IND_ACCESS_TOKEN                             (pasted token, valid 24 h)
//   INDmoney needs NSE security ids: the watcher writes state/nse_ids.json weekly.
//   Without INDmoney secrets, part 2 is simply off. (Groww's live-data API needs its
//   paid plan, so it was removed on 2026-11-10; Groww is still where you place orders.)

const ALLOWED = ["1322737291", "6338805963"]; // chat ids that may give commands
const REPO = "sharm9790-blip/intraday-agents";
const STATE_BRANCH = "claude/intraday-state";
const RETRY_AFTER_S = 600;
const MAX_TRIES = 3;
const IND = "https://api.indstocks.com";
const VERSION = "2026-11-10d";  // shown by ?live=test so you can tell the new code is deployed
const OPEN = ["waiting", "in_zone", "t1_hit"];
const IST_MS = 5.5 * 3600 * 1000;

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(Promise.all([check(env), live(env).catch((e) => ({ ok: false, error: String(e) }))]));
  },
  // Opening the worker's URL runs one check — handy for testing.
  // Add ?live=1 to run the live-price check even outside market hours.
  // Add ?live=test to check the INDmoney login and one price call, any time (read-only).
  async fetch(request, env) {
    const mode = new URL(request.url).searchParams.get("live");
    if (mode === "test") {
      const out = {};
      if (indConfigured(env)) out.indmoney = await indTest(env).catch((e) => ({ ok: false, error: String(e) }));
      if (!Object.keys(out).length) out.error = "no INDmoney secrets in Cloudflare";
      out.worker_version = VERSION;
      out.indmoney_secrets = { IND_CLIENT_ID: !!env.IND_CLIENT_ID, IND_MPIN: !!env.IND_MPIN,
                               IND_TOTP_SECRET: !!env.IND_TOTP_SECRET, IND_ACCESS_TOKEN: !!env.IND_ACCESS_TOKEN };
      return new Response(JSON.stringify(out, null, 2), {
        headers: { "content-type": "application/json" },
      });
    }
    const force = mode === "1";
    const [bot, prices] = await Promise.all([
      check(env), live(env, force).catch((e) => ({ ok: false, error: String(e) }))]);
    return new Response(JSON.stringify({ bot, live: prices }, null, 2), {
      headers: { "content-type": "application/json" },
    });
  },
};

// ---------- 1) bot replies ----------

async function check(env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.GITHUB_TOKEN || !env.STATE) {
    return { ok: false, error: "missing TELEGRAM_BOT_TOKEN, GITHUB_TOKEN or STATE binding" };
  }
  const tg = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}`;
  const res = await fetch(`${tg}/getUpdates?timeout=0&allowed_updates=` +
    encodeURIComponent('["message"]'));
  const body = await res.json().catch(() => ({}));
  if (!body.ok) return { ok: false, telegram: res.status, error: body.description };

  const mine = body.result.filter((u) =>
    u.message && u.message.text && ALLOWED.includes(String(u.message.chat.id)));
  if (!mine.length) return { ok: true, waiting: 0 };

  const st = JSON.parse((await env.STATE.get("last")) || "{}");
  const newest = Math.max(...mine.map((u) => u.update_id));
  const now = Math.floor(Date.now() / 1000);
  const fresh = newest > (st.id || 0);
  const stuck = !fresh && now - (st.at || 0) > RETRY_AFTER_S && (st.tries || 0) < MAX_TRIES;
  if (!fresh && !stuck) return { ok: true, waiting: mine.length, handed_over: true };

  const chats = [...new Set(mine.map((u) => u.message.chat.id))];
  await Promise.all(chats.map((c) =>
    fetch(`${tg}/sendChatAction`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: c, action: "typing" }),
    })));

  const gh = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/mytrade.yml/dispatches`, {
      method: "POST",
      headers: ghHeaders(env, "application/vnd.github+json"),
      body: JSON.stringify({ ref: "main" }),
    });
  if (gh.status !== 204) {
    return { ok: false, github: gh.status, error: (await gh.text()).slice(0, 200) };
  }
  await env.STATE.put("last", JSON.stringify({
    id: newest, at: now, tries: fresh ? 1 : (st.tries || 0) + 1,
  }));
  return { ok: true, waiting: mine.length, started_reply: true };
}

function ghHeaders(env, accept) {
  return {
    authorization: `Bearer ${env.GITHUB_TOKEN}`,
    accept,
    "x-github-api-version": "2022-11-28",
    "user-agent": "intraday-bot-worker",
    "content-type": "application/json",
  };
}

// ---------- 2) live price alerts ----------

export function istNow(ms = Date.now()) {
  const d = new Date(ms + IST_MS);
  const pad = (n) => String(n).padStart(2, "0");
  return {
    date: `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`,
    hm: `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`,
    mins: d.getUTCHours() * 60 + d.getUTCMinutes(),
    weekday: d.getUTCDay(), // 0 Sun … 6 Sat
  };
}

const rs = (x) => "₹" + Number(x).toLocaleString("en-IN", { maximumFractionDigits: 2 });
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// What to watch today: live plans (everyone) and open real trades (their owner).
export function watchList(watch, mt, today) {
  const plans = (watch && watch.date === today ? watch.live || [] : [])
    .filter((e) => OPEN.includes(e.status) && !e.user_sold && e.plan);
  const trades = [];
  if (mt && mt.date === today) {
    for (const [chat, ts] of Object.entries(mt.trades || {})) {
      for (const t of ts) if (t.left > 0) trades.push({ chat, t });
    }
  }
  const symbols = [...new Set([...plans.map((e) => e.symbol), ...trades.map((x) => x.t.symbol)])];
  return { plans, trades, symbols };
}

// Pure decision: which alerts to send now. `sent` = keys already sent today.
// Returns [{key, to: [chat ids], text}].
export function decide(list, prices, sent, hm, mt) {
  const out = [];
  const paused = new Set((mt && mt.paused) || []);
  const everyone = ALLOWED.filter((c) => !paused.has(c));
  const add = (key, to, icon, title, lines) => {
    if (sent[key] || !to.length) return;
    out.push({ key, to, text: [`⚡ <b>${title}</b>`, `🕐 ${hm} IST · live price`, "", ...lines].join("\n") });
    sent[key] = 1;
  };
  for (const e of list.plans) {
    const p = e.plan, px = prices[e.symbol];
    if (px == null) continue;
    const s = esc(e.symbol), k = `plan:${e.vid}:`;
    const now = `💹 Now <b>${rs(px)}</b>`;
    if (e.status === "waiting") {
      if (px >= p.cancel_if_high_reaches) {
        add(k + "runup", everyone, "⚡", `RAN UP — ${s}`, [now,
          `Reached the cancel level ${rs(p.cancel_if_high_reaches)} without the dip.`, "",
          "👉 <b>Don't chase.</b> If your limit order hasn't filled, cancel it."]);
      } else if (px <= p.zone_high && px >= p.cancel_if_close_below) {
        add(k + "zone", everyone, "⚡", `IN BUY ZONE — ${s}`, [now,
          `📥 Zone ${rs(p.zone_low)}–${rs(p.zone_high)} · max ${rs(p.max_entry)}`,
          `🛑 SL ${rs(p.sl)} · 🎯 T1 ${rs(p.t1)}`, "",
          "👉 A limit order at the entry can fill now.",
          "The 5-min watcher confirms with 🔔 BUY TRIGGER on a green close."]);
      } else if (px < p.cancel_if_close_below) {
        add(k + "below", everyone, "⚡", `BELOW CANCEL LEVEL — ${s}`, [now,
          `Under ${rs(p.cancel_if_close_below)}. If a 5-min candle closes here the plan is cancelled.`, "",
          "👉 Don't buy unless it recovers."]);
      }
    }
  }
  for (const { chat, t } of list.trades) {
    const px = prices[t.symbol];
    if (px == null) continue;
    const s = esc(t.symbol), k = `trade:${chat}:${t.symbol}:${t.opened}:`;
    const f = t.flags || {};
    const pnl = t.left * (px - t.entry) + (t.realized || 0);
    const now = `💹 Now <b>${rs(px)}</b> · P&amp;L <b>${pnl >= 0 ? "+" : "−"}₹${Math.abs(Math.round(pnl)).toLocaleString("en-IN")}</b>`;
    if (t.sl && px <= t.sl) {
      const cost = t.sl >= t.entry;
      add(k + "sl" + t.sl, [chat], "⚡", `${cost ? "BACK AT COST" : "SL LEVEL HIT"} — ${s}`, [now,
        `Price is at/below your SL ${rs(t.sl)}.`, "",
        `👉 Check your SL-M filled; if not, sell your ${t.left} now.`,
        `Then send <code>SOLD ${s}</code>`]);
    } else if (t.t2 && f.t1 && px >= t.t2) {
      add(k + "t2", [chat], "⚡", `T2 REACHED — ${s}`, [now, `T2 ${rs(t.t2)} reached.`, "",
        `👉 Sell the remaining <b>${t.left}</b>`, `Then send <code>SOLD ${s}</code>`]);
    } else if (t.t1 && !f.t1 && px >= t.t1) {
      const half = Math.min(Math.floor(t.qty / 2), t.left);
      add(k + "t1", [chat], "⚡", `T1 REACHED — ${s}`, [now, `T1 ${rs(t.t1)} reached.`, "",
        `👉 Sell <b>${half}</b> (if your T1 order didn't fill)`,
        `👉 Edit SL-M: qty <b>${t.left - half}</b> (what's left), trigger cost <b>${rs(t.entry)}</b>`,
        `Then send <code>SOLD ${s} ${half} ${t.t1}</code>`]);
    }
  }
  return out;
}

async function readState(env, file) {
  const r = await fetch(
    `https://api.github.com/repos/${REPO}/contents/state/${file}?ref=${encodeURIComponent(STATE_BRANCH)}`,
    { headers: ghHeaders(env, "application/vnd.github.raw+json") });
  if (r.status === 404) return {};
  if (!r.ok) throw new Error(`GitHub ${file} ${r.status}`);
  return r.json();
}

export function base32(s) {
  const A = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "", out = [];
  for (const c of s.replace(/[\s=-]/g, "").toUpperCase()) {
    const v = A.indexOf(c);
    if (v < 0) throw new Error("bad TOTP secret");
    bits += v.toString(2).padStart(5, "0");
  }
  for (let i = 0; i + 8 <= bits.length; i += 8) out.push(parseInt(bits.slice(i, i + 8), 2));
  return new Uint8Array(out);
}

async function totp(secret, ms = Date.now()) {
  const key = await crypto.subtle.importKey("raw", base32(secret), { name: "HMAC", hash: "SHA-1" }, false, ["sign"]);
  const ctr = new ArrayBuffer(8);
  new DataView(ctr).setUint32(4, Math.floor(ms / 30000));
  const h = new Uint8Array(await crypto.subtle.sign("HMAC", key, ctr));
  const o = h[h.length - 1] & 15;
  const n = ((h[o] & 127) << 24 | h[o + 1] << 16 | h[o + 2] << 8 | h[o + 3]) % 1e6;
  return String(n).padStart(6, "0");
}

// ---------- INDmoney (INDstocks) prices: free, read-only ----------
function indConfigured(env) {
  return !!(env.IND_ACCESS_TOKEN || (env.IND_CLIENT_ID && env.IND_MPIN && env.IND_TOTP_SECRET));
}

async function indToken(env, fresh = false, isTest = false) {
  if (env.IND_ACCESS_TOKEN && !env.IND_CLIENT_ID) return env.IND_ACCESS_TOKEN;
  if (!fresh) {
    const c = JSON.parse((await env.STATE.get("ind_token")) || "{}");
    if (c.token && c.exp > Date.now()) return c.token;
  }
  // INDmoney locks token generation for 15 min after 5 wrong TOTPs: after a failed
  // login, wait 20 min before trying again (never hammer it every minute).
  if (!isTest && (await env.STATE.get("ind_login_wait"))) {
    throw new Error("INDmoney login failed recently; retrying after 20 min");
  }
  const r = await fetch(`${IND}/generate/token`, {
    method: "POST",
    headers: { "x-api-key": env.IND_CLIENT_ID, "content-type": "application/json" },
    body: JSON.stringify({ mpin: String(env.IND_MPIN), totp: await totp(env.IND_TOTP_SECRET) }),
  });
  const j = await r.json().catch(() => ({}));
  const tok = j.token || (j.data && j.data.token);
  if (!r.ok || !tok) {
    const why = String(j.message || (j.error && (j.error.message || j.error)) || "").slice(0, 160);
    // "wait a minute" = INDmoney's 1-token-per-60-s throttle, not a wrong code: short pause.
    // Anything else (wrong MPIN/TOTP): 20 min, so 5 bad codes can never lock the account.
    const throttled = /wait a minute|too many|429/i.test(why) || r.status === 429;
    await env.STATE.put("ind_login_wait", "1", { expirationTtl: throttled ? 90 : 1200 });
    throw new Error(`INDmoney login ${r.status}${why ? ": " + why : ""}`);
  }
  await env.STATE.put("ind_token", JSON.stringify({ token: tok, exp: Date.now() + 23.5 * 3600000 }));
  return tok;
}

async function nseIds(env) {
  const c = JSON.parse((await env.STATE.get("nse_ids")) || "{}");
  if (c.ids && c.exp > Date.now()) return c.ids;
  const doc = await readState(env, "nse_ids.json");
  if (!doc.ids) throw new Error("state/nse_ids.json missing (the watcher builds it on its next run)");
  await env.STATE.put("nse_ids", JSON.stringify({ ids: doc.ids, exp: Date.now() + 12 * 3600000 }));
  return doc.ids;
}

export function indCodes(symbols, ids) {
  const code = {}, missing = [];
  for (const s of symbols) (ids[s] ? (code["NSE_" + ids[s]] = s) : missing.push(s));
  return { code, missing };
}

export function indParse(j, code) {
  const out = {};
  for (const [k, v] of Object.entries((j && j.data) || {})) {
    const px = v && (v.live_price ?? v.ltp);
    if (code[k] && typeof px === "number") out[code[k]] = px;
  }
  return out;
}

async function indLtp(env, token, symbols) {
  const { code, missing } = indCodes(symbols, await nseIds(env));
  const keys = Object.keys(code);
  if (!keys.length) return { auth: true, prices: {}, missing };
  const r = await fetch(`${IND}/market/quotes/ltp?scrip-codes=${encodeURIComponent(keys.join(","))}`, {
    headers: { authorization: token, accept: "application/json" },
  });
  if (r.status === 401 || r.status === 403) {
    const body = await r.text().catch(() => "");
    return { auth: false, status: r.status, why: body.replace(/\s+/g, " ").slice(0, 200) };
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok || String(j.status).toLowerCase() !== "success") {
    throw new Error(`INDmoney LTP ${r.status} ${String(j.message || "").slice(0, 120)}`);
  }
  return { auth: true, prices: indParse(j, code), missing };
}

async function indTest(env) {
  const method = env.IND_CLIENT_ID ? "client id + MPIN + TOTP" : "pasted access token";
  if (!env.STATE) return { ok: false, method, error: "KV binding STATE missing" };
  let token, login = "ok (saved token)";
  try {
    const c = JSON.parse((await env.STATE.get("ind_token")) || "{}");
    if (!(c.token && c.exp > Date.now())) { token = await indToken(env, true, true); login = "ok (new login)"; }
    else token = c.token;
  } catch (e) {
    const wait = /wait a minute/i.test(String(e.message)) ? " → wait 2 minutes, then open ?live=test once" : "";
    return { ok: false, method, login: "failed", error: String(e.message || e) + wait };
  }
  let res = await indLtp(env, token, ["SBIN", "RELIANCE"]).catch((e) => ({ error: String(e.message || e) }));
  if (res.error) return { ok: false, method, login, prices: "failed", error: res.error };
  if (!res.auth) return { ok: false, method, login, prices: `refused (${res.status})`, says: res.why };
  return { ok: true, method, login, prices: res.prices,
           note: "Outside market hours these are the last closing prices." };
}

// Prices for the watch list from INDmoney (free).
async function getPrices(env, symbols) {
  let res = await indLtp(env, await indToken(env), symbols);
  if (!res.auth && env.IND_CLIENT_ID) res = await indLtp(env, await indToken(env, true), symbols);
  return { ...res, source: "INDmoney" };
}

async function telegram(env, chat, text) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chat, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
}

async function live(env, force = false) {
  if (!indConfigured(env)) return { ok: true, off: "no INDmoney secrets" };
  if (!env.TELEGRAM_BOT_TOKEN || !env.GITHUB_TOKEN || !env.STATE) return { ok: false, error: "missing settings" };
  const t = istNow();
  if (!force && (t.weekday === 0 || t.weekday === 6 || t.mins < 555 || t.mins > 930)) {
    return { ok: true, off: "market closed" };
  }
  const [watch, mt] = await Promise.all([readState(env, "watch.json"), readState(env, "mytrades.json")]);
  const list = watchList(watch, mt, t.date);
  if (!list.symbols.length) return { ok: true, watching: 0 };

  let res;
  try {
    res = await getPrices(env, list.symbols);
  } catch (e) {
    res = { auth: false, error: String(e.message || e), source: "INDmoney" };
  }
  if (!res.auth) {
    const flag = `pricefail:${t.date}`;
    if (!(await env.STATE.get(flag))) {
      await env.STATE.put(flag, "1", { expirationTtl: 86400 });
      await telegram(env, ALLOWED[0], `⚠️ <b>Live price alerts off</b>\n${res.source} login or prices were refused. ` +
        "Open the worker URL with ?live=test to see why. The 5-min alerts still work.");
    }
    return { ok: false, source: res.source, error: res.error || `${res.source} auth refused` };
  }

  const sentKey = `livesent:${t.date}`;
  const sent = JSON.parse((await env.STATE.get(sentKey)) || "{}");
  const alerts = decide(list, res.prices, sent, t.hm, mt);
  for (const a of alerts) for (const c of a.to) await telegram(env, c, a.text);
  if (alerts.length) await env.STATE.put(sentKey, JSON.stringify(sent), { expirationTtl: 2 * 86400 });
  return { ok: true, source: res.source, watching: list.symbols, prices: res.prices,
           missing: res.missing && res.missing.length ? res.missing : undefined, sent: alerts.map((a) => a.key) };
}
