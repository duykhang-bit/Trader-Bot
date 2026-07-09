# ============================================================
# QUANT VOLUME PROFILE — VWAP, POC, Value Area
# ============================================================
import logging
import numpy as np
import pandas as pd
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def calc_vwap(df: pd.DataFrame) -> pd.Series:
    """
    VWAP = Σ(typical_price × volume) / Σ(volume)
    typical_price = (high + low + close) / 3
    """
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    cum_tp_vol = (tp * df["volume"]).cumsum()
    cum_vol    = df["volume"].cumsum()
    vwap = cum_tp_vol / cum_vol.replace(0, 1e-10)
    return vwap.rename("vwap")


def calc_rolling_vwap(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """
    Rolling VWAP — tính lại VWAP mỗi `window` nến.
    Hữu ích hơn cumulative VWAP cho intraday trading.
    """
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    sum_tp_vol = (tp * df["volume"]).rolling(window).sum()
    sum_vol    = df["volume"].rolling(window).sum().replace(0, 1e-10)
    return (sum_tp_vol / sum_vol).rename("vwap_rolling")


def calc_volume_profile(df: pd.DataFrame,
                         n_bins: int = 20) -> Dict:
    """
    Volume Profile: phân phối volume theo price level.

    Returns:
        {
            "poc":        float,  # Point of Control — price với volume cao nhất
            "vah":        float,  # Value Area High (70% volume trên POC)
            "val":        float,  # Value Area Low  (70% volume dưới POC)
            "profile":    list,   # [(price_mid, volume), ...] sorted by price
        }
    """
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    vol   = df["volume"].astype(float)

    price_min = low.min()
    price_max = high.max()

    if price_max <= price_min:
        return {"poc": df["close"].iloc[-1], "vah": price_max, "val": price_min, "profile": []}

    # Tạo bins
    bins       = np.linspace(price_min, price_max, n_bins + 1)
    bin_vol    = np.zeros(n_bins)
    bin_mids   = [(bins[i] + bins[i+1]) / 2 for i in range(n_bins)]

    for idx in range(len(df)):
        candle_low  = low.iloc[idx]
        candle_high = high.iloc[idx]
        candle_vol  = vol.iloc[idx]

        # Phân bổ volume vào các bin mà candle overlap
        for b in range(n_bins):
            bin_low  = bins[b]
            bin_high = bins[b+1]
            overlap  = max(0.0, min(candle_high, bin_high) - max(candle_low, bin_low))
            candle_range = max(candle_high - candle_low, 1e-10)
            bin_vol[b] += candle_vol * (overlap / candle_range)

    # POC = bin với volume cao nhất
    poc_idx = int(np.argmax(bin_vol))
    poc     = bin_mids[poc_idx]

    # Value Area = 70% tổng volume quanh POC
    total_vol   = bin_vol.sum()
    target_vol  = total_vol * 0.70

    # Expand từ POC ra ngoài đến khi đạt 70%
    va_indices = [poc_idx]
    accumulated = bin_vol[poc_idx]
    lo_ptr = poc_idx - 1
    hi_ptr = poc_idx + 1

    while accumulated < target_vol:
        lo_vol = bin_vol[lo_ptr] if lo_ptr >= 0 else -1
        hi_vol = bin_vol[hi_ptr] if hi_ptr < n_bins else -1

        if lo_vol < 0 and hi_vol < 0:
            break
        if hi_vol >= lo_vol:
            va_indices.append(hi_ptr)
            accumulated += hi_vol
            hi_ptr += 1
        else:
            va_indices.append(lo_ptr)
            accumulated += lo_vol
            lo_ptr -= 1

    vah = max(bin_mids[i] for i in va_indices)
    val = min(bin_mids[i] for i in va_indices)

    profile = sorted(zip(bin_mids, bin_vol.tolist()), key=lambda x: x[0])

    return {
        "poc":     round(poc, 6),
        "vah":     round(vah, 6),
        "val":     round(val, 6),
        "profile": [(round(p, 6), round(v, 2)) for p, v in profile],
    }


def get_vp_signal(df: pd.DataFrame,
                  bias: str = None,
                  window: int = 50) -> Dict:
    """
    Phân tích Volume Profile + VWAP → tín hiệu giao dịch.

    Logic:
    - Giá trên VWAP + trên VAH → LONG (breakout khỏi value area)
    - Giá dưới VWAP + dưới VAL → SHORT
    - Giá trong Value Area (VAL-VAH) → ít ưu tiên
    - POC gần giá → vùng hỗ trợ/kháng cự mạnh

    Returns:
        {
            "signal":    "LONG"/"SHORT"/"NEUTRAL",
            "vwap":      float,
            "poc":       float,
            "vah":       float,
            "val":       float,
            "price_vs":  "ABOVE_VAH"/"IN_VA"/"BELOW_VAL",
            "poc_dist":  float,   # % cách POC
            "score":     float,   # 0-100
            "reason":    str,
        }
    """
    if df is None or len(df) < 20:
        return {"signal": "NEUTRAL", "vwap": 0, "poc": 0,
                "vah": 0, "val": 0, "price_vs": "UNKNOWN",
                "poc_dist": 0, "score": 50, "reason": "Insufficient data"}

    df_w   = df.tail(window).copy().reset_index(drop=True)
    vwap_s = calc_rolling_vwap(df_w, min(20, len(df_w)))
    vp     = calc_volume_profile(df_w, n_bins=20)

    price  = float(df_w["close"].iloc[-1])
    vwap   = float(vwap_s.iloc[-1])
    poc    = vp["poc"]
    vah    = vp["vah"]
    val    = vp["val"]

    poc_dist = abs(price - poc) / poc * 100 if poc > 0 else 0

    # Xác định giá ở đâu so với Value Area
    if price > vah:
        price_vs = "ABOVE_VAH"
    elif price < val:
        price_vs = "BELOW_VAL"
    else:
        price_vs = "IN_VA"

    score = 50.0
    reasons = []

    # VWAP signal
    if price > vwap * 1.001:
        score += 10
        reasons.append(f"Price>VWAP({vwap:.2f})")
    elif price < vwap * 0.999:
        score -= 10
        reasons.append(f"Price<VWAP({vwap:.2f})")

    # Value Area signal
    if price_vs == "ABOVE_VAH":
        score += 15
        reasons.append(f"Above VAH({vah:.2f})")
    elif price_vs == "BELOW_VAL":
        score -= 15
        reasons.append(f"Below VAL({val:.2f})")
    else:
        # Trong VA — giảm confidence
        if price > poc:
            score += 3
        else:
            score -= 3
        reasons.append(f"InVA POC={poc:.2f}")

    # POC proximity — gần POC = strong S/R
    if poc_dist < 0.3:
        reasons.append(f"NearPOC({poc_dist:.1f}%)")
        # POC act as S/R — không tăng score mà chỉ thêm context

    score = max(0.0, min(100.0, score))

    if score >= 62:
        signal = "LONG"
    elif score <= 38:
        signal = "SHORT"
    else:
        signal = "NEUTRAL"

    if bias and signal != "NEUTRAL" and signal != bias:
        signal = "NEUTRAL"
        reasons.append(f"VP ngược bias({bias})")

    return {
        "signal":   signal,
        "vwap":     round(vwap, 6),
        "poc":      poc,
        "vah":      vah,
        "val":      val,
        "price_vs": price_vs,
        "poc_dist": round(poc_dist, 2),
        "score":    round(score, 1),
        "reason":   " | ".join(reasons),
    }


def vp_confirms(vp_result: Dict, bias: str) -> Tuple[bool, str]:
    """
    Kiểm tra volume profile có xác nhận direction không.
    Returns: (confirms, reason)
    """
    price_vs = vp_result.get("price_vs", "UNKNOWN")

    if bias == "LONG":
        if price_vs == "BELOW_VAL":
            return False, f"VP: price below VAL({vp_result.get('val',0):.2f}) — bearish"
        if vp_result["score"] < 40:
            return False, f"VP score={vp_result['score']} quá thấp cho LONG"
    elif bias == "SHORT":
        if price_vs == "ABOVE_VAH":
            # SHORT khi giá trên VAH — OK (breakout có thể fail)
            pass
        if vp_result["score"] > 60:
            return False, f"VP score={vp_result['score']} quá cao cho SHORT"

    return True, vp_result["reason"]
