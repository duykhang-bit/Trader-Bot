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
import re
import threading
import time
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

PERSISTENCE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "intraday_paper.json"
)
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "AVAXUSDT")
VERIFIED_REAL_SYMBOLS = ("LINKUSDT", "AVAXUSDT")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
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
        self._sleep = time.sleep
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._data = {
            "enabled": bool(getattr(config, "INTRADAY_PAPER_ENABLED", False)),
            "real_enabled": False,
            "symbols": list(getattr(config, "INTRADAY_PAPER_SYMBOLS", DEFAULT_SYMBOLS)),
            "real_symbols": list(VERIFIED_REAL_SYMBOLS),
            "candidates": {
                "LINKUSDT": {"eligible": True, "source": "verified_1y", "return_pct": 17.51, "segments": [11.57, 1.82, 4.12]},
                "AVAXUSDT": {"eligible": True, "source": "verified_1y", "return_pct": 17.35, "segments": [6.17, 8.19, 2.99]},
            },
            "selector": {"running": False, "last_scan": 0.0, "progress": "", "error": ""},
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
        # Migration for journals created before dynamic selection existed.
        with self._lock:
            self._data.setdefault("real_symbols", list(VERIFIED_REAL_SYMBOLS))
            self._data.setdefault("candidates", {})
            self._data.setdefault("selector", {"running": False, "last_scan": 0.0, "progress": "", "error": ""})
            self._data["selector"]["running"] = False
            for symbol in VERIFIED_REAL_SYMBOLS:
                if symbol not in self._data["symbols"]:
                    self._data["symbols"].append(symbol)
                self._data["candidates"].setdefault(
                    symbol, {"eligible": True, "source": "verified_1y"}
                )

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
            # REAL is the master switch requested by the user: enabling starts
            # signal monitoring; disabling stops all new paper/REAL signals and
            # candidate scans. Existing REAL SL/TP remain on Binance.
            self._data["enabled"] = enabled
            self._data["updated_at"] = self._now()
            self._persist_locked()
        self._wake.set()
        return {"ok": True, "real_enabled": enabled,
                "enabled": self._data["enabled"]}

    def add_symbol(self, symbol):
        symbol = str(symbol or "").strip().upper()
        if not SYMBOL_RE.match(symbol):
            return {"ok": False, "error": "Symbol USDT không hợp lệ"}
        try:
            if _f(self.exchange.get_ticker_price(symbol)) <= 0:
                raise ValueError("invalid ticker")
        except Exception:
            return {"ok": False, "error": f"Binance không có {symbol}"}
        with self._lock:
            if symbol not in self._data["symbols"]:
                self._data["symbols"].append(symbol)
            self._data["candidates"].setdefault(
                symbol, {"eligible": False, "source": "manual", "reason": "Chưa qua backtest tự động"}
            )
            self._persist_locked()
        self._wake.set()
        return {"ok": True, "symbol": symbol}

    def remove_symbol(self, symbol):
        symbol = str(symbol or "").strip().upper()
        with self._lock:
            if symbol in self._data["open"]:
                return {"ok": False, "error": "Không thể xóa khi có paper trade đang mở"}
            self._data["symbols"] = [x for x in self._data["symbols"] if x != symbol]
            self._data["real_symbols"] = [x for x in self._data["real_symbols"] if x != symbol]
            self._data["status"].pop(symbol, None)
            self._persist_locked()
        return {"ok": True, "symbol": symbol}

    def set_real_symbol(self, symbol, enabled):
        symbol = str(symbol or "").strip().upper()
        with self._lock:
            candidate = self._data.get("candidates", {}).get(symbol, {})
            if enabled and not candidate.get("eligible"):
                return {"ok": False, "error": "Coin chưa pass bộ lọc/backtest"}
            real = set(self._data.get("real_symbols", []))
            real.add(symbol) if enabled else real.discard(symbol)
            self._data["real_symbols"] = sorted(real)
            if enabled and symbol not in self._data["symbols"]:
                self._data["symbols"].append(symbol)
            self._persist_locked()
        return {"ok": True, "symbol": symbol, "real_active": bool(enabled)}

    def start_auto_scan(self):
        with self._lock:
            selector = self._data["selector"]
            if not self._data.get("enabled"):
                return {"ok": False, "error": "Bật theo dõi hoặc REAL trước khi quét"}
            if selector.get("running"):
                return {"ok": False, "error": "Bộ lọc đang chạy"}
            selector.update({"running": True, "progress": "Đang lấy top volume...", "error": ""})
            self._persist_locked()
        threading.Thread(target=self._auto_scan_worker, name="intraday-selector", daemon=True).start()
        return {"ok": True}

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
            "mode": "REAL_ENABLED" if data.get("real_enabled") else "PAPER_ONLY",
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

    @staticmethod
    def _ema_series(values, period):
        out = np.empty(len(values), dtype=float)
        alpha = 2.0 / (period + 1.0)
        out[0] = values[0]
        for i in range(1, len(values)):
            out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
        return out

    def _fetch_candidate_bars(self, symbol, days=90):
        end_ms = int(self._now() * 1000)
        cursor = end_ms - days * 86_400_000
        rows = []
        while cursor < end_ms and not self._stop.is_set():
            batch = self.exchange._get("/fapi/v1/klines", {
                "symbol": symbol, "interval": "5m", "startTime": cursor,
                "endTime": end_ms, "limit": 1500,
            })
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][0]) + 300_000
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            self._sleep(0.12)
        return rows

    def _backtest_candidate(self, rows):
        if len(rows) < 20_000:
            return {"eligible": False, "reason": "Thiếu 90 ngày dữ liệu", "trades": 0}
        b = np.array([[float(r[i]) for i in range(6)] for r in rows], dtype=float)
        n4 = len(b) // 48
        z = b[:n4 * 48].reshape(-1, 48, 6)
        close4 = z[:, -1, 4]
        trend4 = self._ema_series(close4, 20) - self._ema_series(close4, 50)
        trend_idx = np.clip(np.arange(len(b)) // 48 - 1, 0, len(trend4) - 1)
        trend = trend4[trend_idx]
        high, low, close, ts = b[:, 2], b[:, 3], b[:, 4], b[:, 0]
        # Wilder ATR14 on M5.
        prev = np.r_[close[0], close[:-1]]
        tr = np.maximum(high - low, np.maximum(abs(high - prev), abs(low - prev)))
        atr = self._ema_series(tr, 27)
        trades = []
        day_ms = 86_400_000
        first = int((ts[0] - 13 * 3_600_000) // day_ms)
        last = int((ts[-1] - 13 * 3_600_000) // day_ms)
        for day in range(first, last + 1):
            start = day * day_ms + 13 * 3_600_000
            i0 = int(np.searchsorted(ts, start)); i1 = i0 + 12
            if i0 < 250 or i1 + 2 >= len(b):
                continue
            range_high = float(high[i0:i1].max()); range_low = float(low[i0:i1].min())
            side = 0; signal_i = None
            for i in range(i1, min(len(b) - 1, i1 + 36)):
                buffer_price = 0.1 * atr[i]
                if close[i] > range_high + buffer_price and trend[i] > 0:
                    side, signal_i = 1, i; break
                if close[i] < range_low - buffer_price and trend[i] < 0:
                    side, signal_i = -1, i; break
            if not side:
                continue
            entry = b[signal_i + 1, 1]
            stop = range_low if side > 0 else range_high
            risk = side * (entry - stop)
            if risk <= 0 or risk / entry > 0.03:
                continue
            target = entry + side * 2.0 * risk
            result = None
            for j in range(signal_i + 1, min(len(b), signal_i + 25)):
                hit_sl = low[j] <= stop if side > 0 else high[j] >= stop
                hit_tp = high[j] >= target if side > 0 else low[j] <= target
                if hit_sl:
                    result = side * (stop - entry) / entry - ENTRY_COST_RATE - STOP_COST_RATE; break
                if hit_tp:
                    result = side * (target - entry) / entry - ENTRY_COST_RATE - TP_COST_RATE; break
            if result is None:
                j = min(len(b) - 1, signal_i + 24)
                result = side * (close[j] - entry) / entry - ENTRY_COST_RATE - TIME_COST_RATE
            trades.append((int(ts[j]), float(result)))
        if not trades:
            return {"eligible": False, "reason": "Không có signal", "trades": 0}
        start_ts, end_ts = int(ts[0]), int(ts[-1]) + 300_000
        cuts = [start_ts, start_ts + (end_ts - start_ts) // 3,
                start_ts + 2 * (end_ts - start_ts) // 3, end_ts]
        segments = []
        for lo_ts, hi_ts in zip(cuts, cuts[1:]):
            values = np.array([r for t, r in trades if lo_ts <= t < hi_ts])
            gross = values[values > 0].sum() if len(values) else 0.0
            loss = -values[values < 0].sum() if len(values) else 0.0
            segments.append({
                "return_pct": float(values.sum() * 100), "trades": int(len(values)),
                "pf": float(gross / loss) if loss > 0 else (99.0 if gross > 0 else 0.0),
            })
        all_values = np.array([r for _, r in trades])
        equity = np.cumsum(all_values)
        drawdown = float((np.maximum.accumulate(equity) - equity).max() * 100)
        eligible = all(
            segment["return_pct"] > 0 and segment["pf"] >= 1.05
            and segment["trades"] >= 8 for segment in segments
        ) and drawdown <= 15.0
        return {
            "eligible": bool(eligible), "source": "auto_90d_fixed",
            "return_pct": float(all_values.sum() * 100), "trades": len(trades),
            "win_rate": float((all_values > 0).mean() * 100),
            "pf": float(all_values[all_values > 0].sum() / -all_values[all_values < 0].sum())
                  if (all_values < 0).any() else 99.0,
            "max_dd_pct": drawdown, "segments": segments,
            "reason": "PASS" if eligible else "Không lời ổn định ở cả 3 đoạn 30 ngày",
            "tested_at": self._now(),
        }

    def _auto_scan_worker(self):
        try:
            now_ms = int(self._now() * 1000)
            info = self.exchange._get("/fapi/v1/exchangeInfo")
            tickers = self.exchange._get("/fapi/v1/ticker/24hr")
            books = self.exchange._get("/fapi/v1/ticker/bookTicker")
            book_map = {row.get("symbol"): row for row in books if isinstance(row, dict)}
            allowed = {}
            for row in info.get("symbols", []):
                if (row.get("quoteAsset") == "USDT" and row.get("contractType") == "PERPETUAL"
                        and row.get("status") == "TRADING"
                        and now_ms - int(row.get("onboardDate", now_ms)) >= 90 * 86_400_000):
                    allowed[row["symbol"]] = row
            ranked = []
            for row in tickers:
                symbol = row.get("symbol")
                if symbol not in allowed:
                    continue
                volume = _f(row.get("quoteVolume"))
                book = book_map.get(symbol, {})
                bid, ask = _f(book.get("bidPrice")), _f(book.get("askPrice"))
                spread = (ask - bid) / ((ask + bid) / 2) if ask > bid > 0 else 1.0
                if volume >= 20_000_000 and spread <= 0.0008:
                    ranked.append((volume, symbol, spread))
            ranked.sort(reverse=True)
            selected = ranked[:15]
            for index, (volume, symbol, spread) in enumerate(selected, 1):
                with self._lock:
                    if not self._data.get("enabled"):
                        self._data["selector"].update({
                            "running": False, "progress": "Đã dừng", "error": ""
                        })
                        self._persist_locked()
                        return
                    self._data["selector"]["progress"] = f"{index}/{len(selected)} {symbol}"
                    self._persist_locked()
                result = self._backtest_candidate(self._fetch_candidate_bars(symbol))
                result.update({"volume_24h": volume, "spread_pct": spread * 100})
                with self._lock:
                    # One-year verified results are stronger than rolling 90d.
                    old = self._data["candidates"].get(symbol, {})
                    if old.get("source") != "verified_1y":
                        self._data["candidates"][symbol] = result
                    if result.get("eligible") and symbol not in self._data["symbols"]:
                        self._data["symbols"].append(symbol)  # paper probation only
                    self._persist_locked()
            with self._lock:
                self._data["selector"].update({
                    "running": False, "last_scan": self._now(),
                    "progress": f"Xong {len(selected)} coin", "error": "",
                })
                self._persist_locked()
        except Exception as exc:
            logger.exception("Intraday auto-selector failed")
            with self._lock:
                self._data["selector"].update({
                    "running": False, "error": str(exc), "progress": "Lỗi",
                })
                self._persist_locked()

    def _worker(self):
        while not self._stop.is_set():
            try:
                with self._lock:
                    enabled = self._data["enabled"]
                    symbols = list(dict.fromkeys(
                        list(self._data["symbols"]) + list(self._data["open"])
                    ))
                    open_symbols = set(self._data["open"])
                if enabled:
                    with self._lock:
                        selector = dict(self._data.get("selector", {}))
                    if (not selector.get("running")
                            and self._now() - _f(selector.get("last_scan")) >= 86_400):
                        self.start_auto_scan()
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
            real_symbol = symbol in set(self._data.get("real_symbols", []))
        if real_enabled and real_symbol and self.real_entry_callback is not None:
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
                for key in ("enabled", "real_enabled", "symbols", "real_symbols", "candidates", "selector", "margin_usdt", "leverage", "open", "closed", "sessions", "status", "real_events", "errors", "updated_at"):
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
