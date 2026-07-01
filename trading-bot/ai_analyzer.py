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
    Chạy TradingAgents cho 1 coin.
    Trả về: {"ticker": "BTC-USD", "bias": "LONG/SHORT/HOLD", "decision": "Buy/...", "reason": "..."}
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # Thêm TradingAgents vào sys.path
    if TRADING_AGENTS_DIR not in sys.path:
        sys.path.insert(0, TRADING_AGENTS_DIR)

    # Load .env từ TradingAgents
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(TRADING_AGENTS_DIR, ".env"))
    except ImportError:
        pass

    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = "google"
        config["deep_think_llm"] = "gemini-2.0-flash-thinking-exp"
        config["quick_think_llm"] = "gemini-2.0-flash"
        config["max_debate_rounds"] = 1
        config["max_risk_discuss_rounds"] = 1

        ta = TradingAgentsGraph(debug=False, config=config)
        final_state, decision = ta.propagate(ticker, date_str, asset_type="crypto")

        # decision = "Buy" / "Overweight" / "Hold" / "Underweight" / "Sell"
        bias = DECISION_MAP.get(decision, "HOLD")

        # Lấy reason từ final_trade_decision
        reason = final_state.get("final_trade_decision", "")[:500]

        return {
            "ticker": ticker,
            "bias": bias,
            "decision": decision,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"AI Analyzer failed for {ticker}: {e}")
        return {
            "ticker": ticker,
            "bias": "HOLD",  # Default HOLD nếu lỗi
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
