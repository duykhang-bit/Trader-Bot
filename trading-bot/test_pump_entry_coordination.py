import unittest

from pump_entry_coordinator import (
    BearishBosResult,
    PumpEntryCoordinator,
    PumpEntryState,
    decide_short_entry,
    detect_bearish_bos_1m,
)


def candle(close_time, high, low, close, open_=None):
    return {
        "close_time": close_time,
        "open": close if open_ is None else open_,
        "high": high,
        "low": low,
        "close": close,
    }


def bearish_structure(last_close=99.0, last_low=98.5):
    # peak(110) -> structure low(100) -> lower high(108) -> close BOS < 100
    return [
        candle(1_000, 110, 104, 108),
        candle(2_000, 106, 100, 102),
        candle(3_000, 108, 102, 105),
        candle(4_000, 105, 99.5, 101),
        candle(5_000, 104, 98.8, 100.5),
        candle(6_000, 103, last_low, last_close),
    ]


class BearishBosTests(unittest.TestCase):
    def test_closed_candle_peak_lower_high_and_bos(self):
        result = detect_bearish_bos_1m(
            bearish_structure(), now_ms=7_000, lookback=30
        )
        self.assertTrue(result.confirmed)
        self.assertEqual(result.peak_price, 110)
        self.assertEqual(result.lower_high, 108)
        self.assertEqual(result.broken_level, 100)
        self.assertEqual(result.bos_close_time, 6_000)

    def test_wick_break_without_close_is_not_bos(self):
        result = detect_bearish_bos_1m(
            bearish_structure(last_close=100.5, last_low=98.0),
            now_ms=7_000,
        )
        self.assertFalse(result.confirmed)
        self.assertEqual(result.reason, "no_close_below_structure_low")

    def test_open_candle_is_ignored(self):
        rows = bearish_structure(last_close=100.5, last_low=98.0)
        rows.append(candle(9_000, 101, 95, 96))  # still open at now_ms
        result = detect_bearish_bos_1m(rows, now_ms=8_000)
        self.assertFalse(result.confirmed)
        self.assertEqual(result.bos_close_time, 6_000)

    def test_new_high_invalidates_structure(self):
        rows = bearish_structure()
        rows[4] = candle(5_000, 111, 100, 105)
        result = detect_bearish_bos_1m(rows, now_ms=7_000)
        self.assertFalse(result.confirmed)


