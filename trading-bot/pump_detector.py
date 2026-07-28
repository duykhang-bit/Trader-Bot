# ============================================================
# PUMP DETECTOR — Phát hiện đỉnh pump để SHORT
# ============================================================
# 6 tín hiệu chấm điểm (tổng 100đ):
#   1. Volume Exhaustion  (25đ) — volume kiệt sức sau pump
#   2. Wick Rejection     (20đ) — bóng nến trên dài = bị xả
#   3. RSI Divergence     (20đ) — giá tăng nhưng RSI không theo
#   4. Price Deceleration (15đ) — tốc độ tăng chậm dần từng nến
#   5. Thin Pump          (10đ) — bay không cần thanh khoản = dev bơm
#   6. HTF Rejection      (10đ) — 15m BB upper / xa EMA50 / RSI OB
#
# Score >= PUMP_TOP_MIN_SCORE (default 60) → SHORT signal
# ============================================================
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple

import pandas as pd
import numpy as np

# Import đúng hàm từ indicators.py thực tế
from indicators import (
    calculate_rsi,
    calculate_ema,
    calculate_atr,
    calculate_bollinger,
    calculate_volume_ma,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# CONFIG DEFAULTS — override bằng config.py
# ─────────────────────────────────────────────────────────────
DEFAULT_CFG = {
    "PUMP_PRICE_RISE_PCT":      15.0,   # % tăng tối thiểu từ đáy để coi là "đang pump"
    "PUMP_LOOKBACK_CANDLES":    20,     # số nến 1m nhìn lại để tìm đáy
    "VOL_EXHAUST_RATIO":        0.45,   # volume hiện tại <= 45% đỉnh pump → kiệt sức
    "VOL_EXHAUST_CANDLES":      5,      # số nến cuối để tính volume trung bình
    "WICK_REJECT_RATIO":        1.8,    # bóng trên >= 1.8× thân nến
    "RSI_DIV_LOOKBACK":         10,     # số nến nhìn lại tìm RSI peak trước
    "DECEL_CANDLES":            3,      # số nến cuối đo đà giảm tốc
    "PUMP_TOP_MIN_SCORE":       60,     # tổng điểm >= 60 → xác nhận đỉnh
    "PUMP_SIGNAL_COOLDOWN_S":   300,    # 5 phút không spam cùng coin
}


# ─────────────────────────────────────────────────────────────
# DATA CLASS
# ─────────────────────────────────────────────────────────────
@dataclass
class PumpSignal:
    """Kết quả phân tích 1 coin."""
    symbol:       str
    is_pump_top:  bool          # True → SHORT ngay
    score:        int           # 0–100
    pump_pct:     float         # % giá tăng từ đáy
    signals:      List[str]     # danh sách tín hiệu kích hoạt
    entry_price:  float         # giá entry đề xuất
    sl_price:     float         # SL = đỉnh + 1.5% buffer
    tp1_price:    float         # TP1 = 38.2% Fibonacci retracement
    tp2_price:    float         # TP2 = 61.8% Fibonacci retracement
    atr:          float
    volume_ratio: float         # volume / MA20
    rsi:          float
    timestamp:    float = field(default_factory=time.time)

    def to_telegram(self) -> str:
        score_bar = "█" * (self.score // 10) + "░" * (10 - self.score // 10)
        sigs = "\n".join(f"  • {s}" for s in self.signals)
        rr = abs(self.entry_price - self.tp1_price) / abs(self.entry_price - self.sl_price) if abs(self.entry_price - self.sl_price) > 0 else 0
        return (
            f"🚨 <b>PUMP TOP — SHORT SIGNAL</b>\n"
            f"{'─'*34}\n"
            f"🪙 Coin   : <b>{self.symbol}</b>\n"
            f"📈 Pump   : <b>+{self.pump_pct:.1f}%</b> từ đáy\n"
            f"📊 Score  : <b>{self.score}/100</b>  [{score_bar}]\n"
            f"{'─'*34}\n"
            f"🔴 Entry  : <b>${self.entry_price:,.6g}</b>\n"
            f"🛑 SL     : <b>${self.sl_price:,.6g}</b>  (+1.5%)\n"
            f"🎯 TP1    : <b>${self.tp1_price:,.6g}</b>  (38.2%)\n"
            f"🎯 TP2    : <b>${self.tp2_price:,.6g}</b>  (61.8%)\n"
            f"📐 RR     : 1:{rr:.1f}\n"
            f"{'─'*34}\n"
            f"📉 RSI    : {self.rsi:.1f}  |  Vol: {self.volume_ratio:.1f}×\n"
            f"{'─'*34}\n"
            f"🔍 <b>Tín hiệu:</b>\n{sigs}\n"
            f"{'─'*34}\n"
            f"⚠️ <i>Dev pump — dùng size nhỏ, SL chặt</i>"
        )


# ─────────────────────────────────────────────────────────────
# CORE DETECTOR
# ─────────────────────────────────────────────────────────────
class PumpDetector:
    """
    Phát hiện đỉnh pump real-time.

    Cách dùng:
        detector = PumpDetector(config)
        signal = detector.analyze("BANKUSDT", df_1m, df_15m)
        if signal and signal.is_pump_top:
            # gửi Telegram / vào SHORT
    """

    def __init__(self, config=None):
        self.cfg = self._load_cfg(config)
        self._cooldown: Dict[str, float] = {}  # {symbol: last_signal_ts}

    @staticmethod
    def _load_cfg(config) -> dict:
        cfg = dict(DEFAULT_CFG)
        if config is None:
            return cfg
        for k in DEFAULT_CFG:
            v = getattr(config, k, None)
            if v is not None:
                cfg[k] = v
        return cfg

    # ── Public ────────────────────────────────────────────────
    def analyze(self,
                symbol:  str,
                df_1m:   pd.DataFrame,
                df_15m:  Optional[pd.DataFrame] = None) -> Optional[PumpSignal]:
        """
        Phân tích coin có đang ở đỉnh pump không.

        Args:
            symbol:  ví dụ "BANKUSDT"
            df_1m:   DataFrame 1m (tối thiểu 50 nến)
            df_15m:  DataFrame 15m (tùy chọn, dùng để HTF confirm)

        Returns:
            PumpSignal hoặc None
        """
        if df_1m is None or len(df_1m) < 30:
            return None

        # Cooldown — không spam cùng coin
        now = time.time()
        if now - self._cooldown.get(symbol, 0) < self.cfg["PUMP_SIGNAL_COOLDOWN_S"]:
            return None

        # Step 1: Có đang pump không?
        pump_pct, pump_low, pump_high = self._detect_pump_move(df_1m)
        if pump_pct < self.cfg["PUMP_PRICE_RISE_PCT"]:
            return None

        logger.info(f"[PumpDetector] {symbol}: pump +{pump_pct:.1f}% | checking top...")

        # Step 2: Chấm điểm đỉnh
        score, signals = self._score_pump_top(df_1m, df_15m, pump_high)

        # Step 3: Tính entry / SL / TP
        current_price = df_1m["close"].iloc[-1]
        atr     = calculate_atr(df_1m["high"], df_1m["low"], df_1m["close"], 14).iloc[-1]
        rsi     = calculate_rsi(df_1m["close"], 14).iloc[-1]
        vol_ma  = calculate_volume_ma(df_1m["volume"], 20).iloc[-1]
        vol_ratio = df_1m["volume"].iloc[-1] / vol_ma if vol_ma > 0 else 1.0

        sl_price  = pump_high * 1.015                                          # SL 1.5% trên đỉnh
        tp1_price = current_price - (current_price - pump_low) * 0.382        # 38.2% fib
        tp2_price = current_price - (current_price - pump_low) * 0.618        # 61.8% fib

        is_top = score >= self.cfg["PUMP_TOP_MIN_SCORE"]

        if is_top:
            self._cooldown[symbol] = now
            logger.info(f"[PumpDetector] {symbol}: TOP CONFIRMED score={score} | {signals}")

        return PumpSignal(
            symbol       = symbol,
            is_pump_top  = is_top,
            score        = min(score, 100),
            pump_pct     = pump_pct,
            signals      = signals,
            entry_price  = round(current_price, 8),
            sl_price     = round(sl_price, 8),
            tp1_price    = round(tp1_price, 8),
            tp2_price    = round(tp2_price, 8),
            atr          = round(atr, 8),
            volume_ratio = round(vol_ratio, 2),
            rsi          = round(rsi, 1),
        )

    # ── Phát hiện đợt pump ───────────────────────────────────
    def _detect_pump_move(self, df: pd.DataFrame) -> Tuple[float, float, float]:
        """Tìm đỉnh/đáy trong PUMP_LOOKBACK_CANDLES nến gần nhất."""
        lb  = self.cfg["PUMP_LOOKBACK_CANDLES"]
        win = df.tail(lb)
        low  = win["low"].min()
        high = win["high"].max()
        if low <= 0:
            return 0.0, low, high
        pct = (high - low) / low * 100
        return round(pct, 2), low, high

    # ── Chấm điểm tổng ───────────────────────────────────────
    def _score_pump_top(self,
                        df_1m:  pd.DataFrame,
                        df_15m: Optional[pd.DataFrame],
                        pump_high: float) -> Tuple[int, List[str]]:
        score   = 0
        signals = []

        s, sig = self._vol_exhaustion(df_1m)
        score += s
        if sig: signals.append(sig)

        s, sig = self._wick_rejection(df_1m)
        score += s
        if sig: signals.append(sig)

        s, sig = self._rsi_divergence(df_1m)
        score += s
        if sig: signals.append(sig)

        s, sig = self._price_deceleration(df_1m)
        score += s
        if sig: signals.append(sig)

        s, sig = self._thin_pump(df_1m)
        score += s
        if sig: signals.append(sig)

        if df_15m is not None and len(df_15m) >= 20:
            s, sig = self._htf_rejection(df_15m)
            score += s
            if sig: signals.append(sig)

        return score, signals

    # ── Tín hiệu 1: Volume kiệt sức (25đ) ───────────────────
    def _vol_exhaustion(self, df: pd.DataFrame) -> Tuple[int, str]:
        n = self.cfg["VOL_EXHAUST_CANDLES"]
        if len(df) < n + 5:
            return 0, ""

        vol      = df["volume"]
        vol_peak = vol.iloc[-20:].max() if len(vol) >= 20 else vol.max()
        vol_cur  = vol.iloc[-1]
        vol_avg  = vol.iloc[-n:].mean()

        ex_ratio = vol_cur  / vol_peak if vol_peak > 0 else 1.0
        tr_ratio = vol_avg  / vol_peak if vol_peak > 0 else 1.0

        if ex_ratio <= 0.25:
            return 25, f"🔴 Volume kiệt sức nặng ({ex_ratio:.0%} vs đỉnh pump)"
        elif ex_ratio <= self.cfg["VOL_EXHAUST_RATIO"]:
            return 18, f"🟠 Volume giảm mạnh ({ex_ratio:.0%} vs đỉnh)"
        elif tr_ratio <= 0.55:
            return 10, f"🟡 Volume đang giảm dần ({tr_ratio:.0%} vs đỉnh)"
        return 0, ""

    # ── Tín hiệu 2: Wick rejection (20đ) ────────────────────
    def _wick_rejection(self, df: pd.DataFrame) -> Tuple[int, str]:
        if len(df) < 3:
            return 0, ""

        best_s, best_sig = 0, ""
        for i in [-1, -2, -3]:
            row = df.iloc[i]
            o, h, l, c = row["open"], row["high"], row["low"], row["close"]
            body  = abs(c - o)
            upper = h - max(c, o)
            total = h - l if h > l else 0.0001

            # Bỏ doji
            if body < total * 0.05:
                continue

            ratio     = upper / body      if body  > 0 else 0
            upper_pct = upper / total * 100

            if   ratio >= 3.0 and upper_pct >= 50:
                s, sig = 20, f"🔴 Wick rejection cực mạnh (bóng {ratio:.1f}× thân)"
            elif ratio >= self.cfg["WICK_REJECT_RATIO"] and upper_pct >= 40:
                s, sig = 15, f"🟠 Wick rejection mạnh ({ratio:.1f}× thân, {upper_pct:.0f}% range)"
            elif upper_pct >= 35:
                s, sig = 8,  f"🟡 Bóng trên dài ({upper_pct:.0f}% range)"
            else:
                continue

            if s > best_s:
                best_s, best_sig = s, sig

        return best_s, best_sig

    # ── Tín hiệu 3: RSI Divergence (20đ) ────────────────────
    def _rsi_divergence(self, df: pd.DataFrame) -> Tuple[int, str]:
        lb = self.cfg["RSI_DIV_LOOKBACK"]
        if len(df) < lb + 5:
            return 0, ""

        rsi   = calculate_rsi(df["close"], 14)
        price = df["close"]

        rsi_now   = rsi.iloc[-1]
        price_now = price.iloc[-1]

        # Peak RSI trong cửa sổ trước (không tính 3 nến cuối)
        rsi_win   = rsi.iloc[-(lb+3):-3]
        price_win = price.iloc[-(lb+3):-3]
        if rsi_win.empty:
            return 0, ""

        rsi_prev_peak   = rsi_win.max()
        price_prev_peak = price_win.max()

        ob_bonus = 5 if rsi_now > 70 else 0
        ob_tag   = f" | RSI={rsi_now:.0f} OB" if ob_bonus else f" | RSI={rsi_now:.0f}"

        price_still_high = price_now >= price_prev_peak * 0.97
        rsi_lower        = rsi_now   <  rsi_prev_peak   - 5

        if price_still_high and rsi_lower:
            gap   = rsi_prev_peak - rsi_now
            score = min(15 + ob_bonus, 20)
            return score, f"🔴 Bearish RSI divergence (peak {rsi_prev_peak:.0f}→{rsi_now:.0f}, Δ{gap:.0f}){ob_tag}"
        elif rsi_now > 70:
            return 8, f"🟠 RSI overbought ({rsi_now:.0f})"
        elif rsi_now > 65:
            return 4, f"🟡 RSI cao ({rsi_now:.0f})"
        return 0, ""

    # ── Tín hiệu 4: Price Deceleration (15đ) ────────────────
    def _price_deceleration(self, df: pd.DataFrame) -> Tuple[int, str]:
        n = self.cfg["DECEL_CANDLES"]
        if len(df) < n + 2:
            return 0, ""

        close = df["close"]
        changes = []
        for i in range(-n, 0):
            prev = close.iloc[i - 1]
            cur  = close.iloc[i]
            if prev > 0:
                changes.append((cur - prev) / prev * 100)

        if len(changes) < 2:
            return 0, ""

        decelerating = all(changes[i] > changes[i+1] for i in range(len(changes)-1))
        last_positive = changes[-1] > 0
        slowing = changes[0] > 0 and changes[-1] < changes[0] * 0.4

        if decelerating and last_positive:
            return 15, f"🔴 Đà tăng chậm dần rõ ({changes[0]:.2f}%→{changes[-1]:.2f}%)"
        elif slowing:
            return 8,  f"🟠 Đà tăng giảm mạnh ({changes[0]:.2f}%→{changes[-1]:.2f}%)"
        return 0, ""

    # ── Tín hiệu 5: Thin Pump (10đ) ─────────────────────────
    def _thin_pump(self, df: pd.DataFrame) -> Tuple[int, str]:
        """Giá fly nhưng volume rất thấp = dev bơm không cần thanh khoản."""
        if len(df) < 20:
            return 0, ""

        close  = df["close"]
        volume = df["volume"]

        price_10ago = close.iloc[-10]
        price_now   = close.iloc[-1]
        price_chg   = (price_now - price_10ago) / price_10ago * 100 if price_10ago > 0 else 0

        # Volume baseline = 20 nến trước đó
        vol_base  = volume.iloc[-30:-10].mean() if len(volume) >= 30 else volume.mean()
        vol_dur   = volume.iloc[-10:].mean()
        vol_mult  = vol_dur / vol_base if vol_base > 0 else 1.0

        if   price_chg > 10 and vol_mult < 2.0:
            return 10, f"🔴 Thin pump: +{price_chg:.1f}% với vol {vol_mult:.1f}× (dev bơm!)"
        elif price_chg > 5  and vol_mult < 1.5:
            return 6,  f"🟠 Suspicious pump: +{price_chg:.1f}% vol thấp ({vol_mult:.1f}×)"
        return 0, ""

    # ── Tín hiệu 6: HTF Rejection 15m (10đ) ─────────────────
    def _htf_rejection(self, df_15m: pd.DataFrame) -> Tuple[int, str]:
        """Giá đang ở BB upper / quá xa EMA50 / RSI OB trên 15m."""
        if len(df_15m) < 20:
            return 0, ""

        close = df_15m["close"]
        rsi   = calculate_rsi(close, 14)
        ema50 = calculate_ema(close, 50)
        bb_u, bb_m, bb_l = calculate_bollinger(close, 20, 2.0)

        price = close.iloc[-1]
        rsi15 = rsi.iloc[-1]
        e50   = ema50.iloc[-1]
        bbu   = bb_u.iloc[-1]

        score = 0
        parts = []

        if price >= bbu * 0.995:
            score += 5
            parts.append("15m BB upper")
        if e50 > 0 and price > e50 * 1.05:
            score += 3
            parts.append(f"15m giá xa EMA50 +{(price/e50-1)*100:.0f}%")
        if rsi15 > 70:
            score += 2
            parts.append(f"15m RSI={rsi15:.0f} OB")

        if score > 0:
            return score, f"🟠 HTF: {' | '.join(parts)}"
        return 0, ""


# ─────────────────────────────────────────────────────────────
# HELPER: Convert klines → DataFrame
# ─────────────────────────────────────────────────────────────
def _to_df(klines: list) -> pd.DataFrame:
    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


# ─────────────────────────────────────────────────────────────
# SCANNER HELPER — gọi từ pump_scan_engine trong bot.py
# ─────────────────────────────────────────────────────────────
def scan_for_pump_tops(exchange,
                        symbols: List[str],
                        config,
                        notifier=None) -> List[PumpSignal]:
    """
    Quét danh sách coin, tìm đỉnh pump để SHORT.
    Trả về danh sách PumpSignal confirmed (is_pump_top=True).
    """
    detector  = PumpDetector(config)
    confirmed = []

    for symbol in symbols:
        try:
            klines_1m  = exchange.get_klines(symbol, "1m",  limit=200)
            klines_15m = exchange.get_klines(symbol, "15m", limit=50)
            df_1m      = _to_df(klines_1m)
            df_15m     = _to_df(klines_15m)

            sig = detector.analyze(symbol, df_1m, df_15m)
            if sig is None:
                continue

            logger.info(
                f"[PumpScan] {symbol}: +{sig.pump_pct:.1f}% "
                f"score={sig.score} top={sig.is_pump_top}"
            )

            if sig.is_pump_top:
                confirmed.append(sig)
                if notifier:
                    try:
                        notifier.telegram.send(sig.to_telegram())
                        logger.info(f"[PumpScan] Alert sent: {symbol}")
                    except Exception as e:
                        logger.warning(f"[PumpScan] Telegram failed: {e}")

        except Exception as e:
            logger.debug(f"[PumpScan] {symbol} error: {e}")

    return confirmed
