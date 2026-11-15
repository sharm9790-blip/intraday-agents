# Telegram verdict format (mobile)

`state/latest_verdict.txt` is sent to Telegram with `parse_mode=HTML` and read on a phone.

Rules:
- Only `<b>`, `<i>`, `<code>` and `<pre>` tags. No markdown (`**`, `#`, `|`). No links (no stock-page links).
- **Tables** = a `<pre>` block with columns lined up by spaces (monospace on the phone): at most **32 characters per line**, 3–5 short columns, symbols cut to 10 characters, numbers right-aligned, no ₹ inside the table (put "₹" in the heading line above it). Use a table whenever 2+ stocks are compared (watchlists, rejected list). The GO card itself stays as short lines.
- End result messages (NO TRADE, watchlists) with **one 💡 line**: what the numbers mean today, in a few words.
- Escape `&` as `&amp;`, `<` as `&lt;`, `>` as `&gt;` in any text (stock `M&M` → `M&amp;M`).
- One fact per line, ≤ 34 characters where possible. Blank line between blocks.
- Prices as `₹766.5`, Indian grouping for big numbers (`₹1,42,000` or `₹1.42L`).
- Keep it under 2,500 characters. Reasons are a few words, not sentences.
- Fill every `<…>` with real values from the Judge. Drop a line rather than write "n/a".

## GO template

```
🟢 <b>GO — HDFCBANK</b>
🕐 09:54 IST  ·  Confidence 5.5/10

📥 Buy  <b>₹766.5</b>  (max ₹767)
⚡ <b>BUY NOW</b> (entry_type now) or ⏳ <b>Wait for dip</b> (entry_type dip)
🛑 Stop loss  <b>₹761.8</b>
🎯 Target 1  <b>₹777</b>  (sell half)
🎯 Target 2  <b>₹784</b>
📦 Qty  <b>185</b> shares

<b>✅ Buy only if</b>
• Dips to ₹766–767.5
• Green 5-min close above ₹766
• Bank Nifty above 57,600

<b>❌ Cancel if</b>
• Not triggered by 10:45
• Hits ₹777 before the dip
• 5-min close below ₹764

<b>💰 Risk vs reward</b>
Max loss ≈ ₹1,000
Profit ≈ ₹835 (T1) · ₹2,460 (T2)
Reward 1.8× the risk

<b>📲 Set in INDmoney now</b>
1. 🔎 Search HDFCBANK
2. 🔔 Price alert at ₹766.5 (no buy order yet)
3. Buy only on 🔔 BUY TRIGGER, then SL-M at ₹761.8
4. Sell 92 shares at ₹777
5. Exit all by 15:00

<b>Why this stock</b>
• Futures long build-up
• Puts written at 750–770
• At 30-day high, sector strong

<b>Skipped</b>
• M&amp;M — 3,300 call wall
• TITAN — range already 2× ATR

📡 Watcher checks every 5 min
<i>Analysis only, not advice. You place orders yourself.</i>
```

## Draft order block (every GO)

Insert right after the `📦 Qty` line of the GO template. It is a ready-to-copy order for the user to place in INDmoney themselves; nothing is sent. `<code>` makes each value tap-to-copy in Telegram.

For a **DIP** plan (`entry_type` dip) the user must NOT place a buy limit in advance: it fills while the price is still falling, before the confirmation (13 Nov: −₹1,182 on two such fills; the plans themselves never triggered). Use:

```
⚡ <b>HIGH CONFIDENCE 6.5/10</b>   ← this line only when confidence is 6/10 or higher
📝 <b>Draft order</b> (you place it)
⏳ <b>Don't place the buy yet</b>
Wait for 🔔 BUY TRIGGER (green
5-min close back above 766)
Then: Qty <code>185</code> · Limit <code>768</code>
SL-M trigger  <code>761.8</code>
Sell half  <code>92</code> @ <code>777</code>
📲 INDmoney: search <code>HDFCBANK</code>
```

For a **BUY NOW** plan (`entry_type` now) the order can go in at once:

```
📝 <b>Draft order</b> (you place it)
Intraday · Limit · NSE
Qty  <code>185</code>
Buy limit  <code>768</code>   ← max_entry
SL-M trigger  <code>761.8</code>
Sell half  <code>92</code> @ <code>777</code>
📲 INDmoney: search <code>HDFCBANK</code>
```

No stock-page link: the user places orders in INDmoney. Values are plain numbers (no ₹) so they paste cleanly. "Sell half" = half of Qty (rounded down) @ T1 price. Give the INDmoney line as the tap-to-copy NSE symbol only.

## NO TRADE template

```
⚪ <b>NO TRADE — 11:45 IST</b>

📈 Nifty 24,650  (+0.5%)
🏦 Bank Nifty 57,900  (+0.2%)
Mood: large caps up, midcaps weak

<b>Why nothing passed</b>
<pre>Stock      R:R  Reason
M&amp;M       1.2  3,300 call wall
TITAN      0.9  range 2× ATR
DLF        1.3  reward &lt; risk</pre>
💡 Large caps capped by call walls

🔎 Next scan: 12:45 IST
<i>Analysis only, not advice. You place orders yourself.</i>
```

## Connector problem

```
⚠️ <b>NO ANALYSIS — 11:45 IST</b>

Market data connectors not attached:
• INDmoney
• InvestSights

👉 Attach them to the routine on claude.ai
<i>Analysis only, not advice.</i>
```


## Premarket templates (`state/premarket_msg.txt`)

Overnight (~08:50) and final (~09:12, title `📋 <b>FINAL WATCHLIST · 09:12</b>`, add the gap per stock):

```
🌅 <b>PREMARKET WATCHLIST · 08:52</b>

🌍 GIFT Nifty +0.4% · US up · crude flat
📅 Results today: TCS (skipped)

<b>Watchlist</b> (₹; buy only above, never before 09:20)
<pre>Stock      Above   SL   T1  Qty
KALYANKJIL   701  692  716  110
MCX         3768 3712 3840   17
LICHSGFIN  592.5  585  603  133</pre>
<b>Why</b>
• KALYANKJIL — closed at high, 2× vol
• MCX — 20-day high, F&amp;O long build-up
💡 3 of 5 from financials: sector strong

⏳ <b>Don't buy before 09:20.</b>
🔔 The bot sends BUY NOW when the
first 5-min candle closes above the
level on good volume. Not by 09:40 → dropped.
<i>Analysis only, not advice. You place orders yourself.</i>
```
Final list only: add a `Gap` column to the table, and dropped stocks under `<b>Dropped</b>` as a small table (`Stock  Gap  Reason`, e.g. `HINDCOPPER +5.2% gap > 4%`).