class ShortEntryDecisionTests(unittest.TestCase):
    def test_price_below_planned_entry_places_non_marketable_sell_limit(self):
        decision = decide_short_entry(current_price=98, planned_entry=100)
        self.assertEqual(decision.action, "LIMIT")
        self.assertEqual(decision.price, 100)
        self.assertGreater(decision.price, 98)

    def test_market_only_when_price_is_near_entry(self):
        decision = decide_short_entry(current_price=100.1, planned_entry=100)
        self.assertEqual(decision.action, "MARKET")

    def test_price_too_far_above_entry_is_rejected(self):
        decision = decide_short_entry(current_price=101, planned_entry=100)
        self.assertEqual(decision.action, "REJECT")
        self.assertEqual(decision.reason, "price_too_far_above_entry")

    def test_retest_too_far_below_is_rejected(self):
        decision = decide_short_entry(
            current_price=90, planned_entry=100, max_retest_distance_pct=5
        )
        self.assertEqual(decision.action, "REJECT")


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.coordinator = PumpEntryCoordinator(
            reservation_ttl_sec=5,
            watch_ttl_sec=30,
            clock=lambda: self.now,
        )
        self.bos = BearishBosResult(
            True, "bearish_bos_confirmed", peak_price=110,
            lower_high=108, broken_level=100, bos_close=99,
            bos_close_time=6_000,
        )

    def test_state_transition_and_single_reservation(self):
        state = self.coordinator.observe_pump("AAAUSDT", 110, "ws")
        self.assertEqual(state, PumpEntryState.WATCHING_TOP)
        self.assertTrue(self.coordinator.confirm_bos("AAAUSDT", self.bos))
        self.assertEqual(
            self.coordinator.snapshot("AAAUSDT"), PumpEntryState.BOS_CONFIRMED
        )
        first = self.coordinator.reserve("AAAUSDT", "ws")
        self.assertIsNotNone(first)
        self.assertIsNone(self.coordinator.reserve("AAAUSDT", "pump_detector"))
        self.assertTrue(
            self.coordinator.placement_succeeded(first, in_trade=True)
        )
        self.assertEqual(
            self.coordinator.snapshot("AAAUSDT"), PumpEntryState.IN_TRADE
        )

    def test_new_high_resets_bos_confirmation(self):
        self.coordinator.observe_pump("AAAUSDT", 110, "ws")
        self.coordinator.confirm_bos("AAAUSDT", self.bos)
        state = self.coordinator.observe_pump("AAAUSDT", 111, "confirmed_top")
        self.assertEqual(state, PumpEntryState.WATCHING_TOP)

    def test_failed_placement_releases_reservation(self):
        self.coordinator.observe_pump("AAAUSDT", 110, "ws")
        self.coordinator.confirm_bos("AAAUSDT", self.bos)
        reservation = self.coordinator.reserve("AAAUSDT", "ws")
        self.assertTrue(self.coordinator.placement_failed(reservation))
        self.assertEqual(
            self.coordinator.snapshot("AAAUSDT"), PumpEntryState.WATCHING_TOP
        )

    def test_stale_reservation_expires_without_deadlock(self):
        self.coordinator.observe_pump("AAAUSDT", 110, "ws")
        self.coordinator.confirm_bos("AAAUSDT", self.bos)
        self.assertIsNotNone(self.coordinator.reserve("AAAUSDT", "ws"))
        self.now += 6
        self.assertEqual(
            self.coordinator.snapshot("AAAUSDT"), PumpEntryState.WATCHING_TOP
        )

    def test_closed_trade_moves_through_cooldown(self):
        self.coordinator.observe_pump("AAAUSDT", 110, "ws")
        self.coordinator.confirm_bos("AAAUSDT", self.bos)
        reservation = self.coordinator.reserve("AAAUSDT", "ws")
        self.coordinator.placement_succeeded(reservation, in_trade=True)
        self.coordinator.mark_flat("AAAUSDT", cooldown_sec=7)
        self.assertEqual(
            self.coordinator.snapshot("AAAUSDT"), PumpEntryState.COOLDOWN
        )
        self.now += 8
        self.assertEqual(
            self.coordinator.snapshot("AAAUSDT"), PumpEntryState.IDLE
        )

    def test_acknowledged_limit_owned_until_terminal_reconciliation(self):
        self.coordinator.observe_pump("AAAUSDT", 110, "pump_detector")
        self.coordinator.confirm_bos("AAAUSDT", self.bos)
        reservation = self.coordinator.reserve("AAAUSDT", "pump_detector")
        self.coordinator.placement_succeeded(reservation, in_trade=False)
        self.now += 600
        self.assertEqual(
            self.coordinator.snapshot("AAAUSDT"), PumpEntryState.ENTRY_PENDING
        )
        self.coordinator.release_pending("AAAUSDT")
        state = self.coordinator.observe_pump("AAAUSDT", 111, "new_high")
        self.assertEqual(state, PumpEntryState.WATCHING_TOP)


class _Telegram:
    def __init__(self):
        self.messages = []

    def send(self, message, **kwargs):
        self.messages.append(message)


class _Notifier:
    def __init__(self):
        self.telegram = _Telegram()


