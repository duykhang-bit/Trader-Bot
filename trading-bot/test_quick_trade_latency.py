import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from exchange import BinanceFutures
import web_dashboard


def make_response(status, payload):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    response.url = "https://fapi.binance.com/fapi/v1/leverage"
    return response


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected POST")
        return self.responses.pop(0)


class ExchangeRetryTests(unittest.TestCase):
    def make_exchange(self, responses):
        exchange = BinanceFutures("key", "secret", testnet=True)
        exchange.session = SequenceSession(responses)
        return exchange

    def test_post_does_not_retry_deterministic_400(self):
        exchange = self.make_exchange([
            make_response(400, {"code": -2028, "msg": "insufficient margin balance"}),
        ])
        with patch("exchange.time.sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                exchange._post("/fapi/v1/leverage", {"symbol": "XRPUSDT", "leverage": 15})
        self.assertEqual(len(exchange.session.calls), 1)
        sleep.assert_not_called()

    def test_insufficient_margin_fails_leverage_once(self):
        exchange = self.make_exchange([
            make_response(400, {"code": -2028, "msg": "insufficient margin balance"}),
        ])
        with patch("exchange.time.sleep") as sleep:
            actual = exchange.set_leverage("XRPUSDT", 15)
        self.assertIsNone(actual)
        self.assertEqual(len(exchange.session.calls), 1)
        sleep.assert_not_called()

    def test_invalid_leverage_falls_back_once_to_supported_value(self):
        exchange = self.make_exchange([
            make_response(400, {"code": -4028, "msg": "Leverage 15 is not valid"}),
            make_response(200, {"symbol": "XRPUSDT", "leverage": 10}),
        ])
        with patch("exchange.time.sleep") as sleep:
            actual = exchange.set_leverage("XRPUSDT", 15)
        self.assertEqual(actual, 10)
        self.assertEqual(len(exchange.session.calls), 2)
        self.assertEqual(exchange.session.calls[1]["params"]["leverage"], 10)
        sleep.assert_not_called()

    def test_error_url_containing_429_is_not_mistaken_for_rate_limit(self):
        response = make_response(400, {"code": -2028, "msg": "insufficient margin balance"})
        response.url += "?signature=abc429def"
        exchange = self.make_exchange([response])
        with patch("exchange.time.sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                exchange._post("/fapi/v1/leverage", {"symbol": "XRPUSDT", "leverage": 15})
        self.assertEqual(len(exchange.session.calls), 1)
        sleep.assert_not_called()


class QuickTradeFailureExchange:
    def __init__(self):
        self.market_calls = 0
        self.leverage_calls = 0

    def get_ticker_price(self, symbol):
        return 1.5

    def set_leverage(self, symbol, leverage):
        self.leverage_calls += 1
        return None

    def place_market_order(self, symbol, side, qty):
        self.market_calls += 1
        raise AssertionError("market order must not be placed")


class QuickTradeRouteTests(unittest.TestCase):
    def setUp(self):
        self.previous = (
            web_dashboard._exchange,
            web_dashboard._config,
            web_dashboard._state,
            web_dashboard._lock,
        )
        self.exchange = QuickTradeFailureExchange()
        web_dashboard._exchange = self.exchange
        web_dashboard._config = SimpleNamespace(MAX_ORDER_USDT=15, LEVERAGE=15)
        web_dashboard._state = {"trade_log": []}
        web_dashboard._lock = threading.RLock()
        with web_dashboard._quick_trade_guard:
            web_dashboard._quick_trade_inflight.clear()
        web_dashboard.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.client = web_dashboard.app.test_client()
        with self.client.session_transaction() as session:
            session["authenticated"] = True

    def tearDown(self):
        with web_dashboard._quick_trade_guard:
            web_dashboard._quick_trade_inflight.clear()
        (
            web_dashboard._exchange,
            web_dashboard._config,
            web_dashboard._state,
            web_dashboard._lock,
        ) = self.previous

    def test_quick_trade_stops_before_market_when_leverage_fails(self):
        response = self.client.post(
            "/api/quick_trade", json={"symbol": "XRP", "side": "LONG"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("leverage", response.get_json()["msg"])
        self.assertEqual(self.exchange.leverage_calls, 1)
        self.assertEqual(self.exchange.market_calls, 0)
        with web_dashboard._quick_trade_guard:
            self.assertNotIn("XRPUSDT", web_dashboard._quick_trade_inflight)

    def test_second_request_for_same_symbol_fails_fast(self):
        with web_dashboard._quick_trade_guard:
            web_dashboard._quick_trade_inflight.add("XRPUSDT")
        response = self.client.post(
            "/api/quick_trade", json={"symbol": "XRP", "side": "SHORT"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("đang xử lý", response.get_json()["msg"])
        self.assertEqual(self.exchange.leverage_calls, 0)
        self.assertEqual(self.exchange.market_calls, 0)


if __name__ == "__main__":
    unittest.main()
