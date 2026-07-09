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

    def get_best_entry_cluster(self,
                                symbol: str,
                                current_price: float,
                                direction: str,
                                min_usd: float = 30_000,
                                cluster_gap_pct: float = 0.008
                                ) -> dict:
        """
        Tìm cluster tối ưu để vào lệnh dựa theo thanh khoản.

        Logic (theo hình Coinglass heatmap):
        ┌────────────────────────────────────────────────────────┐
        │ SHORT: tìm vùng liq TRÊN giá hiện tại                 │
        │   - Gom các bucket gần nhau (< cluster_gap_pct) thành │
        │     1 cluster → tổng USD lớn hơn                      │
        │   - Nếu nhiều cluster → chọn cluster GẦN NHẤT        │
        │   - Entry = giá THẤP NHẤT trong cluster               │
        │     (dễ khớp nhất, giá chạm đáy cluster là vào)      │
        │                                                        │
        │ LONG: tìm vùng liq DƯỚI giá hiện tại                  │
        │   - Gom cluster tương tự                               │
        │   - Nếu nhiều cluster → chọn cluster GẦN NHẤT        │
        │   - Entry = giá CAO NHẤT trong cluster                 │
        │     (dễ khớp nhất, giá chạm đỉnh cluster là vào)     │
        └────────────────────────────────────────────────────────┘

        Returns:
            {
                "entry":       float,  # giá vào lệnh tối ưu
                "cluster_low": float,  # giá thấp nhất trong cluster
                "cluster_high":float,  # giá cao nhất trong cluster
                "total_usd":   float,  # tổng USD trong cluster
                "n_buckets":   int,    # số bucket trong cluster
                "dist_pct":    float,  # % cách giá hiện tại
                "sl_zone":     float,  # vùng đặt SL (ngoài cluster)
            }
            hoặc None nếu không tìm được
        """
        sym = symbol.upper()
        with self._lock:
            buckets = dict(self._buckets.get(sym, {}))

        if not buckets:
            return None

        # Lọc theo hướng và min_usd
        if direction == "SHORT":
            raw = sorted(
                [(p, u) for p, u in buckets.items() if p > current_price and u >= min_usd],
                key=lambda x: x[0]
            )
        else:  # LONG
            raw = sorted(
                [(p, u) for p, u in buckets.items() if p < current_price and u >= min_usd],
                key=lambda x: x[0], reverse=True
            )

        if not raw:
            return None

        # ── Gom thành clusters ────────────────────────────────
        # Cluster = các bucket liên tiếp có khoảng cách < cluster_gap_pct
        clusters = []
        cur_cluster = [raw[0]]

        for i in range(1, len(raw)):
            prev_price = cur_cluster[-1][0]
            this_price = raw[i][0]
            gap = abs(this_price - prev_price) / prev_price
            if gap <= cluster_gap_pct:
                cur_cluster.append(raw[i])
            else:
                clusters.append(cur_cluster)
                cur_cluster = [raw[i]]
        clusters.append(cur_cluster)

        # ── Tính tổng USD mỗi cluster ─────────────────────────
        cluster_stats = []
        for cl in clusters:
            prices = [p for p, u in cl]
            usds   = [u for p, u in cl]
            total  = sum(usds)
            low    = min(prices)
            high   = max(prices)
            dist   = abs((low if direction == "SHORT" else high) - current_price) / current_price * 100
            cluster_stats.append({
                "buckets":     cl,
                "low":         low,
                "high":        high,
                "total_usd":   total,
                "n_buckets":   len(cl),
                "dist_pct":    dist,
            })

        # ── Chọn cluster tối ưu ───────────────────────────────
        # Ưu tiên: cluster GẦN NHẤT có đủ USD (≥ min_usd * 2)
        # Nếu cluster gần nhất quá nhỏ → lấy cluster lớn nhất USD
        threshold_usd = min_usd * 2

        nearby = [c for c in cluster_stats if c["dist_pct"] <= 8.0]
        if not nearby:
            nearby = cluster_stats  # fallback: lấy tất cả

        # Trong các cluster gần (<8%), ưu tiên:
        # 1. Cluster đầu tiên (gần nhất) nếu đủ USD
        # 2. Nếu không đủ USD → cluster có tổng USD lớn nhất
        best = nearby[0]  # cluster gần nhất (đã sort theo khoảng cách)
        if best["total_usd"] < threshold_usd:
            # Tìm cluster lớn nhất USD trong tầm gần
            best = max(nearby, key=lambda c: c["total_usd"])

        if best["total_usd"] < min_usd:
            return None

        # ── Entry: giá dễ khớp nhất ───────────────────────────
        if direction == "SHORT":
            # Entry tại đáy cluster (giá thấp nhất) — dễ chạm nhất khi pump lên
            entry = best["low"]
            # SL: trên đỉnh cluster + 0.2% buffer
            sl_zone = best["high"] * 1.002
        else:  # LONG
            # Entry tại đỉnh cluster (giá cao nhất) — dễ chạm nhất khi dump xuống
            entry = best["high"]
            # SL: dưới đáy cluster - 0.2% buffer
            sl_zone = best["low"] * 0.998

        dist_pct = abs(entry - current_price) / current_price * 100

        return {
            "entry":        round(entry,    8),
            "cluster_low":  round(best["low"],  8),
            "cluster_high": round(best["high"], 8),
            "total_usd":    round(best["total_usd"], 0),
            "n_buckets":    best["n_buckets"],
            "dist_pct":     round(dist_pct, 2),
            "sl_zone":      round(sl_zone,  8),
        }

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


