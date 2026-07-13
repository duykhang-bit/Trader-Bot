# ============================================================
# INDICATORS — 2 Strategy: Low-vol (BTC/ETH) & High-vol (Alts)
# ============================================================
import pandas as pd
import numpy as np


# ── Base indicators ───────────────────────────────────────────
def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain  = delta.where(delta > 0, 0.0)
    loss  = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(com=period-1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period-1, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_ema(prices: pd.Series, period: int) -> pd.Series:
    return prices.ewm(span=period, adjust=False).mean()

def calculate_macd(prices: pd.Series, fast=12, slow=26, signal=9):
    ema_fast   = calculate_ema(prices, fast)
    ema_slow   = calculate_ema(prices, slow)
    macd_line  = ema_fast - ema_slow
    signal_line= calculate_ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line

def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period=14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(com=period-1, min_periods=period).mean()

def calculate_bollinger(prices: pd.Series, period=20, std=2.0):
    mid   = prices.rolling(period).mean()
    sigma = prices.rolling(period).std()
    return mid + std*sigma, mid, mid - std*sigma

def calculate_volume_ma(volume: pd.Series, period=20) -> pd.Series:
    return volume.rolling(window=period).mean()


# ── Coin classification ───────────────────────────────────────
LOW_VOL_COINS  = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}   # ít biến động
HIGH_VOL_COINS = {
    "SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT",
    "LINKUSDT","DOTUSDT","NEARUSDT","APTUSDT","ARBUSDT",
    "OPUSDT","INJUSDT","TIAUSDT","SEIUSDT","SUIUSDT",
    "CRVUSDT","LTCUSDT","RUNEUSDT","VETUSDT","KAVAUSDT",
}

def get_coin_type(symbol: str) -> str:
    if symbol in LOW_VOL_COINS:
        return "LOW_VOL"
    return "HIGH_VOL"


# ── HTF trend ─────────────────────────────────────────────────
def get_htf_trend(df_htf: pd.DataFrame) -> str:
    if df_htf is None or len(df_htf) < 50:
        return "NEUTRAL"
    close = df_htf["close"]
    ema20 = calculate_ema(close, 20)
    ema50 = calculate_ema(close, 50)
    rsi   = calculate_rsi(close, 14)
    p     = close.iloc[-1]
    e20   = ema20.iloc[-1]
    e50   = ema50.iloc[-1]
    r     = rsi.iloc[-1]
    if p > e20 > e50 and r > 50: return "UP"
    if p < e20 < e50 and r < 50: return "DOWN"
    return "NEUTRAL"


# ── Strategy 1: LOW_VOL (BTC/ETH) — Breakout + BB squeeze ────
def get_signal_low_vol(df: pd.DataFrame, config) -> str:
    """
    BTC/ETH ít biến động → dùng Bollinger Band breakout:
    - LONG : giá đóng cửa TRÊN BB upper + RSI > 50 + volume surge
    - SHORT: giá đóng cửa DƯỚI BB lower + RSI < 50 + volume surge
    Không cần crossover — chỉ cần breakout khỏi dải BB.
    """
    close  = df["close"]
    volume = df["volume"]
    if len(close) < 30:
        return "HOLD"

    bb_upper, bb_mid, bb_lower = calculate_bollinger(close, 20, 2.0)
    rsi    = calculate_rsi(close, 14)
    vol_ma = calculate_volume_ma(volume, 20)

    cur_price  = close.iloc[-1]
    prev_price = close.iloc[-2]
    cur_upper  = bb_upper.iloc[-1]
    cur_lower  = bb_lower.iloc[-1]
    cur_rsi    = rsi.iloc[-1]
    cur_vol    = volume.iloc[-1]
    avg_vol    = vol_ma.iloc[-1]
    vol_surge  = cur_vol > avg_vol * 0.8  # volume tăng nhẹ là đủ

    # BB width — chỉ vào khi BB đủ rộng (không sideway quá)
    bb_width_pct = (cur_upper - cur_lower) / bb_mid.iloc[-1] * 100
    if bb_width_pct < 0.5:  # BB quá hẹp → sideway → bỏ qua
        return "HOLD"

    # LONG: giá breakout lên trên BB upper
    if cur_price > cur_upper and prev_price <= bb_upper.iloc[-2]:
        if cur_rsi > 50 and vol_surge:
            return "LONG"

    # SHORT: giá breakout xuống dưới BB lower
    if cur_price < cur_lower and prev_price >= bb_lower.iloc[-2]:
        if cur_rsi < 50 and vol_surge:
            return "SHORT"

    # Fallback: RSI extreme + giá ở biên BB (không cần breakout)
    if cur_rsi <= 32 and cur_price <= cur_lower * 1.005:
        return "LONG"
    if cur_rsi >= 68 and cur_price >= cur_upper * 0.995:
        return "SHORT"

    return "HOLD"


