# ============================================================
# MULTI-COIN SCANNER — Tự quét coin, chọn cái tốt nhất vào lệnh
# ============================================================
import logging
import time
import requests
import pandas as pd
from dataclasses import dataclass
from typing import Optional, List
from indicators import (
    get_signal, calculate_rsi, calculate_ema, calculate_atr,
    get_mtf_trend, is_volatile_coin, get_pullback_signal,
    get_smart_entry_signal, compute_signal_score,
)
from pump_detector import PumpDetector, PumpSignal, scan_for_pump_tops

logger = logging.getLogger(__name__)

# Fallback list nếu không fetch được từ Binance
_FALLBACK_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "LTCUSDT", "BCHUSDT", "ETCUSDT", "XLMUSDT", "ATOMUSDT",
    "UNIUSDT", "AAVEUSDT", "NEARUSDT", "APTUSDT", "SUIUSDT",
    "ARBUSDT", "OPUSDT", "INJUSDT", "SEIUSDT", "RUNEUSDT",
    "FETUSDT", "WLDUSDT", "TAOUSDT", "RENDERUSDT", "LABUSDT",
]

# Coin ưu tiên — luôn scan đầu tiên, bonus +10 điểm
PRIORITY_COINS = [
    "LABUSDT",
]

def fetch_dynamic_watchlist(base_url: str = "https://testnet.binancefuture.com",
                             top_n: int = 80) -> List[str]:
    """
    Lấy danh sách coin từ Binance Futures:
    - Chỉ lấy USDT pairs
    - Sắp xếp theo volume 24h (coin hot nhất lên đầu)
    - Ưu tiên top gainers (tăng giá mạnh nhất)
    - Cập nhật mỗi lần gọi → luôn có coin mới
    """
    try:
        resp = requests.get(f"{base_url}/fapi/v1/ticker/24hr", timeout=10)
        resp.raise_for_status()
        tickers = resp.json()

        # Chỉ lấy USDT pairs, loại stablecoin
        exclude = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDTUSDT"}
        usdt_pairs = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and t["symbol"] not in exclude
            and float(t.get("quoteVolume", 0)) > 1_000_000  # volume > $1M
        ]

        # Tính điểm ưu tiên: kết hợp volume + price change
        def priority(t):
            vol   = float(t.get("quoteVolume", 0))
            chg   = float(t.get("priceChangePercent", 0))
            # Ưu tiên coin tăng mạnh (chg > 3%) và volume cao
            bonus = 2.0 if chg > 5 else (1.5 if chg > 3 else 1.0)
            return vol * bonus

        usdt_pairs.sort(key=priority, reverse=True)
        symbols = [t["symbol"] for t in usdt_pairs[:top_n]]

        logger.info(f"📡 Dynamic watchlist: {len(symbols)} coins fetched from Binance")

        # Log top gainers
        gainers = sorted(usdt_pairs[:top_n],
                         key=lambda t: float(t.get("priceChangePercent", 0)),
                         reverse=True)[:5]
        for g in gainers:
            logger.info(f"  🚀 {g['symbol']}: +{float(g['priceChangePercent']):.1f}% | vol=${float(g['quoteVolume'])/1e6:.0f}M")

        return symbols

    except Exception as e:
        logger.warning(f"fetch_dynamic_watchlist failed: {e} — using fallback")
        return _FALLBACK_WATCHLIST.copy()


# WATCHLIST được load lúc khởi động
# - Nếu config.WATCHLIST_MODE = "fixed" → dùng config.FIXED_COINS
# - Nếu config.WATCHLIST_MODE = "dynamic" → fetch từ Binance theo volume
def _load_initial_watchlist() -> List[str]:
    try:
        import config as _cfg
        if getattr(_cfg, "WATCHLIST_MODE", "dynamic") == "fixed":
            # Ưu tiên đọc watchlist.json (do dashboard lưu) nếu có
            import json as _json, os as _os
            wl_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "watchlist.json")
            if _os.path.exists(wl_path):
                try:
                    with open(wl_path) as _f:
                        saved = _json.load(_f)
                    if saved:
                        logger.info(f"📌 Watchlist from file: {len(saved)} coins")
                        return list(saved)
                except Exception:
                    pass
            coins = list(getattr(_cfg, "FIXED_COINS", _FALLBACK_WATCHLIST))
            logger.info(f"📌 Fixed watchlist: {coins}")
            return coins
    except Exception:
        pass
    return fetch_dynamic_watchlist()

WATCHLIST = _load_initial_watchlist()
_watchlist_last_update = 0

# Active universe: top 10 coin được lọc mỗi 5 phút để scan nhanh
_active_universe: List[str] = []
_universe_last_update = 0

def get_watchlist(base_url: str = "https://testnet.binancefuture.com") -> List[str]:
    """Trả về WATCHLIST. Fixed mode: dùng watchlist.json từ dashboard hoặc FIXED_COINS từ config."""
    import time
    global WATCHLIST, _watchlist_last_update

    try:
        import config as _cfg
        if getattr(_cfg, "WATCHLIST_MODE", "dynamic") == "fixed":
            # Đọc watchlist.json nếu có
            import json as _json, os as _os
            wl_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "watchlist.json")
            if _os.path.exists(wl_path):
                try:
                    with open(wl_path) as _f:
                        saved = _json.load(_f)
                    if saved:
                        return list(saved)
                except Exception:
                    pass
            return list(getattr(_cfg, "FIXED_COINS", WATCHLIST))
    except Exception:
        pass

    if time.time() - _watchlist_last_update > 1800:  # 30 phút
        WATCHLIST = fetch_dynamic_watchlist(base_url)
        _watchlist_last_update = time.time()
    # Đảm bảo priority coins luôn có trong list và đứng đầu
    result = list(PRIORITY_COINS)
    for s in WATCHLIST:
        if s not in result:
            result.append(s)
    return result


def get_active_universe(base_url: str = "https://testnet.binancefuture.com",
                        top_n: int = 10) -> List[str]:
    """
    Fixed mode: trả về FIXED_COINS từ config.
    Dynamic mode: lọc top N coin theo volume + biến động, refresh mỗi 3 phút.
    """
    import time
    global _active_universe, _universe_last_update

    try:
        import config as _cfg
        if getattr(_cfg, "WATCHLIST_MODE", "dynamic") == "fixed":
            # Đọc watchlist.json nếu có
            import json as _json, os as _os
            wl_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "watchlist.json")
            if _os.path.exists(wl_path):
                try:
                    with open(wl_path) as _f:
                        saved = _json.load(_f)
                    if saved:
                        logger.info(f"🎯 Active universe (watchlist.json): {saved}")
                        return list(saved)
                except Exception:
                    pass
            coins = list(getattr(_cfg, "FIXED_COINS", WATCHLIST))
            logger.info(f"🎯 Active universe (fixed): {coins}")
            return coins
    except Exception:
        pass

    if time.time() - _universe_last_update < 180 and _active_universe:  # 3 phút cache
        return _active_universe

    try:
        resp = requests.get(f"{base_url}/fapi/v1/ticker/24hr", timeout=10)
        resp.raise_for_status()
        tickers = resp.json()

        exclude = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDTUSDT"}
        usdt_pairs = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and t["symbol"] not in exclude
            and float(t.get("quoteVolume", 0)) > 5_000_000  # volume > $5M
        ]

        # Score: volume × |price_change| × ATR proxy
        def universe_score(t):
            vol    = float(t.get("quoteVolume", 0))
            chg    = abs(float(t.get("priceChangePercent", 0)))
            high   = float(t.get("highPrice", 1))
            low    = float(t.get("lowPrice", 1))
            spread = (high - low) / low * 100  # daily range %
            return vol * (1 + chg/10) * (1 + spread/10)

        usdt_pairs.sort(key=universe_score, reverse=True)
        top_symbols = [t["symbol"] for t in usdt_pairs[:top_n]]

        # Luôn có priority coins
        result = list(PRIORITY_COINS)
        for s in top_symbols:
            if s not in result:
                result.append(s)
        result = result[:top_n + len(PRIORITY_COINS)]

        _active_universe = result
        _universe_last_update = time.time()

        logger.info(f"🎯 Active universe updated: {result}")
        return result

    except Exception as e:
        logger.warning(f"get_active_universe failed: {e}")
        return _active_universe if _active_universe else get_watchlist(base_url)[:top_n]


