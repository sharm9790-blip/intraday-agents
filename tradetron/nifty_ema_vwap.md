# Strategy Spec — Nifty EMA VWAP Pullback v1
## METADATA
| Field | Value |
|---|---|
| Strategy name | Nifty EMA VWAP Pullback v1 |
| Description | Intraday Nifty futures (current month, 2 lots, MIS) trend-pullback on 5-min candles. Long when EMA 9 is above EMA 21 and both are rising, price is above VWAP, and the last candle dipped to EMA 9 but closed back above it green, on at least 1.2 times the 20-candle average volume, with Nifty futures up 0.1 to 2 percent on the day. Short is the mirror image. Entries 09:45 to 14:00, one trade a day. Stop beyond the pullback candle and VWAP, at least 0.1 percent and skipped if more than about 0.57 percent away. One lot is booked at 1.5R and the stop moves to cost; the other lot exits on a 5-min close back across EMA 21, at 3R, or at 15:10. Built for paper trading (Live Offline) first. |
| Price execution | Market Price |
| Exit shorts first | Yes |
| Reactivation minutes | 2 |
## UNIVERSAL EXIT
- `Time(NSE)` `>=` `Number(1510)`
## SETS
### Set 1: EMA VWAP long
#### Identity
| Field | Value |
|---|---|
| Set name | EMA VWAP long |
| Underlying | NIFTY 50 |
#### Entry condition (AND-joined)
- `Time(NSE)` `>=` `Number(945)`
- `Time(NSE)` `<=` `Number(1400)`
- `Positions Detail('Entry','All','All','quantity','NIFTY 50')` `==` `Number(0)`
- `Position(Volume series(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All'),-1)` `>=` `Math Operation(Position(SMA(Volume series(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All'),20),-1), Number(1.2), '*')`
- `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),9),-1)` `>` `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),21),-1)`
- `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),9),-1)` `>` `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),9),-3)`
- `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),21),-1)` `>` `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),21),-3)`
- `Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)` `>` `Position(VWAP Series(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m'),-1)`
- `Position(LOW(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)` `<=` `Math Operation(Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),9),-1), Number(1.0005), '*')`
- `Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)` `>` `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),9),-1)`
- `Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)` `>` `Position(OPEN(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)`
- `Math Operation(Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'day','All')),-1), '/')` `>=` `Number(1.001)`
- `Math Operation(Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'day','All')),-1), '/')` `<=` `Number(1.02)`
- `Math Operation(Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), Min(Position(LOW(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), Position(VWAP Series(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m'),-1)), '/')` `<=` `Number(1.0057)`
#### Legs
| # | Buy/Sell | Underlying | Type | Expiry | Strike | Qty | Product |
|---|---|---|---|---|---|---|---|
| 1 | B | NIFTY 50 | XX | Current Month |  | 2 | MIS |
#### Set Runtime Vars written on entry
| Name | Type | Value expression |
|---|---|---|
| L_sl | number | `Min(Math Operation(Min(Position(LOW(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), Position(VWAP Series(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m'),-1)), Number(0.9997), '*'), Math Operation(Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), Number(0.999), '*'))` |
#### Exit condition (OR-joined)
- (group, AND-joined)
  - `Get Runtime Number("L_t1")` `<` `Number(1)`
  - `LTP(Instrument Name("NFO,NIFTY 50,Current Month,,,,"))` `<=` `Get Runtime Number("L_sl")`
- (group, AND-joined)
  - `Get Runtime Number("L_t1")` `>=` `Number(1)`
  - `LTP(Instrument Name("NFO,NIFTY 50,Current Month,,,,"))` `<=` `Traded Instrument('Entry','price','NIFTY 50',1,1,1)`
- (group, AND-joined)
  - `Get Runtime Number("L_t1")` `>=` `Number(1)`
  - `LTP(Instrument Name("NFO,NIFTY 50,Current Month,,,,"))` `>=` `Math Operation(Traded Instrument('Entry','price','NIFTY 50',1,1,1), Math Operation(Math Operation(Traded Instrument('Entry','price','NIFTY 50',1,1,1), Get Runtime Number("L_sl"), '-'), Number(3), '*'), '+')`
- (group, AND-joined)
  - `Get Runtime Number("L_t1")` `>=` `Number(1)`
  - `Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)` `<` `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),21),-1)`
