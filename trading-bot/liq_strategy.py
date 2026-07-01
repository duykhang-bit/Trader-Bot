# ============================================================
# LIQUIDATION STRATEGY — 2-Order Split Entry
#
# Logic:
#  SHORT setup (giá pump lên vùng liq LONG phía trên):
#    - Lệnh 1 (size nhỏ ~35%): vào ngay khi giá chạm vùng liq đầu tiên
#    - Lệnh 2 (size lớn ~65%): vào khi giá đẩy tiếp lên vùng liq thứ 2
#    - SL: cao hơn đỉnh vùng liq lệnh 2 + 2%
#    - TP: vùng liq SHORT lớn nhất phía dưới (nơi giá sẽ dump xuống)
#
#  LONG setup (giá dump xuống vùng liq SHORT phía dưới): ngược lại
# ============================================================
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

from liquidation_tracker import LiquidationTracker

logger = logging.getLogger(__name__)


@dataclass
class LiqSetup:
    """Kết quả phân tích: 1 setup trade hoàn chỉnh."""
    symbol:        str
    direction:     str          # "SHORT" | "LONG"
    entry1:        float        # giá vào lệnh 1 (nhỏ hơn)
    entry2:        float        # giá vào lệnh 2 (lớn hơn)
    sl:            float        # stop loss
    tp:            float        # take profit
    liq1_usd:      float        # USD liquidation tại vùng entry1
    liq2_usd:      float        # USD liquidation tại vùng entry2
    tp_liq_usd:    float        # USD liquidation tại vùng TP
    confidence:    float        # 0-100 độ tin cậy
    reason:        str

    # size split
    size1_pct:     float = 0.35  # 35% tổng size vào lệnh 1
    size2_pct:     float = 0.65  # 65% tổng size vào lệnh 2


@dataclass
class SplitPosition:
    """Theo dõi trạng thái 2 lệnh đang mở."""
    symbol:       str
    direction:    str
    entry1:       float
    entry2:       float
    sl:           float
    tp:           float
    qty1:         float = 0.0
    qty2:         float = 0.0
    filled1:      bool  = False
    filled2:      bool  = False
    sl_placed:    bool  = False
    tp_placed:    bool  = False
    open_time:    float = field(default_factory=time.time)


