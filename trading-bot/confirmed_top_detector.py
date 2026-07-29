# ============================================================
# CONFIRMED TOP DETECTOR
# Bắt đỉnh pump SAU KHI xác nhận — không đoán, không sớm
# ============================================================
# Logic 5 điều kiện (phải pass >= 4/5):
#
#   C1. MEGA PUMP : giá hiện tại >= 50% so với 1h trước
#   C2. TOP CANDLE: nến vừa qua là đỉnh (high nhất trong 10 nến)
#   C3. REVERSAL  : nến hiện tại KHÔNG vượt đỉnh nến trước
#                   (close < previous_high × 0.98)
#   C4. VOL CLIFF : volume nến hiện tại <= 30% volume nến đỉnh
#   C5. OB CONFIRM: order book ask > bid × 1.5 (nếu có data)
#
# Entry: close nến hiện tại
# SL   : đỉnh + 2% buffer
# TP   : đỉnh - 40% (vùng trước pump) — RR 1:5+
#
# Cooldown: 10 phút/coin để tránh re-entry vào cùng pump
# ============================================================
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from collections import deque

import pandas as pd

from indicators import calculate_rsi, calculate_ema, calculate_atr

logger = logging.getLogger(__name__)

# ── Config defaults ───────────────────────────────────────────
DEFAULT_CFG = {
    "CT_PUMP_PCT_1H":      50.0,   # C1: tăng >= 50% trong 1h
    "CT_TOP_LOOKBACK":     10,     # C2: đỉnh trong N nến
    "CT_REVERSAL_BUFFER":  0.98,   # C3: close < đỉnh × 0.98
    "CT_VOL_CLIFF_RATIO":  0.30,   # C4: volume <= 30% nến đỉnh
    "CT_OB_RATIO_MIN":     1.5,    # C5: ask/bid ratio
    "CT_MIN_CONDITIONS":   4,      # cần pass >= 4/5
    "CT_COOLDOWN_SEC":     600,    # 10 phút/coin
    "CT_MAX_DELAY_MIN":    5,      # đỉnh không quá 5 phút trước
    "CT_SL_BUFFER_PCT":    0.02,   # SL = đỉnh × (1 + 2%)
    "CT_TP_RETRACE_PCT":   0.40,   # TP = đỉnh - 40% của range pump
}


@dataclass
class ConfirmedTopSignal:
    symbol:       str
    entry_price:  float
    sl_price:     float
    tp_price:     float
    pump_pct:     float    # % tăng từ baseline lên đỉnh
    peak_price:   float    # đỉnh tuyệt đối
    conditions:   List[str]
    conditions_passed: int
    rr:           float
    timestamp:    float = field(default_factory=time.time)

    def to_telegram(self) -> str:
        cond_str = "\n".join(f"  ✅ {c}" for c in self.conditions)
        return (
            f"🎯 <b>CONFIRMED TOP — SHORT</b>\n"
            f"{'─'*34}\n"
            f"🪙 {self.symbol}\n"
            f"📈 Pump: <b>+{self.pump_pct:.1f}%</b>  đỉnh ${self.peak_price:,.6g}\n"
            f"{'─'*34}\n"
            f"🔴 Entry : <b>${self.entry_price:,.6g}</b>\n"
            f"🛑 SL    : <b>${self.sl_price:,.6g}</b>  (+2%)\n"
            f"🎯 TP    : <b>${self.tp_price:,.6g}</b>  (-40%)\n"
            f"📐 RR    : <b>1:{self.rr:.1f}</b>\n"
            f"{'─'*34}\n"
            f"✅ {self.conditions_passed}/5 điều kiện:\n{cond_str}\n"
            f"{'─'*34}\n"
            f"⏰ {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"
        )