# ============================================================
# P0 HELPERS — Market Regime, BTC Context, 1H Location,
#              Structure SL/TP, No-Chase
# ============================================================

def _calc_adx(high: "pd.Series", low: "pd.Series", close: "pd.Series",
              period: int = 14) -> float:
    """Tính ADX đơn giản để phân biệt TREND vs RANGE."""
    try:
        import numpy as np
        h = high.values
        l = low.values
        c = close.values
        n = len(c)
        if n < period + 2:
            return 25.0  # default — coi như TREND nếu không đủ data

        plus_dm  = np.zeros(n)
        minus_dm = np.zeros(n)
        tr_arr   = np.zeros(n)
        for i in range(1, n):
            up   = h[i] - h[i-1]
            down = l[i-1] - l[i]
            plus_dm[i]  = up   if up > down and up > 0   else 0
            minus_dm[i] = down if down > up and down > 0 else 0
            tr_arr[i]   = max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1]))

        # Wilder smoothing
        def wilder(arr, p):
            out = np.zeros(n)
            out[p] = arr[1:p+1].sum()
            for i in range(p+1, n):
                out[i] = out[i-1] - out[i-1]/p + arr[i]
            return out

        atr_w   = wilder(tr_arr, period)
        pdm_w   = wilder(plus_dm, period)
        mdm_w   = wilder(minus_dm, period)

        pdi = 100 * pdm_w / np.where(atr_w > 0, atr_w, 1)
        mdi = 100 * mdm_w / np.where(atr_w > 0, atr_w, 1)
        dx  = 100 * np.abs(pdi - mdi) / np.where((pdi + mdi) > 0, pdi + mdi, 1)

        adx_arr = wilder(dx, period)
        return float(adx_arr[-1])
    except Exception:
        return 25.0  # fallback — coi như đang TREND


def detect_regime(df_4h: "pd.DataFrame", cfg=None) -> dict:
    """
    Phát hiện Market Regime từ 4H data.

    Returns:
        {
            "regime":  "TREND_UP" | "TREND_DOWN" | "RANGE" | "CHAOS",
            "bias":    "LONG" | "SHORT" | "NEUTRAL",
            "slope":   float,   # EMA50 slope % normalized
            "adx":     float,
            "reason":  str,
        }
    """
    result = {"regime": "RANGE", "bias": "NEUTRAL",
              "slope": 0.0, "adx": 0.0, "reason": "default"}
    try:
        from indicators import calculate_ema, calculate_atr
        close = df_4h["close"]
        high  = df_4h["high"]
        low   = df_4h["low"]

        if len(close) < 55:
            result["reason"] = "không đủ data 4H"
            return result

        ema9  = calculate_ema(close, 9)
        ema21 = calculate_ema(close, 21)
        ema50 = calculate_ema(close, 50)

        price    = float(close.iloc[-1])
        e9       = float(ema9.iloc[-1])
        e21      = float(ema21.iloc[-1])
        e50      = float(ema50.iloc[-1])
        e50_prev = float(ema50.iloc[-5])  # 5 nến trước

        # EMA50 slope normalize (%)
        slope = (e50 - e50_prev) / e50_prev * 100 if e50_prev > 0 else 0.0

        # ADX
        adx = _calc_adx(high, low, close)

        # CHAOS: ATR spike bất thường
        slope_threshold = getattr(cfg, "REGIME_SLOPE_THRESHOLD", 0.05) if cfg else 0.05
        chaos_mult      = getattr(cfg, "CHAOS_ATR_MULT", 2.5)          if cfg else 2.5
        adx_range       = getattr(cfg, "ADX_RANGE_THRESHOLD", 25)       if cfg else 25

        atr_now  = float(calculate_atr(high, low, close).iloc[-1])
        atr_avg  = float(calculate_atr(high, low, close).rolling(20).mean().iloc[-1])
        is_chaos = atr_now > chaos_mult * atr_avg if atr_avg > 0 else False

        result["slope"] = round(slope, 4)
        result["adx"]   = round(adx, 1)

        if is_chaos:
            result["regime"] = "CHAOS"
            result["bias"]   = "NEUTRAL"
            result["reason"] = f"CHAOS: ATR={atr_now:.5f} > {chaos_mult}×avg={atr_avg:.5f}"
            return result

        # RANGE: ADX thấp VÀ slope gần 0
        if adx < adx_range and abs(slope) < slope_threshold:
            result["regime"] = "RANGE"
            result["bias"]   = "NEUTRAL"
            result["reason"] = f"RANGE: ADX={adx:.1f}<{adx_range} slope={slope:.3f}%"
            return result

        # TREND: cần slope đủ mạnh + EMA alignment
        if (slope > slope_threshold
                and e9 > e21
                and price > e50):
            result["regime"] = "TREND_UP"
            result["bias"]   = "LONG"
            result["reason"] = f"TREND_UP: slope={slope:.3f}% EMA9>21 price>EMA50"
        elif (slope < -slope_threshold
              and e9 < e21
              and price < e50):
            result["regime"] = "TREND_DOWN"
            result["bias"]   = "SHORT"
            result["reason"] = f"TREND_DOWN: slope={slope:.3f}% EMA9<21 price<EMA50"
        else:
            # Slope có nhưng EMA chưa align đủ → RANGE/WEAK
            result["regime"] = "RANGE"
            result["bias"]   = "NEUTRAL"
            result["reason"] = f"WEAK: slope={slope:.3f}% ADX={adx:.1f} EMA not aligned"

    except Exception as e:
        result["reason"] = f"detect_regime error: {e}"

    return result


# Cache BTC context — tránh fetch 3 lần/coin khi scan nhiều coin
_btc_context_cache: dict = {}
_btc_context_ts:    float = 0.0
_BTC_CONTEXT_TTL:   float = 60.0  # giây