# ============================================================
# QUANT UTILITIES — Cluster Analysis + Microstructure
# ============================================================

from dataclasses import dataclass, field as dc_field
from typing import List, Dict, Optional, Tuple
import time as _time


@dataclass
class LiqCluster:
    """
    Một cluster thanh khoản — tập hợp nhiều vùng liq gần nhau.

    Quant concept: "Liquidity pools" — vùng giá có nhiều stop loss
    và liquidation orders xếp chồng nhau. Giá thường bị hút về đây
    trước khi đảo chiều (stop hunt / liquidity grab).
    """
    lower: float          # cạnh dưới cluster
    upper: float          # cạnh trên cluster
    peak:  float          # giá có USD liq cao nhất trong cluster
    total_usd: float      # tổng USD trong cluster
    peak_usd:  float      # USD tại peak bucket
    n_buckets: int        # số bucket trong cluster
    age_weight: float     # trọng số tuổi — data mới hơn = trọng số cao hơn


def cluster_liq_zones(
    heatmap: Dict[float, float],
    current_price: float,
    side: str,                    # "SHORT" (phía trên) hoặc "LONG" (phía dưới)
    cluster_gap_pct: float = 0.008,   # gom bucket cách nhau < 0.8%
    min_cluster_usd: float = 50_000,  # cluster phải có tổng >= $50k
    window_12h_weight: bool = True,   # ưu tiên data trong 12h (decay đã tính)
) -> List[LiqCluster]:
    """
    Quant technique: Liquidity Cluster Detection
    ─────────────────────────────────────────────────────────────
    Gom các bucket liq gần nhau thành cluster theo thuật toán
    Single-linkage clustering (dùng trong market microstructure).

    Tư duy quant:
    - Nhiều stop loss xếp gần nhau = "stop loss cluster"
    - Giá thường sweep qua cluster để lấy thanh khoản rồi đảo chiều
    - SHORT: cluster phía TRÊN giá → entry tại LOWER edge (giá chạm vào)
    - LONG:  cluster phía DƯỚI giá → entry tại UPPER edge (giá chạm vào)

    Returns: list LiqCluster sắp xếp theo total_usd giảm dần
    """
    if not heatmap:
        return []

    # Lọc bucket theo side
    if side == "SHORT":
        buckets = sorted(
            [(p, u) for p, u in heatmap.items() if p > current_price and u >= 5_000],
            key=lambda x: x[0]
        )
    else:  # LONG
        buckets = sorted(
            [(p, u) for p, u in heatmap.items() if p < current_price and u >= 5_000],
            key=lambda x: x[0], reverse=True  # gần giá nhất trước
        )

    if not buckets:
        return []

    # Single-linkage clustering
    clusters: List[List[Tuple[float, float]]] = []
    current_cluster = [buckets[0]]

    for i in range(1, len(buckets)):
        prev_price = current_cluster[-1][0]
        curr_price = buckets[i][0]
        gap = abs(curr_price - prev_price) / prev_price

        if gap <= cluster_gap_pct:
            current_cluster.append(buckets[i])
        else:
            clusters.append(current_cluster)
            current_cluster = [buckets[i]]
    clusters.append(current_cluster)

    # Chuyển sang LiqCluster objects
    result: List[LiqCluster] = []
    for cl in clusters:
        prices = [p for p, _ in cl]
        usds   = [u for _, u in cl]
        total  = sum(usds)

        if total < min_cluster_usd:
            continue

        peak_idx = usds.index(max(usds))
        result.append(LiqCluster(
            lower      = min(prices),
            upper      = max(prices),
            peak       = prices[peak_idx],
            total_usd  = total,
            peak_usd   = usds[peak_idx],
            n_buckets  = len(cl),
            age_weight = 1.0,  # decay đã apply trong liquidation_tracker
        ))

    # Sắp xếp: cluster mạnh nhất (USD lớn nhất) đầu tiên
    result.sort(key=lambda c: c.total_usd, reverse=True)
    return result


