# Strategy Spec — Opening Momentum MOM50SHAR v1
## METADATA
| Field | Value |
|---|---|
| Strategy name | Opening Momentum MOM50SHAR v1 |
| Description | Intraday long-only opening momentum on the NIFTY 50 stocks. Between 09:20 and 09:40, buys a stock that gapped up at most 2.5%, is up 1% to 3.5% on the day, closed its last 5-min candle green near the day high and above VWAP, and has already traded at least 20% of a normal day's volume, while NIFTYBEES is not down more than 0.3%. At most 2 stocks a day. Stop = the higher of the day low and VWAP minus 0.3% (at least 0.4% away, skip if more than 1.5%). Half is sold at 1.5R and the rest then exits at cost, a trailing stop, 3R, or 15:00. If 1.5R is not reached by 11:15 the whole trade is closed. Built for paper trading first. |
| Price execution | Market Price |
| Exit shorts first | Yes |
| Reactivation minutes | 2 |
## UNIVERSAL EXIT
- `Time(NSE)` `>=` `Number(1500)`
## SETS
### Set 1: Opening momentum
#### Identity
| Field | Value |
|---|---|
| Set name | Opening momentum |
| Underlying | |
| List | 265134 |
#### Entry condition (AND-joined)
- `Time(NSE)` `>=` `Number(920)`
- `Time(NSE)` `<=` `Number(940)`
- `Positions Detail('Entry','All','All','quantity',"|MOM50SHAR|")` `==` `Number(0)`
- `Positions Detail('Entry','B','All','count','')` `<` `Number(2)`
- `Math Operation(Position(OPEN(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'day','All')),0), Position(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'day','All')),-1), '/')` `<=` `Number(1.025)`
- `Math Operation(Position(OPEN(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'day','All')),0), Position(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'day','All')),-1), '/')` `>=` `Number(0.99)`
- `Math Operation(Position(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m','All')),-1), Position(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'day','All')),-1), '/')` `>=` `Number(1.01)`
- `Math Operation(Position(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m','All')),-1), Position(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'day','All')),-1), '/')` `<=` `Number(1.035)`
- `Position(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m','All')),-1)` `>` `Position(OPEN(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m','All')),-1)`
- `Position(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m','All')),-1)` `>=` `Math Operation(Position(HIGH(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'day','All')),0), Number(0.998), '*')`
- `Position(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m','All')),-1)` `>` `Position(VWAP Series(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m'),-1)`
- `Position(Volume series(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'day','All'),0)` `>=` `Math Operation(Position(SMA(Volume series(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'day','All'),20),-1), Number(0.2), '*')`
- `Math Operation(Position(CLOSE(Symbol(Instrument Name("NSE,NIFTYBEES,,,,,"),'5m','All')),-1), Position(CLOSE(Symbol(Instrument Name("NSE,NIFTYBEES,,,,,"),'day','All')),-1), '/')` `>=` `Number(0.997)`
- `Math Operation(Position(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m','All')),-1), Max(Position(LOW(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'day','All')),0), Math Operation(Position(VWAP Series(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m'),-1), Number(0.997), '*')), '/')` `<=` `Number(1.015)`
#### Sizing
| Field | Value |
|---|---|
| Qty type | Value |
#### Legs
| # | Buy/Sell | Underlying | Type | Expiry | Strike | Qty | Product |
|---|---|---|---|---|---|---|---|
| 1 | B | | Stock | | | 25000 | MIS |
| 2 | B | | Stock | | | 25000 | MIS |
#### Set Runtime Vars written on entry
| Name | Type | Value expression |
|---|---|---|
| \|MOM50SHAR\|_sl | number | `Min(Max(Position(LOW(Symbol(Instrument Name("NSE,\|MOM50SHAR\|,,,,,"),'day','All')),0), Math Operation(Position(VWAP Series(Instrument Name("NSE,\|MOM50SHAR\|,,,,,"),'5m'),-1), Number(0.997), '*')), Math Operation(Position(CLOSE(Symbol(Instrument Name("NSE,\|MOM50SHAR\|,,,,,"),'5m','All')),-1), Number(0.996), '*'))` |
#### Exit condition (OR-joined)
- (group, AND-joined)
  - `Get Runtime Number("|MOM50SHAR|_t1")` `<` `Number(1)`
  - `LTP(Instrument Name("NSE,|MOM50SHAR|,,,,,"))` `<=` `Get Runtime Number("|MOM50SHAR|_sl")`
- (group, AND-joined)
  - `Get Runtime Number("|MOM50SHAR|_t1")` `<` `Number(1)`
  - `Time(NSE)` `>=` `Number(1115)`
- (group, AND-joined)
  - `Get Runtime Number("|MOM50SHAR|_t1")` `>=` `Number(1)`
  - `LTP(Instrument Name("NSE,|MOM50SHAR|,,,,,"))` `<=` `Traded Instrument('Entry','price',"|MOM50SHAR|",1,1,1)`
- (group, AND-joined)
  - `Get Runtime Number("|MOM50SHAR|_t1")` `>=` `Number(1)`
  - `LTP(Instrument Name("NSE,|MOM50SHAR|,,,,,"))` `>=` `Math Operation(Traded Instrument('Entry','price',"|MOM50SHAR|",1,1,1), Math Operation(Math Operation(Traded Instrument('Entry','price',"|MOM50SHAR|",1,1,1), Get Runtime Number("|MOM50SHAR|_sl"), '-'), Number(3), '*'), '+')`
- (group, AND-joined)
  - `Get Runtime Number("|MOM50SHAR|_t1")` `>=` `Number(1)`
  - `Position(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m','All')),-1)` `<=` `Math Operation(Position(Highest High Value(CLOSE(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m','All')),6),-1), Position(ATR(Symbol(Instrument Name("NSE,|MOM50SHAR|,,,,,"),'5m','All'),6),-1), '-')`
#### Repair conditions
##### Repair 1: Book half at T1 (1.5R)
| Field | Value |
|---|---|
| Type | Repair Once |
| Sub-type | None |
###### Condition
- `LTP(Instrument Name("NSE,|MOM50SHAR|,,,,,"))` `>=` `Math Operation(Traded Instrument('Entry','price',"|MOM50SHAR|",1,1,1), Math Operation(Math Operation(Traded Instrument('Entry','price',"|MOM50SHAR|",1,1,1), Get Runtime Number("|MOM50SHAR|_sl"), '-'), Number(1.5), '*'), '+')`
###### Positions
| # | Buy/Sell | Underlying | Type | Expiry | Strike | Qty | Product |
|---|---|---|---|---|---|---|---|
| 1 | S | | Stock | | | `Traded Instrument('Entry','quantity',"\|MOM50SHAR\|",1,1,1)` | MIS |
###### Runtime Vars
| Name | Type | Value expression |
|---|---|---|
| \|MOM50SHAR\|_t1 | number | `Number(1)` |
