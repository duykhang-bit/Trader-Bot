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
