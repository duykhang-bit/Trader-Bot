# ============================================================
# AUTO SL/TP — Phân tích chart đa khung + thanh khoản để đề xuất SL/TP
# ============================================================
import logging
import pandas as pd
import numpy as np
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)


def suggest_sltp(exchange, symbol: str, side: str, entry_price: float,
                 liq_tracker=None) -> Dict:
    """
    Phân tích đa khung thời gian + thanh khoản để đề xuất SL/TP tối ưu.
    
    Phương pháp:
    1. ATR (Average True Range) — đo biến động
    2. Support/Resistance (swing high/low 20 nến)
    3. EMA 50 — trend support/resistance
    4. Liquidation zones (nếu có liq_tracker)
    5. Risk:Reward tối thiểu 1:2
    
    Returns:
        {
            "sl": float,
            "tp": float,
            "sl_pct": float,    # % từ entry đến SL
            "tp_pct": float,    # % từ entry đến TP
            "rr": float,        # Risk:Reward ratio
            "method": str,      # phương pháp chính
            "details": str,     # chi tiết phân tích
        }
    """
    try:
        # Lấy data đa khung
        klines_15m = exchange.get_klines(symbol, "15m", limit=100)
        klines_1h = exchange.get_klines(symbol, "1h", limit=100)
        klines_4h = exchange.get_klines(symbol, "4h", limit=50)

        df_15m = _to_df(klines_15m)
        df_1h = _to_df(klines_1h)
        df_4h = _to_df(klines_4h)

        price = df_15m["close"].iloc[-1]

        # 1. ATR — đo biến động
        atr_15m = _calc_atr(df_15m, 14)
        atr_1h = _calc_atr(df_1h, 14)
        atr_4h = _calc_atr(df_4h, 14)

        # 2. Support/Resistance từ 1h
        supports, resistances = _find_sr_levels(df_1h, price)

        # 3. EMA 50 trên 1h
        ema50 = df_1h["close"].ewm(span=50).mean().iloc[-1]

        # 4. Liquidity zones
        liq_above = None
        liq_below = None
        if liq_tracker:
            try:
                liq_above = liq_tracker.get_nearest_liq_above(symbol, price, min_usd=50000)
                liq_below = liq_tracker.get_nearest_liq_below(symbol, price, min_usd=50000)
            except Exception:
                pass

        # === Tính SL ===
        sl_candidates = []
        details = []

        if side == "LONG":
            # ATR-based SL: 1.5x ATR dưới entry
            atr_sl = entry_price - atr_1h * 1.5
            sl_candidates.append(("ATR 1.5x", atr_sl))
            details.append(f"ATR(1h)=${atr_1h:.2f}")

            # Support level gần nhất dưới entry
            valid_supports = [s for s in supports if s < entry_price * 0.995]
            if valid_supports:
                sr_sl = max(valid_supports)  # support gần nhất
                sl_candidates.append(("Support", sr_sl))
                details.append(f"Support=${sr_sl:.2f}")

            # EMA50 nếu nằm dưới entry
            if ema50 < entry_price * 0.995:
                sl_candidates.append(("EMA50", ema50 * 0.998))
                details.append(f"EMA50=${ema50:.2f}")

            # Liq zone dưới
            if liq_below and liq_below < entry_price * 0.995:
                sl_candidates.append(("Liq zone", liq_below * 0.998))
                details.append(f"Liq↓=${liq_below:.2f}")

            # Chọn SL tốt nhất: gần entry nhất nhưng không quá gần (>0.5%)
            valid_sls = [(name, sl) for name, sl in sl_candidates
                         if sl < entry_price * 0.995 and sl > entry_price * 0.95]
            if valid_sls:
                # Chọn SL cao nhất (gần entry nhất) = risk ít nhất
                best_sl_name, best_sl = max(valid_sls, key=lambda x: x[1])
            else:
                # Fallback: 2% dưới entry
                best_sl_name = "Default 2%"
                best_sl = entry_price * 0.98

            # === Tính TP ===
            risk = entry_price - best_sl
            # TP tối thiểu RR 1:2
            default_tp = entry_price + risk * 2.5

            tp_candidates = []
            # Resistance gần nhất trên entry
            valid_resistances = [r for r in resistances if r > entry_price * 1.005]
            if valid_resistances:
                sr_tp = min(valid_resistances)  # resistance gần nhất
                tp_candidates.append(("Resistance", sr_tp))
                details.append(f"Resistance=${sr_tp:.2f}")

            # Liq zone trên
            if liq_above and liq_above > entry_price * 1.005:
                tp_candidates.append(("Liq zone", liq_above))
                details.append(f"Liq↑=${liq_above:.2f}")

            # ATR-based TP
            tp_candidates.append(("ATR 3x", entry_price + atr_1h * 3))

            # Chọn TP: gần nhất mà vẫn đảm bảo RR >= 1.5
            min_tp = entry_price + risk * 1.5  # RR tối thiểu 1.5
            valid_tps = [(name, tp) for name, tp in tp_candidates if tp >= min_tp]
            if valid_tps:
                best_tp_name, best_tp = min(valid_tps, key=lambda x: x[1])
            else:
                best_tp_name = "RR 2.5x"
                best_tp = default_tp

        else:  # SHORT
            # ATR-based SL: 1.5x ATR trên entry
            atr_sl = entry_price + atr_1h * 1.5
            sl_candidates.append(("ATR 1.5x", atr_sl))
            details.append(f"ATR(1h)=${atr_1h:.2f}")

            # Resistance gần nhất trên entry
            valid_resistances = [r for r in resistances if r > entry_price * 1.005]
            if valid_resistances:
                sr_sl = min(valid_resistances)
                sl_candidates.append(("Resistance", sr_sl))
                details.append(f"Resistance=${sr_sl:.2f}")

            # EMA50 nếu nằm trên entry
            if ema50 > entry_price * 1.005:
                sl_candidates.append(("EMA50", ema50 * 1.002))
                details.append(f"EMA50=${ema50:.2f}")

            # Liq zone trên
            if liq_above and liq_above > entry_price * 1.005:
                sl_candidates.append(("Liq zone", liq_above * 1.002))
                details.append(f"Liq↑=${liq_above:.2f}")

            # Chọn SL: thấp nhất (gần entry nhất)
            valid_sls = [(name, sl) for name, sl in sl_candidates
                         if sl > entry_price * 1.005 and sl < entry_price * 1.05]
            if valid_sls:
                best_sl_name, best_sl = min(valid_sls, key=lambda x: x[1])
            else:
                best_sl_name = "Default 2%"
                best_sl = entry_price * 1.02

            # === Tính TP ===
            risk = best_sl - entry_price
            default_tp = entry_price - risk * 2.5

            tp_candidates = []
            # Support gần nhất dưới entry
            valid_supports = [s for s in supports if s < entry_price * 0.995]
            if valid_supports:
                sr_tp = max(valid_supports)
                tp_candidates.append(("Support", sr_tp))
                details.append(f"Support=${sr_tp:.2f}")

            # Liq zone dưới
            if liq_below and liq_below < entry_price * 0.995:
                tp_candidates.append(("Liq zone", liq_below))
                details.append(f"Liq↓=${liq_below:.2f}")

            # ATR-based TP
            tp_candidates.append(("ATR 3x", entry_price - atr_1h * 3))

            # Chọn TP
            max_tp = entry_price - risk * 1.5  # RR tối thiểu 1.5
            valid_tps = [(name, tp) for name, tp in tp_candidates if tp <= max_tp]
            if valid_tps:
                best_tp_name, best_tp = max(valid_tps, key=lambda x: x[1])
            else:
                best_tp_name = "RR 2.5x"
                best_tp = default_tp

        # Tính metrics
        if side == "LONG":
            sl_pct = (entry_price - best_sl) / entry_price * 100
            tp_pct = (best_tp - entry_price) / entry_price * 100
        else:
            sl_pct = (best_sl - entry_price) / entry_price * 100
            tp_pct = (entry_price - best_tp) / entry_price * 100

        rr = tp_pct / sl_pct if sl_pct > 0 else 0

        method = f"SL: {best_sl_name} | TP: {best_tp_name}"

        return {
            "sl": round(best_sl, _price_decimals(entry_price)),
            "tp": round(best_tp, _price_decimals(entry_price)),
            "sl_pct": round(sl_pct, 2),
            "tp_pct": round(tp_pct, 2),
            "rr": round(rr, 1),
            "method": method,
            "details": " | ".join(details),
        }

    except Exception as e:
        logger.error(f"suggest_sltp error for {symbol}: {e}")
        # Fallback: 2% SL, 4% TP
        if side == "LONG":
            sl = entry_price * 0.98
            tp = entry_price * 1.04
        else:
            sl = entry_price * 1.02
            tp = entry_price * 0.96
        return {
            "sl": round(sl, _price_decimals(entry_price)),
            "tp": round(tp, _price_decimals(entry_price)),
            "sl_pct": 2.0,
            "tp_pct": 4.0,
            "rr": 2.0,
            "method": "Fallback 2%/4%",
            "details": f"Error: {e}",
        }


