# ============================================================
# AI ANALYZER — Chạy TradingAgents-main để xác định bias
# (Long/Short/Hold) cho mỗi coin trước khi bot vào lệnh
# ============================================================
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Path tới TradingAgents-main
TRADING_AGENTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "TradingAgents-main"
)

# File output — trading bot đọc file này để biết bias
BIAS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "ai_bias.json"
)

# Map coin symbol (Binance) → TradingAgents ticker format
SYMBOL_MAP = {
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD",
    "SOLUSDT": "SOL-USD",
    "BNBUSDT": "BNB-USD",
    "XRPUSDT": "XRP-USD",
    "DOGEUSDT": "DOGE-USD",
    "ADAUSDT": "ADA-USD",
    "AVAXUSDT": "AVAX-USD",
    "LINKUSDT": "LINK-USD",
    "DOTUSDT": "DOT-USD",
    "NFPUSDT": "NFP-USD",     # có thể không hỗ trợ trên yfinance
}

# Map TradingAgents decision → trading bias
DECISION_MAP = {
    "Buy":         "LONG",
    "Overweight":  "LONG",
    "Hold":        "HOLD",
    "Underweight": "SHORT",
    "Sell":        "SHORT",
}


def analyze_coin(ticker: str, date_str: str = None) -> dict:
    """
    Phân tích bias cho 1 coin dùng indicators (RSI/EMA/MACD/BB/HTF).
    Không dùng tradingagents — hoạt động standalone trên server.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # Tìm symbol từ ticker
    sym = ticker  # fallback
    for s, t in SYMBOL_MAP.items():
        if t == ticker:
            sym = s
            break

    try:
        # Import exchange từ bot state (nếu chạy trong bot)
        # Hoặc dùng requests thẳng tới Binance public API
        import requests
        import pandas as pd

        def _fetch_klines(symbol, interval, limit=100):
            url = "https://fapi.binance.com/fapi/v1/klines"
            r = requests.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
            r.raise_for_status()
            data = r.json()
            df = pd.DataFrame(data, columns=[
                "open_time","open","high","low","close","volume",
                "close_time","quote_volume","trades",
                "taker_buy_base","taker_buy_quote","ignore"
            ])
            for col in ["open","high","low","close","volume"]:
                df[col] = df[col].astype(float)
            return df

        df_15m = _fetch_klines(sym, "15m", 100)
        df_1h  = _fetch_klines(sym, "1h",  50)
        df_4h  = _fetch_klines(sym, "4h",  50)

        from indicators import compute_signal_score
        css = compute_signal_score(df_15m, df_1h, df_4h)

        signal   = css["signal"]   # LONG / SHORT / WAIT
        win_rate = css["win_rate"]
        reasons  = (css["long_reasons"] if signal == "LONG"
                    else css["short_reasons"] if signal == "SHORT"
                    else css["long_reasons"] + css["short_reasons"])

        bias = signal if signal != "WAIT" else "HOLD"

        return {
            "ticker": ticker,
            "bias":   bias,
            "decision": f"WR={win_rate:.0f}%",
            "reason": " | ".join(reasons[:5]),
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"analyze_coin failed for {sym}: {e}")
        return {
            "ticker": ticker,
            "bias": "HOLD",
            "decision": "Error",
            "reason": str(e)[:200],
            "timestamp": datetime.now().isoformat(),
        }


def analyze_all(symbols: list, date_str: str = None) -> dict:
    """
    Phân tích tất cả coin trong danh sách.
    Ghi kết quả ra ai_bias.json.
    Trả về: {symbol: {bias, decision, reason, timestamp}}
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    results = {}
    for sym in symbols:
        ticker = SYMBOL_MAP.get(sym)
        if not ticker:
            logger.warning(f"No ticker mapping for {sym}, skip")
            results[sym] = {
                "ticker": sym,
                "bias": "HOLD",
                "decision": "UnmappedSymbol",
                "reason": f"No yfinance mapping for {sym}",
                "timestamp": datetime.now().isoformat(),
            }
            continue

        print(f"\n{'='*50}")
        print(f"🧠 Analyzing {ticker} ({sym})...")
        print(f"{'='*50}")

        start = time.time()
        result = analyze_coin(ticker, date_str)
        elapsed = time.time() - start

        icon = "🟢" if result["bias"] == "LONG" else ("🔴" if result["bias"] == "SHORT" else "⚪")
        print(f"{icon} {sym}: {result['decision']} → {result['bias']} ({elapsed:.1f}s)")

        results[sym] = result

    # Ghi ra file
    output = {
        "analyzed_at": datetime.now().isoformat(),
        "date": date_str,
        "coins": results,
    }
    with open(BIAS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*50}")
    print(f"✅ Saved to: {BIAS_FILE}")
    print(f"{'='*50}")

    return results


def load_bias() -> dict:
    """
    Đọc bias từ ai_bias.json.
    Trả về: {symbol: "LONG"/"SHORT"/"HOLD"} hoặc {} nếu file không tồn tại/quá cũ.
    """
    if not os.path.exists(BIAS_FILE):
        return {}

    try:
        with open(BIAS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Kiểm tra thời gian — nếu quá 4 giờ thì coi như hết hạn
        analyzed_at = datetime.fromisoformat(data["analyzed_at"])
        age_hours = (datetime.now() - analyzed_at).total_seconds() / 3600
        if age_hours > 4:
            logger.warning(f"AI bias expired ({age_hours:.1f}h old)")
            return {}

        return {sym: info["bias"] for sym, info in data.get("coins", {}).items()}

    except Exception as e:
        logger.error(f"load_bias error: {e}")
        return {}


# ============================================================
# MAIN — chạy standalone: python3 ai_analyzer.py
# ============================================================
if __name__ == "__main__":
    import config as bot_config

    coins = getattr(bot_config, "FIXED_COINS", ["BTCUSDT", "SOLUSDT", "ETHUSDT", "BNBUSDT"])
    print(f"🧠 AI Analysis for: {coins}")
    print(f"📅 Date: {datetime.now().strftime('%Y-%m-%d')}")

    results = analyze_all(coins)

    print(f"\n{'='*50}")
    print("📋 SUMMARY:")
    print(f"{'='*50}")
    for sym, info in results.items():
        icon = "🟢" if info["bias"] == "LONG" else ("🔴" if info["bias"] == "SHORT" else "⚪")
        print(f"  {icon} {sym:<12} → {info['bias']:<6} ({info['decision']})")
