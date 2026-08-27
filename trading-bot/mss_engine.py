# ============================================================
# MSS ENGINE — Market Structure Shift + Liquidity Sweep
# ============================================================
#
# TIER A — FULL CONFIDENCE:
#   15M sweep + reclaim + MSS + retest + 5M confirm → ENTRY
#
# TIER B — HIGH CONFIDENCE:
#   15M sweep + reclaim + 5M MSS + retest → ENTRY (score thấp hơn)
#
# TIER C — PENDING:
#   Sweep + reclaim nhưng chưa có MSS → chờ confirmation
#
# TIER D — NO TRADE:
#   Không có sweep/MSS rõ ràng
#
# Flow:
#   analyze_mss(df_15m, df_5m, signal, cfg) → MSSResult
#
# ============================================================

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────

@dataclass
class SwingPoint:
    idx:    int     # index trong df
    price:  float
    kind:   str     # "high" | "low"
    ts:     float   # timestamp (unix)


@dataclass
class MSSResult:
    """Kết quả phân tích MSS cho 1 coin."""
    tier:          str     # "A" | "B" | "C" | "D"
    signal:        str     # "LONG" | "SHORT" | "NONE"
    entry_price:   float   # giá entry đề xuất (tại retest level)
    sl_price:      float   # SL dưới/trên sweep point + buffer
    confidence:    float   # 0-100, dùng để bonus score
    sweep_price:   float   # giá sweep xảy ra
    mss_level:     float   # level MSS (lower high bị break / higher low bị break)
    retest_level:  float   # level retest
    reason:        str     # mô tả ngắn
    ts:            float = field(default_factory=time.time)  # timestamp detect


# ─────────────────────────────────────────────────────────────
# SWING POINT DETECTION
# ─────────────────────────────────────────────────────────────

def find_swings(df: pd.DataFrame, lookback: int = 20, min_pivot: int = 2) -> List[SwingPoint]:
    """
    Tìm swing high/low thực sự trong lookback nến gần nhất.
    Swing high: high[i] > high[i-n..i-1] AND high[i] > high[i+1..i+n]
    Swing low:  low[i]  < low[i-n..i-1]  AND low[i]  < low[i+1..i+n]

    min_pivot: số nến mỗi bên phải nhỏ hơn/lớn hơn (tránh micro pivot)
    """
    swings = []
    n = len(df)
    start = max(0, n - lookback - min_pivot)
    end   = n - min_pivot - 1   # để lại min_pivot nến cuối (chưa confirmed)

    highs = df["high"].values
    lows  = df["low"].values

    try:
        times = df["open_time"].values.astype(float) / 1000
    except Exception:
        times = np.arange(n, dtype=float)

    for i in range(start + min_pivot, end):
        # Swing high
        left_ok  = all(highs[i] >= highs[i - j] for j in range(1, min_pivot + 1))
        right_ok = all(highs[i] >= highs[i + j] for j in range(1, min_pivot + 1))
        if left_ok and right_ok:
            swings.append(SwingPoint(idx=i, price=float(highs[i]),
                                     kind="high", ts=float(times[i])))

        # Swing low
        left_ok  = all(lows[i] <= lows[i - j] for j in range(1, min_pivot + 1))
        right_ok = all(lows[i] <= lows[i + j] for j in range(1, min_pivot + 1))
        if left_ok and right_ok:
            swings.append(SwingPoint(idx=i, price=float(lows[i]),
                                     kind="low", ts=float(times[i])))

    # Sort theo index tăng dần
    swings.sort(key=lambda s: s.idx)
    return swings


def get_recent_swing_low(swings: List[SwingPoint]) -> Optional[SwingPoint]:
    """Lấy swing low gần nhất (index cao nhất)."""
    lows = [s for s in swings if s.kind == "low"]
    return lows[-1] if lows else None


def get_recent_swing_high(swings: List[SwingPoint]) -> Optional[SwingPoint]:
    """Lấy swing high gần nhất (index cao nhất)."""
    highs = [s for s in swings if s.kind == "high"]
    return highs[-1] if highs else None