def get_btc_context(exchange, cfg=None) -> dict:
    """
    Lấy BTC market context từ 3 timeframe (4H + 1H + 15M).
    Cache 60s để không fetch lại cho từng coin.

    Returns:
        {
            "state_4h":  "STRONG_BULL"|"BULL"|"NEUTRAL"|"BEAR"|"STRONG_BEAR",
            "state_1h":  tương tự,
            "state_15m": tương tự,
            "score_adj": float,   # điểm cộng/trừ vào candidate score
            "block_long":  bool,  # block LONG alt
            "block_short": bool,  # block SHORT alt
            "reason":    str,
        }
    """
    global _btc_context_cache, _btc_context_ts
    import time as _time

    result = {
        "state_4h": "NEUTRAL", "state_1h": "NEUTRAL", "state_15m": "NEUTRAL",
        "score_adj_long": 0.0, "score_adj_short": 0.0,
        "block_long": False, "block_short": False,
        "reason": "BTC context disabled",
    }

    if cfg and not getattr(cfg, "BTC_FILTER_ENABLED", True):
        return result

    if _time.time() - _btc_context_ts < _BTC_CONTEXT_TTL and _btc_context_cache:
        return _btc_context_cache

    try:
        from indicators import calculate_ema

        def _classify_btc(df) -> str:
            """STRONG_BULL / BULL / NEUTRAL / BEAR / STRONG_BEAR"""
            if df is None or len(df) < 55:
                return "NEUTRAL"
            close = df["close"]
            e9  = calculate_ema(close, 9)
            e21 = calculate_ema(close, 21)
            e50 = calculate_ema(close, 50)
            p   = float(close.iloc[-1])
            v9  = float(e9.iloc[-1]);  v21 = float(e21.iloc[-1]); v50 = float(e50.iloc[-1])
            # Slope EMA50
            e50_prev = float(e50.iloc[-5])
            slope = (v50 - e50_prev) / e50_prev * 100 if e50_prev > 0 else 0.0

            bull = (v9 > v21 and p > v50)
            bear = (v9 < v21 and p < v50)

            if bull and slope > 0.08:  return "STRONG_BULL"
            if bull:                   return "BULL"
            if bear and slope < -0.08: return "STRONG_BEAR"
            if bear:                   return "BEAR"
            return "NEUTRAL"

        from scanner import _klines_to_df
        kl_4h  = exchange.get_klines("BTCUSDT", "4h",  limit=60)
        kl_1h  = exchange.get_klines("BTCUSDT", "1h",  limit=60)
        kl_15m = exchange.get_klines("BTCUSDT", "15m", limit=60)
        df_4h  = _klines_to_df(kl_4h)
        df_1h  = _klines_to_df(kl_1h)
        df_15m = _klines_to_df(kl_15m)

        s4h  = _classify_btc(df_4h)
        s1h  = _classify_btc(df_1h)
        s15m = _classify_btc(df_15m)

        # Score adjustment cho LONG alt
        same_bonus    = getattr(cfg, "BTC_SAME_DIR_BONUS",    7) if cfg else 7
        same_1h_bonus = getattr(cfg, "BTC_SAME_DIR_1H_BONUS", 4) if cfg else 4
        opp_penalty   = getattr(cfg, "BTC_OPPOSE_PENALTY",    8) if cfg else 8
        opp_1h_penalty= getattr(cfg, "BTC_OPPOSE_1H_PENALTY", 5) if cfg else 5

        adj_long = adj_short = 0.0
        reasons  = []

        # 4H
        if s4h in ("BULL", "STRONG_BULL"):
            adj_long  += same_bonus;  adj_short -= opp_penalty
            reasons.append(f"BTC4H={s4h}")
        elif s4h in ("BEAR", "STRONG_BEAR"):
            adj_long  -= opp_penalty; adj_short += same_bonus
            reasons.append(f"BTC4H={s4h}")

        # 1H
        if s1h in ("BULL", "STRONG_BULL"):
            adj_long  += same_1h_bonus; adj_short -= opp_1h_penalty
            reasons.append(f"BTC1H={s1h}")
        elif s1h in ("BEAR", "STRONG_BEAR"):
            adj_long  -= opp_1h_penalty; adj_short += same_1h_bonus
            reasons.append(f"BTC1H={s1h}")

        # 15M — chỉ penalty khi strong ngược chiều (tránh noise)
        if s15m == "STRONG_BEAR":
            adj_long  -= 5; reasons.append("BTC15M=STRONG_BEAR")
        elif s15m == "STRONG_BULL":
            adj_short -= 5; reasons.append("BTC15M=STRONG_BULL")

        # Block hoàn toàn khi BTC strong ngược trên cả 3TF
        strong_block = getattr(cfg, "BTC_STRONG_BLOCK", True) if cfg else True
        block_long  = (strong_block
                       and s4h == "STRONG_BEAR"
                       and s1h in ("BEAR", "STRONG_BEAR")
                       and s15m in ("BEAR", "STRONG_BEAR"))
        block_short = (strong_block
                       and s4h == "STRONG_BULL"
                       and s1h in ("BULL", "STRONG_BULL")
                       and s15m in ("BULL", "STRONG_BULL"))

        result = {
            "state_4h":       s4h,
            "state_1h":       s1h,
            "state_15m":      s15m,
            "score_adj_long":  round(adj_long, 1),
            "score_adj_short": round(adj_short, 1),
            "block_long":      block_long,
            "block_short":     block_short,
            "reason":          " | ".join(reasons) if reasons else "BTC NEUTRAL",
        }

        _btc_context_cache = result
        _btc_context_ts    = _time.time()
        logger.debug(f"[BTC] {result['reason']} adj_long={adj_long:+.0f} adj_short={adj_short:+.0f} "
                     f"block_long={block_long} block_short={block_short}")

    except Exception as e:
        logger.debug(f"[BTC] get_btc_context error: {e}")

    return result


def find_swing_highs_lows(high: "pd.Series", low: "pd.Series",
                          lookback: int = 20) -> dict:
    """
    Tìm swing high/low thực sự (local peaks/troughs) trong lookback nến gần nhất.
    Swing high: high[i] > high[i-1] AND high[i] > high[i+1]
    Swing low:  low[i]  < low[i-1]  AND low[i]  < low[i+1]

    Returns:
        {
            "swing_highs": [float, ...],  # sorted descending
            "swing_lows":  [float, ...],  # sorted ascending
            "nearest_resistance": float,
            "nearest_support":    float,
        }
    """
    try:
        n = min(lookback, len(high) - 2)
        h_vals = high.values
        l_vals = low.values

        swing_highs = []
        swing_lows  = []
        for i in range(1, n):
            idx = len(h_vals) - n + i  # index trong series
            if idx <= 0 or idx >= len(h_vals) - 1:
                continue
            if h_vals[idx] > h_vals[idx-1] and h_vals[idx] > h_vals[idx+1]:
                swing_highs.append(float(h_vals[idx]))
            if l_vals[idx] < l_vals[idx-1] and l_vals[idx] < l_vals[idx+1]:
                swing_lows.append(float(l_vals[idx]))

        swing_highs.sort(reverse=True)
        swing_lows.sort()

        cur_price = float(high.iloc[-1])
        # Nearest resistance: swing high ngay trên giá hiện tại
        nearest_res = next((h for h in swing_highs if h > cur_price), swing_highs[0] if swing_highs else cur_price * 1.05)
        # Nearest support: swing low ngay dưới giá hiện tại
        cur_low     = float(low.iloc[-1])
        nearest_sup = next((l for l in reversed(swing_lows) if l < cur_low), swing_lows[-1] if swing_lows else cur_price * 0.95)

        return {
            "swing_highs":        swing_highs,
            "swing_lows":         swing_lows,
            "nearest_resistance": nearest_res,
            "nearest_support":    nearest_sup,
        }
    except Exception:
        cur = float(high.iloc[-1]) if len(high) > 0 else 1.0
        return {
            "swing_highs": [cur * 1.05],
            "swing_lows":  [cur * 0.95],
            "nearest_resistance": cur * 1.05,
            "nearest_support":    cur * 0.95,
        }


