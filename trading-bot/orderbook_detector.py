# ============================================================
# ORDER BOOK PRESSURE DETECTOR
# Phát hiện dev đang xả hàng qua order book real-time
# ============================================================
# Logic:
#   - Subscribe Binance WS depth stream cho pump coins
#   - Mỗi update: tính ask_pressure vs bid_pressure
#   - Khi ask wall tăng đột biến + bid wall mỏng = dev đang xả
#   - Kết hợp với WS spike → xác nhận đỉnh pump
#
# Signals (0-100):
#   ask_dominance   : ask_vol / (ask_vol + bid_vol) — càng cao càng bearish
#   wall_ratio      : ask wall lớn nhất / bid wall lớn nhất
#   spread_pct      : spread % — khi spread rộng = liquidity thấp = dễ thao túng
#   imbalance_score : tổng hợp → >= 60 = xác nhận đỉnh
# ============================================================
import logging
import time
import threading
import json
from collections import deque, defaultdict
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
OB_DEPTH_LEVELS      = 20     # số levels order book theo dõi
OB_WINDOW_SEC        = 15     # cửa sổ lịch sử để so sánh
OB_ASK_DOM_THRESHOLD = 0.62   # ask chiếm >= 62% tổng volume → bearish
OB_WALL_RATIO_MIN    = 1.8    # ask wall / bid wall >= 1.8 → bán áp đảo
OB_IMBALANCE_MIN     = 55     # score >= 55 → xác nhận áp lực bán


# ─────────────────────────────────────────────────────────────
# DATA CLASS
# ─────────────────────────────────────────────────────────────
class OBSnapshot:
    """Snapshot order book tại 1 thời điểm."""
    __slots__ = ["ts", "ask_vol", "bid_vol", "best_ask",
                 "best_bid", "ask_wall", "bid_wall", "spread_pct",
                 "ask_dominance", "wall_ratio", "imbalance_score"]

    def __init__(self, bids: list, asks: list, ts: float = None):
        self.ts = ts or time.time()

        # Top N levels
        bid_levels = sorted([(float(p), float(q)) for p, q in bids],
                            key=lambda x: -x[0])[:OB_DEPTH_LEVELS]
        ask_levels = sorted([(float(p), float(q)) for p, q in asks],
                            key=lambda x: x[0])[:OB_DEPTH_LEVELS]

        if not bid_levels or not ask_levels:
            self.ask_vol = self.bid_vol = 0.0
            self.best_ask = self.best_bid = 0.0
            self.ask_wall = self.bid_wall = 0.0
            self.spread_pct = 0.0
            self.ask_dominance = 0.5
            self.wall_ratio = 1.0
            self.imbalance_score = 0
            return

        self.best_bid = bid_levels[0][0]
        self.best_ask = ask_levels[0][0]
        self.bid_vol  = sum(q for _, q in bid_levels)
        self.ask_vol  = sum(q for _, q in ask_levels)

        # Wall: level có volume lớn nhất
        self.bid_wall = max(q for _, q in bid_levels)
        self.ask_wall = max(q for _, q in ask_levels)

        # Spread
        mid = (self.best_bid + self.best_ask) / 2
        self.spread_pct = (self.best_ask - self.best_bid) / mid * 100 if mid > 0 else 0

        # Derived metrics
        total = self.ask_vol + self.bid_vol
        self.ask_dominance = self.ask_vol / total if total > 0 else 0.5
        self.wall_ratio    = self.ask_wall / self.bid_wall if self.bid_wall > 0 else 1.0

        # Imbalance score (0-100)
        self.imbalance_score = self._calc_score()

    def _calc_score(self) -> int:
        score = 0

        # Ask dominance (40đ)
        if self.ask_dominance >= 0.70:
            score += 40
        elif self.ask_dominance >= 0.65:
            score += 30
        elif self.ask_dominance >= OB_ASK_DOM_THRESHOLD:
            score += 20
        elif self.ask_dominance >= 0.55:
            score += 10

        # Wall ratio (35đ)
        if self.wall_ratio >= 3.0:
            score += 35
        elif self.wall_ratio >= 2.5:
            score += 28
        elif self.wall_ratio >= OB_WALL_RATIO_MIN:
            score += 20
        elif self.wall_ratio >= 1.4:
            score += 10

        # Spread (15đ) — spread rộng = thin book = dễ thao túng
        if self.spread_pct >= 0.3:
            score += 15
        elif self.spread_pct >= 0.15:
            score += 8
        elif self.spread_pct >= 0.05:
            score += 4

        # Bid wall nhỏ hơn ask wall nhiều (10đ)
        if self.ask_wall > self.bid_wall * 2.5:
            score += 10
        elif self.ask_wall > self.bid_wall * 1.8:
            score += 5

        return min(score, 100)

    def is_bearish(self) -> bool:
        return self.imbalance_score >= OB_IMBALANCE_MIN

    def summary(self) -> str:
        return (f"imb={self.imbalance_score} "
                f"ask_dom={self.ask_dominance:.0%} "
                f"wall={self.wall_ratio:.1f}× "
                f"spread={self.spread_pct:.3f}%")


