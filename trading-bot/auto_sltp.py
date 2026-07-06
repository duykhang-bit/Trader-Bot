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
    Đặt SL/TP dựa vào THANH KHOẢN (liquidation zones) là ưu tiên số 1.

    Nguyên tắc:
    ┌─────────────────────────────────────────────────────────────┐
    │  SL  → nằm DƯỚI vùng liq bị quét (stop hunt zone)         │
    │        Giá sẽ quét liq rồi đảo chiều → SL đặt sau vùng đó │
    │                                                             │
    │  TP  → vùng liq LỚN NHẤT phía target (nơi giá tiến tới)   │
    │        Giá bị hút về vùng liq tập trung → TP đặt ở đó     │
    └─────────────────────────────────────────────────────────────┘

    Ưu tiên:
      SL: liq_zone_bị_quét → swing_high/low → ATR fallback
      TP: liq_zone_lớn_nhất_phía_target → resistance/support → ATR fallback

    Returns:
        {
            "sl": float,
            "tp": float,
            "sl_pct": float,
            "tp_pct": float,
            "rr": float,
            "method": str,
            "details": str,
        }
    """
    decimals = _price_decimals(entry_price)

    try:
        # ── Lấy data chart đa khung ──────────────────────────
        klines_15m = exchange.get_klines(symbol, "15m", limit=100)
        klines_1h  = exchange.get_klines(symbol, "1h",  limit=100)
        klines_4h  = exchange.get_klines(symbol, "4h",  limit=50)
        df_15m = _to_df(klines_15m)
        df_1h  = _to_df(klines_1h)
        df_4h  = _to_df(klines_4h)

        price  = df_15m["close"].iloc[-1]
        atr_1h = _calc_atr(df_1h, 14)
        atr_4h = _calc_atr(df_4h, 14)

        # Swing high/low từ 1h (20 nến gần nhất)
        supports, resistances = _find_sr_levels(df_1h, price)
        # Recent swing extremes từ 4h (dùng để backup SL)
        sup_4h, res_4h = _find_sr_levels(df_4h, price)

        details = [f"entry=${entry_price:.{decimals}f}", f"ATR(1h)=${atr_1h:.{decimals}f}"]

        # ── Lấy toàn bộ liq heatmap ──────────────────────────
        heatmap: Dict = {}
        if liq_tracker:
            try:
                heatmap = liq_tracker.get_liq_heatmap(symbol) or {}
            except Exception:
                pass

        # Tách vùng liq phía dưới và phía trên entry
        liq_below_map = {p: u for p, u in heatmap.items() if p < entry_price}
        liq_above_map = {p: u for p, u in heatmap.items() if p > entry_price}

        # ── ═══════════════════════════════════════════════════
        # LONG: SL dưới vùng liq SHORT bị quét, TP lên vùng liq LONG lớn nhất
        # ── ═══════════════════════════════════════════════════
        if side == "LONG":

            # ── SL: nằm SAU (dưới) vùng liq bị quét ──────────
            # Vùng liq SHORT phía dưới = nơi stop hunt sẽ xảy ra
            # SL đặt thêm buffer dưới vùng đó để tránh bị quét
            sl_method = "ATR fallback"
            best_sl   = entry_price - atr_1h * 2.0  # fallback

            if liq_below_map:
                # Lấy vùng liq gần entry nhất phía dưới (vùng bị quét đầu tiên)
                # → SL đặt dưới vùng đó 0.3% (buffer tránh stop hunt)
                sweep_zone = max(liq_below_map.keys())  # gần entry nhất phía dưới
                sweep_usd  = liq_below_map[sweep_zone]

                # Nếu có vùng liq lớn ($50k+) thì SL đặt dưới nó
                if sweep_usd >= 50_000:
                    # SL = dưới đáy vùng liq 0.3%
                    candidate_sl = sweep_zone * 0.997
                    # Kiểm tra không quá xa (max 5% từ entry)
                    if candidate_sl > entry_price * 0.95:
                        best_sl   = candidate_sl
                        sl_method = f"Below liq↓${sweep_zone:.{decimals}f}(${sweep_usd/1e3:.0f}k)"
                        details.append(f"SweepZone=${sweep_zone:.{decimals}f}")
                    else:
                        # Vùng liq quá xa → dùng swing low gần nhất + buffer
                        sl_method = "Liq too far, swing low"

            # Nếu không có liq data hoặc vùng liq quá xa → swing low
            if sl_method in ("ATR fallback", "Liq too far, swing low"):
                valid_sup = [s for s in supports if s < entry_price * 0.995]
                if valid_sup:
                    swing_low = max(valid_sup)  # swing low gần nhất
                    # SL = dưới swing low 0.2% (buffer nhỏ)
                    candidate_sl = swing_low * 0.998
                    if candidate_sl > entry_price * 0.95:
                        best_sl   = candidate_sl
                        sl_method = f"Below swing low ${swing_low:.{decimals}f}"
                        details.append(f"SwingLow=${swing_low:.{decimals}f}")
                    else:
                        # Swing low quá xa → ATR 2x
                        best_sl   = entry_price - atr_1h * 2.0
                        sl_method = f"ATR×2.0"
                else:
                    best_sl   = entry_price - atr_1h * 2.0
                    sl_method = f"ATR×2.0"

            # Hard floor: SL không được quá 5% dưới entry (bảo vệ rủi ro)
            best_sl = max(best_sl, entry_price * 0.95)
            risk    = entry_price - best_sl

            # ── TP: vùng liq LONG lớn nhất phía trên ──────────
            # Thị trường bị hút về vùng tập trung liq lớn
            tp_method = "RR 2.5x fallback"
            best_tp   = entry_price + risk * 2.5  # fallback RR 1:2.5

            if liq_above_map:
                # Lấy tất cả vùng liq phía trên đảm bảo RR >= 1.5
                min_tp_rr = entry_price + risk * 1.5
                valid_liq_tp = {p: u for p, u in liq_above_map.items()
                                if p >= min_tp_rr}

                if valid_liq_tp:
                    # TP = vùng liq LỚN NHẤT (về USD) phía trên
                    # → đây là vùng giá bị hút mạnh nhất
                    target_zone = max(valid_liq_tp.keys(), key=lambda p: valid_liq_tp[p])
                    target_usd  = valid_liq_tp[target_zone]
                    # TP đặt ngay trước vùng liq (0.2% dưới) để chốt trước khi đảo chiều
                    best_tp   = target_zone * 0.998
                    tp_method = f"Liq↑${target_zone:.{decimals}f}(${target_usd/1e3:.0f}k)"
                    details.append(f"LiqTarget=${target_zone:.{decimals}f}")

            # Fallback nếu liq TP không đủ RR: dùng resistance hoặc ATR
            if best_tp < entry_price + risk * 1.5:
                valid_res = [r for r in resistances if r > entry_price + risk * 1.5]
                if valid_res:
                    best_tp   = min(valid_res) * 0.999
                    tp_method = f"Resistance ${min(valid_res):.{decimals}f}"
                else:
                    best_tp   = entry_price + risk * 2.5
                    tp_method = "RR 2.5x"

        # ── ═══════════════════════════════════════════════════
        # SHORT: SL trên vùng liq LONG bị quét, TP xuống vùng liq SHORT lớn nhất
        # ── ═══════════════════════════════════════════════════
        else:

            # ── SL: nằm SAU (trên) vùng liq bị quét ──────────
            sl_method = "ATR fallback"
            best_sl   = entry_price + atr_1h * 2.0  # fallback

            if liq_above_map:
                # Vùng liq LONG gần entry nhất phía trên = nơi stop hunt xảy ra
                sweep_zone = min(liq_above_map.keys())
                sweep_usd  = liq_above_map[sweep_zone]

                if sweep_usd >= 50_000:
                    candidate_sl = sweep_zone * 1.003  # SL trên vùng liq 0.3%
                    if candidate_sl < entry_price * 1.05:
                        best_sl   = candidate_sl
                        sl_method = f"Above liq↑${sweep_zone:.{decimals}f}(${sweep_usd/1e3:.0f}k)"
                        details.append(f"SweepZone=${sweep_zone:.{decimals}f}")
                    else:
                        sl_method = "Liq too far, swing high"

            if sl_method in ("ATR fallback", "Liq too far, swing high"):
                valid_res = [r for r in resistances if r > entry_price * 1.005]
                if valid_res:
                    swing_high = min(valid_res)
                    candidate_sl = swing_high * 1.002
                    if candidate_sl < entry_price * 1.05:
                        best_sl   = candidate_sl
                        sl_method = f"Above swing high ${swing_high:.{decimals}f}"
                        details.append(f"SwingHigh=${swing_high:.{decimals}f}")
                    else:
                        best_sl   = entry_price + atr_1h * 2.0
                        sl_method = "ATR×2.0"
                else:
                    best_sl   = entry_price + atr_1h * 2.0
                    sl_method = "ATR×2.0"

            # Hard ceiling: SL không quá 5% trên entry
            best_sl = min(best_sl, entry_price * 1.05)
            risk    = best_sl - entry_price

            # ── TP: vùng liq SHORT lớn nhất phía dưới ─────────
            tp_method = "RR 2.5x fallback"
            best_tp   = entry_price - risk * 2.5

            if liq_below_map:
                max_tp_rr = entry_price - risk * 1.5
                valid_liq_tp = {p: u for p, u in liq_below_map.items()
                                if p <= max_tp_rr}

                if valid_liq_tp:
                    target_zone = min(valid_liq_tp.keys(), key=lambda p: -valid_liq_tp[p])
                    target_usd  = valid_liq_tp[target_zone]
                    best_tp   = target_zone * 1.002  # TP ngay trước vùng liq (0.2% trên)
                    tp_method = f"Liq↓${target_zone:.{decimals}f}(${target_usd/1e3:.0f}k)"
                    details.append(f"LiqTarget=${target_zone:.{decimals}f}")

            if best_tp > entry_price - risk * 1.5:
                valid_sup = [s for s in supports if s < entry_price - risk * 1.5]
                if valid_sup:
                    best_tp   = max(valid_sup) * 1.001
                    tp_method = f"Support ${max(valid_sup):.{decimals}f}"
                else:
                    best_tp   = entry_price - risk * 2.5
                    tp_method = "RR 2.5x"

        # ── Tính metrics ──────────────────────────────────────
        if side == "LONG":
            sl_pct = (entry_price - best_sl) / entry_price * 100
            tp_pct = (best_tp - entry_price) / entry_price * 100
        else:
            sl_pct = (best_sl - entry_price) / entry_price * 100
            tp_pct = (entry_price - best_tp) / entry_price * 100

        # Đảm bảo sl_pct và tp_pct dương
        sl_pct = abs(sl_pct)
        tp_pct = abs(tp_pct)
        rr     = tp_pct / sl_pct if sl_pct > 0 else 0

        logger.info(
            f"[suggest_sltp] {symbol} {side} | entry={entry_price:.{decimals}f} "
            f"SL={best_sl:.{decimals}f}(-{sl_pct:.2f}%) "
            f"TP={best_tp:.{decimals}f}(+{tp_pct:.2f}%) RR=1:{rr:.1f} "
            f"| SL:{sl_method} | TP:{tp_method}"
        )

        return {
            "sl":      round(best_sl, decimals),
            "tp":      round(best_tp, decimals),
            "sl_pct":  round(sl_pct, 2),
            "tp_pct":  round(tp_pct, 2),
            "rr":      round(rr, 1),
            "method":  f"SL: {sl_method} | TP: {tp_method}",
            "details": " | ".join(details),
        }

    except Exception as e:
        logger.error(f"suggest_sltp error for {symbol}: {e}", exc_info=True)
        # Fallback an toàn: 1.5% SL, 3% TP (RR 1:2)
        if side == "LONG":
            sl = round(entry_price * 0.985, decimals)
            tp = round(entry_price * 1.030, decimals)
        else:
            sl = round(entry_price * 1.015, decimals)
            tp = round(entry_price * 0.970, decimals)
        return {
            "sl":      sl,
            "tp":      tp,
            "sl_pct":  1.5,
            "tp_pct":  3.0,
            "rr":      2.0,
            "method":  "Fallback 1.5%/3%",
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