def get_best_entry_from_clusters(
    clusters: List[LiqCluster],
    current_price: float,
    side: str,
    max_dist_pct: float = 0.08,   # tối đa 8% từ giá hiện tại
) -> Optional[Tuple[float, float, LiqCluster]]:
    """
    Chọn entry point tối ưu từ danh sách clusters.

    Quant logic — Liquidity Sweep Entry:
    ─────────────────────────────────────────────────────────────
    SHORT: entry tại LOWER edge của cluster phía trên
           → giá bơm lên quét liq rồi quay đầu → short ngay cạnh dưới
    LONG:  entry tại UPPER edge của cluster phía dưới
           → giá dump xuống quét liq rồi bật lên → long ngay cạnh trên

    Ưu tiên:
    1. Cluster có nhiều USD nhất (strong liquidity pool)
    2. Không quá xa giá hiện tại (> max_dist_pct → bỏ qua)
    3. Nếu nhiều cluster gần nhau (trong 1%) → lấy cái extreme nhất

    Returns: (entry_price, sl_price, cluster) hoặc None
    """
    valid = []
    for cl in clusters:
        if side == "SHORT":
            dist = (cl.lower - current_price) / current_price
            if 0 < dist <= max_dist_pct:
                valid.append(cl)
        else:  # LONG
            dist = (current_price - cl.upper) / current_price
            if 0 < dist <= max_dist_pct:
                valid.append(cl)

    if not valid:
        return None

    # Kiểm tra nếu có nhiều cluster gần nhau (gap < 1.5%)
    # → merge logic: lấy extreme edge của group
    if len(valid) >= 2:
        first  = valid[0]
        second = valid[1]
        gap = abs(first.peak - second.peak) / first.peak
        if gap < 0.015:
            # Merge 2 cluster gần nhau → lấy extreme
            if side == "SHORT":
                # Lấy upper của cluster cao hơn làm vùng sweep
                merged_upper = max(first.upper, second.upper)
                merged_total = first.total_usd + second.total_usd
                # Entry tại lower của cluster thấp hơn (gần giá hơn)
                entry_cluster = first if first.lower < second.lower else second
            else:
                merged_lower  = min(first.lower, second.lower)
                merged_total  = first.total_usd + second.total_usd
                entry_cluster = first if first.upper > second.upper else second
            valid[0] = entry_cluster

    best = valid[0]

    if side == "SHORT":
        # Entry tại lower edge − 0.05% (vào ngay khi chạm cluster)
        entry = round(best.lower * 0.9995, 8)
        # SL trên upper edge + 0.3% (tránh stop hunt qua cluster)
        sl    = round(best.upper * 1.003, 8)
    else:  # LONG
        # Entry tại upper edge + 0.05%
        entry = round(best.upper * 1.0005, 8)
        # SL dưới lower edge − 0.3%
        sl    = round(best.lower * 0.997, 8)

    return entry, sl, best
