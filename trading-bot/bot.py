# ============================================================
# MULTI-COIN TRADING BOT — Dashboard + Auto Trade
# ============================================================
import time, logging, os, sys, threading

# Đảm bảo thư mục chứa bot.py luôn có trong sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests as _req
import pandas as pd
from datetime import datetime

# ── SINGLE INSTANCE LOCK — chỉ cho phép 1 bot chạy ──
_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.lock")
def _check_single_instance():
    global _lock_fp
    if sys.platform == "win32":
        # Windows: dùng msvcrt thay fcntl
        import msvcrt
        _lock_fp = open(_LOCK_FILE, 'w')
        try:
            msvcrt.locking(_lock_fp.fileno(), msvcrt.LK_NBLCK, 1)
            _lock_fp.write(str(os.getpid()))
            _lock_fp.flush()
        except OSError:
            print("⚠️ Bot đã đang chạy (instance khác). Thoát.")
            sys.exit(0)
    else:
        # Linux/macOS: dùng fcntl
        import fcntl
        _lock_fp = open(_LOCK_FILE, 'w')
        try:
            fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _lock_fp.write(str(os.getpid()))
            _lock_fp.flush()
        except IOError:
            print("⚠️ Bot đã đang chạy (instance khác). Thoát.")
            sys.exit(0)

_check_single_instance()

# Print server IP on startup (for Binance whitelist)
try:
    _my_ip = _req.get("https://ifconfig.me", timeout=5).text.strip()
    print(f"🌐 SERVER IP: {_my_ip}")
except:
    _my_ip = "unknown"
    print("⚠️ Could not detect server IP")

import config
from exchange import BinanceFutures
from indicators import calculate_atr, get_signal
from scanner import scan_market, WATCHLIST, _klines_to_df
from notifier import Notifier
from liquidation_tracker import LiquidationTracker
from liq_strategy import LiqStrategy, SplitPosition

os.makedirs("logs", exist_ok=True)

# Rotating log: tối đa 5MB/file, giữ 2 file backup → tự xóa cũ
from logging.handlers import RotatingFileHandler
_log_handler = RotatingFileHandler(
    config.LOG_FILE,
    maxBytes=5 * 1024 * 1024,   # 5MB
    backupCount=2,
    encoding="utf-8"
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(
    level=logging.INFO,          # Chỉ INFO trở lên, bỏ DEBUG
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_log_handler]
)
logger = logging.getLogger(__name__)

# ============================================================
# SHARED STATE
# ============================================================
state = {
    "prices":         {},
    "balance":        0.0,
    "scan_no":        0,
    "last_scan":      "--:--",
    "position":       None,
    "symbol":         None,
    "entry":          0.0,
    "sl":             0.0,
    "tp":             0.0,
    "qty":            0.0,
    "candidates":     [],
    "trade_log":      [],
    "open_positions": [],   # Tất cả positions từ Binance API
    "running":        True,
    # --- Liquidation strategy state ---
    "split_positions": {},  # {symbol: SplitPosition} — các lệnh split đang chờ/mở
    "liq_data":       {},   # {symbol: total_liq_usd} — để hiển thị dashboard
    "pending_smart_orders": {},  # {order_id: {symbol, side, qty, sl, tp}} — limit orders chờ fill
}
lock = threading.Lock()

# ============================================================
# DASHBOARD
# ============================================================
# Bật ANSI escape codes trên Windows
if os.name == "nt":
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)

_dashboard_initialized = False

def clear():
    os.system("cls" if os.name == "nt" else "clear")

