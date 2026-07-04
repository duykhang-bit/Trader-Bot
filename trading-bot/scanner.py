# ============================================================
# MULTI-COIN SCANNER — Tự quét coin, chọn cái tốt nhất vào lệnh
# ============================================================
import logging
import requests
import pandas as pd
from dataclasses import dataclass
from typing import Optional, List
from indicators import get_signal, calculate_rsi, calculate_ema, calculate_atr, get_mtf_trend, is_volatile_coin, get_pullback_signal

logger = logging.getLogger(__name__)

# Fallback list nếu không fetch được từ Binance
_FALLBACK_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "LTCUSDT", "BCHUSDT", "ETCUSDT", "XLMUSDT", "ATOMUSDT",
    "UNIUSDT", "AAVEUSDT", "NEARUSDT", "APTUSDT", "SUIUSDT",
    "ARBUSDT", "OPUSDT", "INJUSDT", "SEIUSDT", "RUNEUSDT",
    "FETUSDT", "WLDUSDT", "TAOUSDT", "RENDERUSDT", "LABUSDT",
]

# Coin ưu tiên — luôn scan đầu tiên, bonus +10 điểm
PRIORITY_COINS = [
    "LABUSDT",
]

def fetch_dynamic_watchlist(base_url: str = "https://testnet.binancefuture.com",
                             top_n: int = 80) -> List[str]:
    """
    Lấy danh sách coin từ Binance Futures:
    - Chỉ lấy USDT pairs
    - Sắp xếp theo volume 24h (coin hot nhất lên đầu)
    - Ưu tiên top gainers (tăng giá mạnh nhất)
    - Cập nhật mỗi lần gọi → luôn có coin mới
    """
    try:
        resp = requests.get(f"{base_url}/fapi/v1/ticker/24hr", timeout=10)
        resp.raise_for_status()
        tickers = resp.json()

        # Chỉ lấy USDT pairs, loại stablecoin
        exclude = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDTUSDT"}
        usdt_pairs = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and t["symbol"] not in exclude
            and float(t.get("quoteVolume", 0)) > 1_000_000  # volume > $1M
        ]

        # Tính điểm ưu tiên: kết hợp volume + price change
        def priority(t):
            vol   = float(t.get("quoteVolume", 0))
            chg   = float(t.get("priceChangePercent", 0))
            # Ưu tiên coin tăng mạnh (chg > 3%) và volume cao
            bonus = 2.0 if chg > 5 else (1.5 if chg > 3 else 1.0)
            return vol * bonus

        usdt_pairs.sort(key=priority, reverse=True)
        symbols = [t["symbol"] for t in usdt_pairs[:top_n]]

        logger.info(f"📡 Dynamic watchlist: {len(symbols)} coins fetched from Binance")

        # Log top gainers
        gainers = sorted(usdt_pairs[:top_n],
                         key=lambda t: float(t.get("priceChangePercent", 0)),
                         reverse=True)[:5]
        for g in gainers:
            logger.info(f"  🚀 {g['symbol']}: +{float(g['priceChangePercent']):.1f}% | vol=${float(g['quoteVolume'])/1e6:.0f}M")

        return symbols

    except Exception as e:
        logger.warning(f"fetch_dynamic_watchlist failed: {e} — using fallback")
        return _FALLBACK_WATCHLIST.copy()


# WATCHLIST được load lúc khởi động
# - Nếu config.WATCHLIST_MODE = "fixed" → dùng config.FIXED_COINS
# - Nếu config.WATCHLIST_MODE = "dynamic" → fetch từ Binance theo volume
def _load_initial_watchlist() -> List[str]:
    try:
        import config as _cfg
        if getattr(_cfg, "WATCHLIST_MODE", "dynamic") == "fixed":
            coins = list(getattr(_cfg, "FIXED_COINS", _FALLBACK_WATCHLIST))
            logger.info(f"📌 Fixed watchlist: {coins}")
            return coins
    except Exception:
        pass
    return fetch_dynamic_watchlist()

