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
    "PUMP_TOP_MIN_SCORE":       75,     # tổng điểm >= 75 → xác nhận đỉnh (tăng từ 60 để tránh vào sớm)
    "PUMP_SIGNAL_COOLDOWN_S":   300,    # 5 phút không spam cùng coin
}


# ─────────────────────────────────────────────────────────────
# DATA CLASS — PUMP ALERT (pump đang lên, chưa đủ điều kiện SHORT)
# ─────────────────────────────────────────────────────────────
@dataclass
class PumpAlertSignal:
    """
    Tín hiệu coin đang pump nhưng chưa đủ điều kiện SHORT.
    Gửi thông báo để trader tự quyết định vào LONG (đang đà) hay chờ SHORT.
    """
    symbol:      str
    pump_pct:    float    # % tăng từ đáy trong cửa sổ PUMP_LOOKBACK_CANDLES
    price:       float    # giá hiện tại
    rsi:         float
    volume_ratio: float   # vol hiện tại / MA20
    score:       int      # 0-100, score pump top (chưa đủ để SHORT)
    reason:      str      # mô tả ngắn tại sao chưa SHORT
    timestamp:   float = field(default_factory=time.time)

    def to_telegram(self) -> str:
        bar = "█" * (self.pump_pct // 5 if self.pump_pct < 50 else 10)
        bar = bar[:10].ljust(10, "░")
        return (
            f"🚀 <b>PUMP ALERT — Đang bơm!</b>\n"
            f"{'─'*30}\n"
            f"🪙 Coin    : <b>{self.symbol}</b>\n"
            f"📈 Pump    : <b>+{self.pump_pct:.1f}%</b> từ đáy\n"
            f"💰 Giá HT  : <b>${self.price:,.6g}</b>\n"
            f"📊 RSI     : {self.rsi:.1f}  |  Vol: {self.volume_ratio:.1f}×\n"
            f"📉 Score   : {self.score}/100 (cần ≥ ngưỡng SHORT)\n"
            f"{'─'*30}\n"
            f"⚠️ <i>{self.reason}</i>\n"
            f"💡 Có thể: ▲ LONG theo đà  hoặc  ⏳ Chờ đỉnh để SHORT\n"
            f"⏰ {__import__('datetime').datetime.fromtimestamp(self.timestamp).strftime('%H:%M:%S')}"
        )


# ─────────────────────────────────────────────────────────────
# DATA CLASS — PUMP SIGNAL (đỉnh pump, SHORT)
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
    entry_type:   str           # "LIMIT" hoặc "MARKET"
    sl_price:     float         # SL = đỉnh thật + ATR buffer
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
        sl_pct = abs(self.sl_price - self.entry_price) / self.entry_price * 100 if self.entry_price > 0 else 0
        return (
            f"🚨 <b>PUMP TOP — SHORT SIGNAL</b>\n"
            f"{'─'*34}\n"
            f"🪙 Coin   : <b>{self.symbol}</b>\n"
            f"📈 Pump   : <b>+{self.pump_pct:.1f}%</b> từ đáy\n"
            f"📊 Score  : <b>{self.score}/100</b>  [{score_bar}]\n"
            f"{'─'*34}\n"
            f"🔴 Entry  : <b>${self.entry_price:,.6g}</b>  [{self.entry_type}]\n"
            f"🛑 SL     : <b>${self.sl_price:,.6g}</b>  (+{sl_pct:.1f}% từ đỉnh)\n"
            f"🎯 TP1    : <b>${self.tp1_price:,.6g}</b>  (38.2%)\n"
            f"🎯 TP2    : <b>${self.tp2_price:,.6g}</b>  (61.8%)\n"
            f"📐 RR     : 1:{rr:.1f}\n"
            f"{'─'*34}\n"
            f"📉 RSI    : {self.rsi:.1f}  |  Vol: {self.volume_ratio:.1f}×\n"
            f"{'─'*34}\n"
            f"🔍 <b>Tín hiệu:</b>\n{sigs}\n"
            f"{'─'*34}\n"
            f"⚠️ <i>Dev pump — dùng size nhỏ, SL theo ATR</i>"
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

    def check_pump_rising(self,
                          symbol: str,
                          df_1m:  pd.DataFrame,
                          df_15m: Optional[pd.DataFrame] = None,
                          alert_cooldown: dict = None) -> Optional["PumpAlertSignal"]:
        """
        Phát hiện coin đang pump lên nhưng CHƯA đủ điều kiện SHORT.
        Dùng để gửi thông báo sớm cho trader tự quyết định:
          - Vào LONG theo đà
          - Chờ đỉnh để SHORT

        Điều kiện kích hoạt alert (thấp hơn nhiều so với SHORT):
          - Pump >= PUMP_PRICE_RISE_PCT * 0.5  (ví dụ: 10% thay vì 20%)
          - Volume surge >= 1.5x MA20
          - Chưa phải đỉnh (RSI < 72 hoặc score < min_score)

        Args:
            symbol:         ký hiệu coin
            df_1m:          DataFrame 1m
            df_15m:         DataFrame 15m (tùy chọn)
            alert_cooldown: dict {symbol: ts} để tránh spam (mutable, pass ref)

        Returns:
            PumpAlertSignal nếu đủ điều kiện alert, None nếu không
        """
        if df_1m is None or len(df_1m) < 20:
            return None

        now = time.time()

        # Cooldown riêng cho alert (ít nhất 5 phút)
        if alert_cooldown is not None:
            last = alert_cooldown.get(f"alert_{symbol}", 0)
            if now - last < 300:
                return None

        # Step 1: Tính pump %
        pump_pct, pump_low, pump_high = self._detect_pump_move(df_1m)
        alert_threshold = self.cfg["PUMP_PRICE_RISE_PCT"] * 0.5  # 50% ngưỡng SHORT
        if pump_pct < alert_threshold:
            return None

        # Giá phải còn trong 20% đỉnh pump — nếu đã rớt xa thì là pump cũ
        current_price_check = df_1m["close"].iloc[-1]
        if pump_high > 0 and current_price_check < pump_high * 0.80:
            return None

        # Step 2: Kiểm tra volume surge
        vol = df_1m["volume"]
        vol_ma20 = calculate_volume_ma(vol, 20).iloc[-1]
        vol_cur  = vol.iloc[-1]
        vol_ratio = vol_cur / vol_ma20 if vol_ma20 > 0 else 1.0

        # Volume phải tăng (ít nhất 1.3x) để xác nhận đây là pump thật
        if vol_ratio < 1.3:
            return None

        # Step 2b: Giá phải đang TĂNG — không alert khi coin đang giảm
        # Check close của 3 nến gần nhất phải cao hơn 3 nến trước đó
        if len(df_1m) >= 6:
            recent_avg = df_1m["close"].iloc[-3:].mean()
            prev_avg   = df_1m["close"].iloc[-6:-3].mean()
            if recent_avg <= prev_avg:
                return None  # Giá đang đi xuống → không phải pump alert

        # Step 3: Tính score pump top để biết còn xa ngưỡng SHORT bao nhiêu
        score, signals = self._score_pump_top(df_1m, df_15m, pump_high)

        # Nếu đã đủ điểm SHORT → không alert (sẽ được xử lý bởi analyze())
        min_score = self.cfg["PUMP_TOP_MIN_SCORE"]
        rsi = calculate_rsi(df_1m["close"], 14).iloc[-1]
        current_price = df_1m["close"].iloc[-1]

        # Đã là đỉnh rồi → không alert ở đây nữa
        is_top = (
            score >= min_score
            and rsi >= 72
            and pump_pct >= 20.0
            and current_price >= pump_high * 0.97
        )
        if is_top:
            return None

        # Step 4: Xây dựng reason text
        reasons = []
        if pump_pct >= self.cfg["PUMP_PRICE_RISE_PCT"]:
            reasons.append(f"Pump +{pump_pct:.1f}% (đủ ngưỡng, chờ đỉnh)")
        else:
            reasons.append(f"Pump +{pump_pct:.1f}% (chưa tới {self.cfg['PUMP_PRICE_RISE_PCT']:.0f}%)")

        if score < min_score:
            reasons.append(f"Score {score}/{min_score} (cần thêm {min_score - score}đ)")
        if rsi < 72:
            reasons.append(f"RSI {rsi:.0f} (cần ≥72 để SHORT)")

        reason_str = " | ".join(reasons) if reasons else f"Pump +{pump_pct:.1f}% | Vol {vol_ratio:.1f}×"

        # Ghi nhận cooldown
        if alert_cooldown is not None:
            alert_cooldown[f"alert_{symbol}"] = now

        return PumpAlertSignal(
            symbol      = symbol,
            pump_pct    = round(pump_pct, 2),
            price       = round(current_price, 8),
            rsi         = round(rsi, 1),
            volume_ratio= round(vol_ratio, 2),
            score       = min(score, 100),
            reason      = reason_str,
            timestamp   = now,
        )

    def analyze(self,
                symbol:  str,
                df_1m:   pd.DataFrame,
                df_15m:  Optional[pd.DataFrame] = None,
                ob_tracker = None,
                ws_high_override: float = 0.0) -> Optional[PumpSignal]:
        """
        Phân tích coin có đang ở đỉnh pump không.

        Args:
            symbol:           ví dụ "BANKUSDT"
            df_1m:            DataFrame 1m (tối thiểu 50 nến)
            df_15m:           DataFrame 15m (tùy chọn)
            ob_tracker:       OrderBookTracker (tùy chọn)
            ws_high_override: đỉnh realtime từ WS — chính xác hơn klines
        """
        if df_1m is None or len(df_1m) < 30:
            return None

        # Cooldown — không spam cùng coin
        now = time.time()
        if now - self._cooldown.get(symbol, 0) < self.cfg["PUMP_SIGNAL_COOLDOWN_S"]:
            return None

        # Step 1: Có đang pump không?
        pump_pct, pump_low, pump_high = self._detect_pump_move(df_1m)

        # Ưu tiên đỉnh WS realtime nếu cao hơn đỉnh từ klines
        # WS cập nhật mỗi giây → chính xác hơn klines 1m
        if ws_high_override > pump_high:
            pump_high = ws_high_override
            # Tính lại pump_pct với đỉnh WS
            if pump_low > 0:
                pump_pct = round((pump_high - pump_low) / pump_low * 100, 2)

        if pump_pct < self.cfg["PUMP_PRICE_RISE_PCT"]:
            return None

        # Giá phải còn trong 20% của đỉnh pump
        # Nếu đã rớt xa khỏi đỉnh → đây là pump CŨ, không phải đang pump
        current_price = df_1m["close"].iloc[-1]   # define sớm, dùng cho check bên dưới
        if pump_high > 0 and current_price < pump_high * 0.80:
            return None

        logger.info(f"[PumpDetector] {symbol}: pump +{pump_pct:.1f}% | checking top...")

        # Step 2: Chấm điểm đỉnh
        score, signals = self._score_pump_top(df_1m, df_15m, pump_high)

        # Bonus: Order book pressure — ask wall đang đè = xác nhận đỉnh mạnh hơn
        ob_score = 0
        if ob_tracker is not None:
            try:
                snap = ob_tracker.get_snapshot(symbol)
                if snap and snap.imbalance_score >= 55:
                    ob_score = 20  # +20đ nếu order book confirm xả
                    signals.append(f"📊 OB: ask áp đảo {snap.ask_dominance:.0%} wall={snap.wall_ratio:.1f}×")
                elif snap and snap.ask_dominance >= 0.58:
                    ob_score = 10
                    signals.append(f"📊 OB: ask cao {snap.ask_dominance:.0%}")
            except Exception:
                pass
        score += ob_score

        # Step 3: Tính entry / SL / TP
        current_price = df_1m["close"].iloc[-1]
        atr     = calculate_atr(df_1m["high"], df_1m["low"], df_1m["close"], 14).iloc[-1]
        rsi     = calculate_rsi(df_1m["close"], 14).iloc[-1]
        vol_ma  = calculate_volume_ma(df_1m["volume"], 20).iloc[-1]
        vol_ratio = df_1m["volume"].iloc[-1] / vol_ma if vol_ma > 0 else 1.0

        # ── Xác nhận đảo chiều — phải check trước khi quyết định entry type ──
        reversal_confirmed = self._confirm_reversal(df_1m, pump_high)

        # ── Entry price + type trước — cần để tính TP chính xác ────
        near_top_pct   = (pump_high - current_price) / pump_high * 100 if pump_high > 0 else 99
        use_limit_top  = near_top_pct <= 1.0
        use_market     = reversal_confirmed and near_top_pct > 1.0

        if use_limit_top:
            final_entry      = round(pump_high * 0.995, 8)
            final_entry_type = "LIMIT"
        else:
            final_entry      = round(current_price, 8)
            final_entry_type = "MARKET"

        # ── SL dùng ATR nhưng có hard cap ───────────────────────────
        # - Tối thiểu 1.5% từ đỉnh (để không bị giật nhỏ xuyên thủng)
        # - Tối đa 3.0% từ đỉnh (hard cap — nếu xuyên qua 3% là pump vẫn còn sức)
        # - Dùng ATR×1.5 làm base, rồi clamp vào [1.5%, 3.0%]
        atr_pct       = (atr / pump_high * 100) if pump_high > 0 else 1.5
        sl_buffer_pct = max(1.5, min(atr_pct * 1.5, 3.0))   # clamp [1.5%, 3.0%]
        sl_price      = pump_high * (1 + sl_buffer_pct / 100)

        # ── TP: Fibonacci retracement từ ENTRY → đáy pump ───────────
        # Đúng hơn: entry là đỉnh, tp xuống theo fib từ đỉnh xuống đáy
        tp1_price = final_entry - (final_entry - pump_low) * 0.382   # 38.2% fib
        tp2_price = final_entry - (final_entry - pump_low) * 0.618   # 61.8% fib

        # Validate RR tối thiểu 1.5 — nếu không đủ thì không vào
        risk   = abs(sl_price - final_entry)
        reward = abs(final_entry - tp1_price)
        rr     = reward / risk if risk > 0 else 0
        if rr < 1.5:
            logger.info(f"[PumpDetector] {symbol}: RR={rr:.1f} < 1.5, skip")
            return None

        is_top = (
            score >= self.cfg["PUMP_TOP_MIN_SCORE"]
            and rsi >= 72
            and pump_pct >= 20.0
            and (use_limit_top or use_market)  # LIMIT gần đỉnh HOẶC MARKET sau xác nhận
        )

        if is_top:
            self._cooldown[symbol] = now
            logger.info(
                f"[PumpDetector] {symbol}: TOP CONFIRMED score={score} "
                f"entry={final_entry_type}@{final_entry:.6g} "
                f"SL={sl_price:.6g} (+{sl_buffer_pct:.1f}%) RR=1:{rr:.1f} | {signals}"
            )

        return PumpSignal(
            symbol       = symbol,
            is_pump_top  = is_top,
            score        = min(score, 100),
            pump_pct     = pump_pct,
            signals      = signals,
            entry_price  = final_entry,
            entry_type   = final_entry_type,
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

    # ── Xác nhận đảo chiều thật (BẮT BUỘC trước khi SHORT) ──
    def _confirm_reversal(self, df: pd.DataFrame, pump_high: float) -> bool:
        """
        Kiểm tra giá đã thật sự đảo chiều chưa.
        Cần ĐỦ 2 trong 4 điều kiện:

          1. Lower high liên tiếp: high[-1] < high[-2] < high[-3]
          2. Close xuống >= 1.5% từ đỉnh pump
          3. Nến đỏ to (thân >= 60% range) trong 2 nến gần nhất
          4. Volume xác nhận: vol nến đỏ >= 1.2× vol nến xanh liền trước
             → có người thật sự xả, không chỉ thiếu người mua
        """
        if len(df) < 5:
            return False

        conditions_met = 0

        # 1. Lower high 3 nến liên tiếp
        h1 = df["high"].iloc[-1]
        h2 = df["high"].iloc[-2]
        h3 = df["high"].iloc[-3]
        if h1 < h2 and h2 < h3:
            conditions_met += 1

        # 2. Close đã xuống ít nhất 1.5% từ đỉnh pump
        close_now = df["close"].iloc[-1]
        if pump_high > 0 and close_now <= pump_high * 0.985:
            conditions_met += 1

        # 3. Nến đỏ to trong 2 nến gần nhất
        for i in [-1, -2]:
            row  = df.iloc[i]
            o, h, l, c = row["open"], row["high"], row["low"], row["close"]
            body = o - c
            rng  = h - l
            if rng > 0 and body >= rng * 0.6 and c < o:
                conditions_met += 1
                break

        # 4. Volume xác nhận xả: nến đỏ gần nhất có vol >= 1.2× nến xanh liền trước
        for i in range(-1, -4, -1):
            row = df.iloc[i]
            if row["close"] < row["open"]:          # nến đỏ
                prev = df.iloc[i - 1]
                if prev["close"] > prev["open"]:    # nến xanh liền trước
                    if row["volume"] >= prev["volume"] * 1.2:
                        conditions_met += 1
                break  # chỉ tìm nến đỏ gần nhất

        return conditions_met >= 2

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
# SCANNER HELPERS — gọi từ pump_scan_engine trong bot.py
# ─────────────────────────────────────────────────────────────

# Shared cooldown dict cho pump alerts — tránh spam
_pump_alert_cooldown: dict = {}


def scan_for_pump_alerts(exchange,
                          symbols: List[str],
                          config,
                          notifier=None) -> List[PumpAlertSignal]:
    """
    Quét danh sách coin, tìm coin đang PUMP LÊN nhưng chưa đủ điều kiện SHORT.
    Gửi Telegram thông báo để trader tự quyết định vào LONG hay chờ SHORT.
    Trả về danh sách PumpAlertSignal.
    """
    detector = PumpDetector(config)
    alerts   = []

    for symbol in symbols:
        try:
            klines_1m  = exchange.get_klines(symbol, "1m",  limit=200)
            klines_15m = exchange.get_klines(symbol, "15m", limit=50)
            df_1m      = _to_df(klines_1m)
            df_15m     = _to_df(klines_15m)

            alert = detector.check_pump_rising(symbol, df_1m, df_15m,
                                               alert_cooldown=_pump_alert_cooldown)
            if alert is None:
                continue

            logger.info(
                f"[PumpAlert] {symbol}: +{alert.pump_pct:.1f}% "
                f"score={alert.score} rsi={alert.rsi:.0f} vol={alert.volume_ratio:.1f}×"
            )
            alerts.append(alert)

            if notifier:
                try:
                    notifier.telegram.send(alert.to_telegram())
                    logger.info(f"[PumpAlert] Alert sent: {symbol}")
                except Exception as e:
                    logger.warning(f"[PumpAlert] Telegram failed: {e}")

        except Exception as e:
            logger.debug(f"[PumpAlert] {symbol} error: {e}")

    return alerts


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