def print_dashboard():
    import io
    _buf = io.StringIO()
    _real_stdout = sys.stdout
    sys.stdout = _buf
    with lock: s = dict(state); tlog = list(state["trade_log"]); grids = dict(state.get("grids", {}))
    W = 58
    def row(t=""): return f"║  {t:<{W-4}}║"
    now = datetime.now().strftime("%H:%M:%S")

    closed = [t for t in tlog if t["status"] == "CLOSED"]
    closed_real = [t for t in closed if abs(t.get("pnl_usdt", 0)) > 0.001]
    total_pnl = sum(t.get("pnl_usdt", 0) for t in closed_real)
    wins  = sum(1 for t in closed_real if t.get("pnl_usdt", 0) > 0)
    loss  = len(closed_real) - wins
    wr    = wins / len(closed_real) * 100 if closed_real else 0
    avg_win  = sum(t.get("pnl_usdt",0) for t in closed_real if t.get("pnl_usdt",0)>0) / max(wins,1)
    avg_loss = sum(t.get("pnl_usdt",0) for t in closed_real if t.get("pnl_usdt",0)<=0) / max(loss,1)
    pnl_icon = "📈" if total_pnl >= 0 else "📉"

    # Số coin đang scan
    from scanner import WATCHLIST as _wl
    n_scanning = len(_wl)

    clear()
    print("╔" + "═"*W + "╗")
    print("║" + " 🤖  MULTI-COIN BOT — BINANCE FUTURES ".center(W) + "║")
    print("╠" + "═"*W + "╣")
    print(row(f"🕐 {now}   💼 Balance: ${s['balance']:,.2f} USDT"))
    print(row(f"🔍 Scanning: {n_scanning} coins  |  Scan #{s['scan_no']}  ({s['last_scan']})"))
    print(row(f"{pnl_icon} PnL: ${total_pnl:+.2f}  |  ✅{wins}W ❌{loss}L  WR:{wr:.0f}%"))

    # Liq tracker status
    liq_data = s.get("liq_data", {})
    liq_ws   = s.get("liq_connected", False)
    ws_icon  = "🟢" if liq_ws else "🔴"
    if liq_data:
        liq_parts = [f"{sym.replace('USDT','')}:${v/1e6:.1f}M" for sym,v in list(liq_data.items())[:4]]
        print(row(f"{ws_icon} LiqWS  |  " + "  ".join(liq_parts)))
    else:
        print(row(f"{ws_icon} LiqWS: {'connected, warming up...' if liq_ws else 'connecting...'}"))

    # Split positions đang chờ/mở
    splits = s.get("split_positions", {})
    if splits:
        print("╠" + "═"*W + "╣")
        print("║" + " ⚡  SPLIT POSITIONS (LIQ STRATEGY) ".center(W) + "║")
        for sym_sp, sp in splits.items():
            f1 = "✅" if sp.filled1 else "⏳"
            f2 = "✅" if sp.filled2 else "⏳"
            icon = "🟢" if sp.direction == "LONG" else "🔴"
            print(row(
                f"{icon}{sym_sp:<10} {sp.direction:<5} "
                f"E1:{f1}${sp.entry1:.2f}  E2:{f2}${sp.entry2:.2f}  "
                f"SL:${sp.sl:.2f}  TP:${sp.tp:.2f}"
            ))
    if closed_real:
        rr_color = "🟢" if avg_win > abs(avg_loss) else "🔴"
        print(row(f"{rr_color} Avg Win: ${avg_win:+.2f}  |  Avg Loss: ${avg_loss:+.2f}  |  RR:{abs(avg_win/avg_loss):.1f}x" if avg_loss != 0 else f"Avg Win: ${avg_win:+.2f}"))
    print("╠" + "═"*W + "╣")

    # --- Tất cả positions đang mở (đọc từ Binance) ---
    open_positions = s.get("open_positions", [])
    if open_positions:
        total_unrealized = sum(p.get("_pnl", 0.0) for p in open_positions)
        pnl_icon2 = "📈" if total_unrealized >= 0 else "📉"
        print("║" + f" 📌  {len(open_positions)} LỆNH ĐANG MỞ  |  {pnl_icon2} Unrealized: ${total_unrealized:+.2f} ".center(W) + "║")
        print(row(f"{'Coin':<10} {'Side':<5} {'Entry':>8} {'Mark':>8} {'PnL$':>8} {'%':>6} {'Lev'}"))
        print(row("─"*(W-6)))
        for p in open_positions:
            sym   = p["symbol"]
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            mark  = p.get("_mark", s["prices"].get(sym, entry))
            pnl   = p.get("_pnl", 0.0)
            pct   = p.get("_pct", 0.0)
            lev   = p.get("_lev", config.LEVERAGE)
            side  = "LONG" if amt > 0 else "SHORT"
            icon  = "🟢" if side=="LONG" else "🔴"
            print(row(f"{icon}{sym:<9} {side:<5} ${entry:>7.4f} ${mark:>7.4f} ${pnl:>+7.2f} {pct:>+5.1f}% {lev}x"))
    elif s["position"]:
        # Fallback: dùng state nội bộ
        cp = s["prices"].get(s["symbol"], s["entry"])
        pnl_pct = (cp-s["entry"])/s["entry"]*100 if s["position"]=="LONG" else (s["entry"]-cp)/s["entry"]*100
        pnl_usd = pnl_pct/100*s["entry"]*config.LEVERAGE
        pnl_icon2 = "📈" if pnl_usd >= 0 else "📉"
        side_tag = "🟢 LONG" if s["position"]=="LONG" else "🔴 SHORT"
        sl_dist_pct = abs(cp-s["sl"])/cp*100 if cp else 0
        tp_dist_pct = abs(s["tp"]-cp)/cp*100 if cp else 0
        progress = min(abs(cp-s["entry"])/abs(s["tp"]-s["entry"]),1.0) if s["tp"]!=s["entry"] else 0
        prog_bar = "█"*int(progress*10) + "░"*(10-int(progress*10))
        print("║" + " 📌  LỆNH ĐANG MỞ (REALTIME) ".center(W) + "║")
        print(row(f"Coin     : {s['symbol']}   {side_tag}   {config.LEVERAGE}x"))
        print(row(f"Entry    : ${s['entry']:.4f}   ▶  Giá HT: ${cp:.4f}"))
        print(row(f"🛑 SL    : ${s['sl']:.4f}   (còn {sl_dist_pct:.2f}% đến SL)"))
        print(row(f"🎯 TP    : ${s['tp']:.4f}   (còn {tp_dist_pct:.2f}% đến TP)"))
        print(row(f"📦 Qty   : {s['qty']}   (~${s['qty']*s['entry']:,.2f} USDT)"))
        print(row(f"Progress : [{prog_bar}] {progress*100:.0f}%"))
        print(row(f"{pnl_icon2} PnL    : ${pnl_usd:+.2f}  ({pnl_pct:+.2f}%)  x{config.LEVERAGE}"))
    else:
        pass  # Sẽ hiện ở dưới cùng

    print("╠" + "═"*W + "╣")

    # --- Giá realtime 4 coin/dòng ---
    print("║" + " 💹  GIÁ REALTIME ".center(W) + "║")
    prices = s["prices"]
    for i in range(0, len(WATCHLIST), 4):
        parts = []
        for sym in WATCHLIST[i:i+4]:
            p = prices.get(sym, 0)
            name = sym.replace("USDT","")
            if p >= 1000:   parts.append(f"{name:<5}${p:>9,.0f}")
            elif p >= 1:    parts.append(f"{name:<5}${p:>8.3f}")
            else:           parts.append(f"{name:<5}${p:>9.5f}")
        print(row("  ".join(parts)))

    print("╠" + "═"*W + "╣")

    # --- Top signals ---
    print("║" + f" 📊  TOP SIGNALS (scan {s['last_scan']}) ".center(W) + "║")
    if s["candidates"]:
        for c in s["candidates"][:5]:
            filled  = int(c.score / 10)
            bar     = "█" * filled + "░" * (10 - filled)
            pct     = c.score          # score 0-100 = %
            if c.signal == "LONG":
                tag   = "\033[92m▲LONG \033[0m"   # xanh lá
                bar_c = f"\033[92m{bar}\033[0m"
                pct_c = f"\033[92m{pct:.0f}%\033[0m"
            else:
                tag   = "\033[91m▼SHORT\033[0m"   # đỏ
                bar_c = f"\033[91m{bar}\033[0m"
                pct_c = f"\033[91m{pct:.0f}%\033[0m"
            # RSI màu theo mức
            if c.rsi >= 65:
                rsi_c = f"\033[91mRSI={c.rsi}\033[0m"   # đỏ = overbought
            elif c.rsi <= 35:
                rsi_c = f"\033[92mRSI={c.rsi}\033[0m"   # xanh = oversold
            else:
                rsi_c = f"\033[93mRSI={c.rsi}\033[0m"   # vàng = neutral
            sym = c.symbol.replace("USDT", "")
            line = f"{sym:<10} {tag} [{bar_c}] {pct_c} {rsi_c}"
            print("║  " + line)
    else:
        print(row("Chưa có coin nào đủ điểm"))

    print("╠" + "═"*W + "╣")

    # --- Thống kê lãi lỗ ---
    closed    = [t for t in tlog if t["status"] == "CLOSED"]
    wins      = sum(1 for t in closed if t.get("pnl_usdt", 0) > 0)
    losses    = len(closed) - wins
    total_pnl = sum(t.get("pnl_usdt", 0) for t in closed)
    unrealized = sum(p.get("_pnl", 0) for p in s.get("open_positions", []))
    winrate   = wins / len(closed) * 100 if closed else 0
    pnl_icon  = "📈" if total_pnl >= 0 else "📉"

    print("║" + " 💰  THỐNG KÊ LÃI LỖ ".center(W) + "║")
    print(row(f"Realized : ${total_pnl:+.2f}   Unrealized: ${unrealized:+.2f}   Tổng: ${total_pnl+unrealized:+.2f}"))
    print(row(f"Lệnh: {len(closed)}  ✅{wins} win  ❌{losses} loss  WinRate: {winrate:.0f}%"))

    # Grid bots status
    grids_local = s.get("grids", {})
    if grids_local:
        grid_profit = sum(g.get_status()["total_profit"] for g in grids_local.values())
        grid_trades = sum(g.get_status()["trade_count"] for g in grids_local.values())
        print(row(f"🔲 Grid: {len(grids_local)} bots  {grid_trades} trades  Profit: ${grid_profit:+.4f}"))
        for sym_g, g in grids_local.items():
            st = g.get_status()
            print(row(f"  {sym_g:<12} ${st['lower']:.2f}-${st['upper']:.2f}  {st['trade_count']}t  ${st['total_profit']:+.4f}"))
    print("║" + " 📋  LỊCH SỬ LỆNH ".center(W) + "║")
    if closed:
        # Lọc lệnh có PnL thực (bỏ lệnh $0.00 từ Binance sync)
        closed_real = [t for t in closed if abs(t.get("pnl_usdt", 0)) > 0.001]
        # Sort gần nhất lên đầu
        closed_sorted = sorted(closed_real, key=lambda t: t.get("time", ""), reverse=True)
        wins_r  = sum(1 for t in closed_real if t.get("pnl_usdt", 0) > 0)
        loss_r  = sum(1 for t in closed_real if t.get("pnl_usdt", 0) <= 0)
        wr      = wins_r / len(closed_real) * 100 if closed_real else 0
        print(row(f"Tổng: {len(closed_real)}  ✅{wins_r}W ❌{loss_r}L  WR:{wr:.0f}%  PnL: ${total_pnl:+.2f}"))
        print(row("─"*(W-6)))
        print(row(f"{'#':<3} {'Coin':<10} {'Side':<5} {'Entry':>8} {'Close':>8} {'PnL$':>8} {'%':>6} {'Giờ':>5}"))
        print(row("─"*(W-6)))
        for i, t in enumerate(closed_sorted, 1):
            p       = t.get("pnl_usdt", 0)
            pct     = t.get("pnl_pct", 0)
            close_p = t.get("close", 0)
            icon    = "✅" if p > 0 else "❌"
            sym     = t['symbol'].replace("USDT","")
            # Màu % lời/lỗ
            if p > 0:
                pct_str = f"\033[92m+{pct:.1f}%\033[0m"
                pnl_str = f"\033[92m${p:+.2f}\033[0m"
            else:
                pct_str = f"\033[91m{pct:.1f}%\033[0m"
                pnl_str = f"\033[91m${p:+.2f}\033[0m"
            print("║  " + f"{icon}{i:<2} {sym:<9} {t['side']:<5} ${t['entry']:>7.4f} ${close_p:>7.4f} {pnl_str:>8} {pct_str:>6} {t['time'][11:16]}")
        # Dòng tổng kết avg %
        avg_win_pct  = sum(t.get("pnl_pct",0) for t in closed_real if t.get("pnl_pct",0)>0) / max(wins,1)
        avg_loss_pct = sum(t.get("pnl_pct",0) for t in closed_real if t.get("pnl_pct",0)<=0) / max(loss,1)
        print(row("─"*(W-6)))
        print("║  " + f"\033[92m✅ Avg lời: +{avg_win_pct:.2f}%\033[0m  |  \033[91m❌ Avg lỗ: {avg_loss_pct:.2f}%\033[0m  |  WR: {wr:.0f}%")
    else:
        print(row("Chưa có lệnh nào được đóng"))

    # --- Status dưới cùng ---
    print("╠" + "═"*W + "╣")
    if not s["position"] and not s.get("open_positions"):
        print("║" + " 💤  CHƯA CÓ LỆNH — Đang quét thị trường... ".center(W) + "║")
        print(row(f"  Last scan: {s['last_scan']}   |   Next scan: ~60s   |   Leverage: {config.LEVERAGE}x   |   Max: ${config.MAX_ORDER_USDT}"))
    else:
        print(row(f"  ✅ Bot đang chạy   |   Last scan: {s['last_scan']}   |   {config.LEVERAGE}x   |   Max ${config.MAX_ORDER_USDT}/lệnh"))

    print("╚" + "═"*W + "╝")
    print("  ⌨️  Ctrl+C để dừng  |  Telegram: /help")

    # Restore stdout trước, clear sau, rồi mới write — đúng thứ tự
    sys.stdout = _real_stdout
    output = _buf.getvalue()
    clear()
    sys.stdout.write(output)
    sys.stdout.flush()

# ============================================================
# THREAD 0: Dashboard refresh mỗi 1 giây (độc lập)
# ============================================================
def dashboard_updater():
    if not sys.stdout.isatty():
        return
    while state["running"]:
        try:
            print_dashboard()
        except Exception:
            pass
        time.sleep(28800)  # 8 tiếng

