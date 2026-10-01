"""Durable, read-only Binance Futures trade ledger for the dashboard.

Only the background worker in :class:`BinanceTradeLedger` performs Binance I/O.
Flask request handlers consume immutable snapshots via ``snapshot()``.
"""
from __future__ import annotations

import atexit
import copy
import hashlib
import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000
CHUNK_MS = 6 * DAY_MS - 1
OVERLAP_MS = 5 * 60_000
MAX_PAGES_PER_CHUNK = 1_000
CACHE_VERSION = 2
DEFAULT_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "binance_trade_ledger.json"
)
TRANSFER_TYPES = {"TRANSFER"}


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _iso_utc(timestamp_ms: int) -> str:
    if not timestamp_ms:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


def _local_time(timestamp_ms: int) -> str:
    if not timestamp_ms:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _legacy_timestamp_ms(value: Any) -> Optional[int]:
    if not value:
        return None
    text = str(value)
    try:
        return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError):
            return None


def _fill_key(fill: Dict[str, Any]) -> Tuple[str, str]:
    return str(fill.get("symbol", "")), str(fill.get("id", ""))


def _income_key(record: Dict[str, Any]) -> Tuple[Any, ...]:
    tran_id = str(record.get("tranId", "") or "")
    if tran_id and tran_id != "0":
        return ("tranId", tran_id)
    return (
        "composite",
        str(record.get("symbol", "")),
        str(record.get("incomeType", "")),
        int(record.get("time", 0) or 0),
        str(record.get("income", "0")),
        str(record.get("asset", "")),
    )


def _event_id(record: Dict[str, Any]) -> str:
    return json.dumps(_income_key(record), separators=(",", ":"), ensure_ascii=True)


def _position_key(symbol: str, position_side: Any) -> Tuple[str, str]:
    return symbol, str(position_side or "BOTH").upper()


def _signed_quantity(fill: Dict[str, Any]) -> float:
    qty = abs(_number(fill.get("qty")))
    return qty if str(fill.get("side", "")).upper() == "BUY" else -qty


def _new_cycle(
    symbol: str,
    position_side: str,
    position: float,
    opened_at_ms: int,
    boundary_start: bool,
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "position_side": position_side,
        "side": "LONG" if position > 0 else "SHORT",
        "opened_at_ms": opened_at_ms,
        "opened_at": _local_time(opened_at_ms),
        "closed_at_ms": None,
        "closed_at": "",
        "boundary_start": bool(boundary_start),
        "complete": False,
        "gross_realized_pnl": 0.0,
        "commission": 0.0,
        "funding": 0.0,
        "net_pnl": 0.0,
        "commission_complete": True,
        "funding_complete": True,
        "net_complete": True,
        "commission_assets": {},
        "funding_event_ids": [],
        "ambiguous_funding_event_ids": [],
        "fill_ids": [],
        "entry_price": 0.0,
        "close_price": 0.0,
        "quantity": 0.0,
        "_entry_qty": 0.0,
        "_entry_notional": 0.0,
        "_close_qty": 0.0,
        "_close_notional": 0.0,
    }


def _add_fill_portion(
    cycle: Dict[str, Any],
    fill: Dict[str, Any],
    quantity: float,
    commission: float,
    gross: float,
    role: str,
) -> None:
    if quantity <= 0:
        return
    price = _number(fill.get("price"))
    fill_id = f"{fill.get('symbol', '')}:{fill.get('id', '')}:{role}"
    cycle["fill_ids"].append(fill_id)
    cycle["gross_realized_pnl"] += gross
    asset = str(fill.get("commissionAsset") or "UNKNOWN").upper()
    cycle["commission_assets"][asset] = (
        cycle["commission_assets"].get(asset, 0.0) + commission
    )
    # Never treat raw BNB/other units as USDT. Their historical conversion is
    # unavailable here, so canonical cycle net fails closed instead.
    if asset == "USDT":
        cycle["commission"] += commission
    elif commission:
        cycle["commission_complete"] = False
    if role == "entry":
        cycle["_entry_qty"] += quantity
        cycle["_entry_notional"] += price * quantity
        cycle["quantity"] = max(cycle["quantity"], cycle["_entry_qty"])
    else:
        cycle["_close_qty"] += quantity
        cycle["_close_notional"] += price * quantity


