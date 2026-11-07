// Instant bot replies at any hour (Cloudflare Worker, free plan).
//
// Every minute (Cron Trigger "* * * * *") this asks Telegram whether anyone
// messaged the bot. If an allowed chat did, it shows "typing…" and starts the
// GitHub workflow mytrade.yml, which reads the message and replies (~30–60 s).
//
// It never confirms updates (no `offset`), so mytrade.py still receives every
// message exactly as before. KV key "last" remembers what was already handed
// over, so each message starts the workflow once (retried if it was not
// answered after 10 min, at most 3 times).
//
// Settings in the Cloudflare dashboard (Worker → Settings):
//   Secret  TELEGRAM_BOT_TOKEN   the bot token
//   Secret  GITHUB_TOKEN         fine-grained PAT, repo intraday-agents only,
//                                Actions: Read and write
//   KV binding  STATE            any KV namespace
//   Cron trigger  * * * * *

const ALLOWED = ["1322737291", "6338805963"]; // chat ids that may give commands
const REPO = "sharm9790-blip/intraday-agents";
const RETRY_AFTER_S = 600;
const MAX_TRIES = 3;

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(check(env));
  },
  // Opening the worker's URL runs one check — handy for testing.
  async fetch(request, env) {
    const r = await check(env);
    return new Response(JSON.stringify(r, null, 2), {
      headers: { "content-type": "application/json" },
    });
  },
};

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
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "user-agent": "intraday-bot-worker",
        "content-type": "application/json",
      },
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