# ─────────────────────────────────────────────────────────────
# ORDER BOOK TRACKER
# ─────────────────────────────────────────────────────────────
class OrderBookTracker:
    """
    Subscribe Binance Futures depth WS cho pump coins.
    Cung cấp imbalance score real-time.
    """

    def __init__(self, base_ws: str = "wss://fstream.binance.com"):
        self.base_ws   = base_ws
        self._snapshots: Dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self._latest:    Dict[str, OBSnapshot] = {}
        self._symbols:   set = set()
        self._lock       = threading.Lock()
        self._ws_thread  = None
        self._running    = False

    def add_symbols(self, symbols: list):
        """Thêm coin cần theo dõi order book."""
        with self._lock:
            new = set(s.upper() for s in symbols) - self._symbols
            if not new:
                return
            self._symbols.update(new)
            logger.info(f"[OB] Added symbols: {new}")

        # Restart WS với danh sách mới
        self._restart_ws()

    def remove_symbol(self, symbol: str):
        with self._lock:
            self._symbols.discard(symbol.upper())
            self._latest.pop(symbol.upper(), None)
        self._restart_ws()

    def get_snapshot(self, symbol: str) -> Optional[OBSnapshot]:
        return self._latest.get(symbol.upper())

    def get_imbalance_score(self, symbol: str) -> int:
        snap = self.get_snapshot(symbol)
        return snap.imbalance_score if snap else 0

    def is_bearish(self, symbol: str) -> bool:
        snap = self.get_snapshot(symbol)
        return snap.is_bearish() if snap else False

    def get_trend(self, symbol: str, window: int = 5) -> str:
        """
        Xu hướng imbalance score trong N snapshot gần nhất.
        Returns: 'INCREASING' | 'STABLE' | 'DECREASING'
        """
        hist = list(self._snapshots.get(symbol.upper(), []))
        if len(hist) < window:
            return "STABLE"
        scores = [s.imbalance_score for s in hist[-window:]]
        delta  = scores[-1] - scores[0]
        if delta >= 10:
            return "INCREASING"   # áp lực bán đang tăng
        if delta <= -10:
            return "DECREASING"   # áp lực bán đang giảm
        return "STABLE"

    # ── WebSocket ─────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._restart_ws()
        logger.info("[OB] OrderBookTracker started")

    def stop(self):
        self._running = False
        logger.info("[OB] OrderBookTracker stopped")

    def _restart_ws(self):
        """Dừng WS thread cũ, khởi động lại với symbol list mới."""
        if self._ws_thread and self._ws_thread.is_alive():
            self._running = False
            time.sleep(0.3)
            self._running = True

        self._ws_thread = threading.Thread(
            target=self._ws_loop, daemon=True
        )
        self._ws_thread.start()

    def _ws_loop(self):
        """WS loop — tự reconnect khi mất kết nối."""
        import websocket as ws_lib

        while self._running:
            with self._lock:
                syms = list(self._symbols)

            if not syms:
                time.sleep(2)
                continue

            # Build stream URL: mỗi coin 1 depth stream 20 levels
            streams = "/".join(
                [f"{s.lower()}@depth20@500ms" for s in syms]
            )
            url = f"{self.base_ws}/stream?streams={streams}"

            def on_message(wsapp, message):
                try:
                    data    = json.loads(message)
                    payload = data.get("data", data)
                    stream  = data.get("stream", "")
                    # Extract symbol từ stream name: "bankusdt@depth20@500ms"
                    sym = stream.split("@")[0].upper() if "@" in stream else ""
                    if not sym:
                        return

                    bids = payload.get("b", [])
                    asks = payload.get("a", [])
                    if not bids or not asks:
                        return

                    snap = OBSnapshot(bids, asks)
                    with self._lock:
                        self._latest[sym]    = snap
                        self._snapshots[sym].append(snap)

                    if snap.is_bearish():
                        logger.debug(
                            f"[OB] {sym} BEARISH: {snap.summary()}"
                        )
                except Exception as e:
                    logger.debug(f"[OB] on_message error: {e}")

            def on_error(wsapp, error):
                logger.debug(f"[OB] WS error: {error}")

            def on_close(wsapp, *args):
                logger.debug("[OB] WS closed")

            try:
                wsapp = ws_lib.WebSocketApp(
                    url,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                wsapp.run_forever(ping_interval=20, ping_timeout=8)
            except Exception as e:
                logger.debug(f"[OB] WS exception: {e}")

            if self._running:
                time.sleep(3)


# ─────────────────────────────────────────────────────────────
# SINGLETON — dùng chung trong toàn bot
# ─────────────────────────────────────────────────────────────
_ob_tracker: Optional[OrderBookTracker] = None


def get_ob_tracker(base_ws: str = "wss://fstream.binance.com") -> OrderBookTracker:
    global _ob_tracker
    if _ob_tracker is None:
        _ob_tracker = OrderBookTracker(base_ws)
        _ob_tracker.start()
    return _ob_tracker


# ─────────────────────────────────────────────────────────────
# HELPER: 3-tầng pump top confirmation
# ─────────────────────────────────────────────────────────────
def confirm_pump_top(symbol: str,
                     spike_pct: float,
                     pump_score: int,
                     ob_tracker: OrderBookTracker = None) -> dict:
    """
    Xác nhận đỉnh pump qua 3 tầng — dùng trong _ws_spike_full_analysis.

    Returns:
        {
            "confirmed": bool,
            "confidence": int,      # 0-100
            "tier1_spike": bool,    # giá tăng đột biến
            "tier2_ob": bool,       # order book bearish
            "tier3_signal": bool,   # pump detector score đủ
            "ob_score": int,
            "reason": str
        }
    """
    if ob_tracker is None:
        ob_tracker = get_ob_tracker()

    # Tầng 1: WS Spike (đã pass khi gọi hàm này)
    tier1 = spike_pct >= 3.0

    # Tầng 2: Order Book Pressure
    ob_snap  = ob_tracker.get_snapshot(symbol)
    ob_score = ob_snap.imbalance_score if ob_snap else 0
    ob_trend = ob_tracker.get_trend(symbol)
    # Bearish nếu score >= 50 HOẶC đang tăng mạnh
    tier2 = ob_score >= 50 or (ob_score >= 35 and ob_trend == "INCREASING")

    # Tầng 3: Pump detector signal đủ mạnh
    tier3 = pump_score >= 55

    tiers_passed = sum([tier1, tier2, tier3])

    # Confidence: tất cả 3 tầng = 90+, 2 tầng = 65-75, 1 tầng = 40
    if tiers_passed == 3:
        confidence = min(70 + ob_score // 10 + pump_score // 10, 95)
    elif tiers_passed == 2:
        confidence = 55 + ob_score // 10
    else:
        confidence = 35

    # Confirmed khi ít nhất 2/3 tầng pass
    confirmed = tiers_passed >= 2

    reasons = []
    if tier1: reasons.append(f"Spike+{spike_pct:.1f}%")
    if tier2: reasons.append(f"OB={ob_score}({ob_trend})")
    if tier3: reasons.append(f"PumpScore={pump_score}")
    if not tier2: reasons.append(f"OB_WEAK={ob_score}")
    if not tier3: reasons.append(f"Score_LOW={pump_score}")

    return {
        "confirmed":     confirmed,
        "confidence":    confidence,
        "tiers_passed":  tiers_passed,
        "tier1_spike":   tier1,
        "tier2_ob":      tier2,
        "tier3_signal":  tier3,
        "ob_score":      ob_score,
        "ob_trend":      ob_trend,
        "reason":        " | ".join(reasons),
    }
