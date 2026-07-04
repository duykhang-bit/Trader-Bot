# ============================================================
# TELEGRAM COMMAND HANDLER
# Nhắn lệnh từ Telegram → bot phản hồi và thực thi
# ============================================================
# Các lệnh hỗ trợ:
#   /addcoin PEPEUSDT     — thêm coin vào watchlist
#   /removecoin PEPEUSDT  — xóa coin khỏi watchlist
#   /list                 — xem danh sách coin đang scan
#   /status               — xem trạng thái bot + position
#   /stop                 — dừng bot
#   /leverage 10          — đổi đòn bẩy
#   /risk 1               — đổi risk % mỗi lệnh
# ============================================================
import logging
import os
import requests
import threading
import time
import pandas as pd

logger = logging.getLogger(__name__)


class TelegramCommandHandler:
    def __init__(self, bot_token: str, chat_id: str, state: dict,
                 state_lock: threading.Lock, watchlist: list, config):
        self.token = bot_token
        self.chat_id = chat_id
        self.state = state
        self.lock = state_lock
        self.watchlist = watchlist
        self.config = config
        self.last_update_id = 0
        self.running = True

        # Load watchlist từ file (nếu có)
        self._load_watchlist()

    def _save_watchlist(self):
        """Lưu watchlist vào file để giữ khi restart"""
        import json
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
        try:
            with open(path, "w") as f:
                json.dump(list(self.watchlist), f)
        except Exception:
            pass

    def _load_watchlist(self):
        """Load watchlist từ file khi bot start"""
        import json
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    saved = json.load(f)
                if saved:
                    # Merge: giữ coin từ config + thêm coin saved
                    for coin in saved:
                        if coin not in self.watchlist:
                            self.watchlist.append(coin)
                    logger.info(f"Loaded watchlist from file: {self.watchlist}")
        except Exception:
            pass

    def send(self, text: str, markup=None):
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        if markup:
            payload["reply_markup"] = markup
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload,
                timeout=10
            )
            if resp.status_code != 200:
                logger.error(f"Telegram send FAILED ({resp.status_code}): {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Telegram send error: {e}")

    def get_updates(self):
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"offset": self.last_update_id + 1, "timeout": 10},
                timeout=15
            )
            results = resp.json().get("result", [])
            # Auto-clear: nếu quá nhiều updates pending → skip hết, lấy mới nhất
            if len(results) > 20:
                self.last_update_id = results[-1]["update_id"]
                logger.warning(f"Telegram: skipped {len(results)} stale updates")
                return []
            return results
        except Exception:
            return []

    def handle(self, text: str) -> str:
        text = text.strip()
        # Fix: bỏ space thừa giữa / và tên lệnh (vd: "/ addcoin" → "/addcoin")
        if text.startswith("/ "):
            text = "/" + text[2:].strip()
        parts = text.split()
        cmd = parts[0].lower() if parts else ""

        # /addcoin PEPEUSDT
        if cmd == "/addcoin":
            if len(parts) < 2:
                return "❌ Dùng: /addcoin PEPEUSDT"
            coin = parts[1].upper()
            if not coin.endswith("USDT"):
                coin += "USDT"
            if coin in self.watchlist:
                return f"⚠️ {coin} đã có trong watchlist rồi"
            self.watchlist.append(coin)
            self._save_watchlist()
            return f"✅ Đã thêm <b>{coin}</b> vào watchlist\n📋 Tổng: {len(self.watchlist)} coins"

        # /removecoin PEPEUSDT
        elif cmd == "/removecoin":
            if len(parts) < 2:
                return "❌ Dùng: /removecoin PEPEUSDT"
            coin = parts[1].upper()
            if not coin.endswith("USDT"):
                coin += "USDT"
            if coin not in self.watchlist:
                return f"⚠️ {coin} không có trong watchlist"
            self.watchlist.remove(coin)
            self._save_watchlist()
            return f"✅ Đã xóa <b>{coin}</b> khỏi watchlist\n📋 Còn lại: {len(self.watchlist)} coins"

        # /list
        elif cmd == "/list":
            coins = [c.replace("USDT", "") for c in self.watchlist]
            lines = []
            for i in range(0, len(coins), 5):
                lines.append("  ".join(coins[i:i+5]))
            return f"📋 <b>Watchlist ({len(self.watchlist)} coins):</b>\n" + "\n".join(lines)

        # /status
        elif cmd == "/status":
            with self.lock:
                bal      = self.state.get("balance", 0)
                scan_no  = self.state.get("scan_no", 0)
                tlog     = list(self.state.get("trade_log", []))
                open_pos = list(self.state.get("open_positions", []))
                grids    = dict(self.state.get("grids", {}))

            closed    = [t for t in tlog if t["status"] == "CLOSED"]
            wins      = sum(1 for t in closed if t.get("pnl_usdt", 0) > 0)
            losses    = len(closed) - wins
            total_pnl = sum(t.get("pnl_usdt", 0) for t in closed)
            winrate   = wins / len(closed) * 100 if closed else 0
            unrealized = sum(p.get("_pnl", 0) for p in open_pos)

            lines = [
                f"📊 <b>BOT STATUS</b>",
                f"━━━━━━━━━━━━━━━━━━━━━━━",
                f"💼 Balance   : <b>${bal:,.2f} USDT</b>",
                f"🔄 Scan #    : {scan_no}",
                f"",
                f"📈 <b>THỐNG KÊ</b>",
                f"━━━━━━━━━━━━━━━━━━━━━━━",
                f"Tổng lệnh   : {len(closed)}",
                f"✅ Win / ❌ Loss : {wins} / {losses}  ({winrate:.0f}%)",
                f"💵 Realized  : <b>${total_pnl:+.2f}</b>",
                f"💰 Unrealized: <b>${unrealized:+.2f}</b>",
                f"📊 Tổng PnL  : <b>${total_pnl+unrealized:+.2f}</b>",
            ]

            # Positions đang mở
            if open_pos:
                lines += ["", f"📌 <b>{len(open_pos)} LỆNH ĐANG MỞ</b>", "━━━━━━━━━━━━━━━━━━━━━━━"]
                for p in open_pos:
                    amt  = float(p.get("positionAmt", 0))
                    ep   = float(p.get("entryPrice", 0))
                    mark = p.get("_mark", ep)
                    pnl  = p.get("_pnl", 0)
                    pct  = p.get("_pct", 0)
                    side = "🟢LONG" if amt > 0 else "🔴SHORT"
                    lev  = p.get("_lev", self.config.LEVERAGE)
                    lines.append(f"{side} {p['symbol']} {lev}x\n  ${ep:.4f}→${mark:.4f} <b>${pnl:+.2f}</b> ({pct:+.1f}%)")

            # Lịch sử 10 lệnh gần nhất
            if closed:
                lines += ["", f"📋 <b>10 LỆNH GẦN NHẤT</b>", "━━━━━━━━━━━━━━━━━━━━━━━"]
                for t in reversed(closed[-10:]):
                    p   = t.get("pnl_usdt", 0)
                    pct = t.get("pnl_pct", 0)
                    icon = "✅" if p > 0 else "❌"
                    lines.append(f"{icon} {t['symbol']} {t['side']} <b>${p:+.2f}</b>({pct:+.1f}%) {t['time'][11:16]}")

            # Grid bots
            if grids:
                grid_profit = sum(g.get_status()["total_profit"] for g in grids.values())
                lines += ["", f"🔲 <b>GRID BOTS ({len(grids)})</b>", "━━━━━━━━━━━━━━━━━━━━━━━"]
                for sym_g, g in grids.items():
                    st = g.get_status()
                    lines.append(f"• {sym_g}: {st['trade_count']} trades | <b>${st['total_profit']:+.4f}</b>")
                lines.append(f"Grid tổng: <b>${grid_profit:+.4f}</b>")

            return "\n".join(lines)

        # /pnl — bảng lãi/lỗ chi tiết tất cả lệnh đang mở
        elif cmd == "/pnl":
            exchange = self._get_exchange()
            return self._get_positions_table(exchange, detailed=True)

        # /leverage 10
        elif cmd == "/leverage":
            if len(parts) < 2 or not parts[1].isdigit():
                return f"❌ Dùng: /leverage 10\nHiện tại: {self.config.LEVERAGE}x"
            lev = int(parts[1])
            if lev < 1 or lev > 100:
                return "❌ Leverage phải từ 1-100"
            self.config.LEVERAGE = lev
            return f"✅ Đã đổi leverage thành <b>{lev}x</b>"

        # /usdt 5
        elif cmd == "/usdt":
            if len(parts) < 2:
                return f"❌ Dùng: /usdt 5\nHiện tại: ${self.config.MAX_ORDER_USDT}/lệnh"
            try:
                val = float(parts[1])
                if val < 1 or val > 1000:
                    return "❌ USDT phải từ 1 - 1000"
                self.config.MAX_ORDER_USDT = val
                return f"✅ Đã đổi margin thành <b>${val}/lệnh</b>\n💰 Notional: ${val * self.config.LEVERAGE}/lệnh ({self.config.LEVERAGE}x)"
            except ValueError:
                return "❌ Nhập số, ví dụ: /usdt 5"

        # /risk 1
        elif cmd == "/risk":
            if len(parts) < 2:
                return f"❌ Dùng: /risk 2\nHiện tại: {self.config.RISK_PER_TRADE*100:.1f}%"
            try:
                risk = float(parts[1])
                if risk < 0.5 or risk > 10:
                    return "❌ Risk phải từ 0.5% - 10%"
                self.config.RISK_PER_TRADE = risk / 100
                return f"✅ Đã đổi risk thành <b>{risk}%</b> mỗi lệnh"
            except ValueError:
                return "❌ Nhập số, ví dụ: /risk 2"

        # /score 50 — đổi MIN_SCORE
        elif cmd == "/score":
            if len(parts) < 2:
                return f"❌ Dùng: /score 50\nHiện tại: {self.config.MIN_SCORE:.0f}%"
            try:
                val = float(parts[1])
                if val < 10 or val > 100:
                    return "❌ Score phải từ 10 - 100"
                self.config.MIN_SCORE = val
                return (f"✅ Đã đổi MIN_SCORE thành <b>{val:.0f}%</b>\n"
                        f"💡 Bot sẽ chỉ vào lệnh khi coin đạt ≥ {val:.0f}% score")
            except ValueError:
                return "❌ Nhập số, ví dụ: /score 50"

        # /export — xuất báo cáo HTML gửi qua Telegram
        elif cmd == "/export":
            self.send("⏳ Đang tạo báo cáo...")
            try:
                from report_generator import generate_and_send
                with self.lock:
                    tlog     = list(self.state.get("trade_log", []))
                    bal      = self.state.get("balance", 0)
                    open_pos = list(self.state.get("open_positions", []))
                    splits   = dict(self.state.get("split_positions", {}))

                filepath = generate_and_send(
                    trade_log       = tlog,
                    balance         = bal,
                    open_positions  = open_pos,
                    split_positions = splits,
                    bot_token       = self.token,
                    chat_id         = self.chat_id,
                )
                all_closed = [t for t in tlog if t.get("status") == "CLOSED" and abs(t.get("pnl_usdt", 0)) > 0.001]
                total_pnl  = sum(t.get("pnl_usdt", 0) for t in all_closed)
                wins       = sum(1 for t in all_closed if t.get("pnl_usdt", 0) > 0)
                wr         = wins / len(all_closed) * 100 if all_closed else 0
                return (f"📊 <b>Báo cáo đã xuất!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📋 Tổng lệnh : <b>{len(all_closed)}</b>\n"
                        f"🎯 Win rate  : <b>{wr:.1f}%</b>\n"
                        f"💵 Tổng PnL  : <b>${total_pnl:+.2f}</b>\n"
                        f"💼 Balance   : <b>${bal:,.2f}</b>")
            except Exception as e:
                return f"❌ Lỗi xuất báo cáo: {e}"

        # /stop
        elif cmd == "/stop":
            with self.lock:
                self.state["running"] = False
                self.state["_clean_exit"] = True  # đánh dấu thoát chủ động
            return "⛔ Bot đang dừng..."

        # /closeall — đóng tất cả lệnh nhưng không dừng bot
        elif cmd == "/closeall":
            exchange = self._get_exchange()
            if not exchange:
                return "❌ Không kết nối được exchange"
            closed = []
            try:
                all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
                open_pos = [p for p in all_pos if abs(float(p.get("positionAmt", 0))) > 0]
                for p in open_pos:
                    sym = p["symbol"]
                    amt = float(p["positionAmt"])
                    side = "SELL" if amt > 0 else "BUY"
                    qty = abs(amt)
                    exchange.place_market_order(sym, side, qty)
                    exchange.cancel_all_orders(sym)
                    closed.append(sym)
                # Reset state
                with self.lock:
                    self.state["position"] = None
                    self.state["symbol"] = None
                    self.state["entry"] = 0.0
                    self.state["sl"] = 0.0
                    self.state["tp"] = 0.0
                    self.state["qty"] = 0.0
                if closed:
                    return f"✅ Đã đóng {len(closed)} lệnh:\n" + "\n".join(f"• {s}" for s in closed)
                else:
                    return "ℹ️ Không có lệnh nào đang mở"
            except Exception as e:
                return f"❌ Lỗi: {e}"

        # /settp — Hiển thị positions chưa có SL/TP, bấm nút để auto set
        elif cmd == "/settp":
            exchange = self._get_exchange()
            if not exchange:
                return "❌ Không kết nối được exchange"
            from auto_sltp import get_positions_without_sltp, suggest_sltp
            unprotected = get_positions_without_sltp(exchange)
            if not unprotected:
                return "✅ Tất cả positions đã có SL/TP"

            liq_tracker = self.state.get("liq_tracker")
            lines = ["⚠️ <b>POSITIONS CHƯA CÓ SL/TP:</b>\n"]
            for pos in unprotected:
                sym = pos["symbol"]
                side = pos["side"]
                entry = pos["entry"]
                qty = pos["qty"]
                pnl = pos["pnl"]
                missing = []
                if not pos["has_sl"]: missing.append("SL")
                if not pos["has_tp"]: missing.append("TP")

                # Suggest SL/TP
                suggestion = suggest_sltp(exchange, sym, side, entry, liq_tracker)

                icon = "🟢" if side == "LONG" else "🔴"
                pnl_icon = "📈" if pnl >= 0 else "📉"
                lines.append(
                    f"{icon} <b>{sym.replace('USDT','')} {side}</b>\n"
                    f"   Entry: ${entry:.4f} | {pnl_icon} PnL: ${pnl:+.2f}\n"
                    f"   ❌ Thiếu: {', '.join(missing)}\n"
                    f"   💡 Đề xuất: SL=${suggestion['sl']} (-{suggestion['sl_pct']}%)"
                    f" | TP=${suggestion['tp']} (+{suggestion['tp_pct']}%)\n"
                    f"   📐 RR: 1:{suggestion['rr']} | {suggestion['method']}\n"
                )

            lines.append("\n👇 Bấm nút bên dưới để auto set SL/TP:")
            msg = "\n".join(lines)

            # Tạo inline buttons cho từng position
            buttons = []
            for pos in unprotected:
                sym = pos["symbol"]
                buttons.append([{
                    "text": f"🛡️ Set SL/TP — {sym.replace('USDT','')}",
                    "callback_data": f"settp_{sym}"
                }])
            # Nút set tất cả
            if len(unprotected) > 1:
                buttons.append([{"text": "🛡️ Set ALL SL/TP", "callback_data": "settp_ALL"}])

            self.send(msg, markup={"inline_keyboard": buttons})
            return None  # Đã gửi message riêng

        # /grid BTCUSDT 78000 82000 20 100
        elif cmd == "/grid":
            if len(parts) < 6:
                return ("❌ Dùng: /grid SYMBOL LOWER UPPER GRIDS USDT\n"
                        "Ví dụ: /grid BTCUSDT 78000 82000 20 100\n"
                        "→ Grid BTC từ $78k-$82k, 20 lưới, $100 vốn")
            try:
                symbol = parts[1].upper()
                if not symbol.endswith("USDT"): symbol += "USDT"
                lower  = float(parts[2])
                upper  = float(parts[3])
                grids  = int(parts[4])
                usdt   = float(parts[5])
                if lower >= upper:
                    return "❌ Lower phải nhỏ hơn Upper"
                if grids < 2 or grids > 200:
                    return "❌ Số lưới phải từ 2-200"
                if usdt < 10:
                    return "❌ Vốn tối thiểu $10"

                from grid_strategy import GridBot
                current_price = self.state.get("prices", {}).get(symbol, 0)
                if current_price == 0:
                    return f"❌ Không lấy được giá {symbol}"

                grid = GridBot(symbol, lower, upper, grids, usdt,
                               self._get_exchange(), self._get_notifier())
                grid.setup(current_price)

                with self.lock:
                    if "grids" not in self.state:
                        self.state["grids"] = {}
                    self.state["grids"][symbol] = grid

                return (f"✅ Grid Bot started!\n"
                        f"📊 {symbol}: ${lower} - ${upper}\n"
                        f"🔢 {grids} lưới | ${usdt} USDT\n"
                        f"💵 Mỗi lưới: ${usdt/grids:.2f}")
            except ValueError:
                return "❌ Sai định dạng. Dùng: /grid BTCUSDT 78000 82000 20 100"

        # /stopgrid
        elif cmd == "/stopgrid":
            sym = parts[1].upper() if len(parts) > 1 else None
            with self.lock:
                grids = self.state.get("grids", {})
            if not grids:
                return "⚠️ Không có grid nào đang chạy"
            if sym and sym in grids:
                grids[sym].stop()
                with self.lock: del self.state["grids"][sym]
                return f"✅ Đã dừng grid {sym}"
            else:
                for g in grids.values(): g.stop()
                with self.lock: self.state["grids"] = {}
                return "✅ Đã dừng tất cả grid bots"

        # /help
        elif cmd in ["/help", "/start"]:
            return """🤖 <b>TRADING BOT COMMANDS</b>

/addcoin PEPE     — Thêm coin vào scan
/removecoin PEPE  — Xóa coin khỏi scan
/list             — Xem danh sách coin
/status           — Xem trạng thái + lệnh
/dashboard        — 📺 Dashboard đầy đủ (như terminal)
/pnl              — Bảng lãi/lỗ tất cả lệnh
/leverage 10      — Đổi đòn bẩy (1-20x)
/usdt 5           — Đổi margin mỗi lệnh ($1-$1000)
/risk 2           — Đổi risk % mỗi lệnh
/score 50         — Đổi MIN_SCORE vào lệnh (10-100%)
/closeall         — Đóng TẤT CẢ lệnh ngay
/settp            — 🛡️ Auto set SL/TP cho lệnh chưa có
/trade BTC        — Đánh giá LONG/SHORT coin
/grid BTCUSDT 78000 82000 20 100  — Bật grid bot
/stopgrid         — Dừng grid bot
/export           — 📤 Xuất báo cáo HTML trading
/stop             — Dừng toàn bộ bot
/ping             — Kiểm tra bot sống
/help             — Xem lệnh này"""

        # /ping
        elif cmd == "/ping":
            import time as _t
            return (f"🟢 <b>Bot đang chạy!</b>\n"
                    f"⏰ {_t.strftime('%H:%M:%S')}\n"
                    f"📊 {self.config.SYMBOL} | {self.config.LEVERAGE}x")

        # /dashboard — Hiển thị toàn bộ dashboard terminal lên Telegram
        elif cmd == "/dashboard":
            return self._build_dashboard_message()

        # /balance
        elif cmd == "/balance":
            exchange = self._get_exchange()
            if not exchange:
                return "❌ Không kết nối được exchange"
            try:
                bal = exchange.get_account_balance()
                return f"💼 <b>Balance</b>: <b>${bal:,.2f} USDT</b>"
            except Exception as e:
                return f"❌ Lỗi: {e}"

        # /position
        elif cmd == "/position":
            exchange = self._get_exchange()
            return self._get_positions_table(exchange)

        # /orders
        elif cmd == "/orders":
            exchange = self._get_exchange()
            if not exchange:
                return "❌ Không kết nối được exchange"
            try:
                # Lấy tất cả open orders
                all_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                if not all_orders:
                    return "📭 Không có lệnh pending nào."
                lines = [f"📋 <b>Open Orders ({len(all_orders)})</b>", "━━━━━━━━━━━━━━━━━━━━━━━"]
                for o in all_orders[:20]:
                    t     = o.get("type", "")
                    side  = o.get("side", "")
                    sym   = o.get("symbol", "").replace("USDT", "")
                    # stopPrice hoặc price — check cả 2, bỏ qua "0" và ""
                    stop_p = float(o.get("stopPrice", 0) or 0)
                    limit_p = float(o.get("price", 0) or 0)
                    price = stop_p if stop_p > 0 else limit_p
                    lines.append(f"• {sym} {t} {side} @ <b>${price:,.{_price_decimals(price)}f}</b>")
                lines.append("")
                lines.append("Gõ /cancelall để hủy tất cả")
                lines.append("Hoặc /cancel BTCUSDT để hủy 1 coin")

                # Inline buttons cancel từng coin
                symbols_with_orders = list(set(o.get("symbol", "") for o in all_orders))
                buttons = []
                row = []
                for sym in symbols_with_orders:
                    name = sym.replace("USDT", "")
                    row.append({"text": f"❌ Cancel {name}", "callback_data": f"cancelorders_{sym}"})
                    if len(row) == 2:
                        buttons.append(row)
                        row = []
                if row:
                    buttons.append(row)
                buttons.append([{"text": "🗑 Cancel ALL", "callback_data": "cancelorders_ALL"}])

                self.send("\n".join(lines), markup={"inline_keyboard": buttons})
                return None
            except Exception as e:
                return f"❌ Lỗi: {e}"

        # /cancel BTCUSDT — hủy tất cả orders cho 1 coin
        elif cmd == "/cancel":
            if len(parts) < 2:
                return "❌ Dùng: /cancel BTC hoặc /cancel BTCUSDT"
            symbol = parts[1].upper()
            if not symbol.endswith("USDT"):
                symbol += "USDT"
            exchange = self._get_exchange()
            if not exchange:
                return "❌ Không kết nối được exchange"
            try:
                exchange.cancel_all_orders(symbol)
                return f"✅ Đã hủy tất cả orders cho <b>{symbol}</b>"
            except Exception as e:
                return f"❌ Lỗi: {e}"

        # /cancelall — hủy tất cả orders tất cả coin
        elif cmd == "/cancelall":
            exchange = self._get_exchange()
            if not exchange:
                return "❌ Không kết nối được exchange"
            try:
                all_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                symbols = list(set(o.get("symbol", "") for o in all_orders))
                for sym in symbols:
                    exchange.cancel_all_orders(sym)
                return f"✅ Đã hủy tất cả {len(all_orders)} orders ({len(symbols)} coins)"
            except Exception as e:
                return f"❌ Lỗi: {e}"

        # /long — vào lệnh LONG coin
        elif cmd == "/long":
            if len(parts) > 1:
                symbol = parts[1].upper()
                if not symbol.endswith("USDT"):
                    symbol += "USDT"
                self.send(f"🔍 Đang phân tích <b>{symbol}</b> cho LONG...")
                self._analyze_and_send(symbol)
                return None
            else:
                # Hiện menu chọn coin
                buttons = []
                row = []
                for sym in self.watchlist:
                    name = sym.replace("USDT", "")
                    row.append({"text": f"🟢 {name}", "callback_data": f"quick_LONG_{sym}"})
                    if len(row) == 3:
                        buttons.append(row)
                        row = []
                if row:
                    buttons.append(row)
                buttons.append([{"text": "❌ Hủy", "callback_data": "cancel_trade"}])
                self.send("🟢 <b>LONG — Chọn coin:</b>", markup={"inline_keyboard": buttons})
                return None

        # /short — vào lệnh SHORT coin
        elif cmd == "/short":
            if len(parts) > 1:
                symbol = parts[1].upper()
                if not symbol.endswith("USDT"):
                    symbol += "USDT"
                self.send(f"🔍 Đang phân tích <b>{symbol}</b> cho SHORT...")
                self._analyze_and_send(symbol)
                return None
            else:
                # Hiện menu chọn coin
                buttons = []
                row = []
                for sym in self.watchlist:
                    name = sym.replace("USDT", "")
                    row.append({"text": f"🔴 {name}", "callback_data": f"quick_SHORT_{sym}"})
                    if len(row) == 3:
                        buttons.append(row)
                        row = []
                if row:
                    buttons.append(row)
                buttons.append([{"text": "❌ Hủy", "callback_data": "cancel_trade"}])
                self.send("🔴 <b>SHORT — Chọn coin:</b>", markup={"inline_keyboard": buttons})
                return None

        # /history
        elif cmd == "/history":
            from trade_history import load_history
            history = load_history()
            if not history:
                return "📭 Chưa có lệnh nào."
            closed = [t for t in history if t.get("status") == "CLOSED"]
            recent = closed[-10:][::-1]
            lines = ["📋 <b>10 LỆNH GẦN NHẤT</b>", "━━━━━━━━━━━━━━━━━━━━━━━"]
            for t in recent:
                pnl = t.get("pnl_usdt", 0)
                pct = t.get("pnl_pct", 0)
                icon = "✅" if pnl > 0 else "❌"
                sym = t.get("symbol", "").replace("USDT", "")
                lines.append(f"{icon} {sym} {t.get('side','')} <b>${pnl:+.2f}</b>({pct:+.1f}%) {t.get('time','')[11:16]}")
            return "\n".join(lines)

        # /stats
        elif cmd == "/stats":
            from trade_history import load_history
            history = load_history()
            closed = [t for t in history if t.get("status") == "CLOSED"]
            if not closed:
                return "📭 Chưa có lệnh nào được đóng."
            wins = sum(1 for t in closed if t.get("pnl_usdt", 0) > 0)
            losses = len(closed) - wins
            total_pnl = sum(t.get("pnl_usdt", 0) for t in closed)
            wr = wins / len(closed) * 100 if closed else 0
            best = max(t.get("pnl_usdt", 0) for t in closed)
            worst = min(t.get("pnl_usdt", 0) for t in closed)
            return (
                f"📊 <b>THỐNG KÊ GIAO DỊCH</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 Tổng lệnh : <b>{len(closed)}</b>\n"
                f"✅ Thắng     : <b>{wins}</b>\n"
                f"❌ Thua      : <b>{losses}</b>\n"
                f"🎯 Win rate  : <b>{wr:.1f}%</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💵 Tổng PnL  : <b>${total_pnl:+.2f}</b>\n"
                f"🏆 Lệnh tốt nhất : <b>${best:+.2f}</b>\n"
                f"💀 Lệnh tệ nhất  : <b>${worst:+.2f}</b>"
            )

        # /trade BTC  hoặc  /tradebtc  hoặc  /trade BTCUSDT
        elif cmd.startswith("/trade"):
            # Hỗ trợ: /trade BTC, /tradebtc, /trade BTCUSDT
            if cmd == "/trade":
                symbol_raw = parts[1] if len(parts) > 1 else ""
            else:
                # /tradebtc → lấy phần sau /trade
                symbol_raw = cmd[len("/trade"):]

            if not symbol_raw:
                return "❌ Dùng: /trade BTC hoặc /trade BTCUSDT"

            symbol_raw = symbol_raw.upper()
            if not symbol_raw.endswith("USDT"):
                symbol = symbol_raw + "USDT"
            else:
                symbol = symbol_raw

            # Gửi ngay thông báo đang phân tích
            self.send(f"🔍 Đang phân tích <b>{symbol}</b>...")
            self._analyze_and_send(symbol)
            return None  # đã send trực tiếp, không cần return text

        else:
            return f"❓ Không hiểu lệnh <b>{cmd}</b>\nGõ /help để xem danh sách lệnh"

    def _build_dashboard_message(self) -> str:
        """
        Build dashboard message cho Telegram — tương tự terminal dashboard
        nhưng format HTML thay vì ANSI colors.
        """
        import time as _t
        from datetime import datetime as _dt

        with self.lock:
            s = dict(self.state)
            tlog = list(self.state.get("trade_log", []))
            open_pos = list(self.state.get("open_positions", []))
            grids = dict(self.state.get("grids", {}))
            candidates = list(self.state.get("candidates", []))

        now = _dt.now().strftime("%H:%M:%S")
        bal = s.get("balance", 0)
        scan_no = s.get("scan_no", 0)
        last_scan = s.get("last_scan", "--:--")

        # Thống kê lệnh đã đóng
        closed = [t for t in tlog if t.get("status") == "CLOSED"]
        closed_real = [t for t in closed if abs(t.get("pnl_usdt", 0)) > 0.001]
        total_pnl = sum(t.get("pnl_usdt", 0) for t in closed_real)
        wins = sum(1 for t in closed_real if t.get("pnl_usdt", 0) > 0)
        losses = len(closed_real) - wins
        wr = wins / len(closed_real) * 100 if closed_real else 0
        unrealized = sum(p.get("_pnl", 0) for p in open_pos)

        pnl_icon = "📈" if total_pnl >= 0 else "📉"

        lines = [
            f"📺 <b>DASHBOARD — MULTI-COIN BOT</b>",
            f"━━━━━━━━━━━━━━━━━━━━━━━",
            f"🕐 {now}   💼 <b>${bal:,.2f} USDT</b>",
            f"🔍 Scanning: {len(self.watchlist)} coins  |  Scan #{scan_no} ({last_scan})",
            f"{pnl_icon} PnL: <b>${total_pnl:+.2f}</b>  |  ✅{wins}W ❌{losses}L  WR:{wr:.0f}%",
        ]

        if closed_real:
            avg_win = sum(t.get("pnl_usdt", 0) for t in closed_real if t.get("pnl_usdt", 0) > 0) / max(wins, 1)
            avg_loss = sum(t.get("pnl_usdt", 0) for t in closed_real if t.get("pnl_usdt", 0) <= 0) / max(losses, 1)
            rr_val = abs(avg_win / avg_loss) if avg_loss != 0 else 0
            lines.append(f"{'🟢' if avg_win > abs(avg_loss) else '🔴'} Avg Win: ${avg_win:+.2f}  |  Avg Loss: ${avg_loss:+.2f}  |  RR:{rr_val:.1f}x")

        # ── Positions đang mở ──
        lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━")
        if open_pos:
            total_unr = sum(p.get("_pnl", 0) for p in open_pos)
            unr_icon = "📈" if total_unr >= 0 else "📉"
            lines.append(f"📌 <b>{len(open_pos)} LỆNH ĐANG MỞ</b>  {unr_icon} ${total_unr:+.2f}")
            lines.append("<code>")
            for p in open_pos:
                sym = p["symbol"].replace("USDT", "")
                amt = float(p.get("positionAmt", 0))
                entry = float(p.get("entryPrice", 0))
                mark = p.get("_mark", entry)
                pnl = p.get("_pnl", 0)
                pct = p.get("_pct", 0)
                lev = p.get("_lev", self.config.LEVERAGE)
                side = "L" if amt > 0 else "S"
                icon = "🟢" if amt > 0 else "🔴"
                lines.append(f"{icon}{sym:<7} {side} ${entry:.4f}→${mark:.4f} ${pnl:+.2f} ({pct:+.1f}%) {lev}x")
            lines.append("</code>")
        else:
            lines.append("💤 Không có lệnh đang mở")

        # ── Giá realtime ──
        prices = s.get("prices", {})
        if prices:
            lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"💹 <b>GIÁ REALTIME</b>")
            lines.append("<code>")
            price_items = [(sym, p) for sym, p in prices.items() if p > 0]
            # Hiện tối đa 20 coin
            for i in range(0, min(len(price_items), 20), 4):
                row_parts = []
                for sym, p in price_items[i:i+4]:
                    name = sym.replace("USDT", "")
                    if p >= 1000:
                        row_parts.append(f"{name:<5}${p:>8,.0f}")
                    elif p >= 1:
                        row_parts.append(f"{name:<5}${p:>7.3f}")
                    else:
                        row_parts.append(f"{name:<5}${p:>8.5f}")
                lines.append(" ".join(row_parts))
            lines.append("</code>")

        # ── Top Signals ──
        if candidates:
            lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"📊 <b>TOP SIGNALS</b> (scan {last_scan})")
            for c in candidates[:5]:
                sym = c.symbol.replace("USDT", "")
                if c.signal == "LONG":
                    tag = "▲LONG "
                else:
                    tag = "▼SHORT"
                score_bar = "█" * int(c.score / 10) + "░" * (10 - int(c.score / 10))
                lines.append(f"  {sym:<8} {tag} [{score_bar}] {c.score:.0f}% RSI={c.rsi}")

        # ── Lịch sử 5 lệnh gần nhất ──
        if closed_real:
            lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"📋 <b>5 LỆNH GẦN NHẤT</b>")
            sorted_closed = sorted(closed_real, key=lambda t: t.get("time", ""), reverse=True)
            for t in sorted_closed[:5]:
                p = t.get("pnl_usdt", 0)
                pct = t.get("pnl_pct", 0)
                icon = "✅" if p > 0 else "❌"
                sym = t.get("symbol", "").replace("USDT", "")
                lines.append(f"  {icon} {sym} {t.get('side','')} <b>${p:+.2f}</b> ({pct:+.1f}%) {t.get('time','')[11:16]}")

        # ── Grid bots ──
        if grids:
            lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"🔲 <b>GRID BOTS ({len(grids)})</b>")
            for sym_g, g in grids.items():
                try:
                    st = g.get_status()
                    lines.append(f"  • {sym_g}: {st['trade_count']} trades | ${st['total_profit']:+.4f}")
                except:
                    lines.append(f"  • {sym_g}: (loading...)")

        # ── Footer ──
        lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"⚡ {self.config.LEVERAGE}x  |  Max ${self.config.MAX_ORDER_USDT}/lệnh  |  Risk {self.config.RISK_PER_TRADE*100:.1f}%")
        lines.append(f"🔄 Gõ /dashboard để refresh")

        return "\n".join(lines)

    def _analyze_and_send(self, symbol: str):
        """
        Phân tích kỹ thuật coin và gửi đánh giá LONG/SHORT/WAIT
        kèm Entry, SL, TP, tỉ lệ RR, win rate ước tính và nút xác nhận.
        """
        try:
            logger.info(f"[Telegram] _analyze_and_send started for {symbol}")
            from indicators import (calculate_rsi, calculate_ema, calculate_macd,
                                    calculate_atr, calculate_bollinger,
                                    calculate_volume_ma, get_htf_trend)

            # Lấy dữ liệu nhiều khung thời gian
            def to_df(klines):
                df = pd.DataFrame(klines, columns=[
                    "open_time","open","high","low","close","volume",
                    "close_time","quote_volume","trades",
                    "taker_buy_base","taker_buy_quote","ignore"
                ])
                for col in ["open","high","low","close","volume"]:
                    df[col] = df[col].astype(float)
                return df

            # Dùng exchange instance (đã có SSL verify=False) thay vì requests trực tiếp
            exchange = self._get_exchange()
            if not exchange:
                self.send(f"❌ Không kết nối được exchange")
                return

            data_source = "Binance 📡"

            try:
                logger.info(f"[Telegram] Fetching 15m klines for {symbol}...")
                klines_15m = exchange.get_klines(symbol, "15m", limit=100)
            except Exception as e:
                self.send(
                    f"❌ <b>{symbol} không tồn tại</b> trên Binance Futures.\n"
                    f"Kiểm tra lại tên coin. Ví dụ: /trade BTC, /trade SOL\n"
                    f"Lỗi: {e}"
                )
                return

            if not klines_15m or len(klines_15m) == 0:
                self.send(
                    f"❌ <b>{symbol} không tồn tại</b> trên Binance Futures.\n"
                    f"Kiểm tra lại tên coin. Ví dụ: /trade BTC, /trade SOL"
                )
                return

            df15 = to_df(klines_15m)

            try:
                logger.info(f"[Telegram] Fetching 1h klines...")
                klines_1h = exchange.get_klines(symbol, "1h", limit=50)
                df1h = to_df(klines_1h)
            except Exception:
                df1h = df15.copy()

            try:
                logger.info(f"[Telegram] Fetching 4h klines...")
                klines_4h = exchange.get_klines(symbol, "4h", limit=50)
                df4h = to_df(klines_4h)
            except Exception:
                df4h = df15.copy()

            logger.info(f"[Telegram] Klines fetched, calculating indicators...")

            close  = df15["close"]
            high   = df15["high"]
            low    = df15["low"]
            volume = df15["volume"]

            price   = close.iloc[-1]
            price_1 = close.iloc[-2]

            # ── Tính indicators ──────────────────────────────────────
            rsi      = calculate_rsi(close, 14)
            ema9     = calculate_ema(close, 9)
            ema21    = calculate_ema(close, 21)
            ema50    = calculate_ema(close, 50)
            ml, sl_line, hist = calculate_macd(close)
            bb_up, bb_mid, bb_lo = calculate_bollinger(close, 20, 2.0)
            atr      = calculate_atr(high, low, close, 14)
            vol_ma   = calculate_volume_ma(volume, 20)

            cur_rsi   = rsi.iloc[-1]
            prev_rsi  = rsi.iloc[-2]
            cur_ema9  = ema9.iloc[-1]
            cur_ema21 = ema21.iloc[-1]
            cur_ema50 = ema50.iloc[-1]
            cur_hist  = hist.iloc[-1]
            prev_hist = hist.iloc[-2]
            cur_atr   = atr.iloc[-1]
            cur_vol   = volume.iloc[-1]
            avg_vol   = vol_ma.iloc[-1]
            vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0

            htf_1h = get_htf_trend(df1h)
            htf_4h = get_htf_trend(df4h)

            # ── Tính điểm LONG / SHORT ───────────────────────────────
            long_pts  = []
            short_pts = []

            # RSI
            if cur_rsi < 35:
                long_pts.append(f"RSI={cur_rsi:.1f} oversold 🔵")
            elif cur_rsi < 45 and cur_rsi > prev_rsi:
                long_pts.append(f"RSI={cur_rsi:.1f} đang tăng ↑")

            if cur_rsi > 65:
                short_pts.append(f"RSI={cur_rsi:.1f} overbought 🔴")
            elif cur_rsi > 55 and cur_rsi < prev_rsi:
                short_pts.append(f"RSI={cur_rsi:.1f} đang giảm ↓")

            # EMA
            if cur_ema9 > cur_ema21:
                long_pts.append("EMA9↑EMA21 uptrend")
            else:
                short_pts.append("EMA9↓EMA21 downtrend")

            if price > cur_ema50:
                long_pts.append("Giá trên EMA50")
            else:
                short_pts.append("Giá dưới EMA50")

            # MACD
            if cur_hist > 0 and cur_hist > prev_hist:
                long_pts.append("MACD histogram tăng ↑")
            elif cur_hist < 0 and cur_hist < prev_hist:
                short_pts.append("MACD histogram giảm ↓")

            # Bollinger Band
            if price <= bb_lo.iloc[-1] * 1.005:
                long_pts.append("Giá chạm BB lower")
            if price >= bb_up.iloc[-1] * 0.995:
                short_pts.append("Giá chạm BB upper")

            # Volume
            if vol_ratio >= 1.5:
                if cur_rsi < 50:
                    long_pts.append(f"Volume surge ×{vol_ratio:.1f}")
                else:
                    short_pts.append(f"Volume surge ×{vol_ratio:.1f}")

            # HTF trend
            if htf_1h == "UP":
                long_pts.append("HTF 1h: UP 🟢")
            elif htf_1h == "DOWN":
                short_pts.append("HTF 1h: DOWN 🔴")

            if htf_4h == "UP":
                long_pts.append("HTF 4h: UP 🟢")
            elif htf_4h == "DOWN":
                short_pts.append("HTF 4h: DOWN 🔴")

            long_score  = len(long_pts)
            short_score = len(short_pts)
            total_pts   = max(long_score + short_score, 1)

            # ── Quyết định tín hiệu ──────────────────────────────────
            if long_score >= 3 and long_score > short_score + 1:
                signal = "LONG"
            elif short_score >= 3 and short_score > long_score + 1:
                signal = "SHORT"
            else:
                signal = "WAIT"

            logger.info(f"[Telegram] Signal={signal} score L={long_score} S={short_score} for {symbol}")

            # ── Tính Entry / SL / TP ─────────────────────────────────
            atr_val = cur_atr
            sl_mult = getattr(self.config, "ATR_SL_MULTIPLIER", 2.0)
            tp_mult = getattr(self.config, "ATR_TP_MULTIPLIER", 4.0)

            if signal == "LONG":
                entry = price
                sl    = round(price - atr_val * sl_mult, _price_decimals(price))
                tp    = round(price + atr_val * tp_mult, _price_decimals(price))
            elif signal == "SHORT":
                entry = price
                sl    = round(price + atr_val * sl_mult, _price_decimals(price))
                tp    = round(price - atr_val * tp_mult, _price_decimals(price))
            else:
                entry = price
                sl    = round(price - atr_val * sl_mult, _price_decimals(price))
                tp    = round(price + atr_val * tp_mult, _price_decimals(price))

            risk_dist   = abs(entry - sl)
            reward_dist = abs(tp - entry)
            rr_ratio    = reward_dist / risk_dist if risk_dist > 0 else 0

            # ── Win rate ước tính dựa trên score ────────────────────
            # Công thức: base 45% + mỗi điểm thêm ~5%, tối đa 80%
            if signal == "LONG":
                win_rate = min(45 + long_score * 5, 80)
            elif signal == "SHORT":
                win_rate = min(45 + short_score * 5, 80)
            else:
                win_rate = 40

            # ── Thay đổi giá 24h ─────────────────────────────────────
            chg_pct = (price - close.iloc[-96]) / close.iloc[-96] * 100 if len(close) >= 96 else 0

            # ── Tạo message ──────────────────────────────────────────
            coin_name = symbol.replace("USDT", "")
            chg_arrow = "▲" if chg_pct >= 0 else "▼"

            if signal == "LONG":
                sig_icon = "🟢 LONG"
                sig_bar  = "🟩🟩🟩🟩🟩" + "⬜" * (5 - min(long_score, 5))
                reasons  = long_pts
            elif signal == "SHORT":
                sig_icon = "🔴 SHORT"
                sig_bar  = "🟥🟥🟥🟥🟥"[:min(short_score,5)*2] + "⬜" * (5 - min(short_score, 5))
                reasons  = short_pts
            else:
                sig_icon = "⏳ WAIT"
                sig_bar  = "⬜⬜⬜⬜⬜"
                reasons  = long_pts + short_pts

            lines = [
                f"📊 <b>PHÂN TÍCH {coin_name}/USDT</b>  <i>[{data_source}]</i>",
                f"━━━━━━━━━━━━━━━━━━━━━━━",
                f"💰 Giá hiện tại : <b>${price:,.{_price_decimals(price)}f}</b>  {chg_arrow}{abs(chg_pct):.2f}%",
                f"📈 ATR (14)     : ${atr_val:,.{_price_decimals(atr_val)}f}",
                f"📊 Volume       : ×{vol_ratio:.1f} so TB",
                f"",
                f"🎯 <b>TÍN HIỆU: {sig_icon}</b>",
                f"   {sig_bar}",
                f"   LONG [{long_score}đ]  SHORT [{short_score}đ]",
                f"",
                f"📋 <b>LÝ DO:</b>",
            ]
            for r in reasons[:5]:
                lines.append(f"  • {r}")

            lines += [
                f"",
                f"━━━━━━━━━━━━━━━━━━━━━━━",
                f"💵 Entry  : <b>${entry:,.{_price_decimals(entry)}f}</b>",
                f"🛑 SL     : <b>${sl:,.{_price_decimals(sl)}f}</b>  ({abs(entry-sl)/entry*100:.2f}%)",
                f"🎯 TP     : <b>${tp:,.{_price_decimals(tp)}f}</b>  ({abs(tp-entry)/entry*100:.2f}%)",
                f"📐 RR     : <b>1:{rr_ratio:.1f}</b>",
                f"🏆 Win rate ước tính: <b>~{win_rate}%</b>",
                f"",
                f"🌐 HTF 1h: {'🟢 UP' if htf_1h=='UP' else '🔴 DOWN' if htf_1h=='DOWN' else '⚪ NEUTRAL'}  "
                f"| 4h: {'🟢 UP' if htf_4h=='UP' else '🔴 DOWN' if htf_4h=='DOWN' else '⚪ NEUTRAL'}",
            ]

            msg = "\n".join(lines)

            logger.info(f"[Telegram] Building keyboard for {symbol} signal={signal}")

            # ── Inline keyboard — luôn cho chọn cả LONG và SHORT ──
            # Tính SL/TP cho hướng ngược lại
            if signal == "LONG":
                sl_reverse = round(price + atr_val * sl_mult, _price_decimals(price))
                tp_reverse = round(price - atr_val * tp_mult, _price_decimals(price))
            else:
                sl_reverse = round(price - atr_val * sl_mult, _price_decimals(price))
                tp_reverse = round(price + atr_val * tp_mult, _price_decimals(price))

            # Truncate prices cho callback_data (max 64 bytes)
            def _cb_price(p):
                if p >= 1000: return f"{p:.1f}"
                if p >= 1: return f"{p:.2f}"
                return f"{p:.4f}"

            if signal == "LONG":
                markup = {"inline_keyboard": [
                    [{"text": f"✅ LONG @ ${entry:,.{_price_decimals(entry)}f} (khuyến nghị)", "callback_data": f"trade_LONG_{_cb_price(sl)}_{_cb_price(tp)}_auto_{symbol}"}],
                    [{"text": f"⚡ SHORT @ ${entry:,.{_price_decimals(entry)}f}", "callback_data": f"trade_SHORT_{_cb_price(sl_reverse)}_{_cb_price(tp_reverse)}_auto_{symbol}"}],
                    [{"text": "❌ Bỏ qua", "callback_data": "cancel_trade"}],
                ]}
            elif signal == "SHORT":
                markup = {"inline_keyboard": [
                    [{"text": f"✅ SHORT @ ${entry:,.{_price_decimals(entry)}f} (khuyến nghị)", "callback_data": f"trade_SHORT_{_cb_price(sl)}_{_cb_price(tp)}_auto_{symbol}"}],
                    [{"text": f"⚡ LONG @ ${entry:,.{_price_decimals(entry)}f}", "callback_data": f"trade_LONG_{_cb_price(sl_reverse)}_{_cb_price(tp_reverse)}_auto_{symbol}"}],
                    [{"text": "❌ Bỏ qua", "callback_data": "cancel_trade"}],
                ]}
            else:
                # WAIT
                sl_long  = round(price - atr_val * sl_mult, _price_decimals(price))
                tp_long  = round(price + atr_val * tp_mult, _price_decimals(price))
                sl_short = round(price + atr_val * sl_mult, _price_decimals(price))
                tp_short = round(price - atr_val * tp_mult, _price_decimals(price))
                markup = {"inline_keyboard": [
                    [{"text": f"⚡ LONG @ ${entry:,.{_price_decimals(entry)}f}", "callback_data": f"trade_LONG_{_cb_price(sl_long)}_{_cb_price(tp_long)}_auto_{symbol}"}],
                    [{"text": f"⚡ SHORT @ ${entry:,.{_price_decimals(entry)}f}", "callback_data": f"trade_SHORT_{_cb_price(sl_short)}_{_cb_price(tp_short)}_auto_{symbol}"}],
                    [{"text": "❌ Bỏ qua", "callback_data": "cancel_trade"}],
                ]}

            self.send(msg, markup=markup)
            logger.info(f"[Telegram] Analysis sent for {symbol}")

        except Exception as e:
            logger.error(f"_analyze_and_send error: {e}", exc_info=True)
            self.send(f"❌ Lỗi phân tích {symbol}: {e}")

    def _get_positions_table(self, exchange, detailed: bool = False) -> str:
        """
        Tổng hợp tất cả lệnh đang mở thành bảng:
        Coin | Side | Entry | Mark | PnL$ | % | Lev
        """
        if not exchange:
            return "❌ Không kết nối được exchange"

        try:
            all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
            open_pos = [p for p in all_pos if abs(float(p.get("positionAmt", 0))) > 0]
        except Exception as e:
            return f"❌ Lỗi lấy positions: {e}"

        if not open_pos:
            return "💤 <b>Không có lệnh nào đang mở</b>"

        total_pnl = sum(float(p["unRealizedProfit"]) for p in open_pos)
        total_icon = "🟢" if total_pnl >= 0 else "🔴"

        lines = [f"📌 <b>LỆNH ĐANG MỞ ({len(open_pos)} lệnh)</b>\n"]

        # Header
        lines.append("<code>")
        lines.append(f"{'Coin':<10} {'Side':<6} {'Entry':>10} {'Mark':>10} {'PnL$':>8} {'%':>7} {'Lev':>4}")
        lines.append("─" * 58)

        for p in open_pos:
            sym   = p["symbol"].replace("USDT", "")
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            mark  = float(p.get("markPrice", entry))
            pnl   = float(p["unRealizedProfit"])
            lev   = int(float(p.get("leverage", self.config.LEVERAGE)))

            side = "LONG " if amt > 0 else "SHORT"

            # Tính % PnL theo chiều lệnh (có tính leverage) — giống Binance ROE%
            if entry > 0:
                raw_pct = (mark - entry) / entry * 100
                pnl_pct = (raw_pct if amt > 0 else -raw_pct) * lev
            else:
                pnl_pct = 0.0

            pnl_sign  = "+" if pnl >= 0 else ""
            pct_sign  = "+" if pnl_pct >= 0 else ""

            lines.append(
                f"{sym:<10} {side:<6} {entry:>10.4f} {mark:>10.4f} "
                f"{pnl_sign}{pnl:>7.2f} {pct_sign}{pnl_pct:>6.2f}% {lev:>3}x"
            )

        lines.append("─" * 58)
        pnl_sign = "+" if total_pnl >= 0 else ""
        lines.append(f"{'TỔNG PnL':<34} {pnl_sign}{total_pnl:>7.2f}")
        lines.append("</code>")
        lines.append(f"\n{total_icon} Tổng: <b>{'+' if total_pnl >= 0 else ''}{total_pnl:.2f} USDT</b>")

        return "\n".join(lines)

    def _get_exchange(self):
        """Lấy exchange instance từ state"""
        return self.state.get("_exchange")

    def _get_notifier(self):
        """Lấy notifier instance từ state"""
        return self.state.get("_notifier")

    def _execute_trade_from_callback(self, signal: str, sl: float, tp: float, symbol: str):
        """Thực thi lệnh sau khi user bấm xác nhận từ /trade — dùng smart entry"""
        try:
            exchange = self._get_exchange()
            if not exchange:
                self.send("❌ Không kết nối được exchange")
                return

            from smart_entry import find_optimal_entry, place_smart_order

            # Tìm entry tối ưu từ 1m/5m chart
            self.send(f"🔍 Phân tích chart 1m/5m tìm entry tối ưu...")
            entry_info = find_optimal_entry(exchange, symbol, signal, self.config)

            # Tính qty
            lev = getattr(self.config, "LEVERAGE", 10)
            max_usdt = getattr(self.config, "MAX_ORDER_USDT", 15)
            price = entry_info["entry_price"]
            qty = (max_usdt * lev) / price
            qty = round(qty, _qty_decimals(price))
            min_q = _min_qty(price)
            if qty < min_q:
                qty = min_q
                # Check nếu margin vượt balance thì báo lỗi
                actual_margin = qty * price / lev
                bal = exchange.get_account_balance()
                if actual_margin > bal:
                    self.send(f"❌ Không đủ balance. {symbol} cần tối thiểu ${actual_margin:.2f} margin (min qty={min_q})")
                    return

            # Set leverage
            try:
                exchange.set_leverage(symbol, lev)
            except Exception:
                pass

            # Đặt lệnh thông minh
            result = place_smart_order(exchange, symbol, signal, qty, entry_info, self.config,
                                        bot_state=self.state, bot_lock=self.lock)

            rr = abs(entry_info["tp"] - price) / abs(price - entry_info["sl"]) if abs(price - entry_info["sl"]) > 0 else 0
            notional = qty * price
            improvement = entry_info["improvement_pct"]

            if result["type"] == "LIMIT":
                order_type_msg = f"📋 LIMIT ORDER (chờ khớp)\n💡 Tốt hơn market: <b>{improvement:.2f}%</b>"
            else:
                order_type_msg = f"⚡ MARKET ORDER (khớp ngay)"

            # Levels chi tiết
            levels_msg = ""
            if entry_info.get("levels"):
                lvl_parts = []
                for name, val in entry_info["levels"].items():
                    lvl_parts.append(f"{name}=${val:,.2f}")
                levels_msg = "\n🔬 Levels: " + " | ".join(lvl_parts[:5])

            self.send(
                f"🚀 <b>ĐÃ VÀO LỆNH {signal} — {symbol}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💵 Entry   : <b>${price:,.4f}</b>\n"
                f"📊 Current : ${entry_info['current_price']:,.4f}\n"
                f"🛑 SL      : <b>${entry_info['sl']:,.4f}</b>  ({abs(price-entry_info['sl'])/price*100:.2f}%)\n"
                f"🎯 TP      : <b>${entry_info['tp']:,.4f}</b>  ({abs(entry_info['tp']-price)/price*100:.2f}%)\n"
                f"📐 RR      : <b>1:{rr:.1f}</b>\n"
                f"📦 Qty     : <b>{qty}</b>  (~${notional:,.2f} USDT)\n"
                f"⚡ Leverage: <b>{lev}x</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{order_type_msg}\n"
                f"🧠 Method  : {entry_info['method']}\n"
                f"🎯 Confluence: <b>{entry_info.get('confluence_score', 0)}/7</b>"
                f"{levels_msg}"
            )

        except Exception as e:
            logger.error(f"_execute_trade_from_callback error: {e}", exc_info=True)
            self.send(f"❌ Lỗi đặt lệnh: {e}")

    def _answer_callback(self, callback_query_id: str, text: str = "✅"):
        """Trả lời callback query để tắt loading spinner trên nút"""
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=5
            )
        except Exception:
            pass

    def run(self):
        """Chạy ngầm, lắng nghe lệnh từ Telegram"""
        logger.info("Telegram command handler started")

        # Bỏ qua tất cả updates cũ khi bot mới start
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"offset": -1, "timeout": 1},
                timeout=5
            )
            results = resp.json().get("result", [])
            if results:
                self.last_update_id = results[-1]["update_id"]
                logger.info(f"Skipped {len(results)} old updates (last_id={self.last_update_id})")
        except Exception:
            pass

        self.send("🎮 <b>Bot sẵn sàng nhận lệnh!</b>\nGõ /help để xem danh sách lệnh",
                 markup={"keyboard": [
                     [{"text": "📊 Status"}, {"text": "💼 Balance"}],
                     [{"text": "📌 Position"}, {"text": "📋 Orders"}],
                     [{"text": "🟢 LONG"}, {"text": "🔴 SHORT"}],
                     [{"text": "🛡️ Set SL/TP"}, {"text": "📈 Stats"}],
                     [{"text": "🕐 History"}, {"text": "❌ Close Position"}],
                     [{"text": "⚙️ Score"}, {"text": "📤 Export"}],
                 ], "resize_keyboard": True})

        while self.running and self.state.get("running", True):
            updates = self.get_updates()
            for update in updates:
                self.last_update_id = update["update_id"]

                # ── Xử lý callback (bấm nút inline keyboard) ──
                cb = update.get("callback_query")
                if cb:
                    if str(cb.get("from", {}).get("id")) != str(self.chat_id):
                        continue
                    data = cb.get("data", "")
                    self._answer_callback(cb["id"])

                    if data.startswith("trade_"):
                        # Format: trade_LONG_sl_tp_auto_BTCUSDT
                        parts_cb = data.split("_")
                        if len(parts_cb) >= 6:
                            sig    = parts_cb[1]           # LONG / SHORT
                            sl_val = float(parts_cb[2])
                            tp_val = float(parts_cb[3])
                            # parts_cb[4] = "auto"
                            sym    = parts_cb[5]           # BTCUSDT
                            self.send(f"⏳ Đang đặt lệnh {sig} {sym}...")
                            # Chạy trên thread riêng để không block polling
                            t = threading.Thread(
                                target=self._execute_trade_from_callback,
                                args=(sig, sl_val, tp_val, sym),
                                daemon=True
                            )
                            t.start()
                        else:
                            self.send("❌ Callback data lỗi định dạng")

                    elif data.startswith("quick_"):
                        # Format: quick_LONG_BTCUSDT or quick_SHORT_SOLUSDT
                        parts_cb = data.split("_")
                        if len(parts_cb) >= 3:
                            sig = parts_cb[1]              # LONG / SHORT
                            sym = parts_cb[2]              # BTCUSDT
                            self.send(f"🔍 Đang phân tích <b>{sym}</b> cho {sig}...")
                            t = threading.Thread(
                                target=self._analyze_and_send,
                                args=(sym,),
                                daemon=True
                            )
                            t.start()

                    elif data.startswith("cancelorders_"):
                        # Format: cancelorders_BTCUSDT or cancelorders_ALL
                        target = data.replace("cancelorders_", "")
                        exchange = self._get_exchange()
                        if exchange:
                            if target == "ALL":
                                try:
                                    all_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                                    symbols = list(set(o.get("symbol", "") for o in all_orders))
                                    for s in symbols:
                                        exchange.cancel_all_orders(s)
                                    self.send(f"✅ Đã hủy tất cả {len(all_orders)} orders")
                                except Exception as e:
                                    self.send(f"❌ Lỗi: {e}")
                            else:
                                try:
                                    exchange.cancel_all_orders(target)
                                    self.send(f"✅ Đã hủy orders cho <b>{target}</b>")
                                except Exception as e:
                                    self.send(f"❌ Lỗi: {e}")

                    elif data.startswith("settp_"):
                        # Format: settp_BTCUSDT or settp_ALL
                        target = data.replace("settp_", "")
                        exchange = self._get_exchange()
                        if exchange:
                            from auto_sltp import get_positions_without_sltp, auto_set_sltp
                            liq_tracker = self.state.get("liq_tracker")
                            unprotected = get_positions_without_sltp(exchange)

                            if target == "ALL":
                                results = []
                                for pos in unprotected:
                                    r = auto_set_sltp(exchange, pos["symbol"], pos["side"],
                                                     pos["entry"], pos["qty"], liq_tracker)
                                    results.append(r["msg"])
                                self.send("\n\n".join(results) if results else "✅ Không có gì cần set")
                            else:
                                pos = next((p for p in unprotected if p["symbol"] == target), None)
                                if pos:
                                    r = auto_set_sltp(exchange, pos["symbol"], pos["side"],
                                                     pos["entry"], pos["qty"], liq_tracker)
                                    self.send(r["msg"])
                                else:
                                    self.send(f"ℹ️ {target} đã có SL/TP hoặc không có position")

                    elif data == "cancel_trade":
                        self.send("❌ <b>Đã hủy.</b>")

                    continue

                # ── Xử lý text message ──
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                    continue
                text = msg.get("text", "")
                if not text:
                    continue

                # Map keyboard button text → command
                button_map = {
                    "📊 status": "/status",
                    "💼 balance": "/balance",
                    "📌 position": "/position",
                    "📋 orders": "/orders",
                    "🟢 long": "/long",
                    "🔴 short": "/short",
                    "📈 stats": "/stats",
                    "🕐 history": "/history",
                    "❌ close position": "/closeall",
                    "📺 dashboard": "/dashboard",
                    "🛡️ set sl/tp": "/settp",
                    "⚙️ score": "/score",
                    "📤 export": "/export",
                }
                text_lower = text.strip().lower()
                if text_lower in button_map:
                    text = button_map[text_lower]

                if text.startswith("/"):
                    # Commands that call _analyze_and_send run on separate thread
                    cmd_lower = text.split()[0].lower()
                    if cmd_lower in ("/trade", "/long", "/short") or cmd_lower.startswith("/trade"):
                        t = threading.Thread(
                            target=self._handle_and_reply,
                            args=(text,),
                            daemon=True
                        )
                        t.start()
                    else:
                        reply = self.handle(text)
                        if reply:
                            self.send(reply)

            time.sleep(2)

    def _handle_and_reply(self, text: str):
        """Handle command on separate thread and send reply"""
        try:
            reply = self.handle(text)
            if reply:
                self.send(reply)
        except Exception as e:
            logger.error(f"_handle_and_reply error: {e}")
            self.send(f"❌ Lỗi: {e}")

            time.sleep(2)


# ── Helper functions (module-level) ──────────────────────────

def _price_decimals(price: float) -> int:
    """Số chữ số thập phân phù hợp theo giá"""
    if price >= 1000:  return 1
    if price >= 100:   return 2
    if price >= 10:    return 3
    if price >= 1:     return 4
    if price >= 0.1:   return 5
    return 6

def _qty_decimals(price: float) -> int:
    """Số chữ số thập phân cho qty"""
    if price >= 10000: return 3    # BTC: 0.001
    if price >= 1000:  return 3    # ETH: 0.001
    if price >= 100:   return 2    # BNB: 0.01
    if price >= 10:    return 1    # SOL: 0.1
    return 0

def _min_qty(price: float) -> float:
    """Qty tối thiểu theo giá coin (Binance Futures rules)"""
    if price >= 10000: return 0.001   # BTC
    if price >= 1000:  return 0.001   # ETH
    if price >= 100:   return 0.01    # BNB
    if price >= 10:    return 0.1     # SOL
    if price >= 1:     return 1.0     # XRP, altcoins ~$1-$10
    return 10.0