WATCHLIST = _load_initial_watchlist()
_watchlist_last_update = 0

# Active universe: top 10 coin được lọc mỗi 5 phút để scan nhanh
_active_universe: List[str] = []
_universe_last_update = 0

def get_watchlist(base_url: str = "https://testnet.binancefuture.com") -> List[str]:
    """Trả về WATCHLIST. Fixed mode: dùng FIXED_COINS từ config. Dynamic mode: refresh mỗi 30 phút."""
    import time
    global WATCHLIST, _watchlist_last_update

    try:
        import config as _cfg
        if getattr(_cfg, "WATCHLIST_MODE", "dynamic") == "fixed":
            return list(getattr(_cfg, "FIXED_COINS", WATCHLIST))
    except Exception:
        pass

    if time.time() - _watchlist_last_update > 1800:  # 30 phút
        WATCHLIST = fetch_dynamic_watchlist(base_url)
        _watchlist_last_update = time.time()
    # Đảm bảo priority coins luôn có trong list và đứng đầu
    result = list(PRIORITY_COINS)
    for s in WATCHLIST:
        if s not in result:
            result.append(s)
    return result


def get_active_universe(base_url: str = "https://testnet.binancefuture.com",
                        top_n: int = 10) -> List[str]:
    """
    Fixed mode: trả về FIXED_COINS từ config.
    Dynamic mode: lọc top N coin theo volume + biến động, refresh mỗi 3 phút.
    """
    import time
    global _active_universe, _universe_last_update

    # Fixed mode → trả thẳng, không fetch Binance
    try:
        import config as _cfg
        if getattr(_cfg, "WATCHLIST_MODE", "dynamic") == "fixed":
            coins = list(getattr(_cfg, "FIXED_COINS", WATCHLIST))
            logger.info(f"🎯 Active universe (fixed): {coins}")
            return coins
    except Exception:
        pass

    if time.time() - _universe_last_update < 180 and _active_universe:  # 3 phút cache
        return _active_universe

    try:
        resp = requests.get(f"{base_url}/fapi/v1/ticker/24hr", timeout=10)
        resp.raise_for_status()
        tickers = resp.json()

        exclude = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDTUSDT"}
        usdt_pairs = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and t["symbol"] not in exclude
            and float(t.get("quoteVolume", 0)) > 5_000_000  # volume > $5M
        ]

        # Score: volume × |price_change| × ATR proxy
        def universe_score(t):
            vol    = float(t.get("quoteVolume", 0))
            chg    = abs(float(t.get("priceChangePercent", 0)))
            high   = float(t.get("highPrice", 1))
            low    = float(t.get("lowPrice", 1))
            spread = (high - low) / low * 100  # daily range %
            return vol * (1 + chg/10) * (1 + spread/10)

        usdt_pairs.sort(key=universe_score, reverse=True)
        top_symbols = [t["symbol"] for t in usdt_pairs[:top_n]]

        # Luôn có priority coins
        result = list(PRIORITY_COINS)
        for s in top_symbols:
            if s not in result:
                result.append(s)
        result = result[:top_n + len(PRIORITY_COINS)]

        _active_universe = result
        _universe_last_update = time.time()

        logger.info(f"🎯 Active universe updated: {result}")
        return result

    except Exception as e:
        logger.warning(f"get_active_universe failed: {e}")
        return _active_universe if _active_universe else get_watchlist(base_url)[:top_n]


@dataclass
class CoinScore:
    symbol: str
    signal: str          # LONG / SHORT / HOLD
    score: float         # 0-100, càng cao càng tốt
    rsi: float
    trend: str           # BULLISH / BEARISH / NEUTRAL
    atr_pct: float       # ATR % — đo độ biến động
    reason: str          # Lý do vào lệnh