def check_1h_location(df_1h: "pd.DataFrame", signal: str,
                      entry_price: float, atr_1h: float,
                      cfg=None) -> dict:
    """
    Kiểm tra vị trí giá trên 1H so với swing S/R thực sự.

    Returns:
        {
            "ok":       bool,
            "room_atr": float,   # room tính bằng ATR 1H
            "nearest":  float,   # mức S/R gần nhất theo hướng TP
            "reason":   str,
        }
    """
    min_room = getattr(cfg, "LOCATION_MIN_ROOM_ATR", 1.5) if cfg else 1.5
    lookback = getattr(cfg, "LOCATION_SWING_LOOKBACK", 20) if cfg else 20

    try:
        swings  = find_swing_highs_lows(df_1h["high"], df_1h["low"], lookback)
        if signal == "LONG":
            nearest = swings["nearest_resistance"]
            room    = (nearest - entry_price) / atr_1h if atr_1h > 0 else 99.0
            ok      = room >= min_room
            reason  = (f"LONG location: resistance={nearest:.6f} room={room:.1f}×ATR"
                       f" {'✅' if ok else '❌ < ' + str(min_room) + 'xATR'}")
        else:  # SHORT
            nearest = swings["nearest_support"]
            room    = (entry_price - nearest) / atr_1h if atr_1h > 0 else 99.0
            ok      = room >= min_room
            reason  = (f"SHORT location: support={nearest:.6f} room={room:.1f}×ATR"
                       f" {'✅' if ok else '❌ < ' + str(min_room) + 'xATR'}")

        return {"ok": ok, "room_atr": round(room, 2), "nearest": nearest, "reason": reason}

    except Exception as e:
        return {"ok": True, "room_atr": 99.0, "nearest": 0.0, "reason": f"location check error: {e}"}


def calc_structure_sl_tp(df_15m: "pd.DataFrame", signal: str,
                         entry_price: float, cfg=None) -> dict:
    """
    Tính SL dựa trên structure (swing low/high) + ATR buffer.
    Tính TP tại nearest swing resistance/support trên 15m.

    Returns:
        {
            "sl":         float,
            "tp":         float,
            "sl_pct":     float,   # % SL cách entry
            "tp_pct":     float,
            "rr":         float,
            "sl_reason":  str,
            "tp_reason":  str,
        }
    """
    from indicators import calculate_atr
    try:
        atr     = float(calculate_atr(df_15m["high"], df_15m["low"], df_15m["close"]).iloc[-1])
        swings  = find_swing_highs_lows(df_15m["high"], df_15m["low"], lookback=20)

        atr_buf   = getattr(cfg, "SL_ATR_BUFFER_MULT", 0.5) if cfg else 0.5
        sl_min    = getattr(cfg, "SL_MIN_PCT", 0.008)        if cfg else 0.008
        sl_max    = getattr(cfg, "SL_MAX_PCT", 0.06)         if cfg else 0.06
        use_struct= getattr(cfg, "SL_STRUCTURE_ENABLED", True) if cfg else True

        # ATR-based SL — tối thiểu 2% cách entry, scale theo volatility
        # Đây là fallback an toàn khi structure không tìm được swing phù hợp
        atr_sl_long  = entry_price - max(atr * 2.0, entry_price * 0.02)
        atr_sl_short = entry_price + max(atr * 2.0, entry_price * 0.02)

        if signal == "LONG":
            # Structure SL: swing low gần nhất DƯỚI entry - ATR buffer
            valid_lows = [l for l in swings["swing_lows"] if l < entry_price]
            if valid_lows and use_struct:
                struct_sl = max(valid_lows) - atr * atr_buf
                # Chỉ dùng structure SL nếu nó thực sự dưới entry VÀ không quá xa
                if struct_sl < entry_price and struct_sl >= entry_price * (1 - sl_max):
                    sl = struct_sl
                    sl_reason = f"struct_sl={struct_sl:.6f} (swing_low - {atr_buf}×ATR)"
                else:
                    sl = atr_sl_long
                    sl_reason = f"atr_sl (struct out of range) ATR×2={atr*2:.6f}"
            else:
                sl = atr_sl_long
                sl_reason = f"atr_sl (no valid swing) ATR×2={atr*2:.6f}"

            # Đảm bảo SL dưới entry, tối thiểu sl_min
            sl = min(sl, entry_price * (1 - sl_min))   # không quá sát entry
            sl = max(sl, entry_price * (1 - sl_max))   # không quá xa entry
            if sl >= entry_price:
                sl = atr_sl_long

            # TP: nearest swing resistance TRÊN entry_price
            valid_highs = [h for h in swings["swing_highs"] if h > entry_price]
            tp_struct = min(valid_highs) if valid_highs else 0.0
            tp_atr    = entry_price + atr * 8
            tp        = tp_struct if use_struct and tp_struct > entry_price else tp_atr
            tp_reason = f"swing_res={tp_struct:.6f}" if tp == tp_struct else f"atr_tp ATR×8"

        else:  # SHORT
            # Structure SL: swing high gần nhất TRÊN entry + ATR buffer
            valid_highs = [h for h in swings["swing_highs"] if h > entry_price]
            if valid_highs and use_struct:
                struct_sl = min(valid_highs) + atr * atr_buf
                if struct_sl > entry_price and struct_sl <= entry_price * (1 + sl_max):
                    sl = struct_sl
                    sl_reason = f"struct_sl={struct_sl:.6f} (swing_high + {atr_buf}×ATR)"
                else:
                    sl = atr_sl_short
                    sl_reason = f"atr_sl (struct out of range) ATR×2={atr*2:.6f}"
            else:
                sl = atr_sl_short
                sl_reason = f"atr_sl (no valid swing) ATR×2={atr*2:.6f}"

            # Đảm bảo SL trên entry
            sl = max(sl, entry_price * (1 + sl_min))
            sl = min(sl, entry_price * (1 + sl_max))
            if sl <= entry_price:
                sl = atr_sl_short

            # TP: nearest swing support DƯỚI entry_price
            valid_lows = [l for l in swings["swing_lows"] if l < entry_price]
            tp_struct = max(valid_lows) if valid_lows else 0.0
            tp_atr    = entry_price - atr * 8
            tp        = tp_struct if use_struct and tp_struct < entry_price else tp_atr
            tp_reason = f"swing_sup={tp_struct:.6f}" if tp == tp_struct else f"atr_tp ATR×8"
        sl = round(sl, 8)
        tp = round(tp, 8)

        risk   = abs(entry_price - sl)
        reward = abs(tp - entry_price)
        rr     = reward / risk if risk > 0 else 0.0
        sl_pct = risk / entry_price * 100
        tp_pct = reward / entry_price * 100

        return {
            "sl": sl, "tp": tp,
            "sl_pct": round(sl_pct, 3), "tp_pct": round(tp_pct, 3),
            "rr": round(rr, 2),
            "sl_reason": sl_reason, "tp_reason": tp_reason,
        }

    except Exception as e:
        # Fallback ATR-based
        from indicators import calculate_atr
        atr = float(calculate_atr(df_15m["high"], df_15m["low"], df_15m["close"]).iloc[-1])
        if signal == "LONG":
            sl = round(entry_price - max(atr * 2.0, entry_price * 0.02), 8)
            tp = round(entry_price + atr * 8, 8)
        else:
            sl = round(entry_price + max(atr * 2.0, entry_price * 0.02), 8)
            tp = round(entry_price - atr * 8, 8)
        risk   = abs(entry_price - sl)
        reward = abs(tp - entry_price)
        rr     = reward / risk if risk > 0 else 0.0
        return {
            "sl": sl, "tp": tp,
            "sl_pct": round(risk/entry_price*100, 3),
            "tp_pct": round(reward/entry_price*100, 3),
            "rr": round(rr, 2),
            "sl_reason": f"fallback ATR×2 ({e})",
            "tp_reason":  "fallback ATR×8",
        }


