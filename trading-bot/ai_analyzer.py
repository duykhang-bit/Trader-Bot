# ============================================================
# AI ANALYZER — Dùng TradingAgents-main (LLM thật) để phân tích bias
# Fallback: Groq → Gemini → DeepSeek
# Chạy 1-2 lần/ngày, kết quả lưu ai_bias.json
# ============================================================
import json
import logging
import os
import sys
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# Path tới TradingAgents-main
TRADING_AGENTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "TradingAgents-main"
)

# File output
BIAS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "ai_bias.json"
)

# Map Binance symbol → TradingAgents ticker
SYMBOL_MAP = {
    "BTCUSDT":  "BTC-USD",
    "ETHUSDT":  "ETH-USD",
    "SOLUSDT":  "SOL-USD",
    "BNBUSDT":  "BNB-USD",
    "XRPUSDT":  "XRP-USD",
    "DOGEUSDT": "DOGE-USD",
    "ADAUSDT":  "ADA-USD",
    "AVAXUSDT": "AVAX-USD",
    "LINKUSDT": "LINK-USD",
    "DOTUSDT":  "DOT-USD",
    "NEARUSDT": "NEAR-USD",
    "HYPEUSDT": "HYPE-USD",
    "SPCXUSDT": "SPCX-USD",
    "ZECUSDT":  "ZEC-USD",
    "TLMUSDT":  "TLM-USD",
    "VANRYUSDT":"VANRY-USD",
    "AIAUSDT":  "AIA-USD",
    "DEXEUSDT": "DEXE-USD",
    "ONUSDT":   "ON-USD",
    "BEATUSDT": "BEAT-USD",
    "UBUSDT":   "UB-USD",
    "BOTUSDT":  "BOT-USD",
}

# Thứ tự fallback provider
LLM_PROVIDERS = [
    {
        "name":       "groq",
        "provider":   "groq",
        "deep_model": "llama-3.3-70b-versatile",
        "fast_model": "llama-3.1-8b-instant",
        "env_key":    "GROQ_API_KEY",
    },
    {
        "name":       "gemini",
        "provider":   "google",
        "deep_model": "gemini-2.0-flash",
        "fast_model": "gemini-2.0-flash",
        "env_key":    "GOOGLE_API_KEY",
    },
    {
        "name":       "deepseek",
        "provider":   "deepseek",
        "deep_model": "deepseek-reasoner",
        "fast_model": "deepseek-chat",
        "env_key":    "DEEPSEEK_API_KEY",
    },
]

# Map TradingAgents decision → bias
DECISION_MAP = {
    "Buy":         "LONG",
    "Overweight":  "LONG",
    "Hold":        "HOLD",
    "Underweight": "SHORT",
    "Sell":        "SHORT",
}