# ── Strategy 2: HIGH_VOL (Alts) — Momentum + EMA cross ───────
def get_signal_high_vol(df: pd.DataFrame, config) -> str:
    """
    Altcoins biến động mạnh → dùng momentum:
    - LONG : EMA9 > EMA21 + RSI thoát oversold (< 40 → tăng) + MACD dương
    - SHORT: EMA9 < EMA21 + RSI thoát overbought (> 60 → giảm) + MACD âm
    Ngưỡng RSI nới rộng hơn (40/60 thay vì 35/65).
    """
    close  = df["close"]
    volume = df["volume"]
    if len(close) < 30:
        return "HOLD"

    rsi      = calculate_rsi(close, 14)
    ema9     = calculate_ema(close, 9)
    ema21    = calculate_ema(close, 21)
    ema50    = calculate_ema(close, 50)
    ml, sl, hist = calculate_macd(close, 12, 26, 9)
    vol_ma   = calculate_volume_ma(volume, 20)

    cur_rsi  = rsi.iloc[-1]
    prev_rsi = rsi.iloc[-2]
    cur_e9   = ema9.iloc[-1]
    cur_e21  = ema21.iloc[-1]
    cur_e50  = ema50.iloc[-1]
    cur_price= close.iloc[-1]
    cur_hist = hist.iloc[-1]
    prev_hist= hist.iloc[-2]
    cur_vol  = volume.iloc[-1]
    avg_vol  = vol_ma.iloc[-1]
    vol_ok   = cur_vol > avg_vol * 0.7  # nới lỏng volume filter

    # ── LONG conditions ──
    long_score = 0

    # RSI thoát oversold (< 40 rồi tăng lên)
    if prev_rsi < 40 and cur_rsi >= 40:
        long_score += 3  # strong signal
    elif cur_rsi < 45 and cur_rsi > prev_rsi:
        long_score += 2  # RSI đang tăng từ vùng thấp
    elif cur_rsi < 50 and cur_rsi > prev_rsi:
        long_score += 1

    # EMA9 trên EMA21 (uptrend)
    if cur_e9 > cur_e21:
        long_score += 2
    # EMA9 vừa cắt lên EMA21 (golden cross)
    if ema9.iloc[-2] <= ema21.iloc[-2] and cur_e9 > cur_e21:
        long_score += 2  # bonus cho crossover

    # Giá trên EMA50 (trend filter)
    if cur_price > cur_e50:
        long_score += 1

    # MACD histogram dương và tăng
    if cur_hist > 0 and cur_hist > prev_hist:
        long_score += 2
    elif cur_hist > prev_hist:
        long_score += 1

    if long_score >= 3 and vol_ok:
        return "LONG"

    # ── SHORT conditions ──
    short_score = 0

    # RSI thoát overbought (> 60 rồi giảm xuống)
    if prev_rsi > 60 and cur_rsi <= 60:
        short_score += 3
    elif cur_rsi > 55 and cur_rsi < prev_rsi:
        short_score += 2
    elif cur_rsi > 50 and cur_rsi < prev_rsi:
        short_score += 1

    # EMA9 dưới EMA21 (downtrend)
    if cur_e9 < cur_e21:
        short_score += 2
    # EMA9 vừa cắt xuống EMA21 (death cross)
    if ema9.iloc[-2] >= ema21.iloc[-2] and cur_e9 < cur_e21:
        short_score += 2

    # Giá dưới EMA50
    if cur_price < cur_e50:
        short_score += 1

    # MACD histogram âm và giảm
    if cur_hist < 0 and cur_hist < prev_hist:
        short_score += 2
    elif cur_hist < prev_hist:
        short_score += 1

    if short_score >= 3 and vol_ok:
        return "SHORT"

    return "HOLD"


