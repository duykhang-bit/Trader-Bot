"""Regression: a failed order-list read must not trigger duplicate SL/TP."""
import unittest

import auto_sltp


class FakeEx:
    def __init__(self, algo=None, algo_error=False, orders_error=False):
        self.algo = algo or []
        self.algo_error = algo_error
        self.orders_error = orders_error

    def _get(self, path, params=None, signed=False):
        if path == "/fapi/v2/positionRisk":
            return [{"symbol": "XAUUSDT", "positionAmt": "-0.015", "entryPrice": "3658", "markPrice": "3650"}]
        if path == "/fapi/v1/openOrders":
            if self.orders_error:
                raise RuntimeError("400 Bad Request")
            return []
        if path == "/fapi/v1/openAlgoOrders":
            if self.algo_error:
                raise RuntimeError("400 Bad Request")
            return self.algo
        raise AssertionError(path)


SL_TP = [{"symbol": "XAUUSDT", "orderType": "STOP_MARKET", "triggerPrice": "3895"},
         {"symbol": "XAUUSDT", "orderType": "TAKE_PROFIT_MARKET", "triggerPrice": "3567"}]


class AutoSltpDetectionTests(unittest.TestCase):
    def test_algo_read_failure_is_not_unprotected(self):
        self.assertEqual(auto_sltp.get_positions_without_sltp(FakeEx(algo_error=True)), [])

    def test_orders_read_failure_is_not_unprotected(self):
        self.assertEqual(auto_sltp.get_positions_without_sltp(FakeEx(orders_error=True)), [])

    def test_existing_sl_tp_detected(self):
        self.assertEqual(auto_sltp.get_positions_without_sltp(FakeEx(algo=SL_TP)), [])

    def test_really_missing_sl_tp_still_detected(self):
        res = auto_sltp.get_positions_without_sltp(FakeEx(algo=[]))
        self.assertEqual([p["symbol"] for p in res], ["XAUUSDT"])


if __name__ == "__main__":
    unittest.main()