def score_coin(symbol: str, df: pd.DataFrame, config) -> Optional[CoinScore]:
    """
    Chấm điểm 1 coin dựa trên nhiều tiêu chí:
    - Signal strength (RSI, EMA, MACD)
    - Trend alignment
    - Volatility (ATR)
    - Volume
    """
    try:
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # Tính indicators
        rsi = calculate_rsi(close, config.RSI_PERIOD).iloc[-1]
        ema_fast = calculate_ema(close, config.EMA_FAST).iloc[-1]
        ema_slow = calculate_ema(close, config.EMA_SLOW).iloc[-1]
        ema_trend = calculate_ema(close, config.EMA_TREND).iloc[-1]
        atr = calculate_atr(high, low, close).iloc[-1]
        current_price = close.iloc[-1]
        atr_pct = (atr / current_price) * 100

        # Volume surge: volume hiện tại so với trung bình 20 nến
        vol_avg = volume.rolling(20).mean().iloc[-1]
        vol_ratio = volume.iloc[-1] / vol_avg if vol_avg > 0 else 1.0

        # Lấy signal — pass symbol để chọn đúng strategy
        signal = get_signal(df, config, symbol=symbol)

        if signal == "HOLD":
            return None

        # Filter entry quality: chỉ vào lệnh ở vùng giá tốt
        # SHORT: chỉ short khi RSI >= 40
        if signal == "SHORT" and rsi < 40:
            return None
        if signal == "LONG" and rsi > 60:
            return None

        # Kiểm tra giá đang gần recent high/low (20 nến)
        recent_high = high.rolling(20).max().iloc[-1]
        recent_low  = low.rolling(20).min().iloc[-1]
        price_range = recent_high - recent_low
        if price_range > 0:
            price_pos = (current_price - recent_low) / price_range
            if signal == "SHORT" and price_pos < 0.4:
                return None
            if signal == "LONG" and price_pos > 0.6:
                return None

        # --- Chấm điểm ---
        score = 0.0
        reasons = []

        # 1. RSI strength (30 điểm)
        if signal == "LONG":
            # RSI càng gần oversold càng tốt khi vừa thoát ra
            rsi_score = max(0, (50 - rsi) / 15 * 30) if rsi < 50 else 10
        else:
            rsi_score = max(0, (rsi - 50) / 15 * 30) if rsi > 50 else 10
        score += min(rsi_score, 30)
        reasons.append(f"RSI={rsi:.1f}")

        # 2. Trend alignment (25 điểm)
        if signal == "LONG" and current_price > ema_trend:
            score += 25
            trend = "BULLISH"
            reasons.append("Trend↑")
        elif signal == "SHORT" and current_price < ema_trend:
            score += 25
            trend = "BEARISH"
            reasons.append("Trend↓")
        else:
            trend = "NEUTRAL"

        # 3. EMA alignment (20 điểm)
        if signal == "LONG" and ema_fast > ema_slow:
            score += 20
            reasons.append("EMA cross↑")
        elif signal == "SHORT" and ema_fast < ema_slow:
            score += 20
            reasons.append("EMA cross↓")

        # 4. Volume surge (15 điểm)
        if vol_ratio >= 2.0:
            score += 15
            reasons.append(f"Vol×{vol_ratio:.1f}")
        elif vol_ratio >= 1.5:
            score += 10
            reasons.append(f"Vol×{vol_ratio:.1f}")
        elif vol_ratio >= 1.2:
            score += 5

        # 5. ATR volatility (10 điểm) — cần đủ biến động để có lợi nhuận
        if 1.5 <= atr_pct <= 5.0:
            score += 10
            reasons.append(f"ATR={atr_pct:.1f}%")
        elif atr_pct > 5.0:
            score += 5  # Quá volatile thì trừ điểm

        # Bonus điểm cho priority coins
        if symbol in PRIORITY_COINS:
            score = min(score + 10, 100)
            reasons.append("⭐PRIORITY")

        return CoinScore(
            symbol=symbol,
            signal=signal,
            score=round(score, 1),
            rsi=round(rsi, 1),
            trend=trend,
            atr_pct=round(atr_pct, 2),
            reason=" | ".join(reasons)
        )

    except Exception as e:
        logger.debug(f"Score failed for {symbol}: {e}")
        return None