# ── Main entry point ──────────────────────────────────────────
def get_signal(df: pd.DataFrame, config, df_htf: pd.DataFrame = None,
               symbol: str = "") -> str:
    """
    Chọn strategy phù hợp theo loại coin:
    - LOW_VOL (BTC/ETH): Bollinger Band breakout
    - HIGH_VOL (Alts)  : Momentum + EMA cross
    """
    coin_type = get_coin_type(symbol)
    htf_trend = get_htf_trend(df_htf) if df_htf is not None else "NEUTRAL"

    if coin_type == "LOW_VOL":
        sig = get_signal_low_vol(df, config)
    else:
        sig = get_signal_high_vol(df, config)

    # Filter: không LONG khi HTF downtrend mạnh, không SHORT khi HTF uptrend mạnh
    if sig == "LONG"  and htf_trend == "DOWN": return "HOLD"
    if sig == "SHORT" and htf_trend == "UP":   return "HOLD"

    return sig


# ── Multi-Timeframe Trend ─────────────────────────────────────
def get_mtf_trend(df_4h: pd.DataFrame, df_1h: pd.DataFrame,
                  df_15m: pd.DataFrame, df_1m: pd.DataFrame = None) -> dict:
    """
    Phân tích xu hướng đa khung thời gian.
    Trả về: {"bias": "LONG"/"SHORT"/"NEUTRAL", "strength": "STRONG"/"MEDIUM"/"WEAK", "detail": str}
    """
    scores = {"LONG": 0, "SHORT": 0}

    for label, df in [("4h", df_4h), ("1h", df_1h), ("15m", df_15m)]:
        if df is None or len(df) < 50:
            continue
        close = df["close"]
        ema20 = calculate_ema(close, 20)
        ema50 = calculate_ema(close, 50)
        rsi = calculate_rsi(close, 14)
        p = close.iloc[-1]
        e20 = ema20.iloc[-1]
        e50 = ema50.iloc[-1]
        r = rsi.iloc[-1]

        weight = 2 if label == "4h" else (1.5 if label == "1h" else 1)
        if p > e20 > e50 and r > 50:
            scores["LONG"] += weight
        elif p < e20 < e50 and r < 50:
            scores["SHORT"] += weight

    total = scores["LONG"] + scores["SHORT"]
    if total == 0:
        return {"bias": "NEUTRAL", "strength": "WEAK", "detail": "No clear trend"}

    if scores["LONG"] > scores["SHORT"]:
        bias = "LONG"
        ratio = scores["LONG"] / max(total, 1)
    else:
        bias = "SHORT"
        ratio = scores["SHORT"] / max(total, 1)

    if ratio >= 0.8:
        strength = "STRONG"
    elif ratio >= 0.6:
        strength = "MEDIUM"
    else:
        strength = "WEAK"
        bias = "NEUTRAL"

    detail = f"L={scores['LONG']:.1f} S={scores['SHORT']:.1f}"
    return {"bias": bias, "strength": strength, "detail": detail}


def is_volatile_coin(df_1h: pd.DataFrame, threshold_pct: float = 4.0) -> bool:
    """
    Kiểm tra coin có biến động mạnh không (daily range > threshold%).
    Dùng ATR 14 trên 1h so với giá hiện tại.
    """
    if df_1h is None or len(df_1h) < 20:
        return False
    close = df_1h["close"]
    high = df_1h["high"]
    low = df_1h["low"]
    atr = calculate_atr(high, low, close, 14).iloc[-1]
    price = close.iloc[-1]
    atr_pct = (atr / price) * 100 * 4  # ước lượng daily range từ 1h ATR
    return atr_pct >= threshold_pct