def check_no_chase(current_price: float, planned_entry: float,
                   atr: float, signal: str, cfg=None) -> bool:
    """
    Trả về True nếu giá đã chạy quá xa planned entry → KHÔNG CHASE.
    Dùng % cách entry thay vì ATR×mult để tránh bị quá chặt với coin giá cao.
    LONG:  current_price > planned_entry × (1 + max_pct) → chase
    SHORT: current_price < planned_entry × (1 - max_pct) → chase
    max_pct = max(NO_CHASE_ATR_MULT × atr/entry, 0.03) — tối thiểu 3%
    """
    mult    = getattr(cfg, "NO_CHASE_ATR_MULT", 0.5) if cfg else 0.5
    # Tính % tương đương, tối thiểu 3% để không bị quá chặt
    if planned_entry > 0 and atr > 0:
        atr_pct = atr / planned_entry
        max_pct = max(mult * atr_pct, 0.03)   # tối thiểu 3%
    else:
        max_pct = 0.03

    if signal == "LONG":
        return current_price > planned_entry * (1 + max_pct)
    else:
        return current_price < planned_entry * (1 - max_pct)


@dataclass
class CoinScore:
    symbol: str
    signal: str          # LONG / SHORT / HOLD
    score: float         # 0-100, càng cao càng tốt
    rsi: float
    trend: str           # BULLISH / BEARISH / NEUTRAL
    atr_pct: float       # ATR % — đo độ biến động
    reason: str          # Lý do vào lệnh


def score_coin(symbol: str, df: pd.DataFrame, config) -> Optional[CoinScore]:
    """
    Chấm điểm 1 coin dựa trên nhiều tiêu chí:
    - Signal strength (RSI, EMA, MACD)
    - Trend alignment
    - Volatility (ATR)
    - Volume
    """
    try:
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # Tính indicators
        rsi = calculate_rsi(close, config.RSI_PERIOD).iloc[-1]
        ema_fast = calculate_ema(close, config.EMA_FAST).iloc[-1]
        ema_slow = calculate_ema(close, config.EMA_SLOW).iloc[-1]
        ema_trend = calculate_ema(close, config.EMA_TREND).iloc[-1]
        atr = calculate_atr(high, low, close).iloc[-1]
        current_price = close.iloc[-1]
        atr_pct = (atr / current_price) * 100

        # Volume surge: volume hiện tại so với trung bình 20 nến
        vol_avg = volume.rolling(20).mean().iloc[-1]
        vol_ratio = volume.iloc[-1] / vol_avg if vol_avg > 0 else 1.0

        # Lấy signal — pass symbol để chọn đúng strategy
        signal = get_signal(df, config, symbol=symbol)

        if signal == "HOLD":
            return None

        # Filter entry quality: chỉ vào lệnh ở vùng giá tốt
        # SHORT: chỉ short khi RSI >= 40
        if signal == "SHORT" and rsi < 40:
            return None

        # LONG RSI filter: nới lỏng cho coin pump mạnh (pullback sau pump)
        # Sau pump +20%, RSI vẫn > 60 khi đang ở đáy pullback → không block
        # Chỉ block khi RSI > 70 (thực sự overbought) hoặc
        # RSI > 60 nhưng giá không giảm (không phải pullback)
        if signal == "LONG" and rsi > 70:
            return None
        if signal == "LONG" and rsi > 60:
            # Cho phép nếu giá đang pullback (giá hiện tại < nến trước 3 nến)
            price_3_bars_ago = close.iloc[-4] if len(close) >= 4 else close.iloc[0]
            is_pulling_back = current_price < price_3_bars_ago * 0.998
            if not is_pulling_back:
                return None

        # Kiểm tra giá đang gần recent high/low (20 nến)
        recent_high = high.rolling(20).max().iloc[-1]
        recent_low  = low.rolling(20).min().iloc[-1]
        price_range = recent_high - recent_low
        if price_range > 0:
            price_pos = (current_price - recent_low) / price_range
            if signal == "SHORT" and price_pos < 0.4:
                return None
            # Nới lỏng price_pos filter cho LONG:
            # Sau pump mạnh, range 20 nến rất rộng → đáy pullback vẫn > 0.6
            # Chỉ block khi giá thực sự ở đỉnh range (> 0.80)
            if signal == "LONG" and price_pos > 0.80:
                return None

        # --- Chấm điểm ---
        score = 0.0
        reasons = []

        # 1. RSI strength (30 điểm)
        if signal == "LONG":
            # RSI càng gần oversold càng tốt khi vừa thoát ra
            rsi_score = max(0, (50 - rsi) / 15 * 30) if rsi < 50 else 10
        else:
            rsi_score = max(0, (rsi - 50) / 15 * 30) if rsi > 50 else 10
        score += min(rsi_score, 30)
        reasons.append(f"RSI={rsi:.1f}")

        # 2. Trend alignment (25 điểm)
        if signal == "LONG" and current_price > ema_trend:
            score += 25
            trend = "BULLISH"
            reasons.append("Trend↑")
        elif signal == "SHORT" and current_price < ema_trend:
            score += 25
            trend = "BEARISH"
            reasons.append("Trend↓")
        else:
            trend = "NEUTRAL"

        # 3. EMA alignment (20 điểm)
        if signal == "LONG" and ema_fast > ema_slow:
            score += 20
            reasons.append("EMA cross↑")
        elif signal == "SHORT" and ema_fast < ema_slow:
            score += 20
            reasons.append("EMA cross↓")

        # 4. Volume surge (15 điểm)
        if vol_ratio >= 2.0:
            score += 15
            reasons.append(f"Vol×{vol_ratio:.1f}")
        elif vol_ratio >= 1.5:
            score += 10
            reasons.append(f"Vol×{vol_ratio:.1f}")
        elif vol_ratio >= 1.2:
            score += 5

        # 5. ATR volatility (10 điểm) — cần đủ biến động để có lợi nhuận
        if 1.5 <= atr_pct <= 5.0:
            score += 10
            reasons.append(f"ATR={atr_pct:.1f}%")
        elif atr_pct > 5.0:
            score += 5  # Quá volatile thì trừ điểm

        # Bonus điểm cho priority coins
        if symbol in PRIORITY_COINS:
            score = min(score + 10, 100)
            reasons.append("⭐PRIORITY")

        return CoinScore(
            symbol=symbol,
            signal=signal,
            score=round(score, 1),
            rsi=round(rsi, 1),
            trend=trend,
            atr_pct=round(atr_pct, 2),
            reason=" | ".join(reasons)
        )

    except Exception as e:
        logger.debug(f"Score failed for {symbol}: {e}")
        return None