def _finish_cycle(cycle: Dict[str, Any], closed_at_ms: int) -> None:
    cycle["closed_at_ms"] = closed_at_ms
    cycle["closed_at"] = _local_time(closed_at_ms)
    cycle["complete"] = not cycle["boundary_start"]


def _stable_cycle_id(cycle: Dict[str, Any]) -> str:
    first_fill = cycle.get("fill_ids", [""])[0] if cycle.get("fill_ids") else ""
    raw = "|".join(
        [
            str(cycle.get("symbol", "")),
            str(cycle.get("position_side", "BOTH")),
            str(cycle.get("opened_at_ms", 0)),
            str(cycle.get("side", "")),
            str(first_fill),
            "boundary" if cycle.get("boundary_start") else "complete",
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def reconstruct_position_cycles(
    fills: Iterable[Dict[str, Any]],
    income: Iterable[Dict[str, Any]],
    current_positions: Iterable[Dict[str, Any]],
    window_start_ms: int,
) -> List[Dict[str, Any]]:
    """Reconstruct flat-to-flat position cycles from chronological fills.

    Boundary cycles remain visible but are incomplete outcomes. Funding is
    attached only when one position leg is unambiguous; hedge ambiguity remains
    aggregate-only and explicitly makes affected cycle net unavailable.
    """
    fills_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for raw in fills:
        fill = dict(raw)
        symbol = str(fill.get("symbol", ""))
        if symbol:
            fills_by_key[_position_key(symbol, fill.get("positionSide"))].append(fill)

    end_positions: Dict[Tuple[str, str], float] = defaultdict(float)
    for position_row in current_positions or []:
        symbol = str(position_row.get("symbol", ""))
        if symbol:
            end_positions[_position_key(symbol, position_row.get("positionSide"))] += _number(
                position_row.get("positionAmt")
            )

    cycles: List[Dict[str, Any]] = []
    open_cycles: Dict[Tuple[str, str], Dict[str, Any]] = {}
    all_keys = set(fills_by_key) | {key for key, qty in end_positions.items() if qty}

    for key in sorted(all_keys):
        symbol, position_side = key
        keyed_fills = sorted(
            fills_by_key.get(key, []),
            key=lambda item: (int(item.get("time", 0) or 0), int(item.get("id", 0) or 0)),
        )
        position = end_positions.get(key, 0.0) - sum(
            _signed_quantity(fill) for fill in keyed_fills
        )
        epsilon = 1e-12
        cycle: Optional[Dict[str, Any]] = None
        if abs(position) > epsilon:
            cycle = _new_cycle(symbol, position_side, position, window_start_ms, True)
            cycle["quantity"] = abs(position)
            open_cycles[key] = cycle

        for fill in keyed_fills:
            timestamp_ms = int(fill.get("time", 0) or 0)
            signed_qty = _signed_quantity(fill)
            qty = abs(signed_qty)
            if qty <= epsilon:
                continue
            commission = abs(_number(fill.get("commission")))
            gross = _number(fill.get("realizedPnl"))

            if abs(position) <= epsilon:
                position = 0.0
                cycle = _new_cycle(symbol, position_side, signed_qty, timestamp_ms, False)
                _add_fill_portion(cycle, fill, qty, commission, gross, "entry")
                position = signed_qty
                open_cycles[key] = cycle
                continue

            if cycle is None:
                cycle = _new_cycle(symbol, position_side, position, window_start_ms, True)
                cycle["quantity"] = abs(position)
                open_cycles[key] = cycle

            if position * signed_qty > 0:
                _add_fill_portion(cycle, fill, qty, commission, gross, "entry")
                position += signed_qty
                continue

            closing_qty = min(abs(position), qty)
            closing_ratio = closing_qty / qty
            _add_fill_portion(
                cycle, fill, closing_qty, commission * closing_ratio, gross, "exit"
            )
            old_sign = 1.0 if position > 0 else -1.0
            remaining_qty = qty - closing_qty
            position += signed_qty

            if abs(position) <= epsilon or old_sign * position < 0:
                _finish_cycle(cycle, timestamp_ms)
                cycles.append(cycle)
                open_cycles.pop(key, None)
                cycle = None

            if remaining_qty > epsilon:
                new_signed = remaining_qty * (1.0 if signed_qty > 0 else -1.0)
                cycle = _new_cycle(symbol, position_side, new_signed, timestamp_ms, False)
                _add_fill_portion(
                    cycle,
                    fill,
                    remaining_qty,
                    commission * (1.0 - closing_ratio),
                    0.0,
                    "entry",
                )
                position = new_signed
                open_cycles[key] = cycle
            elif abs(position) <= epsilon:
                position = 0.0

        if cycle is not None and abs(position) > epsilon:
            open_cycles[key] = cycle

    cycles.extend(open_cycles.values())

    for record in sorted(income or [], key=lambda item: int(item.get("time", 0) or 0)):
        if str(record.get("incomeType", "")).upper() != "FUNDING_FEE":
            continue
        symbol = str(record.get("symbol", ""))
        timestamp_ms = int(record.get("time", 0) or 0)
        candidates = [
            cycle
            for cycle in cycles
            if cycle.get("symbol") == symbol
            and int(cycle.get("opened_at_ms", 0) or 0) <= timestamp_ms
            and (
                cycle.get("closed_at_ms") is None
                or timestamp_ms <= int(cycle.get("closed_at_ms") or 0)
            )
        ]
        discriminator = str(record.get("positionSide") or "").upper()
        if discriminator in {"LONG", "SHORT", "BOTH"}:
            candidates = [
                cycle for cycle in candidates
                if str(cycle.get("position_side", "BOTH")).upper() == discriminator
            ]
        event_id = _event_id(record)
        if len(candidates) == 1:
            candidates[0]["funding"] += _number(record.get("income"))
            candidates[0]["funding_event_ids"].append(event_id)
        elif len(candidates) > 1:
            # No arbitrary leg-count/exposure split: the aggregate event remains
            # exact, while every potentially affected cycle is explicitly partial.
            for candidate in candidates:
                candidate["funding_complete"] = False
                candidate["ambiguous_funding_event_ids"].append(event_id)

    for cycle in cycles:
        entry_qty = cycle.pop("_entry_qty", 0.0)
        entry_notional = cycle.pop("_entry_notional", 0.0)
        close_qty = cycle.pop("_close_qty", 0.0)
        close_notional = cycle.pop("_close_notional", 0.0)
        cycle["entry_price"] = entry_notional / entry_qty if entry_qty else 0.0
        cycle["close_price"] = close_notional / close_qty if close_qty else 0.0
        cycle["gross_realized_pnl"] = round(cycle["gross_realized_pnl"], 12)
        cycle["commission"] = round(cycle["commission"], 12)
        cycle["funding"] = round(cycle["funding"], 12)
        cycle["net_complete"] = bool(
            cycle.get("commission_complete", True)
            and cycle.get("funding_complete", True)
        )
        cycle["net_pnl"] = (
            round(cycle["gross_realized_pnl"] - cycle["commission"] + cycle["funding"], 12)
            if cycle["net_complete"] else None
        )
        cycle["commission_assets"] = {
            asset: round(value, 12)
            for asset, value in sorted(cycle["commission_assets"].items())
        }
        cycle["cycle_id"] = _stable_cycle_id(cycle)

    return sorted(
        cycles,
        key=lambda item: (
            int(item.get("opened_at_ms", 0) or 0),
            str(item.get("symbol", "")),
            str(item.get("cycle_id", "")),
        ),
    )


def build_financial_events(
    fills: Iterable[Dict[str, Any]],
    income: Iterable[Dict[str, Any]],
    cycles: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build the single accounting basis consumed by every financial API."""
    cycle_list = list(cycles)
    allocated_funding = {
        event_id
        for cycle in cycle_list
        for event_id in cycle.get("funding_event_ids", [])
    }
    ambiguous_funding = {
        event_id
        for cycle in cycle_list
        for event_id in cycle.get("ambiguous_funding_event_ids", [])
    }
    events: List[Dict[str, Any]] = []
    for fill in fills:
        timestamp_ms = int(fill.get("time", 0) or 0)
        gross = _number(fill.get("realizedPnl"))
        raw_commission = abs(_number(fill.get("commission")))
        asset = str(fill.get("commissionAsset") or "UNKNOWN").upper()
        commission_complete = asset == "USDT" or raw_commission == 0
        commission_usdt = raw_commission if asset == "USDT" else 0.0
        net = gross - commission_usdt if commission_complete else None
        events.append({
            "event_id": f"fill:{fill.get('symbol', '')}:{fill.get('id', '')}",
            "event_type": "fill",
            "time_ms": timestamp_ms,
            "time": _local_time(timestamp_ms),
            "symbol": str(fill.get("symbol", "")),
            "gross": round(gross, 12),
            "commission": round(commission_usdt, 12),
            "commission_asset": asset,
            "raw_commission": round(raw_commission, 12),
            "funding": 0.0,
            "net": round(net, 12) if net is not None else None,
            "commission_complete": commission_complete,
            "net_complete": commission_complete,
            "allocation_status": "fill",
        })
    for record in income:
        if str(record.get("incomeType", "")).upper() != "FUNDING_FEE":
            continue
        timestamp_ms = int(record.get("time", 0) or 0)
        amount = _number(record.get("income"))
        asset = str(record.get("asset") or "UNKNOWN").upper()
        net_complete = asset == "USDT"
        event_id = _event_id(record)
        allocation_status = (
            "ambiguous_hedge" if event_id in ambiguous_funding
            else "cycle" if event_id in allocated_funding
            else "aggregate_only"
        )
        events.append({
            "event_id": event_id,
            "event_type": "funding",
            "time_ms": timestamp_ms,
            "time": _local_time(timestamp_ms),
            "symbol": str(record.get("symbol", "")),
            "gross": 0.0,
            "commission": 0.0,
            "funding": round(amount, 12) if net_complete else 0.0,
            "funding_asset": asset,
            "raw_funding": round(amount, 12),
            "net": round(amount, 12) if net_complete else None,
            "commission_complete": True,
            "net_complete": net_complete,
            "allocation_status": allocation_status,
            "aggregate_only": allocation_status != "cycle",
        })
    return sorted(
        events,
        key=lambda item: (
            int(item.get("time_ms", 0) or 0),
            str(item.get("event_type", "")),
            str(item.get("event_id", "")),
        ),
    )


def aggregate_financial_events(
    events: Iterable[Dict[str, Any]], transfers: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    event_list = list(events)
    transfer_list = list(transfers)
    gross = sum(_number(item.get("gross")) for item in event_list)
    commission = sum(_number(item.get("commission")) for item in event_list)
    funding = sum(_number(item.get("funding")) for item in event_list)
    net_complete = all(bool(item.get("net_complete", True)) for item in event_list)
    net = sum(_number(item.get("net")) for item in event_list) if net_complete else None
    unsupported_transfer_assets = sorted({
        str(item.get("asset") or "UNKNOWN").upper()
        for item in transfer_list
        if abs(_number(item.get("amount"))) > 0
        and str(item.get("asset") or "UNKNOWN").upper() != "USDT"
    })
    transfer_complete = not unsupported_transfer_assets
    transfer_usdt = sum(
        _number(item.get("amount")) for item in transfer_list
        if str(item.get("asset") or "UNKNOWN").upper() == "USDT"
    )
    return {
        "gross": round(gross, 12),
        "commission": round(commission, 12),
        "funding": round(funding, 12),
        "net": round(net, 12) if net is not None else None,
        "transfer": round(transfer_usdt, 12) if transfer_complete else None,
        "transfer_excluding_unconverted": round(transfer_usdt, 12),
        "transfer_complete": transfer_complete,
        "unsupported_transfer_assets": unsupported_transfer_assets,
        "commission_complete": all(
            bool(item.get("commission_complete", True)) for item in event_list
        ),
        "net_complete": net_complete,
        "incomplete_event_count": sum(
            1 for item in event_list if not bool(item.get("net_complete", True))
        ),
    }


class BinanceTradeLedger:
    """Background-synced Binance canonical ledger with an atomic JSON cache."""

    def __init__(
        self,
        exchange: Any,
        legacy_history: Optional[Iterable[Dict[str, Any]]] = None,
        cache_file: str = DEFAULT_CACHE_FILE,
        refresh_seconds: int = 60,
        retention_days: int = 365,
        stale_seconds: int = 180,
    ) -> None:
        self.exchange = exchange
        self.legacy_history = [dict(item) for item in (legacy_history or [])]
        self.cache_file = cache_file
        self.refresh_seconds = max(10, int(refresh_seconds))
        self.retention_days = max(30, int(retention_days))
        self.stale_seconds = max(self.refresh_seconds * 2, int(stale_seconds))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._data = self._empty_data()
        self._load_cache()
        atexit.register(self.stop)

    def _empty_data(self) -> Dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "ready": False,
            "source": "binance",
            "fills": [],
            "income": [],
            "cycles": [],
            "financial_events": [],
            "transfers": [],
            "aggregate": {
                "gross": 0.0, "commission": 0.0, "funding": 0.0,
                "net": 0.0, "transfer": 0.0,
                "transfer_excluding_unconverted": 0.0,
                "transfer_complete": True, "unsupported_transfer_assets": [],
                "commission_complete": True, "net_complete": True,
                "incomplete_event_count": 0,
            },
            "account": {
                "wallet_balance": 0.0,
                "margin_balance": 0.0,
                "available_balance": 0.0,
                "positions": [],
            },
            "queried_symbols": [],
            "window_start_ms": 0,
            "window_start": "",
            "window_clipped": False,
            "last_sync_ms": 0,
            "last_sync": "",
            "synced_at": "",
            "syncing": False,
            "errors": [],
            "non_usdt_commission_assets": [],
            "ambiguous_funding_event_ids": [],
            "financial_warnings": [],
        }

    def _load_cache(self) -> None:
        if not os.path.exists(self.cache_file):
            return
        try:
            with open(self.cache_file, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict) or loaded.get("version") != CACHE_VERSION:
                raise ValueError("unsupported ledger cache schema")
            loaded["ready"] = bool(loaded.get("last_sync_ms"))
            loaded["syncing"] = False
            with self._lock:
                self._data = loaded
        except Exception as exc:
            backup = f"{self.cache_file}.corrupt.{int(time.time())}"
            try:
                os.replace(self.cache_file, backup)
            except OSError:
                backup = "unavailable"
            logger.warning("[Ledger] Ignoring corrupt cache (%s), backup=%s", exc, backup)

    def _write_cache(self, data: Dict[str, Any]) -> None:
        directory = os.path.dirname(self.cache_file)
        os.makedirs(directory, exist_ok=True)
        tmp_file = f"{self.cache_file}.tmp"
        payload = copy.deepcopy(data)
        payload["syncing"] = False
        with open(tmp_file, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_file, self.cache_file)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="binance-trade-ledger", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self, include_raw: bool = False) -> Dict[str, Any]:
        with self._lock:
            if include_raw:
                result = copy.deepcopy(self._data)
            else:
                dashboard_keys = (
                    "version", "ready", "source", "cycles", "financial_events",
                    "transfers", "aggregate", "account", "window_start_ms",
                    "window_start", "window_clipped", "last_sync_ms", "last_sync",
                    "synced_at", "syncing", "errors", "non_usdt_commission_assets",
                    "ambiguous_funding_event_ids", "financial_warnings",
                )
                result = {
                    key: copy.deepcopy(self._data.get(key))
                    for key in dashboard_keys if key in self._data
                }
                result["fill_count"] = len(self._data.get("fills", []))
                result["income_count"] = len(self._data.get("income", []))
        last_sync_ms = int(result.get("last_sync_ms", 0) or 0)
        result["stale"] = (
            not last_sync_ms
            or int(time.time() * 1000) - last_sync_ms > self.stale_seconds * 1000
        )
        return result

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.sync_once()
            except Exception as exc:
                logger.exception("[Ledger] Sync failed: %s", exc)
                with self._lock:
                    errors = list(self._data.get("errors", []))[-9:]
                    errors.append(f"{datetime.now().isoformat(timespec='seconds')}: {exc}")
                    self._data["errors"] = errors
            elapsed = time.monotonic() - started
            self._stop.wait(max(1.0, self.refresh_seconds - elapsed))

    def _requested_window_start(self, now_ms: int) -> Tuple[int, bool]:
        requested = now_ms - 30 * DAY_MS
        legacy_times = [
            timestamp
            for timestamp in (
                _legacy_timestamp_ms(item.get("time")) for item in self.legacy_history
            )
            if timestamp is not None
        ]
        if legacy_times:
            requested = min(requested, min(legacy_times))
        retention_start = now_ms - self.retention_days * DAY_MS
        return max(requested, retention_start), requested < retention_start

    @staticmethod
    def _page_signature(page: List[Dict[str, Any]]) -> str:
        canonical = json.dumps(page, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _fetch_pages(
        self,
        endpoint: str,
        start_ms: int,
        end_ms: int,
        symbol: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        chunk_start = start_ms
        while chunk_start <= end_ms:
            chunk_end = min(end_ms, chunk_start + CHUNK_MS)
            seen_signatures = set()
            from_id: Optional[int] = None
            income_cursor = chunk_start
            for _page_index in range(MAX_PAGES_PER_CHUNK):
                if symbol:
                    # Binance userTrades rejects fromId combined with a time range.
                    # Use the range only for page one, then continue by ID and
                    # enforce the original chunk bounds locally.
                    params: Dict[str, Any] = {"symbol": symbol, "limit": 1000}
                    if from_id is None:
                        params.update({"startTime": chunk_start, "endTime": chunk_end})
                    else:
                        params["fromId"] = from_id
                else:
                    # Income history has no ID cursor. Advance an inclusive time
                    # cursor, detect repeated/no-progress pages, and never use page.
                    params = {
                        "startTime": income_cursor,
                        "endTime": chunk_end,
                        "limit": 1000,
                    }
                page = self.exchange._get(endpoint, params, signed=True) or []
                if not isinstance(page, list):
                    raise ValueError(f"unexpected {endpoint} response")
                page_dicts = [dict(item) for item in page]
                signature = self._page_signature(page_dicts)
                if signature in seen_signatures:
                    raise RuntimeError(f"{endpoint} pagination repeated a page")
                seen_signatures.add(signature)

                in_chunk = [
                    item for item in page_dicts
                    if chunk_start <= int(item.get("time", 0) or 0) <= chunk_end
                ]
                rows.extend(in_chunk)
                if len(page_dicts) < 1000:
                    break

                if symbol:
                    ids = [int(item.get("id", 0) or 0) for item in page_dicts]
                    max_id = max(ids) if ids else 0
                    next_from_id = max_id + 1
                    if max_id <= 0 or (from_id is not None and next_from_id <= from_id):
                        raise RuntimeError(f"{endpoint} pagination did not advance")
                    from_id = next_from_id
                    if page_dicts and min(
                        int(item.get("time", 0) or 0) for item in page_dicts
                    ) > chunk_end:
                        break
                else:
                    times = [int(item.get("time", 0) or 0) for item in page_dicts]
                    next_cursor = max(times) if times else income_cursor
                    if next_cursor <= income_cursor:
                        raise RuntimeError(f"{endpoint} pagination did not advance")
                    income_cursor = next_cursor
            else:
                raise RuntimeError(
                    f"{endpoint} pagination exceeded {MAX_PAGES_PER_CHUNK} pages"
                )
            chunk_start = chunk_end + 1
        return rows

    @staticmethod
    def _normalize_fill(raw: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "symbol": str(raw.get("symbol", "")),
            "id": raw.get("id"),
            "orderId": raw.get("orderId"),
            "side": str(raw.get("side", "")).upper(),
            "positionSide": str(raw.get("positionSide") or "BOTH").upper(),
            "qty": str(raw.get("qty", "0")),
            "price": str(raw.get("price", "0")),
            "realizedPnl": str(raw.get("realizedPnl", "0")),
            "commission": str(raw.get("commission", "0")),
            "commissionAsset": str(raw.get("commissionAsset") or "UNKNOWN").upper(),
            "time": int(raw.get("time", 0) or 0),
        }

    @staticmethod
    def _normalize_income(raw: Dict[str, Any]) -> Dict[str, Any]:
        normalized = {
            "symbol": str(raw.get("symbol", "")),
            "incomeType": str(raw.get("incomeType", "")).upper(),
            "income": str(raw.get("income", "0")),
            "asset": str(raw.get("asset") or "UNKNOWN").upper(),
            "time": int(raw.get("time", 0) or 0),
            "tranId": raw.get("tranId"),
        }
        if raw.get("positionSide"):
            normalized["positionSide"] = str(raw.get("positionSide")).upper()
        return normalized

    @staticmethod
    def _account_snapshot(raw: Dict[str, Any]) -> Dict[str, Any]:
        positions = [
            {
                "symbol": str(item.get("symbol", "")),
                "positionSide": str(item.get("positionSide") or "BOTH").upper(),
                "positionAmt": str(item.get("positionAmt", "0")),
            }
            for item in raw.get("positions", [])
            if abs(_number(item.get("positionAmt"))) > 0
        ]
        return {
            "wallet_balance": _number(raw.get("totalWalletBalance")),
            "margin_balance": _number(raw.get("totalMarginBalance")),
            "available_balance": _number(raw.get("availableBalance")),
            "positions": positions,
        }

    def sync_once(self) -> None:
        """Run one atomic sync; failures preserve the previous durable snapshot."""
        try:
            self._sync_once_impl()
        except Exception:
            with self._lock:
                self._data["syncing"] = False
            raise

    def _sync_once_impl(self) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            previous = copy.deepcopy(self._data)
            self._data["syncing"] = True

        window_start_ms, clipped = self._requested_window_start(now_ms)
        previous_window = int(previous.get("window_start_ms", 0) or 0)
        last_sync_ms = int(previous.get("last_sync_ms", 0) or 0)
        if last_sync_ms:
            fetch_start_ms = max(window_start_ms, last_sync_ms - OVERLAP_MS)
            if previous_window and window_start_ms < previous_window:
                fetch_start_ms = window_start_ms
        else:
            fetch_start_ms = window_start_ms

        account_raw = self.exchange._get("/fapi/v2/account", signed=True)
        if not isinstance(account_raw, dict):
            raise ValueError("unexpected account response")
        account = self._account_snapshot(account_raw)

        new_income = [
            self._normalize_income(item)
            for item in self._fetch_pages("/fapi/v1/income", fetch_start_ms, now_ms)
        ]
        income_map = {
            _income_key(item): item
            for item in previous.get("income", [])
            if int(item.get("time", 0) or 0) >= window_start_ms
        }
        income_map.update({_income_key(item): item for item in new_income})
        income = sorted(
            income_map.values(), key=lambda item: int(item.get("time", 0) or 0)
        )

        symbols = {str(item.get("symbol", "")) for item in income if item.get("symbol")}
        symbols.update(
            str(item.get("symbol", ""))
            for item in self.legacy_history if item.get("symbol")
        )
        symbols.update(
            str(item.get("symbol", ""))
            for item in account.get("positions", []) if item.get("symbol")
        )

        queried_symbols = set(previous.get("queried_symbols", []))
        new_fills: List[Dict[str, Any]] = []
        for symbol in sorted(symbols):
            symbol_start = fetch_start_ms if symbol in queried_symbols else window_start_ms
            rows = self._fetch_pages(
                "/fapi/v1/userTrades", symbol_start, now_ms, symbol=symbol
            )
            new_fills.extend(self._normalize_fill(item) for item in rows)
            queried_symbols.add(symbol)

        fill_map = {
            _fill_key(item): item
            for item in previous.get("fills", [])
            if int(item.get("time", 0) or 0) >= window_start_ms
        }
        fill_map.update({_fill_key(item): item for item in new_fills})
        fills = sorted(
            fill_map.values(),
            key=lambda item: (
                int(item.get("time", 0) or 0),
                str(item.get("symbol", "")),
                int(item.get("id", 0) or 0),
            ),
        )
        cycles = reconstruct_position_cycles(
            fills, income, account.get("positions", []), window_start_ms
        )
        transfers = [
            {
                "event_id": _event_id(item),
                "time_ms": int(item.get("time", 0) or 0),
                "time": _local_time(int(item.get("time", 0) or 0)),
                "amount": _number(item.get("income")),
                "asset": item.get("asset", "UNKNOWN"),
                "income_type": item.get("incomeType", ""),
            }
            for item in income
            if str(item.get("incomeType", "")).upper() in TRANSFER_TYPES
        ]
        financial_events = build_financial_events(fills, income, cycles)
        aggregate = aggregate_financial_events(financial_events, transfers)
        non_usdt_assets = sorted({
            str(item.get("commissionAsset") or "UNKNOWN").upper()
            for item in fills
            if str(item.get("commissionAsset") or "UNKNOWN").upper() != "USDT"
            and abs(_number(item.get("commission"))) > 0
        })
        ambiguous_funding_ids = sorted({
            event_id
            for cycle in cycles
            for event_id in cycle.get("ambiguous_funding_event_ids", [])
        })
        warnings = []
        if non_usdt_assets:
            warnings.append(
                "Net unavailable: non-USDT commission requires historical conversion ("
                + ", ".join(non_usdt_assets) + ")"
            )
        if ambiguous_funding_ids:
            warnings.append(
                f"{len(ambiguous_funding_ids)} hedge funding event(s) are aggregate-only"
            )
        if not aggregate.get("transfer_complete", True):
            warnings.append(
                "Wallet bridge unavailable: non-USDT transfer requires historical conversion ("
                + ", ".join(aggregate.get("unsupported_transfer_assets", [])) + ")"
            )

        updated = {
            "version": CACHE_VERSION,
            "ready": True,
            "source": "binance",
            "fills": fills,
            "income": income,
            "cycles": cycles,
            "financial_events": financial_events,
            "transfers": transfers,
            "aggregate": aggregate,
            "account": account,
            "queried_symbols": sorted(queried_symbols),
            "window_start_ms": window_start_ms,
            "window_start": _iso_utc(window_start_ms),
            "window_clipped": clipped,
            "last_sync_ms": now_ms,
            "last_sync": _iso_utc(now_ms),
            "synced_at": _iso_utc(now_ms),
            "syncing": False,
            "errors": [],
            "non_usdt_commission_assets": non_usdt_assets,
            "ambiguous_funding_event_ids": ambiguous_funding_ids,
            "financial_warnings": warnings,
        }
        self._write_cache(updated)
        with self._lock:
            self._data = updated
        logger.info(
            "[Ledger] Synced %d fills, %d income records, %d cycles across %d symbols",
            len(fills), len(income), len(cycles), len(queried_symbols),
        )