def get_pullback_signal(df: pd.DataFrame, config, bias: str) -> str:
    """
    Tìm điểm pullback entry theo hướng bias (LONG/SHORT).
    - LONG bias: chờ giá pullback về EMA21 rồi bật lên
    - SHORT bias: chờ giá pullback lên EMA21 rồi quay xuống
    """
    if df is None or len(df) < 30:
        return "HOLD"

    close = df["close"]
    ema21 = calculate_ema(close, 21)
    rsi = calculate_rsi(close, 14)

    cur_price = close.iloc[-1]
    prev_price = close.iloc[-2]
    cur_ema = ema21.iloc[-1]
    cur_rsi = rsi.iloc[-1]

    if bias == "LONG":
        # Giá vừa chạm/xuyên EMA21 rồi bật lên
        near_ema = abs(cur_price - cur_ema) / cur_ema < 0.005  # trong 0.5%
        bouncing = cur_price > prev_price and prev_price <= ema21.iloc[-2] * 1.003
        if (near_ema or bouncing) and cur_rsi > 40 and cur_rsi < 60:
            return "LONG"

    elif bias == "SHORT":
        # Giá vừa chạm/xuyên EMA21 rồi quay xuống
        near_ema = abs(cur_price - cur_ema) / cur_ema < 0.005
        dropping = cur_price < prev_price and prev_price >= ema21.iloc[-2] * 0.997
        if (near_ema or dropping) and cur_rsi > 40 and cur_rsi < 60:
            return "SHORT"

    return "HOLD"


