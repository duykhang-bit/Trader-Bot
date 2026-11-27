"""Read-only liquidity-zone analysis for one coin over 12h and 24h frames.

Uses the same Binance OI estimate, chart frame (frame high/low with at least
±4% around mark) and 0.27 visibility threshold as Auto Liquidity Zone, so the
"highest qualifying zone above" here is exactly where Auto Liq SHORT would
place its order for the 12h frame. Never places orders.
"""
from __future__ import annotations

import math
import time

from auto_liq_zone_orders import LIQUIDITY_THRESHOLD, MARK_FRAME_MARGIN_PCT
from liq_heatmap_api import calc_liq_zones_from_oi

FRAMES = {"12h": 144, "24h": 288}  # 5m candles
TOP_N = 8
_CACHE = {}
CACHE_TTL_S = 60


def _side(zones, mark, frame_low, frame_high, above):
    rows = [(float(p), float(u)) for p, u in zones.items()
            if float(u) > 0 and ((mark < float(p) <= frame_high) if above else (frame_low <= float(p) < mark))]
    if not rows:
        return {"zones": [], "target": None, "strongest": None}
    biggest = max(u for _, u in rows)
    qualified = [r for r in rows if r[1] >= biggest * LIQUIDITY_THRESHOLD]
    target = (max if above else min)(qualified, key=lambda r: r[0])
    strongest = max(rows, key=lambda r: r[1])
    top = sorted(rows, key=lambda r: -r[1])[:TOP_N]
    top.sort(key=lambda r: -r[0] if above else r[0])

    def view(r):
        return {"price": r[0], "usd": round(r[1]), "dist_pct": round(abs(r[0] - mark) / mark * 100, 2),
                "share_pct": round(r[1] / biggest * 100), "qualified": r[1] >= biggest * LIQUIDITY_THRESHOLD}
    return {"zones": [view(r) for r in top], "target": view(target), "strongest": view(strongest)}


def analyze(exchange, symbol):
    symbol = str(symbol or "").strip().upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    cached = _CACHE.get(symbol)
    if cached and time.time() - cached["ts"] < CACHE_TTL_S:
        return cached
    mark = float(exchange.get_mark_price(symbol) or 0)
    if not math.isfinite(mark) or mark <= 0:
        raise ValueError(f"Không lấy được giá {symbol}")
    klines = exchange.get_klines(symbol, "5m", limit=max(FRAMES.values()))
    out = {"ok": True, "symbol": symbol, "mark": mark, "ts": time.time(), "frames": {}}
    margin = MARK_FRAME_MARGIN_PCT / 100
    for name, n in FRAMES.items():
        k = klines[-n:]
        hi = max(float(x[2]) for x in k)
        lo = min(float(x[3]) for x in k)
        zones = calc_liq_zones_from_oi(symbol, period="5m", lookback=n)
        f_lo, f_hi = min(lo, mark * (1 - margin)), max(hi, mark * (1 + margin))
        out["frames"][name] = {
            "candle_high": hi, "candle_low": lo, "frame_high": f_hi, "frame_low": f_lo,
            "above": _side(zones, mark, f_lo, f_hi, True),
            "below": _side(zones, mark, f_lo, f_hi, False),
        }
    _CACHE[symbol] = out
    return out
