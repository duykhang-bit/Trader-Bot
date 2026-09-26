"""Pump SHORT structure confirmation and per-symbol entry coordination.

This module is deliberately free of exchange/network dependencies so its market
structure and state transitions can be unit-tested safely.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import threading
import time
import uuid
from typing import Any, Iterable, Mapping, Optional


class PumpEntryState(str, Enum):
    IDLE = "IDLE"
    PUMPING = "PUMPING"
    WATCHING_TOP = "WATCHING_TOP"
    BOS_CONFIRMED = "BOS_CONFIRMED"
    ENTRY_PENDING = "ENTRY_PENDING"
    IN_TRADE = "IN_TRADE"
    COOLDOWN = "COOLDOWN"


@dataclass(frozen=True)
class BearishBosResult:
    confirmed: bool
    reason: str
    peak_price: float = 0.0
    lower_high: float = 0.0
    broken_level: float = 0.0
    bos_close: float = 0.0
    bos_close_time: int = 0


@dataclass(frozen=True)
class ShortEntryDecision:
    action: str  # LIMIT, MARKET, REJECT
    price: float
    reason: str


@dataclass(frozen=True)
class EntryReservation:
    symbol: str
    token: str
    source: str
    expires_at: float


@dataclass
class _SymbolState:
    state: PumpEntryState = PumpEntryState.IDLE
    peak_price: float = 0.0
    source: str = ""
    updated_at: float = 0.0
    reservation_token: str = ""
    reservation_expires_at: float = 0.0
    cooldown_until: float = 0.0


def _records(candles: Any) -> list[Mapping[str, Any]]:
    if candles is None:
        return []
    if hasattr(candles, "to_dict"):
        try:
            return list(candles.to_dict("records"))
        except Exception:
            return []
    if isinstance(candles, Iterable) and not isinstance(candles, (str, bytes)):
        return [row for row in candles if isinstance(row, Mapping)]
    return []


def detect_bearish_bos_1m(candles: Any, *, now_ms: int,
                           lookback: int = 30) -> BearishBosResult:
    """Confirm peak -> structure low -> lower high -> close below structure low.

    Only candles whose ``close_time`` is strictly earlier than ``now_ms`` are
    considered. The latest *closed* candle must provide the break; a wick below
    the level is insufficient.
    """
    clean: dict[int, dict[str, float]] = {}
    for row in _records(candles):
        try:
            close_time = int(float(row["close_time"]))
            values = {
                key: float(row[key]) for key in ("open", "high", "low", "close")
            }
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if close_time >= int(now_ms) or not all(math.isfinite(v) for v in values.values()):
            continue
        if values["low"] <= 0 or values["high"] < values["low"]:
            continue
        clean[close_time] = {"close_time": close_time, **values}

    rows = [clean[key] for key in sorted(clean)][-max(6, int(lookback)):]
    if len(rows) < 6:
        return BearishBosResult(False, "insufficient_closed_candles")

    bos = rows[-1]
    history = rows[:-1]
    # Leave room after the peak for a structure low and a confirmed lower high.
    peak_index = max(range(max(1, len(history) - 3)), key=lambda i: history[i]["high"])
    peak = history[peak_index]["high"]
    after_peak = history[peak_index + 1:]
    if len(after_peak) < 3:
        return BearishBosResult(False, "insufficient_post_peak_structure", peak_price=peak)
    if any(row["high"] >= peak for row in after_peak) or bos["high"] >= peak:
        return BearishBosResult(False, "peak_broken", peak_price=peak)

    lower_high_index: Optional[int] = None
    for i in range(1, len(after_peak) - 1):
        high = after_peak[i]["high"]
        if high >= after_peak[i - 1]["high"] and high > after_peak[i + 1]["high"] and high < peak:
            lower_high_index = i
    if lower_high_index is None:
        return BearishBosResult(False, "no_lower_high", peak_price=peak)

    structure_rows = after_peak[:lower_high_index]
    if not structure_rows:
        return BearishBosResult(False, "no_structure_low", peak_price=peak)
    structure_low = min(row["low"] for row in structure_rows)
    lower_high = after_peak[lower_high_index]["high"]
    if bos["close"] >= structure_low:
        return BearishBosResult(
            False, "no_close_below_structure_low", peak, lower_high,
            structure_low, bos["close"], int(bos["close_time"]),
        )
    return BearishBosResult(
        True, "bearish_bos_confirmed", peak, lower_high, structure_low,
        bos["close"], int(bos["close_time"]),
    )


def decide_short_entry(*, current_price: float, planned_entry: float,
                       market_proximity_pct: float = 0.20,
                       max_above_entry_pct: float = 0.75,
                       max_retest_distance_pct: float = 5.0) -> ShortEntryDecision:
    """Choose a non-marketable SELL LIMIT, a tightly revalidated MARKET, or reject."""
    if current_price <= 0 or planned_entry <= 0:
        return ShortEntryDecision("REJECT", 0.0, "invalid_price")
    delta_pct = (current_price - planned_entry) / planned_entry * 100.0
    if current_price < planned_entry:
        distance = abs(delta_pct)
        if distance > abs(max_retest_distance_pct):
            return ShortEntryDecision("REJECT", 0.0, "retest_too_far_below")
        return ShortEntryDecision("LIMIT", planned_entry, "wait_for_retest_above_market")
    if delta_pct > abs(market_proximity_pct):
        reason = "price_too_far_above_entry" if delta_pct > abs(max_above_entry_pct) else "outside_market_entry_zone"
        return ShortEntryDecision("REJECT", 0.0, reason)
    return ShortEntryDecision("MARKET", current_price, "price_near_revalidated_entry")


class PumpEntryCoordinator:
    """Thread-safe owner of one pump entry reservation per symbol."""

    def __init__(self, *, reservation_ttl_sec: float = 20.0,
                 watch_ttl_sec: float = 180.0,
                 new_high_epsilon_pct: float = 0.0,
                 clock=time.monotonic):
        self._reservation_ttl = max(0.1, float(reservation_ttl_sec))
        self._watch_ttl = max(self._reservation_ttl, float(watch_ttl_sec))
        self._new_high_epsilon_pct = max(0.0, float(new_high_epsilon_pct))
        self._clock = clock
        self._lock = threading.RLock()
        self._symbols: dict[str, _SymbolState] = {}

    def _expire(self, item: _SymbolState, now: float) -> None:
        if (
            item.state == PumpEntryState.ENTRY_PENDING
            and item.reservation_expires_at > 0
            and now >= item.reservation_expires_at
        ):
            item.state = PumpEntryState.WATCHING_TOP
            item.reservation_token = ""
            item.reservation_expires_at = 0.0
        elif item.state == PumpEntryState.COOLDOWN and now >= item.cooldown_until:
            item.state = PumpEntryState.IDLE
        elif item.state in {PumpEntryState.PUMPING, PumpEntryState.WATCHING_TOP, PumpEntryState.BOS_CONFIRMED} and now - item.updated_at >= self._watch_ttl:
            item.state = PumpEntryState.IDLE
            item.peak_price = 0.0

    def observe_pump(self, symbol: str, peak_price: float, source: str) -> PumpEntryState:
        now = self._clock()
        with self._lock:
            item = self._symbols.setdefault(symbol, _SymbolState(updated_at=now))
            self._expire(item, now)
            threshold = item.peak_price * (1.0 + self._new_high_epsilon_pct / 100.0)
            protected_state = item.state in {
                PumpEntryState.ENTRY_PENDING,
                PumpEntryState.IN_TRADE,
                PumpEntryState.COOLDOWN,
            }
            if (
                not protected_state
                and peak_price > 0
                and (item.peak_price <= 0 or peak_price > threshold)
            ):
                item.peak_price = peak_price
                item.state = PumpEntryState.PUMPING
                item.reservation_token = ""
                item.reservation_expires_at = 0.0
            if item.state in {PumpEntryState.IDLE, PumpEntryState.PUMPING}:
                item.state = PumpEntryState.WATCHING_TOP
            item.source = source
            item.updated_at = now
            return item.state

    def confirm_bos(self, symbol: str, result: BearishBosResult) -> bool:
        now = self._clock()
        with self._lock:
            item = self._symbols.setdefault(symbol, _SymbolState(updated_at=now))
            self._expire(item, now)
            if not result.confirmed:
                if item.state not in {PumpEntryState.ENTRY_PENDING, PumpEntryState.IN_TRADE, PumpEntryState.COOLDOWN}:
                    item.state = PumpEntryState.WATCHING_TOP
                item.updated_at = now
                return False
            # Allow tiny exchange rounding differences between a detector peak
            # and the raw closed-kline high, while still rejecting a new high.
            if item.peak_price > 0 and result.peak_price < item.peak_price * 0.999:
                item.state = PumpEntryState.WATCHING_TOP
                item.updated_at = now
                return False
            if item.state in {PumpEntryState.WATCHING_TOP, PumpEntryState.PUMPING}:
                item.state = PumpEntryState.BOS_CONFIRMED
            item.updated_at = now
            return item.state == PumpEntryState.BOS_CONFIRMED

    def reserve(self, symbol: str, source: str) -> Optional[EntryReservation]:
        now = self._clock()
        with self._lock:
            item = self._symbols.setdefault(symbol, _SymbolState(updated_at=now))
            self._expire(item, now)
            if item.state != PumpEntryState.BOS_CONFIRMED:
                return None
            token = uuid.uuid4().hex
            item.state = PumpEntryState.ENTRY_PENDING
            item.source = source
            item.updated_at = now
            item.reservation_token = token
            item.reservation_expires_at = now + self._reservation_ttl
            return EntryReservation(symbol, token, source, item.reservation_expires_at)

    def placement_succeeded(self, reservation: EntryReservation, *, in_trade: bool) -> bool:
        with self._lock:
            item = self._symbols.get(reservation.symbol)
            if not item or item.reservation_token != reservation.token:
                return False
            item.state = PumpEntryState.IN_TRADE if in_trade else PumpEntryState.ENTRY_PENDING
            item.updated_at = self._clock()
            if not in_trade:
                # Acknowledged exchange LIMIT ownership ends only when the
                # monitor confirms fill or terminal cancellation.
                item.reservation_expires_at = 0.0
            return True

    def placement_failed(self, reservation: EntryReservation) -> bool:
        with self._lock:
            item = self._symbols.get(reservation.symbol)
            if not item or item.reservation_token != reservation.token:
                return False
            item.state = PumpEntryState.WATCHING_TOP
            item.updated_at = self._clock()
            item.reservation_token = ""
            item.reservation_expires_at = 0.0
            return True

    def mark_flat(self, symbol: str, cooldown_sec: float) -> None:
        """Move a reconciled closed trade through COOLDOWN before re-arming."""
        with self._lock:
            item = self._symbols.get(symbol)
            if not item or item.state != PumpEntryState.IN_TRADE:
                return
            now = self._clock()
            item.state = PumpEntryState.COOLDOWN
            item.updated_at = now
            item.cooldown_until = now + max(0.0, float(cooldown_sec))
            item.reservation_token = ""

    def mark_in_trade(self, symbol: str) -> None:
        """Reconcile a pending LIMIT after the exchange reports a SHORT fill."""
        with self._lock:
            now = self._clock()
            item = self._symbols.setdefault(symbol, _SymbolState())
            item.state = PumpEntryState.IN_TRADE
            item.updated_at = now
            item.reservation_expires_at = 0.0

    def release_pending(self, symbol: str) -> None:
        """Release coordinator ownership after a LIMIT is terminal/cancelled."""
        with self._lock:
            item = self._symbols.get(symbol)
            if item and item.state == PumpEntryState.ENTRY_PENDING:
                item.state = PumpEntryState.WATCHING_TOP
                item.updated_at = self._clock()
                item.reservation_token = ""
                item.reservation_expires_at = 0.0

    def cooldown(self, symbol: str, seconds: float) -> None:
        with self._lock:
            now = self._clock()
            item = self._symbols.setdefault(symbol, _SymbolState())
            item.state = PumpEntryState.COOLDOWN
            item.updated_at = now
            item.cooldown_until = now + max(0.0, float(seconds))
            item.reservation_token = ""

    def snapshot(self, symbol: str) -> PumpEntryState:
        now = self._clock()
        with self._lock:
            item = self._symbols.setdefault(symbol, _SymbolState(updated_at=now))
            self._expire(item, now)
            return item.state