class LiqStrategy:
    """
    Phân tích liquidation data → sinh setup 2 lệnh.
    """

    def __init__(self, tracker: LiquidationTracker, config):
        self.tracker = tracker
        self.config  = config

        # Tham số từ config (có default fallback)
        self.sl_buffer_pct    = getattr(config, "LIQ_SL_BUFFER_PCT",    0.02)   # SL cách vùng liq 2%
        self.entry_offset_pct = getattr(config, "LIQ_ENTRY_OFFSET_PCT", 0.001)  # offset giá entry 0.1%
        self.min_liq_usd      = getattr(config, "LIQ_MIN_USD",          100_000) # vùng liq tối thiểu $100k
        self.min_tp_liq_usd   = getattr(config, "LIQ_MIN_TP_USD",       200_000) # TP liq tối thiểu $200k
        self.entry2_min_gap   = getattr(config, "LIQ_ENTRY2_MIN_GAP",   0.005)  # entry2 cách entry1 ít nhất 0.5%

    # ── Public ────────────────────────────────────────────────

    def analyze(self, symbol: str, current_price: float) -> Optional[LiqSetup]:
        """
        Phân tích và trả về LiqSetup nếu có setup hợp lệ.
        Trả về None nếu không đủ điều kiện.
        Tôn trọng AI bias: chỉ SHORT khi AI nói SHORT/HOLD, chỉ LONG khi AI nói LONG/HOLD.
        """
        sym = symbol.upper()

        # Cần đủ data trước khi trade
        if not self.tracker.has_enough_data(sym, min_usd=self.min_liq_usd * 2):
            logger.debug(f"{sym}: chưa đủ liq data")
            return None

        # Đọc AI bias nếu có
        ai_bias = self._get_ai_bias(sym)

        # Thử SHORT setup trước
        short_setup = None
        long_setup  = None

        if ai_bias in ("SHORT", "HOLD", None):
            short_setup = self._check_short_setup(sym, current_price)

        if ai_bias in ("LONG", "HOLD", None):
            long_setup = self._check_long_setup(sym, current_price)

        # Chọn setup có confidence cao hơn
        setups = [s for s in [short_setup, long_setup] if s is not None]
        if not setups:
            return None

        # Nếu AI có bias rõ ràng (LONG/SHORT) → ưu tiên setup cùng hướng
        if ai_bias in ("LONG", "SHORT"):
            matching = [s for s in setups if s.direction == ai_bias]
            if matching:
                setups = matching  # Chỉ giữ setup cùng hướng AI

        best = max(setups, key=lambda s: s.confidence)
        logger.info(
            f"[LiqStrategy] {sym} {best.direction} confidence={best.confidence:.0f} "
            f"ai_bias={ai_bias} "
            f"entry1={best.entry1:.4f} entry2={best.entry2:.4f} "
            f"sl={best.sl:.4f} tp={best.tp:.4f}"
        )
        return best

    def _get_ai_bias(self, symbol: str) -> Optional[str]:
        """Đọc bias từ ai_bias.json (output của ai_analyzer.py)."""
        try:
            from ai_analyzer import load_bias
            biases = load_bias()
            return biases.get(symbol, None)
        except Exception:
            return None

    def calc_quantities(self,
                         setup: LiqSetup,
                         balance: float,
                         leverage: int
                         ) -> Tuple[float, float]:
        """
        Tính qty cho 2 lệnh dựa trên balance + risk config.
        Trả về (qty1, qty2).
        """
        risk_per_trade = getattr(self.config, "RISK_PER_TRADE", 0.01)
        max_usdt       = getattr(self.config, "MAX_ORDER_USDT",  15.0)

        # Tổng risk USD cho cả 2 lệnh
        total_risk = balance * risk_per_trade
        sl_dist    = abs(setup.entry1 - setup.sl)
        if sl_dist <= 0:
            sl_dist = setup.entry1 * 0.02  # fallback 2%

        # Tổng qty theo risk
        total_qty = total_risk / sl_dist

        # Hard cap: margin không vượt MAX_ORDER_USDT
        max_notional = max_usdt * leverage
        qty_by_cap   = max_notional / setup.entry1
        total_qty    = min(total_qty, qty_by_cap)

        # Notional tối thiểu $5 mỗi lệnh
        min_qty = 5.0 / setup.entry1

        qty1 = max(round(total_qty * setup.size1_pct, 3), min_qty)
        qty2 = max(round(total_qty * setup.size2_pct, 3), min_qty)

        return qty1, qty2

    # ── Internal ──────────────────────────────────────────────

    def _check_short_setup(self, symbol: str, price: float) -> Optional[LiqSetup]:
        """
        SHORT setup: tìm 2 vùng liq LONG phía TRÊN giá hiện tại.
        - entry1: vùng liq đầu tiên (gần nhất) phía trên
        - entry2: vùng liq thứ 2 (cao hơn entry1)
        - sl: cao hơn entry2 + sl_buffer_pct
        - tp: vùng liq SHORT lớn nhất phía DƯỚI (nơi giá sẽ dump tới)
        """
        sym = symbol.upper()
        with self.tracker._lock:
            buckets = dict(self.tracker._buckets.get(sym, {}))

        if not buckets:
            return None

        # Lấy tất cả vùng liq phía trên có đủ USD
        above = sorted(
            [(p, u) for p, u in buckets.items()
             if p > price and u >= self.min_liq_usd],
            key=lambda x: x[0]
        )
        if len(above) < 1:
            return None

        # entry1: vùng gần nhất phía trên
        entry1_price, liq1_usd = above[0]
        # Short vào ngay dưới vùng liq một chút (giá đang tiến tới)
        entry1 = entry1_price * (1 - self.entry_offset_pct)

        # entry2: vùng liq tiếp theo đủ xa hơn entry1
        entry2_price, liq2_usd = None, 0.0
        for p, u in above[1:]:
            if p >= entry1_price * (1 + self.entry2_min_gap) and u >= self.min_liq_usd:
                entry2_price, liq2_usd = p, u
                break

        if entry2_price is None:
            # Chỉ có 1 vùng liq → dùng entry1 + min_gap làm entry2
            entry2_price = entry1_price * (1 + self.entry2_min_gap * 2)
            liq2_usd = liq1_usd * 0.5  # ước tính

        entry2 = entry2_price * (1 - self.entry_offset_pct)

        # SL: cao hơn entry2_price + buffer
        sl = entry2_price * (1 + self.sl_buffer_pct)

        # TP: vùng liq SHORT (SELL bị liq) lớn nhất phía dưới
        # Vùng SHORT liq = nơi short sellers bị squeeze khi giá tăng
        # Khi giá pump lên thì sẽ dump xuống vùng liq này
        tp_price = self._find_tp_for_short(sym, price, buckets)
        if tp_price is None:
            # Fallback: TP = entry1 - (sl - entry2) * 2  (RR 1:2)
            tp_price = entry1 * (1 - (sl - entry2) / entry2 * 2)

        tp_liq_usd = buckets.get(self.tracker._price_to_bucket(tp_price), 0)

        # Validate RR tối thiểu 1:1.5
        risk  = sl - entry1
        reward = entry1 - tp_price
        if risk <= 0 or reward <= 0 or reward / risk < 1.5:
            logger.debug(f"{sym} SHORT RR={reward/risk:.2f} < 1.5, skip")
            return None

        # Confidence score
        confidence = self._calc_confidence(
            liq1_usd, liq2_usd, tp_liq_usd,
            reward / risk
        )
        if confidence < 40:
            return None

        return LiqSetup(
            symbol     = sym,
            direction  = "SHORT",
            entry1     = round(entry1, 4),
            entry2     = round(entry2, 4),
            sl         = round(sl, 4),
            tp         = round(tp_price, 4),
            liq1_usd   = liq1_usd,
            liq2_usd   = liq2_usd,
            tp_liq_usd = tp_liq_usd,
            confidence = confidence,
            reason     = (
                f"SHORT liq1=${liq1_usd/1e6:.2f}M@{entry1_price:.2f} "
                f"liq2=${liq2_usd/1e6:.2f}M@{entry2_price:.2f} "
                f"TP=${tp_liq_usd/1e6:.2f}M@{tp_price:.2f} "
                f"RR={reward/risk:.1f}"
            ),
        )

    def _check_long_setup(self, symbol: str, price: float) -> Optional[LiqSetup]:
        """
        LONG setup: tìm 2 vùng liq SHORT phía DƯỚI giá hiện tại.
        - entry1: vùng liq đầu tiên (gần nhất) phía dưới
        - entry2: vùng liq thứ 2 (thấp hơn entry1)
        - sl: thấp hơn entry2 - sl_buffer_pct
        - tp: vùng liq LONG lớn nhất phía TRÊN
        """
        sym = symbol.upper()
        with self.tracker._lock:
            buckets = dict(self.tracker._buckets.get(sym, {}))

        if not buckets:
            return None

        # Lấy tất cả vùng liq phía dưới
        below = sorted(
            [(p, u) for p, u in buckets.items()
             if p < price and u >= self.min_liq_usd],
            key=lambda x: x[0],
            reverse=True   # gần nhất trước
        )
        if len(below) < 1:
            return None

        entry1_price, liq1_usd = below[0]
        entry1 = entry1_price * (1 + self.entry_offset_pct)  # long ngay trên vùng liq

        entry2_price, liq2_usd = None, 0.0
        for p, u in below[1:]:
            if p <= entry1_price * (1 - self.entry2_min_gap) and u >= self.min_liq_usd:
                entry2_price, liq2_usd = p, u
                break

        if entry2_price is None:
            entry2_price = entry1_price * (1 - self.entry2_min_gap * 2)
            liq2_usd = liq1_usd * 0.5

        entry2 = entry2_price * (1 + self.entry_offset_pct)

        # SL: thấp hơn entry2_price - buffer
        sl = entry2_price * (1 - self.sl_buffer_pct)

        # TP: vùng liq LONG lớn nhất phía trên
        tp_price = self._find_tp_for_long(sym, price, buckets)
        if tp_price is None:
            tp_price = entry1 * (1 + (entry2 - sl) / entry2 * 2)

        tp_liq_usd = buckets.get(self.tracker._price_to_bucket(tp_price), 0)

        risk   = entry1 - sl
        reward = tp_price - entry1
        if risk <= 0 or reward <= 0 or reward / risk < 1.5:
            logger.debug(f"{sym} LONG RR={reward/risk:.2f} < 1.5, skip")
            return None

        confidence = self._calc_confidence(liq1_usd, liq2_usd, tp_liq_usd, reward / risk)
        if confidence < 40:
            return None

        return LiqSetup(
            symbol     = sym,
            direction  = "LONG",
            entry1     = round(entry1, 4),
            entry2     = round(entry2, 4),
            sl         = round(sl, 4),
            tp         = round(tp_price, 4),
            liq1_usd   = liq1_usd,
            liq2_usd   = liq2_usd,
            tp_liq_usd = tp_liq_usd,
            confidence = confidence,
            reason     = (
                f"LONG liq1=${liq1_usd/1e6:.2f}M@{entry1_price:.2f} "
                f"liq2=${liq2_usd/1e6:.2f}M@{entry2_price:.2f} "
                f"TP=${tp_liq_usd/1e6:.2f}M@{tp_price:.2f} "
                f"RR={reward/risk:.1f}"
            ),
        )

    def _find_tp_for_short(self, symbol: str, current_price: float,
                            buckets: dict) -> Optional[float]:
        """
        TP cho SHORT: vùng liq LỚN NHẤT phía dưới giá hiện tại.
        Đây là nơi giá có khả năng dump tới khi liq xong phía trên.
        """
        candidates = [
            (p, u) for p, u in buckets.items()
            if p < current_price and u >= self.min_tp_liq_usd
        ]
        if not candidates:
            # Thử với ngưỡng thấp hơn
            candidates = [
                (p, u) for p, u in buckets.items()
                if p < current_price and u >= self.min_liq_usd
            ]
        if not candidates:
            return None
        # Vùng có USD lớn nhất phía dưới
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    def _find_tp_for_long(self, symbol: str, current_price: float,
                           buckets: dict) -> Optional[float]:
        """
        TP cho LONG: vùng liq LỚN NHẤT phía trên giá hiện tại.
        """
        candidates = [
            (p, u) for p, u in buckets.items()
            if p > current_price and u >= self.min_tp_liq_usd
        ]
        if not candidates:
            candidates = [
                (p, u) for p, u in buckets.items()
                if p > current_price and u >= self.min_liq_usd
            ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    def _calc_confidence(self,
                          liq1_usd: float,
                          liq2_usd: float,
                          tp_liq_usd: float,
                          rr: float) -> float:
        """
        Tính confidence score 0-100.
        Càng nhiều tiền bị liq ở vùng entry + TP → confidence cao hơn.
        """
        score = 0.0

        # 1. Liq tại entry1 (30đ)
        if liq1_usd >= 5_000_000:    score += 30
        elif liq1_usd >= 1_000_000:  score += 22
        elif liq1_usd >= 500_000:    score += 15
        elif liq1_usd >= 100_000:    score += 8

        # 2. Liq tại entry2 (25đ)
        if liq2_usd >= 5_000_000:    score += 25
        elif liq2_usd >= 1_000_000:  score += 18
        elif liq2_usd >= 500_000:    score += 12
        elif liq2_usd >= 100_000:    score += 6

        # 3. Liq tại TP — TP càng "có thanh khoản" càng tốt (30đ)
        if tp_liq_usd >= 5_000_000:  score += 30
        elif tp_liq_usd >= 2_000_000: score += 22
        elif tp_liq_usd >= 500_000:  score += 14
        elif tp_liq_usd >= 200_000:  score += 8

        # 4. RR (15đ)
        if rr >= 3.0:    score += 15
        elif rr >= 2.0:  score += 10
        elif rr >= 1.5:  score += 6

        return min(score, 100.0)
