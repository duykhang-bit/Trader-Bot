# ============================================================
# QUANT ORDER FLOW — Delta, CVD, Buy/Sell Pressure
# ============================================================
import logging
import numpy as np
import pandas as pd
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def calc_delta(df: pd.DataFrame) -> pd.Series:
    """
    Tính Delta = Buy Volume - Sell Volume mỗi candle.

    Ước tính từ OHLCV:
    - Nến xanh (close > open): buy_vol ≈ taker_buy_base, sell_vol = vol - buy
    - Dùng cột taker_buy_base_asset_volume nếu có, fallback dùng close/open ratio

    Returns: Series delta (dương = net buying, âm = net selling)
    """
    if "taker_buy_base" in df.columns:
        buy_vol  = df["taker_buy_base"].astype(float)
        sell_vol = df["volume"].astype(float) - buy_vol
    else:
        # Fallback: ước tính theo body ratio
        body_ratio = (df["close"] - df["open"]) / (df["high"] - df["low"] + 1e-10)
        body_ratio = body_ratio.clip(-1, 1)
        buy_vol  = df["volume"] * ((body_ratio + 1) / 2)
        sell_vol = df["volume"] * ((1 - body_ratio) / 2)

    return (buy_vol - sell_vol).rename("delta")


def calc_cvd(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """
    Cumulative Volume Delta (CVD) — tổng tích lũy delta.
    CVD tăng = áp lực mua tích lũy
    CVD giảm = áp lực bán tích lũy
    """
    delta = calc_delta(df)
    cvd   = delta.rolling(window=window).sum()
    return cvd.rename("cvd")


def calc_buy_sell_ratio(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """
    Buy/Sell ratio = buy_vol / total_vol (rolling window).
    > 0.5 = buying pressure dominant
    < 0.5 = selling pressure dominant
    """
    if "taker_buy_base" in df.columns:
        buy_vol = df["taker_buy_base"].astype(float)
    else:
        body_ratio = (df["close"] - df["open"]) / (df["high"] - df["low"] + 1e-10)
        body_ratio = body_ratio.clip(-1, 1)
        buy_vol = df["volume"] * ((body_ratio + 1) / 2)

    total_vol = df["volume"].astype(float).replace(0, 1e-10)
    ratio = buy_vol / total_vol
    return ratio.rolling(window).mean().rename("buy_sell_ratio")


def get_orderflow_signal(df: pd.DataFrame,
                          bias: str = None,
                          window: int = 14) -> Dict:
    """
    Phân tích order flow và trả về tín hiệu.

    Returns:
        {
            "signal":      "LONG"/"SHORT"/"NEUTRAL",
            "delta_last":  float,   # delta nến cuối
            "cvd":         float,   # CVD hiện tại
            "buy_ratio":   float,   # % mua trong window
            "pressure":    "BUY"/"SELL"/"NEUTRAL",
            "score":       float,   # 0-100, đo độ mạnh
            "reason":      str,
        }
    """
    if df is None or len(df) < window + 5:
        return {"signal": "NEUTRAL", "delta_last": 0, "cvd": 0,
                "buy_ratio": 0.5, "pressure": "NEUTRAL", "score": 50, "reason": "Insufficient data"}

    delta     = calc_delta(df)
    cvd_series= calc_cvd(df, window)
    ratio_ser = calc_buy_sell_ratio(df, window)

    delta_last  = float(delta.iloc[-1])
    delta_prev  = float(delta.iloc[-2])
    cvd_last    = float(cvd_series.iloc[-1])
    cvd_prev    = float(cvd_series.iloc[-2])
    buy_ratio   = float(ratio_ser.iloc[-1])

    # CVD trend: đang tăng hay giảm
    cvd_rising  = cvd_last > cvd_prev
    cvd_falling = cvd_last < cvd_prev

    # Delta momentum: delta hiện tại lớn hơn delta trước
    delta_accelerating_buy  = delta_last > 0 and delta_last > delta_prev
    delta_accelerating_sell = delta_last < 0 and delta_last < delta_prev

    # Score tổng hợp (0-100)
    score = 50.0
    reasons = []

    # Buy pressure signals
    if buy_ratio > 0.60:
        score += 15
        reasons.append(f"BuyRatio={buy_ratio:.0%}")
    elif buy_ratio > 0.55:
        score += 8
        reasons.append(f"BuyRatio={buy_ratio:.0%}")
    elif buy_ratio < 0.40:
        score -= 15
        reasons.append(f"SellRatio={1-buy_ratio:.0%}")
    elif buy_ratio < 0.45:
        score -= 8

    if cvd_rising:
        score += 12
        reasons.append(f"CVD↑{cvd_last:+.0f}")
    elif cvd_falling:
        score -= 12
        reasons.append(f"CVD↓{cvd_last:+.0f}")

    if delta_accelerating_buy:
        score += 10
        reasons.append(f"Δ+{delta_last:.0f}")
    elif delta_accelerating_sell:
        score -= 10
        reasons.append(f"Δ{delta_last:.0f}")

    score = max(0.0, min(100.0, score))

    # Signal
    if score >= 65:
        signal   = "LONG"
        pressure = "BUY"
    elif score <= 35:
        signal   = "SHORT"
        pressure = "SELL"
    else:
        signal   = "NEUTRAL"
        pressure = "NEUTRAL"

    # Filter theo bias nếu có
    if bias and signal != "NEUTRAL" and signal != bias:
        signal = "NEUTRAL"
        reasons.append(f"OF ngược bias({bias})")

    return {
        "signal":     signal,
        "delta_last": round(delta_last, 2),
        "cvd":        round(cvd_last, 2),
        "buy_ratio":  round(buy_ratio, 3),
        "pressure":   pressure,
        "score":      round(score, 1),
        "reason":     " | ".join(reasons),
    }


def orderflow_confirms(of_result: Dict, bias: str) -> bool:
    """
    Kiểm tra order flow có xác nhận direction không.
    Dùng để filter trong scan_engine.
    """
    if of_result["pressure"] == "NEUTRAL":
        return True  # NEUTRAL không block
    if bias == "LONG"  and of_result["pressure"] == "SELL":
        return False
    if bias == "SHORT" and of_result["pressure"] == "BUY":
        return False
    return True
