# ============================================================
# NOTIFICATION MODULE — Telegram với CONFIRM trước khi vào lệnh
# ============================================================
import logging
import os
import requests
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# ============================================================
# CONFIG
# ============================================================
NOTIFICATION_CONFIG = {
    "telegram": {
        "enabled": True,
        "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", "8260921432:AAFVMGHTFokIitSG9Bw3nMNhLw-CMXHOqsA"),
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID", "1158898649"),
    }
}


# ============================================================
# TELEGRAM NOTIFIER + CONFIRM SYSTEM
# ============================================================
class TelegramNotifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.base_url = f"https://api.telegram.org/bot{self.cfg['bot_token']}"
        self._last_update_id = None

    def send(self, message: str, reply_markup=None) -> int:
        """Gửi message, trả về message_id"""
        if not self.cfg.get("enabled"):
            return 0
        try:
            payload = {
                "chat_id": self.cfg["chat_id"],
                "text": message,
                "parse_mode": "HTML",
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup

            resp = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            logger.info("Telegram notification sent")
            return data.get("result", {}).get("message_id", 0)
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return 0

    def send_confirm_request(self, signal: str, symbol: str, price: float,
                              sl: float, tp: float, balance: float, reason: str) -> int:
        """
        Gửi tin nhắn xác nhận với 1 nút ✅ XÁC NHẬN VÀO LỆNH
        """
        emoji  = "🟢 LONG  ▲" if signal == "LONG"  else "🔴 SHORT ▼"
        rr     = abs(tp - price) / abs(price - sl) if abs(price - sl) > 0 else 0
        sl_pct = abs(price - sl) / price * 100
        tp_pct = abs(tp - price) / price * 100

        msg = (
            f"{'─'*32}\n"
            f"⚡ <b>TÍN HIỆU MỚI</b>\n"
            f"{'─'*32}\n"
            f"📌 Lệnh  : <b>{emoji}</b>\n"
            f"💰 Giá vào: <b>${price:,.2f}</b>\n"
            f"🛑 Stop Loss : <b>${sl:,.2f}</b>  <i>(-{sl_pct:.2f}%)</i>\n"
            f"🎯 Take Profit: <b>${tp:,.2f}</b>  <i>(+{tp_pct:.2f}%)</i>\n"
            f"📐 Risk/Reward: <b>1 : {rr:.1f}</b>\n"
            f"{'─'*32}\n"
            f"👇 Bấm xác nhận để vào lệnh\n"
            f"⏰ Hết hạn sau <b>3 phút</b>"
        )

        reply_markup = {
            "inline_keyboard": [[
                {"text": f"✅ XÁC NHẬN — {signal} ${price:,.0f}", "callback_data": f"confirm_{signal}"}
            ]]
        }

        return self.send(msg, reply_markup=reply_markup)

    def wait_for_confirm(self, timeout_seconds: int = 180) -> bool:
        """
        Poll Telegram updates, chờ user bấm nút.
        Returns True nếu confirm, False nếu reject/timeout
        """
        logger.info(f"Waiting for Telegram confirmation ({timeout_seconds}s)...")
        deadline = time.time() + timeout_seconds

        while time.time() < deadline:
            try:
                params = {"timeout": 10, "allowed_updates": ["callback_query"]}
                if self._last_update_id:
                    params["offset"] = self._last_update_id + 1

                resp = requests.get(
                    f"{self.base_url}/getUpdates",
                    params=params,
                    timeout=15
                )
                data = resp.json()

                for update in data.get("result", []):
                    self._last_update_id = update["update_id"]

                    cb = update.get("callback_query")
                    if not cb:
                        continue

                    # Trả lời callback để tắt loading trên nút
                    requests.post(f"{self.base_url}/answerCallbackQuery", json={
                        "callback_query_id": cb["id"],
                        "text": "Đã nhận!"
                    }, timeout=5)

                    cb_data = cb.get("data", "")
                    if cb_data.startswith("confirm_"):
                        logger.info("✅ User confirmed trade!")
                        return True
                    elif cb_data == "reject":
                        logger.info("❌ User rejected trade.")
                        return False

            except Exception as e:
                logger.warning(f"Polling error: {e}")
                time.sleep(3)

        logger.info("⏰ Confirmation timeout — skipping trade")
        return False

    def edit_message(self, message_id: int, new_text: str):
        """Update tin nhắn sau khi confirm/reject"""
        try:
            requests.post(f"{self.base_url}/editMessageText", json={
                "chat_id": self.cfg["chat_id"],
                "message_id": message_id,
                "text": new_text,
                "parse_mode": "HTML"
            }, timeout=10)
        except Exception as e:
            logger.warning(f"Edit message failed: {e}")


# ============================================================
# UNIFIED NOTIFIER
# ============================================================
class Notifier:
    def __init__(self, config: dict = None):
        cfg = config or NOTIFICATION_CONFIG
        self.telegram = TelegramNotifier(cfg["telegram"])

    def request_confirm(self, signal: str, symbol: str, price: float,
                        sl: float, tp: float, balance: float, reason: str) -> bool:
        """
        Gửi Telegram xin xác nhận.
        Returns True nếu user bấm ✅, False nếu ❌ hoặc timeout
        """
        msg_id = self.telegram.send_confirm_request(
            signal, symbol, price, sl, tp, balance, reason
        )
        confirmed = self.telegram.wait_for_confirm(timeout_seconds=180)

        if confirmed:
            self.telegram.edit_message(
                msg_id,
                f"✅ <b>XÁC NHẬN THÀNH CÔNG</b>\n"
                f"🚀 Đang vào lệnh <b>{signal} {symbol}</b> @ <b>${price:,.2f}</b>..."
            )
        else:
            self.telegram.edit_message(
                msg_id,
                f"⏰ <b>Đã hết hạn / Bỏ qua</b>\n"
                f"Tín hiệu <b>{signal} {symbol}</b> không được xác nhận."
            )

        return confirmed

    def notify_signal(self, signal: str, symbol: str, price: float,
                      sl: float, tp: float, balance: float, reason: str = ""):
        """Thông báo đã vào lệnh (sau khi confirm)"""
        emoji = "🟢 LONG" if signal == "LONG" else "🔴 SHORT"
        msg = (
            f"🚀 <b>ĐÃ VÀO LỆNH</b>\n"
            f"{'─'*30}\n"
            f"📊 {symbol}  |  {emoji}\n"
            f"💰 Entry : <b>${price:,.2f}</b>\n"
            f"🛑 SL    : <b>${sl:,.2f}</b>\n"
            f"🎯 TP    : <b>${tp:,.2f}</b>\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        self.telegram.send(msg)

    def notify_close(self, symbol: str, side: str, entry: float,
                     close_price: float, pnl: float, reason: str = ""):
        """Thông báo đóng lệnh"""
        emoji = "✅ PROFIT" if pnl > 0 else "❌ LOSS"
        pnl_pct = (close_price - entry) / entry * 100 if side == "LONG" else (entry - close_price) / entry * 100
        msg = (
            f"🔒 <b>ĐÓNG LỆNH — {emoji}</b>\n"
            f"{'─'*30}\n"
            f"📊 {symbol}  |  {side}\n"
            f"📥 Entry : <b>${entry:,.2f}</b>\n"
            f"📤 Close : <b>${close_price:,.2f}</b>\n"
            f"💵 PnL   : <b>${pnl:.2f}  ({pnl_pct:+.2f}%)</b>\n"
            f"📝 {reason}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        self.telegram.send(msg)

    def notify_error(self, error_msg: str):
        msg = (
            f"⚠️ <b>BOT ERROR</b>\n"
            f"{'─'*30}\n"
            f"<code>{error_msg[:300]}</code>\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        self.telegram.send(msg)

    def notify_startup(self, symbol: str, balance: float, leverage: int):
        msg = (
            f"🚀 <b>BOT STARTED</b>\n"
            f"{'─'*30}\n"
            f"📊 Symbol  : <b>{symbol}</b>\n"
            f"💼 Balance : <b>${balance:,.2f} USDT</b>\n"
            f"⚡ Leverage: <b>{leverage}x</b>\n"
            f"🔔 Confirm mode: <b>BẬT</b> — Bot sẽ hỏi trước khi vào lệnh\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        self.telegram.send(msg)
