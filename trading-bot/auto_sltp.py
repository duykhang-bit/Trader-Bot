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

            # ── SL: đặt SAU vùng liq thứ 2 phía dưới ─────────
            # Vùng liq gần nhất = nơi giá quét trước → SL đặt dưới vùng thứ 2
            # Nếu chỉ có 1 vùng liq → SL dưới vùng đó + ATR buffer
            sl_method = "ATR fallback"
            best_sl   = entry_price - atr_1h * 2.0

            if liq_below_map:
                # Sort theo giá giảm dần (gần entry nhất trước)
                sorted_liq_below = sorted(
                    [(p, u) for p, u in liq_below_map.items() if u >= 30_000],
                    key=lambda x: x[0], reverse=True
                )

                if len(sorted_liq_below) >= 2:
                    # Có >= 2 vùng liq → SL dưới vùng thứ 2 (sau khi quét xong vùng 1)
                    sweep_zone2, sweep_usd2 = sorted_liq_below[1]
                    candidate_sl = sweep_zone2 * 0.996  # 0.4% dưới vùng thứ 2
                    if candidate_sl > entry_price * 0.94:  # max 6% từ entry
                        best_sl   = candidate_sl
                        sl_method = (f"Below 2nd liq↓${sweep_zone2:.{decimals}f}"
                                     f"(${sweep_usd2/1e3:.0f}k)")
                        details.append(f"SweepZone2=${sweep_zone2:.{decimals}f}")
                    else:
                        # Quá xa → dùng vùng liq 1 + ATR buffer
                        sweep_zone1 = sorted_liq_below[0][0]
                        candidate_sl = sweep_zone1 * 0.995
                        if candidate_sl > entry_price * 0.94:
                            best_sl   = candidate_sl
                            sl_method = f"Below 1st liq + ATR buffer"

                elif len(sorted_liq_below) == 1:
                    # Chỉ 1 vùng liq → SL dưới đó + ATR×1.0 buffer
                    sweep_zone1, sweep_usd1 = sorted_liq_below[0]
                    candidate_sl = min(sweep_zone1 * 0.996,
                                       sweep_zone1 - atr_1h)
                    if candidate_sl > entry_price * 0.94:
                        best_sl   = candidate_sl
                        sl_method = (f"Below liq↓${sweep_zone1:.{decimals}f}"
                                     f"+ATR(${sweep_usd1/1e3:.0f}k)")
                        details.append(f"SweepZone=${sweep_zone1:.{decimals}f}")

            # Không có liq data → swing low
            if sl_method == "ATR fallback":
                valid_sup = [s for s in supports if s < entry_price * 0.995]
                if valid_sup:
                    # Lấy swing low thứ 2 nếu có (tránh dừng ở swing low gần)
                    swing_levels = sorted(valid_sup, reverse=True)
                    swing_ref = swing_levels[1] if len(swing_levels) >= 2 else swing_levels[0]
                    candidate_sl = swing_ref * 0.998
                    if candidate_sl > entry_price * 0.94:
                        best_sl   = candidate_sl
                        sl_method = f"Below swing low2 ${swing_ref:.{decimals}f}"
                    else:
                        best_sl   = entry_price - atr_1h * 2.0
                        sl_method = "ATR×2.0"
                else:
                    best_sl   = entry_price - atr_1h * 2.0
                    sl_method = "ATR×2.0"

            # Hard floor: max 6% dưới entry, tối thiểu 1% dưới entry
            best_sl = max(best_sl, entry_price * 0.94)   # không quá xa (max 6%)
            best_sl = min(best_sl, entry_price * 0.99)   # không quá sát (min 1%)
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

            # ── SL: nằm SAU (trên) vùng liq thứ 2 bị quét ──────────
            sl_method = "ATR fallback"
            best_sl   = entry_price + atr_1h * 2.0

            if liq_above_map:
                sorted_liq_above = sorted(
                    [(p, u) for p, u in liq_above_map.items() if u >= 30_000],
                    key=lambda x: x[0]  # gần entry nhất trước
                )

                if len(sorted_liq_above) >= 2:
                    sweep_zone2, sweep_usd2 = sorted_liq_above[1]
                    candidate_sl = sweep_zone2 * 1.004
                    if candidate_sl < entry_price * 1.06:
                        best_sl   = candidate_sl
                        sl_method = (f"Above 2nd liq↑${sweep_zone2:.{decimals}f}"
                                     f"(${sweep_usd2/1e3:.0f}k)")
                        details.append(f"SweepZone2=${sweep_zone2:.{decimals}f}")
                    else:
                        sweep_zone1 = sorted_liq_above[0][0]
                        candidate_sl = sweep_zone1 * 1.005
                        if candidate_sl < entry_price * 1.06:
                            best_sl   = candidate_sl
                            sl_method = "Above 1st liq + ATR buffer"

                elif len(sorted_liq_above) == 1:
                    sweep_zone1, sweep_usd1 = sorted_liq_above[0]
                    candidate_sl = max(sweep_zone1 * 1.004, sweep_zone1 + atr_1h)
                    if candidate_sl < entry_price * 1.06:
                        best_sl   = candidate_sl
                        sl_method = (f"Above liq↑${sweep_zone1:.{decimals}f}"
                                     f"+ATR(${sweep_usd1/1e3:.0f}k)")

            if sl_method == "ATR fallback":
                valid_res = [r for r in resistances if r > entry_price * 1.005]
                if valid_res:
                    swing_levels = sorted(valid_res)
                    swing_ref = swing_levels[1] if len(swing_levels) >= 2 else swing_levels[0]
                    candidate_sl = swing_ref * 1.002
                    if candidate_sl < entry_price * 1.06:
                        best_sl   = candidate_sl
                        sl_method = f"Above swing high2 ${swing_ref:.{decimals}f}"
                    else:
                        best_sl   = entry_price + atr_1h * 2.0
                        sl_method = "ATR×2.0"
                else:
                    best_sl   = entry_price + atr_1h * 2.0
                    sl_method = "ATR×2.0"

            # Hard ceiling: max 6% trên entry, tối thiểu 1% trên entry
            best_sl = min(best_sl, entry_price * 1.06)   # không quá xa (max 6%)
            best_sl = max(best_sl, entry_price * 1.01)   # không quá sát (min 1%)
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

    SL/TP được đặt qua /fapi/v1/algoOrder (Algo Conditional API).
    Cần check /fapi/v1/openAlgoOrders — không phải /fapi/v1/openOrders.
    """
    try:
        # ── 1. Lấy tất cả positions đang mở ──────────────────
        all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
        open_pos = [p for p in all_pos if abs(float(p.get("positionAmt", 0))) > 0]

        if not open_pos:
            return []

        # ── 2. Lấy regular open orders (không dùng để check SL/TP nữa,
        #        chỉ dùng để biết lệnh entry LIMIT nào chưa fill) ──
        try:
            regular_orders = exchange._get("/fapi/v1/openOrders", signed=True)
        except Exception:
            regular_orders = []

        # Regular reduce-only orders (STOP_MARKET / TAKE_PROFIT_MARKET đặt qua v1/order)
        regular_sl_syms = set()
        regular_tp_syms = set()
        for o in regular_orders:
            if not o.get("reduceOnly", False):
                continue
            otype = o.get("type", "")
            sym   = o.get("symbol", "")
            if otype in ("STOP_MARKET", "STOP"):
                regular_sl_syms.add(sym)
            if otype in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                regular_tp_syms.add(sym)

        # ── 3. Lấy Algo/Conditional orders — đây là nơi SL/TP được lưu ──
        algo_sl_syms = set()
        algo_tp_syms = set()
        try:
            algo_resp = exchange._get("/fapi/v1/openAlgoOrders", signed=True)
            # API trả về {"total": N, "orders": [...]}
            if isinstance(algo_resp, dict):
                algo_list = algo_resp.get("orders", [])
            elif isinstance(algo_resp, list):
                algo_list = algo_resp
            else:
                algo_list = []

            for o in algo_list:
                sym   = o.get("symbol", "")
                # Algo orders dùng "orderType" thay vì "type"
                otype = o.get("orderType", o.get("type", ""))
                if otype in ("STOP_MARKET", "STOP"):
                    algo_sl_syms.add(sym)
                if otype in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                    algo_tp_syms.add(sym)

        except Exception as e:
            logger.debug(f"get_positions_without_sltp: algo orders error: {e}")

        # ── 4. Check từng position ────────────────────────────
        unprotected = []
        for p in open_pos:
            sym   = p["symbol"]
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            side  = "LONG" if amt > 0 else "SHORT"

            has_sl = sym in regular_sl_syms or sym in algo_sl_syms
            has_tp = sym in regular_tp_syms or sym in algo_tp_syms

            if not has_sl or not has_tp:
                unprotected.append({
                    "symbol":   sym,
                    "side":     side,
                    "entry":    entry,
                    "qty":      abs(amt),
                    "has_sl":   has_sl,
                    "has_tp":   has_tp,
                    "mark":     float(p.get("markPrice", entry)),
                    "pnl":      float(p.get("unRealizedProfit", 0)),
                    "leverage": int(float(p.get("leverage", 1))),
                })

        return unprotected

    except Exception as e:
        logger.error(f"get_positions_without_sltp error: {e}", exc_info=True)
        return []


def auto_set_sltp(exchange, symbol: str, side: str, entry: float, qty: float,
                  liq_tracker=None, sl_floor=None) -> Dict:
    """
    Phân tích chart và tự đặt SL/TP trên Binance cho position.
    sl_floor: SL không được xa hơn mức này (để giới hạn risk 1%)
    
    Returns:
        {"ok": True/False, "sl": float, "tp": float, "msg": str}
    """
    suggestion = suggest_sltp(exchange, symbol, side, entry, liq_tracker)

    sl = suggestion["sl"]
    tp = suggestion["tp"]

    # Enforce sl_floor — không để SL xa hơn mức risk cho phép
    if sl_floor is not None:
        if side == "LONG" and sl < sl_floor:
            sl = sl_floor
            logger.info(f"[AutoSLTP] {symbol} LONG SL capped: {suggestion['sl']:.6f} → {sl:.6f} (risk 1% limit)")
        elif side == "SHORT" and sl > sl_floor:
            sl = sl_floor
            logger.info(f"[AutoSLTP] {symbol} SHORT SL capped: {suggestion['sl']:.6f} → {sl:.6f} (risk 1% limit)")

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
