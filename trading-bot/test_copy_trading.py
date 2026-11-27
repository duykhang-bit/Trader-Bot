"""Unit tests for copy_trading.CopyTradingService mirroring logic (no network)."""
import os
import tempfile
import unittest

import copy_trading as ct


def pos_payload(*rows):
    data = [{"symbol": s, "positionAmount": str(a), "entryPrice": str(e),
             "leverage": lev, "positionSide": side} for s, a, e, lev, side in rows]
    data.append({"symbol": "ZZZUSDT", "positionAmount": "0", "entryPrice": "0",
                 "leverage": 1, "positionSide": "BOTH"})
    return {"code": "000000", "data": data}


class FakeHttp:
    def __init__(self):
        self.positions = pos_payload()
        self.fail = False
        self.position_show = True

    def __call__(self, url, body=None, timeout=20):
        if self.fail:
            raise OSError("network down")
        if url.startswith(ct.DETAIL_URL):
            return {"data": {"nickname": "Leader", "positionShow": self.position_show}}
        if url.startswith(ct.POSITIONS_URL):
            return self.positions
        raise AssertionError(url)


class CopyTradingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.http = FakeHttp()
        self.opened, self.closed = [], []
        self.live = {}
        self.trading = True

        def open_cb(signal):
            self.opened.append(signal)
            self.live[signal["symbol"]] = {"positionAmt": "1" if signal["side"] == "LONG" else "-1",
                                           "markPrice": "1", "unRealizedProfit": "0"}
            return {"ok": True, "entry": 1.0, "qty": 1, "sl": 0.97,
                    "leverage": signal["leverage"], "client_order_id": f"copy_{len(self.opened)}"}

        def close_cb(item):
            self.closed.append(item["symbol"])
            self.live.pop(item["symbol"], None)
            return {"ok": True, "exit": 1.0}

        self.svc = ct.CopyTradingService(
            None, None, open_callback=open_cb, close_callback=close_cb,
            position_callback=lambda s: self.live.get(s),
            is_trading_enabled=lambda: self.trading, http_json=self.http,
            persistence_file=os.path.join(self.tmp, "c.json"))
        self.svc.toggle(True)

    def test_existing_positions_are_not_copied(self):
        self.http.positions = pos_payload(("ETHUSDT", 5, 2000, 20, "LONG"))
        self.assertTrue(self.svc.follow("123", True)["ok"])
        self.svc.poll_once()
        self.assertEqual(self.opened, [])

    def test_new_position_opens_with_capped_leverage_and_closes_after_two_polls(self):
        self.svc.follow("123", True)
        self.http.positions = pos_payload(("SOLUSDT", 10, 70, 25, "BOTH"))
        self.svc.poll_once()
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.opened[0]["side"], "LONG")
        self.assertEqual(self.opened[0]["leverage"], 5)
        self.svc.poll_once()  # still open at leader -> no duplicate
        self.assertEqual(len(self.opened), 1)
        self.http.positions = pos_payload()
        self.svc.poll_once()
        self.assertEqual(self.closed, [])  # needs 2 confirmations
        self.svc.poll_once()
        self.assertEqual(self.closed, ["SOLUSDT"])

    def test_fetch_failure_never_closes(self):
        self.svc.follow("123", True)
        self.http.positions = pos_payload(("SOLUSDT", -10, 70, 3, "BOTH"))
        self.svc.poll_once()
        self.assertEqual(self.opened[0]["side"], "SHORT")
        self.http.fail = True
        for _ in range(5):
            self.svc.poll_once()
        self.assertEqual(self.closed, [])
        self.http.positions = {"code": "999", "data": None}
        self.http.fail = False
        for _ in range(5):
            self.svc.poll_once()
        self.assertEqual(self.closed, [])

    def test_disabled_or_paused_skips_and_does_not_retry_same_position(self):
        self.svc.follow("123", True)
        self.trading = False
        self.http.positions = pos_payload(("SOLUSDT", 10, 70, 3, "LONG"))
        self.svc.poll_once()
        self.trading = True
        self.svc.poll_once()
        self.assertEqual(self.opened, [])

    def test_reopen_after_close_is_copied_again(self):
        self.svc.follow("123", True)
        self.http.positions = pos_payload(("SOLUSDT", 10, 70, 3, "LONG"))
        self.svc.poll_once()
        self.http.positions = pos_payload()
        self.svc.poll_once(); self.svc.poll_once()
        self.http.positions = pos_payload(("SOLUSDT", 10, 71, 3, "LONG"))
        self.svc.poll_once()
        self.assertEqual(len(self.opened), 2)

    def test_sl_hit_on_binance_marks_closed(self):
        self.svc.follow("123", True)
        self.http.positions = pos_payload(("SOLUSDT", 10, 70, 3, "LONG"))
        self.svc.poll_once()
        self.live.pop("SOLUSDT")
        self.svc.poll_once()
        snap = self.svc.snapshot()
        self.assertEqual(snap["copies"], {})
        self.assertEqual(len(snap["closed"]), 1)
        self.assertEqual(len(self.opened), 1)  # leader still open -> not re-entered

    def test_max_copies_limit(self):
        self.svc.update_settings(2, 5, 3, 1)
        self.svc.follow("123", True)
        self.http.positions = pos_payload(("SOLUSDT", 10, 70, 3, "LONG"), ("ETHUSDT", 1, 2000, 3, "LONG"))
        self.svc.poll_once()
        self.assertEqual(len(self.opened), 1)

    def test_hidden_positions_cannot_be_followed(self):
        self.http.position_show = False
        self.assertFalse(self.svc.follow("123", True)["ok"])

    def test_settings_validation(self):
        self.assertFalse(self.svc.update_settings(2, 50, 3, 3)["ok"])
        self.assertFalse(self.svc.update_settings(2, 5, 0, 3)["ok"])
        self.assertTrue(self.svc.update_settings(3, 3, 2, 2)["ok"])


if __name__ == "__main__":
    unittest.main()
