"""Neutral futures grid that copies the range/grid count of a Binance grid bot.

Binance does not expose an API to create its own grid bots, so this engine
places the grid with normal LIMIT orders on the user's account.

Mechanics (classic neutral grid, one-way mode):
- Levels split [lower, upper] (arithmetic or geometric, like the source bot).
- Start: BUY LIMIT at every level below price, SELL LIMIT at every level above;
  the level nearest the price stays empty.
- BUY filled at level i  -> SELL LIMIT at level i+1.
  SELL filled at level j -> BUY LIMIT at level j-1.
- Grid count is reduced (same range) when the margin cannot fund the source
  bot's count at Binance's minimum order notional.

Safety (checked every cycle, even while the bot is paused):
- Stop + market-close when mark leaves [lower, upper] by more than stop_pct.
- Stop + market-close when grid PnL (Binance realized+fees+funding plus
  unrealized) <= -max_loss_usdt.
- Stop + market-close when the live position differs from what the grid's own
  fills explain (someone/something else touched the symbol).
- Leverage <= 5x, margin <= 50% of available balance, symbol must be flat with
  no open orders at start. While paused no new orders are placed.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import time
import uuid

logger = logging.getLogger(__name__)

PERSISTENCE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "grid_copy.json"
)
CLIENT_PREFIX = "gridc_"
MAX_LEVERAGE = 5
MIN_ORDER_NOTIONAL_BUFFER = 1.15  # place each order >= 1.15 x exchange min notional
CYCLE_SECONDS = 5
MAX_AVAILABLE_FRACTION = 0.75  # keep 25% free for fees/funding/other exits
REQUIREMENT_REFRESH_S = 300
BALANCE_REFRESH_S = 1800  # 30 phút; còn cập nhật ngay sau mỗi lần Copy/Dừng
MIN_GRIDS = 5
MAX_STEP_PCT = 3.0
MISMATCH_CONFIRMATIONS = 3
TERMINAL = {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}


def _f(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def build_levels(lower, upper, count, kind="ARITHMETIC"):
    lower, upper, count = float(lower), float(upper), int(count)
    if kind == "GEO" or kind == "GEOMETRIC":
        ratio = (upper / lower) ** (1.0 / count)
        return [lower * ratio ** i for i in range(count + 1)]
    step = (upper - lower) / count
    return [lower + step * i for i in range(count + 1)]


def _step_pct(lower, upper, count, kind):
    lv = build_levels(lower, upper, count, kind)
    return max((lv[i + 1] - lv[i]) / lv[i] for i in range(count)) * 100


def select_grid(lower, upper, grids, kind, price, step, decimals, min_notional, total_notional):
    """Pick the largest grid count (<= source) that the notional can fund.

    A copied grid must stay a real grid: >= MIN_GRIDS levels and each step
    <= MAX_STEP_PCT (or the source bot's own step if wider). Same qty on every
    level; each order clears the exchange minimum at the lowest level; resting
    orders sum <= funded notional * 1.25. Returns (chosen|None, min_count, max_step).
    """
    max_step = max(MAX_STEP_PCT, _step_pct(lower, upper, grids, kind) + 1e-9)
    min_count = next((c for c in range(MIN_GRIDS, grids + 1)
                      if _step_pct(lower, upper, c, kind) <= max_step), grids)
    for count in range(grids, min_count - 1, -1):
        levels = build_levels(lower, upper, count, kind)
        nearest_raw = min(range(len(levels)), key=lambda i: abs(levels[i] - price))
        qty = math.floor((total_notional / count) / price / step + 1e-12) * step
        qty_min = math.ceil(min_notional * MIN_ORDER_NOTIONAL_BUFFER / min(levels) / step - 1e-12) * step
        qty = round(max(qty, qty_min), int(decimals))
        resting = qty * sum(x for i, x in enumerate(levels) if i != nearest_raw)
        if qty > 0 and resting <= total_notional * 1.25:
            return (count, levels, qty), min_count, max_step
    return None, min_count, max_step


def required_notional(lower, upper, grids, kind, price, step, decimals, min_notional):
    """Smallest margin*leverage that select_grid accepts (None if never)."""
    total = max(min_notional * MIN_ORDER_NOTIONAL_BUFFER * MIN_GRIDS / 1.25, 1.0)
    for _ in range(400):
        if select_grid(lower, upper, grids, kind, price, step, decimals, min_notional, total)[0]:
            return round(total, 2)
        total *= 1.02
    return None


def estimate_requirement(bot, price, filters):
    """Requirement for copying one Binance grid bot at the current price."""
    try:
        lower, upper = float(bot.get("lower")), float(bot.get("upper"))
        grids = int(bot.get("grids") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "Thiếu thông số"}
    kind = bot.get("type") or "ARITHMETIC"
    if price <= 0 or filters is None:
        return {"ok": False, "reason": "Không có giá/bộ lọc"}
    if grids < MIN_GRIDS:
        return {"ok": False, "reason": f"Bot gốc < {MIN_GRIDS} lưới"}
    if not (lower < price < upper):
        return {"ok": False, "reason": "Giá đã ra khỏi range"}
    step, decimals, min_notional = filters
    need = required_notional(lower, upper, grids, kind, price, step, decimals, min_notional)
    if need is None:
        return {"ok": False, "reason": "Range quá rộng"}
    _, min_count, max_step = select_grid(lower, upper, grids, kind, price, step, decimals, min_notional, need)
    chosen = select_grid(lower, upper, grids, kind, price, step, decimals, min_notional, need)[0]
    return {"ok": True, "need_notional": need, "grids_at_min": chosen[0] if chosen else min_count,
            "max_step": round(max_step, 2)}


class GridCopyService:
    def __init__(self, exchange, *, notifier=None, pnl_callback=None,
                 is_trading_enabled=lambda: False, on_symbols_changed=None, bots_provider=None,
                 now_fn=time.time, persistence_file=PERSISTENCE_FILE):
        self.exchange = exchange
        self.notifier = notifier
        self.pnl_callback = pnl_callback
        self.is_trading_enabled = is_trading_enabled
        self.on_symbols_changed = on_symbols_changed
        self.bots_provider = bots_provider
        self._req_ts = 0.0
        self._bal_ts = 0.0
        self._now = now_fn
        self._file = persistence_file
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._data = {"grids": {}, "history": [], "events": [], "requirements": {}, "last_attempt": {}, "balance": {},
                      "settings": {"margin_usdt": 10.0, "leverage": 5, "stop_pct": 2.0, "max_loss_usdt": 2.0}}
        self._load()

    # ---------------- persistence ----------------
    def _load(self):
        try:
            with open(self._file, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                self._data.update({k: v for k, v in saved.items() if k in self._data})
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("[GridCopy] load failed: %s", exc)

    def _persist_locked(self):
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            tmp = self._file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False)
            os.replace(tmp, self._file)
        except Exception as exc:
            logger.warning("[GridCopy] persist failed: %s", exc)

    def _event_locked(self, message, symbol=""):
        self._data["events"].append({"ts": self._now(), "symbol": symbol, "msg": message})
        self._data["events"] = self._data["events"][-100:]

    def _notify(self, text):
        try:
            if self.notifier is not None:
                self.notifier.telegram.send(text)
        except Exception:
            pass

    def snapshot(self):
        with self._lock:
            snap = copy.deepcopy(self._data)
        for grid in snap["grids"].values():
            grid["open_orders"] = len(grid.get("orders") or {})
            grid.pop("orders", None)
        snap["history"] = snap["history"][-30:]
        snap["events"] = snap["events"][-20:]
        return snap

    def active_symbols(self):
        with self._lock:
            return set(self._data["grids"].keys())

    def _symbols_changed(self):
        if self.on_symbols_changed is not None:
            try:
                self.on_symbols_changed(self.active_symbols())
            except Exception:
                pass

    # ---------------- lifecycle ----------------
    def start(self):
        self._symbols_changed()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="grid-copy", daemon=True)
            self._thread.start()
            return self._thread

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _worker(self):
        while not self._stop.is_set():
            for symbol in list(self.active_symbols()):
                try:
                    self.cycle(symbol)
                except Exception as exc:
                    logger.warning("[GridCopy] %s cycle error: %s", symbol, exc)
                    with self._lock:
                        grid = self._data["grids"].get(symbol)
                        if grid is not None:
                            grid["error"] = str(exc)[:200]
                            self._persist_locked()
            self._settle_history()
            if self._now() - self._bal_ts >= BALANCE_REFRESH_S:
                self._bal_ts = self._now()
                try:
                    self.refresh_balance()
                except Exception as exc:
                    logger.warning("[GridCopy] balance refresh failed: %s", exc)
            if self._now() - self._req_ts >= REQUIREMENT_REFRESH_S:
                self._req_ts = self._now()
                try:
                    self.refresh_requirements()
                except Exception as exc:
                    logger.warning("[GridCopy] requirement refresh failed: %s", exc)
            self._stop.wait(CYCLE_SECONDS)

    def refresh_balance(self):
        """Live account breakdown so the web shows why margin is short."""
        acc = self.exchange._get("/fapi/v2/account", signed=True) or {}
        bal = {
            "ts": self._now(),
            "equity": round(_f(acc.get("totalMarginBalance")), 2),
            "available": round(_f(acc.get("availableBalance")), 2),
            "orders_margin": round(_f(acc.get("totalOpenOrderInitialMargin")), 2),
            "positions_margin": round(_f(acc.get("totalPositionInitialMargin")), 2),
        }
        bal["grid_cap"] = round(bal["available"] * MAX_AVAILABLE_FRACTION, 2)
        with self._lock:
            self._data["balance"] = bal
            req = self._data.get("requirements")
            if isinstance(req, dict):
                req["available"] = bal["available"]
            self._persist_locked()
        return bal

    def refresh_requirements(self):
        """Estimate the margin each listed Binance bot needs (shown on the web)."""
        bots = list(self.bots_provider() or []) if self.bots_provider else []
        if not bots:
            return {}
        ex = self.exchange
        info = ex._get("/fapi/v1/exchangeInfo") or {}
        filters = {}
        wanted = {b.get("symbol") for b in bots}
        for item in info.get("symbols", []):
            if item.get("symbol") not in wanted:
                continue
            step, decimals, min_notional = 1.0, 0, 5.0
            for f in item.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f.get("stepSize", 1))
                    decimals = 0 if step >= 1 else len(str(step).rstrip("0").split(".")[-1])
                elif f.get("filterType") == "MIN_NOTIONAL":
                    min_notional = float(f.get("notional", 5.0))
            filters[item["symbol"]] = (step, decimals, min_notional)
        marks = {}
        for row in ex._get("/fapi/v1/premiumIndex") or []:
            marks[row.get("symbol")] = _f(row.get("markPrice"))
        result = {}
        for bot in bots:
            sym = bot.get("symbol")
            est = estimate_requirement(bot, marks.get(sym, 0.0), filters.get(sym))
            if est.get("ok"):
                est["margin_at"] = {str(l): round(est["need_notional"] / l, 2) for l in range(1, MAX_LEVERAGE + 1)}
            result[str(bot.get("id"))] = est
        try:
            available = round(_f(ex.get_account_balance()), 2)
        except Exception:
            available = None
        with self._lock:
            self._data["requirements"] = {"ts": self._now(), "available": available, "bots": result}
            self._persist_locked()
        return result

    # ---------------- start ----------------
    def update_settings(self, margin_usdt, leverage, stop_pct, max_loss_usdt):
        try:
            values = {"margin_usdt": round(float(margin_usdt), 2), "leverage": int(leverage),
                      "stop_pct": float(stop_pct), "max_loss_usdt": float(max_loss_usdt)}
        except (TypeError, ValueError):
            return {"ok": False, "error": "Giá trị không hợp lệ"}
        if not (1 <= values["margin_usdt"] <= 500):
            return {"ok": False, "error": "Margin 1–500 USDT"}
        if not (1 <= values["leverage"] <= MAX_LEVERAGE):
            return {"ok": False, "error": f"Lev 1–{MAX_LEVERAGE}x"}
        if not (0.5 <= values["stop_pct"] <= 20):
            return {"ok": False, "error": "Dừng ngoài range 0.5–20%"}
        if not (0.5 <= values["max_loss_usdt"] <= 500):
            return {"ok": False, "error": "Max lỗ 0.5–500 USDT"}
        with self._lock:
            self._data["settings"] = values
            self._persist_locked()
        return {"ok": True, "settings": values}

    def start_grid(self, **kwargs):
        """Start a grid and remember the outcome so the web can show it."""
        result = self._start_grid(**kwargs)
        self._bal_ts = 0.0  # refresh balance on the next cycle
        with self._lock:
            self._data["last_attempt"] = {
                "ts": self._now(), "symbol": str(kwargs.get("symbol") or ""),
                "margin": kwargs.get("margin_usdt"), "leverage": kwargs.get("leverage"),
                "ok": bool(result.get("ok")), "error": result.get("error", ""),
            }
            self._persist_locked()
        return result

    def _start_grid(self, *, symbol, lower, upper, grids, kind="ARITHMETIC",
                    margin_usdt, leverage, stop_pct=2.0, max_loss_usdt=None,
                    source=None):
        symbol = str(symbol or "").strip().upper()
        try:
            lower, upper = float(lower), float(upper)
            grids = int(grids)
            margin = float(margin_usdt)
            lev = int(leverage)
            stop_pct = float(stop_pct)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Thông số không hợp lệ"}
        max_loss = float(max_loss_usdt) if max_loss_usdt not in (None, "") else round(margin * 0.3, 2)
        if not symbol.endswith("USDT"):
            return {"ok": False, "error": "Chỉ hỗ trợ coin USDT-M"}
        if not (0 < lower < upper):
            return {"ok": False, "error": "Range không hợp lệ"}
        if not (1 <= lev <= MAX_LEVERAGE):
            return {"ok": False, "error": f"Leverage grid 1–{MAX_LEVERAGE}x (đã giới hạn để tránh cháy)"}
        if not (1 <= margin <= 500):
            return {"ok": False, "error": "Margin 1–500 USDT"}
        if not (0.5 <= stop_pct <= 20) or max_loss <= 0:
            return {"ok": False, "error": "Stop ngoài range 0.5–20%, max loss > 0"}
        if not self.is_trading_enabled():
            return {"ok": False, "error": "Bot đang Pause — bấm Start Bot trước"}
        with self._lock:
            if symbol in self._data["grids"]:
                return {"ok": False, "error": f"{symbol} đang có grid chạy"}
        ex = self.exchange
        price = _f(ex.get_mark_price(symbol))
        if price <= 0:
            return {"ok": False, "error": "Không lấy được giá"}
        if not (lower < price < upper):
            return {"ok": False, "error": f"Giá hiện tại {price:.8g} nằm ngoài range {lower:.8g}–{upper:.8g} của bot gốc"}
        if ex.get_position(symbol):
            return {"ok": False, "error": f"{symbol} đang có vị thế — grid cần coin trống"}
        if ex._get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True):
            return {"ok": False, "error": f"{symbol} đang có lệnh chờ — grid cần coin trống"}
        available = _f(ex.get_account_balance())
        if available <= 0 or margin > available * MAX_AVAILABLE_FRACTION:
            return {"ok": False, "error": (f"Margin grid phải ≤{MAX_AVAILABLE_FRACTION*100:.0f}% số dư khả dụng: "
                                           f"tối đa ${available * MAX_AVAILABLE_FRACTION:.2f} (khả dụng ${available:.2f})")}
        step, max_qty, decimals, min_notional = ex.get_qty_precision(symbol)
        step, min_notional = float(step), float(min_notional or 5.0)
        total_notional = margin * lev
        if grids < MIN_GRIDS:
            return {"ok": False, "error": f"Bot gốc chỉ có {grids} lưới, cần ít nhất {MIN_GRIDS}"}
        chosen, min_count, max_step = select_grid(lower, upper, grids, kind, price, step,
                                                  decimals, min_notional, total_notional)
        if chosen is None:
            need_notional = required_notional(lower, upper, grids, kind, price, step, decimals, min_notional)
            if need_notional is None:
                return {"ok": False, "error": "Vốn không đủ: range này quá rộng so với giá hiện tại, không copy được"}
            need_margin = need_notional / lev
            return {"ok": False, "error": (
                f"Vốn không đủ cho grid đúng nghĩa: range này cần ≥{min_count} lưới "
                f"(mỗi nấc ≤{max_step:.1f}%) → cần margin ≥ ${need_margin:.2f} ở {lev}x"
                + (f" (hoặc ${need_notional / MAX_LEVERAGE:.2f} ở {MAX_LEVERAGE}x)" if lev < MAX_LEVERAGE else "")
                + ". Chọn bot range hẹp hơn hoặc thêm vốn.")}
        count, levels, qty = chosen
        effective = int(ex.set_leverage(symbol, lev) or 0)
        if effective <= 0:
            return {"ok": False, "error": "Không xác nhận được leverage"}
        rounded = [float(ex._round_price(x, symbol)) for x in levels]
        if len(set(rounded)) != len(rounded):
            return {"ok": False, "error": "Lưới quá dày so với bước giá của coin"}
        nearest = min(range(len(rounded)), key=lambda i: abs(rounded[i] - price))
        desired = {}
        for i, level in enumerate(rounded):
            if i == nearest:
                continue
            desired[str(i)] = "BUY" if level < price else "SELL"
        grid = {
            "id": uuid.uuid4().hex[:6], "symbol": symbol, "lower": lower, "upper": upper,
            "kind": "GEO" if kind in ("GEO", "GEOMETRIC") else "ARITHMETIC",
            "source_grids": grids, "grids": count, "levels": rounded, "qty": qty,
            "margin": margin, "leverage": effective, "stop_pct": stop_pct,
            "max_loss": max_loss, "start_price": price, "started_at": self._now(),
            "source": source or {}, "desired": desired, "orders": {},
            "filled_pos": 0.0, "fills": 0, "buys": 0, "sells": 0, "seq": 0,
            "mismatch": 0, "status": "RUNNING", "error": "",
            "realized": 0.0, "fee": 0.0, "unrealized": 0.0, "pnl": 0.0, "mark": price,
        }
        with self._lock:
            self._data["grids"][symbol] = grid
            self._event_locked(
                f"START grid {symbol} {lower:.8g}–{upper:.8g} · {count} lưới (bot gốc {grids}) · "
                f"qty {qty} · ${margin:.2f} × {effective}x · stop ±{stop_pct}% · max lỗ ${max_loss:.2f}", symbol)
            self._persist_locked()
        self._symbols_changed()
        self._place_missing(symbol)
        self._notify(f"🔲 <b>GRID START</b> {symbol}\nRange {lower:.8g}–{upper:.8g} · {count} lưới\n"
                     f"Margin ${margin:.2f} × {effective}x · Max lỗ ${max_loss:.2f}")
        with self._lock:
            placed = len(self._data["grids"].get(symbol, {}).get("orders") or {})
        return {"ok": True, "symbol": symbol, "grids": count, "qty": qty,
                "placed": placed, "reduced": count < grids}

    # ---------------- cycle ----------------
    def _client_id(self, grid, level):
        grid["seq"] = int(grid.get("seq", 0)) + 1
        return f"{CLIENT_PREFIX}{grid['id']}_{level}_{grid['seq']}"[:36]

    def _place_missing(self, symbol):
        if not self.is_trading_enabled():
            return
        with self._lock:
            grid = self._data["grids"].get(symbol)
            if grid is None or grid["status"] != "RUNNING":
                return
            todo = [(lvl, side) for lvl, side in grid["desired"].items() if lvl not in grid["orders"]]
        for level, side in todo:
            with self._lock:
                grid = self._data["grids"].get(symbol)
                if grid is None or grid["status"] != "RUNNING":
                    return
                price = grid["levels"][int(level)]
                qty = grid["qty"]
                cid = self._client_id(grid, level)
                self._persist_locked()
            try:
                result = self.exchange.place_limit_order(symbol, side, qty, price, client_order_id=cid)
            except Exception as exc:
                with self._lock:
                    grid = self._data["grids"].get(symbol)
                    if grid is not None:
                        grid["error"] = f"Đặt {side} @{price:.8g} lỗi: {str(exc)[:120]}"
                        self._persist_locked()
                continue
            with self._lock:
                grid = self._data["grids"].get(symbol)
                if grid is None:
                    return
                grid["orders"][level] = {"side": side, "order_id": (result or {}).get("orderId"),
                                         "client_id": cid, "price": price, "qty": qty}
                grid["error"] = ""
                self._persist_locked()

    def cycle(self, symbol):
        with self._lock:
            grid = copy.deepcopy(self._data["grids"].get(symbol))
        if grid is None or grid["status"] != "RUNNING":
            return
        ex = self.exchange
        open_orders = ex._get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True) or []
        open_by_id = {str(o.get("orderId")): o for o in open_orders}
        partial = 0.0
        for level, order in list(grid["orders"].items()):
            live = open_by_id.get(str(order.get("order_id")))
            if live is not None:
                executed = _f(live.get("executedQty"))
                partial += executed if order["side"] == "BUY" else -executed
                continue
            status = ex._get("/fapi/v1/order", {"symbol": symbol, "orderId": order.get("order_id")}, signed=True) or {}
            state = str(status.get("status", "")).upper()
            executed = _f(status.get("executedQty"))
            if state == "FILLED":
                self._on_fill(symbol, level, order, executed or order["qty"])
            elif state in TERMINAL:
                with self._lock:
                    live_grid = self._data["grids"].get(symbol)
                    if live_grid is not None:
                        live_grid["orders"].pop(level, None)
                        if executed > 0:
                            live_grid["filled_pos"] += executed if order["side"] == "BUY" else -executed
                        self._event_locked(f"Lệnh lưới {order['side']} @{order['price']:.8g} bị {state} → đặt lại", symbol)
                        self._persist_locked()
            # Unknown/empty status: retry next cycle.
        self._place_missing(symbol)
        self._risk_check(symbol, partial)

    def _on_fill(self, symbol, level, order, qty):
        with self._lock:
            grid = self._data["grids"].get(symbol)
            if grid is None:
                return
            grid["orders"].pop(level, None)
            grid["desired"].pop(level, None)
            i = int(level)
            if order["side"] == "BUY":
                grid["filled_pos"] += qty
                grid["buys"] += 1
                target, side = i + 1, "SELL"
            else:
                grid["filled_pos"] -= qty
                grid["sells"] += 1
                target, side = i - 1, "BUY"
            grid["fills"] += 1
            if 0 <= target < len(grid["levels"]):
                grid["desired"][str(target)] = side
            grid["last_fill"] = {"ts": self._now(), "side": order["side"], "price": order["price"]}
            self._event_locked(f"Khớp {order['side']} @{order['price']:.8g} → đặt {side} @{grid['levels'][target]:.8g}"
                               if 0 <= target < len(grid["levels"]) else f"Khớp {order['side']} @{order['price']:.8g}", symbol)
            self._persist_locked()

    def _risk_check(self, symbol, partial):
        ex = self.exchange
        live = ex.get_position(symbol)
        amount = _f((live or {}).get("positionAmt"))
        unrealized = _f((live or {}).get("unRealizedProfit"))
        mark = _f((live or {}).get("markPrice")) or _f(ex.get_mark_price(symbol))
        with self._lock:
            grid = self._data["grids"].get(symbol)
            if grid is None:
                return
            start_ms = int(grid["started_at"] * 1000) - 5000
        pnl = {}
        with self._lock:
            grid = self._data["grids"].get(symbol) or {}
            due = self._now() - _f(grid.get("pnl_ts")) >= 30
            if due and grid:
                grid["pnl_ts"] = self._now()
        if due and self.pnl_callback is not None:
            try:
                pnl = self.pnl_callback(symbol, start_ms, int(self._now() * 1000) + 60000) or {}
            except Exception:
                pnl = {}
        with self._lock:
            grid = self._data["grids"].get(symbol)
            if grid is None:
                return
            if pnl:
                grid["realized"] = round(_f(pnl.get("realized")), 6)
                grid["fee"] = round(_f(pnl.get("fee")), 6)
            grid["unrealized"] = round(unrealized, 6)
            grid["mark"] = mark
            grid["position"] = amount
            grid["pnl"] = round(grid["realized"] + grid["fee"] + unrealized, 6)
            expected = grid["filled_pos"] + partial
            tolerance = grid["qty"] * 0.5
            if abs(amount - expected) > tolerance:
                grid["mismatch"] = int(grid.get("mismatch", 0)) + 1
            else:
                grid["mismatch"] = 0
            reason = ""
            band_low = grid["lower"] * (1 - grid["stop_pct"] / 100)
            band_high = grid["upper"] * (1 + grid["stop_pct"] / 100)
            if mark > 0 and (mark < band_low or mark > band_high):
                reason = f"Giá {mark:.8g} ra khỏi range quá {grid['stop_pct']}%"
            elif grid["pnl"] <= -grid["max_loss"]:
                reason = f"Lỗ ${grid['pnl']:.2f} chạm max lỗ ${grid['max_loss']:.2f}"
            elif grid["mismatch"] >= MISMATCH_CONFIRMATIONS:
                reason = f"Vị thế {amount:g} khác số grid khớp {expected:g} (bị can thiệp)"
            self._persist_locked()
        if reason:
            self.stop_grid(symbol, reason)

    # ---------------- stop ----------------
    def stop_grid(self, symbol, reason="Dừng tay trên web"):
        symbol = str(symbol or "").strip().upper()
        with self._lock:
            grid = self._data["grids"].get(symbol)
            if grid is None:
                return {"ok": False, "error": "Không có grid"}
            grid["status"] = "STOPPING"
            grid["stop_reason"] = reason
            orders = list(grid["orders"].values())
            grid_id = grid["id"]
            self._persist_locked()
        ex = self.exchange
        errors = []
        for order in orders:
            try:
                ex._delete("/fapi/v1/order", {"symbol": symbol, "orderId": order.get("order_id")})
            except Exception as exc:
                errors.append(str(exc)[:80])
        # Sweep any grid-owned order that was placed but not recorded.
        try:
            for order in ex._get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True) or []:
                cid = str(order.get("clientOrderId") or "")
                if cid.startswith(f"{CLIENT_PREFIX}{grid_id}_"):
                    ex._delete("/fapi/v1/order", {"symbol": symbol, "orderId": order.get("orderId")})
        except Exception as exc:
            errors.append(str(exc)[:80])
        closed = False
        for _ in range(3):
            try:
                live = ex.get_position(symbol)
                amount = _f((live or {}).get("positionAmt"))
                if amount == 0:
                    closed = True
                    break
                q = abs(amount)
                ex.place_market_order(symbol, "SELL" if amount > 0 else "BUY", q, reduce_only=True)
                time.sleep(0.3)
            except Exception as exc:
                errors.append(str(exc)[:80])
                time.sleep(0.3)
        if not closed:
            with self._lock:
                grid = self._data["grids"].get(symbol)
                if grid is not None:
                    grid["error"] = "KHẨN CẤP: chưa đóng được vị thế grid — " + "; ".join(errors)[:160]
                    grid["status"] = "STOPPING"
                    self._persist_locked()
            self._notify(f"🚨 <b>GRID {symbol}</b> dừng nhưng CHƯA đóng được vị thế. Kiểm tra Binance!")
            return {"ok": False, "error": "Chưa đóng được vị thế, sẽ thử lại"}
        with self._lock:
            grid = self._data["grids"].pop(symbol, None)
            if grid is not None:
                grid.update({"status": "STOPPED", "stopped_at": self._now(), "orders": {},
                             "pnl_final": False, "final_pnl": None})
                self._data["history"].append(grid)
                self._data["history"] = self._data["history"][-100:]
                self._event_locked(f"STOP grid {symbol}: {reason}", symbol)
            self._persist_locked()
        self._symbols_changed()
        self._bal_ts = 0.0
        self._notify(f"🔲 <b>GRID STOP</b> {symbol}\nLý do: {reason}")
        return {"ok": True}

    def retry_stopping(self):
        for symbol, grid in list(self.snapshot()["grids"].items()):
            if grid.get("status") == "STOPPING":
                self.stop_grid(symbol, grid.get("stop_reason") or "Thử đóng lại")

    def _settle_history(self):
        self.retry_stopping()
        if self.pnl_callback is None:
            return
        now = self._now()
        with self._lock:
            todo = [(i, copy.deepcopy(g)) for i, g in enumerate(self._data["history"])
                    if not g.get("pnl_final") and now - _f(g.get("stopped_at")) >= 15
                    and int(g.get("pnl_attempts", 0)) < 10]
        for index, grid in todo:
            try:
                res = self.pnl_callback(grid["symbol"], int(grid["started_at"] * 1000) - 5000,
                                        int(grid["stopped_at"] * 1000) + 120000) or {}
            except Exception:
                res = None
            with self._lock:
                hist = self._data["history"]
                if index >= len(hist) or hist[index].get("id") != grid["id"]:
                    continue
                live = hist[index]
                live["pnl_attempts"] = int(live.get("pnl_attempts", 0)) + 1
                if res is not None and (res.get("records") or live["pnl_attempts"] >= 3):
                    live["final_pnl"] = round(_f(res.get("realized")) + _f(res.get("fee")), 6)
                    live["pnl_final"] = True
                self._persist_locked()
