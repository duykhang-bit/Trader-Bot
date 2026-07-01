# ============================================================
# SMART ENTRY v2 — Multi-Method Confluence Entry
#
# Phân tích chart 1m/5m/15m bằng nhiều phương pháp:
# 1. VWAP (Volume Weighted Average Price)
# 2. Fair Value Gap (FVG) — imbalance zones
# 3. Order Block — institutional supply/demand zones
# 4. Fibonacci 0.618/0.705 retracement
# 5. Volume Profile POC (Point of Control)
# 6. EMA Pullback (EMA9, EMA21)
# 7. Support/Resistance (swing high/low)
#
# Chọn entry = vùng hội tụ nhiều phương pháp nhất
# ============================================================
import logging
import pandas as pd
import numpy as np
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)


def find_optimal_entry(exchange, symbol: str, side: str, config) -> dict:
    """
    Phân tích đa phương pháp tìm entry tối ưu.
    
    Returns:
        {
            "entry_price": float,
            "current_price": float,
            "improvement_pct": float,
            "method": str,
            "sl": float,
            "tp": float,
            "use_limit": bool,
            "confluence_score": int,  # số phương pháp hội tụ (1-7)
            "levels": dict,           # chi tiết từng level
        }
    """
    try:
        klines_1m = exchange.get_klines(symbol, "1m", limit=120)
        klines_5m = exchange.get_klines(symbol, "5m", limit=100)
        klines_15m = exchange.get_klines(symbol, "15m", limit=100)

        df_1m = _to_df(klines_1m)
        df_5m = _to_df(klines_5m)
        df_15m = _to_df(klines_15m)

        price = df_1m["close"].iloc[-1]

        # ── Tính tất cả entry levels ──
        levels = {}

        # 1. VWAP
        vwap = _calc_vwap(df_1m)
        if vwap:
            levels["vwap"] = vwap

        # 2. Fair Value Gap (FVG)
        fvg = _find_fvg(df_1m, side, price)
        if fvg:
            levels["fvg"] = fvg

        # 3. Order Block
        ob = _find_order_block(df_5m, side, price)
        if ob:
            levels["order_block"] = ob

        # 4. Fibonacci 0.618
        fib = _calc_fibonacci(df_15m, side, price)
        if fib:
            levels["fib_618"] = fib

        # 5. Volume Profile POC
        poc = _calc_volume_poc(df_1m, price)
        if poc:
            levels["poc"] = poc

        # 6. EMA Pullback
        ema_level = _calc_ema_pullback(df_1m, df_5m, side, price)
        if ema_level:
            levels["ema"] = ema_level

        # 7. Support/Resistance
        sr = _calc_support_resistance(df_15m, side, price)
        if sr:
            levels["support_resistance"] = sr

        # ── Tìm vùng hội tụ ──
        entry_price, confluence_score, method = _find_confluence(
            levels, side, price
        )

        # ── Tính SL / TP ──
        atr_15m = _calc_atr(df_15m, 14)
        atr_5m = _calc_atr(df_5m, 14)

        if side == "LONG":
            # SL: dưới entry - 1.5×ATR hoặc dưới swing low gần nhất
            swing_low = df_15m["low"].rolling(20).min().iloc[-1]
            sl = min(entry_price - atr_15m * 1.5, swing_low - atr_5m * 0.5)
            sl = round(sl, _price_dec(price))

            # TP: RR 1:2.5 hoặc resistance gần nhất
            risk = entry_price - sl
            tp = entry_price + risk * 2.5
            swing_high = df_15m["high"].rolling(20).max().iloc[-1]
            tp = min(tp, swing_high)
            tp = round(tp, _price_dec(price))
        else:
            swing_high = df_15m["high"].rolling(20).max().iloc[-1]
            sl = max(entry_price + atr_15m * 1.5, swing_high + atr_5m * 0.5)
            sl = round(sl, _price_dec(price))

            risk = sl - entry_price
            tp = entry_price - risk * 2.5
            swing_low = df_15m["low"].rolling(20).min().iloc[-1]
            tp = max(tp, swing_low)
            tp = round(tp, _price_dec(price))

        entry_price = round(entry_price, _price_dec(price))
        improvement = abs(price - entry_price) / price * 100

        # SAFETY: đảm bảo entry đúng hướng
        # LONG: entry phải <= price (mua rẻ hơn)
        # SHORT: entry phải >= price (bán đắt hơn)
        if side == "LONG" and entry_price > price:
            entry_price = round(price * 0.998, _price_dec(price))  # 0.2% dưới
            method = "safety_cap_long"
            confluence_score = 1
        elif side == "SHORT" and entry_price < price:
            entry_price = round(price * 1.002, _price_dec(price))  # 0.2% trên
            method = "safety_cap_short"
            confluence_score = 1

        improvement = abs(price - entry_price) / price * 100

        # Nếu improvement quá nhỏ (<0.03%) hoặc confluent=1 → market
        use_limit = improvement >= 0.03 and confluence_score >= 2

        return {
            "entry_price": entry_price,
            "current_price": price,
            "improvement_pct": round(improvement, 3),
            "method": method,
            "sl": sl,
            "tp": tp,
            "use_limit": use_limit,
            "confluence_score": confluence_score,
            "levels": {k: round(v, _price_dec(price)) for k, v in levels.items()},
        }

    except Exception as e:
        logger.error(f"[SmartEntry] Error for {symbol}: {e}")
        return _fallback_entry(exchange, symbol, side)