def get_positions_without_sltp(exchange) -> List[Dict]:
    """
    Lấy danh sách positions đang mở mà KHÔNG có SL hoặc TP trên Binance.
    """
    try:
        # Lấy tất cả positions
        all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
        open_pos = [p for p in all_pos if abs(float(p.get("positionAmt", 0))) > 0]

        if not open_pos:
            return []

        # Lấy tất cả open orders (regular)
        all_orders = exchange._get("/fapi/v1/openOrders", signed=True)

        # Lấy Algo/Conditional orders (SL/TP mới dùng endpoint này)
        try:
            algo_orders = exchange._get("/fapi/v1/openAlgoOrders", signed=True)
            if isinstance(algo_orders, dict):
                algo_orders = algo_orders.get("orders", [])
        except Exception:
            algo_orders = []

        # Group orders theo symbol
        orders_by_symbol = {}
        for o in all_orders:
            sym = o.get("symbol", "")
            orders_by_symbol.setdefault(sym, []).append(o)

        # Group algo orders theo symbol
        for o in algo_orders:
            sym = o.get("symbol", "")
            orders_by_symbol.setdefault(sym, []).append(o)

        # Check từng position
        unprotected = []
        for p in open_pos:
            sym = p["symbol"]
            amt = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            side = "LONG" if amt > 0 else "SHORT"

            orders = orders_by_symbol.get(sym, [])
            # Check regular orders
            has_sl = any(o.get("type") in ("STOP_MARKET", "STOP") and
                        o.get("reduceOnly", False) for o in orders)
            has_tp = any(o.get("type") in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT") and
                        o.get("reduceOnly", False) for o in orders)
            # Check algo/conditional orders
            if not has_sl:
                has_sl = any(o.get("orderType") == "STOP_MARKET" or
                            o.get("algoType") == "CONDITIONAL" and "STOP" in o.get("orderType", "")
                            for o in orders)
            if not has_tp:
                has_tp = any(o.get("orderType") == "TAKE_PROFIT_MARKET" or
                            o.get("algoType") == "CONDITIONAL" and "TAKE_PROFIT" in o.get("orderType", "")
                            for o in orders)

            if not has_sl or not has_tp:
                unprotected.append({
                    "symbol": sym,
                    "side": side,
                    "entry": entry,
                    "qty": abs(amt),
                    "has_sl": has_sl,
                    "has_tp": has_tp,
                    "mark": float(p.get("markPrice", entry)),
                    "pnl": float(p.get("unRealizedProfit", 0)),
                    "leverage": int(float(p.get("leverage", 1))),
                })

        return unprotected

    except Exception as e:
        logger.error(f"get_positions_without_sltp error: {e}")
        return []


