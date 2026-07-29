# ============================================================
# WS PRICE MONITOR — Phát hiện dump nhanh cho SCAN engine
# Mục đích: wake up scan_engine sớm khi giá pullback/dump mạnh
#           thay vì đợi 60s interval cứng
#
# CHỈ dành cho scan logic — KHÔNG đụng pump radar.
# Dùng chung WS price feed đã có (price_ws_streamer trong bot.py),
# không mở thêm WS connection mới.
# ============================================================
import logging
import time
from collections import deque
from threading import Lock
from typing import Optional, Callable

logger = logging.getLogger(__name__)


# ── Cấu hình mặc định ────────────────────────────────────────────────────────
_DEFAULT_WINDOW_SEC   = 30     # cửa sổ thời gian tính % thay đổi
_DEFAULT_DROP_PCT     = 2.5    # drop >= 2.5% trong 30s → nghi ngờ pullback
_DEFAULT_BOUNCE_PCT   = 2.0    # bounce (pump) >= 2.0% trong 30s → có thể bắt breakout
_DEFAULT_COOLDOWN_SEC = 90     # 90s không trigger lại cùng 1 coin


class ScanPriceMonitor:
    """
    Lắng nghe giá realtime từ WS feed.
    Phát hiện:
      1. DUMP nhanh  (drop >= drop_pct% trong window_sec)  → signal LONG pullback
      2. BOUNCE nhanh (bounce >= bounce_pct trong window_sec) → signal breakout

    Cách dùng:
        monitor = ScanPriceMonitor(symbols=["BEATUSDT", "XRPUSDT", ...])
        monitor.set_callback(on_signal)   # hàm nhận (symbol, direction, chg_pct)

        # Trong WS on_message:
        monitor.on_price_tick(symbol, price)

        # Trong scan_engine — thay vì time.sleep(60):
        triggered = monitor.wait_for_signal(timeout=60)
        if triggered:
            symbol, direction, chg_pct = triggered
            # fast scan coin đó ngay
    """

    def __init__(self,
                 symbols: list,
                 window_sec: float   = _DEFAULT_WINDOW_SEC,
                 drop_pct: float     = _DEFAULT_DROP_PCT,
                 bounce_pct: float   = _DEFAULT_BOUNCE_PCT,
                 cooldown_sec: float = _DEFAULT_COOLDOWN_SEC):

        self.symbols      = set(symbols)
        self.window_sec   = window_sec
        self.drop_pct     = drop_pct
        self.bounce_pct   = bounce_pct
        self.cooldown_sec = cooldown_sec

        # {symbol: deque of (timestamp, price)}
        self._history: dict = {}
        # {symbol: last_trigger_ts}
        self._cooldowns: dict = {}
        self._lock = Lock()

        # Queue để truyền signal sang scan_engine (thread khác)
        self._pending_signal: Optional[tuple] = None   # (symbol, direction, chg_pct)
        self._signal_event   = __import__("threading").Event()

        # Optional callback
        self._callback: Optional[Callable] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def set_symbols(self, symbols: list):
        """Cập nhật danh sách coin cần theo dõi (gọi được bất kỳ lúc nào)."""
        with self._lock:
            self.symbols = set(symbols)

    def add_symbol(self, symbol: str):
        with self._lock:
            self.symbols.add(symbol)

    def set_callback(self, fn: Callable):
        """fn(symbol: str, direction: str, chg_pct: float)"""
        self._callback = fn

    def on_price_tick(self, symbol: str, price: float):
        """
        Gọi mỗi khi nhận tick giá từ WS on_message.
        Cực nhẹ: O(n) trên deque nhỏ, không block WS thread.
        """
        if symbol not in self.symbols:
            return

        now = time.time()

        with self._lock:
            if symbol not in self._history:
                self._history[symbol] = deque(maxlen=300)  # tối đa 5 phút × 1tick/s
            self._history[symbol].append((now, price))

            # Lấy giá cách đây window_sec giây
            cutoff = now - self.window_sec
            old_ticks = [(ts, p) for ts, p in self._history[symbol] if ts <= cutoff]
            if not old_ticks:
                return  # chưa đủ lịch sử

            base_price = old_ticks[-1][1]
            if base_price <= 0:
                return

            chg_pct = (price - base_price) / base_price * 100

            # Cooldown check
            last_trigger = self._cooldowns.get(symbol, 0)
            if now - last_trigger < self.cooldown_sec:
                return

            # Phát hiện dump (pullback → cơ hội LONG)
            if chg_pct <= -self.drop_pct:
                self._cooldowns[symbol] = now
                direction = "LONG"
                self._fire_signal(symbol, direction, chg_pct)

            # Phát hiện bounce/breakout (cơ hội LONG breakout hoặc SHORT reject)
            elif chg_pct >= self.bounce_pct:
                self._cooldowns[symbol] = now
                direction = "BOUNCE"
                self._fire_signal(symbol, direction, chg_pct)

    def wait_for_signal(self, timeout: float) -> Optional[tuple]:
        """
        Block tối đa `timeout` giây, trả về signal sớm nếu có.
        Trả về: (symbol, direction, chg_pct) hoặc None nếu hết giờ.

        Dùng để thay thế time.sleep(60) trong scan_engine:
            result = monitor.wait_for_signal(timeout=60)
            if result:
                sym, direction, chg = result
                logger.info(f"[Monitor] Wake up! {sym} {direction} {chg:.1f}%")
                # fast scan sym ngay
        """
        fired = self._signal_event.wait(timeout=timeout)
        if fired:
            self._signal_event.clear()
            with self._lock:
                sig = self._pending_signal
                self._pending_signal = None
            return sig
        return None

    def reset_cooldown(self, symbol: str):
        """Xóa cooldown của 1 coin (dùng sau khi đã scan xong)."""
        with self._lock:
            self._cooldowns.pop(symbol, None)

    def get_price_change(self, symbol: str) -> Optional[float]:
        """Trả về % thay đổi giá trong window_sec vừa qua. None nếu chưa đủ data."""
        with self._lock:
            hist = self._history.get(symbol)
            if not hist:
                return None
            now    = time.time()
            cutoff = now - self.window_sec
            old    = [(ts, p) for ts, p in hist if ts <= cutoff]
            if not old:
                return None
            base = old[-1][1]
            cur  = hist[-1][1]
            return (cur - base) / base * 100 if base > 0 else None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fire_signal(self, symbol: str, direction: str, chg_pct: float):
        """Gọi trong lock — thông báo cho wait_for_signal + callback."""
        logger.info(
            f"[ScanMonitor] ⚡ {symbol} {direction} {chg_pct:+.1f}% "
            f"trong {self.window_sec}s — wake up scan"
        )
        self._pending_signal = (symbol, direction, chg_pct)
        self._signal_event.set()

        if self._callback:
            try:
                self._callback(symbol, direction, chg_pct)
            except Exception as e:
                logger.debug(f"[ScanMonitor] callback error: {e}")


# ── Singleton — dùng chung giữa bot.py và scan_engine ────────────────────────
_monitor_instance: Optional[ScanPriceMonitor] = None

def get_scan_monitor(symbols: list = None, **kwargs) -> ScanPriceMonitor:
    """
    Lấy (hoặc tạo) ScanPriceMonitor singleton.
    Gọi lần đầu với symbols để khởi tạo.
    Các lần sau gọi không cần tham số để lấy instance.
    """
    global _monitor_instance
    if _monitor_instance is None:
        if symbols is None:
            symbols = []
        _monitor_instance = ScanPriceMonitor(symbols=symbols, **kwargs)
        logger.info(f"[ScanMonitor] Initialized for {len(symbols)} coins")
    elif symbols:
        _monitor_instance.set_symbols(symbols)
    return _monitor_instance