def _load_env():
    """Load .env từ TradingAgents-main."""
    env_file = os.path.join(TRADING_AGENTS_DIR, ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if v.strip():
                        os.environ.setdefault(k.strip(), v.strip())


def _get_active_provider() -> dict:
    """Chọn provider đầu tiên có API key."""
    _load_env()
    for p in LLM_PROVIDERS:
        key = os.environ.get(p["env_key"], "").strip()
        if key:
            logger.info(f"[AI] Using provider: {p['name']}")
            return p
    logger.warning("[AI] No LLM API key found, fallback to indicators")
    return None


def _setup_tradingagents(provider: dict):
    """Khởi tạo TradingAgentsGraph với provider được chọn."""
    if TRADING_AGENTS_DIR not in sys.path:
        sys.path.insert(0, TRADING_AGENTS_DIR)

    os.environ["TRADINGAGENTS_LLM_PROVIDER"]    = provider["provider"]
    os.environ["TRADINGAGENTS_DEEP_THINK_LLM"]  = provider["deep_model"]
    os.environ["TRADINGAGENTS_QUICK_THINK_LLM"] = provider["fast_model"]
    os.environ["TRADINGAGENTS_MAX_DEBATE_ROUNDS"] = "1"
    os.environ["TRADINGAGENTS_MAX_RISK_ROUNDS"]   = "1"
    os.environ["TRADINGAGENTS_CHECKPOINT_ENABLED"] = "false"
    os.environ["TRADINGAGENTS_OUTPUT_LANGUAGE"]   = "English"

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    config = DEFAULT_CONFIG.copy()
    return TradingAgentsGraph(debug=False, config=config)


def _parse_decision(decision_text: str) -> str:
    """Trích xuất Buy/Sell/Hold từ output TradingAgents."""
    if not decision_text:
        return "HOLD"
    text = str(decision_text).lower()
    if any(w in text for w in ["buy", "overweight", "bullish", "long"]):
        return "LONG"
    if any(w in text for w in ["sell", "underweight", "bearish", "short"]):
        return "SHORT"
    return "HOLD"


def analyze_coin_llm(ticker: str, date_str: str, ta_graph) -> dict:
    """Phân tích 1 coin dùng TradingAgents LLM."""
    try:
        start = time.time()
        _, decision = ta_graph.propagate(ticker, date_str)
        elapsed = round(time.time() - start, 1)
        bias = _parse_decision(str(decision))
        return {
            "ticker":    ticker,
            "bias":      bias,
            "decision":  str(decision)[:300],
            "reason":    f"LLM analysis ({elapsed}s)",
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"analyze_coin_llm failed {ticker}: {e}")
        raise


def analyze_coin_indicators(symbol: str) -> dict:
    """Fallback: phân tích bằng indicators (Binance API)."""
    ticker = SYMBOL_MAP.get(symbol, symbol)
    try:
        import requests
        import pandas as pd
        from indicators import compute_signal_score

        def _fetch(sym, interval, limit=100):
            r = requests.get(
                "https://fapi.binance.com/fapi/v1/klines",
                params={"symbol": sym, "interval": interval, "limit": limit},
                timeout=10
            )
            r.raise_for_status()
            df = pd.DataFrame(r.json(), columns=[
                "open_time","open","high","low","close","volume",
                "close_time","quote_volume","trades",
                "taker_buy_base","taker_buy_quote","ignore"
            ])
            for c in ["open","high","low","close","volume"]:
                df[c] = df[c].astype(float)
            return df

        df_15m = _fetch(symbol, "15m")
        df_1h  = _fetch(symbol, "1h",  50)
        df_4h  = _fetch(symbol, "4h",  50)
        css    = compute_signal_score(df_15m, df_1h, df_4h)
        signal = css["signal"]
        bias   = signal if signal != "WAIT" else "HOLD"
        return {
            "ticker":    ticker,
            "bias":      bias,
            "decision":  f"WR={css['win_rate']:.0f}%",
            "reason":    "indicators fallback",
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        return {
            "ticker":    ticker,
            "bias":      "HOLD",
            "decision":  "Error",
            "reason":    str(e)[:200],
            "timestamp": datetime.now().isoformat(),
        }


def analyze_all(symbols: list, date_str: str = None) -> dict:
    """
    Phân tích tất cả coin.
    - Dùng LLM (Groq/Gemini/DeepSeek) nếu có API key
    - Fallback indicators nếu không có key hoặc LLM lỗi
    Ghi kết quả ra ai_bias.json.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    results = {}
    provider = _get_active_provider()
    ta_graph = None

    # Thử khởi tạo LLM
    if provider:
        try:
            ta_graph = _setup_tradingagents(provider)
            logger.info(f"[AI] TradingAgents ready: {provider['name']}")
        except Exception as e:
            logger.error(f"[AI] Setup failed ({provider['name']}): {e}")
            # Thử provider tiếp theo
            for p in LLM_PROVIDERS:
                if p["name"] == provider["name"]:
                    continue
                key = os.environ.get(p["env_key"], "").strip()
                if not key:
                    continue
                try:
                    provider = p
                    ta_graph = _setup_tradingagents(p)
                    logger.info(f"[AI] Fallback to: {p['name']}")
                    break
                except Exception as e2:
                    logger.error(f"[AI] Fallback {p['name']} failed: {e2}")

    for sym in symbols:
        ticker = SYMBOL_MAP.get(sym, sym)
        print(f"\n{'='*50}")
        print(f"🧠 Analyzing {ticker} ({sym})...")
        print(f"{'='*50}")

        if ta_graph:
            try:
                result = analyze_coin_llm(ticker, date_str, ta_graph)
            except Exception:
                logger.warning(f"[AI] LLM failed for {sym}, using indicators")
                result = analyze_coin_indicators(sym)
        else:
            result = analyze_coin_indicators(sym)

        icon = "🟢" if result["bias"] == "LONG" else ("🔴" if result["bias"] == "SHORT" else "⚪")
        print(f"{icon} {sym}: {result['bias']} ({result['reason']})")
        results[sym] = result

    # Lưu file
    output = {
        "analyzed_at": datetime.now().isoformat(),
        "date":        date_str,
        "provider":    provider["name"] if provider else "indicators",
        "coins":       results,
    }
    with open(BIAS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Saved to: {BIAS_FILE}")
    return results


def load_bias() -> dict:
    """Đọc bias từ ai_bias.json. Hết hạn sau 8 tiếng."""
    if not os.path.exists(BIAS_FILE):
        return {}
    try:
        with open(BIAS_FILE) as f:
            data = json.load(f)
        analyzed_at = datetime.fromisoformat(data["analyzed_at"])
        age_hours = (datetime.now() - analyzed_at).total_seconds() / 3600
        if age_hours > 8:
            # Chỉ log 1 lần mỗi giờ, không spam mỗi 2s
            _last_warn = getattr(load_bias, '_last_warn', 0)
            import time as _t
            if _t.time() - _last_warn > 3600:
                logger.debug(f"AI bias expired ({age_hours:.1f}h) — chạy AI analyze để làm mới")
                load_bias._last_warn = _t.time()
            return {}
        return {sym: info["bias"] for sym, info in data.get("coins", {}).items()}
    except Exception as e:
        logger.error(f"load_bias error: {e}")
        return {}


# ── Standalone ────────────────────────────────────────────────
if __name__ == "__main__":
    import config as bot_config
    coins = getattr(bot_config, "FIXED_COINS", ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    # Chỉ phân tích top 5 để tiết kiệm quota
    coins = coins[:5]
    print(f"🧠 AI Analysis for: {coins}")
    results = analyze_all(coins)
    print(f"\n📋 SUMMARY:")
    for sym, info in results.items():
        icon = "🟢" if info["bias"] == "LONG" else ("🔴" if info["bias"] == "SHORT" else "⚪")
        print(f"  {icon} {sym:<12} → {info['bias']:<6} ({info['decision'][:50]})")
