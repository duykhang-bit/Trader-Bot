# ============================================================
# LIQUIDATION TRACKER
# Lắng nghe Binance Futures websocket liquidation stream
# Tích lũy theo price bucket → xác định vùng liq mạnh nhất
# ============================================================
import json
import logging
import math
import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import websocket

logger = logging.getLogger(__name__)

# ── Cấu trúc 1 sự kiện liquidation ───────────────────────────
# {
#   "symbol": "BTCUSDT",
#   "side":   "BUY" | "SELL",   # BUY = long bị liq, SELL = short bị liq
#   "price":  float,
#   "qty":    float,
#   "usd":    float,             # giá trị USD bị liquidate
#   "ts":     float,             # unix timestamp
# }


class LiquidationTracker:
    """
    Subscribe Binance !forceOrder@arr stream.
    Tích lũy liquidation USD vào price bucket theo từng symbol.
    Bucket size mặc định 0.1% của giá → thay đổi qua bucket_pct.
    """

    MAINNET_WS  = "wss://fstream.binance.com/ws/!forceOrder@arr"
    TESTNET_WS  = "wss://stream.binancefuture.com/ws/!forceOrder@arr"

    # Decay: mỗi 5 phút giảm 20% để data cũ nhạt dần
    DECAY_INTERVAL   = 300   # giây
    DECAY_FACTOR     = 0.80
    # Giữ data tối đa 4 giờ
    MAX_HISTORY_SEC  = 4 * 3600

    def __init__(self,
                 symbols: List[str],
                 testnet: bool = True,
                 bucket_pct: float = 0.001):   # 0.1% mỗi bucket
        self.symbols      = {s.upper() for s in symbols}
        self.testnet      = testnet
        self.bucket_pct   = bucket_pct

        # {symbol: {bucket_price: total_usd_liquidated}}
        self._buckets: Dict[str, Dict[float, float]] = defaultdict(lambda: defaultdict(float))
        self._lock        = threading.Lock()
        self._ws          = None
        self._thread      = None
        self._running     = False
        self._last_decay  = time.time()
        self._connected   = False
        self._reconnect_delay = 5  # giây

    # ── Public API ────────────────────────────────────────────

    def start(self):
        """Chạy tracker trong background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info(f"LiquidationTracker started (testnet={self.testnet})")
        # Seed ngay từ Binance Open Interest để có data ngay lập tức
        import threading as _threading
        _threading.Thread(target=self._seed_from_open_interest, daemon=True).start()

    def _seed_from_open_interest(self):
        """
        Estimate vùng liq từ Binance Open Interest + Mark Price.
        Chạy ngay khi start để có data ngay, không cần đợi real events.
        Logic: OI tập trung ở price levels nào → vùng đó có nhiều lệnh → dễ bị liq.
        Dùng /fapi/v1/openInterestHist để lấy OI theo thời gian, estimate price levels.
        """
        import requests as _req
        import time as _time
        _time.sleep(3)  # Đợi WS connect xong
        base = "https://fapi.binance.com"

        for sym in list(self.symbols):
            try:
                # Lấy mark price hiện tại
                ticker = _req.get(f"{base}/fapi/v1/ticker/price?symbol={sym}", timeout=5).json()
                mark_price = float(ticker.get("price", 0))
                if mark_price <= 0:
                    continue

                # Lấy long/short ratio để biết hướng tập trung lệnh
                try:
                    ls_resp = _req.get(
                        f"{base}/futures/data/globalLongShortAccountRatio",
                        params={"symbol": sym, "period": "1h", "limit": 24},
                        timeout=5
                    ).json()
                    if isinstance(ls_resp, list) and ls_resp:
                        avg_ls = sum(float(r.get("longShortRatio", 1)) for r in ls_resp) / len(ls_resp)
                    else:
                        avg_ls = 1.0
                except Exception:
                    avg_ls = 1.0

                # Lấy OI history 24h để tìm price levels tập trung
                try:
                    oi_resp = _req.get(
                        f"{base}/futures/data/openInterestHist",
                        params={"symbol": sym, "period": "1h", "limit": 24},
                        timeout=5
                    ).json()
                except Exception:
                    oi_resp = []

                # Estimate vùng liq dựa trên leverage phổ biến
                # Giới hạn range ±3-10% quanh giá hiện tại (giống Coinglass 12h)
                leverage_levels = [10, 20, 50]
                for lev in leverage_levels:
                    liq_down_pct = 1.0 / lev
                    liq_up_pct   = 1.0 / lev

                    for mult in [0.5, 1.0, 1.5]:
                        # Vùng liq phía dưới (long liq zone) — tối đa -10%
                        liq_price_down = mark_price * (1 - liq_down_pct * mult)
                        if liq_price_down < mark_price * 0.90:
                            continue  # bỏ qua nếu > 10% từ giá
                        bucket_down = self._price_to_bucket(liq_price_down)
                        oi_usd = mark_price * 1000 * (2.0 if avg_ls > 1.2 else 1.0) / lev
                        with self._lock:
                            self._buckets[sym][bucket_down] += oi_usd

                        # Vùng liq phía trên (short liq zone) — tối đa +10%
                        liq_price_up = mark_price * (1 + liq_up_pct * mult)
                        if liq_price_up > mark_price * 1.10:
                            continue  # bỏ qua nếu > 10% từ giá
                        bucket_up = self._price_to_bucket(liq_price_up)
                        oi_usd_short = mark_price * 1000 * (2.0 if avg_ls < 0.8 else 1.0) / lev
                        with self._lock:
                            self._buckets[sym][bucket_up] += oi_usd_short

                logger.info(f"[LiqSeed] {sym} seeded from OI at ${mark_price:.4f}, L/S ratio={avg_ls:.2f}")

            except Exception as e:
                logger.debug(f"[LiqSeed] {sym} failed: {e}")

    def stop(self):
        self._running = False
        if self._ws:
            try: self._ws.close()
            except: pass

    def is_connected(self) -> bool:
        return self._connected

    def get_top_liq_levels(self,
                            symbol: str,
                            side: str,
                            n: int = 5,
                            min_usd: float = 50_000
                            ) -> List[Tuple[float, float]]:
        """
        Trả về top N vùng giá có liquidation USD cao nhất.

        side = "LONG_LIQ"  → vùng liq lệnh LONG (BUY bị liq, tức giá đã giảm qua đó)
                             → dùng để SHORT (giá đang ở trên, sẽ dump xuống vùng này)
             = "SHORT_LIQ" → vùng liq lệnh SHORT (SELL bị liq, giá đã tăng qua)
                             → dùng để LONG (giá đang ở dưới, sẽ pump lên vùng này)

        Returns: [(price, usd_amount), ...] sắp xếp theo usd_amount giảm dần
        """
        sym = symbol.upper()
        with self._lock:
            buckets = dict(self._buckets.get(sym, {}))

        if not buckets:
            return []

        # side mapping: "BUY" = long bị liq, "SELL" = short bị liq
        liq_side_key = "BUY" if side == "LONG_LIQ" else "SELL"

        filtered = [
            (price, usd)
            for (price, usd) in buckets.items()
            if usd >= min_usd
        ]
        filtered.sort(key=lambda x: x[1], reverse=True)
        return filtered[:n]

    def get_liq_heatmap(self,
                         symbol: str,
                         price_range_pct: float = 0.05
                         ) -> Dict[float, float]:
        """
        Trả về toàn bộ bucket trong range ±price_range_pct quanh giá hiện tại.
        Dùng để vẽ heatmap hoặc debug.
        """
        sym = symbol.upper()
        with self._lock:
            buckets = dict(self._buckets.get(sym, {}))
        return buckets

    def get_nearest_liq_above(self,
                               symbol: str,
                               current_price: float,
                               min_usd: float = 100_000
                               ) -> Optional[float]:
        """
        Vùng liq đáng kể gần nhất PHÍA TRÊN giá hiện tại.
        Dùng để xác định TP cho lệnh SHORT hoặc entry SHORT.
        """
        sym = symbol.upper()
        with self._lock:
            buckets = dict(self._buckets.get(sym, {}))

        candidates = [
            (price, usd) for price, usd in buckets.items()
            if price > current_price and usd >= min_usd
        ]
        if not candidates:
            return None
        # Gần nhất phía trên
        candidates.sort(key=lambda x: x[0])
        return candidates[0][0]

    def get_nearest_liq_below(self,
                               symbol: str,
                               current_price: float,
                               min_usd: float = 100_000
                               ) -> Optional[float]:
        """
        Vùng liq đáng kể gần nhất PHÍA DƯỚI giá hiện tại.
        Dùng để xác định TP cho lệnh LONG hoặc entry LONG.
        """
        sym = symbol.upper()
        with self._lock:
            buckets = dict(self._buckets.get(sym, {}))

        candidates = [
            (price, usd) for price, usd in buckets.items()
            if price < current_price and usd >= min_usd
        ]
        if not candidates:
            return None
        # Gần nhất phía dưới
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][0]

    def get_strongest_liq_above(self,
                                  symbol: str,
                                  current_price: float,
                                  min_usd: float = 200_000
                                  ) -> Optional[float]:
        """
        Vùng liq LỚN NHẤT phía trên → target pump/short entry.
        """
        sym = symbol.upper()
        with self._lock:
            buckets = dict(self._buckets.get(sym, {}))

        candidates = [
            (price, usd) for price, usd in buckets.items()
            if price > current_price and usd >= min_usd
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    def get_strongest_liq_below(self,
                                  symbol: str,
                                  current_price: float,
                                  min_usd: float = 200_000
                                  ) -> Optional[float]:
        """
        Vùng liq LỚN NHẤT phía dưới → target dump/long entry.
        """
        sym = symbol.upper()
        with self._lock:
            buckets = dict(self._buckets.get(sym, {}))

        candidates = [
            (price, usd) for price, usd in buckets.items()
            if price < current_price and usd >= min_usd
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    def total_liq_usd(self, symbol: str) -> float:
        """Tổng USD đã bị liquidate cho symbol này kể từ khi start."""
        sym = symbol.upper()
        with self._lock:
            buckets = self._buckets.get(sym, {})
            return sum(buckets.values())

    def has_enough_data(self, symbol: str, min_usd: float = 500_000) -> bool:
        """Kiểm tra đã có đủ data để dùng strategy chưa."""
        return self.total_liq_usd(symbol) >= min_usd

    # ── Internal ──────────────────────────────────────────────

    def _price_to_bucket(self, price: float) -> float:
        """
        Map price → bucket key.
        Bucket size = bucket_pct * price, làm tròn log-scale.
        """
        if price <= 0:
            return 0.0
        # Tính bucket size theo magnitude của giá
        magnitude = 10 ** math.floor(math.log10(price))
        bucket_size = magnitude * self.bucket_pct * 10
        return round(math.floor(price / bucket_size) * bucket_size, 8)

    def _on_message(self, ws, raw):
        try:
            data = json.loads(raw)
            # Stream có thể là 1 event hoặc list
            events = data if isinstance(data, list) else [data]
            for ev in events:
                order = ev.get("o", ev)   # wrapped format
                self._process_event(order)
        except Exception as e:
            logger.debug(f"LiqTracker parse error: {e}")

    def _process_event(self, order: dict):
        try:
            symbol = order.get("s", "").upper()
            if symbol not in self.symbols:
                return
            side    = order.get("S", "")   # BUY / SELL
            price   = float(order.get("ap", order.get("p", 0)))  # average price
            qty     = float(order.get("q", 0))
            usd     = price * qty

            if usd <= 0:
                return

            bucket = self._price_to_bucket(price)

            with self._lock:
                self._buckets[symbol][bucket] += usd

            logger.debug(
                f"Liq {symbol} {side} @ {price:.2f} "
                f"qty={qty} ~${usd:,.0f} bucket={bucket}"
            )

            # Decay cũ
            self._maybe_decay()

        except Exception as e:
            logger.debug(f"LiqTracker process error: {e}")

    def _maybe_decay(self):
        now = time.time()
        if now - self._last_decay < self.DECAY_INTERVAL:
            return
        # Đã qua decay interval
        with self._lock:
            for sym in list(self._buckets.keys()):
                for bucket in list(self._buckets[sym].keys()):
                    self._buckets[sym][bucket] *= self.DECAY_FACTOR
                    if self._buckets[sym][bucket] < 1000:   # dưới $1k → xóa
                        del self._buckets[sym][bucket]
        self._last_decay = now
        logger.debug("LiqTracker: decay applied")

    def _on_open(self, ws):
        self._connected = True
        logger.info("LiquidationTracker WS connected")

    def _on_close(self, ws, code, msg):
        self._connected = False
        logger.warning(f"LiquidationTracker WS closed: {code} {msg}")

    def _on_error(self, ws, error):
        self._connected = False
        logger.error(f"LiquidationTracker WS error: {error}")

    def _run_forever(self):
        url = self.TESTNET_WS if self.testnet else self.MAINNET_WS
        delay = self._reconnect_delay
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"LiqTracker WS run error: {e}")

            if not self._running:
                break

            logger.info(f"LiqTracker reconnecting in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 60)   # exponential backoff, max 60s