# ─────────────────────────────────────────────────────────────
# SWEEP DETECTION
# ─────────────────────────────────────────────────────────────

def detect_sweep(df: pd.DataFrame, swing: SwingPoint, direction: str,
                 tolerance: float = 0.001) -> dict:
    """
    Kiểm tra xem giá có sweep qua swing point rồi reclaim không.

    LONG: sweep dưới swing_low → giá đóng lại trên swing_low
    SHORT: sweep trên swing_high → giá đóng lại dưới swing_high

    Returns:
        {"swept": bool, "reclaimed": bool, "sweep_candle_idx": int,
         "reclaim_candle_idx": int, "sweep_price": float}
    """
    result = {
        "swept": False, "reclaimed": False,
        "sweep_candle_idx": -1, "reclaim_candle_idx": -1,
        "sweep_price": swing.price,
    }

    lows   = df["low"].values
    highs  = df["high"].values
    closes = df["close"].values
    n = len(df)

    # Chỉ check từ sau swing point
    search_start = swing.idx + 1

    if direction == "LONG":
        level = swing.price
        tol   = level * (1 - tolerance)   # phải xuống dưới tol

        for i in range(search_start, n):
            if lows[i] < tol:
                result["swept"]            = True
                result["sweep_candle_idx"] = i
                result["sweep_price"]      = float(lows[i])

                # Reclaim: nến này hoặc nến sau đóng lại trên level
                for j in range(i, min(i + 5, n)):
                    if closes[j] > level:
                        result["reclaimed"]          = True
                        result["reclaim_candle_idx"] = j
                        return result
                return result  # swept nhưng chưa reclaim

    else:  # SHORT
        level = swing.price
        tol   = level * (1 + tolerance)   # phải lên trên tol

        for i in range(search_start, n):
            if highs[i] > tol:
                result["swept"]            = True
                result["sweep_candle_idx"] = i
                result["sweep_price"]      = float(highs[i])

                for j in range(i, min(i + 5, n)):
                    if closes[j] < level:
                        result["reclaimed"]          = True
                        result["reclaim_candle_idx"] = j
                        return result
                return result

    return result


# ─────────────────────────────────────────────────────────────
# MSS DETECTION
# ─────────────────────────────────────────────────────────────

def detect_mss_15m(df: pd.DataFrame, swings: List[SwingPoint],
                   reclaim_idx: int, direction: str) -> dict:
    """
    Sau khi sweep + reclaim, tìm MSS trên 15m.

    LONG MSS: break qua lower high trước đó (bullish structure shift)
    SHORT MSS: break qua higher low trước đó (bearish structure shift)

    Returns:
        {"mss_confirmed": bool, "mss_level": float, "mss_candle_idx": int}
    """
    result = {"mss_confirmed": False, "mss_level": 0.0, "mss_candle_idx": -1}
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    n      = len(df)

    if reclaim_idx < 0 or reclaim_idx >= n:
        return result

    if direction == "LONG":
        # Tìm lower high trước reclaim point (trong các swing highs)
        prev_highs = [s for s in swings
                      if s.kind == "high" and s.idx < reclaim_idx]
        if not prev_highs:
            return result
        # Lower high = swing high ngay trước reclaim, giá thấp hơn swing high trước đó
        # Lấy cái thấp nhất trong 3 swing high gần nhất
        prev_highs_sorted = sorted(prev_highs, key=lambda s: s.idx)[-3:]
        lower_high = min(prev_highs_sorted, key=lambda s: s.price)
        mss_level  = lower_high.price

        # Break: đóng nến trên lower high sau reclaim
        for i in range(reclaim_idx + 1, n):
            if closes[i] > mss_level:
                result["mss_confirmed"]  = True
                result["mss_level"]      = mss_level
                result["mss_candle_idx"] = i
                return result

    else:  # SHORT
        prev_lows = [s for s in swings
                     if s.kind == "low" and s.idx < reclaim_idx]
        if not prev_lows:
            return result
        prev_lows_sorted = sorted(prev_lows, key=lambda s: s.idx)[-3:]
        higher_low = max(prev_lows_sorted, key=lambda s: s.price)
        mss_level  = higher_low.price

        for i in range(reclaim_idx + 1, n):
            if closes[i] < mss_level:
                result["mss_confirmed"]  = True
                result["mss_level"]      = mss_level
                result["mss_candle_idx"] = i
                return result

    return result


