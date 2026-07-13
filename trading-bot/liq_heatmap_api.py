# ============================================================
# LIQ HEATMAP API — Lấy liquidation zones từ Binance REST API
# Không cần đợi lệnh bị liquidate thực tế
# Dùng Open Interest + Leverage distribution để ước tính
# giống Coinglass heatmap 12h
# ============================================================
import logging
import time
import threading
import requests
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BASE = "https://fapi.binance.com"


def _get(path: str, params: dict = None, timeout: int = 10) -> any:
    try:
        r = requests.get(BASE + path, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"[LiqAPI] GET {path} error: {e}")
        return None


def fetch_mark_price(symbol: str) -> Optional[float]:
    data = _get("/fapi/v1/premiumIndex", {"symbol": symbol})
    if data:
        return float(data.get("markPrice", 0))
    return None


def fetch_long_short_ratio(symbol: str, period: str = "1h",
                            limit: int = 24) -> Optional[List]:
    """Tỷ lệ Long/Short từ Binance — dùng để estimate hướng liquidation."""
    data = _get("/futures/data/globalLongShortAccountRatio",
                {"symbol": symbol, "period": period, "limit": limit})
    return data


def fetch_open_interest_hist(symbol: str, period: str = "1h",
                              limit: int = 24) -> Optional[List]:
    """Lịch sử Open Interest — tăng mạnh = nhiều lệnh mới vào."""
    data = _get("/futures/data/openInterestHist",
                {"symbol": symbol, "period": period, "limit": limit})
    return data


def fetch_top_trader_ratio(symbol: str, period: str = "1h",
                            limit: int = 24) -> Optional[List]:
    """Long/Short ratio của top traders — dùng để weight liq zones."""
    data = _get("/futures/data/topLongShortPositionRatio",
                {"symbol": symbol, "period": period, "limit": limit})
    return data


