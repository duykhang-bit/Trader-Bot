"""Unit tests for grid_copy.GridCopyService with a fake Binance exchange."""
import os
import tempfile
import unittest

import grid_copy as gc


class FakeExchange:
    def __init__(self, price=100.0):
        self.price = price
        self.orders = {}
        self.history = {}
        self.next_id = 1
        self.amount = 0.0
        self.balance = 100.0
        self.market_orders = []

    def get_mark_price(self, symbol):
        return self.price

    def get_position(self, symbol):
        if self.amount == 0:
            return None
        return {"positionAmt": str(self.amount), "unRealizedProfit": "0", "markPrice": str(self.price)}

    def get_account_balance(self):
        return self.balance

    def get_qty_precision(self, symbol):
        return (0.01, 1000.0, 2, 5.0)

    def set_leverage(self, symbol, lev):
        return lev

    def _round_price(self, price, symbol=""):
        return round(price, 4)

    def place_limit_order(self, symbol, side, qty, price, client_order_id=""):
        oid = self.next_id
        self.next_id += 1
        self.orders[oid] = {"orderId": oid, "side": side, "qty": qty, "price": price,
                            "clientOrderId": client_order_id, "executedQty": "0", "status": "NEW"}
        return {"orderId": oid}

    def place_market_order(self, symbol, side, qty, reduce_only=False):
        self.market_orders.append((side, qty, reduce_only))
        self.amount += qty if side == "BUY" else -qty
        self.amount = round(self.amount, 8)
        return {}

    def fill(self, oid):
        order = self.orders.pop(oid)
        order["status"] = "FILLED"
        order["executedQty"] = str(order["qty"])
        self.history[oid] = order
        self.amount = round(self.amount + (order["qty"] if order["side"] == "BUY" else -order["qty"]), 8)

    def _get(self, path, params=None, signed=False):
        if path == "/fapi/v1/openOrders":
            return list(self.orders.values())
        if path == "/fapi/v1/order":
            oid = params["orderId"]
            return self.orders.get(oid) or self.history.get(oid) or {}
        raise AssertionError(path)

    def _delete(self, path, params):
        order = self.orders.pop(params["orderId"], None)
        if order:
            order["status"] = "CANCELED"
            self.history[order["orderId"]] = order
        return {}