def scan_market(exchange, config, min_score: float = 40.0, notifier=None) -> Optional[CoinScore]:
    """
    Quét coin theo thứ tự P0:
    1. Market Regime (4H) — CHAOS/RANGE → skip
    2. BTC Context (4H+1H+15M) — block/adjust score
    3. 4H+1H bias xác định hướng
    4. 1H Location — không entry sát S/R
    5. 15M setup + score
    6. compute_signal_score (WR check)
    7. Rank candidates → chọn best theo score+RR composite
    """
    base_url = getattr(config, "LIVE_BASE_URL", "https://demo-fapi.binance.com")

    active = get_active_universe(base_url, top_n=10)
    logger.info(f"🔍 Scanning {len(active)} coins (P0 regime+BTC+location)...")
    candidates = []

    # ── Lấy BTC context 1 lần cho toàn bộ vòng scan ────────────────
    btc_ctx = {}
    try:
        btc_ctx = get_btc_context(exchange, config)
        logger.debug(f"[BTC] {btc_ctx.get('reason','')} "
                     f"adj_long={btc_ctx.get('score_adj_long',0):+.0f} "
                     f"adj_short={btc_ctx.get('score_adj_short',0):+.0f}")
    except Exception as _e:
        logger.debug(f"[BTC] context error: {_e}")

    # ── Cleanup expired pending ──────────────────────────────────────
    now_ts = time.time()
    expired = [s for s, v in _pending_watch.items() if now_ts - v["ts"] > _PENDING_TTL]
    for s in expired:
        logger.info(f"  🗑  PENDING expired: {s}")
        _pending_watch.pop(s, None)

    # ── Retry pending coins ──────────────────────────────────────────
    if _pending_watch:
        logger.info(f"  🔄 Retrying {len(_pending_watch)} pending coins...")
        for p_sym, p_info in list(_pending_watch.items()):
            try:
                p_info["retry"] += 1
                klines_1h  = exchange.get_klines(p_sym, "1h",  limit=100)
                klines_4h  = exchange.get_klines(p_sym, "4h",  limit=100)
                klines_15m = exchange.get_klines(p_sym, "15m", limit=100)
                df_1h  = _klines_to_df(klines_1h)
                df_4h  = _klines_to_df(klines_4h)
                df_15m = _klines_to_df(klines_15m)

                bias = p_info["signal"]
                try:
                    css = compute_signal_score(df_15m, df_1h, df_4h)
                except Exception:
                    css = {"signal": "WAIT", "win_rate": 0, "long_score": 0,
                           "short_score": 0, "long_reasons": [], "short_reasons": []}

                css_signal = css["signal"]
                win_rate   = css["win_rate"]

                if css_signal == "WAIT" or css_signal != bias:
                    logger.debug(f"  ↻  {p_sym} pending retry#{p_info['retry']}: "
                                 f"css={css_signal} WR={win_rate:.0f}%")
                    continue
                if win_rate < 60.0:
                    logger.debug(f"  ↻  {p_sym} pending retry#{p_info['retry']}: "
                                 f"WR={win_rate:.0f}% < 60%")
                    continue

                _pending_watch.pop(p_sym, None)
                base_score  = p_info["score"]
                wr_bonus    = 10 if win_rate >= 80 else (5 if win_rate >= 70 else 0)
                final_score = min(base_score + wr_bonus, 100)
                css_reasons = css["long_reasons"] if bias == "LONG" else css["short_reasons"]

                final = CoinScore(
                    symbol  = p_sym,
                    signal  = bias,
                    score   = final_score,
                    rsi     = calculate_rsi(df_15m["close"], 14).iloc[-1],
                    trend   = "BULLISH" if bias == "LONG" else "BEARISH",
                    atr_pct = (calculate_atr(df_15m["high"], df_15m["low"],
                                             df_15m["close"]).iloc[-1]
                               / df_15m["close"].iloc[-1] * 100),
                    reason  = f"⟳PENDING→LIVE | WR={win_rate:.0f}% | " + " | ".join(css_reasons[:3])
                )
                candidates.append(final)
                logger.info(f"  🔔 PENDING→LIVE {p_sym}: {bias} score={final_score} WR={win_rate:.0f}%")
            except Exception as _e:
                logger.debug(f"  ⚠️  pending retry {p_sym}: {_e}")

    # ── MAIN SCAN: P0 Pipeline ───────────────────────────────────────
    for symbol in active:
        try:
            # ═══ BƯỚC 1: Fetch 4H + 1H data ═══
            klines_4h = exchange.get_klines(symbol, "4h", limit=100)
            klines_1h = exchange.get_klines(symbol, "1h", limit=100)
            df_4h = _klines_to_df(klines_4h)
            df_1h = _klines_to_df(klines_1h)

            # ═══ BƯỚC 2: Market Regime — CHAOS/RANGE → skip ═══
            regime_info = detect_regime(df_4h, config)
            regime = regime_info["regime"]

            if regime == "CHAOS":
                logger.info(f"  ⛔ {symbol}: CHAOS → skip | {regime_info['reason']}")
                continue
            if regime == "RANGE":
                logger.debug(f"  ⏭  {symbol}: RANGE → skip trend-following | {regime_info['reason']}")
                continue

            # ═══ BƯỚC 3: BTC Filter (chỉ áp dụng cho ALT, không cho BTC/ETH) ═══
            is_btc_eth = symbol in ("BTCUSDT", "ETHUSDT")
            btc_block_long  = btc_ctx.get("block_long",  False) and not is_btc_eth
            btc_block_short = btc_ctx.get("block_short", False) and not is_btc_eth

            # ═══ BƯỚC 4: 4H+1H bias ═══
            close_4h = df_4h["close"]
            ema9_4h  = calculate_ema(close_4h, 9).iloc[-1]
            ema21_4h = calculate_ema(close_4h, 21).iloc[-1]
            ema50_4h = calculate_ema(close_4h, 50).iloc[-1]
            price_4h = close_4h.iloc[-1]

            trend_4h = "NEUTRAL"
            if ema9_4h > ema21_4h and price_4h > ema50_4h:
                trend_4h = "LONG"
            elif ema9_4h < ema21_4h and price_4h < ema50_4h:
                trend_4h = "SHORT"

            close_1h = df_1h["close"]
            ema9_1h  = calculate_ema(close_1h, 9).iloc[-1]
            ema21_1h = calculate_ema(close_1h, 21).iloc[-1]

            trend_1h = "NEUTRAL"
            if ema9_1h > ema21_1h:
                trend_1h = "LONG"
            elif ema9_1h < ema21_1h:
                trend_1h = "SHORT"

            if trend_4h == trend_1h and trend_4h != "NEUTRAL":
                bias = trend_4h; strength = "STRONG"
            elif trend_4h != "NEUTRAL":
                bias = trend_4h; strength = "MEDIUM"
            elif trend_1h != "NEUTRAL":
                bias = trend_1h; strength = "MEDIUM"
            else:
                logger.debug(f"  ⏭  {symbol}: 4h={trend_4h} 1h={trend_1h} → NEUTRAL")
                continue

            # Block nếu BTC ngược chiều mạnh
            if bias == "LONG" and btc_block_long:
                logger.info(f"  🚫 {symbol}: LONG blocked by BTC strong bearish")
                continue
            if bias == "SHORT" and btc_block_short:
                logger.info(f"  🚫 {symbol}: SHORT blocked by BTC strong bullish")
                continue

            # ═══ BƯỚC 5: 15m setup ═══
            klines_15m = exchange.get_klines(symbol, "15m", limit=100)
            df_15m = _klines_to_df(klines_15m)

            scored = score_coin(symbol, df_15m, config)

            if not scored or scored.signal != bias:
                volatile = is_volatile_coin(df_1h, threshold_pct=4.0)
                if volatile:
                    pb_signal = get_pullback_signal(df_15m, config, bias)
                    if pb_signal == bias:
                        rsi_val = calculate_rsi(df_15m["close"], 14).iloc[-1]
                        atr_val = calculate_atr(df_15m["high"], df_15m["low"], df_15m["close"]).iloc[-1]
                        scored = CoinScore(
                            symbol=symbol, signal=bias, score=55.0,
                            rsi=round(rsi_val, 1),
                            trend="BULLISH" if bias == "LONG" else "BEARISH",
                            atr_pct=round(atr_val / df_15m["close"].iloc[-1] * 100, 2),
                            reason=f"🔥PULLBACK {bias}"
                        )
                if not scored or scored.signal != bias:
                    logger.debug(f"  ⏭  {symbol}: bias={bias} nhưng 15m không có entry")
                    continue

            # ═══ BƯỚC 6: 1H Location — không entry sát S/R ═══
            cur_price = float(df_15m["close"].iloc[-1])
            atr_1h    = float(calculate_atr(df_1h["high"], df_1h["low"], df_1h["close"]).iloc[-1])
            loc_check = check_1h_location(df_1h, bias, cur_price, atr_1h, config)
            if not loc_check["ok"]:
                logger.info(f"  📍 {symbol}: {loc_check['reason']} → PENDING location")
                _pending_watch[symbol] = {
                    "signal": bias, "score": scored.score, "bias": bias,
                    "win_rate": 0, "ts": time.time(), "retry": 0, "css": {},
                    "skip_reason": "location",
                }
                continue

            # ═══ BƯỚC 7: MTF bonus + BTC score adjustment ═══
            bonus = 15 if strength == "STRONG" else 8
            final_score = min(scored.score + bonus, 100)

            # BTC score adjustment (không áp dụng cho BTC/ETH)
            if not is_btc_eth:
                adj = btc_ctx.get("score_adj_long", 0) if bias == "LONG" \
                      else btc_ctx.get("score_adj_short", 0)
                final_score = max(0, min(100, final_score + adj))

            # ═══ BƯỚC 8: compute_signal_score (WR check) ═══
            try:
                css = compute_signal_score(df_15m, df_1h, df_4h)
            except Exception:
                css = {"signal": "WAIT", "win_rate": 0, "long_score": 0,
                       "short_score": 0, "long_reasons": [], "short_reasons": []}

            css_signal  = css["signal"]
            win_rate    = css["win_rate"]
            css_reasons = css["long_reasons"] if bias == "LONG" else css["short_reasons"]

            if css_signal == "WAIT" or css_signal != bias:
                _pending_watch[symbol] = {
                    "signal": bias, "score": final_score, "bias": bias,
                    "win_rate": win_rate, "ts": time.time(), "retry": 0, "css": css,
                }
                logger.info(f"  📋 PENDING {symbol}: css={css_signal} vs bias={bias} WR={win_rate:.0f}%")
                continue

            WIN_RATE_MIN = 60.0
            if win_rate < WIN_RATE_MIN:
                _pending_watch[symbol] = {
                    "signal": bias, "score": final_score, "bias": bias,
                    "win_rate": win_rate, "ts": time.time(), "retry": 0, "css": css,
                }
                logger.info(f"  📊 LOW WR {symbol}: {bias} WR={win_rate:.0f}% < {WIN_RATE_MIN:.0f}%")
                continue

            # ═══ PASS — tạo candidate với MSS analysis ═══
            _pending_watch.pop(symbol, None)
            wr_bonus    = 10 if win_rate >= 80 else (5 if win_rate >= 70 else 0)
            final_score = min(final_score + wr_bonus, 100)
            mtf_tag  = "MTF✅" if strength == "STRONG" else "MTF⚡"
            reg_tag  = regime_info["regime"]
            btc_tag  = btc_ctx.get("reason", "")[:30]

            # ═══ BƯỚC 9: MSS / Liquidity Sweep Analysis ═══
            mss_result  = None
            mss_tag     = ""
            mss_enabled = getattr(config, "MSS_ENGINE_ENABLED", True)

            if mss_enabled:
                try:
                    from mss_engine import analyze_mss, get_mss_pending

                    # Fetch 5m chỉ khi MSS engine cần (tiết kiệm API)
                    df_5m = None
                    if getattr(config, "MSS_USE_5M_CONFIRM", True):
                        try:
                            klines_5m = exchange.get_klines(symbol, "5m", limit=20)
                            df_5m     = _klines_to_df(klines_5m)
                        except Exception:
                            pass

                    mss_result = analyze_mss(df_15m, df_5m, bias, config)
                    tier       = mss_result.tier

                    if tier == "A":
                        # Tier A: FULL confidence — bonus score cao nhất
                        tier_bonus   = getattr(config, "MSS_TIER_A_BONUS", 20)
                        final_score  = min(final_score + tier_bonus, 100)
                        mss_tag      = f"MSS_A(conf={mss_result.confidence:.0f}%)"
                        logger.info(f"  🎯 {symbol} MSS TIER A: {mss_result.reason[:80]}")

                    elif tier == "B":
                        # Tier B: HIGH confidence — bonus nhỏ hơn
                        tier_bonus   = getattr(config, "MSS_TIER_B_BONUS", 10)
                        final_score  = min(final_score + tier_bonus, 100)
                        mss_tag      = f"MSS_B(conf={mss_result.confidence:.0f}%)"
                        logger.info(f"  📊 {symbol} MSS TIER B: {mss_result.reason[:80]}")

                    elif tier == "C":
                        # Tier C: PENDING MSS — lưu lại chờ fast-check
                        get_mss_pending().add(symbol, bias, mss_result)
                        logger.info(f"  ⏳ {symbol} MSS TIER C PENDING: {mss_result.reason[:80]}")
                        # Vẫn tạo candidate nhưng không có MSS bonus
                        mss_tag = "MSS_C(pending)"

                    else:
                        # Tier D: không có sweep — không bonus nhưng vẫn pass
                        mss_tag = "MSS_D(no_sweep)"

                except Exception as _mss_e:
                    logger.debug(f"  [MSS] {symbol} error: {_mss_e}")
                    mss_tag = "MSS_err"

            final = CoinScore(
                symbol=symbol, signal=bias, score=final_score,
                rsi=scored.rsi, trend=scored.trend, atr_pct=scored.atr_pct,
                reason=(f"{reg_tag} | 4h={trend_4h} 1h={trend_1h} | {mtf_tag} "
                        f"WR={win_rate:.0f}% | {mss_tag} | {scored.reason} | {btc_tag}")
            )
            # Đính kèm mss_result vào candidate để bot.py dùng entry_price
            final.mss_result = mss_result  # type: ignore[attr-defined]

            if final.score >= min_score:
                candidates.append(final)
                logger.info(f"  ✅ {symbol}: {bias} score={final.score} "
                            f"WR={win_rate:.0f}% loc_room={loc_check['room_atr']:.1f}×ATR "
                            f"| {final.reason[:140]}")

        except Exception as e:
            logger.debug(f"  ⚠️  {symbol} skip: {e}")

    # ═══ RANK candidates theo composite score (score + RR bonus) ═══
    # Không dùng raw score đơn thuần — tính composite có RR component
    def _candidate_quality(c: CoinScore) -> float:
        return c.score  # RR check được làm trong bot.py (sau khi có entry zone)

    candidates_sorted = sorted(candidates, key=_candidate_quality, reverse=True)
    scan_market._last_candidates = candidates_sorted

    if not candidates_sorted:
        logger.info("  No strong signals found.")
        return None

    best = candidates_sorted[0]
    logger.info(f"🏆 Best: {best.symbol} | {best.signal} | Score={best.score}")
    return best

