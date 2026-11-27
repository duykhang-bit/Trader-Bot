"""Unit tests for FastMaxLossGuard decision logic (no network)."""
import threading
import time
import types
import unittest

import fast_max_loss as fml


class Ex:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def _get(self, path, params=None, signed=False):
        assert path == "/fapi/v2/positionRisk"
        return self.rows


def acct(sym, amt, entry, up=0.0):
    return {"e": "ACCOUNT_UPDATE", "a": {"P": [{"s": sym, "pa": str(amt), "ep": str(entry), "up": str(up), "ps": "BOTH"}]}}


class FastMaxLossTests(unittest.TestCase):
    def setUp(self):
        self.cfg = types.SimpleNamespace(MAX_LOSS_ENABLED=True, MAX_LOSS_PER_POSITION=5.0)
        self.closes = []
        self.ok = True
        self.done = threading.Event()

        def close(sym, amt, pnl, ml):
            self.closes.append((sym, amt, round(pnl, 2)))
            self.done.set()
            return self.ok

        self.g = fml.FastMaxLossGuard(Ex(), self.cfg, close_callback=close)

    def wait(self):
        self.assertTrue(self.done.wait(2))
        self.done.clear()

    def test_portal_case_closes_at_first_tick_past_limit(self):
        # SHORT 5000 @0.0295: $5 loss at 0.0305.
        self.g.on_user_event(acct("PORTALUSDT", -5000, 0.0295))
        self.g.on_mark_message([{"s": "PORTALUSDT", "p": "0.0300"}])   # -2.5
        self.assertEqual(self.closes, [])
        self.g.on_mark_message([{"s": "PORTALUSDT", "p": "0.03052"}])  # -5.1
        self.wait()
        self.assertEqual(self.closes, [("PORTALUSDT", -5000.0, -5.1)])

    def test_fill_already_beyond_limit_closes_immediately_on_fill(self):
        self.g.on_mark_message([{"s": "PORTALUSDT", "p": "0.0310"}])
        self.g.on_user_event(acct("PORTALUSDT", -5000, 0.0295))
        self.wait()
        self.assertEqual(len(self.closes), 1)

    def test_uses_binance_upnl_when_no_mark_yet(self):
        self.g.on_user_event(acct("XUSDT", 10, 1.0, up=-6))
        self.wait()
        self.assertEqual(self.closes[0][2], -6.0)

    def test_no_double_close_and_rearm_on_size_change(self):
        self.g.on_user_event(acct("XUSDT", 10, 1.0))
        self.g.on_mark_message([{"s": "XUSDT", "p": "0.4"}])
        self.wait()
        self.g.on_mark_message([{"s": "XUSDT", "p": "0.3"}])
        time.sleep(0.1)
        self.assertEqual(len(self.closes), 1)
        self.g.on_user_event(acct("XUSDT", 0, 0))           # flat
        self.g.on_user_event(acct("XUSDT", 20, 1.0))        # new position
        self.wait()  # mark 0.3 already below limit
        self.assertEqual(len(self.closes), 2)

    def test_failed_close_retries(self):
        self.ok = False
        self.g.on_user_event(acct("XUSDT", 10, 1.0))
        self.g.on_mark_message([{"s": "XUSDT", "p": "0.4"}])
        self.wait()
        time.sleep(1.2)
        self.ok = True
        self.g.on_mark_message([{"s": "XUSDT", "p": "0.4"}])
        self.wait()
        self.assertEqual(len(self.closes), 2)

    def test_respects_live_settings_and_skip(self):
        self.cfg.MAX_LOSS_ENABLED = False
        self.g.on_user_event(acct("XUSDT", 10, 1.0))
        self.g.on_mark_message([{"s": "XUSDT", "p": "0.1"}])
        self.cfg.MAX_LOSS_ENABLED = True
        self.cfg.MAX_LOSS_PER_POSITION = 20
        self.g.on_mark_message([{"s": "XUSDT", "p": "0.1"}])   # -9 < 20: keep
        time.sleep(0.1)
        self.assertEqual(self.closes, [])
        self.g.skip_symbol = lambda s: s == "XUSDT"
        self.cfg.MAX_LOSS_PER_POSITION = 1
        self.g.on_mark_message([{"s": "XUSDT", "p": "0.1"}])
        time.sleep(0.1)
        self.assertEqual(self.closes, [])

    def test_seed_positions_from_rest(self):
        self.g.exchange = Ex([{"symbol": "AUSDT", "positionAmt": "-2", "entryPrice": "10"}])
        self.g.seed_positions()
        self.g.on_mark_message([{"s": "AUSDT", "p": "12.6"}])  # -5.2
        self.wait()
        self.assertEqual(self.closes[0][0], "AUSDT")


if __name__ == "__main__":
    unittest.main()
