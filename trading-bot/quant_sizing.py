# ============================================================
# QUANT SIZING — Kelly Criterion + Risk-adjusted position sizing
# ============================================================
import logging
import math
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


def calc_kelly_fraction(trade_log: List[Dict],
                        lookback: int = 50,
                        max_kelly: float = 0.25) -> float:
    """
    Tính Kelly fraction từ lịch sử giao dịch thực tế.

    Kelly formula: f* = (bp - q) / b
        b = avg_win / avg_loss  (reward/risk ratio thực tế)
        p = win rate
        q = 1 - p

    Áp dụng Half-Kelly để giảm variance: f = f* * 0.5
    Cap tối đa max_kelly (default 25%) để bảo vệ vốn.

    Returns: fraction (0.0 - max_kelly)
    """
    closed = [t for t in trade_log
              if t.get("status") == "CLOSED"
              and abs(t.get("pnl_usdt", 0)) > 0.001]

    if len(closed) < 10:
        # Chưa đủ data → dùng fixed sizing theo MAX_ORDER_USDT
        logger.debug(f"[Kelly] Chỉ có {len(closed)} trades, dùng fixed sizing")
        return None  # None = dùng MAX_ORDER_USDT fallback

    # Chỉ lấy lookback trades gần nhất
    recent = sorted(closed, key=lambda t: t.get("time", ""), reverse=True)[:lookback]

    wins   = [t["pnl_usdt"] for t in recent if t.get("pnl_usdt", 0) > 0]
    losses = [abs(t["pnl_usdt"]) for t in recent if t.get("pnl_usdt", 0) < 0]

    if not wins or not losses:
        return 0.01

    p = len(wins) / len(recent)        # win rate
    q = 1 - p                          # loss rate
    avg_win  = sum(wins)  / len(wins)
    avg_loss = sum(losses) / len(losses)
    b = avg_win / avg_loss if avg_loss > 0 else 1.0  # reward/risk

    # Kelly formula
    kelly = (b * p - q) / b if b > 0 else 0.0

    # Half-Kelly để giảm variance
    half_kelly = kelly * 0.5

    # Clamp
    result = max(0.005, min(half_kelly, max_kelly))

    logger.info(
        f"[Kelly] p={p:.1%} b={b:.2f} kelly={kelly:.3f} "
        f"half={half_kelly:.3f} → use={result:.3f} "
        f"(from {len(recent)} trades)"
    )
    return result


def calc_kelly_qty(balance: float,
                   entry_price: float,
                   sl_price: float,
                   trade_log: List[Dict],
                   max_usdt: float = 30.0,
                   leverage: int = 20,
                   lookback: int = 50) -> float:
    """
    Tính qty sử dụng Kelly sizing.

    Size = Kelly_fraction × Balance / risk_per_unit
    risk_per_unit = |entry - sl| (USD per unit)

    Caps:
    - Notional ≤ max_usdt × leverage
    - Minimum notional $5
    """
    kelly = calc_kelly_fraction(trade_log, lookback)

    risk_per_unit = abs(entry_price - sl_price)
    if risk_per_unit <= 0:
        risk_per_unit = entry_price * 0.02  # fallback 2%

    # Nếu kelly=None (chưa đủ lịch sử) → dùng max_usdt cố định
    if kelly is None:
        qty = (max_usdt * leverage) / entry_price
        logger.info(f"[Kelly] Chưa đủ lịch sử → fixed sizing: notional=${qty*entry_price:.2f}")
    else:
        # Kelly qty: risk fraction of balance / risk per unit
        risk_usd = balance * kelly
        qty = risk_usd / risk_per_unit
        logger.info(
            f"[Kelly] balance=${balance:.2f} kelly={kelly:.3f} "
            f"risk_usd=${risk_usd:.2f} risk/unit=${risk_per_unit:.4f} "
            f"→ qty={qty:.4f} notional=${qty*entry_price:.2f}"
        )

    # Cap theo max_usdt × leverage
    max_notional = max_usdt * leverage
    qty_cap = max_notional / entry_price
    qty = min(qty, qty_cap)

    # Minimum $5 notional
    min_qty = 5.0 / entry_price
    qty = max(qty, min_qty)
    return round(qty, 6)


def get_sizing_stats(trade_log: List[Dict], lookback: int = 50) -> Dict:
    """
    Trả về thống kê sizing để hiển thị trên dashboard/telegram.
    """
    closed = [t for t in trade_log
              if t.get("status") == "CLOSED"
              and abs(t.get("pnl_usdt", 0)) > 0.001]

    if not closed:
        return {"kelly": 0.01, "win_rate": 0, "avg_rr": 0, "trades": 0, "edge": 0}

    recent = sorted(closed, key=lambda t: t.get("time", ""), reverse=True)[:lookback]
    wins   = [t["pnl_usdt"] for t in recent if t.get("pnl_usdt", 0) > 0]
    losses = [abs(t["pnl_usdt"]) for t in recent if t.get("pnl_usdt", 0) < 0]

    p        = len(wins) / len(recent) if recent else 0
    avg_win  = sum(wins)  / len(wins)  if wins  else 0
    avg_loss = sum(losses)/ len(losses)if losses else 1
    b        = avg_win / avg_loss if avg_loss > 0 else 0
    kelly    = calc_kelly_fraction(recent, lookback)

    # Expected value per trade
    edge = p * avg_win - (1 - p) * avg_loss

    return {
        "kelly":    round(kelly, 4),
        "win_rate": round(p * 100, 1),
        "avg_rr":   round(b, 2),
        "trades":   len(recent),
        "edge":     round(edge, 2),
        "avg_win":  round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
    }