class PumpExchangeIntegrationTests(unittest.TestCase):
    def setUp(self):
        import bot
        self.bot = bot
        self.bot._pump_entry_coordinator = PumpEntryCoordinator(
            reservation_ttl_sec=20, watch_ttl_sec=180
        )
        with self.bot.lock:
            self.bot.state["pump_limit_orders"] = {}

    def _prepared(self, symbol="AAAUSDT"):
        coordinator = self.bot._pump_entry_coordinator
        coordinator.observe_pump(symbol, 110, "pump_detector")
        bos = BearishBosResult(
            True, "bearish_bos_confirmed", peak_price=110,
            lower_high=108, broken_level=100, bos_close=99,
            bos_close_time=6_000,
        )
        self.assertTrue(coordinator.confirm_bos(symbol, bos))
        reservation = coordinator.reserve(symbol, "pump_detector")
        decision = decide_short_entry(current_price=99, planned_entry=100)
        return reservation, decision, bos

    def test_post_only_sell_rounds_up_and_remains_above_fresh_bid(self):
        from exchange import BinanceFutures

        exchange = BinanceFutures("key", "secret")
        exchange._tick_cache["AAAUSDT"] = 0.1
        submitted = {}

        def fake_get(endpoint, params=None, signed=False, retries=3):
            self.assertEqual(endpoint, "/fapi/v1/ticker/bookTicker")
            return {"bidPrice": "100.05", "askPrice": "100.15"}

        def fake_post(endpoint, params=None, retries=3):
            submitted.update(params)
            return {"orderId": 7, "status": "NEW"}

        exchange._get = fake_get
        exchange._post = fake_post
        result = exchange.place_limit_order(
            "AAAUSDT", "SELL", 1, 100.01,
            post_only=True, client_order_id="pump_retest_test",
        )

        self.assertEqual(submitted["timeInForce"], "GTX")
        self.assertEqual(submitted["newClientOrderId"], "pump_retest_test")
        self.assertEqual(float(submitted["price"]), 100.1)
        self.assertGreater(float(submitted["price"]), 100.05)
        self.assertEqual(float(result["price"]), 100.1)

    def test_accepted_after_timeout_is_reconciled_and_owned(self):
        bot = self.bot

        class FakeExchange:
            def _get(fake, endpoint, params=None, signed=False, **kwargs):
                if endpoint == "/fapi/v2/positionRisk":
                    return [{"symbol": "AAAUSDT", "positionAmt": "0"}]
                if endpoint == "/fapi/v1/openOrders":
                    return []
                if endpoint == "/fapi/v1/order":
                    return {
                        "symbol": "AAAUSDT", "orderId": 42, "status": "NEW",
                        "price": "100.1", "executedQty": "0",
                        "origClientOrderId": params["origClientOrderId"],
                    }
                raise AssertionError(endpoint)

            def place_limit_order(fake, symbol, side, qty, price, **kwargs):
                self.assertTrue(kwargs["post_only"])
                self.assertTrue(kwargs["client_order_id"].startswith("pump_retest_"))
                raise TimeoutError("accepted, response lost")

        prepared = self._prepared()
        result = bot._place_reserved_pump_order(
            FakeExchange(), prepared, "AAAUSDT", 2,
            order_type="LIMIT", price=100.01,
            limit_metadata={"cycle_peak": 110, "sl_price": 113, "tp1_price": 90},
        )

        self.assertEqual(result["orderId"], 42)
        info = bot.state["pump_limit_orders"]["AAAUSDT"]
        self.assertEqual(info["order_id"], 42)
        self.assertTrue(info["client_order_id"].startswith("pump_retest_"))
        self.assertEqual(
            bot._pump_entry_coordinator.snapshot("AAAUSDT"),
            PumpEntryState.ENTRY_PENDING,
        )

    def test_startup_cancels_only_owned_limit_and_flattens_partial_fill(self):
        bot = self.bot
        notifier = _Notifier()

        class FakeExchange:
            def __init__(fake):
                fake.position = -0.4
                fake.cancelled = []

            def _get(fake, endpoint, params=None, signed=False, **kwargs):
                if endpoint == "/fapi/v1/openOrders":
                    return [
                        {"symbol": "AAAUSDT", "orderId": 1, "type": "LIMIT",
                         "reduceOnly": False, "clientOrderId": "pump_retest_deadbeef"},
                        {"symbol": "BBBUSDT", "orderId": 2, "type": "LIMIT",
                         "reduceOnly": False, "clientOrderId": "manual_order"},
                        {"symbol": "CCCUSDT", "orderId": 3, "type": "LIMIT",
                         "reduceOnly": False, "clientOrderId": "scan_limit"},
                    ]
                if endpoint == "/fapi/v2/positionRisk":
                    symbol = (params or {}).get("symbol", "AAAUSDT")
                    return [{"symbol": symbol, "positionAmt": str(fake.position)}]
                raise AssertionError(endpoint)

            def _delete(fake, endpoint, params=None, **kwargs):
                fake.cancelled.append(params["orderId"])
                return {"status": "CANCELED", "executedQty": "0.4"}

            def place_market_order(fake, symbol, side, qty, reduce_only=False, **kwargs):
                self.assertTrue(reduce_only)
                fake.position = 0
                return {"status": "FILLED"}

        exchange = FakeExchange()
        from unittest.mock import patch
        with patch.object(bot.time, "sleep", return_value=None):
            count = bot._cleanup_pump_limits_on_startup(exchange, notifier)

        self.assertEqual(count, 1)
        self.assertEqual(exchange.cancelled, [1])
        self.assertEqual(exchange.position, 0)
        self.assertEqual(len(notifier.telegram.messages), 1)

    def test_fast_spike_producer_routes_through_real_bos_coordinator(self):
        bot = self.bot
        from types import SimpleNamespace
        from unittest.mock import patch
        now = int(__import__("time").time() * 1000)
        rows = bearish_structure()
        klines = []
        for index, row in enumerate(rows):
            klines.append([
                now - (len(rows) - index + 1) * 60_000,
                row["open"], row["high"], row["low"], row["close"], 10,
                now - (len(rows) - index) * 60_000,
                0, 0, 0, 0, 0,
            ])

        class FakeExchange:
            def __init__(fake):
                fake.position = 0
                fake.market_orders = []

            def set_leverage(fake, symbol, leverage):
                return leverage

            def get_ticker_price(fake, symbol):
                return 99.0

            def get_klines(fake, symbol, interval, limit=30):
                return rows if False else klines

            def _get(fake, endpoint, params=None, signed=False, **kwargs):
                if endpoint == "/fapi/v2/positionRisk":
                    return [{"symbol": "AAAUSDT", "positionAmt": str(fake.position)}]
                if endpoint == "/fapi/v1/openOrders":
                    return []
                raise AssertionError(endpoint)

            def place_market_order(fake, symbol, side, qty, **kwargs):
                fake.market_orders.append((symbol, side, kwargs.get("client_order_id")))
                fake.position = -qty
                return {"orderId": 9, "status": "FILLED"}

            def place_stop_loss_order(fake, *args, **kwargs):
                return {"status": "NEW"}

        signal = SimpleNamespace(
            entry_price=99.0, sl_price=113.0, tp1_price=90.0,
            pump_pct=20.0, score=90,
        )
        exchange = FakeExchange()
        with patch("qty_utils.calc_qty_precise", return_value=(1.0, None)), \
             patch.object(bot.time, "sleep", return_value=None), \
             patch.object(bot, "_append_trade", return_value=None):
            bot._execute_spike_short(
                "AAAUSDT", signal, exchange, _Notifier()
            )

        self.assertEqual(len(exchange.market_orders), 1)
        self.assertTrue(exchange.market_orders[0][2].startswith("pump_market_"))
        self.assertEqual(
            bot._pump_entry_coordinator.snapshot("AAAUSDT"),
            PumpEntryState.IN_TRADE,
        )
    def test_post_only_reject_releases_without_market_fallback(self):
        bot = self.bot

        class FakeExchange:
            market_calls = 0

            def _get(fake, endpoint, params=None, signed=False, **kwargs):
                if endpoint == "/fapi/v2/positionRisk":
                    return [{"symbol": "AAAUSDT", "positionAmt": "0"}]
                if endpoint == "/fapi/v1/openOrders":
                    return []
                raise AssertionError(endpoint)

            def place_limit_order(fake, *args, **kwargs):
                raise ValueError("post-only SELL AAAUSDT is marketable")

            def place_market_order(fake, *args, **kwargs):
                fake.market_calls += 1
                raise AssertionError("must not fall back to MARKET")

        prepared = self._prepared()
        exchange = FakeExchange()
        result = bot._place_reserved_pump_order(
            exchange, prepared, "AAAUSDT", 1,
            order_type="LIMIT", price=100,
            limit_metadata={"sl_price": 113, "tp1_price": 90},
        )
        self.assertFalse(result)
        self.assertEqual(exchange.market_calls, 0)
        self.assertNotIn("AAAUSDT", bot.state["pump_limit_orders"])
        self.assertEqual(
            bot._pump_entry_coordinator.snapshot("AAAUSDT"),
            PumpEntryState.WATCHING_TOP,
        )

    def test_market_timeout_checks_live_position_before_release(self):
        bot = self.bot

        class FakeExchange:
            def __init__(fake):
                fake.position = 0

            def _get(fake, endpoint, params=None, signed=False, **kwargs):
                if endpoint == "/fapi/v2/positionRisk":
                    return [{"symbol": "AAAUSDT", "positionAmt": str(fake.position)}]
                if endpoint == "/fapi/v1/openOrders":
                    return []
                if endpoint == "/fapi/v1/order":
                    raise RuntimeError("-2013 Order does not exist")
                raise AssertionError(endpoint)

            def place_market_order(fake, *args, **kwargs):
                fake.position = -1
                raise TimeoutError("response lost after fill")

        prepared = self._prepared()
        result = bot._place_reserved_pump_order(
            FakeExchange(), prepared, "AAAUSDT", 1, order_type="MARKET"
        )
        self.assertTrue(result)
        self.assertEqual(
            bot._pump_entry_coordinator.snapshot("AAAUSDT"),
            PumpEntryState.IN_TRADE,
        )

    def _seed_monitored_limit(self):
        bot = self.bot
        reservation, _decision, _bos = self._prepared()
        self.assertTrue(
            bot._pump_entry_coordinator.placement_succeeded(
                reservation, in_trade=False
            )
        )
        info = {
            "entry_price": 100.0,
            "sl_price": 113.0,
            "tp1_price": 90.0,
            "qty": 1.0,
            "order_id": 77,
            "client_order_id": "pump_retest_bounded",
            "ts": __import__("time").time(),
        }
        with bot.lock:
            bot.state["pump_limit_orders"]["AAAUSDT"] = info
        return info

    def test_limit_monitor_partial_fill_cancels_exact_remainder_and_protects_live_qty(self):
        bot = self.bot
        notifier = _Notifier()
        info = self._seed_monitored_limit()

        class FakeExchange:
            def __init__(fake):
                fake.position = -0.4
                fake.cancelled = []
                fake.sl_calls = []
                fake.tp_calls = []

            def _delete(fake, endpoint, params=None, **kwargs):
                self.assertEqual(endpoint, "/fapi/v1/order")
                fake.cancelled.append(dict(params))
                fake.position = -0.6  # last fill races cancellation
                return {"status": "CANCELED", "executedQty": "0.6"}

            def _get(fake, endpoint, params=None, signed=False, **kwargs):
                if endpoint == "/fapi/v2/positionRisk":
                    return [{
                        "symbol": "AAAUSDT", "positionAmt": str(fake.position),
                        "entryPrice": "100", "markPrice": "101",
                    }]
                raise AssertionError(endpoint)

            def place_stop_loss_order(fake, symbol, side, qty, price):
                fake.sl_calls.append((symbol, side, qty, price))
                return {"status": "NEW"}

            def place_take_profit_order(fake, symbol, side, qty, price):
                fake.tp_calls.append((symbol, side, qty, price))
                return {"status": "NEW"}

        exchange = FakeExchange()
        from unittest.mock import patch
        with patch.object(bot.time, "sleep", return_value=None), \
             patch.object(bot, "_append_trade", return_value=None):
            bot._protect_pump_limit_fill_once(
                exchange, notifier, "AAAUSDT", info
            )

        self.assertEqual(exchange.cancelled, [{
            "symbol": "AAAUSDT", "orderId": 77,
        }])
        self.assertEqual(exchange.sl_calls, [("AAAUSDT", "BUY", 0.6, 113.0)])
        self.assertEqual(exchange.tp_calls, [("AAAUSDT", "BUY", 0.6, 90.0)])
        self.assertNotIn("AAAUSDT", bot.state["pump_limit_orders"])
        self.assertEqual(
            bot._pump_entry_coordinator.snapshot("AAAUSDT"),
            PumpEntryState.IN_TRADE,
        )

    def test_limit_monitor_unconfirmed_cancel_emergency_flattens(self):
        bot = self.bot
        notifier = _Notifier()
        info = self._seed_monitored_limit()

        class FakeExchange:
            def __init__(fake):
                fake.position = -0.4
                fake.market_calls = []
                fake.sl_calls = 0

            def _delete(fake, endpoint, params=None, **kwargs):
                raise TimeoutError("cancel response lost")

            def _get(fake, endpoint, params=None, signed=False, **kwargs):
                if endpoint == "/fapi/v1/order":
                    return {"status": "PARTIALLY_FILLED", "executedQty": "0.4"}
                if endpoint == "/fapi/v2/positionRisk":
                    return [{"symbol": "AAAUSDT", "positionAmt": str(fake.position)}]
                raise AssertionError(endpoint)

            def place_market_order(fake, symbol, side, qty, reduce_only=False, **kwargs):
                self.assertTrue(reduce_only)
                fake.market_calls.append((symbol, side, qty))
                fake.position = 0
                return {"status": "FILLED"}

            def place_stop_loss_order(fake, *args, **kwargs):
                fake.sl_calls += 1
                raise AssertionError("must flatten before placing protection")

        exchange = FakeExchange()
        from unittest.mock import patch
        with patch.object(bot.time, "sleep", return_value=None):
            bot._protect_pump_limit_fill_once(
                exchange, notifier, "AAAUSDT", info
            )

        self.assertEqual(exchange.market_calls, [("AAAUSDT", "BUY", 0.4)])
        self.assertEqual(exchange.sl_calls, 0)
        self.assertEqual(exchange.position, 0)
        self.assertNotIn("AAAUSDT", bot.state["pump_limit_orders"])

    def test_limit_monitor_sl_failure_emergency_flattens_without_tp(self):
        bot = self.bot
        notifier = _Notifier()
        info = self._seed_monitored_limit()

        class FakeExchange:
            def __init__(fake):
                fake.position = -0.6
                fake.sl_calls = 0
                fake.tp_calls = 0
                fake.market_calls = []

            def _delete(fake, endpoint, params=None, **kwargs):
                return {"status": "CANCELED", "executedQty": "0.6"}

            def _get(fake, endpoint, params=None, signed=False, **kwargs):
                if endpoint == "/fapi/v2/positionRisk":
                    return [{
                        "symbol": "AAAUSDT", "positionAmt": str(fake.position),
                        "entryPrice": "100", "markPrice": "101",
                    }]
                raise AssertionError(endpoint)

            def place_stop_loss_order(fake, *args, **kwargs):
                fake.sl_calls += 1
                raise RuntimeError("temporary exchange failure")

            def place_take_profit_order(fake, *args, **kwargs):
                fake.tp_calls += 1
                raise AssertionError("TP must not be placed without SL")

            def place_market_order(fake, symbol, side, qty, reduce_only=False, **kwargs):
                self.assertTrue(reduce_only)
                fake.market_calls.append((symbol, side, qty))
                fake.position = 0
                return {"status": "FILLED"}

        exchange = FakeExchange()
        from unittest.mock import patch
        with patch.object(bot.time, "sleep", return_value=None):
            bot._protect_pump_limit_fill_once(
                exchange, notifier, "AAAUSDT", info
            )

        self.assertEqual(exchange.sl_calls, 3)
        self.assertEqual(exchange.tp_calls, 0)
        self.assertEqual(exchange.market_calls, [("AAAUSDT", "BUY", 0.6)])
        self.assertEqual(exchange.position, 0)
        self.assertNotIn("AAAUSDT", bot.state["pump_limit_orders"])

    @classmethod
    def tearDownClass(cls):
        # atexit owns normal shutdown; explicit cleanup keeps test imports clean.
        import bot
        bot._release_single_instance()


class PumpDetectorReversalGateTests(unittest.TestCase):
    def test_reversal_is_required_even_for_near_top_limit(self):
        import pandas as pd
        import pump_detector as module
        from unittest.mock import patch

        frame = pd.DataFrame({
            "open": [90.0] * 30,
            "high": [100.0] * 30,
            "low": [80.0] * 30,
            "close": [99.5] * 30,
            "volume": [10.0] * 30,
        })
        detector = module.PumpDetector({
            "PUMP_PRICE_RISE_PCT": 15,
            "PUMP_TOP_MIN_SCORE": 60,
            "PUMP_SIGNAL_COOLDOWN_S": 0,
        })
        detector._detect_pump_move = lambda df: (25.0, 80.0, 100.0)
        detector._score_pump_top = lambda *args: (100, [])
        detector._confirm_reversal = lambda *args: False

        with patch.object(module, "calculate_atr", return_value=pd.Series([1.0] * 30)), \
             patch.object(module, "calculate_rsi", return_value=pd.Series([80.0] * 30)):
            signal = detector.analyze("AAAUSDT", frame)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.entry_type, "LIMIT")
        self.assertFalse(signal.is_pump_top)


if __name__ == "__main__":
    unittest.main()