# ============================================================
# THREAD 1a: Giá realtime qua WebSocket (cập nhật mỗi 100ms)
# ============================================================
def price_ws_streamer():
    """WebSocket stream giá realtime từ Binance — nhanh hơn REST 30 lần"""
    import websocket as ws_lib
    import json as _json

    symbols = [s.lower() for s in WATCHLIST]
    streams = "/".join([f"{s}@markPrice@1s" for s in symbols])

    base_ws = "wss://fstream.binance.com" if not config.USE_TESTNET else "wss://stream.binancefuture.com"
    url = f"{base_ws}/stream?streams={streams}"

    def on_message(wsapp, message):
        try:
            data = _json.loads(message)
            payload = data.get("data", {})
            sym = payload.get("s", "")
            mark = float(payload.get("p", 0))
            if sym and mark > 0:
                with lock:
                    state["prices"][sym] = mark
        except Exception:
            pass

    def on_error(wsapp, error):
        logger.debug(f"Price WS error: {error}")

    def on_close(wsapp, close_code, close_msg):
        logger.debug("Price WS closed, reconnecting in 3s...")

    while state["running"]:
        try:
            wsapp = ws_lib.WebSocketApp(
                url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            wsapp.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            logger.debug(f"Price WS exception: {e}")
        if state["running"]:
            time.sleep(3)


# ============================================================
# THREAD 1b: Position/Balance updater mỗi 5 giây (REST)
# ============================================================
def price_updater(exchange):
    consecutive_errors = 0
    while state["running"]:
        try:
            # Giá đã được WebSocket cập nhật, chỉ dùng REST cho coins chưa có giá
            new_prices = {}
            for sym in WATCHLIST:
                with lock:
                    if sym not in state["prices"] or state["prices"][sym] == 0:
                        try: new_prices[sym] = exchange.get_ticker_price(sym)
                        except: pass
            consecutive_errors = 0

            # Lấy tất cả positions đang mở từ Binance
            try:
                all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
                open_pos = [p for p in all_pos if abs(float(p.get("positionAmt", 0))) > 0]
                for p in open_pos:
                    sym   = p["symbol"]
                    amt   = float(p.get("positionAmt", 0))
                    entry = float(p.get("entryPrice", 0))
                    mark  = float(p.get("markPrice", 0)) or new_prices.get(sym, entry)
                    lev   = int(float(p.get("leverage", config.LEVERAGE)))
                    side  = "LONG" if amt > 0 else "SHORT"
                    pnl   = abs(amt) * (mark - entry) if side == "LONG" else abs(amt) * (entry - mark)
                    pct   = ((mark - entry) / entry * 100 * lev) if side == "LONG" else ((entry - mark) / entry * 100 * lev)
                    p["_mark"] = mark
                    p["_pnl"]  = pnl
                    p["_pct"]  = pct
                    p["_lev"]  = lev
            except:
                open_pos = []

            with lock:
                state["prices"].update(new_prices)
                state["balance"] = exchange.get_account_balance()

                # ── Detect positions closed externally (app/web Binance) ──
                prev_positions = {p["symbol"] for p in state.get("open_positions", [])
                                  if abs(float(p.get("positionAmt", 0))) > 0}
                curr_positions = {p["symbol"] for p in open_pos}
                closed_externally = prev_positions - curr_positions

                for sym in closed_externally:
                    # Tìm lệnh OPEN tương ứng trong trade_log
                    for t in reversed(state.get("trade_log", [])):
                        if t.get("symbol") == sym and t.get("status") == "OPEN":
                            # Lấy giá đóng từ price
                            close_price = state["prices"].get(sym, t.get("entry", 0))
                            entry = t.get("entry", 0)
                            side = t.get("side", "LONG")
                            qty = t.get("qty", 0)
                            if entry > 0:
                                pnl_pct = (close_price - entry) / entry * 100 if side == "LONG" else (entry - close_price) / entry * 100
                                pnl_usd = qty * abs(close_price - entry) * (1 if pnl_pct > 0 else -1)
                            else:
                                pnl_pct = 0
                                pnl_usd = 0
                            t.update({
                                "status": "CLOSED",
                                "close": close_price,
                                "pnl_usdt": round(pnl_usd, 2),
                                "pnl_pct": round(pnl_pct, 2),
                                "note": "closed_external"
                            })
                            logger.info(f"[Sync] Detected external close: {sym} PnL=${pnl_usd:+.2f}")
                            from trade_history import save_history
                            save_history(state["trade_log"])
                            # Notify
                            try:
                                notifier_inst = state.get("_notifier")
                                if notifier_inst:
                                    icon = "✅" if pnl_usd >= 0 else "❌"
                                    notifier_inst.telegram.send(
                                        f"🔒 <b>LỆNH ĐÓNG (từ Binance app)</b>\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"📊 {sym} {side}\n"
                                        f"💵 PnL: <b>{icon} ${pnl_usd:+.2f}</b> ({pnl_pct:+.1f}%)\n"
                                        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                    )
                            except Exception:
                                pass
                            break

                state["open_positions"] = open_pos

            # ── Max loss check: đóng lệnh nếu lỗ > MAX_LOSS_PER_POSITION ──
            max_loss = getattr(config, "MAX_LOSS_PER_POSITION", 10.0)
            for p in open_pos:
                pnl = p.get("_pnl", 0)
                if pnl < -max_loss:
                    sym = p["symbol"]
                    amt = float(p.get("positionAmt", 0))
                    close_side = "SELL" if amt > 0 else "BUY"
                    qty = abs(amt)
                    if qty == int(qty):
                        qty = int(qty)
                    try:
                        # Chia batch nếu qty > 100k
                        remaining = qty
                        while remaining > 0:
                            batch = min(remaining, 100000)
                            if batch == int(batch):
                                batch = int(batch)
                            exchange.place_market_order(sym, close_side, batch)
                            remaining -= batch
                        exchange.cancel_all_orders(sym)
                        logger.info(f"[MAX LOSS] Closed {sym} pnl=${pnl:.2f} (exceeded -${max_loss})")

                        # Notify
                        from notifier import Notifier, NOTIFICATION_CONFIG
                        try:
                            notifier_inst = state.get("_notifier")
                            if notifier_inst:
                                notifier_inst.telegram.send(
                                    f"🚨 <b>MAX LOSS HIT — AUTO CLOSE</b>\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"📊 {sym}\n"
                                    f"💵 PnL: <b>${pnl:.2f}</b> (exceeded -${max_loss})\n"
                                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                )
                        except Exception:
                            pass

                        # Update trade log
                        with lock:
                            for t in reversed(state.get("trade_log", [])):
                                if t.get("symbol") == sym and t.get("status") == "OPEN":
                                    t.update({"status": "CLOSED", "close": p.get("_mark", 0),
                                              "pnl_usdt": round(pnl, 2), "pnl_pct": round(p.get("_pct", 0), 2)})
                                    break
                        from trade_history import save_history
                        save_history(state["trade_log"])
                    except Exception as e:
                        logger.error(f"[MAX LOSS] Close failed {sym}: {e}")

        except Exception as e:
            consecutive_errors += 1
            wait = min(30, 5 * consecutive_errors)
            logger.error(f"Price updater: {e} — retry in {wait}s ({consecutive_errors} errors)")
            time.sleep(wait)
            continue
        time.sleep(10)

# ============================================================
# THREAD 2: Trade engine mỗi 60 giây
# ============================================================
def calc_qty(balance, entry, sl):
    # Luôn dùng MAX_ORDER_USDT làm margin mỗi lệnh
    max_notional = config.MAX_ORDER_USDT * config.LEVERAGE
    qty = max_notional / entry

    # Cap max qty (Binance limit cho hầu hết coin)
    if entry >= 10000:    max_qty = 100
    elif entry >= 1000:   max_qty = 1000
    elif entry >= 100:    max_qty = 10000
    elif entry >= 10:     max_qty = 100000
    elif entry >= 1:      max_qty = 500000
    elif entry >= 0.01:   max_qty = 5000000
    else:                 max_qty = 50000000
    qty = min(qty, max_qty)

    # Round theo giá coin (stepSize)
    if entry >= 10000:    qty = round(qty, 3)   # BTC
    elif entry >= 1000:   qty = round(qty, 3)   # ETH
    elif entry >= 100:    qty = round(qty, 1)   # SOL, BNB
    elif entry >= 10:     qty = round(qty, 1)   # mid-cap
    elif entry >= 1:      qty = int(qty)        # LAB, XRP — integer only
    elif entry >= 0.01:   qty = int(qty)        # cheap coins
    else:                 qty = int(qty)
    return max(qty, 0.1 if entry >= 100 else 1)

def trade_engine(exchange, notifier):
    # Startup noti — retry nếu bị rate limit
    for attempt in range(5):
        try:
            bal = exchange.get_account_balance()
            with lock: state["balance"] = bal
            notifier.telegram.send(
                f"🚀 <b>MULTI-COIN BOT STARTED</b>\n"
                f"💼 Balance: <b>${bal:,.2f} USDT</b>\n"
                f"⚡ Leverage: <b>{config.LEVERAGE}x</b>\n"
                f"📊 Scanning <b>{len(WATCHLIST)} coins</b> mỗi {config.LOOP_INTERVAL_SECONDS}s\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
            )
            return
        except Exception as e:
            if "418" in str(e):
                wait = 30 * (attempt + 1)
                logger.warning(f"Rate limited (418), waiting {wait}s... (attempt {attempt+1}/5)")
                time.sleep(wait)
            else:
                logger.error(f"trade_engine startup error: {e}")
                return

# ============================================================
# THREAD 2a: Monitor SL/TP cho position đang mở (mỗi 5 giây)
# ============================================================
def monitor_engine(exchange, notifier):
    while state["running"]:
        try:
            with lock:
                pos   = state["position"]
                sym   = state["symbol"]
                entry = state["entry"]
                sl    = state["sl"]
                tp    = state["tp"]
                qty   = state["qty"]

            if not pos:
                time.sleep(5)
                continue

            cp = exchange.get_mark_price(sym)
            with lock: state["prices"][sym] = cp

            hit = None
            if pos == "LONG":
                if cp <= sl: hit = "❌ SL HIT"
                elif cp >= tp: hit = "✅ TP HIT"
            else:
                if cp >= sl: hit = "❌ SL HIT"
                elif cp <= tp: hit = "✅ TP HIT"

            # Hard stop: lỗ quá MAX_LOSS_PCT thì đóng ngay
            if not hit:
                max_loss_pct = getattr(config, "MAX_LOSS_PCT", 0.30)
                loss_pct = (entry - cp) / entry if pos == "LONG" else (cp - entry) / entry
                if loss_pct >= max_loss_pct:
                    hit = f"🚨 MAX LOSS {loss_pct*100:.1f}% HIT"

            # Trailing stop
            if not hit and config.TRAILING_STOP:
                with lock:
                    trail = state.get("trail_ext", entry)
                if pos == "LONG" and cp > trail:
                    new_sl = cp * (1 - config.TRAILING_STOP_PCT)
                    with lock:
                        if new_sl > state["sl"]:
                            state["sl"] = new_sl
                            state["trail_ext"] = cp
                            logger.info(f"Trailing SL → ${new_sl:.4f}")
                elif pos == "SHORT" and cp < trail:
                    new_sl = cp * (1 + config.TRAILING_STOP_PCT)
                    with lock:
                        if new_sl < state["sl"]:
                            state["sl"] = new_sl
                            state["trail_ext"] = cp
                            logger.info(f"Trailing SL → ${new_sl:.4f}")

            if hit:
                close_side = "SELL" if pos == "LONG" else "BUY"
                try: exchange.place_market_order(sym, close_side, qty)
                except Exception as e: logger.error(f"Close failed: {e}")

                pnl_pct = (cp-entry)/entry*100 if pos=="LONG" else (entry-cp)/entry*100
                pnl_usd = qty * abs(cp - entry) * (1 if pnl_pct > 0 else -1)

                notifier.telegram.send(
                    f"{hit} <b>{sym}</b>\n"
                    f"📌 {pos}  Entry: ${entry:.4f} → Close: ${cp:.4f}\n"
                    f"💵 PnL: <b>${pnl_usd:+.2f}</b> ({pnl_pct:+.2f}%)\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                )

                with lock:
                    for t in reversed(state["trade_log"]):
                        if t["symbol"]==sym and t["status"]=="OPEN":
                            t.update({"status":"CLOSED","close":cp,
                                      "pnl_pct":round(pnl_pct,2),
                                      "pnl_usdt":round(pnl_usd,2)})
                            break
                    state["position"] = None
                    state["symbol"]   = None
                    state["entry"]    = 0.0
                    state["sl"]       = 0.0
                    state["tp"]       = 0.0
                    state["qty"]      = 0.0
                    if pnl_usd < 0:
                        state["last_loss_time"] = time.time()
                from trade_history import save_history
                save_history(state["trade_log"])

        except Exception as e:
            logger.error(f"Monitor engine: {e}", exc_info=True)
        time.sleep(3)

# ============================================================
# THREAD 2b: Scan coin mới và vào lệnh (mỗi LOOP_INTERVAL giây)
# ============================================================
def scan_engine(exchange, notifier):
    while state["running"]:
        try:
            # Cooldown sau khi lỗ
            with lock:
                last_loss_time = state.get("last_loss_time", 0)
            cooldown = getattr(config, "COOLDOWN_AFTER_LOSS", 180)
            if time.time() - last_loss_time < cooldown:
                wait = int(cooldown - (time.time() - last_loss_time))
                logger.info(f"Cooldown sau lỗ: còn {wait}s")
                time.sleep(config.LOOP_INTERVAL_SECONDS)
                continue

            # Kiểm tra số lệnh đang mở
            with lock:
                n_open = len(state.get("open_positions", []))
            if n_open >= config.MAX_OPEN_POSITIONS:
                logger.info(f"Max positions ({n_open}/{config.MAX_OPEN_POSITIONS}), skip scan")
                time.sleep(config.LOOP_INTERVAL_SECONDS)
                continue

            with lock:
                state["scan_no"] += 1
                state["last_scan"] = datetime.now().strftime("%H:%M")

            best = scan_market(exchange, config, min_score=config.MIN_SCORE)
            with lock:
                state["candidates"] = list(getattr(scan_market, "_last_candidates", []))

            if best:
                # Không vào lệnh trùng symbol đã có position trên Binance
                with lock:
                    open_syms = {p["symbol"] for p in state.get("open_positions", [])
                                 if abs(float(p.get("positionAmt", 0))) > 0}
                if best.symbol in open_syms:
                    logger.info(f"Skip {best.symbol}: already has open position")
                    time.sleep(config.LOOP_INTERVAL_SECONDS)
                    continue

                # Không vào lệnh trùng symbol đã có pending order (LIMIT chờ khớp)
                try:
                    pending_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                    pending_syms = {o["symbol"] for o in pending_orders if not o.get("reduceOnly", False)}
                    if best.symbol in pending_syms:
                        logger.info(f"Skip {best.symbol}: already has pending order")
                        time.sleep(config.LOOP_INTERVAL_SECONDS)
                        continue
                except Exception:
                    pass

                klines = exchange.get_klines(best.symbol, config.INTERVAL, limit=200)
                df = _klines_to_df(klines)
                price = df["close"].iloc[-1]
                atr = calculate_atr(df["high"], df["low"], df["close"]).iloc[-1]
                bal = exchange.get_account_balance()

                try: exchange.set_leverage(best.symbol, config.LEVERAGE)
                except: pass

                # SL/TP theo ATR (giữ nguyên như cũ)
                if best.signal == "LONG":
                    sl = price - max(atr * 1.5, price * config.STOP_LOSS_PCT)
                    tp = price + (price - sl) * 3   # RR 1:3
                    side = "BUY"
                else:
                    sl = price + max(atr * 1.5, price * config.STOP_LOSS_PCT)
                    tp = price - (sl - price) * 3   # RR 1:3
                    side = "SELL"

                qty = calc_qty(bal, price, sl)
                min_notional = 5.0
                if qty * price < min_notional:
                    qty = round(min_notional / price + 0.001, 3)

                # Smart Entry: tìm điểm vào + SL/TP tốt nhất từ chart
                from smart_entry import find_optimal_entry, place_smart_order
                entry_info = find_optimal_entry(exchange, best.symbol, best.signal, config)
                # Dùng SL/TP từ chart phân tích (swing low/high, ATR 15m/5m)
                sl = entry_info["sl"]
                tp = entry_info["tp"]

                result = place_smart_order(exchange, best.symbol, best.signal, qty, entry_info, config,
                                           bot_state=state, bot_lock=lock)
                actual_price = result.get("price", price)

                with lock:
                    state["position"]  = best.signal
                    state["symbol"]    = best.symbol
                    state["entry"]     = actual_price
                    state["sl"]        = sl
                    state["tp"]        = tp
                    state["qty"]       = qty
                    state["trail_ext"] = actual_price
                    state["trade_log"].append({
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "symbol": best.symbol, "side": best.signal,
                        "entry": actual_price, "sl": sl, "tp": tp,
                        "qty": qty, "status": "OPEN",
                        "note": f"scan_{result.get('type','MARKET').lower()}"
                    })

                icon = "🟢" if best.signal=="LONG" else "🔴"
                margin = qty * price / config.LEVERAGE
                order_type = "LIMIT (chờ khớp)" if result.get("type") == "LIMIT" else "MARKET"
                notifier.telegram.send(
                    f"{icon} <b>{best.signal} {best.symbol}</b> [{order_type}]\n"
                    f"💰 Entry  : <b>${actual_price:.4f}</b>\n"
                    f"🛑 SL     : <b>${sl:.4f}</b>  ({abs(price-sl)/price*100:.2f}%)\n"
                    f"🎯 TP     : <b>${tp:.4f}</b>  ({abs(tp-price)/price*100:.2f}%)\n"
                    f"📦 Size   : {qty} (~<b>${qty*price:,.2f}</b> notional)\n"
                    f"💵 Margin : <b>${margin:.2f} USDT</b> ({config.LEVERAGE}x)\n"
                    f"⭐ Score  : {best.score}đ | {best.reason}\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                )

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Scan engine: {e}", exc_info=True)
            notifier.telegram.send(f"⚠️ Bot error: {e}")
            time.sleep(60)

        time.sleep(config.LOOP_INTERVAL_SECONDS)

# ============================================================
# THREAD 4: Liquidation Strategy Engine
# Mỗi 30s: phân tích liq data → vào 2 lệnh split nếu có setup
# Mỗi 5s : monitor các lệnh split đang chờ khớp + theo dõi SL/TP
# ============================================================
def liq_engine(exchange, notifier, liq_tracker: LiquidationTracker):
    """
    2 nhiệm vụ:
    A. Scan setup (30s): dùng LiqStrategy.analyze() tìm setup mới
    B. Monitor (5s)    : theo dõi lệnh split đang chờ, đặt SL/TP khi lệnh 1 khớp
    """
    # Kiểm tra có bật strategy này không
    if not getattr(config, "LIQ_STRATEGY_ENABLED", True):
        logger.info("[LiqEngine] LIQ_STRATEGY_ENABLED=False, thread idle")
        while state["running"]:
            time.sleep(30)
        return

    from liq_strategy import LiqStrategy
    strategy       = LiqStrategy(liq_tracker, config)
    min_confidence = getattr(config, "LIQ_MIN_CONFIDENCE", 40)
    timeout_hours  = getattr(config, "LIQ_SETUP_TIMEOUT_HOURS", 6)

    last_scan_time = 0
    SCAN_INTERVAL  = 30   # giây

    while state["running"]:
        now = time.time()

        # ── A. Scan setup mới ────────────────────────────────
        if now - last_scan_time >= SCAN_INTERVAL:
            last_scan_time = now

            # Cập nhật liq_data cho dashboard
            liq_data = {}
            for sym in WATCHLIST:
                total = liq_tracker.total_liq_usd(sym)
                if total > 0:
                    liq_data[sym] = total
            with lock:
                state["liq_data"]       = liq_data
                state["liq_connected"]  = liq_tracker.is_connected()

            # Kiểm tra số lệnh đang mở
            with lock:
                n_open   = len(state.get("open_positions", []))
                n_splits = len(state.get("split_positions", {}))
            max_pos = getattr(config, "MAX_OPEN_POSITIONS", 3)

            if n_open + n_splits >= max_pos:
                logger.info(f"[LiqEngine] Max positions ({n_open}+{n_splits}/{max_pos}), skip scan")
                time.sleep(5)
                continue

            # Scan từng coin trong WATCHLIST
            for sym in WATCHLIST:
                with lock:
                    # Bỏ qua nếu đã có split position cho coin này
                    if sym in state.get("split_positions", {}):
                        continue
                    # Bỏ qua nếu đang có position thường cho coin này
                    if state["symbol"] == sym:
                        continue

                try:
                    price = exchange.get_ticker_price(sym)
                except Exception:
                    continue

                setup = strategy.analyze(sym, price)
                if setup is None:
                    continue

                # Bỏ qua nếu confidence không đủ
                if setup.confidence < min_confidence:
                    logger.info(f"[LiqEngine] {sym} confidence={setup.confidence:.0f} < {min_confidence}, skip")
                    continue

                # Tính qty
                with lock:
                    bal = state["balance"]
                qty1, qty2 = strategy.calc_quantities(setup, bal, config.LEVERAGE)

                # Tạo split position object
                sp = SplitPosition(
                    symbol    = sym,
                    direction = setup.direction,
                    entry1    = setup.entry1,
                    entry2    = setup.entry2,
                    sl        = setup.sl,
                    tp        = setup.tp,
                    qty1      = qty1,
                    qty2      = qty2,
                )
                with lock:
                    state["split_positions"][sym] = sp

                icon = "🟢" if setup.direction == "LONG" else "🔴"
                notifier.telegram.send(
                    f"⚡ <b>LIQ SETUP: {setup.direction} {sym}</b>\n"
                    f"{icon} Entry1 (35%): <b>${setup.entry1:.4f}</b>  qty={qty1}\n"
                    f"{icon} Entry2 (65%): <b>${setup.entry2:.4f}</b>  qty={qty2}\n"
                    f"🛑 SL     : <b>${setup.sl:.4f}</b>\n"
                    f"🎯 TP     : <b>${setup.tp:.4f}</b>\n"
                    f"💧 Liq1   : ${setup.liq1_usd/1e6:.2f}M  |  Liq2: ${setup.liq2_usd/1e6:.2f}M\n"
                    f"⭐ Conf   : {setup.confidence:.0f}  |  {setup.reason}\n"
                    f"⏰ {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"
                )
                logger.info(f"[LiqEngine] Setup: {setup}")

        # ── B. Monitor split positions ───────────────────────
        with lock:
            splits_copy = dict(state.get("split_positions", {}))

        for sym, sp in splits_copy.items():
            try:
                price = exchange.get_ticker_price(sym)
            except Exception:
                continue

            side_market = "BUY"  if sp.direction == "LONG"  else "SELL"
            side_close  = "SELL" if sp.direction == "LONG"  else "BUY"

            # ── Lệnh 1 chưa khớp → kiểm tra giá đã chạm entry1 chưa ──
            if not sp.filled1:
                hit1 = (
                    (sp.direction == "LONG"  and price <= sp.entry1) or
                    (sp.direction == "SHORT" and price >= sp.entry1)
                )
                if hit1:
                    try:
                        exchange.set_leverage(sym, config.LEVERAGE)
                        exchange.place_market_order(sym, side_market, sp.qty1)
                        with lock:
                            state["split_positions"][sym].filled1 = True
                            state["trade_log"].append({
                                "time"  : __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "symbol": sym, "side": sp.direction,
                                "entry" : price, "sl": sp.sl, "tp": sp.tp,
                                "qty"   : sp.qty1, "status": "OPEN",
                                "note"  : "liq_order1"
                            })
                        icon = "🟢" if sp.direction == "LONG" else "🔴"
                        notifier.telegram.send(
                            f"{icon} <b>LIQ ORDER 1 FILLED: {sp.direction} {sym}</b>\n"
                            f"💰 Price  : <b>${price:.4f}</b>  qty={sp.qty1}\n"
                            f"⏳ Chờ Order 2 @ ${sp.entry2:.4f}\n"
                            f"⏰ {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"
                        )
                        logger.info(f"[LiqEngine] Order1 filled {sym} @ {price}")
                    except Exception as e:
                        logger.error(f"[LiqEngine] Order1 place failed {sym}: {e}")

            # ── Lệnh 2 chưa khớp → kiểm tra giá chạm entry2 ──
            elif sp.filled1 and not sp.filled2:
                hit2 = (
                    (sp.direction == "LONG"  and price <= sp.entry2) or
                    (sp.direction == "SHORT" and price >= sp.entry2)
                )
                if hit2:
                    try:
                        exchange.place_market_order(sym, side_market, sp.qty2)
                        with lock:
                            state["split_positions"][sym].filled2 = True
                            state["trade_log"].append({
                                "time"  : __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "symbol": sym, "side": sp.direction,
                                "entry" : price, "sl": sp.sl, "tp": sp.tp,
                                "qty"   : sp.qty2, "status": "OPEN",
                                "note"  : "liq_order2"
                            })
                        # Đặt SL + TP sau khi lệnh 2 khớp
                        total_qty = sp.qty1 + sp.qty2
                        try:
                            exchange.cancel_all_orders(sym)
                            exchange.place_stop_loss_order(sym, side_close, total_qty, sp.sl)
                            exchange.place_take_profit_order(sym, side_close, total_qty, sp.tp)
                            with lock:
                                state["split_positions"][sym].sl_placed = True
                                state["split_positions"][sym].tp_placed = True
                        except Exception as e:
                            logger.error(f"[LiqEngine] SL/TP place failed {sym}: {e}")

                        icon = "🟢" if sp.direction == "LONG" else "🔴"
                        notifier.telegram.send(
                            f"{icon} <b>LIQ ORDER 2 FILLED: {sp.direction} {sym}</b>\n"
                            f"💰 Price  : <b>${price:.4f}</b>  qty={sp.qty2}\n"
                            f"📦 Total  : {total_qty} (order1+order2)\n"
                            f"🛑 SL set : <b>${sp.sl:.4f}</b>\n"
                            f"🎯 TP set : <b>${sp.tp:.4f}</b>\n"
                            f"⏰ {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"
                        )
                        logger.info(f"[LiqEngine] Order2 filled + SL/TP set {sym}")
                    except Exception as e:
                        logger.error(f"[LiqEngine] Order2 place failed {sym}: {e}")

                # Nếu giá đã đi ngược quá xa mà lệnh 2 chưa khớp → huỷ setup
                elif sp.direction == "SHORT" and price < sp.entry1 * 0.985:
                    logger.info(f"[LiqEngine] {sym} price reversed, cancel split setup")
                    _cancel_split(sym, sp, exchange, notifier, side_close, "Giá đảo chiều trước khi Order2 khớp")
                elif sp.direction == "LONG" and price > sp.entry1 * 1.015:
                    logger.info(f"[LiqEngine] {sym} price reversed, cancel split setup")
                    _cancel_split(sym, sp, exchange, notifier, side_close, "Giá đảo chiều trước khi Order2 khớp")

            # ── Cả 2 lệnh đã khớp → monitor SL/TP hit ────────
            elif sp.filled1 and sp.filled2:
                # SL check
                sl_hit = (
                    (sp.direction == "LONG"  and price <= sp.sl) or
                    (sp.direction == "SHORT" and price >= sp.sl)
                )
                tp_hit = (
                    (sp.direction == "LONG"  and price >= sp.tp) or
                    (sp.direction == "SHORT" and price <= sp.tp)
                )
                if sl_hit or tp_hit:
                    tag = "❌ SL HIT" if sl_hit else "✅ TP HIT"
                    total_qty = sp.qty1 + sp.qty2
                    avg_entry = (sp.entry1 * sp.qty1 + sp.entry2 * sp.qty2) / total_qty
                    pnl_pct = (
                        (price - avg_entry) / avg_entry * 100
                        if sp.direction == "LONG"
                        else (avg_entry - price) / avg_entry * 100
                    )
                    pnl_usd = total_qty * abs(price - avg_entry) * (1 if pnl_pct > 0 else -1)

                    try:
                        exchange.cancel_all_orders(sym)
                        exchange.place_market_order(sym, side_close, total_qty)
                    except Exception as e:
                        logger.error(f"[LiqEngine] Close failed {sym}: {e}")

                    notifier.telegram.send(
                        f"{tag} <b>{sp.direction} {sym}</b>\n"
                        f"📌 AvgEntry: ${avg_entry:.4f} → Close: ${price:.4f}\n"
                        f"💵 PnL: <b>${pnl_usd:+.2f}</b> ({pnl_pct:+.2f}%)\n"
                        f"⏰ {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"
                    )
                    # Update trade log + xoá split position
                    with lock:
                        for t in reversed(state["trade_log"]):
                            if t["symbol"] == sym and t["status"] == "OPEN":
                                t.update({"status": "CLOSED", "close": price,
                                          "pnl_pct": round(pnl_pct, 2),
                                          "pnl_usdt": round(pnl_usd, 2)})
                        state["split_positions"].pop(sym, None)
                    from trade_history import save_history
                    save_history(state["trade_log"])
                    logger.info(f"[LiqEngine] {tag} {sym} pnl={pnl_usd:+.2f}")

            # ── Setup quá cũ → huỷ ──
            if not sp.filled1 and time.time() - sp.open_time > timeout_hours * 3600:
                logger.info(f"[LiqEngine] {sym} setup expired ({timeout_hours}h), cancel")
                with lock:
                    state["split_positions"].pop(sym, None)

        time.sleep(5)


def _cancel_split(sym, sp, exchange, notifier, side_close, reason):
    """Huỷ split setup: đóng lệnh 1 nếu đã khớp, xoá khỏi state."""
    if sp.filled1 and not sp.filled2:
        try:
            exchange.place_market_order(sym, side_close, sp.qty1)
        except Exception as e:
            logger.error(f"[LiqEngine] Cancel split close failed {sym}: {e}")
    with lock:
        state["split_positions"].pop(sym, None)
    notifier.telegram.send(
        f"⚠️ <b>LIQ SETUP CANCELLED: {sym}</b>\n"
        f"Lý do: {reason}"
    )


# ============================================================
# THREAD 6: Limit Order Monitor + Auto SL/TP cho positions mới
# Theo dõi pending limit orders, khi fill → đặt SL/TP
# CŨNG: mỗi 30s check positions chưa có SL/TP → tự đặt
# ============================================================
def limit_order_monitor(exchange, notifier):
    """
    2 nhiệm vụ:
    A. Mỗi 5s check pending limit orders trong state → khi fill đặt SL/TP
    B. Mỗi 30s check positions chưa có SL/TP → tự đặt (backup cho case restart)
    """
    import time as _time
    last_auto_check = 0

    while state["running"]:
        try:
            # ── A. Check pending orders (mỗi 5s) ──
            with lock:
                pending = dict(state.get("pending_smart_orders", {}))

            if pending:
                for order_id, info in list(pending.items()):
                    try:
                        result = exchange._get("/fapi/v1/order", {
                            "symbol": info["symbol"],
                            "orderId": int(order_id),
                        }, signed=True)

                        status = result.get("status", "")

                        if status == "FILLED":
                            sym = info["symbol"]
                            side = info["side"]
                            qty = info["qty"]
                            sl = info["sl"]
                            tp = info["tp"]
                            close_side = "SELL" if side == "LONG" else "BUY"

                            logger.info(f"[LimitMonitor] {sym} LIMIT filled! Placing SL/TP...")
                            time.sleep(1)
                            try:
                                exchange.place_stop_loss_order(sym, close_side, qty, sl)
                                logger.info(f"[LimitMonitor] SL placed: {sym} @ {sl}")
                            except Exception as e:
                                logger.error(f"[LimitMonitor] SL failed {sym}: {e}")
                            try:
                                exchange.place_take_profit_order(sym, close_side, qty, tp)
                                logger.info(f"[LimitMonitor] TP placed: {sym} @ {tp}")
                            except Exception as e:
                                logger.error(f"[LimitMonitor] TP failed {sym}: {e}")

                            fill_price = float(result.get("avgPrice", 0) or result.get("price", 0) or 0)
                            def _pd(p):
                                if p >= 10000: return 1
                                if p >= 1000: return 2
                                if p >= 10: return 2
                                if p >= 1: return 4
                                return 5
                            notifier.telegram.send(
                                f"🔔 <b>LIMIT ORDER FILLED!</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📊 {sym} <b>{side}</b>\n"
                                f"💵 Fill Price: <b>${fill_price:,.{_pd(fill_price) if fill_price > 0 else 2}f}</b>\n"
                                f"📦 Qty: {qty}\n"
                                f"🛑 SL set: <b>${sl:,.{_pd(sl)}f}</b>\n"
                                f"🎯 TP set: <b>${tp:,.{_pd(tp)}f}</b>\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            )
                            with lock:
                                state.get("pending_smart_orders", {}).pop(str(order_id), None)

                        elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                            logger.info(f"[LimitMonitor] {info['symbol']} order {status}, removing")
                            with lock:
                                state.get("pending_smart_orders", {}).pop(str(order_id), None)

                    except Exception as e:
                        logger.debug(f"[LimitMonitor] Check order {order_id}: {e}")

            # ── B. Auto SL/TP cho positions mới (mỗi 30s) ──
            if _time.time() - last_auto_check > 30:
                last_auto_check = _time.time()
                try:
                    from auto_sltp import get_positions_without_sltp, auto_set_sltp
                    liq_tracker = state.get("liq_tracker")
                    unprotected = get_positions_without_sltp(exchange)
                    for pos in unprotected:
                        logger.info(f"[AutoSLTP] Detected unprotected: {pos['symbol']} {pos['side']}")
                        auto_set_sltp(exchange, pos["symbol"], pos["side"],
                                     pos["entry"], pos["qty"], liq_tracker)
                except Exception as e:
                    logger.debug(f"[AutoSLTP] Check error: {e}")

        except Exception as e:
            logger.error(f"[LimitMonitor] Error: {e}")

        time.sleep(5)


# ============================================================
# THREAD 10: Position Advisory — mỗi 30 phút phân tích vị thế đang mở
# Gửi lời khuyên qua Telegram: giữ/đóng dựa trên xu hướng hiện tại
# ============================================================
def position_advisor(exchange, notifier):
    """
    Mỗi 30 phút:
    1. Lấy tất cả positions đang mở
    2. Phân tích xu hướng hiện tại (RSI, EMA, MTF)
    3. Gửi Telegram: coin nào nên giữ, coin nào nên đóng
    """
    from indicators import calculate_rsi, calculate_ema, calculate_atr
    from scanner import _klines_to_df

    # Đợi 5 phút sau khi bot start
    time.sleep(300)

    while state["running"]:
        try:
            with lock:
                open_pos = [p for p in state.get("open_positions", [])
                           if abs(float(p.get("positionAmt", 0))) > 0]

            if not open_pos:
                time.sleep(1800)  # 30 phút
                continue

            advice_lines = ["📊 <b>PHÂN TÍCH VỊ THẾ (30 phút)</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"]

            for p in open_pos:
                sym = p["symbol"]
                amt = float(p.get("positionAmt", 0))
                entry = float(p.get("entryPrice", 0))
                side = "LONG" if amt > 0 else "SHORT"
                pnl = float(p.get("unRealizedProfit", 0))

                try:
                    # Lấy data
                    klines_15m = exchange.get_klines(sym, "15m", limit=50)
                    klines_1h = exchange.get_klines(sym, "1h", limit=50)
                    df_15m = _klines_to_df(klines_15m)
                    df_1h = _klines_to_df(klines_1h)

                    close_15m = df_15m["close"]
                    close_1h = df_1h["close"]
                    price = close_15m.iloc[-1]

                    # Indicators
                    rsi_15m = calculate_rsi(close_15m, 14).iloc[-1]
                    rsi_1h = calculate_rsi(close_1h, 14).iloc[-1]
                    ema9 = calculate_ema(close_15m, 9).iloc[-1]
                    ema21 = calculate_ema(close_15m, 21).iloc[-1]
                    ema50 = calculate_ema(close_1h, 50).iloc[-1]

                    # Phân tích xu hướng
                    bullish_signals = 0
                    bearish_signals = 0
                    reasons = []

                    # EMA trend
                    if ema9 > ema21:
                        bullish_signals += 1
                        reasons.append("EMA9>21 ↑")
                    else:
                        bearish_signals += 1
                        reasons.append("EMA9<21 ↓")

                    # Price vs EMA50
                    if price > ema50:
                        bullish_signals += 1
                        reasons.append("Trên EMA50")
                    else:
                        bearish_signals += 1
                        reasons.append("Dưới EMA50")

                    # RSI
                    if rsi_15m > 60:
                        bullish_signals += 1
                        reasons.append(f"RSI={rsi_15m:.0f}↑")
                    elif rsi_15m < 40:
                        bearish_signals += 1
                        reasons.append(f"RSI={rsi_15m:.0f}↓")
                    else:
                        reasons.append(f"RSI={rsi_15m:.0f}")

                    # RSI 1h
                    if rsi_1h > 65:
                        bullish_signals += 1
                    elif rsi_1h < 35:
                        bearish_signals += 1

                    # Quyết định
                    pnl_pct = (price - entry) / entry * 100 if side == "LONG" else (entry - price) / entry * 100
                    icon = "🟢" if side == "LONG" else "🔴"
                    pnl_icon = "📈" if pnl >= 0 else "📉"

                    if side == "LONG":
                        if bearish_signals >= 3:
                            verdict = "⚠️ NÊN ĐÓNG — xu hướng đảo chiều"
                        elif bearish_signals >= 2 and pnl > 0:
                            verdict = "💡 Chốt lời — tín hiệu yếu đi"
                        elif bullish_signals >= 3:
                            verdict = "✅ GIỮ — xu hướng tốt"
                        else:
                            verdict = "🔄 THEO DÕI — chưa rõ xu hướng"
                    else:  # SHORT
                        if bullish_signals >= 3:
                            verdict = "⚠️ NÊN ĐÓNG — xu hướng đảo chiều"
                        elif bullish_signals >= 2 and pnl > 0:
                            verdict = "💡 Chốt lời — tín hiệu yếu đi"
                        elif bearish_signals >= 3:
                            verdict = "✅ GIỮ — xu hướng tốt"
                        else:
                            verdict = "🔄 THEO DÕI — chưa rõ xu hướng"

                    name = sym.replace("USDT", "")
                    advice_lines.append(
                        f"{icon} <b>{name} {side}</b> | {pnl_icon} ${pnl:+.2f} ({pnl_pct:+.1f}%)\n"
                        f"   {' | '.join(reasons)}\n"
                        f"   👉 <b>{verdict}</b>\n"
                    )

                except Exception as e:
                    name = sym.replace("USDT", "")
                    advice_lines.append(f"❓ {name}: không phân tích được\n")

            advice_lines.append(f"\n⏰ {datetime.now().strftime('%H:%M:%S')}")
            notifier.telegram.send("\n".join(advice_lines))

        except Exception as e:
            logger.error(f"[PositionAdvisor] Error: {e}")

        time.sleep(1800)  # 30 phút


# ============================================================
# THREAD 11: Orphan Order Cleanup — mỗi 20 phút xóa SL/TP mồ côi
# ============================================================
def orphan_order_cleanup(exchange, notifier):
    """Nếu coin có SL/TP order nhưng KHÔNG có position → hủy"""
    time.sleep(600)

    while state["running"]:
        try:
            all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
            open_syms = {p["symbol"] for p in all_pos
                        if abs(float(p.get("positionAmt", 0))) > 0}
            cancelled = []

            # Algo orders
            try:
                algo_orders = exchange._get("/fapi/v1/openAlgoOrders", signed=True)
                if isinstance(algo_orders, list):
                    for o in algo_orders:
                        sym = o.get("symbol", "")
                        if sym and sym not in open_syms:
                            try:
                                exchange._delete("/fapi/v1/algoOrder", {"algoId": o.get("algoId", "")})
                                cancelled.append(f"{sym} (algo)")
                            except Exception:
                                pass
            except Exception:
                pass

            # Regular reduceOnly orders
            try:
                all_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                for o in all_orders:
                    sym = o.get("symbol", "")
                    if sym and sym not in open_syms and o.get("reduceOnly", False):
                        try:
                            exchange._delete("/fapi/v1/order", {"symbol": sym, "orderId": o.get("orderId")})
                            cancelled.append(f"{sym} ({o.get('type', '')})")
                        except Exception:
                            pass
            except Exception:
                pass

            if cancelled:
                notifier.telegram.send(
                    f"🧹 <b>DỌN LỆNH MỒ CÔI</b>\n"
                    f"Đã hủy {len(cancelled)} lệnh TP/SL không còn position:\n" +
                    "\n".join(f"• {c}" for c in cancelled) +
                    f"\n⏰ {datetime.now().strftime('%H:%M:%S')}"
                )
                logger.info(f"[OrphanCleanup] Cancelled {len(cancelled)} orphan orders")

        except Exception as e:
            logger.error(f"[OrphanCleanup] Error: {e}")

        time.sleep(1200)  # 20 phút


def memory_cleanup():
    """Mỗi 2 giờ: garbage collect + giới hạn trade_log + clear caches"""
    import gc
    while state["running"]:
        time.sleep(7200)  # 2 giờ
        try:
            gc.collect()
            with lock:
                tlog = state.get("trade_log", [])
                if len(tlog) > 100:
                    state["trade_log"] = tlog[-100:]
                if len(state.get("candidates", [])) > 20:
                    state["candidates"] = state["candidates"][:20]
            if hasattr(scan_market, '_last_candidates'):
                scan_market._last_candidates = scan_market._last_candidates[:10]
            logger.info("[Cleanup] Memory freed, gc collected")
        except Exception:
            pass


# ============================================================
# THREAD 6b: Pending Order Review — mỗi 15 phút kiểm tra lệnh pending
# Nếu xu hướng đã đổi → hủy lệnh không còn hợp lý
# ============================================================
def pending_order_reviewer(exchange, notifier):
    """
    Mỗi 15 phút:
    1. Lấy tất cả pending limit orders
    2. Kiểm tra xu hướng hiện tại (EMA, RSI)
    3. Nếu lệnh LONG nhưng xu hướng BEARISH → hủy
    4. Nếu lệnh SHORT nhưng xu hướng BULLISH → hủy
    5. Nếu giá đã đi xa quá (>3%) khỏi entry → hủy
    """
    from indicators import calculate_rsi, calculate_ema
    from scanner import _klines_to_df

    # Đợi 5 phút sau khi bot start mới bắt đầu review
    time.sleep(300)

    while state["running"]:
        try:
            # Lấy pending orders
            all_orders = exchange._get("/fapi/v1/openOrders", signed=True)
            limit_orders = [o for o in all_orders if o.get("type") == "LIMIT"
                           and not o.get("reduceOnly", False)]

            if not limit_orders:
                time.sleep(900)  # 15 phút
                continue

            cancelled = []
            for order in limit_orders:
                sym = order.get("symbol", "")
                side = order.get("side", "")  # BUY = LONG, SELL = SHORT
                order_price = float(order.get("price", 0))
                order_id = order.get("orderId", "")

                try:
                    # Lấy giá hiện tại
                    current_price = exchange.get_ticker_price(sym)

                    # Check 1: giá đã đi xa quá 3% khỏi entry → hủy
                    dist_pct = abs(current_price - order_price) / order_price * 100
                    if dist_pct > 3:
                        exchange.cancel_all_orders(sym)
                        cancelled.append(f"{sym} (giá xa {dist_pct:.1f}%)")
                        logger.info(f"[PendingReview] Cancelled {sym}: price moved {dist_pct:.1f}% from order")
                        continue

                    # Lấy klines 15m để check xu hướng
                    klines = exchange.get_klines(sym, "15m", limit=50)
                    df = _klines_to_df(klines)
                    close = df["close"]

                    rsi = calculate_rsi(close, 14).iloc[-1]
                    ema9 = calculate_ema(close, 9).iloc[-1]
                    ema21 = calculate_ema(close, 21).iloc[-1]

                    # Check 2: xu hướng ngược với lệnh → hủy
                    if side == "BUY":  # LONG order
                        # Hủy nếu: RSI > 70 (overbought) HOẶC EMA9 < EMA21 (bearish)
                        if rsi > 70 or (ema9 < ema21 and current_price < ema21):
                            exchange.cancel_all_orders(sym)
                            cancelled.append(f"{sym} LONG (xu hướng bearish, RSI={rsi:.0f})")
                            logger.info(f"[PendingReview] Cancelled LONG {sym}: bearish (RSI={rsi:.0f}, EMA9<EMA21)")
                            continue
                    else:  # SELL = SHORT order
                        # Hủy nếu: RSI < 30 (oversold) HOẶC EMA9 > EMA21 (bullish)
                        if rsi < 30 or (ema9 > ema21 and current_price > ema21):
                            exchange.cancel_all_orders(sym)
                            cancelled.append(f"{sym} SHORT (xu hướng bullish, RSI={rsi:.0f})")
                            logger.info(f"[PendingReview] Cancelled SHORT {sym}: bullish (RSI={rsi:.0f}, EMA9>EMA21)")
                            continue

                except Exception as e:
                    logger.debug(f"[PendingReview] Skip {sym}: {e}")

            # Notify nếu có lệnh bị hủy
            if cancelled:
                notifier.telegram.send(
                    f"🔄 <b>PENDING ORDER REVIEW</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"❌ Đã hủy {len(cancelled)} lệnh không còn hợp lý:\n" +
                    "\n".join(f"• {c}" for c in cancelled) +
                    f"\n⏰ {datetime.now().strftime('%H:%M:%S')}"
                )

        except Exception as e:
            logger.error(f"[PendingReview] Error: {e}")

        time.sleep(900)  # 15 phút


# ============================================================
# THREAD 3: Grid Bot engine
# ============================================================
def grid_engine(exchange, notifier):
    """Chạy tất cả grid bots, check filled orders mỗi 10 giây"""
    while state["running"]:
        try:
            with lock:
                grids = dict(state.get("grids", {}))
            for sym, grid in grids.items():
                grid.check_filled()
        except Exception as e:
            logger.error(f"Grid engine: {e}")
        time.sleep(10)

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    exchange = BinanceFutures(config.API_KEY, config.API_SECRET, config.USE_TESTNET)
    notifier = Notifier()

    # Lưu exchange/notifier vào state để Telegram commands dùng
    state["_exchange"] = exchange
    state["_notifier"] = notifier
    state["grids"]     = {}

    # Khởi động Liquidation Tracker
    from scanner import WATCHLIST as _wl
    liq_tracker = LiquidationTracker(
        symbols  = list(_wl),
        testnet  = config.USE_TESTNET,
        bucket_pct = getattr(config, "LIQ_BUCKET_PCT", 0.001),
    )
    liq_tracker.start()
    state["liq_tracker"] = liq_tracker

    # Load lịch sử từ file (nếu có)
    from trade_history import load_history, save_history
    saved_history = load_history()
    with lock:
        state["trade_log"] = saved_history

    # [DISABLED] Sync trade history từ Binance API — đã tắt để clear lịch sử cũ
    # Nếu muốn bật lại, uncomment block bên dưới
    """
    try:
        from datetime import timedelta
        import time as _time
        end_time = int(_time.time() * 1000)
        start_time = end_time - 7 * 24 * 60 * 60 * 1000
        all_trades = exchange._get("/fapi/v1/userTrades", {
            "startTime": start_time,
            "endTime": end_time,
            "limit": 500
        }, signed=True)

        from collections import defaultdict
        order_groups = defaultdict(list)
        for t in all_trades:
            order_groups[t["orderId"]].append(t)

        synced = []
        for order_id, trades in order_groups.items():
            sym   = trades[0]["symbol"]
            side  = trades[0]["side"]
            qty   = sum(float(t["qty"]) for t in trades)
            price = sum(float(t["price"]) * float(t["qty"]) for t in trades) / qty
            pnl   = sum(float(t.get("realizedPnl", 0)) for t in trades)
            ts    = datetime.fromtimestamp(trades[0]["time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")

            if pnl != 0:
                synced.append({
                    "time":      ts,
                    "symbol":    sym,
                    "side":      "LONG" if side == "BUY" else "SHORT",
                    "entry":     price,
                    "close":     price,
                    "qty":       qty,
                    "pnl_usdt":  round(pnl, 2),
                    "pnl_pct":   round(pnl / (price * qty) * 100, 2),
                    "status":    "CLOSED",
                    "source":    "binance_sync"
                })

        if synced:
            existing_times = {t["time"] for t in saved_history}
            new_trades = [t for t in synced if t["time"] not in existing_times]
            with lock:
                state["trade_log"] = saved_history + new_trades
            save_history(state["trade_log"])
            logger.info(f"Synced {len(new_trades)} trades from Binance history")
            notifier.telegram.send(f"🔄 Đã sync {len(new_trades)} lệnh từ Binance history")

    except Exception as e:
        logger.warning(f"Binance history sync failed: {e}")
    """

    # Sync positions đang mở từ Binance khi khởi động
    try:
        all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
        open_pos = [p for p in all_pos if abs(float(p.get("positionAmt", 0))) > 0]
        if open_pos and len(open_pos) == 1:
            # Nếu chỉ có 1 position → restore vào state
            p = open_pos[0]
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            sym   = p["symbol"]
            side  = "LONG" if amt > 0 else "SHORT"
            # Tính lại SL/TP từ entry
            sl = entry * (1 - config.STOP_LOSS_PCT) if side == "LONG" else entry * (1 + config.STOP_LOSS_PCT)
            tp = entry * (1 + config.STOP_LOSS_PCT * 2) if side == "LONG" else entry * (1 - config.STOP_LOSS_PCT * 2)
            with lock:
                state["position"]   = side
                state["symbol"]     = sym
                state["entry"]      = entry
                state["sl"]         = sl
                state["tp"]         = tp
                state["qty"]        = abs(amt)
                state["trail_ext"]  = entry
            logger.info(f"Restored position: {side} {sym} entry={entry} SL={sl:.4f} TP={tp:.4f}")
            notifier.telegram.send(f"🔄 Restored {side} {sym} @ ${entry:.4f}\n🛑 SL: ${sl:.4f}\n🎯 TP: ${tp:.4f}")
    except Exception as e:
        logger.warning(f"Position restore failed: {e}")

    t0 = threading.Thread(target=dashboard_updater, daemon=True)
    t0.start()

    # Web Dashboard — mở http://localhost:5555
    try:
        from web_dashboard import start_web_dashboard
        WEB_PORT = getattr(config, "WEB_DASHBOARD_PORT", 5555)
        start_web_dashboard(state, lock, config, port=WEB_PORT, exchange=exchange)
        print(f"🌐 Web Dashboard: http://localhost:{WEB_PORT}")
    except Exception as e:
        logger.warning(f"Web dashboard disabled: {e}")

    t1 = threading.Thread(target=price_updater, args=(exchange,), daemon=True)
    t1.start()

    # WebSocket price stream (realtime)
    t1ws = threading.Thread(target=price_ws_streamer, daemon=True)
    t1ws.start()

    trade_engine(exchange, notifier)  # send startup notification

    t2a = threading.Thread(target=monitor_engine, args=(exchange, notifier), daemon=True)
    t2a.start()

    t2b = threading.Thread(target=scan_engine, args=(exchange, notifier), daemon=True)
    t2b.start()

    t3 = threading.Thread(target=grid_engine, args=(exchange, notifier), daemon=True)
    t3.start()

    # Liq strategy thread
    t5 = threading.Thread(target=liq_engine, args=(exchange, notifier, liq_tracker), daemon=True)
    t5.start()

    # Limit order monitor thread
    t7 = threading.Thread(target=limit_order_monitor, args=(exchange, notifier), daemon=True)
    t7.start()

    # Pending order reviewer thread (mỗi 15 phút check + hủy lệnh không hợp lý)
    t8 = threading.Thread(target=pending_order_reviewer, args=(exchange, notifier), daemon=True)
    t8.start()

    # Memory cleanup thread (mỗi 2 giờ)
    t9 = threading.Thread(target=memory_cleanup, daemon=True)
    t9.start()

    # Position advisor thread (mỗi 30 phút phân tích + gửi lời khuyên)
    t10 = threading.Thread(target=position_advisor, args=(exchange, notifier), daemon=True)
    t10.start()

    # Orphan order cleanup thread (mỗi 20 phút xóa SL/TP mồ côi)
    t11 = threading.Thread(target=orphan_order_cleanup, args=(exchange, notifier), daemon=True)
    t11.start()

    # AI Analyzer thread — chạy TradingAgents mỗi 4h
    def ai_analyzer_loop():
        import time as _t
        AI_INTERVAL = getattr(config, "AI_ANALYSIS_INTERVAL_HOURS", 4) * 3600
        # Chờ 60s sau khi bot start để ổn định
        _t.sleep(60)
        while state["running"]:
            try:
                from ai_analyzer import analyze_all
                from scanner import WATCHLIST as _wl
                logger.info("[AI Analyzer] Starting analysis...")
                with lock:
                    state["ai_analyzing"] = True
                results = analyze_all(list(_wl))
                with lock:
                    state["ai_analyzing"] = False
                    state["ai_last_run"] = datetime.now().strftime("%H:%M")
                # Notify
                summary = []
                for sym, info in results.items():
                    icon = "🟢" if info["bias"] == "LONG" else ("🔴" if info["bias"] == "SHORT" else "⚪")
                    summary.append(f"{icon} {sym.replace('USDT','')}: {info['bias']}")
                notifier.telegram.send(
                    f"🧠 <b>AI Analysis Complete</b>\n" + "\n".join(summary)
                )
                logger.info(f"[AI Analyzer] Done: {results}")
            except Exception as e:
                logger.error(f"[AI Analyzer] Error: {e}")
                with lock:
                    state["ai_analyzing"] = False
            _t.sleep(AI_INTERVAL)

    if getattr(config, "AI_AUTO_ANALYSIS", True):
        t6 = threading.Thread(target=ai_analyzer_loop, daemon=True)
        t6.start()

    try:
        from telegram_commands import TelegramCommandHandler
        from notifier import NOTIFICATION_CONFIG
        cmd = TelegramCommandHandler(
            bot_token=NOTIFICATION_CONFIG["telegram"]["bot_token"],
            chat_id=NOTIFICATION_CONFIG["telegram"]["chat_id"],
            state=state, state_lock=lock,
            watchlist=WATCHLIST, config=config
        )
        t4 = threading.Thread(target=cmd.run, daemon=True)
        t4.start()
    except Exception as e:
        logger.warning(f"Telegram commands disabled: {e}")

    # Grid auto-start TẮT — gây spam notification + lỗi 400 trên testnet
    # Muốn bật: dùng Telegram /grid hoặc uncomment bên dưới
    # GRID_COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]
    # for sym in GRID_COINS:
    #     try:
    #         price = exchange.get_ticker_price(sym)
    #         lower = round(price * 0.98, 4)
    #         upper = round(price * 1.02, 4)
    #         from grid_strategy import GridBot
    #         g = GridBot(sym, lower, upper, 10, 10, exchange, notifier)
    #         g.setup(price)
    #         state["grids"][sym] = g
    #     except Exception as e:
    #         logger.warning(f"Auto grid {sym} skipped: {e}")

    try:
        logger.info("=== Main loop started ===")
        while True: time.sleep(1)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
        state["_clean_exit"] = True
    except SystemExit as e:
        logger.error(f"SystemExit received: {e}")
    except Exception as e:
        logger.error(f"Main loop crashed: {e}", exc_info=True)
    finally:
        state["running"] = False
        try: clear()
        except: pass
        print("⛔ Bot dừng.")
        # Dừng liq tracker
        try: liq_tracker.stop()
        except: pass

        # Generate & send daily report
        try:
            from report_generator import generate_and_send
            from notifier import NOTIFICATION_CONFIG
            with lock:
                tlog = list(state["trade_log"])
                bal  = state["balance"]
                opos = list(state.get("open_positions", []))
                spos = dict(state.get("split_positions", {}))
            report_path = generate_and_send(
                trade_log       = tlog,
                balance         = bal,
                open_positions  = opos,
                split_positions = spos,
                bot_token       = NOTIFICATION_CONFIG["telegram"]["bot_token"],
                chat_id         = NOTIFICATION_CONFIG["telegram"]["chat_id"],
            )
            print(f"📊 Report saved: {report_path}")
        except Exception as e:
            print(f"⚠️ Report failed: {e}")
        # Dừng tất cả grids (silent)
        for sym, g in state.get("grids", {}).items():
            try: g.exchange.cancel_all_orders(sym)
            except: pass
        # KHÔNG đóng position khi dừng bot — lệnh vẫn giữ trên Binance
        print("💡 Lệnh đang mở vẫn giữ trên Binance (SL/TP đã đặt sẵn)")

        # Nếu tắt chủ động (Telegram /stop hoặc Ctrl+C) → exit 0 → run_bot.bat không restart
        if state.get("_clean_exit") or isinstance(locals().get("e_main"), KeyboardInterrupt):
            sys.exit(0)
        # Nếu crash → exit 1 → run_bot.bat tự restart