# ════════════════════════════════════════════════════════════
# 1. VWAP (Volume Weighted Average Price)
# ════════════════════════════════════════════════════════════
def _calc_vwap(df: pd.DataFrame) -> Optional[float]:
    """VWAP = cumsum(price × volume) / cumsum(volume)"""
    try:
        typical = (df["high"] + df["low"] + df["close"]) / 3
        cum_vol = df["volume"].cumsum()
        cum_tp_vol = (typical * df["volume"]).cumsum()
        vwap = cum_tp_vol / cum_vol
        return float(vwap.iloc[-1])
    except Exception:
        return None


# ════════════════════════════════════════════════════════════
# 2. Fair Value Gap (FVG) — Imbalance Zones
# ════════════════════════════════════════════════════════════
def _find_fvg(df: pd.DataFrame, side: str, price: float) -> Optional[float]:
    """
    FVG = gap giữa nến 1 high và nến 3 low (bullish)
    hoặc nến 1 low và nến 3 high (bearish).
    Tìm FVG gần nhất chưa được fill.
    """
    try:
        fvgs = []
        for i in range(2, len(df) - 1):
            # Bullish FVG: candle[i-2].high < candle[i].low (gap up)
            if df["high"].iloc[i - 2] < df["low"].iloc[i]:
                gap_low = df["high"].iloc[i - 2]
                gap_high = df["low"].iloc[i]
                mid = (gap_low + gap_high) / 2
                # Chưa fill = giá hiện tại chưa quay lại vùng này
                if side == "LONG" and mid < price:
                    fvgs.append(mid)

            # Bearish FVG: candle[i-2].low > candle[i].high (gap down)
            if df["low"].iloc[i - 2] > df["high"].iloc[i]:
                gap_low = df["high"].iloc[i]
                gap_high = df["low"].iloc[i - 2]
                mid = (gap_low + gap_high) / 2
                if side == "SHORT" and mid > price:
                    fvgs.append(mid)

        if not fvgs:
            return None

        # Chọn FVG gần nhất với giá hiện tại
        fvgs.sort(key=lambda x: abs(x - price))
        return fvgs[0]
    except Exception:
        return None


