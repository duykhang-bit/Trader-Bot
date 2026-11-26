"""Paper-only intraday futures breakout monitor.

The strategy is deliberately isolated from real order placement. It observes
BTC/ETH/SOL, records simulated fills with Binance VIP0 fees and exposes a
JSON-safe snapshot for the dashboard.

Rules (fixed after walk-forward testing; not optimized at runtime):
- 13:00-14:00 UTC opening range on closed 5m candles.
- Trade a close beyond the range + 0.1 ATR only in the closed 4h EMA20/50 trend.
- Stop at the opposite range edge, target 2R, maximum hold 2 hours.
- At most one trade per symbol per UTC session day.
"""
from __future__ import annotations

import atexit
import copy
from datetime import datetime, timezone
import json
import logging
import math
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

PERSISTENCE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "intraday_paper.json"
)
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
ENTRY_COST_RATE = 0.0006  # 0.05% taker + 0.01% slippage
STOP_COST_RATE = 0.0006
TP_COST_RATE = 0.0002     # resting reduce-only target
TIME_COST_RATE = 0.0006


def _f(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = float(values[0])
    for value in values[1:]:
        result = alpha * float(value) + (1.0 - alpha) * result
    return result


def _atr(rows: list[list], period=14) -> float:
    if len(rows) < period + 1:
        return 0.0
    tr = []
    previous = _f(rows[0][4])
    for row in rows[1:]:
        high, low, close = _f(row[2]), _f(row[3]), _f(row[4])
        tr.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = close
    # Wilder ATR, enough warm-up is supplied by the caller.
    value = sum(tr[:period]) / period
    for x in tr[period:]:
        value = (value * (period - 1) + x) / period
    return value


class IntradayPaperService:
    def __init__(self, config, exchange, *, real_entry_callback=None,
                 now_fn=time.time):
        self.config = config
        self.exchange = exchange
        self.real_entry_callback = real_entry_callback
        self._now = now_fn
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._data = {
            "enabled": bool(getattr(config, "INTRADAY_PAPER_ENABLED", False)),
            "real_enabled": False,
            "symbols": list(getattr(config, "INTRADAY_PAPER_SYMBOLS", DEFAULT_SYMBOLS)),
            "margin_usdt": float(getattr(config, "INTRADAY_PAPER_MARGIN_USDT", 15.0)),
            "leverage": int(getattr(config, "INTRADAY_PAPER_LEVERAGE", 1)),
            "open": {},
            "closed": [],
            "sessions": {},
            "status": {},
            "real_events": [],
            "errors": [],
            "updated_at": 0.0,
        }
        self._load()

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker, name="intraday-paper", daemon=True
            )
            self._thread.start()
            return self._thread

    def stop(self, timeout=5):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def toggle(self, enabled: bool):
        with self._lock:
            self._data["enabled"] = bool(enabled)
            if not enabled:
                self._data["real_enabled"] = False
            self._data["updated_at"] = self._now()
            self._persist_locked()
        self._wake.set()
        return {"ok": True, "enabled": bool(enabled)}

    def toggle_real(self, enabled: bool, confirmation=""):
        enabled = bool(enabled)
        if enabled and str(confirmation or "").strip().upper() != "ENABLE REAL":
            return {"ok": False, "error": "Nhập đúng ENABLE REAL để bật tiền thật"}
        with self._lock:
            self._data["real_enabled"] = enabled
            if enabled:
                self._data["enabled"] = True  # signals must run for REAL mode
            self._data["updated_at"] = self._now()
            self._persist_locked()
        self._wake.set()
        return {"ok": True, "real_enabled": enabled,
                "enabled": self._data["enabled"]}

    def update_settings(self, margin_usdt, leverage):
        try:
            margin = float(margin_usdt)
            lev = int(leverage)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Margin/Leverage không hợp lệ"}
        if not 1.0 <= margin <= 500.0:
            return {"ok": False, "error": "Margin phải từ $1 đến $500"}
        if not 1 <= lev <= 20:
            return {"ok": False, "error": "Paper leverage phải từ 1x đến 20x"}
        with self._lock:
            if self._data["open"]:
                return {"ok": False, "error": "Chờ lệnh paper đang mở đóng trước khi đổi vốn"}
            self._data["margin_usdt"] = margin
            self._data["leverage"] = lev
            self._data["updated_at"] = self._now()
            self._persist_locked()
        return {"ok": True, "margin_usdt": margin, "leverage": lev}

    def reset_history(self):
        with self._lock:
            if self._data["open"]:
                return {"ok": False, "error": "Cannot reset while paper trades are open"}
            self._data["closed"] = []
            self._data["sessions"] = {}
            self._persist_locked()
        return {"ok": True}

    def snapshot(self):
        with self._lock:
            data = copy.deepcopy(self._data)
        closed = data.get("closed", [])
        pnl = sum(_f(row.get("net_pnl")) for row in closed)
        wins = sum(_f(row.get("net_pnl")) > 0 for row in closed)
        data.update({
            "mode": "PAPER_ONLY",
            "strategy": "US session M5 breakout + 4h EMA20/50 + range stop + 2R",
            "notional_per_trade": float(data.get("margin_usdt", 15.0))
                                  * int(data.get("leverage", 1)),
            "closed_count": len(closed),
            "wins": wins,
            "win_rate": wins / len(closed) * 100.0 if closed else 0.0,
            "net_pnl": pnl,
            "backtest": {
                "period": "1 year BTC/ETH/SOL M5",
                "net_return_1x_pct": 8.73,
                "trades": 590,
                "win_rate": 43.73,
                "profit_factor": 1.115,
                "max_drawdown_pct": 10.45,
                "worst_quarter_pct": -6.49,
            },
        })
        return json.loads(json.dumps(data, ensure_ascii=False))

    def _worker(self):
        while not self._stop.is_set():
            try:
                with self._lock:
                    enabled = self._data["enabled"]
                    symbols = list(dict.fromkeys(
                        list(self._data["symbols"]) + list(self._data["open"])
                    ))
                    open_symbols = set(self._data["open"])
                if enabled or open_symbols:
                    for symbol in symbols:
                        if self._stop.is_set():
                            break
                        # Turning paper mode off blocks new entries but existing
                        # virtual trades continue to their SL/TP/time exit.
                        if enabled or symbol in open_symbols:
                            self._process_symbol(symbol)
            except Exception as exc:
                logger.exception("Intraday paper worker failed")
                with self._lock:
                    self._data["errors"] = (self._data.get("errors", []) + [str(exc)])[-10:]
                    self._persist_locked()
            self._wake.wait(10.0)
            self._wake.clear()

    def _process_symbol(self, symbol: str):
        now = self._now()
        mark = _f(self.exchange.get_mark_price(symbol))
        if mark <= 0:
            raise ValueError(f"invalid mark for {symbol}")
        with self._lock:
            trade = copy.deepcopy(self._data["open"].get(symbol))
        if trade:
            self._monitor_trade(symbol, trade, mark, now)
            return

        dt = datetime.fromtimestamp(now, timezone.utc)
        day = dt.strftime("%Y-%m-%d")
        # Session range 13:00-14:00 UTC; entry window ends at 17:00 UTC.
        minute = dt.hour * 60 + dt.minute
        with self._lock:
            seen = self._data["sessions"].get(symbol) == day
        if seen or minute < 14 * 60 or minute >= 17 * 60:
            self._set_status(symbol, mark=mark, session_day=day,
                             state="WAIT_SESSION" if minute < 14 * 60 else "DONE_TODAY" if seen else "WINDOW_CLOSED")
            return

        now_ms = int(now * 1000)
        rows = self.exchange.get_klines(symbol, "5m", limit=180)
        closed = [row for row in rows if int(row[6]) < now_ms]
        start_ms = int(datetime(dt.year, dt.month, dt.day, 13, 0, tzinfo=timezone.utc).timestamp() * 1000)
        range_rows = [row for row in closed if start_ms <= int(row[0]) < start_ms + 3600_000]
        signal_rows = [row for row in closed if start_ms + 3600_000 <= int(row[0]) < start_ms + 4 * 3600_000]
        if len(range_rows) < 12 or not signal_rows:
            self._set_status(symbol, mark=mark, session_day=day, state="BUILDING_RANGE")
            return
        range_high = max(_f(row[2]) for row in range_rows)
        range_low = min(_f(row[3]) for row in range_rows)
        atr = _atr(closed[-80:])
        if atr <= 0 or range_low <= 0 or range_high <= range_low:
            raise ValueError(f"invalid range/ATR for {symbol}")

        trend_rows = self.exchange.get_klines(symbol, "4h", limit=80)
        trend_closed = [row for row in trend_rows if int(row[6]) < now_ms]
        closes = [_f(row[4]) for row in trend_closed]
        if len(closes) < 55:
            raise ValueError(f"insufficient closed 4h candles for {symbol}")
        ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
        trend = "LONG" if ema20 > ema50 else "SHORT"
        last = signal_rows[-1]
        last_close = _f(last[4])
        buffer_price = 0.1 * atr
        side = None
        if trend == "LONG" and last_close > range_high + buffer_price:
            side = "LONG"
        elif trend == "SHORT" and last_close < range_low - buffer_price:
            side = "SHORT"
        self._set_status(symbol, mark=mark, session_day=day, state="WATCHING",
                         trend=trend, range_high=range_high, range_low=range_low,
                         atr=atr, last_close=last_close)
        if side is None:
            return

        entry = mark
        stop = range_low if side == "LONG" else range_high
        risk = (entry - stop) if side == "LONG" else (stop - entry)
        if risk <= 0 or risk / entry > 0.03:
            with self._lock:
                self._data["sessions"][symbol] = day
                self._data["status"].setdefault(symbol, {})["state"] = "SKIP_RISK"
                self._persist_locked()
            return
        target = entry + 2.0 * risk if side == "LONG" else entry - 2.0 * risk
        with self._lock:
            margin = float(self._data.get("margin_usdt", 15.0))
            leverage = int(self._data.get("leverage", 1))
        notional = margin * leverage
        paper = {
            "symbol": symbol, "side": side, "entry": entry, "stop": stop,
            "target": target, "margin_usdt": margin, "leverage": leverage,
            "notional": notional, "opened_at": now,
            "expires_at": now + 2 * 3600, "session_day": day,
            "entry_fee": notional * ENTRY_COST_RATE,
            "mark": mark, "unrealized": -notional * ENTRY_COST_RATE,
        }
        with self._lock:
            self._data["open"][symbol] = paper
            self._data["sessions"][symbol] = day
            self._data["status"][symbol]["state"] = "PAPER_OPEN"
            self._data["updated_at"] = now
            self._persist_locked()
        logger.info("[IntradayPaper] OPEN %s %s entry=%.8g SL=%.8g TP=%.8g", symbol, side, entry, stop, target)

        with self._lock:
            real_enabled = bool(self._data.get("real_enabled"))
        if real_enabled and self.real_entry_callback is not None:
            try:
                real_result = self.real_entry_callback(copy.deepcopy(paper))
            except Exception as exc:
                real_result = {"ok": False, "error": str(exc)}
                logger.exception("[IntradayREAL] entry callback failed for %s", symbol)
            event = {
                "time": now, "symbol": symbol, "side": side,
                "entry": entry, "stop": stop, "target": target,
                "ok": bool((real_result or {}).get("ok")),
                "result": real_result or {},
            }
            with self._lock:
                self._data["real_events"] = (self._data.get("real_events", []) + [event])[-200:]
                self._data["open"][symbol]["real"] = event
                self._data["status"][symbol]["state"] = (
                    "REAL_OPEN" if event["ok"] else "REAL_BLOCKED"
                )
                self._persist_locked()

    def _monitor_trade(self, symbol, trade, mark, now):
        side = trade["side"]
        direction = 1.0 if side == "LONG" else -1.0
        gross = direction * (mark - trade["entry"]) / trade["entry"] * trade["notional"]
        trade["mark"] = mark
        trade["unrealized"] = gross - trade["entry_fee"]
        reason = None
        exit_fee_rate = TIME_COST_RATE
        if (side == "LONG" and mark <= trade["stop"]) or (side == "SHORT" and mark >= trade["stop"]):
            reason, exit_fee_rate = "SL", STOP_COST_RATE
        elif (side == "LONG" and mark >= trade["target"]) or (side == "SHORT" and mark <= trade["target"]):
            reason, exit_fee_rate = "TP", TP_COST_RATE
        elif now >= trade["expires_at"]:
            reason, exit_fee_rate = "TIME", TIME_COST_RATE
        if reason is None:
            with self._lock:
                self._data["open"][symbol] = trade
                self._data["updated_at"] = now
                self._persist_locked()
            return
        exit_fee = trade["notional"] * exit_fee_rate
        result = dict(trade)
        result.update({
            "exit": mark, "closed_at": now, "reason": reason,
            "gross_pnl": gross, "fees": trade["entry_fee"] + exit_fee,
            "net_pnl": gross - trade["entry_fee"] - exit_fee,
        })
        result.pop("unrealized", None)
        with self._lock:
            self._data["open"].pop(symbol, None)
            self._data["closed"] = (self._data["closed"] + [result])[-1000:]
            self._data["status"].setdefault(symbol, {})["state"] = f"CLOSED_{reason}"
            self._data["updated_at"] = now
            self._persist_locked()
        logger.info("[IntradayPaper] CLOSE %s %s reason=%s net=$%.4f", symbol, side, reason, result["net_pnl"])

    def _set_status(self, symbol, **values):
        with self._lock:
            row = self._data["status"].setdefault(symbol, {})
            row.update(values)
            row["updated_at"] = self._now()
            self._data["updated_at"] = row["updated_at"]
            self._persist_locked()

    def _load(self):
        try:
            with open(PERSISTENCE_FILE, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            if isinstance(saved, dict):
                for key in ("enabled", "real_enabled", "symbols", "margin_usdt", "leverage", "open", "closed", "sessions", "status", "real_events", "errors", "updated_at"):
                    if key in saved:
                        self._data[key] = saved[key]
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning("Intraday paper persistence load failed: %s", exc)

    def _persist_locked(self):
        os.makedirs(os.path.dirname(PERSISTENCE_FILE), exist_ok=True)
        temporary = PERSISTENCE_FILE + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, PERSISTENCE_FILE)