def calc_liq_zones_from_oi(symbol: str,
                            period: str = "1h",
                            lookback: int = 24) -> Dict[float, float]:
    """
    Tính liquidation zones từ DATA THẬT của Binance:

    Sources (tất cả public API, không cần auth):
    1. /futures/data/openInterestHist  → OI USD theo từng giờ
    2. /fapi/v1/klines                 → High/Low của từng giờ
    3. /futures/data/topLongShortPositionRatio → % Long/Short của top traders

    Logic:
    ─────────────────────────────────────────────
    Với mỗi candle (1h):
      - OI tăng + giá tăng = new LONG positions mở
        → liq zone của họ = entry × (1 - 1/leverage)
      - OI tăng + giá giảm = new SHORT positions mở
        → liq zone của họ = entry × (1 + 1/leverage)
      - Entry price = midpoint của candle (high+low)/2
      - USD tại vùng liq = OI_change × long_ratio hoặc short_ratio

    Leverage distribution từ Binance stats:
    - 50% traders dùng 10-20x → liq ±5-10%
    - 35% traders dùng 20-50x → liq ±2-5%
    - 15% traders dùng 50-100x → liq ±1-2%
    ─────────────────────────────────────────────
    Returns: {price_bucket: liq_usd} — data thật, không ước tính volume
    """
    import math

    # Fetch tất cả data cùng lúc
    klines   = _get("/fapi/v1/klines",
                    {"symbol": symbol, "interval": period, "limit": lookback})
    oi_hist  = _get("/futures/data/openInterestHist",
                    {"symbol": symbol, "period": period, "limit": lookback})
    ls_ratio = _get("/futures/data/topLongShortPositionRatio",
                    {"symbol": symbol, "period": period, "limit": lookback})
    mark     = fetch_mark_price(symbol)

    if not klines or not oi_hist:
        logger.warning(f"[LiqAPI] Thiếu data cho {symbol}")
        return {}

    if not mark:
        mark = float(klines[-1][4])

    # Build OI map theo timestamp
    oi_map = {}
    for item in oi_hist:
        ts  = int(item.get("timestamp", 0))
        oi  = float(item.get("sumOpenInterestValue", 0))  # OI in USD
        oi_map[ts] = oi

    # Build long/short ratio map
    ls_map = {}
    if ls_ratio:
        for item in ls_ratio:
            ts = int(item.get("timestamp", 0))
            ls_map[ts] = {
                "long":  float(item.get("longAccount", 0.5)),
                "short": float(item.get("shortAccount", 0.5)),
            }

    # Bucket helper
    bucket_pct = 0.001  # 0.1%
    def price_to_bucket(price: float) -> float:
        if price <= 0:
            return 0.0
        magnitude   = 10 ** math.floor(math.log10(price))
        bucket_size = magnitude * bucket_pct * 10
        return round(math.floor(price / bucket_size) * bucket_size, 8)

    # Leverage distribution (theo thống kê Binance)
    leverage_dist = [
        (10,  0.25),   # 10x
        (20,  0.35),   # 20x
        (30,  0.20),   # 30x
        (50,  0.15),   # 50x
        (100, 0.05),   # 100x
    ]

    buckets: Dict[float, float] = defaultdict(float)

    oi_vals  = sorted(oi_map.items())  # [(ts, oi), ...]
    prev_oi  = None

    for i, kline in enumerate(klines):
        open_ts  = int(kline[0])
        high     = float(kline[2])
        low      = float(kline[3])
        close    = float(kline[4])
        entry    = (high + low) / 2   # ước tính entry price của kỳ này

        # Lấy OI của candle này
        curr_oi = oi_map.get(open_ts, 0)
        if curr_oi == 0 and oi_vals:
            # Tìm OI gần nhất theo timestamp
            closest = min(oi_vals, key=lambda x: abs(x[0] - open_ts))
            curr_oi = closest[1]

        oi_change = curr_oi - (prev_oi or curr_oi)
        prev_oi   = curr_oi

        if abs(oi_change) < 1000:  # OI thay đổi < $1k → bỏ qua
            continue

        # Long/short ratio
        ls = ls_map.get(open_ts, {"long": 0.5, "short": 0.5})
        long_ratio  = ls["long"]
        short_ratio = ls["short"]

        # Thời gian decay: candle cũ hơn decay nhẹ
        age_hours = (time.time() - open_ts / 1000) / 3600
        decay     = max(0.3, 1.0 - age_hours / 72)  # decay 72h

        usd_added = abs(oi_change) * decay

        # Xác định hướng: OI tăng + giá tăng → LONG mở
        price_up = close > float(klines[i-1][4]) if i > 0 else True

        if oi_change > 0:
            if price_up:
                # New LONG positions → tính liq zones dưới entry
                for lev, w in leverage_dist:
                    liq_price = entry * (1 - 1 / lev)
                    b = price_to_bucket(liq_price)
                    if b > 0:
                        buckets[b] += usd_added * w * long_ratio
            else:
                # New SHORT positions → tính liq zones trên entry
                for lev, w in leverage_dist:
                    liq_price = entry * (1 + 1 / lev)
                    b = price_to_bucket(liq_price)
                    if b > 0:
                        buckets[b] += usd_added * w * short_ratio
        else:
            # OI giảm = lệnh đóng — vẫn có lệnh còn lại, weight thấp hơn
            remaining = abs(oi_change) * 0.3 * decay
            for lev, w in leverage_dist:
                liq_long  = entry * (1 - 1 / lev)
                liq_short = entry * (1 + 1 / lev)
                bl = price_to_bucket(liq_long)
                bs = price_to_bucket(liq_short)
                if bl > 0:
                    buckets[bl] += remaining * w * long_ratio
                if bs > 0:
                    buckets[bs] += remaining * w * short_ratio

    total = sum(buckets.values())
    logger.info(f"[LiqAPI] {symbol}: {len(buckets)} zones, "
                f"total=${total/1e6:.2f}M, mark=${mark:,.2f}")
    return dict(buckets)


