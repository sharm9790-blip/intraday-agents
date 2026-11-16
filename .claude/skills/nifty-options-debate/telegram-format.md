# Telegram format — Nifty options PAPER (mobile)

Same rules as `.claude/skills/intraday-debate/telegram-format.md`: Telegram HTML (`<b>`, `<i>`,
`<code>`, `<pre>` only), lines ≤ 34 characters, blank lines between blocks, `&` → `&amp;`,
`<` → `&lt;`, `>` → `&gt;`, under 2,000 characters, fill every `<…>` with real values.
Every message starts with `🧪 <b>PAPER</b>`. **No draft order, no INDmoney order steps.**

## GO (paper)
```
🧪 <b>PAPER</b> · Nifty options
🟢 <b>BUY 24500 CE</b> · 18 Nov
🕐 10:15 IST · Confidence 6/10

📈 Trigger: Nifty above <b>24,480</b>
💰 Premium ~<b>₹140</b> (max ₹144)
🛑 SL: Nifty 24,420 / ₹105
🎯 T1 ₹190 · T2 ₹220
📦 1 lot (65 qty) · Δ 0.55

<b>Risk vs reward</b> (code)
Max loss ≈ ₹2,300
Net R:R 1.6

<b>Why</b>
• Above VWAP + opening range
• Puts written at 24,400
• PCR 1.1 → 1.3

<b>Cancel if</b>
• Not triggered by 10:45
• Nifty 5-min close &lt; VWAP

📊 Paper record: 7 trades · +₹3,120
<i>Paper trade only, not advice.</i>
```

## NO TRADE
```
🧪 <b>PAPER</b> · Nifty options
⚪ <b>NO TRADE — 11:15 IST</b>

📈 Nifty 24,510 (+0.2%) · VIX 13.4
🧱 Calls 24,600 · Puts 24,400
PCR 0.98 · Max pain 24,500

<b>Why not</b>
• Inside opening range
• Call and put writing both rising
💡 Range day: buyers lose to decay

🔎 Next check: 12:15 IST
<i>Paper trade only, not advice.</i>
```
A code BLOCK uses this template with title `⛔ <b>BLOCKED — HH:MM</b>` and the code's reasons.

## Premarket (~08:50)
```
🧪 <b>PAPER</b> · Nifty premarket
🌍 Asia +0.3% · US up · VIX 13.1
📅 Event: RBI policy 10:00

<pre>Level        Nifty
Call wall    24,600
Yday high    24,560
Yday close   24,480
Yday low     24,390
Put floor    24,400</pre>
💡 Bias: mild up, wait for 09:30

<i>Paper trade only, not advice.</i>
```

## Day report (~15:10)
```
🧪 <b>PAPER</b> · Nifty day report

<b>Today</b>
<pre>Contract   Result      P&amp;L
24500 CE   T1        +3,181
24450 PE   SL        −2,050</pre>

<b>Last 20 days</b> (after costs)
Trades 9 · Win rate 44%
Net ₹+2,480 · Profit factor 1.2
Not filled: 3

💡 &lt;3 weeks: too early to judge
<i>Paper trade only, not advice.</i>
```
