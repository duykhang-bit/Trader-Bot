#!/usr/bin/env python3
# ============================================================
# TELEGRAM COMMAND BOT — Chat để điều khiển bot trading
# ============================================================
# Các lệnh hỗ trợ:
#   /status   — Xem giá, indicators, signal hiện tại
#   /balance  — Xem số dư tài khoản
#   /position — Xem lệnh đang mở
#   /orders   — Xem tất cả lệnh đã đặt
#   /long     — Vào lệnh LONG ngay (0.001 BTC)
#   /short    — Vào lệnh SHORT ngay (0.001 BTC)
#   /close    — Đóng position hiện tại
#   /help     — Xem danh sách lệnh
# ============================================================
import sys, os, time, logging
sys.path.insert(0, os.path.dirname(__file__))

import requests
import pandas as pd
import config
from exchange import BinanceFutures
from indicators import (calculate_rsi, calculate_ema, calculate_macd,
                        calculate_atr, get_htf_trend)
from trade_history import load_history, get_stats

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TOKEN   = "8260921432:AAFgBGQwpu_DnT3_mBE-pAAzJFUUM8YXXPg"
CHAT_ID = "1158898649"
BASE    = f"https://api.telegram.org/bot{TOKEN}"

ex = BinanceFutures(config.API_KEY, config.API_SECRET, config.USE_TESTNET)