# ════════════════════════════════════════════════════════════
# 3. Order Block — Institutional Supply/Demand
# ════════════════════════════════════════════════════════════
def _find_order_block(df: pd.DataFrame, side: str, price: float) -> Optional[float]:
    """
    Order Block = nến cuối cùng trước 1 breakout mạnh.
    - Bullish OB: nến đỏ cuối cùng trước khi giá pump mạnh → demand zone
    - Bearish OB: nến xanh cuối cùng trước khi giá dump mạnh → supply zone
    """
    try:
        # Tìm impulse moves (nến có body > 2× ATR)
        close = df["close"]
        opn = df["open"]
        body = (close - opn).abs()
        atr = _calc_atr(df, 14)
        threshold = atr * 1.5

        blocks = []
        for i in range(3, len(df) - 1):
            if body.iloc[i] > threshold:
                # Bullish impulse (nến xanh lớn)
                if close.iloc[i] > opn.iloc[i] and side == "LONG":
                    # OB = nến đỏ ngay trước đó
                    for j in range(i - 1, max(i - 4, 0), -1):
                        if close.iloc[j] < opn.iloc[j]:  # nến đỏ
                            ob_level = (df["high"].iloc[j] + df["low"].iloc[j]) / 2
                            if ob_level < price:
                                blocks.append(ob_level)
                            break

                # Bearish impulse (nến đỏ lớn)
                elif close.iloc[i] < opn.iloc[i] and side == "SHORT":
                    for j in range(i - 1, max(i - 4, 0), -1):
                        if close.iloc[j] > opn.iloc[j]:  # nến xanh
                            ob_level = (df["high"].iloc[j] + df["low"].iloc[j]) / 2
                            if ob_level > price:
                                blocks.append(ob_level)
                            break

        if not blocks:
            return None
        # Gần nhất với giá
        blocks.sort(key=lambda x: abs(x - price))
        return blocks[0]
    except Exception:
        return None


# ════════════════════════════════════════════════════════════
# 4. Fibonacci Retracement 0.618 / 0.705
# ════════════════════════════════════════════════════════════
def _calc_fibonacci(df: pd.DataFrame, side: str, price: float) -> Optional[float]:
    """
    Tìm swing high/low gần nhất trên 15m, tính fib 0.618.
    - LONG: fib 0.618 của swing (low → high) = pullback zone
    - SHORT: fib 0.618 của swing (high → low) = pullback zone
    """
    try:
        highs = df["high"]
        lows = df["low"]

        if side == "LONG":
            # Tìm swing: recent low → recent high
            swing_low_idx = lows.rolling(10).min().iloc[-1]
            swing_high_idx = highs.rolling(10).max().iloc[-1]
            swing_low = float(lows.iloc[-20:].min())
            swing_high = float(highs.iloc[-20:].max())

            if swing_high <= swing_low:
                return None

            # Fib 0.618 retracement from high
            fib_618 = swing_high - (swing_high - swing_low) * 0.618
            fib_705 = swing_high - (swing_high - swing_low) * 0.705

            # Chọn level gần giá hiện tại nhất nhưng dưới price
            candidates = [f for f in [fib_618, fib_705] if f < price]
            if candidates:
                return max(candidates)  # gần price nhất

        else:
            swing_low = float(lows.iloc[-20:].min())
            swing_high = float(highs.iloc[-20:].max())

            if swing_high <= swing_low:
                return None

            fib_618 = swing_low + (swing_high - swing_low) * 0.618
            fib_705 = swing_low + (swing_high - swing_low) * 0.705

            candidates = [f for f in [fib_618, fib_705] if f > price]
            if candidates:
                return min(candidates)

        return None
    except Exception:
        return None


# ════════════════════════════════════════════════════════════
# 5. Volume Profile POC (Point of Control)
# ════════════════════════════════════════════════════════════
def _calc_volume_poc(df: pd.DataFrame, price: float) -> Optional[float]:
    """
    POC = giá có volume giao dịch cao nhất (approximation).
    Chia price range thành buckets, tổng volume mỗi bucket.
    """
    try:
        high = df["high"].max()
        low = df["low"].min()
        if high == low:
            return None

        n_buckets = 50
        bucket_size = (high - low) / n_buckets
        buckets = np.zeros(n_buckets)

        for _, row in df.iterrows():
            # Distribute volume across candle range
            candle_low = row["low"]
            candle_high = row["high"]
            vol = row["volume"]

            low_idx = max(0, int((candle_low - low) / bucket_size))
            high_idx = min(n_buckets - 1, int((candle_high - low) / bucket_size))

            if high_idx >= low_idx:
                per_bucket = vol / max(high_idx - low_idx + 1, 1)
                for i in range(low_idx, high_idx + 1):
                    if i < n_buckets:
                        buckets[i] += per_bucket

        # POC = bucket với volume cao nhất
        poc_idx = int(np.argmax(buckets))
        poc_price = low + (poc_idx + 0.5) * bucket_size

        return float(poc_price)
    except Exception:
        return None


