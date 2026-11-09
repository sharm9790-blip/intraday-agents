# Telegram verdict format (mobile)

`state/latest_verdict.txt` is sent to Telegram with `parse_mode=HTML` and read on a phone.

Rules:
- Only `<b>`, `<i>` and `<code>` tags. No tables, no markdown (`**`, `#`, `|`). The only link allowed is the Groww stock page in the draft-order block.
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

<b>📲 Set in Groww now</b>
1. ➕ Add HDFCBANK to "My Draft Basket"
2. 🔔 Price alert at ₹766.5
3. After buying: SL-M at ₹761.8
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

Insert right after the `📦 Qty` line of the GO template. It is a ready-to-copy order for the user to place in Groww or INDmoney themselves; nothing is sent to either. `<code>` makes each value tap-to-copy in Telegram.

```
⚡ <b>HIGH CONFIDENCE 6.5/10</b>   ← this line only when confidence is 6/10 or higher
📝 <b>Draft order</b> (you place it)
Intraday · Limit · NSE
Qty  <code>185</code>
Buy limit  <code>766.5</code>
SL-M trigger  <code>761.8</code>
Sell half  <code>92</code> @ <code>777</code>
📲 groww.in/stocks/hdfc-bank-ltd
📲 INDmoney: search <code>HDFCBANK</code>
```

Groww link: `https://groww.in/stocks/<slug>`, slug = the company's full name in lowercase, hyphenated, ending `-ltd` (TATASTEEL → `tata-steel-ltd`, M&M → `mahindra-mahindra-ltd`, HDFCBANK → `hdfc-bank-ltd`). The cloud sandbox can't reach groww.in, so don't try to check it: build the slug carefully and include it. The GitHub watcher opens the link before forwarding and removes the 📲 line if the page doesn't exist. Values are plain numbers (no ₹) so they paste cleanly. "Sell half" = half of Qty (rounded down) @ T1 price. INDmoney has no verifiable stock-page link, so give its line as the tap-to-copy NSE symbol only.

## NO TRADE template

```
⚪ <b>NO TRADE — 11:45 IST</b>

📈 Nifty 24,650  (+0.5%)
🏦 Bank Nifty 57,900  (+0.2%)
Mood: large caps up, midcaps weak

<b>Why nothing passed</b>
• M&amp;M — 3,300 call wall
• TITAN — range already 2× ATR
• DLF — reward below risk

🔎 Next scan: 12:45 IST
<i>Analysis only, not advice. You place orders yourself.</i>
```

## Connector problem

```
⚠️ <b>NO ANALYSIS — 11:45 IST</b>

Market data connectors not attached:
• Groww
• InvestSights

👉 Attach them to the routine on claude.ai
<i>Analysis only, not advice.</i>
```