# ─────────────────────────────────────────────────────────────
# RETEST DETECTION
# ─────────────────────────────────────────────────────────────

def detect_retest(df: pd.DataFrame, mss_level: float, mss_idx: int,
                  direction: str, tolerance: float = 0.005) -> dict:
    """
    Sau MSS, tìm retest về mss_level.

    LONG: giá pullback về gần mss_level từ trên (không dưới)
    SHORT: giá pullback về gần mss_level từ dưới (không trên)

    Returns:
        {"retested": bool, "retest_price": float, "retest_idx": int, "held": bool}
    """
    result = {"retested": False, "retest_price": mss_level,
              "retest_idx": -1, "held": False}

    lows   = df["low"].values
    highs  = df["high"].values
    closes = df["close"].values
    n      = len(df)

    if mss_idx < 0 or mss_idx >= n:
        return result

    zone_upper = mss_level * (1 + tolerance)
    zone_lower = mss_level * (1 - tolerance)

    for i in range(mss_idx + 1, n):
        if direction == "LONG":
            # Giá pullback về zone (low chạm zone, nhưng close vẫn trên mss_level)
            if lows[i] <= zone_upper and lows[i] >= zone_lower * 0.995:
                result["retested"]    = True
                result["retest_price"] = float(lows[i])
                result["retest_idx"]  = i
                # Held: nến sau đóng lại trên mss_level
                if i + 1 < n and closes[i + 1] > mss_level:
                    result["held"] = True
                elif closes[i] > mss_level:
                    result["held"] = True
                return result

        else:  # SHORT
            if highs[i] >= zone_lower and highs[i] <= zone_upper * 1.005:
                result["retested"]    = True
                result["retest_price"] = float(highs[i])
                result["retest_idx"]  = i
                if i + 1 < n and closes[i + 1] < mss_level:
                    result["held"] = True
                elif closes[i] < mss_level:
                    result["held"] = True
                return result

    return result


# ─────────────────────────────────────────────────────────────
# 5M CONFIRMATION
# ─────────────────────────────────────────────────────────────

def confirm_5m(df_5m: pd.DataFrame, direction: str,
               lookback: int = 12, vol_mult: float = 1.2) -> dict:
    """
    Xác nhận structure 5m cùng chiều.

    LONG: 5m có higher low + close trên EMA9 + volume OK
    SHORT: 5m có lower high + close dưới EMA9 + volume OK

    Returns:
        {"confirmed": bool, "reason": str, "score": float}
    """
    result = {"confirmed": False, "reason": "no 5m data", "score": 0.0}

    if df_5m is None or len(df_5m) < lookback:
        return result

    try:
        closes = df_5m["close"].values[-lookback:]
        highs  = df_5m["high"].values[-lookback:]
        lows   = df_5m["low"].values[-lookback:]
        vols   = df_5m["volume"].values[-lookback:]

        # EMA9
        from indicators import calculate_ema
        ema9 = calculate_ema(pd.Series(df_5m["close"].values[-lookback:]), 9).values

        cur_close = closes[-1]
        cur_vol   = vols[-1]
        avg_vol   = vols[:-1].mean() if len(vols) > 1 else vols[-1]
        vol_ok    = cur_vol >= avg_vol * vol_mult

        score = 0.0
        reasons = []

        if direction == "LONG":
            # Higher low: low gần nhất cao hơn low trước đó
            hl = len(lows) >= 3 and lows[-1] > lows[-3]
            # Close trên EMA9
            above_ema = cur_close > ema9[-1]
            # Bullish nến cuối
            bull_candle = closes[-1] > df_5m["open"].values[-lookback:][-1]

            if hl:        score += 40; reasons.append("HL")
            if above_ema: score += 35; reasons.append("above_EMA9")
            if vol_ok:    score += 25; reasons.append(f"vol×{cur_vol/avg_vol:.1f}")
            if bull_candle: score = min(score + 10, 100)

        else:  # SHORT
            ll = len(highs) >= 3 and highs[-1] < highs[-3]
            below_ema = cur_close < ema9[-1]
            bear_candle = closes[-1] < df_5m["open"].values[-lookback:][-1]

            if ll:         score += 40; reasons.append("LL")
            if below_ema:  score += 35; reasons.append("below_EMA9")
            if vol_ok:     score += 25; reasons.append(f"vol×{cur_vol/avg_vol:.1f}")
            if bear_candle: score = min(score + 10, 100)

        result["confirmed"] = score >= 60
        result["score"]     = round(score, 1)
        result["reason"]    = " | ".join(reasons) if reasons else "weak"

    except Exception as e:
        result["reason"] = f"5m error: {e}"

    return result


