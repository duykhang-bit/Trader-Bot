# ============================================================
# LIQUIDITY ENGINE — Tự tính vùng thanh khoản miễn phí
# Kết hợp: Order Book walls + OI + Volume + Liq stream + Swing
# Lightweight: REST mỗi 5 phút, ~10-20MB RAM
# ============================================================
import logging
import time
import threading
import requests
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BASE = "https://fapi.binance.com"


def _get(path: str, params: dict = None, timeout: int = 10):
    try:
        r = requests.get(BASE + path, params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"[LiqEngine] GET {path}: {e}")
        return None


def get_orderbook_walls(symbol: str, limit: int = 20) -> Dict[str, List[Tuple[float, float]]]:
    """
    Lấy top bid/ask walls từ order book.
    Returns: {"bids": [(price, qty_usd), ...], "asks": [(price, qty_usd), ...]}
    Sorted by qty_usd descending (wall lớn nhất trước).
    """
    data = _get("/fapi/v1/depth", {"symbol": symbol, "limit": 500})
    if not data:
        return {"bids": [], "asks": []}

    bids = []
    for price_str, qty_str in data.get("bids", []):
        p = float(price_str)
        q = float(qty_str)
        bids.append((p, p * q))  # (price, USD value)

    asks = []
    for price_str, qty_str in data.get("asks", []):
        p = float(price_str)
        q = float(qty_str)
        asks.append((p, p * q))

    # Sort by USD value, lấy top walls
    bids.sort(key=lambda x: x[1], reverse=True)
    asks.sort(key=lambda x: x[1], reverse=True)

    return {"bids": bids[:limit], "asks": asks[:limit]}


def get_volume_profile(symbol: str, interval: str = "15m", limit: int = 50) -> Dict[float, float]:
    """
    Tính volume profile từ klines — giá nào có volume giao dịch nhiều nhất.
    Returns: {price_bucket: total_volume_usd}
    """
    klines = _get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not klines:
        return {}

    # Bucket theo giá trung bình mỗi nến
    vol_profile = defaultdict(float)
    for k in klines:
        high = float(k[2])
        low = float(k[3])
        close = float(k[4])
        volume = float(k[5])
        mid = (high + low) / 2
        usd_vol = volume * mid
        # Round to bucket (0.5% range)
        bucket = round(mid, len(str(mid).split('.')[1]) - 1) if mid < 1 else round(mid, 2)
        vol_profile[bucket] += usd_vol

    return dict(vol_profile)


def get_oi_change(symbol: str) -> Optional[Dict]:
    """
    OI thay đổi gần nhất — OI tăng = lệnh mới mở, OI giảm = lệnh đóng.
    """
    data = _get("/futures/data/openInterestHist",
                {"symbol": symbol, "period": "5m", "limit": 12})
    if not data or len(data) < 2:
        return None

    latest = float(data[-1].get("sumOpenInterestValue", 0))
    prev = float(data[-2].get("sumOpenInterestValue", 0))
    change = latest - prev
    change_pct = change / prev * 100 if prev > 0 else 0

    return {"oi": latest, "change": change, "change_pct": change_pct}


