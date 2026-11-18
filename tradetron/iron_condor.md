# Strategy Spec — Nifty Intraday Iron Condor 2-6 v1
## METADATA
| Field | Value |
|---|---|
| Strategy name | Nifty Intraday Iron Condor 2-6 v1 |
| Description | Intraday hedged option selling on NIFTY 50 weekly options, for testing. Between 09:20 and 10:00 it buys protective wings (CE at ATM+6 strikes and PE at ATM-6), then sells the body (CE at ATM+2, PE at ATM-2), 1 lot each, current weekly expiry, so the worst loss is capped by the wings. Profit is not booked early: the position is held until 15:15 unless the strategy loss reaches Rs 3,000 per lot multiple, which closes everything. One entry per day. |
| Default underlying | NIFTY 50 |
| Price execution | Market Price |
| Exit shorts first | Yes |
| Reactivation minutes | 2 |
## INIT VARS (top-level)
| Name | Type | Default expression | Notes |
|---|---|---|---|
| entered | number | `Number(1)` | one-shot guard: 1 = flat (wings armed), set to 2 on entry, which also arms the body Repair Once |
## SETS
### Set 1: Iron condor
#### Identity
| Field | Value |
|---|---|
| Set name | Iron condor |
| Underlying | NIFTY 50 |
| List | 0 |
#### Entry condition (AND-joined)
- `Time(NSE)` `>=` `Number(920)`
- `Time(NSE)` `<=` `Number(1000)`
- `Get Runtime("entered")` `==` `Number(1)`
#### Legs
| # | Buy/Sell | Underlying | Type | Expiry | Strike | Qty | Product |
|---|---|---|---|---|---|---|---|
| 1 | B | NIFTY 50 | PE | Current Week | `ATM SPOT("NIFTY 50", -6)` | 1 | MIS |
| 2 | B | NIFTY 50 | CE | Current Week | `ATM SPOT("NIFTY 50", 6)` | 1 | MIS |
#### Set Runtime Vars written on entry
| Name | Type | Value expression |
|---|---|---|
| entered | number | `Number(2)` |
#### Repair conditions
##### Repair 1: Sell the body after the wings (margin-safe)
| Field | Value |
|---|---|
| Type | Repair Once |
| Sub-type | None |
###### Condition
- `Get Runtime("entered")` `==` `Number(2)`
###### Positions
| # | Buy/Sell | Underlying | Type | Expiry | Strike | Qty | Product |
|---|---|---|---|---|---|---|---|
| 1 | S | NIFTY 50 | PE | Current Week | `ATM SPOT("NIFTY 50", -2)` | 1 | MIS |
| 2 | S | NIFTY 50 | CE | Current Week | `ATM SPOT("NIFTY 50", 2)` | 1 | MIS |
## UNIVERSAL EXIT (OR-joined)
- `PNL()` `<=` `Math Operation(Number(-3000), Multiplier(), '*')`
- `Time(NSE)` `>=` `Number(1515)`