# ════════════════════════════════════════════════════════════
# 6. EMA Pullback
# ════════════════════════════════════════════════════════════
def _calc_ema_pullback(df_1m: pd.DataFrame, df_5m: pd.DataFrame,
                        side: str, price: float) -> Optional[float]:
    """EMA9/21 trên 1m và 5m làm dynamic support/resistance."""
    try:
        ema9_1m = df_1m["close"].ewm(span=9, adjust=False).mean().iloc[-1]
        ema21_1m = df_1m["close"].ewm(span=21, adjust=False).mean().iloc[-1]
        ema21_5m = df_5m["close"].ewm(span=21, adjust=False).mean().iloc[-1]

        if side == "LONG":
            # EMA dưới giá = support
            candidates = [v for v in [ema9_1m, ema21_1m, ema21_5m] if v < price]
            if candidates:
                return max(candidates)  # gần nhất phía dưới
        else:
            candidates = [v for v in [ema9_1m, ema21_1m, ema21_5m] if v > price]
            if candidates:
                return min(candidates)  # gần nhất phía trên

        return None
    except Exception:
        return None


# ════════════════════════════════════════════════════════════
# 7. Support / Resistance (Swing High/Low)
# ════════════════════════════════════════════════════════════
def _calc_support_resistance(df: pd.DataFrame, side: str, price: float) -> Optional[float]:
    """Tìm swing levels gần nhất từ 15m chart."""
    try:
        # Tìm swing lows (support) và swing highs (resistance)
        pivots = []
        for i in range(2, len(df) - 2):
            # Swing low
            if df["low"].iloc[i] < df["low"].iloc[i-1] and df["low"].iloc[i] < df["low"].iloc[i-2] \
               and df["low"].iloc[i] < df["low"].iloc[i+1] and df["low"].iloc[i] < df["low"].iloc[i+2]:
                pivots.append(("support", float(df["low"].iloc[i])))
            # Swing high
            if df["high"].iloc[i] > df["high"].iloc[i-1] and df["high"].iloc[i] > df["high"].iloc[i-2] \
               and df["high"].iloc[i] > df["high"].iloc[i+1] and df["high"].iloc[i] > df["high"].iloc[i+2]:
                pivots.append(("resistance", float(df["high"].iloc[i])))

        if side == "LONG":
            supports = [p[1] for p in pivots if p[0] == "support" and p[1] < price]
            if supports:
                supports.sort(reverse=True)
                return supports[0]  # nearest support below
        else:
            resists = [p[1] for p in pivots if p[0] == "resistance" and p[1] > price]
            if resists:
                resists.sort()
                return resists[0]  # nearest resistance above

        return None
    except Exception:
        return None


# ════════════════════════════════════════════════════════════
# CONFLUENCE — Tìm vùng hội tụ
# ════════════════════════════════════════════════════════════
def _find_confluence(levels: dict, side: str, price: float) -> Tuple[float, int, str]:
    """
    Tìm vùng giá mà nhiều phương pháp cùng chỉ ra.
    Dùng clustering: nhóm các level gần nhau (trong 0.3% range).
    Chọn cluster có nhiều level nhất = entry tối ưu.
    """
    if not levels:
        # Fallback: offset 0.15%
        if side == "LONG":
            return price * 0.9985, 0, "no_levels_fallback"
        else:
            return price * 1.0015, 0, "no_levels_fallback"

    all_levels = list(levels.values())
    all_names = list(levels.keys())

    # Filter: chỉ giữ level đúng hướng
    if side == "LONG":
        valid = [(name, lvl) for name, lvl in zip(all_names, all_levels)
                 if lvl < price and lvl > price * 0.97]  # max 3% dưới
    else:
        valid = [(name, lvl) for name, lvl in zip(all_names, all_levels)
                 if lvl > price and lvl < price * 1.03]  # max 3% trên

    if not valid:
        # Không có level nào đúng hướng → dùng offset nhỏ
        if side == "LONG":
            entry = price * 0.998  # 0.2% dưới price
        else:
            entry = price * 1.002  # 0.2% trên price
        return entry, 1, "offset_only"

    # Clustering: nhóm các level trong 0.3% range
    cluster_range = price * 0.003  # 0.3%
    valid.sort(key=lambda x: x[1])

    best_cluster = []
    best_cluster_price = 0

    for i, (name, lvl) in enumerate(valid):
        cluster = [(name, lvl)]
        for j, (n2, l2) in enumerate(valid):
            if i != j and abs(l2 - lvl) <= cluster_range:
                cluster.append((n2, l2))
        if len(cluster) > len(best_cluster):
            best_cluster = cluster
            # Entry = average of cluster
            best_cluster_price = sum(l for _, l in cluster) / len(cluster)

    confluence_score = len(best_cluster)
    methods = [n for n, _ in best_cluster]
    method_str = "+".join(methods[:4])

    # Nếu chỉ 1 level, chọn gần price nhất
    if confluence_score == 1:
        best_cluster_price = valid[0][1] if side == "LONG" else valid[-1][1]
        method_str = valid[0][0]

    return best_cluster_price, confluence_score, method_str