#### Repair conditions
##### Repair 1: Book half at T1 (1.5R)
| Field | Value |
|---|---|
| Type | Repair Once |
| Sub-type | None |
###### Condition
- `LTP(Instrument Name("NFO,NIFTY 50,Current Month,,,,"))` `>=` `Math Operation(Traded Instrument('Entry','price','NIFTY 50',1,1,1), Math Operation(Math Operation(Traded Instrument('Entry','price','NIFTY 50',1,1,1), Get Runtime Number("L_sl"), '-'), Number(1.5), '*'), '+')`
###### Positions
| # | Buy/Sell | Underlying | Type | Expiry | Strike | Qty | Product |
|---|---|---|---|---|---|---|---|
| 1 | S | NIFTY 50 | XX | Current Month |  | 1 | MIS |
###### Runtime Vars
| Name | Type | Value expression |
|---|---|---|
| L_t1 | number | `Number(1)` |
### Set 2: EMA VWAP short
#### Identity
| Field | Value |
|---|---|
| Set name | EMA VWAP short |
| Underlying | NIFTY 50 |
#### Entry condition (AND-joined)
- `Time(NSE)` `>=` `Number(945)`
- `Time(NSE)` `<=` `Number(1400)`
- `Positions Detail('Entry','All','All','quantity','NIFTY 50')` `==` `Number(0)`
- `Position(Volume series(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All'),-1)` `>=` `Math Operation(Position(SMA(Volume series(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All'),20),-1), Number(1.2), '*')`
- `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),9),-1)` `<` `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),21),-1)`
- `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),9),-1)` `<` `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),9),-3)`
- `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),21),-1)` `<` `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),21),-3)`
- `Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)` `<` `Position(VWAP Series(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m'),-1)`
- `Position(HIGH(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)` `>=` `Math Operation(Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),9),-1), Number(0.9995), '*')`
- `Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)` `<` `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),9),-1)`
- `Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)` `<` `Position(OPEN(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)`
- `Math Operation(Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'day','All')),-1), '/')` `<=` `Number(0.999)`
- `Math Operation(Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'day','All')),-1), '/')` `>=` `Number(0.98)`
- `Math Operation(Max(Position(HIGH(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), Position(VWAP Series(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m'),-1)), Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), '/')` `<=` `Number(1.0057)`
#### Legs
| # | Buy/Sell | Underlying | Type | Expiry | Strike | Qty | Product |
|---|---|---|---|---|---|---|---|
| 1 | S | NIFTY 50 | XX | Current Month |  | 2 | MIS |
#### Set Runtime Vars written on entry
| Name | Type | Value expression |
|---|---|---|
| S_sl | number | `Max(Math Operation(Max(Position(HIGH(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), Position(VWAP Series(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m'),-1)), Number(1.0003), '*'), Math Operation(Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1), Number(1.001), '*'))` |
#### Exit condition (OR-joined)
- (group, AND-joined)
  - `Get Runtime Number("S_t1")` `<` `Number(1)`
  - `LTP(Instrument Name("NFO,NIFTY 50,Current Month,,,,"))` `>=` `Get Runtime Number("S_sl")`
- (group, AND-joined)
  - `Get Runtime Number("S_t1")` `>=` `Number(1)`
  - `LTP(Instrument Name("NFO,NIFTY 50,Current Month,,,,"))` `>=` `Traded Instrument('Entry','price','NIFTY 50',2,1,1)`
- (group, AND-joined)
  - `Get Runtime Number("S_t1")` `>=` `Number(1)`
  - `LTP(Instrument Name("NFO,NIFTY 50,Current Month,,,,"))` `<=` `Math Operation(Traded Instrument('Entry','price','NIFTY 50',2,1,1), Math Operation(Math Operation(Traded Instrument('Entry','price','NIFTY 50',2,1,1), Get Runtime Number("S_sl"), '-'), Number(3), '*'), '+')`
- (group, AND-joined)
  - `Get Runtime Number("S_t1")` `>=` `Number(1)`
  - `Position(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),-1)` `>` `Position(EMA(CLOSE(Symbol(Instrument Name("NFO,NIFTY 50,Current Month,,,,"),'5m','All')),21),-1)`
#### Repair conditions
##### Repair 1: Book half at T1 (1.5R)
| Field | Value |
|---|---|
| Type | Repair Once |
| Sub-type | None |
###### Condition
- `LTP(Instrument Name("NFO,NIFTY 50,Current Month,,,,"))` `<=` `Math Operation(Traded Instrument('Entry','price','NIFTY 50',2,1,1), Math Operation(Math Operation(Traded Instrument('Entry','price','NIFTY 50',2,1,1), Get Runtime Number("S_sl"), '-'), Number(1.5), '*'), '+')`
###### Positions
| # | Buy/Sell | Underlying | Type | Expiry | Strike | Qty | Product |
|---|---|---|---|---|---|---|---|
| 1 | B | NIFTY 50 | XX | Current Month |  | 1 | MIS |
###### Runtime Vars
| Name | Type | Value expression |
|---|---|---|
| S_t1 | number | `Number(1)` |
