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


def fetch_klines(symbol: str, interval: str = "1h",
                 limit: int = 24) -> Optional[List]:
    data = _get("/fapi/v1/klines",
                {"symbol": symbol, "interval": interval, "limit": limit})
    return data


def estimate_liq_zones(symbol: str,
                        interval: str = "1h",
                        lookback: int = 24) -> Dict[float, float]:
    """
    Ước tính liquidation zones giống Coinglass 12h heatmap.

    Phương pháp:
    ─────────────────────────────────────────────────────────
    1. Lấy klines 1h × 24 candles (12h = 12, 24h = 24)
    2. Với mỗi candle: tính 2 vùng liq ước tính
       - LONG liq zone: candle_low × (1 - 1/leverage_est)
         Người dùng long với leverage L sẽ bị liq khi giá giảm ~1/L
       - SHORT liq zone: candle_high × (1 + 1/leverage_est)
         Người dùng short với leverage L sẽ bị liq khi giá tăng ~1/L
    3. Volume candle = proxy cho lượng lệnh mở tại vùng đó
    4. Accumulate USD = volume × price vào price buckets
    5. Bucket size = 0.1% của giá

    Leverage distribution (ước tính từ thực tế Binance):
    - 30% traders dùng 10x → liq at ±10%
    - 40% traders dùng 20x → liq at ±5%
    - 20% traders dùng 50x → liq at ±2%
    - 10% traders dùng 100x → liq at ±1%

    Returns: {price_bucket: estimated_liq_usd}
    """
    klines = fetch_klines(symbol, interval, lookback)
    if not klines:
        logger.warning(f"[LiqAPI] No klines for {symbol}")
        return {}

    mark_price = fetch_mark_price(symbol)
    if not mark_price:
        mark_price = float(klines[-1][4])  # fallback: close price

    # Leverage distribution
    leverage_dist = [
        (10,  0.30),  # 10x — 30% traders
        (20,  0.40),  # 20x — 40% traders
        (50,  0.20),  # 50x — 20% traders
        (100, 0.10),  # 100x — 10% traders
    ]

    # Bucket size: 0.1% của giá
    bucket_pct = 0.001

    def price_to_bucket(price: float) -> float:
        import math
        if price <= 0:
            return 0.0
        magnitude  = 10 ** math.floor(math.log10(price))
        bucket_size = magnitude * bucket_pct * 10
        return round(math.floor(price / bucket_size) * bucket_size, 8)

    buckets: Dict[float, float] = defaultdict(float)

    for kline in klines:
        open_t  = int(kline[0])
        high    = float(kline[2])
        low     = float(kline[3])
        close   = float(kline[4])
        volume  = float(kline[5])      # base volume
        quote_vol = float(kline[7])    # quote volume (USD)

        # Tính thời gian — candle cũ hơn decay nhẹ
        age_hours = (time.time() * 1000 - open_t) / 3_600_000
        # Decay: candle 12h trước còn 50%, 24h trước còn 25%
        decay = max(0.25, 1.0 - age_hours / 48)

        for leverage, weight in leverage_dist:
            liq_margin = 1.0 / leverage  # % giá move → liq

            # ── LONG liquidation zones ────────────────────────
            # Long positions mở trong candle này sẽ bị liq nếu giá giảm liq_margin
            # Estimated entry: từ low đến high của candle
            # Liq price của long entry tại P với leverage L: P × (1 - 1/L)

            # Vùng liq long = từ low*(1-1/L) đến high*(1-1/L)
            long_liq_low  = low  * (1 - liq_margin)
            long_liq_high = high * (1 - liq_margin)

            # USD tại vùng này = quote_vol × weight của leverage này × decay
            usd = quote_vol * weight * decay * 0.5  # 50% giả định long

            # Phân bổ vào buckets trong range [long_liq_low, long_liq_high]
            n_buckets = max(1, int((long_liq_high - long_liq_low) / (mark_price * bucket_pct * 10)))
            if n_buckets > 0:
                usd_per_bucket = usd / n_buckets
                step = (long_liq_high - long_liq_low) / n_buckets
                for i in range(n_buckets):
                    p = long_liq_low + step * i
                    b = price_to_bucket(p)
                    if b > 0:
                        buckets[b] += usd_per_bucket

            # ── SHORT liquidation zones ───────────────────────
            # Short positions sẽ bị liq nếu giá tăng liq_margin
            # Liq price của short entry tại P với leverage L: P × (1 + 1/L)

            short_liq_low  = low  * (1 + liq_margin)
            short_liq_high = high * (1 + liq_margin)

            usd_short = quote_vol * weight * decay * 0.5  # 50% giả định short

            n_buckets_s = max(1, int((short_liq_high - short_liq_low) / (mark_price * bucket_pct * 10)))
            if n_buckets_s > 0:
                usd_per_bucket_s = usd_short / n_buckets_s
                step_s = (short_liq_high - short_liq_low) / n_buckets_s
                for i in range(n_buckets_s):
                    p = short_liq_low + step_s * i
                    b = price_to_bucket(p)
                    if b > 0:
                        buckets[b] += usd_per_bucket_s

    logger.info(f"[LiqAPI] {symbol}: {len(buckets)} buckets, "
                f"max=${max(buckets.values(), default=0)/1e3:.0f}k "
                f"mark=${mark_price:,.2f}")
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
        """Chạy background thread refresh cache."""
        self._running = True
        # Load lần đầu ngay
        self._refresh_all()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"[LiqHeatmapCache] Started for {self.symbols}")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            time.sleep(self.REFRESH_INTERVAL)
            self._refresh_all()

    def _refresh_all(self):
        for sym in self.symbols:
            try:
                data = estimate_liq_zones(sym, self.interval, self.lookback)
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
