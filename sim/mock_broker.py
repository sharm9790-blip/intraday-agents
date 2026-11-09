"""Mock broker for testing order-execution latency (educational prototype).

MockGrowwAPI copies the *shape* of the growwapi SDK (the same constant names
and a place_order(...) with the same keyword arguments), so the calling code
reads like the real thing. It never opens a network connection. Every
"order" is filled in memory against a simulated price, with random network
delay and random failures, so you can test the timing loop and the error
handling safely.

Run:  python3 sim/mock_broker.py            (20 simulated market orders)
      python3 sim/mock_broker.py --runs 100 --fail-rate 0.1
"""
import argparse
import random
import statistics
import time
import uuid


# ---- Exceptions (same idea as the SDK's error classes) ---------------------
class GrowwAPIException(Exception):
    """Base error for every mock API failure."""


class GrowwAPIAuthenticationException(GrowwAPIException):
    pass


class GrowwAPITimeoutException(GrowwAPIException):
    pass


class GrowwAPIRateLimitException(GrowwAPIException):
    pass


# ---- Mock client ------------------------------------------------------------
class MockGrowwAPI:
    # Constants named like the SDK's, so swapping code between them is obvious.
    EXCHANGE_NSE = "NSE"
    SEGMENT_CASH = "CASH"
    PRODUCT_MIS = "MIS"            # intraday
    ORDER_TYPE_MARKET = "MARKET"
    VALIDITY_DAY = "DAY"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    # Made-up reference prices for the simulation.
    PRICES = {"WIPRO": 245.30, "INFY": 1480.0, "TCS": 3120.0}

    def __init__(self, token, latency_ms=(40, 250), fail_rate=0.05, seed=None):
        if not token or not isinstance(token, str):
            raise GrowwAPIAuthenticationException("token must be a non-empty string")
        self._token = token
        self._lat = latency_ms
        self._fail = fail_rate
        self._rng = random.Random(seed)
        self.orders = []

    def _network(self):
        """Simulate the round trip: sleep, then maybe fail."""
        time.sleep(self._rng.uniform(*self._lat) / 1000)
        r = self._rng.random()
        if r < self._fail * 0.5:
            raise GrowwAPITimeoutException("simulated network timeout")
        if r < self._fail:
            raise GrowwAPIRateLimitException("simulated 429 rate limit")

    def place_order(self, trading_symbol, quantity, validity, exchange, segment,
                    product, order_type, transaction_type, price=0.0,
                    trigger_price=None, order_reference_id=None):
        if quantity < 1:
            raise GrowwAPIException("quantity must be >= 1")
        if trading_symbol not in self.PRICES:
            raise GrowwAPIException(f"unknown symbol {trading_symbol}")
        self._network()
        ref = self.PRICES[trading_symbol]
        # A market order fills at once, with a little slippage.
        fill = round(ref * (1 + self._rng.uniform(-0.0005, 0.0005)), 2)
        order = {
            "groww_order_id": "SIM-" + uuid.uuid4().hex[:10].upper(),
            "order_status": "EXECUTED",
            "trading_symbol": trading_symbol, "quantity": quantity,
            "exchange": exchange, "segment": segment, "product": product,
            "order_type": order_type, "validity": validity,
            "transaction_type": transaction_type, "average_fill_price": fill,
            "order_reference_id": order_reference_id,
        }
        self.orders.append(order)
        return order


# ---- Latency test loop -------------------------------------------------------
def run(runs, fail_rate, seed):
    api = MockGrowwAPI("DUMMY-TOKEN-FOR-SIMULATION", fail_rate=fail_rate, seed=seed)
    times, errors = [], {}
    for i in range(runs):
        t0 = time.perf_counter()
        try:
            o = api.place_order(
                trading_symbol="WIPRO", quantity=1,
                validity=api.VALIDITY_DAY, exchange=api.EXCHANGE_NSE,
                segment=api.SEGMENT_CASH, product=api.PRODUCT_MIS,
                order_type=api.ORDER_TYPE_MARKET,
                transaction_type=api.TRANSACTION_TYPE_BUY,
                order_reference_id=f"sim-{i}",
            )
            ms = (time.perf_counter() - t0) * 1000
            times.append(ms)
            print(f"#{i + 1:>3} {o['order_status']} {o['groww_order_id']} "
                  f"@ ₹{o['average_fill_price']}  {ms:6.1f} ms")
        except (GrowwAPITimeoutException, GrowwAPIRateLimitException) as e:
            errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
            print(f"#{i + 1:>3} network error: {e}")
        except GrowwAPIException as e:
            errors["GrowwAPIException"] = errors.get("GrowwAPIException", 0) + 1
            print(f"#{i + 1:>3} API error: {e}")

    print("\n--- summary ---")
    print(f"filled {len(times)}/{runs}, errors {errors or 'none'}")
    if times:
        s = sorted(times)
        p95 = s[min(len(s) - 1, int(len(s) * 0.95))]
        print(f"latency ms: min {s[0]:.1f}  median {statistics.median(s):.1f}  "
              f"p95 {p95:.1f}  max {s[-1]:.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--fail-rate", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    run(a.runs, a.fail_rate, a.seed)