scan_market._last_candidates = []

# ── Pending watch: coin pass MTF nhưng 1m chưa trigger ──────
_pending_watch: dict = {}
_PENDING_TTL       = 600   # giây — 10 phút
_PENDING_MAX_RETRY = 10    # tối đa 10 lần retry

# ── Pump scan state ──────────────────────────────────────────
_pump_last_scan: float = 0   # timestamp lần quét pump gần nhất


def run_pump_scan(exchange, config, notifier=None) -> dict:
    """
    Quét PUMP_WATCH_COINS tìm đỉnh pump (SHORT) và pump đang lên (ALERT).

    Trả về dict:
        {
            "confirmed": [PumpSignal, ...],   # đỉnh pump xác nhận → SHORT
            "alerts":    [PumpAlertSignal, ...],  # pump đang lên → thông báo
        }

    Gọi từ pump_scan_engine thread trong bot.py (mỗi 30s).
    """
    global _pump_last_scan

    interval = getattr(config, "PUMP_SCAN_INTERVAL_SECONDS", 30)
    now = time.time()
    if now - _pump_last_scan < interval:
        return {"confirmed": [], "alerts": []}
    _pump_last_scan = now

    watch = list(getattr(config, "PUMP_WATCH_COINS", []))

    if not getattr(config, "PUMP_DETECTOR_ENABLED", True):
        return {"confirmed": [], "alerts": []}

    logger.info(f"[PumpScan] Scanning {len(watch)} coins for pump tops + alerts...")

    from pump_detector import scan_for_pump_tops, scan_for_pump_alerts
    confirmed = scan_for_pump_tops(exchange, watch, config, notifier)
    alerts    = scan_for_pump_alerts(exchange, watch, config, notifier)

    if confirmed:
        logger.info(f"[PumpScan] Confirmed tops: {[r.symbol for r in confirmed]}")
    if alerts:
        logger.info(f"[PumpScan] Pump alerts: {[r.symbol for r in alerts]}")

    return {"confirmed": confirmed, "alerts": alerts}


