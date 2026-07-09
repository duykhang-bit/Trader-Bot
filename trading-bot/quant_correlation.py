# ============================================================
# QUANT CORRELATION — Tránh vào 2 coin cùng chiều cùng lúc
# ============================================================
import logging
import numpy as np
import pandas as pd
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Nhóm coin tương quan cao — không nên mở 2 lệnh cùng nhóm
CORR_GROUPS = {
    "BTC_FAMILY":  ["BTCUSDT"],
    "ETH_FAMILY":  ["ETHUSDT", "BNBUSDT"],
    "SOL_FAMILY":  ["SOLUSDT", "AVAXUSDT", "NEARUSDT", "APTUSDT", "SUIUSDT"],
    "XRP_FAMILY":  ["XRPUSDT", "XLMUSDT", "ADAUSDT"],
    "DEFI_FAMILY": ["LINKUSDT", "UNIUSDT", "AAVEUSDT"],
    "MEME_FAMILY": ["DOGEUSDT"],
}


def get_coin_group(symbol: str) -> Optional[str]:
    """Trả về nhóm tương quan của coin."""
    for group, coins in CORR_GROUPS.items():
        if symbol in coins:
            return group
    return None


def calc_price_correlation(df1: pd.DataFrame, df2: pd.DataFrame,
                            window: int = 20) -> float:
    """
    Tính Pearson correlation của returns giữa 2 coin.
    Returns: correlation (-1 đến 1)
    """
    try:
        r1 = df1["close"].pct_change().dropna().tail(window)
        r2 = df2["close"].pct_change().dropna().tail(window)
        min_len = min(len(r1), len(r2))
        if min_len < 5:
            return 0.0
        corr = float(np.corrcoef(r1.values[-min_len:], r2.values[-min_len:])[0, 1])
        return round(corr, 3)
    except Exception:
        return 0.0


def is_correlated_with_open(symbol: str,
                              signal: str,
                              open_positions: List[Dict],
                              df_map: Dict[str, pd.DataFrame] = None,
                              corr_threshold: float = 0.75) -> Tuple[bool, str]:
    """
    Kiểm tra xem coin mới có tương quan cao với position đang mở không.

    Logic:
    1. Cùng nhóm CORR_GROUPS → block (tương quan theo nghĩa business)
    2. Nếu có df_map → tính correlation thực tế, block nếu > threshold
       VÀ cùng chiều (LONG-LONG hoặc SHORT-SHORT)

    Returns:
        (is_correlated, reason)
    """
    if not open_positions:
        return False, ""

    symbol_group = get_coin_group(symbol)

    for pos in open_positions:
        pos_sym  = pos.get("symbol", "")
        pos_side = "LONG" if float(pos.get("positionAmt", 0)) > 0 else "SHORT"

        if pos_sym == symbol:
            continue  # same symbol — handled elsewhere

        # Check cùng nhóm
        pos_group = get_coin_group(pos_sym)
        if (symbol_group and pos_group
                and symbol_group == pos_group
                and signal == pos_side):
            reason = f"{symbol} và {pos_sym} cùng nhóm {symbol_group} ({signal})"
            logger.info(f"[Corr] BLOCK: {reason}")
            return True, reason

        # Check correlation thực tế nếu có df
        if df_map and symbol in df_map and pos_sym in df_map:
            corr = calc_price_correlation(df_map[symbol], df_map[pos_sym])
            if abs(corr) >= corr_threshold and signal == pos_side:
                reason = (f"{symbol} corr({pos_sym})={corr:.2f} ≥ {corr_threshold} "
                          f"cùng chiều {signal}")
                logger.info(f"[Corr] BLOCK: {reason}")
                return True, reason

    return False, ""


def get_portfolio_exposure(open_positions: List[Dict]) -> Dict:
    """
    Phân tích exposure hiện tại của portfolio.
    Returns: {
        "long_count": int,
        "short_count": int,
        "net_bias": "LONG"/"SHORT"/"BALANCED",
        "groups": {group: [symbols]}
    }
    """
    long_syms  = []
    short_syms = []
    groups: Dict[str, List] = {}

    for pos in open_positions:
        sym  = pos.get("symbol", "")
        side = "LONG" if float(pos.get("positionAmt", 0)) > 0 else "SHORT"
        if side == "LONG":
            long_syms.append(sym)
        else:
            short_syms.append(sym)

        grp = get_coin_group(sym)
        if grp:
            groups.setdefault(grp, []).append(f"{sym}:{side}")

    n_long  = len(long_syms)
    n_short = len(short_syms)
    if n_long > n_short + 1:
        bias = "LONG"
    elif n_short > n_long + 1:
        bias = "SHORT"
    else:
        bias = "BALANCED"

    return {
        "long_count":  n_long,
        "short_count": n_short,
        "net_bias":    bias,
        "groups":      groups,
        "long_syms":   long_syms,
        "short_syms":  short_syms,
    }