def scan_market(exchange, config, min_score: float = 40.0) -> Optional[CoinScore]:
    """
    Quét WATCHLIST động từ Binance (refresh mỗi 30 phút).
    Tối ưu tốc độ: 
    - Bước 1: Quick scan 15m cho tất cả coin (~1 API call/coin)
    - Bước 2: MTF full (4h/1h/15m/1m) chỉ cho top 15 coin có score cao nhất
    """
    base_url = getattr(config, "LIVE_BASE_URL", "https://demo-fapi.binance.com")

    # Dùng active universe (top 10-12 coin) thay vì scan hết 80 coin
    active = get_active_universe(base_url, top_n=10)
    logger.info(f"🔍 Scanning {len(active)} coins (active universe)...")
    candidates = []

    # Quick scan + MTF trực tiếp trên active universe (đã nhỏ, không cần 2 bước)
    quick_scores = []
    for symbol in active:
        try:
            klines_15m = exchange.get_klines(symbol, "15m", limit=100)
            df_15m = _klines_to_df(klines_15m)
            scored = score_coin(symbol, df_15m, config)
            if scored and scored.score >= 25:
                quick_scores.append(scored)
        except Exception as e:
            logger.debug(f"  ⚠️  {symbol} quick skip: {e}")

    if not quick_scores:
        logger.info("  No candidates in active universe.")
        scan_market._last_candidates = []
        return None

    logger.info(f"  Active: {len(quick_scores)} candidates → MTF check all")
    top15 = sorted(quick_scores, key=lambda x: x.score, reverse=True)

    # ── Bước 2: MTF full cho top 20 ─────────────────────────────────
    for scored in top15:
        symbol = scored.symbol
        try:
            klines_1h  = exchange.get_klines(symbol, "1h",  limit=100)
            klines_4h  = exchange.get_klines(symbol, "4h",  limit=100)
            klines_1m  = exchange.get_klines(symbol, "1m",  limit=60)
            klines_15m = exchange.get_klines(symbol, "15m", limit=100)

            df_1h  = _klines_to_df(klines_1h)
            df_4h  = _klines_to_df(klines_4h)
            df_1m  = _klines_to_df(klines_1m)
            df_15m = _klines_to_df(klines_15m)

            mtf = get_mtf_trend(df_4h, df_1h, df_15m, df_1m)

            # Detect coin volatile (dao động mạnh)
            volatile = is_volatile_coin(df_1h, threshold_pct=4.0)

            if volatile and mtf["bias"] != "NEUTRAL":
                # Dùng pullback strategy trên 5m (dùng 15m thay thế)
                pb_signal = get_pullback_signal(df_15m, config, mtf["bias"])
                if pb_signal != "HOLD":
                    final_score = min(scored.score + 20, 100)  # bonus lớn hơn vì volatile
                    final = CoinScore(
                        symbol=symbol,
                        signal=pb_signal,
                        score=final_score,
                        rsi=scored.rsi,
                        trend=scored.trend,
                        atr_pct=scored.atr_pct,
                        reason=scored.reason + f" | 🔥VOLATILE PULLBACK {mtf['detail']}"
                    )
                    if final.score >= min_score:
                        candidates.append(final)
                        logger.info(f"  🔥 {symbol}: {pb_signal} PULLBACK score={final.score} | {final.reason}")
                    continue

            # Chấp nhận cả MEDIUM (2/4 khung) thay vì chỉ STRONG
            if mtf["bias"] == "NEUTRAL":
                logger.debug(f"  ⏭  {symbol} MTF neutral: {mtf['detail']}")
                continue

            if scored.signal != mtf["bias"]:
                logger.debug(f"  ⏭  {symbol} signal≠MTF: {scored.signal} vs {mtf['bias']}")
                continue

            # Bonus điểm theo MTF strength
            bonus = 15 if mtf["strength"] == "STRONG" else (8 if mtf["strength"] == "MEDIUM" else 3)
            final_score = min(scored.score + bonus, 100)
            mtf_tag = "MTF✅" if mtf["strength"] == "STRONG" else "MTF⚡"
            final = CoinScore(
                symbol=symbol,
                signal=scored.signal,
                score=final_score,
                rsi=scored.rsi,
                trend=scored.trend,
                atr_pct=scored.atr_pct,
                reason=scored.reason + f" | {mtf_tag} {mtf['detail']}"
            )

            if final.score >= min_score:
                candidates.append(final)
                logger.info(f"  ✅ {symbol}: {final.signal} score={final.score} | {final.reason}")
            else:
                logger.info(f"  📊 {symbol}: {final.signal} score={final.score} (below {min_score})")

        except Exception as e:
            logger.debug(f"  ⚠️  {symbol} MTF skip: {e}")

    # Lưu lại để dashboard hiển thị
    scan_market._last_candidates = sorted(candidates, key=lambda x: x.score, reverse=True)

    if not candidates:
        logger.info("  No strong signals found.")
        return None

    # Chọn coin có điểm cao nhất
    best = max(candidates, key=lambda x: x.score)
    logger.info(f"🏆 Best: {best.symbol} | {best.signal} | Score={best.score}")
    return best