def _klines_to_df(klines: list) -> pd.DataFrame:
    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df
# ///Copy paste 2 lệnh này vào Terminal:

# Lệnh 1:

# cd /Users/leduykhang/Documents/AI-CONTEXT-V2-master/AI-CONTEXT-V2-master/trading-bot
# Lệnh 2:



# python3 bot.py
# ╔══════════════════════════════════════════════════════════╗
# ║         🤖  MULTI-COIN BOT — BINANCE FUTURES             ║
# ╠══════════════════════════════════════════════════════════╣
# ║  🕐 11:30:00   💼 Balance: $4,963.53 USDT                ║
# ║  📈 Tổng PnL: +$0.00   ✅0 win  ❌0 loss  |  Scan #1    ║
# ╠══════════════════════════════════════════════════════════╣
# ║      💤  CHƯA CÓ LỆNH — Đang quét thị trường...         ║
# ╠══════════════════════════════════════════════════════════╣
# ║                    💹  GIÁ REALTIME                      ║
# ║  BTC  $ 81,012  ETH  $  2,307  BNB  $   662  SOL  $  96 ║
# ...
# ╠══════════════════════════════════════════════════════════╣
# ║                   📋  LỊCH SỬ LỆNH                       ║
# ║  Chưa có lệnh nào được đóng                              ║
# ╚══════════════════════════════════════════════════════════╝
# ╠══════════════════════════════════════════════════════════╣
# ║           📌  LỆNH ĐANG MỞ (REALTIME)                    ║
# ║  Coin     : SOLUSDT   🟢 LONG   5x                       ║
# ║  Entry    : $96.2300  ▶  Giá HT: $97.8500                ║
# ║  🛑 SL    : $93.8000   (còn 2.09% đến SL)                ║
# ║  🎯 TP    : $100.850   (còn 3.07% đến TP)                ║
# ║  📦 Qty   : 10.3   (~$991.17 USDT)                       ║
# ║  Progress : [████░░░░░░] 40%                             ║
# ║  📈 PnL   : +$83.50  (+1.68%)  x5                        ║
# ╠══════════════════════════════════════════════════════════╣
# ║                   📋  LỊCH SỬ LỆNH                       ║
# ║  Tổng: 3  ✅2 win  ❌1 loss  |  PnL: +$124.30            ║
# ║  ──────────────────────────────────────────────          ║
# ║  #  Coin       Side  Entry    Close    PnL$     %   Giờ  ║
# ║  ──────────────────────────────────────────────          ║
# ║  ✅1 SOLUSDT   LONG  $94.230  $98.450  +$86.50 +4.5% 10:32║
# ║  ❌2 BTCUSDT   SHORT $81200   $81850   -$33.20 -0.8% 09:15║
# ║  ✅3 SUIUSDT   LONG  $1.2800  $1.3500  +$71.00 +5.5% 08:44║
# ╚══════════════════════════════════════════════════════════╝