# ─────────────────────────────────────────────────────────────
# VOLUME CONFIRMATION
# ─────────────────────────────────────────────────────────────

def check_volume_confirm(df: pd.DataFrame, candle_idx: int,
                         vol_mult: float = 1.2) -> bool:
    """Volume tại candle_idx có lớn hơn avg không."""
    try:
        vols = df["volume"].values
        if candle_idx < 1 or candle_idx >= len(vols):
            return True  # không đủ data → không block
        avg = vols[max(0, candle_idx - 10):candle_idx].mean()
        return float(vols[candle_idx]) >= avg * vol_mult
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────
# MAIN: ANALYZE MSS
# ─────────────────────────────────────────────────────────────

def analyze_mss(df_15m: pd.DataFrame, df_5m: Optional[pd.DataFrame],
                direction: str, cfg=None) -> MSSResult:
    """
    Phân tích MSS/Sweep cho 1 coin trên 15m + 5m.

    Args:
        df_15m:    DataFrame 15m (cần ít nhất 30 nến)
        df_5m:     DataFrame 5m (None nếu chưa fetch)
        direction: "LONG" | "SHORT"
        cfg:       config module

    Returns:
        MSSResult với tier A/B/C/D và entry/sl info
    """
    no_trade = MSSResult(
        tier="D", signal="NONE", entry_price=0.0, sl_price=0.0,
        confidence=0.0, sweep_price=0.0, mss_level=0.0,
        retest_level=0.0, reason="no setup"
    )

    if df_15m is None or len(df_15m) < 25:
        return no_trade

    # ── Params ──────────────────────────────────────────────
    lookback    = getattr(cfg, "MSS_SWING_LOOKBACK",    20)    if cfg else 20
    sweep_tol   = getattr(cfg, "MSS_SWEEP_TOLERANCE",   0.001) if cfg else 0.001
    reclaim_tol = getattr(cfg, "MSS_RECLAIM_TOLERANCE", 0.001) if cfg else 0.001
    chase_atr   = getattr(cfg, "MSS_MAX_CHASE_ATR",     1.5)   if cfg else 1.5
    vol_mult    = getattr(cfg, "MSS_VOLUME_MULT",        1.2)   if cfg else 1.2
    use_5m      = getattr(cfg, "MSS_USE_5M_CONFIRM",    True)  if cfg else True
    lookback_5m = getattr(cfg, "MSS_5M_LOOKBACK",       12)    if cfg else 12

    # ── Tính ATR cho SL buffer ───────────────────────────────
    try:
        from indicators import calculate_atr
        atr = float(calculate_atr(df_15m["high"], df_15m["low"],
                                   df_15m["close"]).iloc[-1])
    except Exception:
        atr = float(df_15m["close"].iloc[-1]) * 0.01

    cur_price = float(df_15m["close"].iloc[-1])

    # ── Tìm swings ──────────────────────────────────────────
    swings = find_swings(df_15m, lookback=lookback, min_pivot=2)
    if not swings:
        return MSSResult(tier="D", signal="NONE", entry_price=0.0, sl_price=0.0,
                         confidence=0.0, sweep_price=0.0, mss_level=0.0,
                         retest_level=0.0, reason="no swings found")

    # ── Lấy swing cần thiết theo hướng ──────────────────────
    if direction == "LONG":
        swing = get_recent_swing_low(swings)
    else:
        swing = get_recent_swing_high(swings)

    if swing is None:
        return MSSResult(tier="D", signal="NONE", entry_price=0.0, sl_price=0.0,
                         confidence=0.0, sweep_price=0.0, mss_level=0.0,
                         retest_level=0.0, reason=f"no swing {direction}")

    # ── Detect Sweep ─────────────────────────────────────────
    sweep_info = detect_sweep(df_15m, swing, direction, tolerance=sweep_tol)

    if not sweep_info["swept"]:
        return MSSResult(tier="D", signal="NONE", entry_price=cur_price,
                         sl_price=0.0, confidence=0.0,
                         sweep_price=swing.price, mss_level=0.0,
                         retest_level=0.0, reason="no liquidity sweep")

    if not sweep_info["reclaimed"]:
        # Swept nhưng chưa reclaim → TIER C: PENDING
        return MSSResult(tier="C", signal="NONE", entry_price=cur_price,
                         sl_price=0.0, confidence=20.0,
                         sweep_price=sweep_info["sweep_price"],
                         mss_level=0.0, retest_level=0.0,
                         reason=f"sweep detected, waiting reclaim @ {swing.price:.6f}")

    reclaim_idx = sweep_info["reclaim_candle_idx"]

    # ── Volume check tại sweep ───────────────────────────────
    vol_ok = check_volume_confirm(df_15m, sweep_info["sweep_candle_idx"], vol_mult)

    # ── Detect MSS 15m ───────────────────────────────────────
    mss_info = detect_mss_15m(df_15m, swings, reclaim_idx, direction)

    if mss_info["mss_confirmed"]:
        mss_level   = mss_info["mss_level"]
        mss_idx     = mss_info["mss_candle_idx"]

        # ── Detect Retest ────────────────────────────────────
        retest_info = detect_retest(df_15m, mss_level, mss_idx, direction)

        # SL: dưới/trên sweep point + ATR buffer
        sl_buf = atr * 0.5
        if direction == "LONG":
            sl_price   = round(sweep_info["sweep_price"] - sl_buf, 8)
            entry_zone = retest_info["retest_price"] if retest_info["retested"] else mss_level
        else:
            sl_price   = round(sweep_info["sweep_price"] + sl_buf, 8)
            entry_zone = retest_info["retest_price"] if retest_info["retested"] else mss_level

        # ── No-chase check ───────────────────────────────────
        chase_dist = abs(cur_price - entry_zone)
        if chase_dist > chase_atr * atr:
            return MSSResult(tier="C", signal="NONE",
                             entry_price=entry_zone, sl_price=sl_price,
                             confidence=30.0, sweep_price=sweep_info["sweep_price"],
                             mss_level=mss_level, retest_level=entry_zone,
                             reason=f"MSS OK but price chased {chase_dist:.5f} > {chase_atr}×ATR")

        # ── 5M Confirmation → TIER A ─────────────────────────
        tier = "B"
        confidence = 60.0
        five_m_reason = "no 5m"

        if use_5m and df_5m is not None:
            five_m = confirm_5m(df_5m, direction, lookback_5m, vol_mult)
            five_m_reason = five_m["reason"]
            if five_m["confirmed"] and retest_info["retested"] and retest_info["held"]:
                tier       = "A"
                confidence = 85.0 + (five_m["score"] - 60) * 0.3   # 85-91
            elif five_m["confirmed"]:
                tier       = "A"
                confidence = 75.0
            else:
                # 5m không confirm → B nếu có retest, C nếu không
                if retest_info["retested"]:
                    tier       = "B"
                    confidence = 55.0
                else:
                    tier       = "B"
                    confidence = 45.0
        else:
            # Không có 5m → dựa vào retest
            if retest_info["retested"] and retest_info["held"]:
                tier       = "A"
                confidence = 70.0
            else:
                tier       = "B"
                confidence = 50.0

        if not vol_ok:
            confidence = max(confidence - 15, 20)
            tier = "B" if tier == "A" else tier

        reason = (f"15M MSS {direction} | sweep={sweep_info['sweep_price']:.6f} "
                  f"mss_level={mss_level:.6f} retest={retest_info['retested']} "
                  f"held={retest_info['held']} 5m={five_m_reason} vol={'✅' if vol_ok else '⚠️'}")

        return MSSResult(
            tier=tier, signal=direction,
            entry_price=round(entry_zone, 8),
            sl_price=sl_price,
            confidence=round(min(confidence, 100), 1),
            sweep_price=sweep_info["sweep_price"],
            mss_level=mss_level,
            retest_level=float(entry_zone),
            reason=reason,
        )

    else:
        # Sweep + reclaim nhưng chưa MSS 15m
        # Thử fallback: 5m MSS → TIER B
        if use_5m and df_5m is not None:
            five_m = confirm_5m(df_5m, direction, lookback_5m, vol_mult)
            if five_m["confirmed"]:
                # Dùng swing level làm entry
                entry_zone = swing.price
                sl_buf     = atr * 0.5
                if direction == "LONG":
                    sl_price = round(sweep_info["sweep_price"] - sl_buf, 8)
                else:
                    sl_price = round(sweep_info["sweep_price"] + sl_buf, 8)

                chase_dist = abs(cur_price - entry_zone)
                if chase_dist > chase_atr * atr:
                    return MSSResult(tier="C", signal="NONE",
                                     entry_price=entry_zone, sl_price=sl_price,
                                     confidence=25.0,
                                     sweep_price=sweep_info["sweep_price"],
                                     mss_level=0.0, retest_level=entry_zone,
                                     reason="fallback 5M MSS but price chased")

                return MSSResult(
                    tier="B", signal=direction,
                    entry_price=round(entry_zone, 8),
                    sl_price=sl_price,
                    confidence=40.0,
                    sweep_price=sweep_info["sweep_price"],
                    mss_level=0.0,
                    retest_level=float(entry_zone),
                    reason=(f"FALLBACK 5M MSS {direction} | "
                            f"sweep={sweep_info['sweep_price']:.6f} "
                            f"5m={five_m['reason']} vol={'✅' if vol_ok else '⚠️'}"),
                )

        # Không có 5m MSS → TIER C: chờ
        return MSSResult(
            tier="C", signal="NONE",
            entry_price=cur_price, sl_price=0.0,
            confidence=15.0,
            sweep_price=sweep_info["sweep_price"],
            mss_level=0.0, retest_level=0.0,
            reason=f"sweep+reclaim OK but no 15M/5M MSS yet — PENDING"
        )


