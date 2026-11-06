"""Persistent liquidation-zone LIMIT entry order service.

``recovery_callback`` is called as ``callback(symbol, source="AutoLiqZone")``
when a filled entry cannot be given a stop loss after three attempts.
``trade_callback``, when supplied, receives one JSON-safe event dictionary after
an entry is filled and its protection has been reconciled.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from indicators import calculate_atr
from liq_heatmap_api import calc_liq_zones_from_oi
from scanner import _klines_to_df

logger = logging.getLogger(__name__)

PERSISTENCE_VERSION = 1
PERSISTENCE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "auto_liq_zone_orders.json"
)
SOURCE_LABEL = "Binance OI estimate (12h, 5m×144; khung giá 12h như Coinglass)"
SOURCE_WARNING = "Vùng ước tính, không phải dữ liệu thanh lý thật của Coinglass."
WINDOW_LABEL = "12h"
# Strict 12h candle frame; any positive estimated liquidity remains eligible.
FRAME_PAD_PCT = 0.0
LIQUIDITY_THRESHOLD = 0.0
CLIENT_ID_PREFIX = "alqz_"
VALID_INTERVALS = {300, 900, 3600}
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
ACTIVE_STATUSES = {"NEW", "PARTIALLY_FILLED", "PENDING_NEW", "UNKNOWN"}
TERMINAL_STATUSES = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_reduce_only(order: dict) -> bool:
    value = order.get("reduceOnly", False)
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)


def _client_id(order: dict) -> str:
    return str(
        order.get("clientOrderId")
        or order.get("origClientOrderId")
        or order.get("clientAlgoId")
        or ""
    )


def _order_type(order: dict) -> str:
    return str(
        order.get("type")
        or order.get("orderType")
        or order.get("algoType")
        or ""
    ).upper()


def _public_item(item: dict) -> dict:
    result = copy.deepcopy(item)
    result.pop("_deleted", None)
    return result


class AutoLiqZoneOrderService:
    """Manage durable LIMIT entries at estimated liquidation-zone buckets.

    The worker continues exact reconciliation while trading is paused. New
    calculations, placements, and reprices require both an enabled item and a
    truthy ``is_trading_enabled()`` callback.
    """

    def __init__(
        self,
        config,
        exchange,
        notifier,
        *,
        entry_placer,
        recovery_callback,
        is_trading_enabled,
        trade_callback=None,
        now_fn=time.time,
        sleep_fn=time.sleep,
    ):
        self.config = config
        self.exchange = exchange
        self.notifier = notifier
        self.entry_placer = entry_placer
        self.recovery_callback = recovery_callback
        self.is_trading_enabled = is_trading_enabled
        self.trade_callback = trade_callback
        self._now = now_fn
        self._sleep = sleep_fn

        self._lock = threading.RLock()
        self._operation_locks_guard = threading.Lock()
        self._operation_locks: Dict[str, threading.RLock] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._items: Dict[str, dict] = {}
        self._snapshot_errors: list[str] = []
        self._startup_done = False
        self._load()

    @staticmethod
    def is_owned_client_id(value) -> bool:
        return isinstance(value, str) and value.startswith(CLIENT_ID_PREFIX)

    def start(self):
        """Start one idempotent daemon worker."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._worker,
                name="auto-liq-zone-orders",
                daemon=True,
            )
            self._thread.start()
            return self._thread

    def stop(self, timeout=5):
        """Wake and join the worker for at most ``timeout`` seconds."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
        return thread is None or not thread.is_alive()

    def snapshot(self) -> dict:
        """Return a detached, JSON-safe dashboard snapshot."""
        trading_enabled = bool(self.is_trading_enabled())
        with self._lock:
            data = {
                "enabled": trading_enabled,
                "source": SOURCE_LABEL,
                "warning": SOURCE_WARNING,
                "window": WINDOW_LABEL,
                "frame_pad_pct": FRAME_PAD_PCT,
                "liquidity_threshold": LIQUIDITY_THRESHOLD,
                "selection": "farthest_nonzero_zone_within_strict_12h_frame",
                "items": [_public_item(item) for item in self._items.values()],
                "errors": list(self._snapshot_errors),
            }
        # A round trip also rejects accidental non-JSON runtime values.
        return json.loads(json.dumps(copy.deepcopy(data), ensure_ascii=False))

    def add(self, symbol, side, refresh_interval) -> dict:
        try:
            normalized = self._normalize_symbol(symbol)
            normalized_side = self._validate_side(side)
            interval = self._validate_interval(refresh_interval)
            with self._lock:
                if any(item["symbol"] == normalized for item in self._items.values()):
                    raise ValueError(f"duplicate symbol: {normalized}")
            price = float(self.exchange.get_ticker_price(normalized))
            if not math.isfinite(price) or price <= 0:
                raise ValueError(f"invalid ticker price for {normalized}")

            now = self._now()
            item_id = uuid.uuid4().hex
            item = self._new_item(
                item_id, normalized, normalized_side, interval, now
            )
            with self._lock:
                if any(existing["symbol"] == normalized for existing in self._items.values()):
                    raise ValueError(f"duplicate symbol: {normalized}")
                self._items[item_id] = item
                self._persist_locked()
            self._wake.set()
            return {"ok": True, "item": _public_item(item)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def update(self, item_id, side=None, refresh_interval=None) -> dict:
        lock = self._operation_lock(str(item_id))
        with lock:
            try:
                with self._lock:
                    item = self._require_item_locked(item_id)
                    old_side = item["side"]
                    new_side = old_side if side is None else self._validate_side(side)
                    new_interval = (
                        item["refresh_interval"]
                        if refresh_interval is None
                        else self._validate_interval(refresh_interval)
                    )
                    if (
                        new_side != old_side
                        and item["order"].get("protection_started")
                    ):
                        raise ValueError("side cannot change after fill protection begins")
                if new_side != old_side and self._has_pending_intent(item):
                    result = self._cancel_and_reconcile(item, "update")
                    if not result["ok"]:
                        return result
                    if result.get("exposure"):
                        return {
                            "ok": False,
                            "error": "side cannot change after the entry gained exposure",
                            "item": _public_item(item),
                        }
                with self._lock:
                    live = self._require_item_locked(item_id)
                    live["side"] = new_side
                    live["refresh_interval"] = new_interval
                    live["next_update"] = 0.0
                    live["status"] = "DUE" if live["enabled"] else "DISABLED"
                    live["error"] = ""
                    self._persist_locked()
                    result_item = _public_item(live)
                self._wake.set()
                return {"ok": True, "item": result_item}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    def toggle(self, item_id, enabled) -> dict:
        lock = self._operation_lock(str(item_id))
        with lock:
            try:
                enabled = bool(enabled)
                with self._lock:
                    item = self._require_item_locked(item_id)
                    item["enabled"] = enabled
                    item["status"] = "DUE" if enabled else "DISABLING"
                    item["next_update"] = 0.0
                    self._persist_locked()
                if not enabled and self._has_pending_intent(item):
                    result = self._cancel_and_reconcile(item, "disable")
                    if not result["ok"]:
                        return result
                if (
                    not enabled
                    and item["order"].get("protection_started")
                    and not item["order"].get("protected")
                    and not item["order"].get("finished")
                ):
                    self._protect_fill(item)
                    if not item["order"].get("protected"):
                        return {
                            "ok": False,
                            "error": item.get("error") or "fill protection is incomplete",
                            "item": _public_item(item),
                        }
                with self._lock:
                    live = self._require_item_locked(item_id)
                    if live["order"].get("protected"):
                        live["status"] = "PROTECTED"
                    elif live["order"].get("finished"):
                        live["status"] = "RECOVERED_FLAT"
                    elif live["order"].get("protection_started"):
                        live["status"] = "ERROR"
                    else:
                        live["status"] = "DUE" if enabled else "DISABLED"
                    self._persist_locked()
                    result_item = _public_item(live)
                self._wake.set()
                return {"ok": True, "item": result_item}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    def delete(self, item_id) -> dict:
        item_id = str(item_id)
        lock = self._operation_lock(item_id)
        with lock:
            try:
                with self._lock:
                    item = self._require_item_locked(item_id)
                    item["enabled"] = False
                    item["_deleted"] = True
                    item["status"] = "DELETING"
                    self._persist_locked()
                if self._has_pending_intent(item):
                    result = self._cancel_and_reconcile(item, "delete")
                    if not result["ok"]:
                        with self._lock:
                            item["_deleted"] = False
                            item["status"] = "ERROR"
                            item["error"] = result["error"]
                            self._persist_locked()
                        return result
                if (
                    item["order"].get("protection_started")
                    and not item["order"].get("protected")
                    and not item["order"].get("finished")
                ):
                    self._protect_fill(item)
                    if not item["order"].get("protected"):
                        with self._lock:
                            item["_deleted"] = False
                            self._persist_locked()
                        return {
                            "ok": False,
                            "error": item.get("error") or "fill protection is incomplete",
                        }
                with self._lock:
                    self._items.pop(item_id, None)
                    self._persist_locked()
                return {"ok": True, "deleted": item_id}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

    def force_refresh(self, item_id=None) -> dict:
        try:
            with self._lock:
                if item_id is None:
                    item_ids = list(self._items)
                else:
                    item_ids = [self._require_item_locked(item_id)["id"]]
            refreshed = []
            for selected_id in item_ids:
                with self._operation_lock(selected_id):
                    with self._lock:
                        item = self._items.get(selected_id)
                        if item is None:
                            continue
                        if not item.get("order", {}).get("protection_started"):
                            item["next_update"] = 0.0
                            item["status"] = (
                                "DUE" if item["enabled"] else "DISABLED"
                            )
                        refreshed.append(selected_id)
                        self._persist_locked()
            self._wake.set()
            return {"ok": True, "item_ids": refreshed}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _new_item(self, item_id, symbol, side, interval, now) -> dict:
        return {
            "id": item_id,
            "symbol": symbol,
            "side": side,
            "refresh_interval": interval,
            "enabled": True,
            "status": "DUE",
            "mark": None,
            "frame_low": None,
            "frame_high": None,
            "strongest_above": None,
            "strongest_below": None,
            "target_price": None,
            "target_usd": None,
            "last_updated": None,
            "next_update": 0.0,
            "error": "",
            "order": self._empty_order(),
            "sl": None,
            "tp": None,
            "created_at": now,
        }

    @staticmethod
    def _empty_order() -> dict:
        return {
            "order_id": None,
            "client_order_id": "",
            "status": "",
            "qty": None,
            "price": None,
            "executed_qty": 0.0,
            "intent_persisted": False,
            "protection_started": False,
            "protected": False,
            "finished": False,
            "history_recorded": False,
            "notified": False,
            "delivery_next": 0.0,
            "notification_attempts": 0,
            "fill_qty": None,
            "fill_price": None,
            "sl_order": None,
            "tp_order": None,
        }

    def _operation_lock(self, item_id: str) -> threading.RLock:
        with self._operation_locks_guard:
            return self._operation_locks.setdefault(item_id, threading.RLock())

    def _require_item_locked(self, item_id) -> dict:
        item = self._items.get(str(item_id))
        if item is None:
            raise KeyError(f"unknown item: {item_id}")
        return item

    @staticmethod
    def _normalize_symbol(symbol) -> str:
        value = str(symbol or "").strip().upper()
        if value and not value.endswith("USDT"):
            value += "USDT"
        if not SYMBOL_RE.fullmatch(value):
            raise ValueError("symbol must match [A-Z0-9]{2,20}USDT")
        return value

    @staticmethod
    def _validate_side(side) -> str:
        value = str(side or "").strip().upper()
        if value not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")
        return value

    @staticmethod
    def _validate_interval(interval) -> int:
        try:
            value = int(interval)
        except (TypeError, ValueError) as exc:
            raise ValueError("refresh_interval must be 300, 900, or 3600") from exc
        if value not in VALID_INTERVALS:
            raise ValueError("refresh_interval must be 300, 900, or 3600")
        return value

    def _load(self) -> None:
        if not os.path.exists(PERSISTENCE_FILE):
            return
        try:
            with open(PERSISTENCE_FILE, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("version") != PERSISTENCE_VERSION:
                raise ValueError(
                    f"unsupported persistence version: {payload.get('version')}"
                )
            loaded: Dict[str, dict] = {}
            symbols = set()
            for raw in payload.get("items", []):
                item = self._coerce_loaded_item(raw)
                if item["symbol"] in symbols:
                    self._snapshot_errors.append(
                        f"duplicate persisted symbol left unloaded: {item['symbol']}"
                    )
                    continue
                symbols.add(item["symbol"])
                loaded[item["id"]] = item
            self._items = loaded
            self._snapshot_errors.extend(
                str(value) for value in payload.get("errors", []) if value
            )
        except Exception as exc:
            logger.error("Failed to load %s: %s", PERSISTENCE_FILE, exc)
            self._snapshot_errors.append(f"persistence load failed: {exc}")

    def _coerce_loaded_item(self, raw: dict) -> dict:
        item_id = str(raw.get("id") or uuid.uuid4().hex)
        item = self._new_item(
            item_id,
            self._normalize_symbol(raw.get("symbol")),
            self._validate_side(raw.get("side")),
            self._validate_interval(raw.get("refresh_interval")),
            _float(raw.get("created_at")) or self._now(),
        )
        for key in (
            "enabled", "status", "mark", "frame_low", "frame_high",
            "strongest_above", "strongest_below",
            "target_price", "target_usd", "last_updated", "next_update", "error",
            "sl", "tp",
        ):
            if key in raw:
                item[key] = copy.deepcopy(raw[key])
        item["enabled"] = bool(item["enabled"])
        order = self._empty_order()
        if isinstance(raw.get("order"), dict):
            for key in order:
                if key in raw["order"]:
                    order[key] = copy.deepcopy(raw["order"][key])
        item["order"] = order
        return item

    def _persist_locked(self) -> None:
        payload = {
            "version": PERSISTENCE_VERSION,
            "items": [_public_item(item) for item in self._items.values()],
            "errors": list(self._snapshot_errors),
        }
        temporary = PERSISTENCE_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, PERSISTENCE_FILE)

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                startup_done = self._startup_done
            if not startup_done:
                try:
                    self._startup_reconcile()
                except Exception as exc:
                    self._record_snapshot_error(f"startup reconciliation failed: {exc}")
                    self._wake.wait(30.0)
                    self._wake.clear()
                    continue
            with self._lock:
                item_ids = list(self._items)
            for item_id in item_ids:
                if self._stop.is_set():
                    break
                lock = self._operation_lock(item_id)
                if not lock.acquire(blocking=False):
                    continue
                try:
                    with self._lock:
                        item = self._items.get(item_id)
                    if item is not None:
                        self._process_item(item)
                except Exception as exc:
                    logger.exception("Auto liquidation-zone item %s failed", item_id)
                    self._set_item_error(item_id, str(exc), backoff=True)
                finally:
                    lock.release()
            self._wake.wait(5.0)
            self._wake.clear()

    def _process_item(self, item: dict) -> None:
        order = item.get("order", {})
        if self._has_pending_intent(item):
            reconciliation = self._query_owned_order(item)
            if reconciliation is not None:
                self._apply_order_state(item, reconciliation)
                order = item["order"]
                if self._has_exposure(reconciliation):
                    self._cancel_partial_remainder(item, reconciliation)
                    self._protect_fill(item)
                    return
                status = str(reconciliation.get("status", "")).upper()
                if status in TERMINAL_STATUSES:
                    self._clear_terminal_order(item, status)
                    order = item["order"]

        if order.get("protection_started"):
            if not order.get("protected") and not order.get("finished"):
                self._protect_fill(item)
            elif order.get("protected") and (
                not order.get("history_recorded") or not order.get("notified")
            ) and self._now() >= _float(order.get("delivery_next")):
                self._deliver_fill_event(item)
            return
        if not item.get("enabled") or not bool(self.is_trading_enabled()):
            return
        if self._now() < _float(item.get("next_update")):
            return
        self._calculate_and_reprice(item)

    def _calculate_and_reprice(self, item: dict) -> None:
        with self._lock:
            item["status"] = "CALCULATING"
            item["error"] = ""
            self._persist_locked()
        symbol = item["symbol"]
        mark = float(self.exchange.get_mark_price(symbol))
        if not math.isfinite(mark) or mark <= 0:
            raise ValueError(f"invalid mark price for {symbol}")
        zones = calc_liq_zones_from_oi(symbol, period="5m", lookback=144)
        # Strict 12h range. Keep every positive estimated-liquidity bucket:
        # SHORT takes the last/highest bucket before the 12h high; LONG takes
        # the last/lowest bucket before the 12h low. No strength cutoff and no
        # frame/entry percentage is added.
        frame_low, frame_high = self._chart_frame(symbol, mark)
        above = [(float(price), float(usd)) for price, usd in zones.items()
                 if mark < float(price) <= frame_high and float(usd) > 0]
        below = [(float(price), float(usd)) for price, usd in zones.items()
                 if frame_low <= float(price) < mark and float(usd) > 0]
        highest_above = max(above, key=lambda value: value[0]) if above else None
        lowest_below = min(below, key=lambda value: value[0]) if below else None
        target_zone = lowest_below if item["side"] == "LONG" else highest_above
        now = self._now()

        no_target = target_zone is None
        with self._lock:
            item["mark"] = mark
            item["frame_low"] = frame_low
            item["frame_high"] = frame_high
            item["strongest_above"] = self._zone_view(highest_above, mark)
            item["strongest_below"] = self._zone_view(lowest_below, mark)
            item["last_updated"] = now
            item["next_update"] = now + item["refresh_interval"]
            if no_target:
                item["target_price"] = None
                item["target_usd"] = None
                item["status"] = "ERROR"
                item["error"] = f"no liquidation zone {item['side'].lower()} target side"
                self._persist_locked()
        if no_target:
            if self._has_pending_intent(item):
                result = self._cancel_and_reconcile(item, "missing-zone")
                if result["ok"] and not result.get("exposure"):
                    with self._lock:
                        item["status"] = "ERROR"
                        item["error"] = (
                            f"no liquidation zone {item['side'].lower()} target side"
                        )
                        item["next_update"] = now + item["refresh_interval"]
                        self._persist_locked()
            return

        rounded_target = float(self.exchange._round_price(target_zone[0], symbol))
        if not math.isfinite(rounded_target) or rounded_target <= 0:
            raise ValueError(f"invalid tick-rounded target for {symbol}")
        with self._lock:
            item["target_price"] = rounded_target
            item["target_usd"] = target_zone[1]
            item["error"] = ""
            self._persist_locked()

        order = item["order"]
        active_price = _float(order.get("price"))
        if self._has_pending_intent(item) and active_price == rounded_target:
            with self._lock:
                item["status"] = "PENDING"
                self._persist_locked()
            return
        if self._has_pending_intent(item):
            result = self._cancel_and_reconcile(item, "reprice")
            if not result["ok"] or result.get("exposure"):
                return
        self._place_entry(item, rounded_target)

    def _chart_frame(self, symbol: str, mark: float) -> tuple[float, float]:
        """Return the exact 12h (5m×144) candle low/high, without padding."""
        frame = _klines_to_df(self.exchange.get_klines(symbol, "5m", limit=144))
        high = float(frame["high"].max())
        low = float(frame["low"].min())
        if not (math.isfinite(high) and math.isfinite(low)) or low <= 0 or high < low:
            raise ValueError(f"invalid 12h price frame for {symbol}")
        return low, high

    @staticmethod
    def _zone_view(zone, mark):
        if zone is None:
            return None
        price, usd = zone
        return {
            "price": price,
            "usd": usd,
            "distance": abs(price - mark) / mark * 100.0,
        }

    def _place_entry(self, item: dict, target: float) -> None:
        symbol = item["symbol"]
        leverage = int(getattr(self.config, "LEVERAGE"))
        max_order_usdt = float(getattr(self.config, "MAX_ORDER_USDT"))
        if leverage <= 0 or max_order_usdt <= 0:
            raise ValueError("LEVERAGE and MAX_ORDER_USDT must be positive")
        effective_leverage = int(self.exchange.set_leverage(symbol, leverage) or 0)
        if effective_leverage <= 0:
            raise ValueError(f"cannot confirm effective leverage for {symbol}")
        step, max_qty, decimals, min_notional = self.exchange.get_qty_precision(symbol)
        step = float(step)
        max_qty = float(max_qty)
        raw_qty = (max_order_usdt * effective_leverage) / target
        if step <= 0 or max_qty <= 0:
            raise ValueError(f"invalid quantity filters for {symbol}")
        capped_qty = min(raw_qty, max_qty)
        qty = math.floor(capped_qty / step + 1e-12) * step
        qty = round(qty, int(decimals))
        if qty <= 0:
            raise ValueError(f"quantity rounds to zero for {symbol}")
        if qty * target < float(min_notional):
            raise ValueError(
                f"order notional {qty * target:.8g} below minimum {min_notional}"
            )

        sl, tp = self._calculate_sl_tp(symbol, item["side"], target)
        client_id = self._new_client_id(item["id"])
        side = "BUY" if item["side"] == "LONG" else "SELL"
        with self._lock:
            item["sl"] = sl
            item["tp"] = tp
            item["order"] = self._empty_order()
            item["order"].update({
                "client_order_id": client_id,
                "status": "INTENT",
                "qty": qty,
                "price": target,
                "intent_persisted": True,
            })
            item["status"] = "SUBMITTING"
            item["error"] = ""
            self._persist_locked()

        try:
            result = self.entry_placer(
                self.exchange,
                symbol,
                side,
                qty,
                order_type="LIMIT",
                price=target,
                client_order_id=client_id,
                post_only=False,
            )
        except Exception as placement_exc:
            try:
                result = self._query_order(symbol, None, client_id)
            except Exception as query_exc:
                if self._is_missing_order(query_exc):
                    self._clear_failed_intent(
                        item, f"placement failed: {placement_exc}"
                    )
                else:
                    self._set_item_error(
                        item["id"],
                        f"placement outcome inconclusive: {placement_exc}; {query_exc}",
                        status="UNKNOWN",
                    )
                return

        if result is False or result is None:
            with self._lock:
                item["order"] = self._empty_order()
                item["status"] = "BLOCKED"
                item["error"] = "entry blocked by live position, pending order, or capacity"
                self._persist_locked()
            return
        if not isinstance(result, dict):
            self._set_item_error(item["id"], "entry placer returned invalid response")
            return
        with self._lock:
            item["order"]["order_id"] = result.get("orderId")
            item["order"]["client_order_id"] = (
                _client_id(result) or client_id
            )
            item["order"]["status"] = str(result.get("status") or "NEW").upper()
            item["order"]["executed_qty"] = _float(result.get("executedQty"))
            item["status"] = "PENDING"
            self._persist_locked()
        if self._has_exposure(result):
            self._cancel_partial_remainder(item, result)
            self._protect_fill(item)

    def _calculate_sl_tp(self, symbol, side, entry) -> tuple[float, float]:
        klines = self.exchange.get_klines(symbol, "15m", limit=20)
        frame = _klines_to_df(klines)
        atr = float(calculate_atr(
            frame["high"], frame["low"], frame["close"], 14
        ).iloc[-1])
        if not math.isfinite(atr) or atr <= 0:
            raise ValueError(f"invalid ATR14 for {symbol}")
        if side == "LONG":
            sl = entry - max(2.0 * atr, 0.02 * entry)
            tp = entry + 8.0 * atr
        else:
            sl = entry + max(2.0 * atr, 0.02 * entry)
            tp = entry - 8.0 * atr
        return round(sl, 8), round(tp, 8)

    def _new_client_id(self, item_id: str) -> str:
        stamp = int(self._now() * 1000)
        suffix = f"{item_id[:8]}_{stamp:x}_{uuid.uuid4().hex[:6]}"
        return (CLIENT_ID_PREFIX + suffix)[:36]

    def _query_order(self, symbol, order_id, client_id):
        params = {"symbol": symbol}
        if order_id is not None and str(order_id) != "":
            params["orderId"] = order_id
        elif client_id:
            params["origClientOrderId"] = client_id
        else:
            raise ValueError("owned order has neither orderId nor client ID")
        return self.exchange._get("/fapi/v1/order", params, signed=True)

    def _query_owned_order(self, item: dict):
        order = item["order"]
        try:
            return self._query_order(
                item["symbol"], order.get("order_id"), order.get("client_order_id")
            )
        except Exception as exc:
            if self._is_missing_order(exc):
                rows = self.exchange._get(
                    "/fapi/v2/positionRisk",
                    {"symbol": item["symbol"]},
                    signed=True,
                )
                if isinstance(rows, dict):
                    rows = [rows]
                live_amount = sum(
                    _float(row.get("positionAmt"))
                    for row in rows or []
                    if row.get("symbol") == item["symbol"]
                )
                if live_amount:
                    direction = "LONG" if live_amount > 0 else "SHORT"
                    if direction != item["side"]:
                        raise RuntimeError(
                            f"missing owned order but live position direction "
                            f"{direction} does not match {item['side']}"
                        ) from exc
                    return {
                        "status": "FILLED",
                        "executedQty": abs(live_amount),
                    }
                return {
                    "status": "EXPIRED",
                    "executedQty": order.get("executed_qty", 0),
                }
            raise

    def _cancel_exact(self, item: dict):
        order = item["order"]
        params = {"symbol": item["symbol"]}
        if order.get("order_id") is not None:
            params["orderId"] = order["order_id"]
        elif order.get("client_order_id"):
            params["origClientOrderId"] = order["client_order_id"]
        else:
            raise ValueError("cannot exactly cancel order without an identifier")
        return self.exchange._delete("/fapi/v1/order", params)

    def _cancel_and_reconcile(self, item: dict, reason: str) -> dict:
        try:
            response = self._cancel_exact(item)
        except Exception as cancel_exc:
            try:
                response = self._query_owned_order(item)
            except Exception as query_exc:
                error = (
                    f"{reason} reconciliation inconclusive: cancel={cancel_exc}; "
                    f"query={query_exc}"
                )
                self._set_item_error(item["id"], error)
                return {"ok": False, "error": error}
        self._apply_order_state(item, response or {})
        if self._has_exposure(response or {}):
            try:
                self._cancel_partial_remainder(item, response or {})
                self._protect_fill(item)
            except Exception as exc:
                error = f"{reason} fill protection failed: {exc}"
                self._set_item_error(item["id"], error)
                return {"ok": False, "error": error, "exposure": True}
            if not item["order"].get("protected"):
                error = item.get("error") or f"{reason} fill protection is incomplete"
                return {"ok": False, "error": error, "exposure": True}
            return {"ok": True, "exposure": True}

        status = str((response or {}).get("status", "")).upper()
        if status not in TERMINAL_STATUSES:
            try:
                final = self._query_owned_order(item)
            except Exception as exc:
                error = f"{reason} reconciliation inconclusive: {exc}"
                self._set_item_error(item["id"], error)
                return {"ok": False, "error": error}
            self._apply_order_state(item, final)
            if self._has_exposure(final):
                try:
                    self._cancel_partial_remainder(item, final)
                    self._protect_fill(item)
                except Exception as exc:
                    error = f"{reason} fill protection failed: {exc}"
                    self._set_item_error(item["id"], error)
                    return {"ok": False, "error": error, "exposure": True}
                if not item["order"].get("protected"):
                    error = item.get("error") or f"{reason} fill protection is incomplete"
                    return {"ok": False, "error": error, "exposure": True}
                return {"ok": True, "exposure": True}
            status = str(final.get("status", "")).upper()
        if status not in TERMINAL_STATUSES:
            error = f"{reason} reconciliation inconclusive: status={status or 'UNKNOWN'}"
            self._set_item_error(item["id"], error)
            return {"ok": False, "error": error}
        self._clear_terminal_order(item, status)
        return {"ok": True, "exposure": False}

    def _cancel_partial_remainder(self, item: dict, state: dict) -> None:
        status = str(state.get("status", "")).upper()
        executed = _float(state.get("executedQty"))
        if executed <= 0 or status == "FILLED" or status in TERMINAL_STATUSES:
            return
        try:
            terminal = self._cancel_exact(item)
        except Exception:
            terminal = self._query_owned_order(item)
        self._apply_order_state(item, terminal or {})
        terminal_status = str((terminal or {}).get("status", "")).upper()
        if terminal_status not in TERMINAL_STATUSES and terminal_status != "FILLED":
            terminal = self._query_owned_order(item)
            self._apply_order_state(item, terminal)
            terminal_status = str(terminal.get("status", "")).upper()
        if terminal_status not in TERMINAL_STATUSES and terminal_status != "FILLED":
            raise RuntimeError(
                f"partial entry remainder is not terminal: {terminal_status or 'UNKNOWN'}"
            )

    @staticmethod
    def _has_exposure(order: dict) -> bool:
        return _float(order.get("executedQty")) > 0 or str(
            order.get("status", "")
        ).upper() == "FILLED"

    @staticmethod
    def _has_pending_intent(item: dict) -> bool:
        order = item.get("order", {})
        if order.get("protection_started"):
            return False
        return bool(
            order.get("intent_persisted")
            and (order.get("order_id") is not None or order.get("client_order_id"))
        )

    def _apply_order_state(self, item: dict, state: dict) -> None:
        with self._lock:
            order = item["order"]
            if state.get("orderId") is not None:
                order["order_id"] = state.get("orderId")
            if _client_id(state):
                order["client_order_id"] = _client_id(state)
            if state.get("status"):
                order["status"] = str(state["status"]).upper()
            exchange_price = _float(state.get("price"))
            if exchange_price > 0 and exchange_price != _float(order.get("price")):
                # User/Binance may amend an owned LIMIT. Re-plan protection from
                # the authoritative price before a later fill can be protected.
                order["price"] = exchange_price
                if not order.get("protection_started"):
                    item["sl"] = None
                    item["tp"] = None
            exchange_qty = _float(state.get("origQty"))
            if exchange_qty > 0:
                order["qty"] = exchange_qty
            order["executed_qty"] = max(
                _float(order.get("executed_qty")), _float(state.get("executedQty"))
            )
            self._persist_locked()

    def _clear_terminal_order(self, item: dict, status: str) -> None:
        with self._lock:
            item["order"] = self._empty_order()
            item["status"] = "DUE" if item.get("enabled") else "DISABLED"
            item["error"] = ""
            item["next_update"] = 0.0
            self._persist_locked()

    def _clear_failed_intent(self, item: dict, error: str) -> None:
        with self._lock:
            item["order"] = self._empty_order()
            item["status"] = "ERROR"
            item["error"] = error
            self._persist_locked()

    def _live_matching_position(self, item: dict) -> tuple[float, float]:
        rows = self.exchange._get(
            "/fapi/v2/positionRisk", {"symbol": item["symbol"]}, signed=True
        )
        if isinstance(rows, dict):
            rows = [rows]
        matching = []
        for row in rows or []:
            if row.get("symbol") != item["symbol"]:
                continue
            amount = _float(row.get("positionAmt"))
            if amount == 0:
                continue
            direction = "LONG" if amount > 0 else "SHORT"
            if direction == item["side"]:
                matching.append((abs(amount), _float(row.get("entryPrice"))))
            else:
                raise RuntimeError(
                    f"live position direction {direction} does not match {item['side']}"
                )
        if not matching:
            raise RuntimeError("entry reports exposure but no matching live position exists")
        return sum(value[0] for value in matching), matching[0][1]

    def _recover_filled_position(self, item: dict, error: str) -> None:
        """Run fail-safe flatten once; a confirmed flat state is terminal."""
        self._set_item_error(item["id"], error)
        recovered = False
        try:
            recovered = bool(self.recovery_callback(item["symbol"], source="AutoLiqZone"))
        except Exception as exc:
            self._set_item_error(item["id"], f"{error}; recovery callback failed: {exc}")
            return
        if recovered:
            with self._lock:
                item["order"]["finished"] = True
                item["status"] = "RECOVERED_FLAT"
                item["error"] = f"{error}; position closed by fail-safe"
                self._persist_locked()

    def _protect_fill(self, item: dict) -> None:
        with self._lock:
            item["order"]["protection_started"] = True
            item["status"] = "PROTECTING"
            self._persist_locked()
        if item.get("sl") is None or item.get("tp") is None:
            try:
                entry_price = _float(item["order"].get("price"))
                if entry_price <= 0:
                    raise ValueError("owned order has no entry price for SL/TP")
                sl, tp = self._calculate_sl_tp(
                    item["symbol"], item["side"], entry_price
                )
                with self._lock:
                    item["sl"] = sl
                    item["tp"] = tp
                    self._persist_locked()
            except Exception as exc:
                error = f"cannot calculate persisted SL/TP for filled entry: {exc}"
                self._recover_filled_position(item, error)
                return
        try:
            quantity, live_entry = self._live_matching_position(item)
        except Exception as exc:
            error = f"cannot protect filled entry from live position: {exc}"
            self._recover_filled_position(item, error)
            return
        close_side = "SELL" if item["side"] == "LONG" else "BUY"
        try:
            regular, algo = self._open_exit_orders(item["symbol"])
        except Exception as exc:
            error = f"cannot inspect existing fill protection: {exc}"
            self._recover_filled_position(item, error)
            return
        stop_present = self._has_matching_exit(
            regular + algo, "STOP", close_side, quantity, float(item["sl"]), item["symbol"]
        )
        tp_present = self._has_matching_exit(
            regular + algo, "TAKE_PROFIT", close_side, quantity, float(item["tp"]), item["symbol"]
        )

        stop_result = None
        if not stop_present:
            for attempt in range(1, 4):
                try:
                    stop_result = self.exchange.place_stop_loss_order(
                        item["symbol"], close_side, quantity, float(item["sl"])
                    )
                    stop_present = bool(stop_result)
                except Exception as exc:
                    logger.warning(
                        "AutoLiqZone SL %s attempt %s/3 failed: %s",
                        item["symbol"], attempt, exc,
                    )
                regular, algo = self._open_exit_orders(item["symbol"])
                stop_present = stop_present or self._has_matching_exit(
                    regular + algo, "STOP", close_side, quantity,
                    float(item["sl"]), item["symbol"]
                )
                if stop_present:
                    break
                if attempt < 3:
                    self._sleep(0.25)
        if not stop_present:
            error = "stop loss missing after 3 placement attempts"
            self._recover_filled_position(item, error)
            return

        tp_result = None
        if not tp_present:
            tp_result = self.exchange.place_take_profit_order(
                item["symbol"], close_side, quantity, float(item["tp"])
            )
            tp_present = bool(tp_result)
            regular, algo = self._open_exit_orders(item["symbol"])
            tp_present = tp_present or self._has_matching_exit(
                regular + algo, "TAKE_PROFIT", close_side, quantity,
                float(item["tp"]), item["symbol"]
            )

        with self._lock:
            protected = bool(stop_present and tp_present)
            item["order"]["sl_order"] = copy.deepcopy(stop_result)
            item["order"]["tp_order"] = copy.deepcopy(tp_result)
            item["order"]["protected"] = protected
            item["order"]["finished"] = protected
            item["order"]["fill_qty"] = quantity
            item["order"]["fill_price"] = live_entry or item["order"].get("price")
            item["status"] = "PROTECTED" if protected else "ERROR"
            item["error"] = "" if protected else "take profit is not confirmed open"
            self._persist_locked()
        if protected:
            self._deliver_fill_event(item)

    def _fill_event(self, item: dict) -> dict:
        order = item["order"]
        return {
            "event_id": order.get("client_order_id") or str(order.get("order_id") or item["id"]),
            "source": "AutoLiqZone", "symbol": item["symbol"], "side": item["side"],
            "quantity": _float(order.get("fill_qty") or order.get("executed_qty")),
            "entry_price": _float(order.get("fill_price") or order.get("price")),
            "sl": item["sl"], "tp": item["tp"], "protected": True,
            "timestamp": self._now(),
        }

    def _deliver_fill_event(self, item: dict) -> None:
        """Durably deliver history then Telegram; retry either missing step."""
        with self._lock:
            order = item["order"]
            order["delivery_next"] = self._now() + 60
            self._persist_locked()
            event = self._fill_event(item)
            history_recorded = bool(order.get("history_recorded"))
            notified = bool(order.get("notified"))
        if not history_recorded:
            try:
                if self.trade_callback is not None:
                    self.trade_callback(copy.deepcopy(event))
                with self._lock:
                    item["order"]["history_recorded"] = True
                    self._persist_locked()
            except Exception:
                logger.exception("AutoLiqZone history callback failed for %s", item["symbol"])
                # Telegram is an independent durable delivery channel; do not
                # suppress it while history persistence waits for its retry.
        if not notified:
            try:
                message_id = self.notifier.telegram.send(
                    "💧 <b>AUTO LIQ ZONE FILLED</b>\n"
                    f"🪙 {item['symbol']} {item['side']}\n"
                    f"Entry: <b>${event['entry_price']:.8g}</b>\n"
                    f"SL: <b>${float(item['sl']):.8g}</b> · TP: <b>${float(item['tp']):.8g}</b>"
                ) if self.notifier is not None else 1
                if not message_id:
                    raise RuntimeError("Telegram returned no message ID")
                with self._lock:
                    item["order"]["notified"] = True
                    item["order"]["notification_attempts"] = int(
                        item["order"].get("notification_attempts", 0)
                    ) + 1
                    if str(item.get("error") or "").startswith("Telegram fill notification"):
                        item["error"] = ""
                    self._persist_locked()
            except Exception as exc:
                with self._lock:
                    item["order"]["notification_attempts"] = int(
                        item["order"].get("notification_attempts", 0)
                    ) + 1
                    item["error"] = f"Telegram fill notification pending retry: {exc}"
                    self._persist_locked()
                logger.warning("AutoLiqZone Telegram retry pending for %s: %s", item["symbol"], exc)

    def _open_exit_orders(self, symbol: str) -> tuple[list, list]:
        regular = self.exchange._get(
            "/fapi/v1/openOrders", {"symbol": symbol}, signed=True
        )
        algo_all = self.exchange._get("/fapi/v1/openAlgoOrders", signed=True)
        if not isinstance(regular, list):
            regular = []
        if not isinstance(algo_all, list):
            algo_all = []
        regular = [order for order in regular if order.get("symbol") == symbol
                   and _is_reduce_only(order)]
        algo = [order for order in algo_all if order.get("symbol") == symbol
                and _is_reduce_only(order)]
        return regular, algo

    def _has_matching_exit(self, orders: list, prefix: str, close_side: str,
                           quantity: float, trigger: float, symbol: str) -> bool:
        """Accept only an exit that covers this fill at this planned trigger."""
        prefix = prefix.upper()
        expected_trigger = float(self.exchange._round_price(trigger, symbol))
        for order in orders:
            if not _order_type(order).startswith(prefix):
                continue
            if str(order.get("side") or "").upper() != close_side.upper():
                continue
            order_qty = _float(order.get("origQty") or order.get("quantity"))
            if order_qty <= 0 or abs(order_qty - quantity) > max(1e-9, quantity * 1e-6):
                continue
            actual_trigger = _float(order.get("triggerPrice") or order.get("stopPrice"))
            if actual_trigger <= 0:
                continue
            rounded_actual = float(self.exchange._round_price(actual_trigger, symbol))
            if rounded_actual == expected_trigger:
                return True
        return False

    def _retire_untracked_owned(self, order: dict, reason: str) -> None:
        """Cancel an unjournaled alqz_ entry; flatten any raced exposure."""
        symbol = str(order.get("symbol") or "")
        order_id = order.get("orderId")
        client_id = _client_id(order)
        if not symbol or (order_id is None and not client_id):
            raise RuntimeError(f"{reason}: owned order lacks exact identity")
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        else:
            params["origClientOrderId"] = client_id
        try:
            result = self.exchange._delete("/fapi/v1/order", params)
        except Exception:
            result = self.exchange._get("/fapi/v1/order", params, signed=True)
        status = str((result or {}).get("status") or "").upper()
        exposed = self._has_exposure(result or {}) or self._has_exposure(order)
        if status not in TERMINAL_STATUSES and status != "FILLED":
            result = self.exchange._get("/fapi/v1/order", params, signed=True)
            status = str((result or {}).get("status") or "").upper()
            exposed = exposed or self._has_exposure(result or {})
        if status not in TERMINAL_STATUSES and status != "FILLED":
            raise RuntimeError(f"{reason}: exact cancel unconfirmed ({symbol}/{client_id}, {status})")
        if exposed:
            if not self.recovery_callback(symbol, source="AutoLiqZoneStartup"):
                raise RuntimeError(f"{reason}: raced fill could not be flattened ({symbol}/{client_id})")
        logger.warning("AutoLiqZone retired untracked owned order %s/%s (%s)", symbol, client_id, reason)

    def _startup_reconcile(self) -> None:
        with self._lock:
            if self._startup_done:
                return
        orders = self.exchange._get("/fapi/v1/openOrders", signed=True)
        if not isinstance(orders, list):
            raise RuntimeError("openOrders returned a non-list response")
        owned = [order for order in orders if self.is_owned_client_id(_client_id(order))]
        by_symbol: Dict[str, list] = {}
        for order in owned:
            by_symbol.setdefault(str(order.get("symbol", "")), []).append(order)

        with self._lock:
            items = list(self._items.values())
        for item in items:
            candidates = by_symbol.pop(item["symbol"], [])
            persisted_id = item.get("order", {}).get("client_order_id", "")
            if persisted_id:
                extras = [order for order in candidates
                          if _client_id(order) != persisted_id]
                if extras:
                    for extra in extras:
                        self._retire_untracked_owned(extra, f"duplicate for {item['symbol']}")
                continue
            if len(candidates) == 1:
                order = candidates[0]
                adopted_price = _float(order.get("price"))
                adopted_sl = adopted_tp = None
                if adopted_price > 0:
                    try:
                        adopted_sl, adopted_tp = self._calculate_sl_tp(
                            item["symbol"], item["side"], adopted_price
                        )
                    except Exception as exc:
                        self._record_snapshot_error(
                            f"adopted order SL/TP calculation failed for "
                            f"{item['symbol']}: {exc}"
                        )
                with self._lock:
                    item["order"] = self._empty_order()
                    item["order"].update({
                        "order_id": order.get("orderId"),
                        "client_order_id": _client_id(order),
                        "status": str(order.get("status") or "NEW").upper(),
                        "qty": _float(order.get("origQty")),
                        "price": adopted_price,
                        "executed_qty": _float(order.get("executedQty")),
                        "intent_persisted": True,
                    })
                    item["sl"] = adopted_sl
                    item["tp"] = adopted_tp
                    item["status"] = "ADOPTED"
                    self._persist_locked()
            elif len(candidates) > 1:
                for duplicate in candidates:
                    self._retire_untracked_owned(
                        duplicate, f"multiple unjournaled orders for {item['symbol']}"
                    )
                with self._lock:
                    item["order"] = self._empty_order()
                    item["status"] = "DUE" if item.get("enabled") else "DISABLED"
                    item["next_update"] = 0.0
                    self._persist_locked()
        for symbol, candidates in by_symbol.items():
            for order in candidates:
                self._retire_untracked_owned(order, f"no configured item for {symbol}")
        with self._lock:
            self._startup_done = True
            self._persist_locked()

    def _record_snapshot_error(self, error: str) -> None:
        with self._lock:
            if error not in self._snapshot_errors:
                self._snapshot_errors.append(error)
                self._persist_locked()

    def _set_item_error(self, item_id, error: str, status="ERROR", backoff=False) -> None:
        with self._lock:
            item = self._items.get(str(item_id))
            if item is not None:
                item["status"] = status
                item["error"] = str(error)
                if backoff and not self._has_pending_intent(item) and not item["order"].get("protection_started"):
                    item["next_update"] = self._now() + min(300, int(item.get("refresh_interval", 300)))
                self._persist_locked()

    @staticmethod
    def _is_missing_order(exc: Exception) -> bool:
        # requests.HTTPError text is only "400 Client Error"; Binance puts the
        # real code (-2013 query / -2011 cancel) in the response body.
        text = str(exc).lower()
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                text += " " + str(response.text or "").lower()
            except Exception:
                pass
        return ("-2013" in text or "-2011" in text
                or "does not exist" in text or "unknown order" in text)