class GridCopyTests(unittest.TestCase):
    def setUp(self):
        self.ex = FakeExchange()
        self.trading = True
        self.svc = gc.GridCopyService(
            self.ex, is_trading_enabled=lambda: self.trading,
            persistence_file=os.path.join(tempfile.mkdtemp(), "g.json"))

    def start(self, **kw):
        args = dict(symbol="TESTUSDT", lower=90, upper=110, grids=4, margin_usdt=20, leverage=5)
        args.update(kw)
        return self.svc.start_grid(**args)

    def orders_by_price(self):
        return {o["price"]: o for o in self.ex.orders.values()}

    def test_start_places_buys_below_sells_above_and_skips_nearest(self):
        r = self.start()
        self.assertTrue(r["ok"], r)
        prices = self.orders_by_price()
        self.assertEqual(sorted(prices), [90, 95, 105, 110])
        self.assertEqual(prices[95]["side"], "BUY")
        self.assertEqual(prices[105]["side"], "SELL")

    def test_buy_fill_places_sell_one_level_up_and_round_trip(self):
        self.start()
        buy = self.orders_by_price()[95]
        self.ex.price = 95
        self.ex.fill(buy["orderId"])
        self.svc.cycle("TESTUSDT")
        prices = self.orders_by_price()
        self.assertEqual(prices[100]["side"], "SELL")
        self.ex.price = 100
        self.ex.fill(prices[100]["orderId"])
        self.svc.cycle("TESTUSDT")
        prices = self.orders_by_price()
        self.assertEqual(prices[95]["side"], "BUY")
        self.assertEqual(self.ex.amount, 0)
        grid = self.svc.snapshot()["grids"]["TESTUSDT"]
        self.assertEqual((grid["buys"], grid["sells"], grid["status"]), (1, 1, "RUNNING"))

    def test_price_out_of_range_stops_and_closes(self):
        self.start(stop_pct=2)
        buy = self.orders_by_price()[95]
        self.ex.fill(buy["orderId"])
        self.ex.price = 87  # below 90 * 0.98
        self.svc.cycle("TESTUSDT")
        snap = self.svc.snapshot()
        self.assertNotIn("TESTUSDT", snap["grids"])
        self.assertEqual(self.ex.orders, {})
        self.assertEqual(self.ex.amount, 0)
        self.assertIn("ra khỏi range", snap["history"][-1]["stop_reason"])

    def test_max_loss_stops(self):
        self.svc.pnl_callback = lambda s, a, b: {"realized": -7.0, "fee": -0.5, "records": 2}
        self.start(max_loss_usdt=5)
        self.svc.cycle("TESTUSDT")
        self.assertNotIn("TESTUSDT", self.svc.snapshot()["grids"])

    def test_external_position_change_stops_after_confirmations(self):
        self.start()
        self.ex.amount = 5.0  # someone else opened a position
        for _ in range(gc.MISMATCH_CONFIRMATIONS - 1):
            self.svc.cycle("TESTUSDT")
            self.assertIn("TESTUSDT", self.svc.snapshot()["grids"])
        self.svc.cycle("TESTUSDT")
        self.assertNotIn("TESTUSDT", self.svc.snapshot()["grids"])
        self.assertEqual(self.ex.amount, 0)

    def test_paused_places_nothing_new_but_keeps_risk_checks(self):
        self.start()
        buy = self.orders_by_price()[95]
        self.ex.fill(buy["orderId"])
        self.trading = False
        self.svc.cycle("TESTUSDT")
        self.assertNotIn(100, self.orders_by_price())
        self.trading = True
        self.svc.cycle("TESTUSDT")
        self.assertIn(100, self.orders_by_price())

    def test_cancelled_order_is_replaced(self):
        self.start()
        sell = self.orders_by_price()[105]
        self.ex._delete("/fapi/v1/order", {"orderId": sell["orderId"]})
        self.svc.cycle("TESTUSDT")
        self.assertEqual(self.orders_by_price()[105]["side"], "SELL")

    def test_validations(self):
        self.assertFalse(self.start(leverage=10)["ok"])
        self.assertFalse(self.start(lower=101, upper=110)["ok"])  # price outside range
        self.ex.amount = 1
        self.assertFalse(self.start()["ok"])  # symbol not flat
        self.ex.amount = 0
        self.assertFalse(self.start(margin_usdt=60)["ok"])  # > 50% balance
        self.trading = False
        self.assertFalse(self.start()["ok"])

    def test_grid_count_reduced_to_fit_min_notional(self):
        r = self.start(grids=100, margin_usdt=10, leverage=5)  # $50 / ~$5.75 per order
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["reduced"])
        self.assertLessEqual(r["grids"], 8)
        for order in self.ex.orders.values():
            self.assertGreaterEqual(order["qty"] * order["price"], 5.0)
        self.assertLessEqual(sum(o["qty"] * o["price"] for o in self.ex.orders.values()), 50 * 1.25)

    def test_wide_range_reduces_until_resting_notional_fits(self):
        self.ex.price = 12
        r = self.start(lower=10, upper=200, grids=40, kind="GEO", margin_usdt=10, leverage=3)
        if r["ok"]:
            self.assertLessEqual(sum(o["qty"] * o["price"] for o in self.ex.orders.values()), 30 * 1.25)
        else:
            self.assertIn("Vốn không đủ", r["error"])

    def test_too_small_margin_rejected_with_needed_amount(self):
        r = self.start(margin_usdt=1, leverage=1)
        self.assertFalse(r["ok"])
        self.assertIn("cần margin", r["error"])

    def test_manual_stop(self):
        self.start()
        self.assertTrue(self.svc.stop_grid("TESTUSDT")["ok"])
        self.assertEqual(self.ex.orders, {})
        self.assertEqual(self.svc.active_symbols(), set())


if __name__ == "__main__":
    unittest.main()