# ── Smart Entry: 1m + 15m Timing ─────────────────────────────
def get_smart_entry_signal(df_15m: pd.DataFrame, df_1m: pd.DataFrame,
                            bias: str) -> dict:
    """
    Smart entry: dùng 15m xác nhận hướng, 1m timing vào lệnh chính xác.

    Logic:
    ┌─────────────────────────────────────────────────────────┐
    │  15m → xác nhận trend + momentum đang ủng hộ bias       │
    │  1m  → chờ micro-pullback / pinbar / momentum bùng nổ  │
    └─────────────────────────────────────────────────────────┘

    Returns:
        {
            "signal": "LONG" | "SHORT" | "WAIT",
            "quality": "A" | "B" | "C",   # A=tốt nhất, C=yếu nhất
            "reason": str,
            "score": float (0-100)
        }
    """
    result = {"signal": "WAIT", "quality": "C", "reason": "", "score": 0.0}

    if df_15m is None or len(df_15m) < 30:
        result["reason"] = "15m: không đủ dữ liệu"
        return result
    if df_1m is None or len(df_1m) < 20:
        result["reason"] = "1m: không đủ dữ liệu"
        return result
    if bias not in ("LONG", "SHORT"):
        result["reason"] = "bias NEUTRAL → bỏ qua"
        return result

    reasons = []
    score = 0.0

    # ── PHÂN TÍCH 15m ───────────────────────────────────────
    close_15 = df_15m["close"]
    high_15  = df_15m["high"]
    low_15   = df_15m["low"]
    vol_15   = df_15m["volume"]

    rsi_15   = calculate_rsi(close_15, 14)
    ema9_15  = calculate_ema(close_15, 9)
    ema21_15 = calculate_ema(close_15, 21)
    ema50_15 = calculate_ema(close_15, 50)
    atr_15   = calculate_atr(high_15, low_15, close_15, 14)
    ml_15, sl_15, hist_15 = calculate_macd(close_15, 12, 26, 9)

    p15      = close_15.iloc[-1]
    rsi15    = rsi_15.iloc[-1]
    e9_15    = ema9_15.iloc[-1]
    e21_15   = ema21_15.iloc[-1]
    e50_15   = ema50_15.iloc[-1]
    hist_15c = hist_15.iloc[-1]
    hist_15p = hist_15.iloc[-2]
    atr15    = atr_15.iloc[-1]

    vol_ma_15 = calculate_volume_ma(vol_15, 20)
    vol_ratio_15 = vol_15.iloc[-1] / vol_ma_15.iloc[-1] if vol_ma_15.iloc[-1] > 0 else 1.0

    # 15m điều kiện theo bias
    if bias == "LONG":
        # EMA stack: 9 > 21 > 50 (uptrend xác nhận)
        if e9_15 > e21_15 > e50_15:
            score += 20
            reasons.append("15m EMA stack↑")
        elif e9_15 > e21_15:
            score += 10
            reasons.append("15m EMA9>21")

        # RSI zone: không quá mua (30-60 là ideal)
        if 35 <= rsi15 <= 55:
            score += 15
            reasons.append(f"15m RSI={rsi15:.0f}✓")
        elif rsi15 < 35:
            score += 8  # oversold — có thể vào nhưng chờ phục hồi
            reasons.append(f"15m RSI={rsi15:.0f}(oversold)")
        elif rsi15 > 65:
            score -= 10  # overbought → tránh vào LONG
            reasons.append(f"15m RSI={rsi15:.0f}(overbought!)")

        # MACD histogram đang tăng
        if hist_15c > 0 and hist_15c > hist_15p:
            score += 10
            reasons.append("15m MACD↑")
        elif hist_15c > hist_15p:
            score += 5
            reasons.append("15m MACD turning↑")

        # Giá trên EMA50 (major trend)
        if p15 > e50_15:
            score += 10
            reasons.append("15m above EMA50")

    else:  # SHORT
        if e9_15 < e21_15 < e50_15:
            score += 20
            reasons.append("15m EMA stack↓")
        elif e9_15 < e21_15:
            score += 10
            reasons.append("15m EMA9<21")

        if 45 <= rsi15 <= 65:
            score += 15
            reasons.append(f"15m RSI={rsi15:.0f}✓")
        elif rsi15 > 65:
            score += 8
            reasons.append(f"15m RSI={rsi15:.0f}(overbought)")
        elif rsi15 < 35:
            score -= 10
            reasons.append(f"15m RSI={rsi15:.0f}(oversold!)")

        if hist_15c < 0 and hist_15c < hist_15p:
            score += 10
            reasons.append("15m MACD↓")
        elif hist_15c < hist_15p:
            score += 5
            reasons.append("15m MACD turning↓")

        if p15 < e50_15:
            score += 10
            reasons.append("15m below EMA50")

    # Volume trên 15m
    if vol_ratio_15 >= 1.5:
        score += 10
        reasons.append(f"15m Vol×{vol_ratio_15:.1f}")
    elif vol_ratio_15 >= 1.2:
        score += 5

    # ── PHÂN TÍCH 1m — Timing chính xác ─────────────────────
    close_1  = df_1m["close"]
    high_1   = df_1m["high"]
    low_1    = df_1m["low"]
    open_1   = df_1m["open"]
    vol_1    = df_1m["volume"]

    rsi_1    = calculate_rsi(close_1, 14)
    ema9_1   = calculate_ema(close_1, 9)
    ema21_1  = calculate_ema(close_1, 21)
    atr_1    = calculate_atr(high_1, low_1, close_1, 14)

    p1       = close_1.iloc[-1]
    p1_prev  = close_1.iloc[-2]
    o1       = open_1.iloc[-1]
    h1       = high_1.iloc[-1]
    l1       = low_1.iloc[-1]
    rsi1     = rsi_1.iloc[-1]
    rsi1_p   = rsi_1.iloc[-2]
    e9_1     = ema9_1.iloc[-1]
    e21_1    = ema21_1.iloc[-1]
    atr1     = atr_1.iloc[-1]

    vol_ma_1 = calculate_volume_ma(vol_1, 20)
    vol_ratio_1 = vol_1.iloc[-1] / vol_ma_1.iloc[-1] if vol_ma_1.iloc[-1] > 0 else 1.0

    # Phát hiện candle patterns trên 1m
    body = abs(p1 - o1)
    upper_wick = h1 - max(p1, o1)
    lower_wick = min(p1, o1) - l1
    total_range = h1 - l1 if h1 > l1 else 0.0001

    # Pinbar (rejection candle)
    is_bullish_pin = (lower_wick > body * 2.0 and lower_wick > upper_wick * 1.5)
    is_bearish_pin = (upper_wick > body * 2.0 and upper_wick > lower_wick * 1.5)

    # Engulfing candle (nến nuốt)
    prev_body = abs(close_1.iloc[-2] - open_1.iloc[-2])
    is_bull_engulf = (p1 > o1 and p1 > open_1.iloc[-2] and o1 < close_1.iloc[-2]
                      and body > prev_body * 1.2)
    is_bear_engulf = (p1 < o1 and p1 < open_1.iloc[-2] and o1 > close_1.iloc[-2]
                      and body > prev_body * 1.2)

    # Momentum burst: nến xanh/đỏ mạnh + volume cao
    is_bull_burst = (p1 > o1 and body > total_range * 0.6 and vol_ratio_1 >= 1.5)
    is_bear_burst = (p1 < o1 and body > total_range * 0.6 and vol_ratio_1 >= 1.5)

    # RSI 1m đang đúng hướng
    rsi1_trending_up   = rsi1 > rsi1_p and rsi1 > 45
    rsi1_trending_down = rsi1 < rsi1_p and rsi1 < 55

    # EMA 1m alignment
    ema1_bull = e9_1 > e21_1
    ema1_bear = e9_1 < e21_1

    # Micro-pullback: giá vừa pullback về EMA9 trên 1m rồi bật
    ema9_1_prev = ema9_1.iloc[-2]
    micro_pullback_long  = (p1_prev <= ema9_1_prev * 1.002 and p1 > ema9_1.iloc[-1])
    micro_pullback_short = (p1_prev >= ema9_1_prev * 0.998 and p1 < ema9_1.iloc[-1])

    # ── Tổng hợp điểm 1m theo bias ─────────────────────────
    if bias == "LONG":
        if is_bullish_pin:
            score += 20
            reasons.append("1m Bullish Pin🕯️")
        elif is_bull_engulf:
            score += 18
            reasons.append("1m Bull Engulf🕯️")
        elif is_bull_burst:
            score += 15
            reasons.append("1m Bull Burst🚀")
        elif micro_pullback_long:
            score += 12
            reasons.append("1m Micro-pullback↗")

        if rsi1_trending_up:
            score += 8
            reasons.append(f"1m RSI↑{rsi1:.0f}")
        if ema1_bull:
            score += 5
            reasons.append("1m EMA↑")

    else:  # SHORT
        if is_bearish_pin:
            score += 20
            reasons.append("1m Bearish Pin🕯️")
        elif is_bear_engulf:
            score += 18
            reasons.append("1m Bear Engulf🕯️")
        elif is_bear_burst:
            score += 15
            reasons.append("1m Bear Burst📉")
        elif micro_pullback_short:
            score += 12
            reasons.append("1m Micro-pullback↘")

        if rsi1_trending_down:
            score += 8
            reasons.append(f"1m RSI↓{rsi1:.0f}")
        if ema1_bear:
            score += 5
            reasons.append("1m EMA↓")

    score = max(0.0, min(score, 100.0))

    # ── Quyết định vào lệnh ────────────────────────────────
    # Cần đủ điểm 15m (xác nhận hướng) VÀ 1m (timing entry)
    has_15m_confirmation = score >= 30   # 15m ổn
    has_1m_trigger = any([
        is_bullish_pin and bias == "LONG",
        is_bearish_pin and bias == "SHORT",
        is_bull_engulf and bias == "LONG",
        is_bear_engulf and bias == "SHORT",
        is_bull_burst and bias == "LONG",
        is_bear_burst and bias == "SHORT",
        micro_pullback_long and bias == "LONG",
        micro_pullback_short and bias == "SHORT",
    ])

    if has_15m_confirmation and has_1m_trigger and score >= 55:
        quality = "A" if score >= 75 else "B"
        result.update({
            "signal": bias,
            "quality": quality,
            "reason": " | ".join(reasons),
            "score": round(score, 1)
        })
    elif has_15m_confirmation and score >= 45:
        # 15m ổn nhưng 1m chưa có trigger rõ ràng → chờ thêm
        result.update({
            "signal": "WAIT",
            "quality": "C",
            "reason": "15m ready, chờ 1m trigger | " + " | ".join(reasons),
            "score": round(score, 1)
        })
    else:
        result.update({
            "signal": "WAIT",
            "reason": "Chưa đủ điều kiện | " + " | ".join(reasons),
            "score": round(score, 1)
        })

    return result


