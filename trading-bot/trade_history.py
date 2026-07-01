# ============================================================
# TRADE HISTORY — Lưu/load lịch sử lệnh vào file JSON
# ============================================================
import json
import os
from datetime import datetime

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "logs", "trade_history.json")


def load_history() -> list:
    """Load lịch sử từ file"""
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_history(trade_log: list):
    """Lưu lịch sử vào file"""
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "w") as f:
            json.dump(trade_log, f, indent=2, ensure_ascii=False)
    except Exception as e:
        pass


def get_stats(trade_log: list) -> dict:
    """Tính thống kê từ lịch sử"""
    closed = [t for t in trade_log if t.get("status") == "CLOSED"]
    wins   = sum(1 for t in closed if t.get("pnl_usdt", 0) > 0)
    losses = len(closed) - wins
    total  = sum(t.get("pnl_usdt", 0) for t in closed)
    wr     = wins / len(closed) * 100 if closed else 0
    return {
        "total":     len(closed),
        "wins":      wins,
        "losses":    losses,
        "winrate":   round(wr, 1),
        "total_pnl": round(total, 2),
    }