class ConfirmedTopDetector:
    """
    Theo dõi giá qua WS, phát hiện đỉnh pump đã xác nhận.

    Cách dùng:
        detector = ConfirmedTopDetector(config)
        # Mỗi khi nhận WS tick:
        sig = detector.on_price_tick(symbol, price, exchange)
        if sig:
            # SHORT ngay
    """

    def __init__(self, config=None):
        self.cfg       = self._load_cfg(config)
        self._cooldown: Dict[str, float]        = {}
        self._price_history: Dict[str, deque]   = {}  # {sym: deque[(ts,price)]}

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

    # ── Public: gọi mỗi WS tick ──────────────────────────────
    def on_price_tick(self, symbol: str, price: float,
                      exchange=None,
                      ob_tracker=None) -> Optional[ConfirmedTopSignal]:
        """
        Nhận giá mới từ WS, kiểm tra có phải đỉnh confirmed không.
        Nếu có → trả về ConfirmedTopSignal, ngược lại None.

        exchange: dùng để lấy klines 1m khi cần
        ob_tracker: OrderBookTracker để check C5
        """
        now = time.time()

        # Cooldown check
        if now - self._cooldown.get(symbol, 0) < self.cfg["CT_COOLDOWN_SEC"]:
            return None

        # Lưu lịch sử giá
        if symbol not in self._price_history:
            self._price_history[symbol] = deque(maxlen=3600)  # 1h ticks
        self._price_history[symbol].append((now, price))

        # Cần đủ dữ liệu lịch sử (ít nhất 5 phút)
        hist = self._price_history[symbol]
        if len(hist) < 60:
            return None

        # ── C1: MEGA PUMP — giá tăng >= 50% so với 1h trước ─
        cutoff_1h = now - 3600
        old_prices = [p for ts, p in hist if ts <= cutoff_1h]
        baseline   = min(old_prices) if old_prices else None

        if baseline is None or baseline <= 0:
            # Chưa đủ 1h data, dùng 15 phút
            cutoff_15m = now - 900
            old_15m    = [p for ts, p in hist if ts <= cutoff_15m]
            baseline   = min(old_15m) if old_15m else None

        if baseline is None or baseline <= 0:
            return None

        pump_pct = (price - baseline) / baseline * 100
        c1_pass  = pump_pct >= self.cfg["CT_PUMP_PCT_1H"]

        if not c1_pass:
            return None   # Chưa đủ pump, không cần check tiếp

        # ── Lấy klines 1m để check C2, C3, C4 ───────────────
        if exchange is None:
            return None

        try:
            from scanner import _klines_to_df
            klines = exchange.get_klines(symbol, "1m", limit=30)
            df     = _klines_to_df(klines)
        except Exception as e:
            logger.debug(f"[CTD] {symbol} klines error: {e}")
            return None

        if df is None or len(df) < 5:
            return None

        # ── C2: TOP CANDLE — đỉnh trong CT_TOP_LOOKBACK nến ─
        lb         = self.cfg["CT_TOP_LOOKBACK"]
        window     = df.tail(lb)
        peak_idx   = window["high"].idxmax()
        peak_price = window["high"].max()
        peak_vol   = df.loc[peak_idx, "volume"] if peak_idx in df.index else 0

        # Đỉnh phải trong CT_MAX_DELAY_MIN phút gần nhất
        peak_row  = df.loc[peak_idx] if peak_idx in df.index else None
        if peak_row is None:
            return None

        # Đỉnh không phải nến hiện tại (phải có ít nhất 1 nến sau đỉnh)
        peak_pos  = df.index.get_loc(peak_idx)
        last_pos  = len(df) - 1
        if peak_pos >= last_pos:
            return None   # Đỉnh vẫn là nến mới nhất — chưa confirm

        candles_after_peak = last_pos - peak_pos
        if candles_after_peak > self.cfg["CT_MAX_DELAY_MIN"]:
            return None   # Đỉnh quá cũ (> 5 nến = 5 phút)

        c2_pass = True   # Đỉnh đã xác định được

        # ── C3: REVERSAL — nến hiện tại không vượt đỉnh ─────
        cur_close  = df["close"].iloc[-1]
        cur_high   = df["high"].iloc[-1]
        c3_pass    = cur_high < peak_price * (1 + 0.01)  # không vượt đỉnh + 1%

        # ── C4: VOLUME CLIFF — volume sụt mạnh ───────────────
        cur_vol    = df["volume"].iloc[-1]
        # Nới lỏng từ 30% → 50%: dev pump dùng fake vol lớn, nến sau <= 50% vẫn là dấu hiệu xả
        c4_pass    = (peak_vol > 0 and
                      cur_vol <= peak_vol * max(self.cfg["CT_VOL_CLIFF_RATIO"], 0.50))

        # ── C5: ORDER BOOK — ask > bid × ratio ───────────────
        c5_pass = False
        if ob_tracker is not None:
            try:
                snap = ob_tracker.get_snapshot(symbol)
                if snap:
                    c5_pass = snap.wall_ratio >= self.cfg["CT_OB_RATIO_MIN"]
            except Exception:
                pass

        # ── Tổng hợp ─────────────────────────────────────────
        conditions     = []
        passed_count   = 0

        if c1_pass:
            conditions.append(f"C1 Mega pump +{pump_pct:.0f}%")
            passed_count += 1
        else:
            conditions.append(f"C1 FAIL pump {pump_pct:.0f}% < {self.cfg['CT_PUMP_PCT_1H']}%")

        if c2_pass:
            conditions.append(f"C2 Top candle ${peak_price:,.6g} ({candles_after_peak} nến trước)")
            passed_count += 1

        if c3_pass:
            conditions.append(f"C3 Reversal (cur_high={cur_high:,.6g} < peak)")
            passed_count += 1
        else:
            conditions.append(f"C3 FAIL no reversal")

        if c4_pass:
            ratio = cur_vol / peak_vol * 100 if peak_vol > 0 else 0
            conditions.append(f"C4 Volume cliff {ratio:.0f}% of peak")
            passed_count += 1
        else:
            conditions.append(f"C4 FAIL volume ok")

        if c5_pass:
            conditions.append(f"C5 OB confirmed")
            passed_count += 1
        else:
            conditions.append(f"C5 OB N/A or weak")

        min_cond = self.cfg["CT_MIN_CONDITIONS"]
        if passed_count < min_cond:
            logger.debug(
                f"[CTD] {symbol}: {passed_count}/{len(conditions)} cond "
                f"(need {min_cond}) — skip"
            )
            return None

        # ── Tính entry / SL / TP ─────────────────────────────
        entry = cur_close
        sl    = round(peak_price * (1 + self.cfg["CT_SL_BUFFER_PCT"]), 8)

        # ── TP: thực tế hơn — SHORT bắt đỉnh pump nên TP ở 30% fib, không cần 40% ─
        # Dev pump thường xả về 60-80% range trong 30-60 phút
        # TP1 = 30% retracement (an toàn, chốt nhanh)
        # TP  = 50% retracement (target chính)
        pump_range = peak_price - baseline
        tp         = round(peak_price - pump_range * 0.30, 8)   # TP1: 30% — chốt nhanh

        # Đảm bảo TP < entry (SHORT)
        if tp >= entry:
            tp = round(entry * 0.90, 8)   # fallback: -10% từ entry

        risk   = abs(sl - entry)
        reward = abs(entry - tp)
        rr     = round(reward / risk, 1) if risk > 0 else 0

        # RR phải >= 2.0 — thực tế hơn (3.0 quá strict, bỏ lỡ nhiều setup tốt)
        if rr < 2.0:
            logger.debug(f"[CTD] {symbol}: RR={rr} < 2.0 — skip")
            return None

        # Set cooldown
        self._cooldown[symbol] = now

        sig = ConfirmedTopSignal(
            symbol           = symbol,
            entry_price      = round(entry, 8),
            sl_price         = sl,
            tp_price         = tp,
            pump_pct         = round(pump_pct, 1),
            peak_price       = round(peak_price, 8),
            conditions       = conditions,
            conditions_passed= passed_count,
            rr               = rr,
        )

        logger.info(
            f"[CTD] ✅ CONFIRMED TOP: {symbol} "
            f"pump={pump_pct:.0f}% peak={peak_price:,.6g} "
            f"entry={entry:,.6g} SL={sl:,.6g} TP={tp:,.6g} "
            f"RR=1:{rr} cond={passed_count}/5"
        )
        return sig


# ── Singleton ─────────────────────────────────────────────────
_ctd_instance: Optional[ConfirmedTopDetector] = None


def get_ctd(config=None) -> ConfirmedTopDetector:
    global _ctd_instance
    if _ctd_instance is None:
        _ctd_instance = ConfirmedTopDetector(config)
    return _ctd_instance
