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
//    branch, gets the Groww last traded price for those stocks in ONE call to
//    Groww's live-data LTP endpoint, and sends an alert the first time a price
//    reaches a level (buy zone, cancel level, SL, T1, T2). The 5-min watcher still
//    applies the plan rules (candle closes) and keeps the scorecard.
//    READ ONLY: the only Groww calls are the login (access token) and
//    /v1/live-data/ltp. Nothing here places, changes or cancels orders.
//
// Settings in the Cloudflare dashboard (Worker → Settings → Variables and Secrets):
//   Secret  TELEGRAM_BOT_TOKEN   the bot token
//   Secret  GITHUB_TOKEN         fine-grained PAT, repo intraday-agents only,
//                                Actions: Read and write, Contents: Read-only
//   KV binding  STATE            any KV namespace
//   Cron trigger  * * * * *
//   Groww login, ONE of these (all Secrets):
//     GROWW_API_KEY + GROWW_API_SECRET       (key + secret, checksum login)
//     GROWW_API_KEY + GROWW_TOTP_SECRET      (key + TOTP seed, code made here)
//     GROWW_ACCESS_TOKEN                     (pasted daily token, expires 06:00)
//   Without Groww secrets, part 2 is simply off.

const ALLOWED = ["1322737291", "6338805963"]; // chat ids that may give commands
const REPO = "sharm9790-blip/intraday-agents";
const STATE_BRANCH = "claude/intraday-state";
const RETRY_AFTER_S = 600;
const MAX_TRIES = 3;
const GROWW = "https://api.groww.in";
const OPEN = ["waiting", "in_zone", "t1_hit"];
const IST_MS = 5.5 * 3600 * 1000;

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(Promise.all([check(env), live(env).catch((e) => ({ ok: false, error: String(e) }))]));
  },
  // Opening the worker's URL runs one check — handy for testing.
  // Add ?live=1 to run the live-price check even outside market hours.
  // Add ?live=test to check the Groww login and one price call, any time (read-only).
  async fetch(request, env) {
    const mode = new URL(request.url).searchParams.get("live");
    if (mode === "test") {
      const r = await growwTest(env).catch((e) => ({ ok: false, error: String(e) }));
      return new Response(JSON.stringify({ groww: r }, null, 2), {
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
    out.push({ key, to, text: [`⚡ <b>${title}</b>`, `🕐 ${hm} IST · Groww live price`, "", ...lines].join("\n") });
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

async function sha256hex(s) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
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

async function growwToken(env, fresh = false) {
  if (env.GROWW_ACCESS_TOKEN && !env.GROWW_API_KEY) return env.GROWW_ACCESS_TOKEN;
  if (!env.GROWW_API_KEY || !(env.GROWW_API_SECRET || env.GROWW_TOTP_SECRET)) return null;
  if (!fresh) {
    const c = JSON.parse((await env.STATE.get("groww_token")) || "{}");
    if (c.token && c.exp > Date.now()) return c.token;
  }
  let body;
  if (env.GROWW_API_SECRET) {
    const ts = String(Math.floor(Date.now() / 1000));
    body = { key_type: "approval", checksum: await sha256hex(env.GROWW_API_SECRET + ts), timestamp: Number(ts) };
  } else {
    body = { key_type: "totp", totp: await totp(env.GROWW_TOTP_SECRET) };
  }
  const r = await fetch(`${GROWW}/v1/token/api/access`, {
    method: "POST",
    headers: { authorization: `Bearer ${env.GROWW_API_KEY}`, "content-type": "application/json",
               accept: "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.token) {
    const why = String((j.error && (j.error.message || j.error.code)) || j.message || "").slice(0, 160);
    throw new Error(`Groww login ${r.status}${why ? ": " + why : ""}`);
  }
  // Valid until Groww's expiry, else the next 06:00 IST; refresh 10 min early.
  let exp = Date.parse(j.expiry || "");
  if (!exp) {
    const d = new Date(Date.now() + IST_MS);
    d.setUTCHours(6, 0, 0, 0);
    exp = d.getTime() - IST_MS + (d.getTime() - IST_MS <= Date.now() ? 86400000 : 0);
  }
  await env.STATE.put("groww_token", JSON.stringify({ token: j.token, exp: exp - 600000 }));
  return j.token;
}

async function ltp(token, symbols) {
  const q = symbols.slice(0, 50).map((s) => "NSE_" + s).join(",");
  const r = await fetch(`${GROWW}/v1/live-data/ltp?segment=CASH&exchange_symbols=${encodeURIComponent(q)}`, {
    headers: { authorization: `Bearer ${token}`, accept: "application/json", "x-api-version": "1.0" },
  });
  if (r.status === 401 || r.status === 403) return { auth: false };
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.status !== "SUCCESS") throw new Error(`Groww LTP ${r.status}`);
  const out = {};
  for (const [k, v] of Object.entries(j.payload || {})) if (typeof v === "number") out[k.replace(/^NSE_/, "")] = v;
  return { auth: true, prices: out };
}

// Login + one LTP call for two big stocks. Shows which login method is set up and
// whether it works; never returns the token or any secret.
async function growwTest(env) {
  const method = env.GROWW_API_KEY
    ? (env.GROWW_API_SECRET ? "api key + secret" : env.GROWW_TOTP_SECRET ? "api key + TOTP" : "api key only (add TOTP secret)")
    : env.GROWW_ACCESS_TOKEN ? "daily access token" : "none";
  if (method === "none") return { ok: false, method, error: "no Groww secrets in Cloudflare" };
  if (!env.STATE) return { ok: false, method, error: "KV binding STATE missing" };
  let token;
  try {
    token = await growwToken(env, true);
  } catch (e) {
    return { ok: false, method, login: "failed", error: String(e.message || e) };
  }
  if (!token) return { ok: false, method, login: "failed", error: "secret missing for this method" };
  const res = await ltp(token, ["SBIN", "RELIANCE"]).catch((e) => ({ error: String(e.message || e) }));
  if (res.error) return { ok: false, method, login: "ok", prices: "failed", error: res.error };
  if (!res.auth) return { ok: false, method, login: "ok", prices: "refused (401/403)" };
  return { ok: true, method, login: "ok", prices: res.prices,
           note: "Outside market hours these are the last closing prices." };
}

async function telegram(env, chat, text) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chat, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
}

async function live(env, force = false) {
  if (!env.GROWW_API_KEY && !env.GROWW_ACCESS_TOKEN) return { ok: true, off: "no Groww secrets" };
  if (!env.TELEGRAM_BOT_TOKEN || !env.GITHUB_TOKEN || !env.STATE) return { ok: false, error: "missing settings" };
  const t = istNow();
  if (!force && (t.weekday === 0 || t.weekday === 6 || t.mins < 555 || t.mins > 930)) {
    return { ok: true, off: "market closed" };
  }
  const [watch, mt] = await Promise.all([readState(env, "watch.json"), readState(env, "mytrades.json")]);
  const list = watchList(watch, mt, t.date);
  if (!list.symbols.length) return { ok: true, watching: 0 };

  let token = await growwToken(env);
  let res = await ltp(token, list.symbols);
  if (!res.auth && env.GROWW_API_KEY) {           // token expired early: log in again once
    token = await growwToken(env, true);
    res = await ltp(token, list.symbols);
  }
  if (!res.auth) {
    const flag = `growwfail:${t.date}`;
    if (!(await env.STATE.get(flag))) {
      await env.STATE.put(flag, "1", { expirationTtl: 86400 });
      await telegram(env, ALLOWED[0], "⚠️ <b>Live price alerts off</b>\nGroww API login was refused. " +
        "Check the Groww secrets in Cloudflare (Worker → Settings). The 5-min alerts still work.");
    }
    return { ok: false, error: "Groww auth refused" };
  }

  const sentKey = `livesent:${t.date}`;
  const sent = JSON.parse((await env.STATE.get(sentKey)) || "{}");
  const alerts = decide(list, res.prices, sent, t.hm, mt);
  for (const a of alerts) for (const c of a.to) await telegram(env, c, a.text);
  if (alerts.length) await env.STATE.put(sentKey, JSON.stringify(sent), { expirationTtl: 2 * 86400 });
  return { ok: true, watching: list.symbols, prices: res.prices, sent: alerts.map((a) => a.key) };
}