def auto_set_sltp(exchange, symbol: str, side: str, entry: float, qty: float,
                  liq_tracker=None) -> Dict:
    """
    Phân tích chart và tự đặt SL/TP trên Binance cho position.
    
    Returns:
        {"ok": True/False, "sl": float, "tp": float, "msg": str}
    """
    suggestion = suggest_sltp(exchange, symbol, side, entry, liq_tracker)

    sl = suggestion["sl"]
    tp = suggestion["tp"]
    close_side = "SELL" if side == "LONG" else "BUY"

    # Lấy giá hiện tại để validate SL/TP
    try:
        current_price = exchange.get_ticker_price(symbol)
    except Exception:
        current_price = entry

    # Fix SL: phải dưới giá hiện tại (LONG) hoặc trên giá hiện tại (SHORT)
    if side == "LONG":
        if sl >= current_price:
            # Giá đã rớt dưới SL đề xuất → đặt SL dưới giá hiện tại 1.5%
            sl = round(current_price * 0.985, _price_decimals(current_price))
            suggestion["method"] = f"SL: Emergency (price dropped) | TP: {suggestion['method'].split('|')[-1].strip()}"
    else:  # SHORT
        if sl <= current_price:
            # Giá đã tăng trên SL đề xuất → đặt SL trên giá hiện tại 1.5%
            sl = round(current_price * 1.015, _price_decimals(current_price))
            suggestion["method"] = f"SL: Emergency (price pumped) | TP: {suggestion['method'].split('|')[-1].strip()}"

    # Fix TP: phải trên giá hiện tại (LONG) hoặc dưới giá hiện tại (SHORT)
    if side == "LONG" and tp <= current_price:
        tp = round(current_price * 1.02, _price_decimals(current_price))
    elif side == "SHORT" and tp >= current_price:
        tp = round(current_price * 0.98, _price_decimals(current_price))

    sl_ok = False
    tp_ok = False

    # Đặt SL
    try:
        exchange.place_stop_loss_order(symbol, close_side, qty, sl)
        sl_ok = True
        logger.info(f"[AutoSLTP] SL placed: {symbol} {close_side} qty={qty} @ {sl}")
    except Exception as e:
        logger.error(f"[AutoSLTP] SL failed {symbol}: {e}")

    # Đặt TP
    try:
        exchange.place_take_profit_order(symbol, close_side, qty, tp)
        tp_ok = True
        logger.info(f"[AutoSLTP] TP placed: {symbol} {close_side} qty={qty} @ {tp}")
    except Exception as e:
        logger.error(f"[AutoSLTP] TP failed {symbol}: {e}")

    if sl_ok and tp_ok:
        msg = (f"✅ SL/TP đã đặt cho {symbol} {side}\n"
               f"🛑 SL: ${sl} (-{suggestion['sl_pct']}%)\n"
               f"🎯 TP: ${tp} (+{suggestion['tp_pct']}%)\n"
               f"📐 RR: 1:{suggestion['rr']}\n"
               f"📊 {suggestion['method']}\n"
               f"🔬 {suggestion['details']}")
    elif sl_ok:
        msg = f"⚠️ SL đặt OK (${sl}) nhưng TP thất bại"
    elif tp_ok:
        msg = f"⚠️ TP đặt OK (${tp}) nhưng SL thất bại"
    else:
        msg = f"❌ Cả SL và TP đều thất bại cho {symbol}"

    return {
        "ok": sl_ok and tp_ok,
        "sl": sl,
        "tp": tp,
        "sl_ok": sl_ok,
        "tp_ok": tp_ok,
        "suggestion": suggestion,
        "msg": msg,
    }