# ── Signal Score & Win Rate Estimator ────────────────────────
def compute_signal_score(df_15m: pd.DataFrame, df_1h: pd.DataFrame,
                         df_4h: pd.DataFrame) -> dict:
    """
    Tính điểm LONG/SHORT và ước tính win rate dựa trên phân tích kỹ thuật.
    
    Logic tương tự telegram_commands._analyze_and_send().
    
    Returns:
        {
            "signal": "LONG" | "SHORT" | "WAIT",
            "long_score": int,
            "short_score": int,
            "win_rate": float (0-100),
            "long_reasons": [str],
            "short_reasons": [str],
        }
    """
    close  = df_15m["close"]
    high   = df_15m["high"]
    low    = df_15m["low"]
    volume = df_15m["volume"]
    
    price = close.iloc[-1]
    
    # Indicators 15m
    rsi      = calculate_rsi(close, 14)
    ema9     = calculate_ema(close, 9)
    ema21    = calculate_ema(close, 21)
    ema50    = calculate_ema(close, 50)
    ml, sl_line, hist = calculate_macd(close)
    bb_up, bb_mid, bb_lo = calculate_bollinger(close, 20, 2.0)
    vol_ma   = calculate_volume_ma(volume, 20)
    
    cur_rsi   = rsi.iloc[-1]
    prev_rsi  = rsi.iloc[-2]
    cur_ema9  = ema9.iloc[-1]
    cur_ema21 = ema21.iloc[-1]
    cur_ema50 = ema50.iloc[-1]
    cur_hist  = hist.iloc[-1]
    prev_hist = hist.iloc[-2]
    cur_vol   = volume.iloc[-1]
    avg_vol   = vol_ma.iloc[-1]
    vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0
    
    # HTF trends
    htf_1h = get_htf_trend(df_1h)
    htf_4h = get_htf_trend(df_4h)
    
    # ── Chấm điểm LONG / SHORT ──────────────────────────────
    long_reasons  = []
    short_reasons = []
    
    # RSI
    if cur_rsi < 35:
        long_reasons.append(f"RSI={cur_rsi:.0f} oversold")
    elif cur_rsi < 45 and cur_rsi > prev_rsi:
        long_reasons.append(f"RSI={cur_rsi:.0f}↑")
    
    if cur_rsi > 65:
        short_reasons.append(f"RSI={cur_rsi:.0f} overbought")
    elif cur_rsi > 55 and cur_rsi < prev_rsi:
        short_reasons.append(f"RSI={cur_rsi:.0f}↓")
    
    # EMA
    if cur_ema9 > cur_ema21:
        long_reasons.append("EMA9>21")
    else:
        short_reasons.append("EMA9<21")
    
    if price > cur_ema50:
        long_reasons.append("Price>EMA50")
    else:
        short_reasons.append("Price<EMA50")
    
    # MACD
    if cur_hist > 0 and cur_hist > prev_hist:
        long_reasons.append("MACD↑")
    elif cur_hist < 0 and cur_hist < prev_hist:
        short_reasons.append("MACD↓")
    
    # Bollinger Band
    if price <= bb_lo.iloc[-1] * 1.005:
        long_reasons.append("BB lower")
    if price >= bb_up.iloc[-1] * 0.995:
        short_reasons.append("BB upper")
    
    # Volume
    if vol_ratio >= 1.5:
        if cur_rsi < 50:
            long_reasons.append(f"Vol×{vol_ratio:.1f}")
        else:
            short_reasons.append(f"Vol×{vol_ratio:.1f}")
    
    # HTF trend
    if htf_1h == "UP":
        long_reasons.append("1h UP")
    elif htf_1h == "DOWN":
        short_reasons.append("1h DOWN")
    
    if htf_4h == "UP":
        long_reasons.append("4h UP")
    elif htf_4h == "DOWN":
        short_reasons.append("4h DOWN")
    
    long_score  = len(long_reasons)
    short_score = len(short_reasons)
    
    # ── Quyết định tín hiệu ────────────────────────────────
    # Cần ít nhất 3 điểm và hơn bên kia 1 điểm (nới từ 2 xuống 1)
    if long_score >= 3 and long_score >= short_score + 1:
        signal = "LONG"
        win_rate = min(50 + long_score * 5, 85)
    elif short_score >= 3 and short_score >= long_score + 1:
        signal = "SHORT"
        win_rate = min(50 + short_score * 5, 85)
    else:
        signal = "WAIT"
        dominant = max(long_score, short_score)
        win_rate = min(40 + dominant * 3, 60)
    
    return {
        "signal": signal,
        "long_score": long_score,
        "short_score": short_score,
        "win_rate": round(win_rate, 1),
        "long_reasons": long_reasons,
        "short_reasons": short_reasons,
    }