# ─────────────────────────────────────────────────────────────
# PENDING SETUP MANAGEMENT
# ─────────────────────────────────────────────────────────────

class MSSPendingManager:
    """
    Quản lý danh sách setups đang chờ MSS confirmation.
    {symbol: {"direction", "result", "ts", "retries"}}
    """

    def __init__(self):
        self._pending: dict = {}

    def add(self, symbol: str, direction: str, result: MSSResult):
        self._pending[symbol] = {
            "direction": direction,
            "result":    result,
            "ts":        time.time(),
            "retries":   0,
        }

    def remove(self, symbol: str):
        self._pending.pop(symbol, None)

    def get(self, symbol: str) -> Optional[dict]:
        return self._pending.get(symbol)

    def expire_old(self, max_age_minutes: float = 30.0):
        """Xóa setups cũ hơn max_age_minutes."""
        cutoff = time.time() - max_age_minutes * 60
        expired = [s for s, v in self._pending.items() if v["ts"] < cutoff]
        for s in expired:
            logger.debug(f"[MSS] Expired pending: {s}")
            self._pending.pop(s, None)

    def all_pending(self) -> dict:
        return dict(self._pending)

    def __len__(self):
        return len(self._pending)


# Global singleton
_mss_pending = MSSPendingManager()


def get_mss_pending() -> MSSPendingManager:
    return _mss_pending