# ════════════════════════════════════════════════════════════
# PLACE ORDER
# ════════════════════════════════════════════════════════════
def place_smart_order(exchange, symbol: str, side: str, qty: float,
                       entry_info: dict, config) -> dict:
    """
    Đặt lệnh thông minh:
    - Confluence >= 2 + improvement >= 0.03%: LIMIT order
    - Else: MARKET order
    """
    leverage = getattr(config, "LEVERAGE", 10)
    try:
        exchange.set_leverage(symbol, leverage)
    except:
        pass

    order_side = "BUY" if side == "LONG" else "SELL"
    close_side = "SELL" if side == "LONG" else "BUY"
    entry_price = entry_info["entry_price"]
    sl = entry_info["sl"]
    tp = entry_info["tp"]

    if entry_info["use_limit"]:
        try:
            result = exchange._post("/fapi/v1/order", {
                "symbol": symbol,
                "side": order_side,
                "type": "LIMIT",
                "quantity": qty,
                "price": exchange._round_price(entry_price),
                "timeInForce": "GTC",
            })
            logger.info(f"[SmartEntry] LIMIT: {side} {symbol} @ {entry_price}")

            import time
            time.sleep(1)
            try:
                exchange.place_stop_loss_order(symbol, close_side, qty, sl)
            except Exception as e:
                logger.warning(f"[SmartEntry] SL pre-place: {e}")
            try:
                exchange.place_take_profit_order(symbol, close_side, qty, tp)
            except Exception as e:
                logger.warning(f"[SmartEntry] TP pre-place: {e}")

            return {"filled": False, "price": entry_price, "type": "LIMIT",
                    "sl": sl, "tp": tp}
        except Exception as e:
            logger.warning(f"[SmartEntry] LIMIT failed → MARKET: {e}")

    # MARKET order
    exchange.place_market_order(symbol, order_side, qty)
    actual_price = exchange.get_ticker_price(symbol)

    import time
    time.sleep(1)
    try:
        exchange.place_stop_loss_order(symbol, close_side, qty, sl)
    except Exception as e:
        logger.warning(f"[SmartEntry] SL: {e}")
    try:
        exchange.place_take_profit_order(symbol, close_side, qty, tp)
    except Exception as e:
        logger.warning(f"[SmartEntry] TP: {e}")

    return {"filled": True, "price": actual_price, "type": "MARKET",
            "sl": sl, "tp": tp}


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════
def _fallback_entry(exchange, symbol, side):
    """Fallback khi analysis fails."""
    try:
        price = exchange.get_ticker_price(symbol)
    except:
        price = 0
    atr_est = price * 0.01
    if side == "LONG":
        entry = price * 0.998
        sl = round(price - atr_est * 2, _price_dec(price))
        tp = round(price + atr_est * 4, _price_dec(price))
    else:
        entry = price * 1.002
        sl = round(price + atr_est * 2, _price_dec(price))
        tp = round(price - atr_est * 4, _price_dec(price))
    return {
        "entry_price": round(entry, _price_dec(price)),
        "current_price": price,
        "improvement_pct": 0.2,
        "method": "fallback",
        "sl": sl, "tp": tp,
        "use_limit": False,
        "confluence_score": 0,
        "levels": {},
    }


def _to_df(klines):
    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def _calc_atr(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    return float(tr.ewm(com=period - 1, min_periods=period).mean().iloc[-1])


def _price_dec(price):
    if price >= 10000: return 1
    if price >= 1000: return 2
    if price >= 10: return 2
    if price >= 1: return 4
    return 5