# ============================================================
# HELPERS
# ============================================================
def _to_df(klines: list) -> pd.DataFrame:
    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def _calc_atr(df: pd.DataFrame, period: int = 14) -> float:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]


def _find_sr_levels(df: pd.DataFrame, current_price: float,
                    lookback: int = 20) -> tuple:
    """Tìm support/resistance từ swing high/low"""
    high = df["high"]
    low = df["low"]

    supports = []
    resistances = []

    for i in range(lookback, len(df) - 1):
        # Swing low = support
        if low.iloc[i] <= low.iloc[i-1] and low.iloc[i] <= low.iloc[i+1] if i+1 < len(df) else True:
            if low.iloc[i] < current_price:
                supports.append(low.iloc[i])
            else:
                resistances.append(low.iloc[i])

        # Swing high = resistance
        if high.iloc[i] >= high.iloc[i-1] and high.iloc[i] >= high.iloc[i+1] if i+1 < len(df) else True:
            if high.iloc[i] > current_price:
                resistances.append(high.iloc[i])
            else:
                supports.append(high.iloc[i])

    # Deduplicate: group levels within 0.3%
    supports = _cluster_levels(sorted(supports, reverse=True), current_price)
    resistances = _cluster_levels(sorted(resistances), current_price)

    return supports[:5], resistances[:5]


def _cluster_levels(levels: list, ref_price: float, threshold_pct: float = 0.3) -> list:
    """Group levels gần nhau (trong 0.3%) thành 1"""
    if not levels:
        return []
    clustered = [levels[0]]
    for l in levels[1:]:
        if abs(l - clustered[-1]) / ref_price * 100 > threshold_pct:
            clustered.append(l)
    return clustered


def _price_decimals(price: float) -> int:
    """Số decimal cho giá"""
    if price >= 10000: return 1
    if price >= 1000: return 2
    if price >= 100: return 2
    if price >= 10: return 3
    if price >= 1: return 4
    return 6