# ── helpers ──────────────────────────────────────────────────
def send(text: str, markup=None):
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if markup:
        payload["reply_markup"] = markup
    try:
        requests.post(f"{BASE}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        logger.error(f"send failed: {e}")

def klines_to_df(klines):
    df = pd.DataFrame(klines, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_volume","trades",
        "taker_buy_base","taker_buy_quote","ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df

def get_df(interval="15m", limit=200):
    return klines_to_df(ex.get_klines(config.SYMBOL, interval, limit=limit))

# ── command handlers ─────────────────────────────────────────
def cmd_help():
    markup = {"keyboard": [
        [{"text": "📊 Status"}, {"text": "💼 Balance"}],
        [{"text": "📌 Position"}, {"text": "📋 Orders"}],
        [{"text": "🟢 LONG"}, {"text": "🔴 SHORT"}],
        [{"text": "📈 Stats"}, {"text": "🕐 History"}],
        [{"text": "❌ Close Position"}]
    ], "resize_keyboard": True, "persistent": True}

    send(
        "🤖 <b>MULTI-COIN TRADING BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 /status   — Giá + Signal realtime\n"
        "💼 /balance  — Số dư tài khoản\n"
        "📌 /position — Lệnh đang mở + PnL\n"
        "📋 /orders   — SL/TP đang đặt\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 /long     — Vào LONG ngay\n"
        "🔴 /short    — Vào SHORT ngay\n"
        "❌ /close    — Đóng position\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📈 /stats    — Thống kê lãi/lỗ\n"
        "🕐 /history  — 10 lệnh gần nhất\n"
        "🏓 /ping     — Kiểm tra bot sống\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Hoặc bấm nút bên dưới",
        markup=markup
    )

def cmd_status():
    try:
        df15 = get_df("15m")
        df1h = get_df("1h")
        close = df15["close"]
        price = close.iloc[-1]
        chg   = (price - close.iloc[-2]) / close.iloc[-2] * 100

        rsi   = calculate_rsi(close).iloc[-1]
        ema9  = calculate_ema(close, 9).iloc[-1]
        ema21 = calculate_ema(close, 21).iloc[-1]
        ema50 = calculate_ema(close, 50).iloc[-1]
        ml, sl_line, _ = calculate_macd(close)
        macd_bull = ml.iloc[-1] > sl_line.iloc[-1]
        ema_bull  = ema9 > ema21
        htf = get_htf_trend(df1h)

        arrow = "▲" if chg >= 0 else "▼"
        rsi_s = "🔵 oversold" if rsi < 35 else "🔴 overbought" if rsi > 65 else "⚪ neutral"
        htf_s = "🟢 UP" if htf == "UP" else "🔴 DOWN" if htf == "DOWN" else "⚪ NEUTRAL"

        long_score  = sum([rsi < 35, ema_bull, macd_bull])
        short_score = sum([rsi > 65, not ema_bull, not macd_bull])

        if long_score >= 2 and price > ema50 and htf != "DOWN":
            sig = "🟢 LONG"
        elif short_score >= 2 and price < ema50 and htf != "UP":
            sig = "🔴 SHORT"
        else:
            sig = "⏳ HOLD"

        send(
            f"📊 <b>{config.SYMBOL} — {time.strftime('%H:%M:%S')}</b>\n"
            f"─────────────────────────\n"
            f"💰 Giá   : <b>${price:,.2f}</b>  {arrow} {abs(chg):.2f}%\n"
            f"📈 RSI   : <b>{rsi:.1f}</b>  {rsi_s}\n"
            f"📉 EMA   : {'🟢 BULL' if ema_bull else '🔴 BEAR'}  (9={ema9:.0f} / 21={ema21:.0f})\n"
            f"⚡ MACD  : {'🟢 BULL' if macd_bull else '🔴 BEAR'}\n"
            f"🌐 HTF 1h: {htf_s}\n"
            f"─────────────────────────\n"
            f"🎯 Signal: <b>{sig}</b>\n"
            f"  LONG  [{long_score}/3]  SHORT [{short_score}/3]"
        )
    except Exception as e:
        send(f"❌ Lỗi: {e}")

def cmd_balance():
    try:
        bal = ex.get_account_balance()
        send(f"💼 <b>Balance</b>: <b>${bal:,.2f} USDT</b>")
    except Exception as e:
        send(f"❌ Lỗi: {e}")

def cmd_position():
    try:
        pos = ex.get_position(config.SYMBOL)
        if not pos:
            send("📭 Không có position đang mở.")
            return
        amt   = float(pos["positionAmt"])
        entry = float(pos["entryPrice"])
        pnl   = float(pos["unRealizedProfit"])
        side  = "🟢 LONG" if amt > 0 else "🔴 SHORT"
        pnl_s = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        send(
            f"📌 <b>Position hiện tại</b>\n"
            f"─────────────────────────\n"
            f"Side  : <b>{side}</b>\n"
            f"Entry : <b>${entry:,.2f}</b>\n"
            f"Size  : <b>{abs(amt)} BTC</b>\n"
            f"PnL   : <b>{pnl_s}</b>"
        )
    except Exception as e:
        send(f"❌ Lỗi: {e}")

def cmd_orders():
    try:
        orders = ex.get_open_orders(config.SYMBOL)
        if not orders:
            send("📭 Không có lệnh đang mở.")
            return
        lines = [f"📋 <b>Open Orders ({len(orders)})</b>", "─────────────────────────"]
        for o in orders:
            t     = o.get("type","")
            side  = o.get("side","")
            price = float(o.get("stopPrice") or o.get("price") or 0)
            lines.append(f"• {t} {side} @ <b>${price:,.2f}</b>")
        send("\n".join(lines))
    except Exception as e:
        send(f"❌ Lỗi: {e}")

def do_trade(signal: str):
    try:
        df = get_df("15m", 50)
        price = df["close"].iloc[-1]
        atr   = calculate_atr(df["high"], df["low"], df["close"]).iloc[-1]

        if signal == "LONG":
            sl = round(price - atr * 1.5, 4)
            tp = round(price + atr * 3.0, 4)
        else:
            sl = round(price + atr * 1.5, 4)
            tp = round(price - atr * 3.0, 4)

        # Tính qty: $15 margin × 10x = $150 notional
        notional = config.MAX_ORDER_USDT * config.LEVERAGE
        qty = notional / price
        # Round theo min lot của coin
        if price > 1000:    qty = round(qty, 3)   # BTC, ETH
        elif price > 10:    qty = round(qty, 2)   # SOL, LTC
        elif price > 1:     qty = round(qty, 1)   # NEAR, ARB
        else:               qty = int(qty)         # DOGE, XRP, ADA
        qty = max(qty, 0.001)

        margin = qty * price / config.LEVERAGE
        rr = abs(tp - price) / abs(price - sl)

        markup = {"inline_keyboard": [[
            {"text": f"✅ XÁC NHẬN {signal} ${price:,.2f}", "callback_data": f"trade_{signal}_{sl}_{tp}_{qty}"},
            {"text": "❌ HỦY", "callback_data": "cancel_trade"}
        ]]}
        send(
            f"⚡ <b>Xác nhận lệnh {signal} {config.SYMBOL}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Entry   : <b>${price:,.4f}</b>\n"
            f"🛑 SL      : <b>${sl:,.4f}</b>  (-{abs(price-sl)/price*100:.2f}%)\n"
            f"🎯 TP      : <b>${tp:,.4f}</b>  (+{abs(tp-price)/price*100:.2f}%)\n"
            f"📦 Notional: <b>${qty*price:,.2f}</b>\n"
            f"💵 Margin  : <b>${margin:.2f} USDT</b> ({config.LEVERAGE}x)\n"
            f"📐 RR      : 1:{rr:.1f}",
            markup=markup
        )
    except Exception as e:
        send(f"❌ Lỗi: {e}")

def execute_trade(signal, sl, tp, qty):
    try:
        df = get_df("15m", 5)
        price = df["close"].iloc[-1]

        # Tính qty đúng: $15 margin × leverage = notional
        notional = config.MAX_ORDER_USDT * config.LEVERAGE
        qty = max(int(notional / price), 1)

        if signal == "LONG":
            ex.place_market_order(config.SYMBOL, "BUY", qty)
        else:
            ex.place_market_order(config.SYMBOL, "SELL", qty)

        # KHÔNG đặt SL/TP order — Binance Demo không hỗ trợ
        # Bot tự monitor software SL/TP

        margin = qty * price / config.LEVERAGE
        rr = abs(tp - price) / abs(price - sl) if abs(price - sl) > 0 else 0
        icon = "🟢" if signal == "LONG" else "🔴"

        send(
            f"{icon} <b>ĐÃ VÀO LỆNH {signal}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 {config.SYMBOL}\n"
            f"💰 Entry   : <b>${price:,.4f}</b>\n"
            f"🛑 SL      : <b>${sl:,.4f}</b>\n"
            f"🎯 TP      : <b>${tp:,.4f}</b>\n"
            f"📦 Notional: <b>${qty*price:,.2f}</b>\n"
            f"💵 Margin  : <b>${margin:.2f} USDT</b> ({config.LEVERAGE}x)\n"
            f"📐 RR      : 1:{rr:.1f}"
        )
    except Exception as e:
        send(f"❌ Lỗi đặt lệnh: {e}")

def cmd_close():
    try:
        pos = ex.get_position(config.SYMBOL)
        if not pos:
            send("📭 Không có position để đóng.")
            return
        ex.cancel_all_orders(config.SYMBOL)
        ex.close_position(config.SYMBOL, pos)
        send(f"✅ <b>Đã đóng position {config.SYMBOL}</b>")
    except Exception as e:
        send(f"❌ Lỗi: {e}")


def cmd_stats():
    try:
        history = load_history()
        stats   = get_stats(history)
        if stats["total"] == 0:
            send("📭 Chưa có lệnh nào được đóng.")
            return
        send(
            f"📊 <b>THỐNG KÊ GIAO DỊCH</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 Tổng lệnh : <b>{stats['total']}</b>\n"
            f"✅ Thắng     : <b>{stats['win']}</b>\n"
            f"❌ Thua      : <b>{stats['loss']}</b>\n"
            f"🎯 Win rate  : <b>{stats['winrate']}%</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 Tổng PnL  : <b>${stats['total_pnl']:+.2f}</b>\n"
            f"🏆 Lệnh tốt nhất : <b>+${stats['best']}</b>\n"
            f"💀 Lệnh tệ nhất  : <b>-${abs(stats['worst'])}</b>"
        )
    except Exception as e:
        send(f"❌ Lỗi: {e}")


def cmd_history():
    try:
        history = load_history()
        if not history:
            send("📭 Chưa có lệnh nào.")
            return
        # Hiện 10 lệnh gần nhất
        recent = history[-10:][::-1]
        lines  = ["📋 <b>10 LỆNH GẦN NHẤT</b>", "━━━━━━━━━━━━━━━━━━━━━━━"]
        for t in recent:
            icon = "✅" if t["result"] == "WIN" else "❌"
            lines.append(
                f"{icon} <b>{t['symbol']}</b> {t['side']} "
                f"<b>${t['pnl_usd']:+.2f}</b> ({t['pnl_pct']:+.1f}%) "
                f"<i>{t['time']}</i>"
            )
        send("\n".join(lines))
    except Exception as e:
        send(f"❌ Lỗi: {e}")

# ── main polling loop ─────────────────────────────────────────
def main():
    last_update_id = None
    pending_trade  = None  # lưu trade đang chờ confirm

    send(
        "🤖 <b>Bot Trading đã sẵn sàng!</b>\n"
        "Gõ /help để xem danh sách lệnh\n"
        "Gõ /status để xem thị trường ngay"
    )
    logger.info("Telegram command bot started, polling...")

    while True:
        try:
            params = {"timeout": 10, "allowed_updates": ["message", "callback_query"]}
            if last_update_id:
                params["offset"] = last_update_id + 1

            resp = requests.get(f"{BASE}/getUpdates", params=params, timeout=15)
            updates = resp.json().get("result", [])

            for upd in updates:
                last_update_id = upd["update_id"]

                # ── Xử lý callback (bấm nút) ──
                cb = upd.get("callback_query")
                if cb:
                    requests.post(f"{BASE}/answerCallbackQuery",
                                  json={"callback_query_id": cb["id"], "text": "✅"}, timeout=5)
                    data = cb.get("data", "")

                    if data.startswith("trade_"):
                        # trade_LONG_80000_82000_0.001
                        parts = data.split("_")
                        sig = parts[1]
                        sl  = float(parts[2])
                        tp  = float(parts[3])
                        qty = float(parts[4])
                        execute_trade(sig, sl, tp, qty)

                    elif data == "cancel_trade":
                        send("❌ <b>Đã hủy lệnh.</b>")

                    continue

                # ── Xử lý text message ──
                msg = upd.get("message", {})
                text = msg.get("text", "").strip().lower()
                if not text:
                    continue

                # Chỉ nhận từ chat_id của mình
                if str(msg.get("chat", {}).get("id")) != CHAT_ID:
                    continue

                logger.info(f"Command: {text}")

                if text in ("/help", "help"):
                    cmd_help()
                elif text in ("/ping", "ping"):
                    send("🟢 <b>Bot đang chạy!</b>\n"
                         f"⏰ {time.strftime('%H:%M:%S')}\n"
                         f"📊 {config.SYMBOL} | {config.LEVERAGE}x")
                elif text in ("/status", "status", "s", "📊 status"):
                    cmd_status()
                elif text in ("/balance", "balance", "bal", "💼 balance"):
                    cmd_balance()
                elif text in ("/position", "position", "pos", "📌 position"):
                    cmd_position()
                elif text in ("/orders", "orders", "📋 orders"):
                    cmd_orders()
                elif text in ("/long", "long", "l", "🟢 long"):
                    do_trade("LONG")
                elif text in ("/short", "short", "sh", "🔴 short"):
                    do_trade("SHORT")
                elif text in ("/close", "close", "c", "❌ close position"):
                    cmd_close()
                elif text in ("/stats", "stats", "📈 stats"):
                    cmd_stats()
                elif text in ("/history", "history", "🕐 history"):
                    cmd_history()
                else:
                    send(
                        f"❓ Không hiểu lệnh <code>{text}</code>\n"
                        "Gõ /help để xem danh sách lệnh"
                    )

        except KeyboardInterrupt:
            logger.info("Bot stopped.")
            break
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
