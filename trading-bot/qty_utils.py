# ============================================================
# QTY UTILS — Cache stepSize + round qty chính xác từ Binance
# Dùng chung cho telegram_commands, web_dashboard, bot.py
# ============================================================
import math
import logging
import threading
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

# Cache stepSize per symbol — tránh gọi API nhiều lần
_step_cache: dict = {}
_cache_lock = threading.Lock()


def get_step_info(exchange, symbol: str) -> Tuple[float, float, int, float]:
    """
    Lấy (step, max_qty, decimals, min_notional) từ Binance.
    Cache 1 giờ để tránh spam API.
    Returns: (step, max_qty, decimals, min_notional)
    """
    import time
    sym = symbol.upper()

    with _cache_lock:
        cached = _step_cache.get(sym)
        if cached and time.time() - cached["ts"] < 3600:
            return cached["step"], cached["max_qty"], cached["decimals"], cached["min_notional"]

    try:
        step, max_qty, decimals, min_notional = exchange.get_qty_precision(sym)
        with _cache_lock:
            _step_cache[sym] = {
                "step": step, "max_qty": max_qty,
                "decimals": decimals, "min_notional": min_notional,
                "ts": time.time()
            }
        return step, max_qty, decimals, min_notional
    except Exception as e:
        logger.debug(f"[QtyUtils] get_step_info {sym}: {e}")
        return 1.0, 100000.0, 0, 5.0


def round_qty(qty: float, step: float, decimals: int) -> float:
    """Round qty xuống theo stepSize."""
    if step >= 1:
        return int(qty // step) * int(step)
    else:
        return round(int(qty / step) * step, decimals)


def calc_qty_precise(exchange, symbol: str,
                     usdt: float, leverage: int,
                     entry_price: float) -> Tuple[float, str]:
    """
    Tính qty chính xác dùng stepSize thật từ Binance.

    Returns: (qty, info_str)
    """
    step, max_qty, decimals, min_notional = get_step_info(exchange, symbol)

    raw_qty = (usdt * leverage) / entry_price if entry_price > 0 else 0.0
    qty = round_qty(raw_qty, step, decimals)

    # Đảm bảo notional >= min_notional
    min_qty_notional = min_notional / entry_price if entry_price > 0 else step
    if step >= 1:
        min_qty_notional = max(int(step), int(math.ceil(min_qty_notional / step)) * int(step))
    else:
        min_qty_notional = max(step, round(math.ceil(min_qty_notional / step) * step, decimals))

    qty = max(qty, min_qty_notional)
    qty = min(qty, max_qty)

    info = (f"step={step} dec={decimals} "
            f"notional=${qty*entry_price:.2f} min_n=${min_notional}")
    logger.debug(f"[QtyUtils] {symbol}: raw={raw_qty:.6f} → qty={qty} | {info}")
    return qty, info
