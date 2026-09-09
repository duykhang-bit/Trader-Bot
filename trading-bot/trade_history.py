# ============================================================
# TRADE HISTORY — Lưu/load lịch sử lệnh vào file JSON
# ============================================================
import json
import os
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "logs", "trade_history.json")

# Lock để tránh concurrent write corrupt file
_history_lock = threading.Lock()


def load_history() -> list:
    """Load lịch sử từ file. Nếu corrupt → log warning, trả về []."""
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            else:
                logger.warning(f"[History] File {HISTORY_FILE} không phải list — trả về []")
                return []
    except json.JSONDecodeError as e:
        logger.warning(f"[History] File bị corrupt ({e}) — backup và trả về []")
        # Backup file corrupt để không mất hoàn toàn
        try:
            backup = HISTORY_FILE + ".corrupt"
            import shutil
            shutil.copy2(HISTORY_FILE, backup)
            logger.warning(f"[History] Đã backup vào {backup}")
        except Exception:
            pass
        return []
    except Exception as e:
        logger.warning(f"[History] load_history lỗi: {e} — trả về []")
        return []


def save_history(trade_log: list):
    """
    Lưu lịch sử vào file — atomic write để tránh corrupt khi crash.
    Dùng _history_lock để tránh concurrent write.
    """
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        # Snapshot list trước để tránh mutation trong khi serialize
        snapshot = list(trade_log)
        tmp_file = HISTORY_FILE + ".tmp"
        with _history_lock:
            # Ghi vào file .tmp trước
            with open(tmp_file, "w") as f:
                json.dump(snapshot, f, indent=2, ensure_ascii=False)
            # Rename atomic — nếu crash giữa chừng, file gốc vẫn còn
            os.replace(tmp_file, HISTORY_FILE)
    except Exception as e:
        logger.error(f"[History] save_history lỗi: {e}")


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