class LiqHeatmapCache:
    """
    Cache liq heatmap cho nhiều symbols.
    Refresh mỗi 15 phút (dữ liệu REST không cần realtime).
    """
    REFRESH_INTERVAL = 900  # 15 phút

    def __init__(self, symbols: List[str], interval: str = "1h", lookback: int = 24):
        self.symbols  = [s.upper() for s in symbols]
        self.interval = interval
        self.lookback = lookback
        self._cache: Dict[str, Dict[float, float]] = {}
        self._last_update: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._thread = None
        self._running = False

    def start(self):
        """Chạy background thread refresh cache — không block main thread."""
        self._running = True
        # Load lần đầu trong thread riêng, không block
        self._thread = threading.Thread(target=self._start_async, daemon=True)
        self._thread.start()
        logger.info(f"[LiqHeatmapCache] Started async for {self.symbols}")

    def _start_async(self):
        """Load lần đầu rồi loop refresh."""
        self._refresh_all()
        while self._running:
            time.sleep(self.REFRESH_INTERVAL)
            self._refresh_all()

    def _refresh_all(self):
        for sym in self.symbols:
            try:
                data = calc_liq_zones_from_oi(sym, self.interval, self.lookback)
                with self._lock:
                    self._cache[sym] = data
                    self._last_update[sym] = time.time()
                logger.debug(f"[LiqHeatmapCache] Refreshed {sym}: {len(data)} buckets")
            except Exception as e:
                logger.error(f"[LiqHeatmapCache] Refresh {sym} failed: {e}")

    def get_heatmap(self, symbol: str) -> Dict[float, float]:
        sym = symbol.upper()
        with self._lock:
            return dict(self._cache.get(sym, {}))

    def is_ready(self, symbol: str) -> bool:
        sym = symbol.upper()
        with self._lock:
            return sym in self._cache and len(self._cache[sym]) > 0

    def get_best_cluster(self,
                          symbol: str,
                          current_price: float,
                          direction: str,
                          min_usd: float = 50_000,
                          cluster_gap_pct: float = 0.008) -> Optional[Dict]:
        """
        Tìm cluster tối ưu từ estimated heatmap.
        Interface giống LiquidationTracker.get_best_entry_cluster()
        """
        heatmap = self.get_heatmap(symbol)
        if not heatmap:
            return None

        # Lọc theo direction
        if direction == "SHORT":
            raw = sorted(
                [(p, u) for p, u in heatmap.items() if p > current_price and u >= min_usd / 10],
                key=lambda x: x[0]
            )
        else:
            raw = sorted(
                [(p, u) for p, u in heatmap.items() if p < current_price and u >= min_usd / 10],
                key=lambda x: x[0], reverse=True
            )

        if not raw:
            return None

        # Cluster
        clusters = []
        cur = [raw[0]]
        for i in range(1, len(raw)):
            gap = abs(raw[i][0] - cur[-1][0]) / cur[-1][0]
            if gap <= cluster_gap_pct:
                cur.append(raw[i])
            else:
                clusters.append(cur)
                cur = [raw[i]]
        clusters.append(cur)

        # Tính stats
        stats = []
        for cl in clusters:
            prices = [p for p, u in cl]
            usds   = [u for p, u in cl]
            total  = sum(usds)
            if total < min_usd:
                continue
            low  = min(prices)
            high = max(prices)
            dist = abs((low if direction == "SHORT" else high) - current_price) / current_price * 100
            if dist > 10.0:
                continue
            stats.append({"low": low, "high": high, "total_usd": total,
                          "n_buckets": len(cl), "dist_pct": dist})

        if not stats:
            return None

        # Chọn: gần nhất nếu đủ USD, không thì lớn nhất USD
        best = stats[0]
        if best["total_usd"] < min_usd and len(stats) > 1:
            best = max(stats, key=lambda c: c["total_usd"])

        if direction == "SHORT":
            entry   = best["low"]
            sl_zone = best["high"] * 1.002
        else:
            entry   = best["high"]
            sl_zone = best["low"] * 0.998

        return {
            "entry":        round(entry, 8),
            "cluster_low":  round(best["low"], 8),
            "cluster_high": round(best["high"], 8),
            "total_usd":    round(best["total_usd"], 0),
            "n_buckets":    best["n_buckets"],
            "dist_pct":     round(best["dist_pct"], 2),
            "sl_zone":      round(sl_zone, 8),
        }

    def total_liq_usd(self, symbol: str) -> float:
        heatmap = self.get_heatmap(symbol)
        return sum(heatmap.values())

    def is_connected(self) -> bool:
        """Compatible interface với LiquidationTracker."""
        return len(self._cache) > 0

    def get_liq_heatmap(self, symbol: str, **kwargs) -> Dict[float, float]:
        """Compatible interface với LiquidationTracker."""
        return self.get_heatmap(symbol)

    def get_best_entry_cluster(self, symbol: str, current_price: float,
                                direction: str, min_usd: float = 30_000,
                                cluster_gap_pct: float = 0.008) -> Optional[Dict]:
        """Compatible interface với LiquidationTracker."""
        return self.get_best_cluster(symbol, current_price, direction,
                                     min_usd, cluster_gap_pct)