def calc_liquidity_zones(symbol: str, current_price: float) -> Dict[str, List[Dict]]:
    """
    Tính Liquidity Zones cho 1 coin.
    Kết hợp: order book walls + volume profile + OI.

    Returns: {
        "long_zones": [{"price": x, "score": y, "reason": "..."}, ...],
        "short_zones": [{"price": x, "score": y, "reason": "..."}, ...],
    }
    Zones sorted by score descending.
    """
    if current_price <= 0:
        return {"long_zones": [], "short_zones": []}

    long_zones = []
    short_zones = []

    # 1. Order Book Walls
    ob = get_orderbook_walls(symbol, limit=10)

    # Bid walls = support (LONG zones)
    for price, usd in ob["bids"]:
        if price < current_price and usd >= 5000:
            dist_pct = (current_price - price) / current_price * 100
            if 1.0 <= dist_pct <= 10.0:
                score = min(usd / 10000, 5.0)  # Max 5 points from OB
                long_zones.append({
                    "price": price, "score": score,
                    "usd": usd, "source": "OB_wall",
                    "reason": f"Bid wall ${usd/1000:.0f}K"
                })

    # Ask walls = resistance (SHORT zones)
    for price, usd in ob["asks"]:
        if price > current_price and usd >= 5000:
            dist_pct = (price - current_price) / current_price * 100
            if 1.0 <= dist_pct <= 10.0:
                score = min(usd / 10000, 5.0)
                short_zones.append({
                    "price": price, "score": score,
                    "usd": usd, "source": "OB_wall",
                    "reason": f"Ask wall ${usd/1000:.0f}K"
                })

    # 2. Volume Profile — vùng volume cao = support/resistance
    vp = get_volume_profile(symbol, "15m", 50)
    if vp:
        # Top volume buckets
        sorted_vp = sorted(vp.items(), key=lambda x: x[1], reverse=True)[:10]
        for price, vol_usd in sorted_vp:
            dist_pct = (current_price - price) / current_price * 100
            if price < current_price and 1.5 <= dist_pct <= 10.0:
                score = min(vol_usd / 500000, 3.0)  # Max 3 points from VP
                long_zones.append({
                    "price": price, "score": score,
                    "usd": vol_usd, "source": "volume",
                    "reason": f"High vol ${vol_usd/1000:.0f}K"
                })
            elif price > current_price:
                dist_pct_up = (price - current_price) / current_price * 100
                if 1.5 <= dist_pct_up <= 10.0:
                    score = min(vol_usd / 500000, 3.0)
                    short_zones.append({
                        "price": price, "score": score,
                        "usd": vol_usd, "source": "volume",
                        "reason": f"High vol ${vol_usd/1000:.0f}K"
                    })

    # 3. OI concentration (từ liq_heatmap_api đã có)
    try:
        from liq_heatmap_api import calc_liq_zones_from_oi
        oi_zones = calc_liq_zones_from_oi(symbol, "1h", 24)
        if oi_zones:
            for price, usd in oi_zones.items():
                if usd < 30000:
                    continue
                if price < current_price:
                    dist_pct = (current_price - price) / current_price * 100
                    if 2.0 <= dist_pct <= 10.0:
                        score = min(usd / 100000, 4.0)  # Max 4 points from OI
                        long_zones.append({
                            "price": price, "score": score,
                            "usd": usd, "source": "OI",
                            "reason": f"OI liq ${usd/1000:.0f}K"
                        })
                elif price > current_price:
                    dist_pct = (price - current_price) / current_price * 100
                    if 2.0 <= dist_pct <= 10.0:
                        score = min(usd / 100000, 4.0)
                        short_zones.append({
                            "price": price, "score": score,
                            "usd": usd, "source": "OI",
                            "reason": f"OI liq ${usd/1000:.0f}K"
                        })
    except Exception:
        pass

    # Gom zones gần nhau (cluster trong 0.5%)
    long_zones = _cluster_zones(long_zones, current_price, pct_gap=0.5)
    short_zones = _cluster_zones(short_zones, current_price, pct_gap=0.5)

    # Sort by score
    long_zones.sort(key=lambda x: x["score"], reverse=True)
    short_zones.sort(key=lambda x: x["score"], reverse=True)

    return {"long_zones": long_zones[:5], "short_zones": short_zones[:5]}


def _cluster_zones(zones: List[Dict], ref_price: float, pct_gap: float = 0.5) -> List[Dict]:
    """Gom zones gần nhau thành 1 cluster, cộng score."""
    if not zones:
        return []

    # Sort by price
    zones.sort(key=lambda x: x["price"])
    clustered = []
    current_cluster = zones[0].copy()

    for z in zones[1:]:
        gap = abs(z["price"] - current_cluster["price"]) / ref_price * 100
        if gap <= pct_gap:
            # Gom vào cluster hiện tại
            current_cluster["score"] += z["score"]
            current_cluster["usd"] = current_cluster.get("usd", 0) + z.get("usd", 0)
            current_cluster["reason"] += f" + {z['reason']}"
        else:
            clustered.append(current_cluster)
            current_cluster = z.copy()

    clustered.append(current_cluster)
    return clustered


def get_best_entry(symbol: str, direction: str, current_price: float,
                   swing_price: float = None) -> Optional[Dict]:
    """
    Tìm entry tốt nhất cho LONG/SHORT.
    Kết hợp liquidity zones + swing 15m.

    Returns: {"price": entry, "score": total_score, "reason": "...", "dist_pct": ...}
    hoặc None nếu không tìm được.
    """
    zones = calc_liquidity_zones(symbol, current_price)

    if direction == "LONG":
        candidates = zones["long_zones"]
    else:
        candidates = zones["short_zones"]

    if not candidates and not swing_price:
        return None

    # Kết hợp với swing price
    best = None

    if candidates:
        best = candidates[0]  # Score cao nhất

    # Nếu có swing price và nó gần hơn (fill dễ hơn)
    if swing_price:
        swing_dist = abs(current_price - swing_price) / current_price * 100
        if 2.0 <= swing_dist <= 10.0:
            if direction == "LONG":
                # LONG: entry = max(liq_zone, swing_low) — cái gần giá hơn
                if best and swing_price > best["price"]:
                    best["price"] = swing_price
                    best["reason"] += " + swing_15m"
                elif not best:
                    best = {"price": swing_price, "score": 3.0,
                            "reason": "swing_15m", "dist_pct": swing_dist}
            else:
                # SHORT: entry = min(liq_zone, swing_high) — cái gần giá hơn
                if best and swing_price < best["price"]:
                    best["price"] = swing_price
                    best["reason"] += " + swing_15m"
                elif not best:
                    best = {"price": swing_price, "score": 3.0,
                            "reason": "swing_15m", "dist_pct": swing_dist}

    if best:
        dist = abs(current_price - best["price"]) / current_price * 100
        best["dist_pct"] = round(dist, 2)
        # Min 2% distance
        if dist < 2.0:
            return None

    return best
