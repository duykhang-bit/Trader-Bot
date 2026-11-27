"""Unit tests for risk_alert (no network)."""
import os
import tempfile
import unittest

import risk_alert as ra


def account(equity=33.0, positions=(), pos_margin=0.0, order_margin=0.0):
    return {
        "totalMarginBalance": str(equity), "availableBalance": "1",
        "totalPositionInitialMargin": str(pos_margin), "totalOpenOrderInitialMargin": str(order_margin),
        "positions": [
            {"symbol": s, "positionAmt": str(a), "entryPrice": str(e), "notional": str(a * e),
             "leverage": str(lev), "initialMargin": str(abs(a * e) / lev), "unrealizedProfit": "0"}
            for s, a, e, lev in positions
        ],
    }


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class FakeNotifier:
    def __init__(self):
        self.telegram = FakeTelegram()


class FakeEx:
    def __init__(self):
        self.acc = account()
        self.algo = []
        self.orders = []

    def _get(self, path, params=None, signed=False):
        return {"/fapi/v2/account": self.acc, "/fapi/v1/openAlgoOrders": self.algo,
                "/fapi/v1/openOrders": self.orders}[path]


class RiskAlertTests(unittest.TestCase):
    def setUp(self):
        self.ex = FakeEx()
        self.n = FakeNotifier()
        self.clock = [1000.0]
        self.svc = ra.RiskAlertService(self.ex, self.n, now_fn=lambda: self.clock[0],
                                       persistence_file=os.path.join(tempfile.mkdtemp(), "r.json"))

    def test_scale_100_percent(self):
        # $33 equity, $330 notional -> 10x -> 10% adverse move wipes the account.
        r = ra.analyze(account(33, [("SOLUSDT", 4.714285, 70, 10)]), [], [])
        p = r["positions"][0]
        self.assertAlmostEqual(p["eff_lev"], 10.0, places=1)
        self.assertAlmostEqual(p["wipe_move_pct"], 10.0, places=1)
        self.assertEqual(r["level_text"], "CỰC CAO") if r["total_eff_lev"] > 10 else self.assertIn(r["level"], (2, 3))

    def test_sl_loss_from_algo_stop(self):
        algo = [{"symbol": "SOLUSDT", "orderType": "STOP_MARKET", "triggerPrice": "68", "reduceOnly": True}]
        r = ra.analyze(account(100, [("SOLUSDT", 1, 70, 5)]), algo, [])
        p = r["positions"][0]
        self.assertEqual(p["sl"], 68.0)
        self.assertAlmostEqual(p["sl_loss"], 2.0)
        self.assertAlmostEqual(p["sl_loss_pct"], 2.0)

    def test_big_position_alerts_once_then_repeats_after_cooldown(self):
        self.ex.acc = account(33, [("SOLUSDT", 2, 70, 10)])  # $140 = 4.2x equity
        self.ex.algo = [{"symbol": "SOLUSDT", "orderType": "STOP_MARKET", "triggerPrice": "69.5", "reduceOnly": True}]
        self.svc.check_once()
        sent = self.n.telegram.sent
        self.assertEqual(len(sent), 1)  # one bundled message, no duplicate account alert
        self.assertIn("424% vốn", sent[0])
        self.assertNotIn("TỔNG TÀI KHOẢN", sent[0])
        self.assertIn("mất 100% vốn khi giá ngược 23.57%", sent[0])
        for _ in range(5):
            self.clock[0] += 20
            self.svc.check_once()
        self.assertEqual(len(sent), 1)
        self.clock[0] += 61 * 60
        self.svc.check_once()
        self.assertEqual(len(sent), 2)

    def test_reason_change_same_level_does_not_resend(self):
        self.ex.acc = account(33, [("SOLUSDT", 2, 70, 10)])
        self.ex.algo = [{"symbol": "SOLUSDT", "orderType": "STOP_MARKET", "triggerPrice": "69.5", "reduceOnly": True}]
        self.svc.check_once()
        self.ex.algo = [{"symbol": "SOLUSDT", "orderType": "STOP_MARKET", "triggerPrice": "70.7", "reduceOnly": True}]
        self.clock[0] += 20
        self.svc.check_once()  # SL moved, loss now 4.2% -> still level CAO
        self.assertEqual(len(self.n.telegram.sent), 1)

    def test_escalation_sends_immediately(self):
        self.ex.acc = account(33, [("SOLUSDT", 2, 70, 10)])
        self.ex.algo = [{"symbol": "SOLUSDT", "orderType": "STOP_MARKET", "triggerPrice": "69.5", "reduceOnly": True}]
        self.svc.check_once()
        self.ex.acc = account(33, [("SOLUSDT", 6, 70, 10)])  # 1272% equity -> CỰC CAO
        self.clock[0] += 20
        self.svc.check_once()
        self.assertEqual(len(self.n.telegram.sent), 2)
        self.assertIn("CỰC CAO", self.n.telegram.sent[1])

    def test_transient_sl_gap_is_ignored(self):
        self.ex.acc = account(100, [("SOLUSDT", 1, 70, 3)])
        stop = [{"symbol": "SOLUSDT", "orderType": "STOP_MARKET", "triggerPrice": "69.5", "reduceOnly": True}]
        self.ex.algo = stop
        self.svc.check_once()
        self.ex.algo = []          # monitor cancels the SL...
        self.clock[0] += 20
        self.svc.check_once()
        self.ex.algo = stop        # ...and places the new one
        self.clock[0] += 20
        self.svc.check_once()
        self.assertEqual(self.n.telegram.sent, [])

    def test_missing_sl_alerts_after_grace(self):
        self.ex.acc = account(100, [("SOLUSDT", 1, 70, 3)])
        self.svc.check_once()
        self.clock[0] += 61
        self.svc.check_once()
        self.assertEqual(self.n.telegram.sent, [])
        self.clock[0] += 30
        self.svc.check_once()
        self.assertEqual(len(self.n.telegram.sent), 1)
        self.assertIn("KHÔNG có SL", self.n.telegram.sent[0])

    def test_account_margin_usage_alert_includes_pending_orders(self):
        self.ex.acc = account(33, [], pos_margin=0, order_margin=32)
        self.svc.check_once()
        self.assertEqual(len(self.n.telegram.sent), 1)
        self.assertIn("TỔNG TÀI KHOẢN", self.n.telegram.sent[0])
        self.assertIn("97% vốn", self.n.telegram.sent[0])

    def test_disabled_sends_nothing(self):
        self.svc.update_settings({"enabled": False})
        self.ex.acc = account(33, [("SOLUSDT", 5, 70, 50)])
        self.svc.check_once()
        self.assertEqual(self.n.telegram.sent, [])

    def test_settings_validation(self):
        self.assertFalse(self.svc.update_settings({"margin_warn_pct": 500})["ok"])
        self.assertTrue(self.svc.update_settings({"pos_lev_warn": 1.5, "leverage_warn": 10})["ok"])


if __name__ == "__main__":
    unittest.main()