scan_market._last_candidates = []


def _klines_to_df(klines: list) -> pd.DataFrame:
    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df
# ///Copy paste 2 lệnh này vào Terminal:

# Lệnh 1:

# cd /Users/leduykhang/Documents/AI-CONTEXT-V2-master/AI-CONTEXT-V2-master/trading-bot
# Lệnh 2:



# python3 bot.py
# ╔══════════════════════════════════════════════════════════╗
# ║         🤖  MULTI-COIN BOT — BINANCE FUTURES             ║
# ╠══════════════════════════════════════════════════════════╣
# ║  🕐 11:30:00   💼 Balance: $4,963.53 USDT                ║
# ║  📈 Tổng PnL: +$0.00   ✅0 win  ❌0 loss  |  Scan #1    ║
# ╠══════════════════════════════════════════════════════════╣
# ║      💤  CHƯA CÓ LỆNH — Đang quét thị trường...         ║
# ╠══════════════════════════════════════════════════════════╣
# ║                    💹  GIÁ REALTIME                      ║
# ║  BTC  $ 81,012  ETH  $  2,307  BNB  $   662  SOL  $  96 ║
# ...
# ╠══════════════════════════════════════════════════════════╣
# ║                   📋  LỊCH SỬ LỆNH                       ║
# ║  Chưa có lệnh nào được đóng                              ║
# ╚══════════════════════════════════════════════════════════╝
# ╠══════════════════════════════════════════════════════════╣
# ║           📌  LỆNH ĐANG MỞ (REALTIME)                    ║
# ║  Coin     : SOLUSDT   🟢 LONG   5x                       ║
# ║  Entry    : $96.2300  ▶  Giá HT: $97.8500                ║
# ║  🛑 SL    : $93.8000   (còn 2.09% đến SL)                ║
# ║  🎯 TP    : $100.850   (còn 3.07% đến TP)                ║
# ║  📦 Qty   : 10.3   (~$991.17 USDT)                       ║
# ║  Progress : [████░░░░░░] 40%                             ║
# ║  📈 PnL   : +$83.50  (+1.68%)  x5                        ║
# ╠══════════════════════════════════════════════════════════╣
# ║                   📋  LỊCH SỬ LỆNH                       ║
# ║  Tổng: 3  ✅2 win  ❌1 loss  |  PnL: +$124.30            ║
# ║  ──────────────────────────────────────────────          ║
# ║  #  Coin       Side  Entry    Close    PnL$     %   Giờ  ║
# ║  ──────────────────────────────────────────────          ║
# ║  ✅1 SOLUSDT   LONG  $94.230  $98.450  +$86.50 +4.5% 10:32║
# ║  ❌2 BTCUSDT   SHORT $81200   $81850   -$33.20 -0.8% 09:15║
# ║  ✅3 SUIUSDT   LONG  $1.2800  $1.3500  +$71.00 +5.5% 08:44║
# ╚══════════════════════════════════════════════════════════╝
