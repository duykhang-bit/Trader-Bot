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
from scanner import scan_market, run_pump_scan, WATCHLIST, _klines_to_df, _pending_watch
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
    "open_positions": [],
    "running":        False,   # mặc định tắt — bật thủ công trên dashboard
    "_watchlist":     list(WATCHLIST),  # sync với scanner WATCHLIST
    # --- Liquidation strategy state ---
    "split_positions": {},
    "liq_data":       {},
    "pending_smart_orders": {},
}
lock = threading.Lock()

# Guard chống double entry — set các symbol đang trong quá trình xử lý order
_executing_symbols: set = set()

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
# THREAD 1a: Giá realtime qua WebSocket (cập nhật mỗi 100ms)
# + PUMP SPIKE DETECTOR gắn thẳng vào WS — phát hiện trong < 1s
# ============================================================

# ── Pump spike tracker — theo dõi % thay đổi giá theo thời gian ──
# {symbol: {"prices": [deque of (ts, price)], "alerted": bool, "alert_ts": float}}
_pump_spike_tracker: dict = {}
_SPIKE_WINDOW_SEC   = 5     # cửa sổ 5 giây — dev pump xảy ra trong vài giây
_SPIKE_MIN_PCT      = 3.0   # tăng >= 3% trong 5s → nghi ngờ pump
_SPIKE_CONFIRM_PCT  = 5.0   # tăng >= 5% trong 5s → xác nhận, SHORT ngay không cần indicators
_SPIKE_COOLDOWN_SEC = 120   # 2 phút không spam cùng coin

# ── Dump detector — phát hiện dump nhanh để SHORT sớm ──
# Dùng chung WS price history, KHÔNG cần REST call thêm
_DUMP_WINDOW_SEC    = 30    # cửa sổ 30s để tính % drop
_DUMP_MIN_PCT       = 3.0   # drop >= 3% trong 30s → nghi ngờ dump
_DUMP_CONFIRM_PCT   = 5.0   # drop >= 5% → SHORT ngay (không cần indicators)
_DUMP_COOLDOWN_SEC  = 120   # 2 phút cooldown
_dump_tracker: dict = {}    # {symbol: {"alert_ts": float, "ws_low": float}}


def _ws_pump_spike_check(sym: str, price: float, exchange_ref, notifier_ref):
    """
    Được gọi từ WS on_message mỗi khi nhận tick giá.
    Nếu phát hiện spike bất thường → lấy klines 1m (async) rồi chạy PumpDetector.
    Cực kỳ nhẹ: chỉ lưu price + tính % change, không block WS thread.
    """
    from collections import deque
    import time as _t

    now = _t.time()

    # Khởi tạo tracker cho coin mới
    if sym not in _pump_spike_tracker:
        _pump_spike_tracker[sym] = {
            "prices":    deque(maxlen=60),
            "alerted":   False,
            "alert_ts":  0.0,
            "ws_high":   0.0,   # đỉnh cao nhất đã thấy qua WS (realtime)
            "ws_high_ts": 0.0,  # timestamp khi đạt đỉnh
        }

    tracker = _pump_spike_tracker[sym]
    tracker["prices"].append((now, price))

    # Cập nhật đỉnh động realtime
    if price > tracker["ws_high"]:
        tracker["ws_high"]    = price
        tracker["ws_high_ts"] = now

    # Lấy giá cách đây SPIKE_WINDOW_SEC giây
    cutoff = now - _SPIKE_WINDOW_SEC
    old_ticks = [(ts, p) for ts, p in tracker["prices"] if ts <= cutoff]
    if not old_ticks:
        return   # Chưa đủ dữ liệu lịch sử

    oldest_price = old_ticks[-1][1]   # giá cũ nhất trong cửa sổ
    if oldest_price <= 0:
        return

    pct_change = (price - oldest_price) / oldest_price * 100

    # Không đủ spike
    if pct_change < _SPIKE_MIN_PCT:
        # Reset alert flag khi giá bình thường trở lại
        if pct_change < _SPIKE_MIN_PCT * 0.5:
            tracker["alerted"] = False
        return

    # Cooldown — không spam
    if now - tracker["alert_ts"] < _SPIKE_COOLDOWN_SEC:
        return

    # Đánh dấu đã alert để tránh duplicate trong cùng spike
    tracker["alerted"] = True
    tracker["alert_ts"] = now

    logger.info(
        f"[WS-PumpSpike] {sym}: +{pct_change:.1f}% trong {_SPIKE_WINDOW_SEC}s "
        f"@ ${price:.6g} — triggering full analysis..."
    )

    # Spawn thread riêng để lấy klines + chạy PumpDetector
    # KHÔNG block WS on_message
    import threading as _th
    _th.Thread(
        target=_ws_spike_full_analysis,
        args=(sym, price, pct_change, exchange_ref, notifier_ref),
        daemon=True
    ).start()


def _ws_spike_full_analysis(sym: str, trigger_price: float, spike_pct: float,
                             exchange_ref, notifier_ref):
    """
    Chạy trong thread riêng sau khi WS phát hiện spike.
    3 TẦNG XÁC NHẬN trước khi alert/short:
      Tầng 1 — WS Spike   : giá tăng >= 3% (đã pass)
      Tầng 2 — Order Book : ask wall áp đảo bid wall (dev đang xả)
      Tầng 3 — PumpDetect : volume kiệt sức + wick rejection + RSI div
    Cần >= 2/3 tầng pass → alert. Cần 3/3 → auto short.

    FAST PATH: spike >= _SPIKE_CONFIRM_PCT (5%) trong 5s → SHORT ngay
    không chờ klines/indicators — dev pump đỉnh tồn tại vài giây.
    """
    import time as _t
    from pump_detector import PumpDetector, _to_df
    from orderbook_detector import get_ob_tracker, confirm_pump_top

    try:
        # ── FAST PATH: spike >= 5% trong 5s → SHORT ngay, không chờ klines ──
        # Dev pump đỉnh chỉ tồn tại 3-10 giây
        # Chờ klines REST (300-500ms) + indicators là đã trễ
        auto_short = getattr(config, "PUMP_AUTO_SHORT", False)
        if auto_short and spike_pct >= _SPIKE_CONFIRM_PCT:
            with lock:
                open_syms = {p["symbol"] for p in state.get("open_positions", [])
                             if abs(float(p.get("positionAmt", 0))) > 0}
                n_open = len(state.get("open_positions", []))

            if sym not in open_syms and n_open < config.MAX_OPEN_POSITIONS:
                try:
                    cur_price = trigger_price  # dùng WS price, không gọi REST
                    ws_high   = _pump_spike_tracker.get(sym, {}).get("ws_high", cur_price)

                    exchange_ref.set_leverage(sym, config.LEVERAGE)
                    qty = (config.MAX_ORDER_USDT * config.LEVERAGE) / cur_price
                    try:
                        step, _, decimals, _ = exchange_ref.get_qty_precision(sym)
                        qty = max(round(int(qty / step) * step, decimals), step)
                    except Exception:
                        qty = round(qty, 3)

                    if qty * cur_price >= 5.0:
                        # SL chặt: 2% trên đỉnh WS (không phải trigger_price)
                        sl_price = round(ws_high * 1.035, 8)
                        # TP: -15% từ entry (pump thường xả nhanh 10-20%)
                        tp_price = round(cur_price * 0.85, 8)

                        exchange_ref.place_market_order(sym, "SELL", qty)
                        _t.sleep(0.3)
                        try: exchange_ref.place_stop_loss_order(sym, "BUY", qty, sl_price)
                        except Exception as e: logger.error(f"[FastShort] SL {sym}: {e}")
                        try: exchange_ref.place_take_profit_order(sym, "BUY", qty, tp_price)
                        except Exception as e: logger.error(f"[FastShort] TP {sym}: {e}")

                        rr = abs(cur_price - tp_price) / abs(sl_price - cur_price)
                        with lock:
                            state["trade_log"].append({
                                "time":   __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "symbol": sym, "side": "SHORT",
                                "entry":  cur_price, "sl": sl_price, "tp": tp_price,
                                "qty":    qty, "status": "OPEN",
                                "note":   f"fast_spike_{spike_pct:.1f}pct",
                            })
                            state.setdefault("pump_trade_symbols", set()).add(sym)

                        notifier_ref.telegram.send(
                            f"⚡ <b>FAST SHORT — DEV PUMP</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🪙 {sym}  📈 <b>+{spike_pct:.1f}%</b> trong 5s\n"
                            f"🔴 Entry : <b>${cur_price:,.6g}</b>  [MARKET]\n"
                            f"🛑 SL    : <b>${sl_price:,.6g}</b>  (+2% đỉnh)\n"
                            f"🎯 TP    : <b>${tp_price:,.6g}</b>  (-10%)\n"
                            f"📐 RR    : 1:{rr:.1f}   📦 Qty: {qty}\n"
                            f"⏰ {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"
                        )
                        logger.info(f"[FastShort] SHORT placed: {sym} +{spike_pct:.1f}% qty={qty}")
                        return  # Không cần chạy slow path nữa
                except Exception as e:
                    logger.error(f"[FastShort] {sym} failed: {e}")

        # ── SLOW PATH: spike 3-5% → chạy 3-tier analysis như cũ ──
        # Lấy order book tracker (đã chạy sẵn)
        ob = get_ob_tracker(
            "wss://fstream.binance.com" if not config.USE_TESTNET
            else "wss://stream.binancefuture.com"
        )
        # Đảm bảo coin đang được track order book
        ob.add_symbols([sym])

        # Lấy klines
        klines_1m  = exchange_ref.get_klines(sym, "1m",  limit=60)
        klines_15m = exchange_ref.get_klines(sym, "15m", limit=30)
        df_1m  = _to_df(klines_1m)
        df_15m = _to_df(klines_15m)

        detector = PumpDetector(config)
        detector.cfg["PUMP_PRICE_RISE_PCT"] = max(spike_pct * 0.7, 5.0)

        # Lấy đỉnh realtime từ WS tracker nếu có — chính xác hơn klines
        ws_high = _pump_spike_tracker.get(sym, {}).get("ws_high", 0.0)
        sig = detector.analyze(sym, df_1m, df_15m, ws_high_override=ws_high)
        pump_score = sig.score if sig else 0

        # ── 3-TẦNG XÁC NHẬN ────────────────────────────────
        confirm = confirm_pump_top(
            symbol     = sym,
            spike_pct  = spike_pct,
            pump_score = pump_score,
            ob_tracker = ob,
        )

        logger.info(
            f"[WS-3Tier] {sym}: tiers={confirm['tiers_passed']}/3 "
            f"confidence={confirm['confidence']} | {confirm['reason']}"
        )

        # Lưu signal vào state (kể cả chưa đủ tầng — để web hiển thị)
        if sig:
            sig_dict = {
                "symbol": sig.symbol, "is_pump_top": sig.is_pump_top,
                "score": sig.score, "pump_pct": sig.pump_pct,
                "signals": sig.signals + [f"OB={confirm['ob_score']} {confirm['ob_trend']}"],
                "entry_price": sig.entry_price, "sl_price": sig.sl_price,
                "tp1_price": sig.tp1_price, "tp2_price": sig.tp2_price,
                "atr": sig.atr, "volume_ratio": sig.volume_ratio,
                "rsi": sig.rsi, "timestamp": sig.timestamp,
                "confidence": confirm["confidence"],
                "tiers": confirm["tiers_passed"],
            }
            with lock:
                signals = state.get("pump_signals", [])
                idx = next((i for i, s in enumerate(signals)
                            if s.get("symbol") == sym), None)
                if idx is not None:
                    signals[idx] = sig_dict
                else:
                    signals.append(sig_dict)
                state["pump_signals"] = signals[-100:]

        # ── ALERT: cần >= 2/3 tầng ─────────────────────────
        should_alert = (
            confirm["tiers_passed"] >= 2 or
            spike_pct >= _SPIKE_CONFIRM_PCT
        )

        if should_alert:
            _ws_spike_send_alert(
                sym, trigger_price, spike_pct, sig, notifier_ref,
                confirm=confirm
            )
        else:
            logger.info(
                f"[WS-3Tier] {sym}: chỉ {confirm['tiers_passed']}/3 tầng "
                f"— bỏ qua (confidence={confirm['confidence']})"
            )
            return

        # ── AUTO SHORT: cần 3/3 tầng hoặc confidence >= 75 ─
        auto_short = getattr(config, "PUMP_AUTO_SHORT", False)
        strong_enough = (
            confirm["tiers_passed"] == 3 or
            confirm["confidence"] >= 75
        )
        if auto_short and strong_enough and sig:
            _ws_spike_do_short(sym, sig, exchange_ref, notifier_ref,
                               confidence=confirm["confidence"])

    except Exception as e:
        logger.error(f"[WS-Spike] Full analysis {sym} error: {e}")


def _ws_spike_send_alert(sym: str, price: float, spike_pct: float,
                          sig, notifier_ref, confirm: dict = None):
    """Gửi Telegram alert pump spike với thông tin 3-tầng."""
    try:
        tiers    = confirm["tiers_passed"] if confirm else "?"
        conf_pct = confirm["confidence"]   if confirm else 0
        ob_score = confirm["ob_score"]     if confirm else 0
        ob_trend = confirm["ob_trend"]     if confirm else ""
        reason   = confirm["reason"]       if confirm else ""
        tier_bar = "🟢" * (tiers if isinstance(tiers, int) else 0) + "⚫" * (3 - (tiers if isinstance(tiers, int) else 0))

        if sig and sig.is_pump_top:
            # Full signal — thêm thông tin 3-tầng vào telegram
            base = sig.to_telegram()
            extra = (
                f"\n{'─'*34}\n"
                f"📊 <b>3-TẦNG XÁC NHẬN:</b> {tier_bar} {tiers}/3\n"
                f"🎯 Confidence: <b>{conf_pct}%</b>\n"
                f"📖 OB Score  : {ob_score} ({ob_trend})\n"
                f"🔍 {reason}"
            )
            notifier_ref.telegram.send(base + extra)
        else:
            score_str = f" | Score: {sig.score}/100" if sig else ""
            notifier_ref.telegram.send(
                f"⚡ <b>PUMP SPIKE</b> {tier_bar}\n"
                f"{'─'*30}\n"
                f"🪙 {sym}  📈 <b>+{spike_pct:.1f}%</b>\n"
                f"💰 ${price:,.6g}{score_str}\n"
                f"📊 Confidence: <b>{conf_pct}%</b>  OB={ob_score}\n"
                f"🔍 {reason}\n"
                f"⚠️ <i>Theo dõi — chưa đủ tín hiệu SHORT</i>\n"
                f"⏰ {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"
            )
    except Exception as e:
        logger.warning(f"[WS-Spike] Alert failed: {e}")


def _ws_spike_do_short(sym: str, sig, exchange_ref, notifier_ref, confidence: int = 0):
    """Vào SHORT ngay sau khi WS phát hiện đỉnh pump."""
    try:
        with lock:
            open_syms = {p["symbol"] for p in state.get("open_positions", [])
                         if abs(float(p.get("positionAmt", 0))) > 0}
            n_open = len(state.get("open_positions", []))

        if sym in open_syms or n_open >= config.MAX_OPEN_POSITIONS:
            return

        exchange_ref.set_leverage(sym, config.LEVERAGE)
        qty = (config.MAX_ORDER_USDT * config.LEVERAGE) / sig.entry_price
        try:
            step, _, decimals, _ = exchange_ref.get_qty_precision(sym)
            qty = max(round(int(qty / step) * step, decimals), step)
        except Exception:
            qty = round(qty, 3)

        if qty * sig.entry_price < 5.0:
            return

        exchange_ref.place_market_order(sym, "SELL", qty)
        import time as _t; _t.sleep(0.3)
        try: exchange_ref.place_stop_loss_order(sym, "BUY", qty, sig.sl_price)
        except Exception as e: logger.error(f"[WS-Spike] SL {sym}: {e}")
        try: exchange_ref.place_take_profit_order(sym, "BUY", qty, sig.tp1_price)
        except Exception as e: logger.error(f"[WS-Spike] TP {sym}: {e}")

        rr = abs(sig.entry_price - sig.tp1_price) / abs(sig.entry_price - sig.sl_price)
        with lock:
            state["trade_log"].append({
                "time":   __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": sym, "side": "SHORT",
                "entry":  sig.entry_price, "sl": sig.sl_price, "tp": sig.tp1_price,
                "qty":    qty, "status": "OPEN",
                "note":   f"ws_spike_s{sig.score}_c{confidence}",
            })

        notifier_ref.telegram.send(
            f"🔴 <b>AUTO SHORT — 3-TẦNG XÁC NHẬN</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {sym}  📈 +{sig.pump_pct:.1f}%  Score {sig.score}/100\n"
            f"🎯 Confidence: <b>{confidence}%</b>\n"
            f"💰 Entry: <b>${sig.entry_price:,.6g}</b>\n"
            f"🛑 SL   : <b>${sig.sl_price:,.6g}</b>\n"
            f"🎯 TP1  : <b>${sig.tp1_price:,.6g}</b>\n"
            f"📐 RR   : 1:{rr:.1f}   📦 Qty: {qty}\n"
            f"⏰ {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"
        )
        logger.info(f"[WS-Spike] SHORT placed: {sym} qty={qty} score={sig.score}")
    except Exception as e:
        logger.error(f"[WS-Spike] Short {sym} failed: {e}")


def _handle_confirmed_top(sig, exchange_ref, notifier_ref):
    """
    Xử lý ConfirmedTopSignal — gửi alert + vào SHORT nếu bật AUTO SHORT.
    Chạy trong thread riêng, không block WS.
    """
    try:
        sym = sig.symbol
        logger.info(
            f"[CTD] CONFIRMED TOP: {sym} pump={sig.pump_pct:.0f}% "
            f"peak={sig.peak_price:,.6g} entry={sig.entry_price:,.6g} "
            f"SL={sig.sl_price:,.6g} TP={sig.tp_price:,.6g} "
            f"RR=1:{sig.rr} cond={sig.conditions_passed}/5"
        )

        # Lưu vào pump_signals cho web
        sig_dict = {
            "symbol":       sym,
            "is_pump_top":  True,
            "score":        min(sig.conditions_passed * 18, 100),
            "pump_pct":     sig.pump_pct,
            "signals":      sig.conditions,
            "entry_price":  sig.entry_price,
            "sl_price":     sig.sl_price,
            "tp1_price":    sig.tp_price,
            "tp2_price":    sig.tp_price,
            "atr":          0,
            "volume_ratio": 0,
            "rsi":          0,
            "timestamp":    sig.timestamp,
            "confidence":   min(sig.conditions_passed * 18, 95),
            "tiers":        sig.conditions_passed,
            "source":       "confirmed_top",
        }
        with lock:
            signals = state.get("pump_signals", [])
            idx = next((i for i, s in enumerate(signals)
                        if s.get("symbol") == sym), None)
            if idx is not None:
                signals[idx] = sig_dict
            else:
                signals.append(sig_dict)
            state["pump_signals"] = signals[-100:]

        # Telegram alert
        notifier_ref.telegram.send(sig.to_telegram())

        # Auto SHORT nếu bật
        auto_short = getattr(config, "PUMP_AUTO_SHORT", False)
        if not auto_short:
            return

        # Check max positions
        with lock:
            open_syms = {p["symbol"] for p in state.get("open_positions", [])
                         if abs(float(p.get("positionAmt", 0))) > 0}
            n_open = len(state.get("open_positions", []))

        if sym in open_syms or n_open >= config.MAX_OPEN_POSITIONS:
            logger.info(f"[CTD] Skip SHORT {sym}: already has position or max reached")
            return

        # Tính qty theo config
        exchange_ref.set_leverage(sym, config.LEVERAGE)
        qty = (config.MAX_ORDER_USDT * config.LEVERAGE) / sig.entry_price
        try:
            step, _, decimals, _ = exchange_ref.get_qty_precision(sym)
            qty = max(round(int(qty / step) * step, decimals), step)
        except Exception:
            qty = round(qty, 3)

        if qty * sig.entry_price < 5.0:
            logger.warning(f"[CTD] {sym} qty too small")
            return

        # Vào lệnh SHORT
        exchange_ref.place_market_order(sym, "SELL", qty)
        import time as _t; _t.sleep(0.3)
        try:
            exchange_ref.place_stop_loss_order(sym, "BUY", qty, sig.sl_price)
        except Exception as e:
            logger.error(f"[CTD] SL failed {sym}: {e}")
        try:
            exchange_ref.place_take_profit_order(sym, "BUY", qty, sig.tp_price)
        except Exception as e:
            logger.error(f"[CTD] TP failed {sym}: {e}")

        with lock:
            state["trade_log"].append({
                "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": sym, "side": "SHORT",
                "entry":  sig.entry_price,
                "sl":     sig.sl_price,
                "tp":     sig.tp_price,
                "qty":    qty, "status": "OPEN",
                "note":   f"confirmed_top_c{sig.conditions_passed}",
            })

        notifier_ref.telegram.send(
            f"🔴 <b>AUTO SHORT — CONFIRMED TOP</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {sym}  📈 pump +{sig.pump_pct:.0f}%\n"
            f"💰 Entry : <b>${sig.entry_price:,.6g}</b>\n"
            f"🛑 SL    : <b>${sig.sl_price:,.6g}</b>\n"
            f"🎯 TP    : <b>${sig.tp_price:,.6g}</b>\n"
            f"📐 RR    : 1:{sig.rr}   📦 Qty: {qty}\n"
            f"✅ {sig.conditions_passed}/5 điều kiện pass\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        logger.info(f"[CTD] SHORT placed: {sym} qty={qty} RR=1:{sig.rr}")

    except Exception as e:
        logger.error(f"[CTD] Handle confirmed top {sig.symbol} failed: {e}")


def _set_sltp_cooldown(symbol: str):
    """Set cooldown để auto_sltp không đặt trùng."""
    with lock:
        state.setdefault("_sltp_cooldown", {})[symbol] = time.time()


def _fetch_actual_pnl(exchange_ref, symbol: str, side: str,
                      qty: float, entry: float, cur_price: float,
                      open_time_ms: int = 0) -> float:
    """
    Lấy PnL thực tế sau khi đóng lệnh từ Binance /fapi/v1/income.
    Kết quả khớp với app Binance (đã tính phí).
    Fallback: tính thủ công nếu API lỗi hoặc trả về 0.
    """
    try:
        # Dùng open_time nếu có, không thì lùi 30 phút để chắc bắt được lệnh
        start_ms = open_time_ms if open_time_ms > 0 else int(time.time() * 1000) - 1_800_000
        realized = exchange_ref.get_realized_pnl(symbol, start_ms)
        if realized != 0:
            logger.debug(f"[PnL] {symbol} income API: ${realized:+.4f}")
            return round(realized, 4)
    except Exception as e:
        logger.debug(f"[PnL] income API failed {symbol}: {e}")

    # Fallback: qty × delta_price (không có phí — ước tính)
    if side == "LONG":
        return round(qty * (cur_price - entry), 4)
    else:
        return round(qty * (entry - cur_price), 4)


def _armed_execute(sym, info, trigger_price):
    """Thực thi armed entry — chạy trong thread riêng, không block WS."""
    try:
        exc  = _ws_exchange_ref[0]
        noti = _ws_notifier_ref[0]
        if not exc or not noti:
            return

        # Remove khỏi armed ngay để không trigger lại
        with lock:
            armed = state.get("armed_entries", {})
            if sym not in armed:
                return  # Đã bị remove bởi thread khác
            armed.pop(sym, None)

            # Check max positions
            n_open = len(state.get("open_positions", []))
            if n_open >= config.MAX_OPEN_POSITIONS:
                return
            open_syms = {p["symbol"] for p in state.get("open_positions", [])
                         if abs(float(p.get("positionAmt", 0))) > 0}
            if sym in open_syms:
                return

        try:
            exc.set_leverage(sym, config.LEVERAGE)
        except Exception:
            pass

        bal = exc.get_total_equity()

        # Tính actual_sl trước để calc_qty dùng đúng SL distance
        actual_sl = info["sl"]
        try:
            from indicators import calculate_atr
            from scanner import _klines_to_df
            klines_15m = exc.get_klines(sym, "15m", limit=50)
            df_15m = _klines_to_df(klines_15m)
            atr_val = float(calculate_atr(df_15m["high"], df_15m["low"], df_15m["close"]).iloc[-1])
            sl_dist = max(atr_val * 2.0, trigger_price * 0.02)
            if info["signal"] == "LONG":
                actual_sl = round(trigger_price - sl_dist, 8)
                if actual_sl >= trigger_price:
                    actual_sl = round(trigger_price * 0.98, 8)
            else:
                actual_sl = round(trigger_price + sl_dist, 8)
                if actual_sl <= trigger_price:
                    actual_sl = round(trigger_price * 1.02, 8)
        except Exception:
            pass

        # Dùng actual_sl để tính qty đúng risk 1%
        qty = calc_qty(bal, trigger_price, actual_sl, symbol=sym, exchange=exc)
        if qty * trigger_price < 5.0:
            qty = round(5.0 / trigger_price + 0.001, 3)

        exc.place_market_order(sym, info["side"], qty)
        time.sleep(0.5)

        # SL retry 3x
        sl_ok = False
        for _try in range(3):
            try:
                exc.place_stop_loss_order(sym, info["close_side"], qty, actual_sl)
                sl_ok = True
                break
            except Exception:
                time.sleep(0.3)
        if not sl_ok:
            logger.debug(f"[Armed] SL FAILED {sym} — keeping position, auto_sltp will retry")
            if noti:
                noti.telegram.send(
                    f"⚠️ <b>SL FAILED</b>: {sym} {info['signal']}\n"
                    f"Không đặt được SL — giữ lệnh, auto SL/TP sẽ thử lại\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                )
            return

        # TP
        try:
            exc.place_take_profit_order(sym, info["close_side"], qty, info["tp"])
        except Exception:
            pass

        with lock:
            state["trade_log"].append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": sym, "side": info["signal"],
                "entry": trigger_price, "sl": actual_sl, "tp": info["tp"],
                "qty": qty, "status": "OPEN", "note": "armed_ws_trigger"
            })

        icon = "🟢" if info["signal"] == "LONG" else "🔴"
        rr = abs(info["tp"] - trigger_price) / abs(trigger_price - info["sl"]) if abs(trigger_price - info["sl"]) > 0 else 0
        noti.telegram.send(
            f"{icon} <b>⚡ ARMED TRIGGERED</b>: {sym} {info['signal']}\n"
            f"💰 Entry: {trigger_price:.6f}\n"
            f"🛑 SL: {info['sl']:.6f} | 🎯 TP: {info['tp']:.6f}\n"
            f"📐 RR: 1:{rr:.1f} | Score: {info['score']}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        logger.info(f"[Armed] ✅ WS TRIGGERED {sym} {info['signal']} @ {trigger_price:.6f}")

    except Exception as e:
        logger.error(f"[Armed] Execute {sym}: {e}")


def price_ws_streamer():
    """WebSocket stream giá realtime từ Binance — nhanh hơn REST 30 lần"""
    import websocket as ws_lib
    import json as _json

    symbols = [s.lower() for s in WATCHLIST]
    streams = "/".join([f"{s}@markPrice@1s" for s in symbols])

    base_ws = "wss://fstream.binance.com" if not config.USE_TESTNET else "wss://stream.binancefuture.com"
    url = f"{base_ws}/stream?streams={streams}"

    # Đặt exchange/notifier reference cho spike checker
    _ws_exchange_ref  = [None]
    _ws_notifier_ref  = [None]

    # Khởi tạo ScanPriceMonitor singleton cho scan coins
    from ws_price_monitor import get_scan_monitor
    _scan_monitor = get_scan_monitor(
        symbols     = list(getattr(config, "FIXED_COINS", WATCHLIST)),
        window_sec  = 30,    # cửa sổ 30s
        drop_pct    = 2.5,   # dump >= 2.5% → wake up LONG check
        bounce_pct  = 2.0,   # bounce >= 2.0% → wake up breakout check
        cooldown_sec = 90,   # 90s cooldown mỗi coin
    )

    def on_message(wsapp, message):
        try:
            data    = _json.loads(message)
            payload = data.get("data", {})
            sym     = payload.get("s", "")
            mark    = float(payload.get("p", 0))
            if sym and mark > 0:
                with lock:
                    state["prices"][sym] = mark

                # ── MAX LOSS REALTIME CHECK — check ngay trên WS tick ──
                if getattr(config, "MAX_LOSS_ENABLED", False):
                    max_loss = getattr(config, "MAX_LOSS_PER_POSITION", 20.0)
                    with lock:
                        open_pos_ws = list(state.get("open_positions", []))
                    for _p in open_pos_ws:
                        if _p.get("symbol") != sym:
                            continue
                        _amt = float(_p.get("positionAmt", 0))
                        if abs(_amt) == 0:
                            continue
                        _entry = float(_p.get("entryPrice", 0))
                        if _entry <= 0:
                            continue
                        # Tính PnL realtime từ mark price WS — không dùng cached PnL
                        _pnl = (mark - _entry) * abs(_amt) if _amt > 0 else (_entry - mark) * abs(_amt)
                        if _pnl < -max_loss:
                            exc  = _ws_exchange_ref[0]
                            noti = _ws_notifier_ref[0]
                            if exc:
                                # Check chưa bị đóng trước đó (tránh double close)
                                _already_closing_key = f"_ml_closing_{sym}"
                                with lock:
                                    if state.get(_already_closing_key):
                                        break
                                    state[_already_closing_key] = True
                                import threading as _th_ml
                                def _do_max_loss_close(_sym, _close_side, _qty, _pnl_val):
                                    try:
                                        exc.place_market_order(_sym, _close_side, _qty)
                                        exc.cancel_all_orders(_sym)
                                        logger.info(f"[MAX LOSS WS] Closed {_sym} pnl=${_pnl_val:.2f}")
                                        if noti:
                                            noti.telegram.send(
                                                f"🚨 <b>MAX LOSS</b>: {_sym}\n"
                                                f"💵 PnL: <b>${_pnl_val:.2f}</b> (vượt -${max_loss})\n"
                                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                            )
                                    except Exception as _e:
                                        logger.error(f"[MAX LOSS WS] {_sym}: {_e}")
                                    finally:
                                        with lock:
                                            state.pop(f"_ml_closing_{_sym}", None)
                                _close_side = "SELL" if _amt > 0 else "BUY"
                                _qty = abs(_amt)
                                _th_ml.Thread(
                                    target=_do_max_loss_close,
                                    args=(sym, _close_side, _qty, _pnl),
                                    daemon=True
                                ).start()
                        break

                # ── ARMED ENTRY CHECK — khớp ngay khi giá tới zone ──
                with lock:
                    armed = state.get("armed_entries", {})
                    armed_info = armed.get(sym)
                if armed_info:
                    entry_p = armed_info["entry_price"]
                    triggered = False
                    if armed_info["signal"] == "LONG" and mark <= entry_p:
                        triggered = True
                    elif armed_info["signal"] == "SHORT" and mark >= entry_p:
                        triggered = True
                    if triggered:
                        # Spawn thread để không block WS
                        import threading as _th_armed
                        _th_armed.Thread(
                            target=_armed_execute,
                            args=(sym, armed_info, mark),
                            daemon=True
                        ).start()

                # ── SCAN PRICE MONITOR — phát hiện dump/bounce cho scan engine ──
                # Chỉ forward tick cho monitor, không chạy indicators ở đây
                _scan_monitor.on_price_tick(sym, mark)

                # ── PUMP SPIKE CHECK — coin trong pump_watch_coins VÀ pump_nhe_coins ──
                with lock:
                    pump_watch     = set(state.get("pump_watch_coins", []))
                    pump_nhe_watch = set(state.get("pump_nhe_coins", []))
                all_pump_coins = pump_watch | pump_nhe_watch
                if sym in all_pump_coins:
                    exc  = _ws_exchange_ref[0]
                    noti = _ws_notifier_ref[0]
                    if exc and noti:
                        # 1. Spike detector — detect pump đang xảy ra
                        _ws_pump_spike_check(sym, mark, exc, noti)
                        # 2. Confirmed top detector — detect đỉnh đã xác nhận
                        try:
                            from confirmed_top_detector import get_ctd
                            from orderbook_detector import get_ob_tracker
                            _ctd = get_ctd(config)
                            _ob  = get_ob_tracker(
                                "wss://fstream.binance.com" if not config.USE_TESTNET
                                else "wss://stream.binancefuture.com"
                            )
                            _ct_sig = _ctd.on_price_tick(sym, mark, exc, _ob)
                            if _ct_sig:
                                import threading as _th
                                _th.Thread(
                                    target=_handle_confirmed_top,
                                    args=(_ct_sig, exc, noti),
                                    daemon=True
                                ).start()
                        except Exception as _cte:
                            logger.debug(f"[CTD] tick error: {_cte}")

        except Exception:
            pass

    def on_error(wsapp, error):
        logger.debug(f"Price WS error: {error}")

    def on_close(wsapp, close_code, close_msg):
        logger.debug("Price WS closed, reconnecting in 3s...")

    while state["running"]:
        try:
            # Lấy exchange/notifier từ state (được set sau khi bot start)
            with lock:
                _ws_exchange_ref[0]  = state.get("_exchange")
                _ws_notifier_ref[0]  = state.get("_notifier")

            # Rebuild stream URL mỗi lần reconnect (watchlist có thể thay đổi)
            with lock:
                pump_watch = list(state.get("pump_watch_coins", []))
            all_syms = list(dict.fromkeys(
                [s.lower() for s in WATCHLIST] +
                [s.lower() for s in pump_watch]
            ))
            streams = "/".join([f"{s}@markPrice@1s" for s in all_syms])
            url = f"{base_ws}/stream?streams={streams}"

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
            # Fetch giá MỌI coin trong WATCHLIST mỗi lần (không check có sẵn nữa)
            new_prices = {}
            for sym in WATCHLIST:
                try:
                    new_prices[sym] = exchange.get_ticker_price(sym)
                except Exception:
                    pass
            # Cập nhật giá pump coins (không có trong WATCHLIST)
            with lock:
                pump_coins_extra = [s for s in state.get("pump_watch_coins", [])
                                    if s not in WATCHLIST]
            for sym in pump_coins_extra:
                try:
                    new_prices[sym] = exchange.get_ticker_price(sym)
                except Exception:
                    pass
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

            # Lấy balance NGOÀI lock — tránh block Flask khi gọi Binance API
            new_balance = exchange.get_account_balance()

            with lock:
                state["prices"].update(new_prices)
                state["balance"] = new_balance

                # ── Detect positions closed externally (app/web Binance) ──
                prev_positions = {p["symbol"] for p in state.get("open_positions", [])
                                  if abs(float(p.get("positionAmt", 0))) > 0}
                curr_positions = {p["symbol"] for p in open_pos}
                closed_externally = prev_positions - curr_positions

                # Lấy thông tin cần thiết từ trade_log TRONG lock
                closed_ext_info = {}
                for sym in closed_externally:
                    for t in reversed(state.get("trade_log", [])):
                        if t.get("symbol") == sym and t.get("status") == "OPEN":
                            # Parse open_time → ms để query Binance income đúng lệnh
                            try:
                                from datetime import datetime as _dtp
                                _ot_str = t.get("time", "")
                                _ot_ms  = int(_dtp.strptime(_ot_str, "%Y-%m-%d %H:%M:%S").timestamp() * 1000) if _ot_str else 0
                            except Exception:
                                _ot_ms = 0
                            closed_ext_info[sym] = {
                                "entry": t.get("entry", 0),
                                "side":  t.get("side", "LONG"),
                                "qty":   t.get("qty", 0),
                                "close_price": state["prices"].get(sym, t.get("entry", 0)),
                                "sl": t.get("sl", 0),
                                "tp": t.get("tp", 0),
                                "open_time_ms": _ot_ms,
                            }
                            break

            # ── Xử lý closed_externally NGOÀI lock — tránh block Flask ──
            for sym, info in closed_ext_info.items():
                entry       = info["entry"]
                side        = info["side"]
                qty         = info["qty"]
                close_price = info["close_price"]
                pnl_usd     = 0.0
                pnl_pct     = 0.0

                # Huỷ SL/TP mồ côi
                try:
                    exchange.cancel_all_orders(sym)
                    logger.info(f"[Sync] Cancelled orphan orders for {sym}")
                except Exception:
                    pass

                # Lấy PnL thật từ Binance income (khớp với app Binance, đã trừ phí)
                try:
                    open_ms = info.get("open_time_ms", 0)
                    # Nếu không có open_time thì dùng 10 phút trước để an toàn
                    start_ms = open_ms if open_ms > 0 else int(time.time() * 1000) - 600_000
                    realized = exchange.get_realized_pnl(sym, start_ms)
                    if realized != 0:
                        pnl_usd = realized
                        pnl_pct = pnl_usd / (entry * qty / config.LEVERAGE) * 100 if entry > 0 and qty > 0 else 0
                        logger.info(f"[Sync] Binance income PnL for {sym}: ${pnl_usd:+.2f}")
                    else:
                        # Fallback: tính thủ công nếu income API trả về 0
                        if entry > 0:
                            pnl_pct = (close_price - entry) / entry * 100 if side == "LONG" else (entry - close_price) / entry * 100
                            pnl_usd = qty * (close_price - entry) if side == "LONG" else qty * (entry - close_price)
                            logger.info(f"[Sync] Fallback calc PnL for {sym}: ${pnl_usd:+.2f}")
                except Exception as _fe:
                    logger.debug(f"[Sync] get_realized_pnl failed {sym}: {_fe}")
                    if entry > 0:
                        pnl_pct = (close_price - entry) / entry * 100 if side == "LONG" else (entry - close_price) / entry * 100
                        pnl_usd = qty * (close_price - entry) if side == "LONG" else qty * (entry - close_price)

                # Ghi lại state TRONG lock — nhanh, không có API call
                with lock:
                    for t in reversed(state.get("trade_log", [])):
                        if t.get("symbol") == sym and t.get("status") == "OPEN":
                            t.update({
                                "status":   "CLOSED",
                                "close":    close_price,
                                "pnl_usdt": round(pnl_usd, 2),
                                "pnl_pct":  round(pnl_pct, 2),
                                "note":     "closed_external"
                            })
                            break

                logger.info(f"[Sync] Detected external close: {sym} PnL=${pnl_usd:+.2f}")
                from trade_history import save_history
                save_history(state["trade_log"])

                # Notify
                try:
                    notifier_inst = state.get("_notifier")
                    if notifier_inst:
                        icon = "✅" if pnl_usd >= 0 else "❌"
                        sl_p = info.get("sl", 0)
                        tp_p = info.get("tp", 0)
                        if tp_p and abs(close_price - tp_p) / max(tp_p, 0.0001) < 0.005:
                            close_reason = "🎯 TP hit"
                        elif sl_p and abs(close_price - sl_p) / max(sl_p, 0.0001) < 0.005:
                            close_reason = "🛑 SL hit"
                        elif pnl_usd > 0:
                            close_reason = "🎯 Chốt lời"
                        else:
                            close_reason = "🛑 Cắt lỗ"
                        notifier_inst.telegram.send(
                            f"🔒 <b>LỆNH ĐÓNG (từ Binance)</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📊 {sym} {side} {close_reason}\n"
                            f"💵 PnL: <b>{icon} ${pnl_usd:+.2f}</b> ({pnl_pct:+.1f}%)\n"
                            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                        )
                except Exception:
                    pass

            with lock:

                state["open_positions"] = open_pos

            # ── Max loss check: đóng lệnh nếu lỗ > threshold ──
            max_loss_enabled = getattr(config, "MAX_LOSS_ENABLED", True)
            max_loss = getattr(config, "MAX_LOSS_PER_POSITION", 20.0)
            if max_loss_enabled:
                for p in open_pos:
                    sym = p["symbol"]
                    amt = float(p.get("positionAmt", 0))
                    if abs(amt) == 0:
                        continue

                    # Tính PnL trực tiếp từ Binance (không dùng _pnl cache)
                    entry = float(p.get("entryPrice", 0))
                    mark = float(p.get("markPrice", 0)) or state.get("prices", {}).get(sym, 0)
                    if entry <= 0 or mark <= 0:
                        continue
                    if amt > 0:  # LONG
                        pnl = (mark - entry) * abs(amt)
                    else:  # SHORT
                        pnl = (entry - mark) * abs(amt)

                    if pnl < -max_loss:
                        # Max loss → đóng NGAY khi lỗ vượt ngưỡng
                        qty = abs(amt)
                        close_side = "SELL" if amt > 0 else "BUY"
                        if qty == int(qty):
                            qty = int(qty)
                        try:
                            remaining = qty
                            while remaining > 0:
                                batch = min(remaining, 100000)
                                if batch == int(batch):
                                    batch = int(batch)
                                exchange.place_market_order(sym, close_side, batch)
                                remaining -= batch
                            exchange.cancel_all_orders(sym)
                            logger.info(f"[MAX LOSS] Closed {sym} pnl=${pnl:.2f} exceeded -${max_loss}")
                            try:
                                notifier_inst = state.get("_notifier")
                                if notifier_inst:
                                    notifier_inst.telegram.send(
                                        f"🚨 <b>MAX LOSS SAFETY NET</b>\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"📊 {sym}\n"
                                        f"💵 PnL: <b>${pnl:.2f}</b> (vượt -${max_loss})\n"
                                        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                    )
                            except Exception:
                                pass
                            with lock:
                                _ml_open_ms = 0
                                for t in reversed(state.get("trade_log", [])):
                                    if t.get("symbol") == sym and t.get("status") == "OPEN":
                                        try:
                                            from datetime import datetime as _dtp_ml
                                            _ml_open_ms = int(_dtp_ml.strptime(t["time"], "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
                                        except Exception:
                                            pass
                                        break
                            time.sleep(0.5)
                            _ml_actual_pnl = _fetch_actual_pnl(exchange, sym, "LONG" if amt > 0 else "SHORT",
                                                               qty, entry, p.get("_mark", mark), _ml_open_ms)
                            with lock:
                                for t in reversed(state.get("trade_log", [])):
                                    if t.get("symbol") == sym and t.get("status") == "OPEN":
                                        t.update({"status": "CLOSED", "close": p.get("_mark", mark),
                                                  "pnl_usdt": round(_ml_actual_pnl, 2), "pnl_pct": round(p.get("_pct", 0), 2)})
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
        time.sleep(3)  # update giá mỗi 3s

# ============================================================
# THREAD 2: Trade engine mỗi 60 giây
# ============================================================

def _get_daily_pnl() -> float:
    """Tính PnL trong ngày hôm nay từ trade_log."""
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    with lock:
        logs = list(state.get("trade_log", []))
    daily = sum(
        t.get("pnl_usdt", 0)
        for t in logs
        if t.get("status") == "CLOSED"
        and t.get("time", "").startswith(today)
    )
    return float(daily)


def _get_consecutive_losses() -> int:
    """Đếm số lần thua liên tiếp gần nhất từ trade_log."""
    with lock:
        logs = list(state.get("trade_log", []))
    closed = [t for t in logs if t.get("status") == "CLOSED"]
    closed.sort(key=lambda t: t.get("time", ""), reverse=True)
    count = 0
    for t in closed:
        if (t.get("pnl_usdt", 0) or 0) < 0:
            count += 1
        else:
            break
    return count


def check_daily_kill_switch(balance: float) -> dict:
    """
    Kiểm tra Daily Kill Switch trước khi vào lệnh mới.

    Returns:
        {"ok": bool, "reason": str}
    """
    if not getattr(config, "DAILY_KILL_SWITCH_ENABLED", True):
        return {"ok": True, "reason": "kill switch disabled"}

    max_loss_pct   = getattr(config, "MAX_DAILY_LOSS_PCT",        0.03)
    max_consec     = getattr(config, "MAX_CONSECUTIVE_LOSSES",    3)
    pause_secs     = getattr(config, "CONSECUTIVE_LOSS_PAUSE_SECS", 1800)

    # Check daily loss %
    daily_pnl = _get_daily_pnl()
    max_loss_usdt = balance * max_loss_pct
    if daily_pnl <= -max_loss_usdt:
        return {
            "ok": False,
            "reason": f"Daily loss ${daily_pnl:.2f} >= limit ${max_loss_usdt:.2f} ({max_loss_pct*100:.0f}%)",
        }

    # Check consecutive losses → pause
    consec = _get_consecutive_losses()
    if consec >= max_consec:
        last_loss_ts = state.get("last_loss_time", 0)
        elapsed = time.time() - last_loss_ts
        if elapsed < pause_secs:
            remaining = int(pause_secs - elapsed)
            return {
                "ok": False,
                "reason": f"{consec} lỗ liên tiếp → pause còn {remaining//60}m{remaining%60:02d}s",
            }

    return {"ok": True, "reason": f"daily={daily_pnl:+.2f} consec={consec}"}


def calc_qty(balance, entry, sl, symbol="", exchange=None):
    """
    Tính qty theo Risk% / SL distance (P0 position sizing).
    Fallback về MAX_ORDER_USDT nếu SL không hợp lệ.
    """
    import math as _math

    # ── P0: Risk-based sizing ─────────────────────────────────
    risk_pct     = getattr(config, "RISK_PER_TRADE_PCT",  0.01)   # 1%
    # Max notional = balance × 50% (tự scale theo balance)
    # Có thể override bằng RISK_MAX_ORDER_USDT nếu set > 0
    _cfg_max     = getattr(config, "RISK_MAX_ORDER_USDT", 0)
    max_notional = _cfg_max if _cfg_max > 0 else balance * 0.5

    if entry > 0 and sl > 0 and abs(entry - sl) > 0:
        sl_dist_pct = abs(entry - sl) / entry          # e.g. 0.02 = 2%
        risk_usdt   = balance * risk_pct               # e.g. $100 × 1% = $1
        # notional = risk / sl_dist
        notional    = risk_usdt / sl_dist_pct          # e.g. $1 / 2% = $50
        notional    = min(notional, max_notional)      # hard cap
        qty         = notional / entry
        logger.debug(f"[SizeCalc] bal=${balance:.2f} risk={risk_pct*100:.1f}%=${risk_usdt:.3f} "
                    f"sl_dist={sl_dist_pct*100:.2f}% notional=${notional:.2f} "
                    f"max_notional=${max_notional:.2f} qty={qty:.4f}")
    else:
        # Fallback: dùng MAX_ORDER_USDT cố định
        notional    = min(config.MAX_ORDER_USDT, max_notional)
        qty         = (notional * config.LEVERAGE) / entry if entry > 0 else 1.0

    # ── Lấy stepSize + maxQty + min_notional từ Binance API ──
    step         = 1.0
    max_qty      = None
    decimals     = 0
    min_notional = 5.0
    if exchange and symbol:
        try:
            step, max_qty, decimals, min_notional = exchange.get_qty_precision(symbol)
        except Exception:
            pass

    if max_qty is None:
        if entry >= 10000:  max_qty = 100
        elif entry >= 1000: max_qty = 1000
        elif entry >= 100:  max_qty = 10000
        elif entry >= 10:   max_qty = 100000
        elif entry >= 1:    max_qty = 500000
        elif entry >= 0.1:  max_qty = 50000
        elif entry >= 0.01: max_qty = 20000
        else:               max_qty = 10000

    qty = min(qty, max_qty)

    # Hard cap notional
    max_notional_qty = max_notional / entry if entry > 0 else qty
    qty = min(qty, max_notional_qty)

    # Round theo stepSize
    if step >= 1:
        qty = int(qty // step) * int(step)
    else:
        qty = round(int(qty / step) * step, decimals)

    # Đảm bảo notional >= min_notional (tránh lỗi 400)
    min_qty_notional = min_notional / entry if entry > 0 else step
    if step >= 1:
        min_qty_notional = max(step, int(_math.ceil(min_qty_notional / step)) * int(step))
    else:
        min_qty_notional = max(step, round(_math.ceil(min_qty_notional / step) * step, decimals))
    qty = max(qty, min_qty_notional)

    return max(qty, step)

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

            # SL/TP đã được đặt trên Binance → KHÔNG tự đóng lệnh ở đây
            # Chỉ cập nhật trailing stop để điều chỉnh SL order trên Binance nếu cần

            # Trailing stop — chỉ update state nội bộ, không đóng market
            if config.TRAILING_STOP:
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

        except Exception as e:
            logger.error(f"Monitor engine: {e}", exc_info=True)
        time.sleep(3)

# ============================================================
# ============================================================
# THREAD 2a2: Position Reversal Monitor — chốt lời sớm khi đảo chiều
# Mỗi 10s: scan tất cả open positions
# Nếu đang có lời + xuất hiện dấu hiệu đảo chiều → đóng ngay
# ============================================================
def position_reversal_monitor(exchange, notifier):
    """
    Monitor tất cả open positions.
    Khi đang có lời mà phát hiện đảo chiều → đóng trước khi về entry / dính SL.
    Điều kiện đảo chiều (cần >= 2/3):
      1. RSI đảo chiều: SHORT đang lời mà RSI < 35 rồi bật lên > 40
      2. EMA cross ngược chiều lệnh
      3. Giá đã chạm TP 50%+ rồi quay đầu > 30% khoảng TP
    """
    import time as _time
    _time.sleep(15)  # Đợi bot ổn định
    logger.info("[ReversalMon] Started — monitoring all positions for early exit")

    # Track RSI trước đó cho từng symbol
    _prev_rsi = {}
    _min_price = {}  # SHORT: giá thấp nhất đạt được
    _max_price = {}  # LONG: giá cao nhất đạt được

    while state["running"]:
        try:
            with lock:
                open_positions = list(state.get("open_positions", []))
                # Chỉ áp dụng reversal monitor cho lệnh do PUMP engine vào
                pump_trade_syms = set(state.get("pump_trade_symbols", set()))

            for pos in open_positions:
                symbol = pos.get("symbol", "")
                amt    = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue

                # ── CHỈ áp dụng cho lệnh pump — bỏ qua lệnh scan thường ──
                if symbol not in pump_trade_syms:
                    continue

                side       = "SHORT" if amt < 0 else "LONG"
                entry      = float(pos.get("entryPrice", 0))
                mark_price = pos.get("_mark", 0) or float(pos.get("markPrice", 0))
                pnl        = pos.get("_pnl", 0)
                pnl_pct    = pos.get("_pct", 0)

                if entry <= 0 or mark_price <= 0:
                    continue

                # Chỉ check khi đang có lời >= 0.3%
                if pnl_pct < 0.3:
                    continue

                # Lời >= 10% → để Trailing Lock lo, Reversal Monitor không đóng
                if pnl_pct >= 10.0:
                    continue

                # ── MFE Retracement logic ──
                mfe_key = f"_mfe_{symbol}"
                with lock:
                    if side == "SHORT":
                        mfe_price = state.get(mfe_key, mark_price)
                        if mark_price < mfe_price:
                            state[mfe_key] = mark_price
                            mfe_price = mark_price
                    else:
                        mfe_price = state.get(mfe_key, mark_price)
                        if mark_price > mfe_price:
                            state[mfe_key] = mark_price
                            mfe_price = mark_price

                if side == "SHORT":
                    mfe_pct = (entry - mfe_price) / entry * 100
                else:
                    mfe_pct = (mfe_price - entry) / entry * 100

                # ── Breakeven Exit: Peak Profit Trailing with Reversal Confirmation ──
                min_hold   = getattr(config, "BREAKEVEN_PUMP_HOLD_SECONDS", 180)
                peak_pct   = getattr(config, "BREAKEVEN_PUMP_PEAK_PCT", 3.0)
                pnl_floor  = getattr(config, "BREAKEVEN_PUMP_PNL_FLOOR", 1.0)

                entry_time = None
                with lock:
                    for t in reversed(state.get("trade_log", [])):
                        if t.get("symbol") == symbol and t.get("status") == "OPEN":
                            entry_time = t.get("time")
                            break
                held_secs = 0
                if entry_time:
                    try:
                        held_secs = (datetime.now() - datetime.strptime(entry_time, "%Y-%m-%d %H:%M:%S")).total_seconds()
                    except Exception:
                        pass
                else:
                    held_secs = 9999  # không tìm được → coi như đã hold đủ

                if not getattr(config, "BREAKEVEN_EXIT_ENABLED", True):
                    pass
                elif held_secs < min_hold:
                    pass  # chưa đủ thời gian hold — chưa kích hoạt
                elif mfe_pct < peak_pct:
                    pass  # chưa đạt peak profit tối thiểu — chưa kích hoạt trailing
                else:
                    # Đã hold đủ min_hold VÀ đạt peak_pct → trailing kích hoạt
                    if pnl_pct <= pnl_floor:
                        with lock:
                            state.pop(mfe_key, None)
                            state.pop(f"_be_rev_{symbol}", None)
                        qty = abs(amt)
                        close_side = "SELL" if side == "LONG" else "BUY"
                        try:
                            exchange.place_market_order(symbol, close_side, qty)
                            exchange.cancel_all_orders(symbol)
                            notifier.telegram.send(
                                f"🔄 <b>⏱ HOLD EXIT (Pump)</b>: {symbol} {side}\n"
                                f"Hold {held_secs:.0f}s · lời rút về {pnl_pct:.1f}% ≤ floor {pnl_floor:.1f}% → chốt\n"
                                f"Peak đạt: {mfe_pct:.1f}%\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            )
                            logger.info(f"[ReversalMon] HOLD EXIT {symbol}: held={held_secs:.0f}s pnl={pnl_pct:.1f}% <= floor={pnl_floor:.1f}%")
                            continue
                        except Exception as e:
                            logger.error(f"[ReversalMon] HOLD EXIT close {symbol}: {e}")

                # ── MFE retracement >= 40% → đóng giữ lời ──
                if mfe_pct >= 3.0:
                    if side == "SHORT":
                        if entry - mfe_price > 0:
                            retracement = (mark_price - mfe_price) / (entry - mfe_price)
                        else:
                            retracement = 0
                    else:
                        if mfe_price - entry > 0:
                            retracement = (mfe_price - mark_price) / (mfe_price - entry)
                        else:
                            retracement = 0

                    if retracement >= 0.40 and pnl_pct > 0.3:
                        # Chỉ đóng khi còn lời thực tế (pnl_pct > 0.3%)
                        # Tránh MFE EXIT khi giá đã hồi vượt entry → lệnh đang lỗ
                        with lock:
                            state.pop(mfe_key, None)
                        qty = abs(amt)
                        close_side = "SELL" if side == "LONG" else "BUY"
                        try:
                            exchange.place_market_order(symbol, close_side, qty)
                            exchange.cancel_all_orders(symbol)
                            notifier.telegram.send(
                                f"🔄 <b>MFE EXIT</b>: {symbol} {side}\n"
                                f"MFE={mfe_pct:.1f}% → hồi {retracement*100:.0f}% → lời {pnl_pct:.1f}%\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            )
                            logger.info(f"[ReversalMon] MFE EXIT {symbol}: mfe={mfe_pct:.1f}% retrace={retracement*100:.0f}%")
                        except Exception as e:
                            logger.error(f"[ReversalMon] MFE close {symbol}: {e}")
                        continue
                    elif retracement >= 0.40 and pnl_pct <= 0.3:
                        # Hồi đủ nhưng đang lỗ/hòa vốn → để SL lo, không đóng thêm lần
                        logger.info(f"[ReversalMon] MFE SKIP {symbol}: retrace={retracement*100:.0f}% nhưng pnl={pnl_pct:.1f}% — để SL xử lý")

                try:
                    # Lấy klines 1m để bắt đảo chiều nhanh
                    klines = exchange.get_klines(symbol, "1m", limit=30)
                    df = _klines_to_df(klines)
                    if df is None or len(df) < 10:
                        continue

                    rsi_series = calculate_rsi(df["close"], 14)
                    rsi_now    = rsi_series.iloc[-1]
                    rsi_prev   = _prev_rsi.get(symbol, rsi_now)

                    ema9  = calculate_ema(df["close"], 9).iloc[-1]
                    ema21 = calculate_ema(df["close"], 21).iloc[-1]
                    close = df["close"].iloc[-1]

                    # Track giá cực trị
                    if side == "SHORT":
                        if symbol not in _min_price or mark_price < _min_price[symbol]:
                            _min_price[symbol] = mark_price
                        min_reached = _min_price[symbol]
                        # % giá đã đi từ entry xuống đáy
                        profit_travel = (entry - min_reached) / entry * 100
                        # % giá đã quay đầu từ đáy lên
                        pullback = (mark_price - min_reached) / max(min_reached, 0.0001) * 100
                    else:
                        if symbol not in _max_price or mark_price > _max_price[symbol]:
                            _max_price[symbol] = mark_price
                        max_reached = _max_price[symbol]
                        profit_travel = (max_reached - entry) / entry * 100
                        pullback = (max_reached - mark_price) / max(max_reached, 0.0001) * 100

                    # ── Điều kiện đảo chiều ──────────────────
                    signals = []

                    # 1. RSI đảo chiều — ngưỡng chặt hơn để tránh false positive khi pump
                    if side == "SHORT":
                        # SHORT đang lời: RSI phải về oversold thật (<35) rồi mới bật
                        # Tránh bắt RSI dao động bình thường trong pump
                        if rsi_prev < 35 and rsi_now > rsi_prev + 5:
                            signals.append(f"RSI bounce {rsi_prev:.0f}→{rsi_now:.0f}")
                    else:
                        # LONG đang lời: RSI đã lên overbought cao (>70) rồi quay xuống rõ
                        if rsi_prev > 70 and rsi_now < rsi_prev - 6:
                            signals.append(f"RSI drop {rsi_prev:.0f}→{rsi_now:.0f}")

                    # 2. EMA cross ngược chiều — cần giá close ĐÃ vượt EMA21
                    # Tránh trigger khi EMA9 > EMA21 do đang giữa pump spike (giá chưa quay đầu)
                    if side == "SHORT" and ema9 > ema21 and close > ema21 * 1.003:
                        # SHORT bị đảo: giá close vẫn vọt cao hơn EMA21 rõ ràng — chưa phải reversal thật
                        # Chỉ tính signal khi close đã kéo EMA9 lên trên EMA21 VÀ profit_travel > 0
                        # (tức giá đã đi về hướng short rồi mới quay ngược lại)
                        if profit_travel > 0.5:
                            signals.append(f"EMA cross UP (9>{21:.0f}) sau lời {profit_travel:.1f}%")
                    elif side == "LONG" and ema9 < ema21 and close < ema21 * 0.997:
                        signals.append(f"EMA cross DOWN (9<21)")

                    # 3. Pullback mạnh sau khi đã đi được lời tốt
                    # Yêu cầu profit_travel >= 2% (đã lời thật, không phải vừa vào)
                    # và pullback >= 40% (hồi đáng kể)
                    if profit_travel >= 2.0 and pullback >= 40:
                        signals.append(f"Pullback {pullback:.0f}% sau khi profit_travel={profit_travel:.1f}%")

                    _prev_rsi[symbol] = rsi_now

                    # Luôn cần >= 2 tín hiệu — bỏ fast-path 1 signal
                    # Lệnh pump nhẹ chưa đủ lời không nên bị cắt chỉ vì 1 pullback ngắn
                    min_signals = 2
                    if len(signals) < min_signals:
                        continue

                    # Kiểm tra config có bật không
                    if not getattr(config, "REVERSAL_MONITOR_ENABLED", True):
                        continue

                    alert_only = getattr(config, "REVERSAL_ALERT_ONLY", False)

                    if alert_only:
                        # Chỉ gửi alert, không đóng
                        notifier.telegram.send(
                            f"⚠️ <b>REVERSAL ALERT</b> (chưa đóng)\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"🪙 {symbol} {side} | Lời {pnl_pct:.1f}%\n"
                            f"📍 Entry: ${entry:.4f} | Mark: ${mark_price:.4f}\n"
                            f"⚠️ Dấu hiệu đảo chiều:\n"
                            + "\n".join([f"  • {s}" for s in signals]) + "\n"
                            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                        )
                        logger.info(f"[ReversalMon] ALERT ONLY {symbol}: {signals}")
                        continue

                    # ── Đóng position ────────────────────────
                    qty      = abs(amt)
                    close_side = "BUY" if side == "SHORT" else "SELL"
                    cur_price  = exchange.get_ticker_price(symbol)

                    exchange.place_market_order(symbol, close_side, qty)
                    exchange.cancel_all_orders(symbol)

                    # Lấy open_time từ trade_log để query PnL đúng lệnh
                    _open_ms = 0
                    with lock:
                        for _t in reversed(state.get("trade_log", [])):
                            if _t.get("symbol") == symbol and _t.get("status") == "OPEN":
                                try:
                                    from datetime import datetime as _dtp2
                                    _open_ms = int(_dtp2.strptime(_t["time"], "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
                                except Exception:
                                    pass
                                break
                    time.sleep(0.5)  # chờ Binance settle income record
                    actual_pnl = _fetch_actual_pnl(exchange, symbol, side, qty, entry, cur_price, _open_ms)
                    icon = "✅" if actual_pnl >= 0 else "⚠️"

                    # Ghi trade log
                    with lock:
                        for t in reversed(state.get("trade_log", [])):
                            if t.get("symbol") == symbol and t.get("status") == "OPEN":
                                t.update({
                                    "status":   "CLOSED",
                                    "close":    cur_price,
                                    "pnl_usdt": round(actual_pnl, 2),
                                    "pnl_pct":  round(pnl_pct, 2),
                                })
                                break
                        # Reset price tracker
                        _min_price.pop(symbol, None)
                        _max_price.pop(symbol, None)

                    notifier.telegram.send(
                        f"🔄 <b>REVERSAL EXIT — Chốt lời sớm</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"🪙 {symbol} {side} | Lời {pnl_pct:.1f}%\n"
                        f"📍 Entry: ${entry:.4f} → Close: ${cur_price:.4f}\n"
                        f"⚠️ Tín hiệu đảo chiều:\n"
                        + "\n".join([f"  • {s}" for s in signals]) + "\n"
                        f"{icon} PnL: <b>${actual_pnl:+.2f}</b>\n"
                        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                    )
                    logger.info(
                        f"[ReversalMon] EXIT {symbol} {side} "
                        f"pnl={actual_pnl:+.2f} signals={signals}"
                    )

                except Exception as e:
                    logger.debug(f"[ReversalMon] {symbol}: {e}")

        except Exception as e:
            logger.error(f"[ReversalMon] Loop error: {e}")

        _time.sleep(10)


# ============================================================
# THREAD 2a3: Scan Position Protector — chốt lời sớm cho lệnh SCAN thường
# Mỗi 15s: check positions KHÔNG phải pump trade
# Nếu đang có lời + có dấu hiệu đảo chiều → đóng trước khi về entry
# Điều kiện nhẹ hơn pump reversal (scan coin không xả nhanh như pump)
# ============================================================
def scan_position_protector(exchange, notifier):
    """
    Bảo vệ lợi nhuận cho lệnh scan thường.
    Chỉ check coin KHÔNG trong pump_trade_symbols.
    Điều kiện đóng sớm (cần >= 2/3):
      1. RSI đảo chiều rõ trên 15m (không dùng 1m vì nhiều noise)
      2. EMA9 cross EMA21 ngược chiều lệnh trên 15m
      3. Giá đã đi được >= 1% lời rồi quay đầu >= 40% khoảng đó
    Fast-path 1m: khi pump ngắn hạn mà 15m chưa kịp phản ánh,
    check thêm 1m — cần >= 2/3 tín hiệu 1m để đóng sớm.
    """
    import time as _time
    _time.sleep(30)  # Đợi bot ổn định
    logger.info("[ScanProtector] Started — protecting scan positions")

    _prev_rsi      = {}     # RSI prev trên 15m
    _prev_rsi_1m   = {}     # RSI prev trên 1m
    _max_price     = {}     # LONG: giá cao nhất
    _min_price     = {}     # SHORT: giá thấp nhất
    _last_klines   = {}     # cache klines để giảm API calls

    while state["running"]:
        try:
            with lock:
                open_positions = list(state.get("open_positions", []))
                pump_trade_syms = set(state.get("pump_trade_symbols", set()))

            for pos in open_positions:
                symbol = pos.get("symbol", "")
                amt    = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue

                # Chỉ xử lý lệnh SCAN thường — bỏ qua pump trades
                if symbol in pump_trade_syms:
                    continue

                side       = "LONG" if amt > 0 else "SHORT"
                entry      = float(pos.get("entryPrice", 0))
                mark_price = pos.get("_mark", 0) or float(pos.get("markPrice", 0))
                pnl_pct    = pos.get("_pct", 0)

                if entry <= 0 or mark_price <= 0:
                    continue

                # Chỉ check khi đang có lời >= 0.8% (lệnh scan cần lời nhiều hơn pump)
                if pnl_pct < 0.8:
                    continue

                # Không đóng nếu config tắt
                if not getattr(config, "SCAN_PROTECT_ENABLED", True):
                    continue

                # Không đóng nếu đang lỗ
                if pnl_pct <= 0:
                    continue

                # Lời >= 8% → để Trailing Lock / MFE lo, không cắt sớm
                # Tránh cắt SHORT giữa pump khi giá chưa kịp đảo chiều thật
                if pnl_pct >= 8.0:
                    continue

                # Track giá cực trị (dùng chung cho cả 15m và 1m)
                if side == "LONG":
                    if symbol not in _max_price or mark_price > _max_price[symbol]:
                        _max_price[symbol] = mark_price
                    max_reached   = _max_price[symbol]
                    profit_travel = (max_reached - entry) / entry * 100
                    pullback      = (max_reached - mark_price) / max(max_reached, 0.0001) * 100
                else:
                    if symbol not in _min_price or mark_price < _min_price[symbol]:
                        _min_price[symbol] = mark_price
                    min_reached   = _min_price[symbol]
                    profit_travel = (entry - min_reached) / entry * 100
                    pullback      = (mark_price - min_reached) / max(min_reached, 0.0001) * 100

                try:
                    from indicators import calculate_rsi, calculate_ema

                    # ── FAST PATH: check 1m trước — bắt pump ngắn hạn ──────
                    # Dùng khi 15m chưa kịp phản ánh (pump diễn ra trong 1-3 candle)
                    klines_1m = exchange.get_klines(symbol, "1m", limit=30)
                    df_1m = _klines_to_df(klines_1m)
                    closed_1m = False
                    if df_1m is not None and len(df_1m) >= 10:
                        rsi_1m     = calculate_rsi(df_1m["close"], 14)
                        rsi_1m_now = rsi_1m.iloc[-1]
                        rsi_1m_prev = _prev_rsi_1m.get(symbol, rsi_1m_now)
                        ema9_1m    = calculate_ema(df_1m["close"], 9).iloc[-1]
                        ema21_1m   = calculate_ema(df_1m["close"], 21).iloc[-1]

                        sigs_1m = []

                        # 1. RSI drop trên 1m
                        if side == "LONG":
                            if rsi_1m_prev > 65 and rsi_1m_now < rsi_1m_prev - 5:
                                sigs_1m.append(f"RSI1m drop {rsi_1m_prev:.0f}→{rsi_1m_now:.0f}")
                        else:
                            if rsi_1m_prev < 35 and rsi_1m_now > rsi_1m_prev + 5:
                                sigs_1m.append(f"RSI1m bounce {rsi_1m_prev:.0f}→{rsi_1m_now:.0f}")

                        # 2. EMA cross trên 1m
                        if side == "LONG" and ema9_1m < ema21_1m:
                            sigs_1m.append(f"EMA9<EMA21 1m (bearish)")
                        elif side == "SHORT" and ema9_1m > ema21_1m:
                            sigs_1m.append(f"EMA9>EMA21 1m (bullish)")

                        # 3. Pullback >= 40% trên 1m (cùng ngưỡng 15m, không hạ)
                        if profit_travel >= 1.0 and pullback >= 40:
                            sigs_1m.append(f"Pullback {pullback:.0f}% 1m (travel={profit_travel:.1f}%)")

                        _prev_rsi_1m[symbol] = rsi_1m_now

                        if len(sigs_1m) >= 2:
                            qty        = abs(amt)
                            close_side = "SELL" if side == "LONG" else "BUY"
                            cur_price  = exchange.get_ticker_price(symbol)

                            exchange.place_market_order(symbol, close_side, qty)
                            exchange.cancel_all_orders(symbol)

                            _open_ms2 = 0
                            with lock:
                                for _t2 in reversed(state.get("trade_log", [])):
                                    if _t2.get("symbol") == symbol and _t2.get("status") == "OPEN":
                                        try:
                                            from datetime import datetime as _dtp3
                                            _open_ms2 = int(_dtp3.strptime(_t2["time"], "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
                                        except Exception:
                                            pass
                                        break
                            time.sleep(0.5)
                            actual_pnl = _fetch_actual_pnl(exchange, symbol, side, qty, entry, cur_price, _open_ms2)
                            icon       = "✅" if actual_pnl >= 0 else "⚠️"

                            with lock:
                                for t in reversed(state.get("trade_log", [])):
                                    if t.get("symbol") == symbol and t.get("status") == "OPEN":
                                        t.update({
                                            "status":   "CLOSED",
                                            "close":    cur_price,
                                            "pnl_usdt": round(actual_pnl, 2),
                                            "pnl_pct":  round(pnl_pct, 2),
                                        })
                                        break
                                _max_price.pop(symbol, None)
                                _min_price.pop(symbol, None)

                            notifier.telegram.send(
                                f"🛡 <b>SCAN PROTECT (1m fast)</b> — Chốt lời sớm\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"🪙 {symbol} {side} | Lời {pnl_pct:.1f}%\n"
                                f"📍 Entry: ${entry:.6g} → Close: ${cur_price:.6g}\n"
                                f"⚠️ Tín hiệu 1m:\n"
                                + "\n".join([f"  • {s}" for s in sigs_1m]) + "\n"
                                f"{icon} PnL: <b>${actual_pnl:+.2f}</b>\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            )
                            logger.info(f"[ScanProtector] 1m EXIT {symbol} {side} pnl={actual_pnl:+.2f} sigs={sigs_1m}")
                            closed_1m = True

                    if closed_1m:
                        continue

                    # ── NORMAL PATH: check 15m ─────────────────────────────
                    klines = exchange.get_klines(symbol, "15m", limit=30)
                    df = _klines_to_df(klines)
                    if df is None or len(df) < 15:
                        continue

                    rsi_series = calculate_rsi(df["close"], 14)
                    rsi_now    = rsi_series.iloc[-1]
                    rsi_prev   = _prev_rsi.get(symbol, rsi_now)
                    ema9       = calculate_ema(df["close"], 9).iloc[-1]
                    ema21      = calculate_ema(df["close"], 21).iloc[-1]

                    signals = []

                    # 1. RSI đảo chiều trên 15m
                    if side == "LONG":
                        if rsi_prev > 65 and rsi_now < rsi_prev - 5:
                            signals.append(f"RSI15m drop {rsi_prev:.0f}→{rsi_now:.0f}")
                    else:
                        if rsi_prev < 35 and rsi_now > rsi_prev + 5:
                            signals.append(f"RSI15m bounce {rsi_prev:.0f}→{rsi_now:.0f}")

                    # 2. EMA cross ngược chiều
                    if side == "LONG" and ema9 < ema21:
                        signals.append(f"EMA9<EMA21 (bearish cross)")
                    elif side == "SHORT" and ema9 > ema21:
                        signals.append(f"EMA9>EMA21 (bullish cross)")

                    # 3. Pullback mạnh sau khi đã lời tốt
                    if profit_travel >= 1.0 and pullback >= 40:
                        signals.append(f"Pullback {pullback:.0f}% (profit_travel={profit_travel:.1f}%)")

                    _prev_rsi[symbol] = rsi_now

                    # Cần >= 2 tín hiệu
                    if len(signals) < 2:
                        continue

                    # ── Đóng position ──────────────────────────
                    qty        = abs(amt)
                    close_side = "SELL" if side == "LONG" else "BUY"
                    cur_price  = exchange.get_ticker_price(symbol)

                    exchange.place_market_order(symbol, close_side, qty)
                    exchange.cancel_all_orders(symbol)

                    _open_ms3 = 0
                    with lock:
                        for _t3 in reversed(state.get("trade_log", [])):
                            if _t3.get("symbol") == symbol and _t3.get("status") == "OPEN":
                                try:
                                    from datetime import datetime as _dtp4
                                    _open_ms3 = int(_dtp4.strptime(_t3["time"], "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
                                except Exception:
                                    pass
                                break
                    time.sleep(0.5)
                    actual_pnl = _fetch_actual_pnl(exchange, symbol, side, qty, entry, cur_price, _open_ms3)
                    icon       = "✅" if actual_pnl >= 0 else "⚠️"

                    with lock:
                        for t in reversed(state.get("trade_log", [])):
                            if t.get("symbol") == symbol and t.get("status") == "OPEN":
                                t.update({
                                    "status":   "CLOSED",
                                    "close":    cur_price,
                                    "pnl_usdt": round(actual_pnl, 2),
                                    "pnl_pct":  round(pnl_pct, 2),
                                })
                                break
                        _max_price.pop(symbol, None)
                        _min_price.pop(symbol, None)

                    notifier.telegram.send(
                        f"🛡 <b>SCAN PROTECT — Chốt lời sớm</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"🪙 {symbol} {side} | Lời {pnl_pct:.1f}%\n"
                        f"📍 Entry: ${entry:.6g} → Close: ${cur_price:.6g}\n"
                        f"⚠️ Dấu hiệu đảo chiều:\n"
                        + "\n".join([f"  • {s}" for s in signals]) + "\n"
                        f"{icon} PnL: <b>${actual_pnl:+.2f}</b>\n"
                        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                    )
                    logger.info(f"[ScanProtector] EXIT {symbol} {side} pnl={actual_pnl:+.2f} signals={signals}")

                except Exception as e:
                    logger.debug(f"[ScanProtector] {symbol}: {e}")

        except Exception as e:
            logger.error(f"[ScanProtector] Loop error: {e}")

        _time.sleep(15)

# ── Interruptible sleep: wake up sớm khi phát hiện spike giá ──────────────
# Dùng giá WS đã có trong state["prices"] — không tốn API call
# Trả về symbol bị spike (để scan ngay), hoặc None nếu hết thời gian
_spike_price_baseline: dict = {}   # {symbol: (price, timestamp)}

# ============================================================
# THREAD: Auto Profit Lock — chốt lời khi coin bay mạnh mà TP còn xa
# ============================================================
def auto_profit_lock(exchange, notifier):
    """Chốt lời ngay khi coin dump/pump mạnh trong 1-2s + đang lời.
    Dùng mark price WS realtime (_mark) — không dùng klines, không cần đợi nến đóng.
    """
    import time as _t
    _t.sleep(20)

    _prev_price: dict = {}   # {symbol: (price, timestamp)}

    while state["running"]:
        try:
            if not getattr(config, "PROFIT_LOCK_ENABLED", True):
                _t.sleep(2)
                continue

            min_pct   = getattr(config, "PROFIT_LOCK_MIN_PCT",   1.0)
            high_pct  = getattr(config, "PROFIT_LOCK_HIGH_PCT",  5.0)
            speed_pct = getattr(config, "PROFIT_LOCK_SPEED_PCT", 1.0)
            now = _t.time()

            with lock:
                open_positions = list(state.get("open_positions", []))

            for pos in open_positions:
                sym     = pos.get("symbol", "")
                amt     = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue

                entry      = float(pos.get("entryPrice", 0))
                mark_price = pos.get("_mark", 0) or float(pos.get("markPrice", 0))
                pnl_pct    = pos.get("_pct", 0)

                if entry <= 0 or mark_price <= 0:
                    _prev_price[sym] = (mark_price, now)
                    continue

                side = "SHORT" if amt < 0 else "LONG"

                should_lock = False
                reason = ""

                # Lời cao → chốt ngay không cần check tốc độ
                if pnl_pct >= high_pct:
                    should_lock = True
                    reason = f"lời cao {pnl_pct:.1f}% ≥ {high_pct:.1f}%"

                elif pnl_pct >= min_pct:
                    # Tính tốc độ giá thay đổi từ lần check trước (~1s)
                    prev = _prev_price.get(sym)
                    if prev:
                        prev_price, prev_ts = prev
                        elapsed = now - prev_ts
                        if 0 < elapsed <= 5 and prev_price > 0:
                            chg_pct = (mark_price - prev_price) / prev_price * 100
                            # SHORT đang lời = giá đang giảm nhanh (chg âm)
                            if side == "SHORT" and chg_pct <= -speed_pct:
                                should_lock = True
                                reason = f"SHORT: dump {abs(chg_pct):.2f}% trong {elapsed:.1f}s, lời {pnl_pct:.1f}%"
                            # LONG đang lời = giá đang tăng nhanh (chg dương)
                            elif side == "LONG" and chg_pct >= speed_pct:
                                should_lock = True
                                reason = f"LONG: pump {chg_pct:.2f}% trong {elapsed:.1f}s, lời {pnl_pct:.1f}%"

                # Cập nhật price track mỗi vòng
                _prev_price[sym] = (mark_price, now)

                if not should_lock:
                    continue

                # Đóng lệnh
                qty        = abs(amt)
                close_side = "SELL" if side == "LONG" else "BUY"
                cur_price  = mark_price  # dùng mark price WS luôn, nhanh hơn REST

                try:
                    exchange.place_market_order(sym, close_side, qty)
                    exchange.cancel_all_orders(sym)

                    _t.sleep(0.5)
                    pnl = _fetch_actual_pnl(exchange, sym, side, qty, entry, cur_price, 0)

                    with lock:
                        for t in reversed(state.get("trade_log", [])):
                            if t.get("symbol") == sym and t.get("status") == "OPEN":
                                t.update({
                                    "status":   "CLOSED",
                                    "close":    cur_price,
                                    "pnl_usdt": round(pnl, 2),
                                    "pnl_pct":  round(pnl_pct, 2),
                                })
                                break

                    icon = "✅" if pnl >= 0 else "⚠️"
                    notifier.telegram.send(
                        f"🔒 <b>PROFIT LOCK — Chốt lời</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"🪙 {sym} {side}\n"
                        f"📍 {reason}\n"
                        f"💰 Entry: ${entry:.6g} → Close: ${cur_price:.6g}\n"
                        f"{icon} PnL: <b>${pnl:+.2f}</b> ({pnl_pct:.1f}%)\n"
                        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                    )
                    logger.info(f"[ProfitLock] {sym} {side} closed: {reason} pnl=${pnl:+.2f}")
                    # Xóa track để không trigger lại
                    _prev_price.pop(sym, None)

                except Exception as e:
                    logger.error(f"[ProfitLock] close {sym}: {e}")

        except Exception as e:
            logger.debug(f"[ProfitLock] loop error: {e}")

        _t.sleep(1)  # check mỗi 1 giây — bắt dump trong 1-2s


# ============================================================
# THREAD: Trailing Profit Lock — dời SL lên theo lợi nhuận
# Khi lãi >= 3% → dời SL lên breakeven
# Khi giá đi 50% tới TP → lock 40% lợi nhuận
# Khi giá đi 70% tới TP → lock 60% lợi nhuận
# ============================================================
def mfe_scan_monitor(exchange, notifier):
    """
    MFE Retracement cho lệnh scan/quick (không phải pump).
    Khi lời >= 3% và hồi >= 40% từ MFE → đóng lệnh.
    Bật/tắt qua config.MFE_SCAN_ENABLED.
    """
    time.sleep(15)
    _mfe_prices = {}  # {symbol: mfe_price}

    while state["running"]:
        try:
            if not getattr(config, "MFE_SCAN_ENABLED", True):
                time.sleep(10)
                continue

            all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
            open_pos = [p for p in all_pos if abs(float(p.get("positionAmt", 0))) > 0]

            with lock:
                pump_syms = set(state.get("pump_trade_symbols", set()))

            for pos in open_pos:
                sym  = pos["symbol"]
                amt  = float(pos.get("positionAmt", 0))
                if amt == 0:
                    continue

                # Chỉ áp dụng cho lệnh KHÔNG phải pump
                if sym in pump_syms:
                    continue

                entry = float(pos.get("entryPrice", 0))
                mark  = float(pos.get("markPrice", 0)) or state.get("prices", {}).get(sym, 0)
                if entry <= 0 or mark <= 0:
                    continue

                is_long = amt > 0
                if is_long:
                    pnl_pct = (mark - entry) / entry * 100
                else:
                    pnl_pct = (entry - mark) / entry * 100

                # Track MFE — chỉ track từ lúc bot chạy, không init từ klines
                if is_long:
                    mfe = _mfe_prices.get(sym, mark)
                    if mark > mfe:
                        _mfe_prices[sym] = mark
                        mfe = mark
                else:
                    mfe = _mfe_prices.get(sym, mark)
                    if mark < mfe:
                        _mfe_prices[sym] = mark
                        mfe = mark

                # Tính MFE %
                if is_long:
                    mfe_pct = (mfe - entry) / entry * 100
                else:
                    mfe_pct = (entry - mfe) / entry * 100

                # ── Breakeven Exit: Peak Profit Trailing with Reversal Confirmation (Scan) ──
                peak_pct  = getattr(config, "BREAKEVEN_SCAN_PEAK_PCT", 2.0)
                if mfe_pct < peak_pct:
                    # chưa đủ peak → skip breakeven, chỉ MFE retracement mới check
                    pass
                else:
                    min_hold  = getattr(config, "BREAKEVEN_SCAN_HOLD_SECONDS", 300)
                    pnl_floor = getattr(config, "BREAKEVEN_SCAN_PNL_FLOOR", 0.7)
                    confirm_n = getattr(config, "BREAKEVEN_REVERSAL_CONFIRM", 2)
                    be_enabled = getattr(config, "BREAKEVEN_EXIT_ENABLED", True)
                    logger.debug(f"[MFEScan] {sym}: mfe={mfe_pct:.1f}% pnl={pnl_pct:.1f}% be_enabled={be_enabled} peak_ok={mfe_pct>=peak_pct}")

                    _entry_time = None
                    with lock:
                        for t in reversed(state.get("trade_log", [])):
                            if t.get("symbol") == sym and t.get("status") == "OPEN":
                                _entry_time = t.get("time")
                                break
                    _held_secs = 0
                    if _entry_time:
                        try:
                            _held_secs = (datetime.now() - datetime.strptime(_entry_time, "%Y-%m-%d %H:%M:%S")).total_seconds()
                        except Exception:
                            pass
                    else:
                        _held_secs = 9999  # không tìm được → coi như đã hold đủ

                    if getattr(config, "BREAKEVEN_EXIT_ENABLED", True) and _held_secs >= min_hold and mfe_pct >= peak_pct:
                        rev_key = f"_be_rev_{sym}"
                        is_reversing = False
                        if not is_long:
                            is_reversing = mark > mfe * 1.002
                        else:
                            is_reversing = mark < mfe * 0.998

                        with lock:
                            rev_count = state.get(rev_key, 0)
                            if is_reversing:
                                rev_count += 1
                                state[rev_key] = rev_count
                            else:
                                state[rev_key] = 0
                                rev_count = 0

                        if rev_count >= confirm_n and pnl_pct <= pnl_floor:
                            qty = abs(amt)
                            close_side = "SELL" if is_long else "BUY"
                            try:
                                exchange.place_market_order(sym, close_side, qty)
                                exchange.cancel_all_orders(sym)
                                _mfe_prices.pop(sym, None)
                                with lock:
                                    state.pop(rev_key, None)
                                notifier.telegram.send(
                                    f"🔄 <b>📈 PEAK PROFIT EXIT (Scan)</b>: {sym} {'LONG' if is_long else 'SHORT'}\n"
                                    f"Peak {mfe_pct:.1f}% → reversal {rev_count}× → còn {pnl_pct:.1f}% → đóng\n"
                                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                )
                                continue
                            except Exception as e:
                                logger.error(f"[MFEScan] BE close {sym}: {e}")

                # Tính retracement
                retrace_pct = getattr(config, "MFE_RETRACE_PCT", 0.40)
                if is_long:
                    if mfe - entry <= 0:
                        continue
                    retracement = (mfe - mark) / (mfe - entry)
                else:
                    if entry - mfe <= 0:
                        continue
                    retracement = (mark - mfe) / (entry - mfe)

                logger.debug(f"[MFEScan] {sym}: mfe={mfe_pct:.1f}% retrace={retracement*100:.0f}% threshold={retrace_pct*100:.0f}%")

                if retracement >= retrace_pct:
                    qty = abs(amt)
                    close_side = "SELL" if is_long else "BUY"
                    try:
                        exchange.place_market_order(sym, close_side, qty)
                        exchange.cancel_all_orders(sym)
                        _mfe_prices.pop(sym, None)
                        notifier.telegram.send(
                            f"🔄 <b>MFE EXIT (Scan)</b>: {sym} {'LONG' if is_long else 'SHORT'}\n"
                            f"MFE={mfe_pct:.1f}% → hồi {retracement*100:.0f}% → lời {pnl_pct:.1f}%\n"
                            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                        )
                        logger.info(f"[MFEScan] EXIT {sym}: mfe={mfe_pct:.1f}% retrace={retracement*100:.0f}%")
                    except Exception as e:
                        logger.error(f"[MFEScan] Close {sym}: {e}")

            # Dọn symbol không còn position
            open_syms = {p["symbol"] for p in open_pos if abs(float(p.get("positionAmt", 0))) > 0}
            for sym in list(_mfe_prices.keys()):
                if sym not in open_syms:
                    _mfe_prices.pop(sym, None)

        except Exception as e:
            logger.error(f"[MFEScan] Error: {e}", exc_info=True)

        time.sleep(5)


def trailing_profit_lock(exchange, notifier):
    """Dời SL lên theo lợi nhuận để không bị quay lại lỗ."""
    time.sleep(30)  # chờ bot khởi động

    while state["running"]:
        try:
            if not getattr(config, "TRAILING_LOCK_ENABLED", True):
                time.sleep(10)
                continue

            # Lấy positions đang mở
            all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
            open_pos = [p for p in all_pos if abs(float(p.get("positionAmt", 0))) > 0]

            for pos in open_pos:
                sym = pos["symbol"]
                amt = float(pos.get("positionAmt", 0))
                entry = float(pos.get("entryPrice", 0))
                mark = float(pos.get("markPrice", 0))

                if entry <= 0 or mark <= 0:
                    continue

                is_long = amt > 0
                qty = abs(amt)

                # Tính % lợi nhuận hiện tại
                if is_long:
                    pnl_pct = (mark - entry) / entry * 100
                else:
                    pnl_pct = (entry - mark) / entry * 100

                # Chưa lãi đủ → skip
                min_pct = getattr(config, "PROFIT_LOCK_MIN_PCT", 3.0)
                if pnl_pct < min_pct:
                    continue

                # Lấy SL hiện tại
                all_orders = exchange._get("/fapi/v1/openOrders",
                                          {"symbol": sym}, signed=True)
                sl_orders = [o for o in all_orders
                             if o.get("type") in ("STOP_MARKET", "STOP")
                             and o.get("reduceOnly", False)]
                if not sl_orders:
                    continue

                current_sl = float(sl_orders[0].get("stopPrice", 0))
                if current_sl <= 0:
                    continue

                # Tính new SL dựa trên % progress tới TP
                # Lấy TP order
                tp_orders = [o for o in all_orders
                             if o.get("type") in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT")
                             and o.get("reduceOnly", False)]
                tp_price = float(tp_orders[0].get("stopPrice", 0)) if tp_orders else 0

                # ── 3 State Trailing SL theo pnl_pct ──
                # State 1: lời >= 3% → dời SL về entry + buffer 0.3%
                # State 2: lời >= 6% → dời SL về entry + 2%
                # State 3: lời >= 10% → trailing SL cách giá 3%
                if pnl_pct >= 10.0:
                    # Trailing cách giá 3%
                    if is_long:
                        new_sl = round(mark * (1 - 0.03), 8)
                    else:
                        new_sl = round(mark * (1 + 0.03), 8)
                    lock_pct = 0.70   # lock ~70% lợi nhuận
                elif pnl_pct >= 6.0:
                    # SL về entry + 2%
                    if is_long:
                        new_sl = round(entry * 1.02, 8)
                    else:
                        new_sl = round(entry * 0.98, 8)
                    lock_pct = 0.33   # lock ~33%
                elif pnl_pct >= 3.0:
                    # SL về entry + buffer 0.3%
                    if is_long:
                        new_sl = round(entry * 1.003, 8)
                    else:
                        new_sl = round(entry * 0.997, 8)
                    lock_pct = 0.10   # lock ~10%
                else:
                    continue

                # Check: new_sl có tốt hơn current_sl không?
                if is_long and new_sl <= current_sl:
                    continue
                if not is_long and new_sl >= current_sl:
                    continue

                # Dời SL
                try:
                    # Cancel SL cũ
                    for o in sl_orders:
                        exchange._delete("/fapi/v1/order",
                                        {"symbol": sym, "orderId": o["orderId"]})

                    # Đặt SL mới
                    close_side = "SELL" if is_long else "BUY"
                    exchange.place_stop_loss_order(sym, close_side, qty, new_sl)

                    logger.info(
                        f"[TrailingLock] {sym} {'LONG' if is_long else 'SHORT'} | "
                        f"SL {current_sl:.6f} → {new_sl:.6f} | "
                        f"PnL={pnl_pct:.1f}% | lock={lock_pct*100:.0f}%"
                    )
                    notifier.telegram.send(
                        f"🔒 <b>PROFIT LOCK</b> {sym}\n"
                        f"SL dời: {current_sl:.6f} → <b>{new_sl:.6f}</b>\n"
                        f"💰 Lock {lock_pct*100:.0f}% lợi nhuận | PnL: +{pnl_pct:.1f}%"
                    )
                except Exception as e:
                    logger.error(f"[TrailingLock] Move SL {sym}: {e}")

        except Exception as e:
            logger.debug(f"[TrailingLock] Error: {e}")

        time.sleep(getattr(config, "PROFIT_LOCK_INTERVAL", 5))


def _wait_or_spike(total_seconds: float, check_interval: float = 2,
                   spike_pct: float = 3.0, dump_pct: float = 2.5):
    """
    Sleep tối đa total_seconds giây, nhưng wake up sớm nếu:
    - Có coin trong FIXED_COINS pump  >= spike_pct% so với baseline  -> SHORT alert
    - Có coin trong FIXED_COINS dump  >= dump_pct% so với baseline   -> LONG alert

    Baseline reset mỗi đầu chu kỳ scan.
    Trả về symbol bị spike/dump, hoặc None nếu hết giờ bình thường.
    """
    global _spike_price_baseline

    with lock:
        current_prices = dict(state.get("prices", {}))
        fixed_coins    = list(getattr(config, "FIXED_COINS", []))
        open_syms = {p["symbol"] for p in state.get("open_positions", [])
                     if abs(float(p.get("positionAmt", 0))) > 0}

    _spike_price_baseline = {
        sym: current_prices[sym]
        for sym in fixed_coins
        if sym in current_prices and sym not in open_syms
    }

    elapsed = 0.0
    while elapsed < total_seconds:
        time.sleep(check_interval)
        elapsed += check_interval

        with lock:
            latest_prices = dict(state.get("prices", {}))
            open_syms_now = {p["symbol"] for p in state.get("open_positions", [])
                             if abs(float(p.get("positionAmt", 0))) > 0}

        for sym, base_price in _spike_price_baseline.items():
            if sym in open_syms_now:
                continue
            cur = latest_prices.get(sym, 0)
            if base_price <= 0 or cur <= 0:
                continue
            chg_pct = (cur - base_price) / base_price * 100
            if chg_pct >= spike_pct:
                logger.info(f"[SpikeDet] {sym} pump +{chg_pct:.1f}% in {elapsed:.0f}s — wake up scan")
                return sym
            if chg_pct <= -dump_pct:
                logger.info(f"[SpikeDet] {sym} dump {chg_pct:.1f}% in {elapsed:.0f}s — wake up scan")
                return sym

    return None


def _fast_spike_scan(symbol: str, exchange, notifier) -> None:
    """
    Fast scan 1 coin khi spike detector wake up scan_engine sớm.
    - Pump >= 3%  → check SHORT (dùng pump_detector)
    - Dump >= 2.5% → check LONG (RSI oversold + volume spike + reversal candle)
    Chỉ tốn 2 API call klines — an toàn rate limit.
    """
    try:
        from pump_detector import PumpDetector, _to_df
        from indicators import calculate_rsi, calculate_atr, calculate_ema

        klines_1m  = exchange.get_klines(symbol, "1m",  limit=60)
        klines_15m = exchange.get_klines(symbol, "15m", limit=50)
        df_1m      = _to_df(klines_1m)
        df_15m     = _to_df(klines_15m)

        cur_price  = df_1m["close"].iloc[-1]
        base_price = _spike_price_baseline.get(symbol, cur_price)
        chg_pct    = (cur_price - base_price) / base_price * 100 if base_price > 0 else 0

        with lock:
            open_syms = {p["symbol"] for p in state.get("open_positions", [])
                         if abs(float(p.get("positionAmt", 0))) > 0}
        if symbol in open_syms:
            return

        # ── SHORT path: coin đang pump ──────────────────────────
        if chg_pct >= 3.0:
            detector = PumpDetector(config)
            sig = detector.analyze(symbol, df_1m, df_15m)
            if sig and sig.is_pump_top:
                logger.info(f"[FastSpike] SHORT {symbol} pump={chg_pct:.1f}% score={sig.score}")
                _execute_spike_short(symbol, sig, exchange, notifier)
            else:
                score = sig.score if sig else 0
                logger.info(f"[FastSpike] {symbol} pump +{chg_pct:.1f}% score={score} chua du")

        # ── LONG path: coin đang dump mạnh ──────────────────────
        elif chg_pct <= -2.5:
            rsi = calculate_rsi(df_1m["close"], 14).iloc[-1]
            vol = df_1m["volume"]
            vol_ma = vol.rolling(20).mean().iloc[-1]
            vol_ratio = vol.iloc[-1] / vol_ma if vol_ma > 0 else 1.0

            last = df_1m.iloc[-1]
            rng  = last["high"] - last["low"]
            lower_wick = min(last["close"], last["open"]) - last["low"]
            lower_wick_ratio = lower_wick / rng if rng > 0 else 0
            reversal_candle = (
                last["close"] > last["open"]
                or lower_wick_ratio >= 0.4
            )

            long_score = 0
            reasons = []
            if rsi <= 35:
                long_score += 40
                reasons.append(f"RSI={rsi:.0f} oversold")
            elif rsi <= 42:
                long_score += 20
                reasons.append(f"RSI={rsi:.0f} near oversold")
            if vol_ratio >= 2.0:
                long_score += 30
                reasons.append(f"Vol {vol_ratio:.1f}x spike")
            elif vol_ratio >= 1.5:
                long_score += 15
                reasons.append(f"Vol {vol_ratio:.1f}x")
            if reversal_candle:
                long_score += 20
                reasons.append("Reversal candle")
            if abs(chg_pct) >= 5.0:
                long_score += 10
                reasons.append(f"Dump {chg_pct:.1f}%")

            logger.info(f"[FastSpike] LONG check {symbol} dump={chg_pct:.1f}% RSI={rsi:.0f} vol={vol_ratio:.1f}x score={long_score}")

            if long_score >= 60:
                atr = calculate_atr(df_1m["high"], df_1m["low"], df_1m["close"], 14).iloc[-1]
                sl  = round(cur_price - atr * 2.0, 8)
                tp  = round(cur_price + atr * 4.0, 8)
                rr  = (tp - cur_price) / (cur_price - sl) if (cur_price - sl) > 0 else 0
                if rr >= 1.5:
                    _execute_spike_long(symbol, cur_price, sl, tp, long_score, reasons, exchange, notifier)
                else:
                    logger.info(f"[FastSpike] {symbol} LONG RR={rr:.1f} < 1.5, skip")
            else:
                logger.info(f"[FastSpike] {symbol} dump {chg_pct:.1f}% long_score={long_score} chua du 60")

    except Exception as e:
        logger.error(f"[FastSpike] {symbol} error: {e}")


def _execute_spike_short(symbol: str, sig, exchange, notifier) -> None:
    """Vào SHORT nhanh cho coin spike."""
    try:
        exchange.set_leverage(symbol, config.LEVERAGE)
        cur_price = exchange.get_ticker_price(symbol)
        if not cur_price or cur_price <= 0:
            cur_price = sig.entry_price

        from qty_utils import calc_qty_precise
        qty, _ = calc_qty_precise(exchange, symbol, config.MAX_ORDER_USDT, config.LEVERAGE, cur_price)
        if qty * cur_price < 5.0:
            return

        exchange.place_market_order(symbol, "SELL", qty)
        time.sleep(0.8)

        sl_ok = False
        for _a in range(3):
            try:
                exchange.place_stop_loss_order(symbol, "BUY", qty, sig.sl_price)
                sl_ok = True
                break
            except Exception:
                time.sleep(0.5)
        if not sl_ok:
            logger.warning(f"[PumpShort] SL failed for {symbol} — keeping position, auto_sltp will retry")

        with lock:
            state["trade_log"].append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol, "side": "SHORT",
                "entry": cur_price, "sl": sig.sl_price, "tp": sig.tp1_price,
                "qty": qty, "status": "OPEN", "note": f"spike_short_s{sig.score}",
            })
            state.setdefault("pump_trade_symbols", set()).add(symbol)

        notifier.telegram.send(
            f"⚡ <b>SPIKE SHORT — Fast Entry</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {symbol}  📈 +{sig.pump_pct:.1f}%  Score {sig.score}/100\n"
            f"💰 Entry : <b>${cur_price:,.6g}</b>  [MARKET]\n"
            f"🛑 SL    : <b>${sig.sl_price:,.6g}</b>\n"
            f"🎯 TP    : <b>${sig.tp1_price:,.6g}</b>\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        logger.info(f"[FastSpike] SHORT placed: {symbol} @ {cur_price}")
    except Exception as e:
        logger.error(f"[FastSpike] SHORT execute {symbol}: {e}")


def _execute_spike_long(symbol: str, cur_price: float, sl: float, tp: float,
                        score: int, reasons: list, exchange, notifier) -> None:
    """Vào LONG nhanh khi coin dump mạnh và đủ điều kiện bắt đáy."""
    try:
        exchange.set_leverage(symbol, config.LEVERAGE)

        from qty_utils import calc_qty_precise
        qty, _ = calc_qty_precise(exchange, symbol, config.MAX_ORDER_USDT, config.LEVERAGE, cur_price)
        if qty * cur_price < 5.0:
            return

        with lock:
            open_syms = {p["symbol"] for p in state.get("open_positions", [])
                         if abs(float(p.get("positionAmt", 0))) > 0}
        if symbol in open_syms:
            return

        exchange.place_market_order(symbol, "BUY", qty)
        time.sleep(0.8)

        sl_ok = False
        for _a in range(3):
            try:
                exchange.place_stop_loss_order(symbol, "SELL", qty, sl)
                sl_ok = True
                break
            except Exception:
                time.sleep(0.5)
        if not sl_ok:
            logger.warning(f"[SpikeShort] SL failed for {symbol} — keeping position")

        try:
            exchange.place_take_profit_order(symbol, "SELL", qty, tp)
        except Exception:
            pass

        rr = (tp - cur_price) / (cur_price - sl) if (cur_price - sl) > 0 else 0
        with lock:
            state["trade_log"].append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol, "side": "LONG",
                "entry": cur_price, "sl": sl, "tp": tp,
                "qty": qty, "status": "OPEN", "note": f"spike_long_s{score}",
            })

        notifier.telegram.send(
            f"⚡ <b>SPIKE LONG — Bat Day Fast</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {symbol}  Score {score}/100\n"
            f"📉 Dump manh — {' | '.join(reasons[:3])}\n"
            f"💰 Entry : <b>${cur_price:,.6g}</b>  [MARKET]\n"
            f"🛑 SL    : <b>${sl:,.6g}</b>\n"
            f"🎯 TP    : <b>${tp:,.6g}</b>\n"
            f"📐 RR    : 1:{rr:.1f}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        logger.info(f"[FastSpike] LONG placed: {symbol} @ {cur_price} SL={sl} TP={tp}")
    except Exception as e:
        logger.error(f"[FastSpike] LONG execute {symbol}: {e}")


# ============================================================
# LIQ SWEEP REVERSAL — vào lệnh khi giá quét hết liq 1 phía rồi đảo chiều
# Bypass trend filter: không cần EMA/MTF align, chỉ cần liq + price action
# ============================================================
def _liq_sweep_reversal_scan(exchange, config):
    """
    Liq Pre-Position: đặt LIMIT order SẴN ở vùng liq, chờ giá tới khớp.
    Không chờ bounce/reject → không bị trễ.
    
    LONG: tìm cluster liq DƯỚI giá → đặt LIMIT BUY tại đó
      - Giá đang giảm hoặc gần cluster (≤8%)
      - Cluster lớn (USD) = xác suất bounce cao
      
    SHORT: tìm cluster liq TRÊN giá → đặt LIMIT SELL tại đó
      - Giá đang tăng hoặc gần cluster (≤8%)
      - Cluster lớn (USD) = xác suất reject cao
    
    Returns: CoinScore hoặc None
    """
    from scanner import CoinScore, _klines_to_df
    from indicators import calculate_rsi, calculate_atr

    liq_inst = state.get("liq_tracker")
    liq_api  = state.get("liq_api_cache")

    # Chọn liq source
    liq_source = None
    if liq_inst and liq_inst.is_connected():
        liq_source = liq_inst
    elif liq_api:
        liq_source = liq_api

    if not liq_source:
        return None

    # Quét FIXED_COINS (hoặc active universe)
    try:
        import config as _cfg
        coins = list(getattr(_cfg, "FIXED_COINS", []))
        if not coins:
            from scanner import get_active_universe
            base_url = getattr(config, "LIVE_BASE_URL", "https://demo-fapi.binance.com")
            coins = get_active_universe(base_url, top_n=10)
    except Exception:
        return None

    # Skip coin đã có position hoặc pending order
    with lock:
        open_syms = {p["symbol"] for p in state.get("open_positions", [])
                     if abs(float(p.get("positionAmt", 0))) > 0}
    try:
        pending_orders = exchange._get("/fapi/v1/openOrders", signed=True)
        pending_syms = {o["symbol"] for o in pending_orders if not o.get("reduceOnly", False)}
    except Exception:
        pending_syms = set()

    best_candidate = None

    for symbol in coins:
        if symbol in open_syms or symbol in pending_syms:
            continue
        if not liq_source.is_ready(symbol) if hasattr(liq_source, 'is_ready') else liq_source.total_liq_usd(symbol) <= 0:
            continue

        try:
            cur_price = exchange.get_ticker_price(symbol)
            if cur_price <= 0:
                continue

            # Lấy klines 15m
            klines_15m = exchange.get_klines(symbol, "15m", limit=30)
            df = _klines_to_df(klines_15m)
            close = df["close"]
            high = df["high"]
            low = df["low"]

            rsi = calculate_rsi(close, 14).iloc[-1]
            atr = calculate_atr(high, low, close).iloc[-1]
            atr_pct = (atr / cur_price) * 100

            # ── LONG: tìm cluster liq DƯỚI → đặt LIMIT BUY đón sẵn ──
            cluster_below = liq_source.get_best_entry_cluster(
                symbol=symbol, current_price=cur_price,
                direction="LONG", min_usd=30_000, cluster_gap_pct=0.008
            )
            if not cluster_below:
                cluster_below = liq_source.get_best_entry_cluster(
                    symbol=symbol, current_price=cur_price,
                    direction="LONG", min_usd=10_000, cluster_gap_pct=0.012
                )

            if cluster_below and cluster_below["dist_pct"] <= 8.0:
                # Giá đang giảm (đi về cluster) HOẶC gần cluster ≤ 2%
                price_3ago = close.iloc[-4] if len(close) >= 4 else close.iloc[0]
                going_down = cur_price < price_3ago
                near = cluster_below["dist_pct"] <= 2.0

                if going_down or near:
                    # Score dựa trên: cluster size + khoảng cách + RSI
                    score = 68.0
                    if cluster_below["total_usd"] >= 50_000:
                        score += 5
                    if cluster_below["dist_pct"] <= 2.0:
                        score += 5
                    if rsi < 35:
                        score += 7
                    elif rsi < 45:
                        score += 3

                    candidate = CoinScore(
                        symbol=symbol,
                        signal="LONG",
                        score=round(min(score, 90), 1),
                        rsi=round(rsi, 1),
                        trend="LIQ_PREPOSITION",
                        atr_pct=round(atr_pct, 2),
                        reason=(f"📍LIQ PRE-LONG | cluster ${cluster_below['total_usd']/1e3:.0f}k "
                                f"dist={cluster_below['dist_pct']:.1f}% | "
                                f"{'↘giá đang giảm' if going_down else '⚡gần cluster'} | RSI={rsi:.0f}")
                    )
                    if not best_candidate or candidate.score > best_candidate.score:
                        best_candidate = candidate
                    continue

            # ── SHORT: tìm cluster liq TRÊN → đặt LIMIT SELL đón sẵn ──
            cluster_above = liq_source.get_best_entry_cluster(
                symbol=symbol, current_price=cur_price,
                direction="SHORT", min_usd=30_000, cluster_gap_pct=0.008
            )
            if not cluster_above:
                cluster_above = liq_source.get_best_entry_cluster(
                    symbol=symbol, current_price=cur_price,
                    direction="SHORT", min_usd=10_000, cluster_gap_pct=0.012
                )

            if cluster_above and cluster_above["dist_pct"] <= 8.0:
                # Giá đang tăng (đi về cluster) HOẶC gần cluster ≤ 2%
                price_3ago = close.iloc[-4] if len(close) >= 4 else close.iloc[0]
                going_up = cur_price > price_3ago
                near = cluster_above["dist_pct"] <= 2.0

                if going_up or near:
                    score = 70.0
                    if cluster_above["total_usd"] >= 50_000:
                        score += 5
                    if cluster_above["dist_pct"] <= 2.0:
                        score += 5
                    if rsi > 65:
                        score += 7
                    elif rsi > 55:
                        score += 3

                    candidate = CoinScore(
                        symbol=symbol,
                        signal="SHORT",
                        score=round(min(score, 90), 1),
                        rsi=round(rsi, 1),
                        trend="LIQ_PREPOSITION",
                        atr_pct=round(atr_pct, 2),
                        reason=(f"📍LIQ PRE-SHORT | cluster ${cluster_above['total_usd']/1e3:.0f}k "
                                f"dist={cluster_above['dist_pct']:.1f}% | "
                                f"{'↗giá đang tăng' if going_up else '⚡gần cluster'} | RSI={rsi:.0f}")
                    )
                    if not best_candidate or candidate.score > best_candidate.score:
                        best_candidate = candidate

        except Exception as e:
            logger.debug(f"[LiqPrePos] {symbol}: {e}")
            continue

    if best_candidate:
        logger.info(f"📍 [LiqPrePosition] {best_candidate.symbol} {best_candidate.signal} "
                    f"score={best_candidate.score} | {best_candidate.reason}")

    return best_candidate


def scan_engine(exchange, notifier):
    _spike_symbol = None   # coin được wake up sớm bởi spike detector

    # Lấy ScanPriceMonitor (đã được khởi tạo trong price_ws_streamer)
    from ws_price_monitor import get_scan_monitor
    _scan_monitor = get_scan_monitor()

    while state["running"]:
        try:
            with lock:
                last_loss_time = state.get("last_loss_time", 0)
            cooldown = getattr(config, "COOLDOWN_AFTER_LOSS", 180)
            if time.time() - last_loss_time < cooldown:
                wait = int(cooldown - (time.time() - last_loss_time))
                logger.info(f"Cooldown sau lỗ: còn {wait}s")
                _scan_monitor.wait_for_signal(timeout=min(wait, config.LOOP_INTERVAL_SECONDS))
                continue

            with lock:
                n_open = len(state.get("open_positions", []))
            if n_open >= config.MAX_OPEN_POSITIONS:
                logger.info(f"Max positions ({n_open}/{config.MAX_OPEN_POSITIONS}), skip scan")
                _scan_monitor.wait_for_signal(timeout=config.LOOP_INTERVAL_SECONDS)
                continue

            with lock:
                state["scan_no"] += 1
                state["last_scan"] = datetime.now().strftime("%H:%M")

            # ── PROMOTE: đẩy armed lên LIMIT khi slot trống ──
            with lock:
                armed = state.get("armed_entries", {})
            if armed:
                try:
                    all_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                    limit_count = len([o for o in all_orders if not o.get("reduceOnly", False) and o.get("type") == "LIMIT"])
                    if limit_count < 2:
                        # Có slot trống → đẩy armed lên LIMIT
                        for a_sym, a_info in list(armed.items()):
                            if limit_count >= 2:
                                break
                            try:
                                # Kiểm tra entry còn hợp lệ với mark price (85%-115%)
                                cur_mark = exchange.get_ticker_price(a_sym)
                                entry_p  = a_info["entry_price"]
                                if cur_mark > 0:
                                    ratio = entry_p / cur_mark
                                    if ratio < 0.85 or ratio > 1.15:
                                        with lock:
                                            state.get("armed_entries", {}).pop(a_sym, None)
                                        logger.info(f"[Armed] ❌ Remove {a_sym}: entry {entry_p:.6f} xa mark {cur_mark:.6f} ({ratio*100:.0f}%)")
                                        continue
                                exchange.set_leverage(a_sym, config.LEVERAGE)
                                bal = exchange.get_total_equity()
                                # Apply offset tại đây khi Promote — dùng raw_entry đã lưu
                                raw_ep = a_info.get("raw_entry", a_info["entry_price"])
                                if getattr(config, "ENTRY_OFFSET_ENABLED", False) and raw_ep > 0:
                                    off_pct = getattr(config, "ENTRY_OFFSET_PCT", 0.003)
                                    if a_info["signal"] == "LONG":
                                        final_entry = round(raw_ep * (1 - off_pct), 8)
                                    else:
                                        final_entry = round(raw_ep * (1 + off_pct), 8)
                                    logger.info(f"[Promote] {a_sym} apply offset: {raw_ep:.6f} → {final_entry:.6f} ({off_pct*100:.1f}%)")
                                else:
                                    final_entry = a_info["entry_price"]
                                qty = calc_qty(bal, final_entry, a_info["sl"], symbol=a_sym, exchange=exchange)
                                if qty * final_entry < 5.0:
                                    qty = round(5.0 / final_entry + 0.001, 3)
                                exchange.place_limit_order(a_sym, a_info["side"], qty, final_entry)
                                try:
                                    ords = exchange._get("/fapi/v1/openOrders", {"symbol": a_sym}, signed=True)
                                    for o in ords:
                                        if not o.get("reduceOnly") and o.get("type") == "LIMIT" and o.get("symbol") == a_sym:
                                            with lock:
                                                psm = state.setdefault("pending_smart_orders", {})
                                                psm[str(o["orderId"])] = {
                                                    "symbol": a_sym, "side": a_info["signal"],
                                                    "qty": float(o.get("origQty", qty)),
                                                    "sl": a_info["sl"], "tp": a_info["tp"],
                                                    "ts": time.time(),
                                                }
                                except Exception:
                                    pass
                                with lock:
                                    state.get("armed_entries", {}).pop(a_sym, None)
                                limit_count += 1
                                logger.info(f"[Promote] Armed→LIMIT: {a_sym} {a_info['signal']} @ {final_entry:.6f}")
                            except Exception as _e:
                                logger.debug(f"[Promote] {a_sym} failed: {_e}")
                except Exception:
                    pass

            # ── FAST CHECK: armed entries — giá tới zone chưa? ──
            with lock:
                armed = dict(state.get("armed_entries", {}))
            if armed:
                for a_sym, a_info in list(armed.items()):
                    try:
                        # Expiry 1 giờ
                        if time.time() - a_info["ts"] > 3600:
                            with lock:
                                state.get("armed_entries", {}).pop(a_sym, None)
                            logger.info(f"[Armed] ⏰ EXPIRED {a_sym} (>15min)")
                            continue

                        # Skip nếu đã có position
                        with lock:
                            open_syms = {p["symbol"] for p in state.get("open_positions", [])
                                         if abs(float(p.get("positionAmt", 0))) > 0}
                            n_open = len(state.get("open_positions", []))
                        if a_sym in open_syms or n_open >= config.MAX_OPEN_POSITIONS:
                            with lock:
                                state.get("armed_entries", {}).pop(a_sym, None)
                            continue

                        # Check giá
                        cur_p = exchange.get_ticker_price(a_sym)
                        entry_p = a_info["entry_price"]

                        # LONG: giá <= entry (dump xuống vùng liq)
                        # SHORT: giá >= entry (pump lên vùng liq)
                        hit = False
                        if a_info["signal"] == "LONG" and cur_p <= entry_p:
                            hit = True
                        elif a_info["signal"] == "SHORT" and cur_p >= entry_p:
                            hit = True

                        if hit:
                            # MARKET ORDER ngay
                            try:
                                exchange.set_leverage(a_sym, config.LEVERAGE)
                            except Exception:
                                pass
                            bal = exchange.get_total_equity()
                            qty = calc_qty(bal, cur_p, a_info["sl"], symbol=a_sym, exchange=exchange)
                            if qty * cur_p < 5.0:
                                qty = round(5.0 / cur_p + 0.001, 3)

                            exchange.place_market_order(a_sym, a_info["side"], qty)
                            time.sleep(0.5)

                            # SL
                            sl_ok = False
                            for _try in range(3):
                                try:
                                    exchange.place_stop_loss_order(a_sym, a_info["close_side"], qty, a_info["sl"])
                                    sl_ok = True
                                    break
                                except Exception:
                                    time.sleep(0.3)
                            if not sl_ok:
                                logger.debug(f"[Armed] SL FAILED {a_sym} — keeping position, auto_sltp will retry")

                            # TP
                            try:
                                exchange.place_take_profit_order(a_sym, a_info["close_side"], qty, a_info["tp"])
                            except Exception:
                                pass

                            with lock:
                                state.get("armed_entries", {}).pop(a_sym, None)
                                state["trade_log"].append({
                                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "symbol": a_sym, "side": a_info["signal"],
                                    "entry": cur_p, "sl": a_info["sl"], "tp": a_info["tp"],
                                    "qty": qty, "status": "OPEN", "note": "armed_liq_entry"
                                })

                            notifier.telegram.send(
                                f"{'🟢' if a_info['signal']=='LONG' else '🔴'} <b>ARMED TRIGGERED</b>: {a_sym} {a_info['signal']}\n"
                                f"💰 Entry: {cur_p:.6f} | SL: {a_info['sl']:.6f} | TP: {a_info['tp']:.6f}\n"
                                f"📐 RR: 1:{a_info['rr']:.1f}\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            )
                            logger.info(f"[Armed] ✅ TRIGGERED {a_sym} {a_info['signal']} @ {cur_p:.6f}")
                            break  # 1 lệnh/loop

                    except Exception as _e:
                        logger.debug(f"[Armed] {a_sym}: {_e}")

            # ── Fast path: đã được thay bằng ScanPriceMonitor ở cuối vòng lặp ──
            # (wake up sớm qua ws_signal ở dưới)

            # ── MSS PENDING FAST-CHECK — xem setup nào đã confirm MSS ──────────
            # Chạy mỗi vòng scan_engine (~60s), check tất cả MSS pending
            # Nếu tier C → tier A/B đã upgrade → promote lên candidate
            try:
                from mss_engine import get_mss_pending, analyze_mss
                from scanner import _klines_to_df
                mss_mgr = get_mss_pending()
                mss_mgr.expire_old(max_age_minutes=getattr(config, "MSS_MAX_SETUP_AGE_MIN", 30))

                for mss_sym, mss_info in list(mss_mgr.all_pending().items()):
                    try:
                        direction = mss_info["direction"]
                        # Check không có open position
                        with lock:
                            open_syms_mss = {p["symbol"] for p in state.get("open_positions", [])
                                            if abs(float(p.get("positionAmt", 0))) > 0}
                        if mss_sym in open_syms_mss:
                            mss_mgr.remove(mss_sym)
                            continue

                        # Re-analyze với data mới nhất
                        kl_15m = exchange.get_klines(mss_sym, "15m", limit=100)
                        df_15m = _klines_to_df(kl_15m)
                        df_5m  = None
                        if getattr(config, "MSS_USE_5M_CONFIRM", True):
                            try:
                                kl_5m = exchange.get_klines(mss_sym, "5m", limit=20)
                                df_5m = _klines_to_df(kl_5m)
                            except Exception:
                                pass

                        new_result = analyze_mss(df_15m, df_5m, direction, config)

                        if new_result.tier in ("A", "B"):
                            # MSS đã confirm → tạo armed entry ngay
                            entry_p = new_result.entry_price
                            sl_p    = new_result.sl_price
                            cur_p   = exchange.get_ticker_price(mss_sym)

                            if entry_p <= 0 or sl_p <= 0:
                                continue

                            # Tính TP từ structure
                            from scanner import calc_structure_sl_tp
                            sltp = calc_structure_sl_tp(df_15m, direction, entry_p, config)
                            tp_p = sltp["tp"]
                            rr   = sltp["rr"]

                            min_rr = getattr(config, "MIN_RR", 1.5)
                            if rr < min_rr:
                                logger.info(f"[MSS] {mss_sym} tier={new_result.tier} RR={rr:.2f} < {min_rr} → skip")
                                mss_mgr.remove(mss_sym)
                                continue

                            side       = "BUY"  if direction == "LONG" else "SELL"
                            close_side = "SELL" if direction == "LONG" else "BUY"

                            with lock:
                                n_open = len(state.get("open_positions", []))
                                # Không tạo armed nếu coin đã có open position
                                open_syms_mss = {p["symbol"] for p in state.get("open_positions", [])
                                                 if abs(float(p.get("positionAmt", 0))) > 0}
                            if n_open >= config.MAX_OPEN_POSITIONS:
                                continue
                            if mss_sym in open_syms_mss:
                                logger.info(f"[MSS] {mss_sym} đã có position → skip armed")
                                mss_mgr.remove(mss_sym)
                                continue

                            with lock:
                                armed_mss = state.setdefault("armed_entries", {})
                                armed_mss[mss_sym] = {
                                    "signal":      direction,
                                    "entry_price": entry_p,
                                    "sl":          sl_p,
                                    "tp":          tp_p,
                                    "rr":          rr,
                                    "score":       80 if new_result.tier == "A" else 70,
                                    "side":        side,
                                    "close_side":  close_side,
                                    "ts":          time.time(),
                                    "note":        f"mss_{new_result.tier}",
                                }
                            mss_mgr.remove(mss_sym)
                            logger.info(f"[MSS] ✅ PROMOTED {mss_sym} {direction} tier={new_result.tier} "
                                        f"entry={entry_p:.6f} sl={sl_p:.6f} RR={rr:.2f}")
                            # Không gửi telegram khi vào ARMED — tránh spam
                            # notifier.telegram.send(...)

                        elif new_result.tier == "D":
                            # Structure invalidated → remove pending
                            mss_mgr.remove(mss_sym)
                            logger.info(f"[MSS] ❌ INVALIDATED {mss_sym} → remove pending")

                        # tier C → giữ nguyên, check lại lần sau

                    except Exception as _mss_e:
                        logger.debug(f"[MSS] fast-check {mss_sym}: {_mss_e}")

            except Exception as _mss_outer:
                logger.debug(f"[MSS] fast-check outer: {_mss_outer}")

            best = scan_market(exchange, config, min_score=config.MIN_SCORE, notifier=notifier)
            with lock:
                state["candidates"] = list(getattr(scan_market, "_last_candidates", []))

            # ── Liq Sweep Reversal — vào lệnh counter-trend khi liq đã bị quét ──
            # Bypass trend filter: khi giá sweep hết liq 1 phía → đảo chiều
            # LONG: giá dump chạm cluster liq dưới + bounce (wick rejection)
            # SHORT: giá pump chạm cluster liq trên + reject (wick rejection)
            if not best:
                best = _liq_sweep_reversal_scan(exchange, config)

            if best:
                with lock:
                    open_syms = {p["symbol"] for p in state.get("open_positions", [])
                                 if abs(float(p.get("positionAmt", 0))) > 0}
                if best.symbol in open_syms:
                    logger.info(f"Skip {best.symbol}: already has open position")
                    _scan_monitor.wait_for_signal(timeout=config.LOOP_INTERVAL_SECONDS)
                    continue
                # Guard double entry: nếu đang xử lý symbol này → skip
                if best.symbol in _executing_symbols:
                    logger.info(f"Skip {best.symbol}: đang trong quá trình đặt lệnh")
                    _scan_monitor.wait_for_signal(timeout=config.LOOP_INTERVAL_SECONDS)
                    continue
                _executing_symbols.add(best.symbol)
                try:
                    pending_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                    pending_syms = {o["symbol"] for o in pending_orders if not o.get("reduceOnly", False)}
                    pending_entry_count = len([o for o in pending_orders if not o.get("reduceOnly", False)])
                    if best.symbol in pending_syms:
                        logger.info(f"Skip {best.symbol}: already has pending order")
                        _scan_monitor.wait_for_signal(timeout=config.LOOP_INTERVAL_SECONDS)
                        continue
                    # Max 2 pending LIMIT entry orders cùng lúc — vẫn chạy xuống để lưu armed
                    if pending_entry_count >= 2:
                        logger.info(f"Pending đầy ({pending_entry_count}/2) → lưu armed thay vì LIMIT")
                except Exception:
                    pass

                klines = exchange.get_klines(best.symbol, config.INTERVAL, limit=200)
                df     = _klines_to_df(klines)
                price  = df["close"].iloc[-1]
                atr    = calculate_atr(df["high"], df["low"], df["close"]).iloc[-1]
                bal    = exchange.get_total_equity()   # dùng equity gốc, không phải available
                try: exchange.set_leverage(best.symbol, config.LEVERAGE)
                except: pass

                liq_inst   = state.get("liq_tracker")
                side       = "BUY"  if best.signal == "LONG" else "SELL"
                close_side = "SELL" if best.signal == "LONG" else "BUY"
                entry_price = price
                sl = tp = 0.0
                order_type_used = "SKIP"
                skip_reason = None

                # ═══ BƯỚC 0: Daily Kill Switch ═══
                kill = check_daily_kill_switch(bal)
                if not kill["ok"]:
                    skip_reason = f"KillSwitch: {kill['reason']}"
                    logger.info(f"[KillSwitch] ⛔ {skip_reason}")

                # ═══ BƯỚC 4: Score >= 70 ═══
                if not skip_reason and best.score < 70:
                    skip_reason = f"Score {best.score} < 70"

                # ═══ BƯỚC 5: Correlation — không vào 2 coin cùng nhóm ═══
                if not skip_reason:
                    try:
                        from quant_correlation import is_correlated_with_open
                        with lock:
                            open_pos = list(state.get("open_positions", []))
                        corr, corr_reason = is_correlated_with_open(
                            best.symbol, best.signal, open_pos
                        )
                        if corr:
                            skip_reason = f"Corr: {corr_reason}"
                    except Exception:
                        pass

                # ═══ BƯỚC 6: Xác định entry_price (MSS → Liq Engine → Swing fallback) ═══
                if not skip_reason:
                    from liquidity_engine import get_best_entry

                    cur_price = exchange.get_ticker_price(best.symbol)
                    raw_entry = 0.0  # khởi tạo rõ ràng tránh dùng dir() check

                    # Swing 15m (20 nến cuối)
                    klines_15m_entry = exchange.get_klines(best.symbol, "15m", limit=20)
                    df_15m_entry = _klines_to_df(klines_15m_entry)
                    swing_low  = df_15m_entry["low"].iloc[-20:].min()
                    swing_high = df_15m_entry["high"].iloc[-20:].max()
                    swing_price = swing_low if best.signal == "LONG" else swing_high

                    # ── Ưu tiên MSS entry_price nếu tier A hoặc B ──────────────
                    mss_res = getattr(best, "mss_result", None)
                    if (mss_res is not None
                            and mss_res.tier in ("A", "B")
                            and mss_res.entry_price > 0):
                        raw_entry = round(mss_res.entry_price, 8)
                        logger.info(f"[MSS] {best.symbol} dùng MSS entry={raw_entry:.6f} "
                                    f"tier={mss_res.tier} conf={mss_res.confidence:.0f}%")
                        # MSS entry đã tính sẵn zone — KHÔNG apply offset thêm
                        entry_price = raw_entry
                    else:
                        # Fallback: Liquidity Engine
                        liq_entry = get_best_entry(best.symbol, best.signal, cur_price, swing_price)
                        if liq_entry:
                            raw_entry = round(liq_entry["price"], 8)
                            logger.info(f"[LiqEngine] {best.symbol} {best.signal}: "
                                        f"entry=${raw_entry:.6f} dist={liq_entry['dist_pct']:.1f}% "
                                        f"score={liq_entry['score']:.1f} | {liq_entry['reason']}")
                        else:
                            # Fallback swing 15m
                            raw_entry = round(swing_low if best.signal == "LONG" else swing_high, 8)
                            logger.info(f"[LiqEngine] {best.symbol}: no liq zone → swing fallback @ {raw_entry:.6f}")

                        # ── Apply Entry Offset chỉ cho Liq Engine / swing fallback ──
                        if getattr(config, "ENTRY_OFFSET_ENABLED", False):
                            offset_pct = getattr(config, "ENTRY_OFFSET_PCT", 0.003)
                            if best.signal == "LONG":
                                entry_price = round(raw_entry * (1 - offset_pct), 8)
                            else:
                                entry_price = round(raw_entry * (1 + offset_pct), 8)
                            logger.info(f"[EntryOffset] {best.symbol} {best.signal}: "
                                        f"{raw_entry:.6f} → {entry_price:.6f} ({offset_pct*100:.1f}%)")
                        else:
                            entry_price = raw_entry

                # ═══ BƯỚC 7: Tính SL / TP theo entry_price cuối + RR + No-Chase ═══
                if not skip_reason:
                    from scanner import calc_structure_sl_tp, check_no_chase

                    # Tính SL/TP từ structure dựa trên entry_price đã apply offset
                    mss_res = getattr(best, "mss_result", None)
                    if (mss_res is not None
                            and mss_res.tier in ("A", "B")
                            and mss_res.sl_price > 0):
                        # MSS có sl_price riêng (dưới/trên sweep point)
                        # Nhưng vẫn cần validate sl ở đúng phía entry sau offset
                        sl_raw = mss_res.sl_price
                        if best.signal == "LONG" and sl_raw < entry_price:
                            sl = sl_raw
                        elif best.signal == "SHORT" and sl_raw > entry_price:
                            sl = sl_raw
                        else:
                            # MSS SL sai phía → fallback structure
                            sltp = calc_structure_sl_tp(df_15m_entry, best.signal, entry_price, config)
                            sl   = sltp["sl"]
                        # TP từ structure tính theo entry_price mới
                        sltp_tp = calc_structure_sl_tp(df_15m_entry, best.signal, entry_price, config)
                        tp      = sltp_tp["tp"]
                        risk    = abs(entry_price - sl)
                        reward  = abs(tp - entry_price)
                        rr      = reward / risk if risk > 0 else 0.0
                        sl_pct  = risk / entry_price * 100 if entry_price > 0 else 0
                        logger.info(f"[SL/TP] {best.symbol} MSS+offset: "
                                    f"entry={entry_price:.6f} sl={sl:.6f}({sl_pct:.2f}%) "
                                    f"tp={tp:.6f} RR={rr:.2f}")
                    else:
                        # Structure SL/TP tính theo entry_price đã offset
                        sltp    = calc_structure_sl_tp(df_15m_entry, best.signal, entry_price, config)
                        sl      = sltp["sl"]
                        tp      = sltp["tp"]
                        rr      = sltp["rr"]
                        logger.info(f"[SL/TP] {best.symbol} {best.signal}: "
                                    f"entry={entry_price:.6f} sl={sl:.6f}({sltp['sl_pct']:.2f}%) "
                                    f"tp={tp:.6f} RR={rr:.2f} | {sltp['sl_reason']}")

                    # Validate SL/TP đúng phía
                    if best.signal == "LONG":
                        if sl >= entry_price:
                            sl = round(entry_price * (1 - getattr(config, "SL_MIN_PCT", 0.008)), 8)
                        if tp <= entry_price:
                            from indicators import calculate_atr as _calc_atr
                            _atr = float(_calc_atr(df_15m_entry["high"], df_15m_entry["low"], df_15m_entry["close"]).iloc[-1])
                            tp = round(entry_price + _atr * 8, 8)
                    else:  # SHORT
                        if sl <= entry_price:
                            sl = round(entry_price * (1 + getattr(config, "SL_MIN_PCT", 0.008)), 8)
                        if tp >= entry_price:
                            from indicators import calculate_atr as _calc_atr
                            _atr = float(_calc_atr(df_15m_entry["high"], df_15m_entry["low"], df_15m_entry["close"]).iloc[-1])
                            tp = round(entry_price - _atr * 8, 8)

                    # Tính lại RR sau validate
                    risk   = abs(entry_price - sl)
                    reward = abs(tp - entry_price)
                    rr     = reward / risk if risk > 0 else 0.0

                    # No-chase: giá đã chạy xa khỏi planned entry?
                    atr_15m = float(calculate_atr(
                        df_15m_entry["high"], df_15m_entry["low"], df_15m_entry["close"]
                    ).iloc[-1])
                    cur_mark = exchange.get_ticker_price(best.symbol)
                    if cur_mark > 0 and check_no_chase(cur_mark, entry_price, atr_15m, best.signal, config):
                        skip_reason = (f"NoChase: mark={cur_mark:.6f} planned={entry_price:.6f} "
                                       f"({abs(cur_mark-entry_price)/entry_price*100:.1f}% away)")

                    # RR check
                    if not skip_reason:
                        min_rr = getattr(config, "MIN_RR", 1.5)
                        if rr < min_rr:
                            skip_reason = f"RR={rr:.2f} < {min_rr} (SL={sl:.6f} TP={tp:.6f})"

                # ═══ BƯỚC 8-9: Đặt LIMIT (max 2) hoặc lưu armed backup ═══
                if not skip_reason:
                    with lock:
                        armed = state.setdefault("armed_entries", {})

                    # Check số LIMIT entry đang có trên Binance
                    try:
                        all_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                        limit_entry_count = len([o for o in all_orders
                                                 if not o.get("reduceOnly", False)
                                                 and o.get("type") == "LIMIT"])
                    except Exception:
                        limit_entry_count = 0

                    if limit_entry_count < 2:
                        # Đặt LIMIT trực tiếp trên Binance (khớp instant khi giá tới)
                        order_type_used = "LIMIT"
                        try:
                            qty = calc_qty(bal, entry_price, sl, symbol=best.symbol, exchange=exchange)
                            if qty * entry_price < 5.0:
                                qty = round(5.0 / entry_price + 0.001, 3)
                            exchange.place_limit_order(best.symbol, side, qty, entry_price)

                            # Lưu pending_smart_orders để monitor đặt SL/TP khi fill
                            try:
                                open_orders = exchange._get("/fapi/v1/openOrders",
                                                           {"symbol": best.symbol}, signed=True)
                                entry_orders = [o for o in open_orders
                                               if not o.get("reduceOnly", False)
                                               and o.get("type") == "LIMIT"
                                               and o.get("symbol") == best.symbol]
                                with lock:
                                    psm = state.setdefault("pending_smart_orders", {})
                                    for o in entry_orders:
                                        oid = str(o["orderId"])
                                        psm[oid] = {
                                            "symbol": best.symbol, "side": best.signal,
                                            "qty": float(o.get("origQty", qty)),
                                            "sl": sl, "tp": tp, "ts": time.time(),
                                        }
                            except Exception:
                                pass

                            logger.info(f"[LiqLimit] LIMIT {side} {best.symbol} @ {entry_price:.6f} qty={qty}")
                        except Exception as e:
                            # LIMIT fail → lưu armed backup
                            armed[best.symbol] = {
                                "signal": best.signal, "entry_price": entry_price,
                                "raw_entry": raw_entry if raw_entry > 0 else entry_price,
                                "sl": sl, "tp": tp, "rr": rr, "score": best.score,
                                "side": side, "close_side": close_side, "ts": time.time(),
                            }
                            order_type_used = "ARMED"
                            logger.info(f"[Armed] LIMIT failed, armed backup: {best.symbol} {e}")
                    else:
                        # Đã có 2 LIMIT → lưu armed (WS trigger khi giá tới)
                        armed[best.symbol] = {
                            "signal": best.signal, "entry_price": entry_price,
                            "raw_entry": raw_entry if raw_entry > 0 else entry_price,
                            "sl": sl, "tp": tp, "rr": rr, "score": best.score,
                            "side": side, "close_side": close_side, "ts": time.time(),
                        }
                        order_type_used = "ARMED"
                        logger.info(f"[Armed] {best.symbol} {best.signal} entry={entry_price:.6f} (2 LIMIT đầy, WS backup)")

                if skip_reason or order_type_used == "SKIP":
                    logger.info(f"[Sweep] SKIP {best.symbol} {best.signal}: {skip_reason}")
                    _executing_symbols.discard(best.symbol)
                    _scan_monitor.wait_for_signal(timeout=config.LOOP_INTERVAL_SECONDS)
                    continue

                # ═══ BƯỚC 10: ARMED — chỉ log, không notify (chờ trigger mới notify) ═══
                qty = calc_qty(bal, entry_price, sl, symbol=best.symbol, exchange=exchange)
                if qty * entry_price < 5.0:
                    qty = round(5.0 / entry_price + 0.001, 3)

                logger.info(f"[Scan] ARMED {best.symbol} {best.signal} entry={entry_price:.6f} "
                            f"SL={sl:.6f} TP={tp:.6f} RR=1:{rr:.1f} score={best.score}")
                _executing_symbols.discard(best.symbol)  # xong → cho phép scan lại
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Scan engine: {e}", exc_info=True)
            _executing_symbols.discard(best.symbol if best else "")
            notifier.telegram.send(f"⚠️ Bot error: {e}")
            time.sleep(60)

        # ── Smart sleep: thay vì sleep 60s cứng,
        # dùng ScanPriceMonitor để wake up sớm khi có dump/bounce mạnh.
        # Pump spike checker (_wait_or_spike) vẫn giữ nguyên cho pump coins.
        # ScanPriceMonitor chỉ dành cho FIXED_COINS / scan thường.
        ws_signal = _scan_monitor.wait_for_signal(timeout=config.LOOP_INTERVAL_SECONDS)
        if ws_signal:
            sym_wake, direction_wake, chg_wake = ws_signal
            logger.info(
                f"[ScanEngine] ⚡ WS wake up: {sym_wake} {direction_wake} "
                f"{chg_wake:+.1f}% — fast scan ngay"
            )
            # Fast scan coin đó ngay (dùng lại _fast_spike_scan với logic LONG/dump)
            try:
                _fast_spike_scan(sym_wake, exchange, notifier)
            except Exception as _e:
                logger.debug(f"[ScanEngine] fast scan {sym_wake}: {_e}")
            # Reset cooldown để lần sau không bị block
            _scan_monitor.reset_cooldown(sym_wake)
        # Nếu hết timeout mà không có signal → vòng lặp tiếp theo (full scan)

# ============================================================
# THREAD 2c: Pump Scan Engine — quét đỉnh pump để SHORT
# Chạy song song với scan_engine, interval riêng
# - Có pump coins → quét mỗi 5s (bắt đỉnh kịp thời)
# - Không có pump coins → quét mỗi 30s (tiết kiệm API)
# ============================================================
def pump_scan_engine(exchange, notifier):
    """
    Mỗi PUMP_SCAN_INTERVAL_SECONDS (30s) hoặc 5s nếu có pump coins:
    1. Quét PUMP_WATCH_COINS + FIXED_COINS
    2. Nếu phát hiện đỉnh pump → gửi Telegram ngay
    3. Nếu config cho phép auto-short → vào lệnh SHORT luôn
    """
    # Đợi 10s cho bot ổn định trước
    time.sleep(10)
    logger.info("[PumpEngine] Started — watching for pump tops...")

    from pump_detector import PumpDetector, _to_df
    from orderbook_detector import OrderBookTracker

    detector   = PumpDetector(config)
    ob_tracker = OrderBookTracker()  # Real-time order book cho pump coins
    logger.info("[PumpEngine] OrderBook tracker initialized")

    while state["running"]:
        try:
            # Lấy danh sách pump coins từ state (web có thể add/remove)
            with lock:
                pump_coins = list(state.get("pump_watch_coins", []))

            # Sync ob_tracker với pump coins hiện tại
            if pump_coins:
                ob_tracker.add_symbols(pump_coins)

            # Lấy config dynamic
            auto_short   = getattr(config, "PUMP_AUTO_SHORT", False)
            soft_short   = getattr(config, "PUMP_AUTO_SHORT_SOFT", False)
            min_score    = getattr(config, "PUMP_TOP_MIN_SCORE", 60)
            slow_interval = getattr(config, "PUMP_SCAN_INTERVAL_SECONDS", 30)

            # Soft mode dùng ngưỡng thấp hơn
            if soft_short and not auto_short:
                detector.cfg["PUMP_TOP_MIN_SCORE"] = getattr(config, "PUMP_TOP_MIN_SCORE", 50)
                detector.cfg["PUMP_PRICE_RISE_PCT"] = 15.0
                _soft_rsi_min = 65
            else:
                detector.cfg["PUMP_TOP_MIN_SCORE"] = getattr(config, "PUMP_TOP_MIN_SCORE", 75)
                detector.cfg["PUMP_PRICE_RISE_PCT"] = getattr(config, "PUMP_PRICE_RISE_PCT", 20.0)
                _soft_rsi_min = 72

            # Interval thông minh:
            # - Có pump coins → 5s (cần bắt đỉnh trong vài giây)
            # - Không có    → 30s (tiết kiệm API)
            interval = 5 if pump_coins else slow_interval

            # Update trạng thái scanning cho web dashboard
            with lock:
                state.setdefault("pump_scan_status", {})
                state["pump_scan_status"]["scanning"] = True

            # ── Dọn signal cũ: xóa nếu giá đã giảm và pump% < ngưỡng alert ──
            # Tránh trường hợp signal cũ còn hiện mãi trên web dù coin đã về bình thường
            with lock:
                current_prices = dict(state.get("prices", {}))
                clean_signals  = []
                for sig in state.get("pump_signals", []):
                    sym_s      = sig.get("symbol", "")
                    pump_pct_s = sig.get("pump_pct", 0)
                    sig_ts     = sig.get("timestamp", 0)
                    cur_p      = current_prices.get(sym_s, 0)
                    entry_p    = sig.get("entry_price", 0)

                    # Xóa nếu:
                    # 1. Signal cũ hơn 30 phút VÀ không phải confirmed top
                    # 2. Hoặc giá đã giảm về dưới entry - 5% (pump đã xả sâu)
                    age_min = (time.time() - sig_ts) / 60 if sig_ts else 999
                    is_top  = sig.get("is_pump_top", False)

                    price_dropped = (cur_p > 0 and entry_p > 0
                                     and cur_p < entry_p * 0.95)
                    expired = age_min > 30 and not is_top

                    if price_dropped or expired:
                        logger.debug(f"[PumpEngine] Clear stale signal: {sym_s} "
                                     f"age={age_min:.0f}m price_dropped={price_dropped}")
                        # Cũng xóa pump_alerts nếu có
                        state.get("pump_alerts", {}).pop(sym_s, None)
                        continue
                    clean_signals.append(sig)
                state["pump_signals"] = clean_signals

            confirmed_this_round = []

            # ── Quét từng pump coin riêng lẻ (nhanh, không bị block) ──
            for symbol in pump_coins:
                try:
                    klines_1m  = exchange.get_klines(symbol, "1m",  limit=200)
                    klines_15m = exchange.get_klines(symbol, "15m", limit=50)
                    df_1m      = _to_df(klines_1m)
                    df_15m     = _to_df(klines_15m)

                    # ── Patch nến 1m cuối với giá realtime từ WebSocket ──
                    # Nến đang hình thành chưa đóng → cập nhật close/high với giá hiện tại
                    # Giúp detector nhận ra đỉnh pump sớm hơn ~30-60s so với đợi nến đóng
                    with lock:
                        ws_price = state.get("prices", {}).get(symbol, 0)
                    if ws_price > 0 and len(df_1m) > 0:
                        last_close = float(df_1m.iloc[-1]["close"])
                        last_high  = float(df_1m.iloc[-1]["high"])
                        # Cập nhật close và high với giá WS mới nhất
                        df_1m.iloc[-1, df_1m.columns.get_loc("close")] = ws_price
                        if ws_price > last_high:
                            df_1m.iloc[-1, df_1m.columns.get_loc("high")] = ws_price

                    sig = detector.analyze(symbol, df_1m, df_15m, ob_tracker=ob_tracker)

                    # ── Tự clear signal cũ nếu coin không còn pump ───────
                    # Nếu pump_pct < 40% ngưỡng → coin đã về bình thường
                    # Xóa ngay khỏi state để web không hiện data cũ
                    pump_threshold = detector.cfg["PUMP_PRICE_RISE_PCT"] * 0.4
                    current_pump = sig.pump_pct if sig else 0
                    if current_pump < pump_threshold:
                        with lock:
                            state["pump_signals"] = [s for s in state.get("pump_signals", [])
                                                     if s.get("symbol") != symbol]
                            state.get("pump_alerts", {}).pop(symbol, None)
                            state.get("_pump_alert_cd", {}).pop(f"alert_{symbol}", None)
                        if sig is None:
                            continue

                    # ── PUMP ALERT: coin đang pump nhưng chưa đủ điều kiện SHORT ──
                    # Chạy song song với analyze() — check ngưỡng thấp hơn
                    try:
                        from pump_detector import PumpDetector as _PD
                        alert_sig = detector.check_pump_rising(
                            symbol, df_1m, df_15m,
                            alert_cooldown=state.setdefault("_pump_alert_cd", {})
                        )
                        if alert_sig is not None:
                            # Lưu alert vào state để web hiển thị
                            alert_dict = {
                                "symbol":       alert_sig.symbol,
                                "is_pump_top":  False,
                                "is_alert":     True,           # flag phân biệt alert vs confirmed
                                "score":        alert_sig.score,
                                "pump_pct":     alert_sig.pump_pct,
                                "signals":      [alert_sig.reason],
                                "entry_price":  alert_sig.price,
                                "sl_price":     0,
                                "tp1_price":    0,
                                "tp2_price":    0,
                                "atr":          0,
                                "volume_ratio": alert_sig.volume_ratio,
                                "rsi":          alert_sig.rsi,
                                "timestamp":    alert_sig.timestamp,
                            }
                            with lock:
                                sigs = state.get("pump_signals", [])
                                idx  = next((i for i, s in enumerate(sigs)
                                             if s.get("symbol") == symbol), None)
                                # Chỉ ghi alert nếu chưa có confirmed top cho coin này
                                if idx is None:
                                    sigs.append(alert_dict)
                                elif not sigs[idx].get("is_pump_top", False):
                                    sigs[idx] = alert_dict
                                state["pump_signals"] = sigs[-100:]

                                # Lưu riêng pump_alerts để web hiển thị banner
                                pa = state.setdefault("pump_alerts", {})
                                pa[symbol] = {
                                    "pump_pct":    alert_sig.pump_pct,
                                    "price":       alert_sig.price,
                                    "rsi":         alert_sig.rsi,
                                    "vol_ratio":   alert_sig.volume_ratio,
                                    "score":       alert_sig.score,
                                    "reason":      alert_sig.reason,
                                    "ts":          alert_sig.timestamp,
                                }

                            # Telegram alert
                            try:
                                notifier.telegram.send(alert_sig.to_telegram())
                                logger.info(f"[PumpAlert] Alert sent: {symbol} +{alert_sig.pump_pct:.1f}%")
                            except Exception as te:
                                logger.warning(f"[PumpAlert] Telegram failed: {te}")
                    except Exception as _ae:
                        logger.debug(f"[PumpAlert] {symbol}: {_ae}")

                    if sig is None:
                        continue

                    # Lưu signal vào state để web hiển thị (kể cả chưa đủ score)
                    sig_dict = {
                        "symbol":       sig.symbol,
                        "is_pump_top":  sig.is_pump_top,
                        "is_alert":     False,
                        "score":        sig.score,
                        "pump_pct":     sig.pump_pct,
                        "signals":      sig.signals,
                        "entry_price":  sig.entry_price,
                        "sl_price":     sig.sl_price,
                        "tp1_price":    sig.tp1_price,
                        "tp2_price":    sig.tp2_price,
                        "atr":          sig.atr,
                        "volume_ratio": sig.volume_ratio,
                        "rsi":          sig.rsi,
                        "timestamp":    sig.timestamp,
                    }
                    with lock:
                        signals = state.get("pump_signals", [])
                        # Cập nhật hoặc thêm mới — confirmed top ghi đè alert
                        existing = next((i for i, s in enumerate(signals)
                                         if s.get("symbol") == symbol), None)
                        if existing is not None:
                            signals[existing] = sig_dict
                        else:
                            signals.append(sig_dict)
                        # Giới hạn 100 entry
                        state["pump_signals"] = signals[-100:]

                        # Xóa pump_alert khi đã có confirmed top
                        if sig.is_pump_top:
                            state.get("pump_alerts", {}).pop(symbol, None)

                    if sig.is_pump_top:
                        confirmed_this_round.append(sig)
                        logger.info(
                            f"[PumpEngine] TOP: {symbol} score={sig.score} "
                            f"+{sig.pump_pct:.1f}%"
                        )
                        # Telegram alert — chỉ gửi khi chưa có vị thế coin này
                        try:
                            with lock:
                                _open_syms_pump = {
                                    p["symbol"] for p in state.get("open_positions", [])
                                    if abs(float(p.get("positionAmt", 0))) > 0
                                }
                            if symbol not in _open_syms_pump:
                                notifier.telegram.send(
                                    sig.to_telegram(),
                                    reply_markup=sig.to_telegram_markup()
                                )
                        except Exception as te:
                            logger.warning(f"[PumpEngine] Telegram failed: {te}")

                except Exception as e:
                    logger.debug(f"[PumpEngine] {symbol} scan error: {e}")

            # ── PUMP NHẸ RADAR scan — ngưỡng thấp hơn, độc lập pump cũ ──
            # Chạy mỗi slow_interval (30s), dùng detector riêng với cfg nhẹ hơn
            # Đọc từ state (được sync realtime khi add/remove qua web) thay vì config
            with lock:
                nhe_coins = list(state.get("pump_nhe_coins", []))
            # Fallback về config nếu state trống
            if not nhe_coins:
                nhe_coins = list(getattr(config, "PUMP_NHE_COINS", []))
            nhe_auto  = getattr(config, "PUMP_NHE_AUTO_SHORT", False)
            nhe_score = getattr(config, "PUMP_NHE_MIN_SCORE", 60)
            nhe_rise  = getattr(config, "PUMP_NHE_PRICE_RISE_PCT", 10.0)

            # Define should_scan_fixed sớm — dùng cho cả NHE block và fixed_coins block
            _scan_tick_now = state.get("_pump_tick", 0)
            _should_scan_fixed = (_scan_tick_now % max(1, slow_interval // interval) == 0)

            if nhe_coins and _should_scan_fixed:
                # Tạo detector riêng với ngưỡng nhẹ hơn — không ảnh hưởng detector cũ
                nhe_detector = PumpDetector(config)
                nhe_detector.cfg["PUMP_TOP_MIN_SCORE"] = nhe_score
                nhe_detector.cfg["PUMP_PRICE_RISE_PCT"] = nhe_rise

                for symbol in nhe_coins:
                    # Bỏ qua nếu đã là pump coin cũ (tránh scan 2 lần)
                    if symbol in pump_coins:
                        continue
                    try:
                        klines_1m  = exchange.get_klines(symbol, "1m",  limit=200)
                        klines_15m = exchange.get_klines(symbol, "15m", limit=50)
                        df_1m      = _to_df(klines_1m)
                        df_15m     = _to_df(klines_15m)

                        # ── Patch nến 1m cuối với giá realtime WS (giống pump mạnh) ──
                        with lock:
                            ws_price_nhe = state.get("prices", {}).get(symbol, 0)
                        if ws_price_nhe > 0 and len(df_1m) > 0:
                            df_1m.iloc[-1, df_1m.columns.get_loc("close")] = ws_price_nhe
                            if ws_price_nhe > float(df_1m.iloc[-1]["high"]):
                                df_1m.iloc[-1, df_1m.columns.get_loc("high")] = ws_price_nhe

                        sig = nhe_detector.analyze(symbol, df_1m, df_15m)
                        if sig is None:
                            continue

                        # Lưu vào state (tag riêng để web phân biệt)
                        sig_dict = {
                            "symbol":       sig.symbol,
                            "is_pump_top":  sig.is_pump_top,
                            "is_nhe":       True,          # flag phân biệt với pump cũ
                            "score":        sig.score,
                            "pump_pct":     sig.pump_pct,
                            "signals":      sig.signals,
                            "entry_price":  sig.entry_price,
                            "sl_price":     sig.sl_price,
                            "tp1_price":    sig.tp1_price,
                            "tp2_price":    sig.tp2_price,
                            "atr":          sig.atr,
                            "volume_ratio": sig.volume_ratio,
                            "rsi":          sig.rsi,
                            "timestamp":    sig.timestamp,
                        }
                        with lock:
                            nhe_sigs = state.setdefault("pump_nhe_signals", [])
                            idx = next((i for i, s in enumerate(nhe_sigs)
                                        if s.get("symbol") == symbol), None)
                            if idx is not None:
                                nhe_sigs[idx] = sig_dict
                            else:
                                nhe_sigs.append(sig_dict)
                            state["pump_nhe_signals"] = nhe_sigs[-50:]

                        if sig.is_pump_top:
                            logger.info(
                                f"[PumpNhe] TOP: {symbol} score={sig.score} "
                                f"+{sig.pump_pct:.1f}% (ngưỡng {nhe_score})"
                            )
                            # Telegram alert — chỉ gửi khi chưa có vị thế coin này
                            try:
                                with lock:
                                    _open_syms_alert = {
                                        p["symbol"] for p in state.get("open_positions", [])
                                        if abs(float(p.get("positionAmt", 0))) > 0
                                    }
                                if symbol not in _open_syms_alert:
                                    nhe_msg = (
                                        sig.to_telegram()
                                        .replace("PUMP TOP — SHORT SIGNAL",
                                                 "PUMP NHẸ TOP — SHORT SIGNAL")
                                        .replace("/100", f"/100 (ngưỡng {nhe_score})")
                                    )
                                    notifier.telegram.send(
                                        nhe_msg,
                                        reply_markup=sig.to_telegram_markup()
                                    )
                            except Exception as _te:
                                logger.warning(f"[PumpNhe] Telegram failed: {_te}")

                            # AUTO SHORT nếu bật
                            if nhe_auto:
                                try:
                                    with lock:
                                        open_syms_nhe = {
                                            p["symbol"] for p in state.get("open_positions", [])
                                            if abs(float(p.get("positionAmt", 0))) > 0
                                        }
                                        n_open_nhe = len(state.get("open_positions", []))

                                    if (symbol not in open_syms_nhe
                                            and n_open_nhe < config.MAX_OPEN_POSITIONS):
                                        exchange.set_leverage(symbol, config.LEVERAGE)
                                        cur_p_nhe = exchange.get_ticker_price(symbol)
                                        if not cur_p_nhe or cur_p_nhe <= 0:
                                            cur_p_nhe = sig.entry_price

                                        qty_nhe = (config.MAX_ORDER_USDT * config.LEVERAGE) / cur_p_nhe
                                        try:
                                            step, _, decimals, _ = exchange.get_qty_precision(symbol)
                                            qty_nhe = max(
                                                round(int(qty_nhe / step) * step, decimals), step
                                            )
                                        except Exception:
                                            qty_nhe = round(qty_nhe, 3)

                                        if qty_nhe * cur_p_nhe >= 5.0:
                                            exchange.place_market_order(symbol, "SELL", qty_nhe)
                                            time.sleep(0.8)

                                            # SL với retry
                                            sl_ok_nhe = False
                                            for _a in range(3):
                                                try:
                                                    exchange.place_stop_loss_order(
                                                        symbol, "BUY", qty_nhe, sig.sl_price
                                                    )
                                                    sl_ok_nhe = True
                                                    break
                                                except Exception:
                                                    time.sleep(0.5)

                                            if not sl_ok_nhe:
                                                logger.warning(f"[PumpNhe] SL failed for {symbol} — keeping position")
                                            else:
                                                try:
                                                    exchange.place_take_profit_order(
                                                        symbol, "BUY", qty_nhe, sig.tp1_price
                                                    )
                                                except Exception:
                                                    pass
                                                # Set cooldown — auto_sltp không đặt trùng
                                                _set_sltp_cooldown(symbol)

                                                rr_nhe = (
                                                    abs(cur_p_nhe - sig.tp1_price)
                                                    / abs(cur_p_nhe - sig.sl_price)
                                                    if abs(cur_p_nhe - sig.sl_price) > 0 else 0
                                                )
                                                with lock:
                                                    state["trade_log"].append({
                                                        "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                                        "symbol": symbol, "side": "SHORT",
                                                        "entry":  cur_p_nhe,
                                                        "sl": sig.sl_price, "tp": sig.tp1_price,
                                                        "qty": qty_nhe, "status": "OPEN",
                                                        "note": f"pump_nhe_short_s{sig.score}",
                                                    })
                                                    state.setdefault("pump_trade_symbols", set()).add(symbol)

                                                notifier.telegram.send(
                                                    f"🔴 <b>AUTO SHORT — PUMP NHẸ</b>\n"
                                                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                                                    f"🪙 {symbol}  📈 +{sig.pump_pct:.1f}%  "
                                                    f"Score {sig.score}/{nhe_score}\n"
                                                    f"💰 Entry : <b>${cur_p_nhe:,.6g}</b>\n"
                                                    f"🛑 SL    : <b>${sig.sl_price:,.6g}</b>\n"
                                                    f"🎯 TP    : <b>${sig.tp1_price:,.6g}</b>\n"
                                                    f"📐 RR    : 1:{rr_nhe:.1f}   📦 Qty: {qty_nhe}\n"
                                                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                                )
                                                logger.info(
                                                    f"[PumpNhe] SHORT placed: {symbol} "
                                                    f"score={sig.score} qty={qty_nhe}"
                                                )
                                except Exception as _nhe_e:
                                    logger.error(f"[PumpNhe] Short {symbol} failed: {_nhe_e}")

                    except Exception as _nhe_scan_e:
                        logger.debug(f"[PumpNhe] {symbol} scan error: {_nhe_scan_e}")

            # ── Cũng quét FIXED_COINS nhưng chậm hơn (mỗi slow_interval) ──
            # Chỉ chạy khi interval == slow_interval (không có pump coins)
            # hoặc mỗi 6 vòng (30s) khi đang chạy 5s
            with lock:
                _scan_tick = state.get("_pump_tick", 0) + 1
                state["_pump_tick"] = _scan_tick

            fixed_coins = [c for c in list(getattr(config, "FIXED_COINS", WATCHLIST))
                           if c not in pump_coins]
            should_scan_fixed = _should_scan_fixed

            if should_scan_fixed and fixed_coins:
                for symbol in fixed_coins:
                    try:
                        klines_1m  = exchange.get_klines(symbol, "1m",  limit=200)
                        klines_15m = exchange.get_klines(symbol, "15m", limit=50)
                        df_1m      = _to_df(klines_1m)
                        df_15m     = _to_df(klines_15m)
                        sig = detector.analyze(symbol, df_1m, df_15m, ob_tracker=ob_tracker)
                        if sig and sig.is_pump_top:
                            confirmed_this_round.append(sig)
                            logger.info(f"[PumpEngine] Fixed coin TOP: {symbol} score={sig.score}")
                            try:
                                notifier.telegram.send(
                                    sig.to_telegram(),
                                    reply_markup=sig.to_telegram_markup()
                                )
                            except Exception:
                                pass
                    except Exception as e:
                        logger.debug(f"[PumpEngine] fixed {symbol}: {e}")

            # ── PUMP REVERSAL EXIT ──────────────────────────
            # Chỉ áp dụng cho lệnh SHORT do pump engine vào (không đụng lệnh thường)
            try:
                with lock:
                    open_positions = list(state.get("open_positions", []))
                    # Chỉ những coin pump engine đã vào lệnh
                    pump_trade_syms = set(state.get("pump_trade_symbols", set()))
                    pump_shorts = {
                        p["symbol"]
                        for p in open_positions
                        if float(p.get("positionAmt", 0)) < 0  # đang SHORT
                        and p["symbol"] in pump_trade_syms      # do pump engine vào
                    }

                # Chỉ chạy khi Reversal Monitor đang bật VÀ không phải chỉ alert
                if not getattr(config, "REVERSAL_MONITOR_ENABLED", True):
                    pump_shorts = set()
                elif getattr(config, "REVERSAL_ALERT_ONLY", False):
                    pump_shorts = set()

                for symbol in pump_shorts:
                    # Tính thời gian đã giữ lệnh
                    _min_hold_pump = getattr(config, "PUMP_REVERSAL_HOLD_SECONDS", 60)
                    _entry_time_pump = None
                    _held = 0
                    with lock:
                        for t in reversed(state.get("trade_log", [])):
                            if t.get("symbol") == symbol and t.get("status") == "OPEN":
                                _entry_time_pump = t.get("time")
                                break
                    if _entry_time_pump:
                        try:
                            _held = (datetime.now() - datetime.strptime(_entry_time_pump, "%Y-%m-%d %H:%M:%S")).total_seconds()
                        except Exception:
                            _held = 0

                    # Điều kiện đóng SHORT — Hướng A:
                    # - Nếu cur_pnl >= floor_pct → activate ngay dù chưa đủ 60s (bảo vệ lời)
                    # - Nếu chưa đủ 60s VÀ chưa lời >= floor_pct → bỏ qua, để SL lo
                    try:
                        cur_price = exchange.get_ticker_price(symbol)
                        pos_entry = next(
                            (float(p.get("entryPrice", 0)) for p in open_positions
                             if p["symbol"] == symbol), 0
                        )
                        floor_pct = getattr(config, "PUMP_REVERSAL_FLOOR_PCT", 1.0)

                        if pos_entry > 0:
                            cur_pnl_pct = (pos_entry - cur_price) / pos_entry * 100  # SHORT

                            # Track MFE cho pump reversal
                            mfe_key_pump = f"_pump_mfe_{symbol}"
                            with lock:
                                prev_mfe = state.get(mfe_key_pump, 0)
                                if cur_pnl_pct > prev_mfe:
                                    state[mfe_key_pump] = cur_pnl_pct
                                    prev_mfe = cur_pnl_pct

                            logger.debug(
                                f"[PumpRevExit] {symbol}: held={_held:.0f}s "
                                f"mfe={prev_mfe:.2f}% cur={cur_pnl_pct:.2f}% floor={floor_pct}%"
                            )

                            # Hướng A:
                            # Nếu prev_mfe >= floor_pct → đã từng lời đủ → theo dõi bất kể thời gian
                            # Nếu prev_mfe < floor_pct VÀ chưa đủ 60s → bỏ qua (noise đầu lệnh)
                            already_profitable = prev_mfe >= floor_pct
                            held_enough        = _held >= _min_hold_pump

                            if already_profitable and cur_pnl_pct <= floor_pct:
                                # Đã từng lời >= 1%, giờ rút về <= 1% → đóng
                                should_exit = True
                            elif not already_profitable and not held_enough:
                                # Chưa lời đủ 1% VÀ chưa đủ 60s → bỏ qua
                                should_exit = False
                            else:
                                should_exit = False
                        else:
                            should_exit = False
                    except Exception:
                        should_exit = False

                    if should_exit:
                        try:
                            all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
                            pos = next((p for p in all_pos
                                       if p["symbol"] == symbol
                                       and abs(float(p.get("positionAmt", 0))) > 0), None)
                            if not pos:
                                continue

                            amt   = float(pos["positionAmt"])
                            qty   = abs(amt)
                            entry = float(pos.get("entryPrice", 0))
                            close_price = exchange.get_ticker_price(symbol)

                            # Lấy open_time_ms từ trade_log trước khi đóng
                            _pr_open_ms = 0
                            with lock:
                                for _t_pr in reversed(state.get("trade_log", [])):
                                    if _t_pr.get("symbol") == symbol and _t_pr.get("status") == "OPEN":
                                        try:
                                            from datetime import datetime as _dtp_pr
                                            _pr_open_ms = int(_dtp_pr.strptime(_t_pr["time"], "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
                                        except Exception:
                                            pass
                                        break

                            # Đóng SHORT → BUY
                            exchange.place_market_order(symbol, "BUY", qty)
                            exchange.cancel_all_orders(symbol)

                            time.sleep(0.5)
                            pnl = _fetch_actual_pnl(exchange, symbol, "SHORT", qty, entry, close_price, _pr_open_ms)

                            # Ghi trade log
                            with lock:
                                for t in reversed(state.get("trade_log", [])):
                                    if t.get("symbol") == symbol and t.get("status") == "OPEN":
                                        t.update({
                                            "status": "CLOSED",
                                            "close": close_price,
                                            "pnl_usdt": round(pnl, 2),
                                            "pnl_pct": round((entry - close_price) / entry * 100, 2),
                                        })
                                        break
                                # Xóa khỏi pump_trade_symbols sau khi đã đóng
                                state.get("pump_trade_symbols", set()).discard(symbol)
                                # Reset cooldown để có thể SHORT lại ngay nếu pump tiếp
                                if hasattr(detector, '_cooldown'):
                                    detector._cooldown.pop(symbol, None)

                            icon = "✅" if pnl >= 0 else "⚠️"
                            profit_tag = "Chốt lời" if pnl >= 0 else "Cắt lỗ sớm"
                            notifier.telegram.send(
                                f"🔄 <b>PUMP REVERSAL EXIT — {profit_tag}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"🪙 {symbol} lời rút về {cur_pnl_pct:.1f}% ≤ floor {floor_pct:.1f}% (peak={prev_mfe:.1f}%)\n"
                                f"⚡ Đóng SHORT giữ lời\n"
                                f"💰 Entry: ${entry:.6f} → Close: ${close_price:.6f}\n"
                                f"{icon} PnL: <b>${pnl:+.2f}</b>\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            )
                            logger.info(
                                f"[PumpRevExit] REVERSAL EXIT: {symbol} "
                                f"mfe={prev_mfe:.1f}% cur={cur_pnl_pct:.1f}% floor={floor_pct:.1f}% pnl=${pnl:+.2f}"
                            )
                        except Exception as ex:
                            logger.error(f"[PumpEngine] Reversal exit {symbol}: {ex}")
            except Exception as ex:
                logger.error(f"[PumpEngine] Reversal check error: {ex}")

            # ── Update scan status cho web ──────────────────
            with lock:
                st = state.setdefault("pump_scan_status", {})
                st["scanning"]   = False
                st["last_scan"]  = datetime.now().strftime("%H:%M:%S")
                st["scan_count"] = st.get("scan_count", 0) + 1
                # Sync pump_watch_coins → config
                config.PUMP_WATCH_COINS = list(state.get("pump_watch_coins", []))

            # ── AUTO SHORT nếu bật (hard hoặc soft mode) ──────────────────────────────
            if (auto_short or soft_short) and confirmed_this_round:
                with lock:
                    n_open = len(state.get("open_positions", []))
                if n_open >= config.MAX_OPEN_POSITIONS:
                    confirmed_this_round = []

                for sig in confirmed_this_round:
                    symbol = sig.symbol
                    try:
                        # Kiểm tra không có position / pending
                        with lock:
                            open_syms = {
                                p["symbol"] for p in state.get("open_positions", [])
                                if abs(float(p.get("positionAmt", 0))) > 0
                            }
                        if symbol in open_syms:
                            continue
                        try:
                            pending_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                            if any(o["symbol"] == symbol and not o.get("reduceOnly")
                                   for o in pending_orders):
                                continue
                        except Exception:
                            pass

                        exchange.set_leverage(symbol, config.LEVERAGE)

                        # Lấy giá market HIỆN TẠI để tính qty chính xác
                        try:
                            current_price = exchange.get_ticker_price(symbol)
                        except Exception:
                            current_price = sig.entry_price
                        if not current_price or current_price <= 0:
                            current_price = sig.entry_price

                        qty = (config.MAX_ORDER_USDT * config.LEVERAGE) / current_price
                        try:
                            step, max_qty, decimals, min_notional = exchange.get_qty_precision(symbol)
                            qty = max(round(int(qty / step) * step, decimals), step)
                        except Exception:
                            qty = round(qty, 3)

                        if qty * current_price < 5.0:
                            continue

                        entry_type = getattr(sig, "entry_type", "MARKET")

                        if entry_type == "LIMIT":
                            # Đặt LIMIT tại sig.entry_price (99.5% đỉnh)
                            # Nếu giá đã dưới entry_price → dùng MARKET ngay
                            if current_price <= sig.entry_price:
                                exchange.place_market_order(symbol, "SELL", qty)
                                order_tag = "MARKET (đã dưới limit)"
                            else:
                                exchange.place_limit_order(symbol, "SELL", qty, sig.entry_price)
                                order_tag = f"LIMIT @ ${sig.entry_price:.6g}"
                                logger.info(f"[PumpEngine] LIMIT SHORT placed: {symbol} @ {sig.entry_price:.6g}")
                                # Lưu vào state để theo dõi fill
                                with lock:
                                    state.setdefault("pump_limit_orders", {})[symbol] = {
                                        "entry_price": sig.entry_price,
                                        "sl_price":    sig.sl_price,
                                        "tp1_price":   sig.tp1_price,
                                        "qty":         qty,
                                        "ts":          time.time(),
                                    }
                                # Gửi Telegram thông báo đặt lệnh chờ
                                try:
                                    notifier.telegram.send(
                                        f"⏳ <b>LIMIT SHORT ĐẶT SẴN</b>\n"
                                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                                        f"🪙 {symbol}  📈 +{sig.pump_pct:.1f}%\n"
                                        f"📌 Chờ giá quay về <b>${sig.entry_price:.6g}</b>\n"
                                        f"🛑 SL: <b>${sig.sl_price:.6g}</b>\n"
                                        f"🎯 TP: <b>${sig.tp1_price:.6g}</b>\n"
                                        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                    )
                                except Exception:
                                    pass
                                # Skip SL/TP setup — sẽ đặt sau khi lệnh fill
                                continue
                        else:
                            exchange.place_market_order(symbol, "SELL", qty)
                            order_tag = "MARKET"

                        time.sleep(0.8)  # đợi lệnh fill trước khi đặt SL/TP

                        # Đặt SL với retry 3 lần — không có SL là nguy hiểm
                        sl_ok = False
                        for _attempt in range(3):
                            try:
                                exchange.place_stop_loss_order(symbol, "BUY", qty, sig.sl_price)
                                sl_ok = True
                                break
                            except Exception as e:
                                logger.debug(f"[PumpEngine] SL attempt {_attempt+1} {symbol}: {e}")
                                time.sleep(0.5)
                        if not sl_ok:
                            logger.warning(f"[PumpEngine] SL FAILED for {symbol} — keeping position, auto_sltp will retry")

                        # Đặt TP (không bắt buộc, lỗi thì bỏ qua)
                        try:
                            exchange.place_take_profit_order(symbol, "BUY", qty, sig.tp1_price)
                        except Exception as e:
                            logger.warning(f"[PumpEngine] TP {symbol}: {e}")
                        # Set cooldown để auto_sltp không đặt trùng
                        _set_sltp_cooldown(symbol)

                        rr = abs(current_price - sig.tp1_price) / abs(current_price - sig.sl_price) if abs(current_price - sig.sl_price) > 0 else 0
                        with lock:
                            state["trade_log"].append({
                                "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "symbol": symbol, "side": "SHORT",
                                "entry":  sig.entry_price,
                                "sl": sig.sl_price, "tp": sig.tp1_price,
                                "qty": qty, "status": "OPEN",
                                "note": f"pump_short_s{sig.score}",
                            })
                            # Track lệnh này là do pump engine vào → dùng cho reversal exit
                            pump_trades = state.setdefault("pump_trade_symbols", set())
                            pump_trades.add(symbol)

                        notifier.telegram.send(
                            f"🔴 <b>AUTO SHORT — PUMP TOP</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🪙 {symbol}  📈 +{sig.pump_pct:.1f}%  Score {sig.score}/100\n"
                            f"💰 Entry : <b>${sig.entry_price:,.6g}</b>  [{order_tag}]\n"
                            f"🛑 SL    : <b>${sig.sl_price:,.6g}</b>\n"
                            f"🎯 TP1   : <b>${sig.tp1_price:,.6g}</b>\n"
                            f"📐 RR    : 1:{rr:.1f}   📦 Qty: {qty}\n"
                            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                        )
                        logger.info(f"[PumpEngine] SHORT placed: {symbol} score={sig.score}")

                    except Exception as e:
                        logger.error(f"[PumpEngine] Short {symbol} failed: {e}")

        except Exception as e:
            logger.error(f"[PumpEngine] Loop error: {e}", exc_info=True)
            with lock:
                state.setdefault("pump_scan_status", {})["scanning"] = False

        time.sleep(interval)


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

            # ── Cả 2 lệnh đã khớp → Binance tự xử lý SL/TP, bot chỉ sync state ──
            elif sp.filled1 and sp.filled2:
                # SL/TP đã được đặt trên Binance sau khi order2 fill
                # Không tự đóng lệnh ở đây — để Binance xử lý
                # Khi Binance đóng, price_updater sẽ detect và sync trade log
                pass

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
                pump_limits = dict(state.get("pump_limit_orders", {}))

            # ── A0. Check pump LIMIT SHORT orders ──────────────────
            if pump_limits:
                for sym, info in list(pump_limits.items()):
                    try:
                        # Timeout 10 phút — nếu không khớp thì huỷ
                        if time.time() - info["ts"] > 600:
                            try:
                                exchange.cancel_all_orders(sym)
                            except Exception:
                                pass
                            with lock:
                                state.get("pump_limit_orders", {}).pop(sym, None)
                            logger.info(f"[PumpLimit] {sym} LIMIT expired, cancelled")
                            continue

                        # Kiểm tra có position chưa (lệnh đã fill)
                        all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
                        pos = next((p for p in all_pos
                                    if p["symbol"] == sym
                                    and float(p.get("positionAmt", 0)) < 0), None)  # SHORT = âm

                        if pos:
                            # Lệnh đã fill → đặt SL/TP ngay
                            qty      = abs(float(pos["positionAmt"]))
                            sl_price = info["sl_price"]
                            tp_price = info["tp1_price"]
                            fill_price = float(pos.get("entryPrice", info["entry_price"]))
                            cur_mark = float(pos.get("markPrice", 0)) or exchange.get_ticker_price(sym)

                            time.sleep(0.3)

                            # SL retry 3 lần — nếu giá đã bay quá SL gốc → dùng SL mới
                            sl_ok = False
                            for _sl_try in range(3):
                                try:
                                    exchange.place_stop_loss_order(sym, "BUY", qty, sl_price)
                                    sl_ok = True
                                    break
                                except Exception as e:
                                    # SL fail → có thể giá đã vượt SL → tính SL mới
                                    if "price" in str(e).lower() or "400" in str(e):
                                        # SHORT: SL phải > giá hiện tại
                                        new_sl = round(cur_mark * 1.03, 8)  # 3% trên giá hiện tại
                                        try:
                                            exchange.place_stop_loss_order(sym, "BUY", qty, new_sl)
                                            sl_price = new_sl
                                            sl_ok = True
                                            logger.warning(f"[PumpLimit] SL adjusted: {sym} → ${new_sl:.6g} (giá đã bay)")
                                            break
                                        except Exception:
                                            pass
                                    time.sleep(0.5)

                            if not sl_ok:
                                logger.warning(f"[PumpLimit] SL FAILED {sym} — keeping position, auto_sltp will retry")
                            else:
                                logger.info(f"[PumpLimit] SL placed: {sym} @ {sl_price}")

                            try:
                                exchange.place_take_profit_order(sym, "BUY", qty, tp_price)
                                logger.info(f"[PumpLimit] TP placed: {sym} @ {tp_price}")
                            except Exception as e:
                                logger.error(f"[PumpLimit] TP failed {sym}: {e}")
                            notifier.telegram.send(
                                f"🔴 <b>PUMP LIMIT FILLED!</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🪙 {sym} SHORT\n"
                                f"💰 Fill: <b>${fill_price:.6g}</b>\n"
                                f"🛑 SL set: <b>${sl_price:.6g}</b>\n"
                                f"🎯 TP set: <b>${tp_price:.6g}</b>\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            )
                            with lock:
                                state.get("pump_limit_orders", {}).pop(sym, None)
                                state["trade_log"].append({
                                    "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "symbol": sym, "side": "SHORT",
                                    "entry":  fill_price,
                                    "sl":     sl_price, "tp": tp_price,
                                    "qty":    qty, "status": "OPEN",
                                    "note":   "pump_limit_filled",
                                })

                    except Exception as e:
                        logger.debug(f"[PumpLimit] Monitor {sym}: {e}")

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
                                # Set cooldown để auto_sltp không đặt trùng
                                state.setdefault("_sltp_cooldown", {})[sym] = time.time()
                            logger.info(f"[LimitMonitor] {info['symbol']} order {status}, removing")
                            with lock:
                                state.get("pending_smart_orders", {}).pop(str(order_id), None)

                    except Exception as e:
                        logger.debug(f"[LimitMonitor] Check order {order_id}: {e}")

            # ── B. Auto SL/TP cho positions mới (mỗi 30s) ──
            # Chỉ đặt nếu position THỰC SỰ không có SL/TP trên Binance
            # VÀ không có pending entry order chưa khớp (tránh đặt trùng)
            # VÀ chưa đặt trong 5 phút gần nhất (tránh duplicate)
            if _time.time() - last_auto_check > 30:
                last_auto_check = _time.time()
                try:
                    from auto_sltp import get_positions_without_sltp, auto_set_sltp
                    liq_tracker = state.get("liq_tracker")
                    unprotected = get_positions_without_sltp(exchange)

                    # Lấy danh sách pending entry orders (chưa khớp)
                    try:
                        pending_entry_syms = {
                            o["symbol"] for o in exchange._get("/fapi/v1/openOrders", signed=True)
                            if not o.get("reduceOnly", False)
                               and o.get("type") == "LIMIT"
                        }
                    except Exception:
                        pending_entry_syms = set()

                    # Cooldown: không đặt SL/TP lại cho coin đã đặt trong 5 phút
                    _sltp_cooldown = state.setdefault("_sltp_cooldown", {})
                    now_ts = _time.time()

                    for pos in unprotected:
                        sym = pos["symbol"]
                        # Bỏ qua nếu còn pending entry order chưa khớp
                        if sym in pending_entry_syms:
                            logger.debug(f"[AutoSLTP] Skip {sym}: còn pending entry order")
                            continue
                        # Bỏ qua nếu đã đặt trong 5 phút gần đây
                        if sym in _sltp_cooldown and now_ts - _sltp_cooldown[sym] < 300:
                            continue

                        logger.info(f"[AutoSLTP] Detected unprotected: {sym} {pos['side']}")

                        # ── Risk validation: SL không được xa hơn risk 1% cho phép ──
                        # Nếu SL quá xa → điều chỉnh lại để risk <= 1% balance
                        _bal_sltp = exchange.get_total_equity()
                        _risk_max = _bal_sltp * getattr(config, "RISK_PER_TRADE_PCT", 0.01)
                        _notional = abs(float(pos["qty"])) * float(pos["entry"])
                        if _notional > 0:
                            _max_sl_dist_pct = _risk_max / _notional
                            # Cap: SL không xa hơn max_sl_dist từ entry
                            _entry_v = float(pos["entry"])
                            if pos["side"] == "LONG":
                                _sl_floor = round(_entry_v * (1 - _max_sl_dist_pct), 8)
                            else:
                                _sl_floor = round(_entry_v * (1 + _max_sl_dist_pct), 8)
                        else:
                            _sl_floor = None

                        result = auto_set_sltp(exchange, sym, pos["side"],
                                               pos["entry"], pos["qty"], liq_tracker,
                                               sl_floor=_sl_floor)
                        _sltp_cooldown[sym] = now_ts  # Đánh dấu đã đặt
                        # Notify Telegram
                        try:
                            notifier_inst = state.get("_notifier")
                            if notifier_inst:
                                icon = "✅" if result["ok"] else "⚠️"
                                missing = []
                                if not pos["has_sl"]: missing.append("SL")
                                if not pos["has_tp"]: missing.append("TP")
                                notifier_inst.telegram.send(
                                    f"{icon} <b>AUTO SL/TP SET</b>\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"📊 {sym} {pos['side']} (thiếu: {', '.join(missing)})\n"
                                    f"{result['msg']}"
                                )
                        except Exception as _ne:
                            logger.debug(f"[AutoSLTP] Notify failed: {_ne}")
                except Exception as e:
                    logger.debug(f"[AutoSLTP] Check error: {e}")

        except Exception as e:
            logger.error(f"[LimitMonitor] Error: {e}")

        time.sleep(5)


# ============================================================
# THREAD 10b: Partial TP Monitor — chốt từng phần khi đạt % lời
# ============================================================
# ============================================================
# PROFIT PROTECTION + TRAILING SL MONITOR
# ============================================================
# Flow 3 tầng SL:
#   Tầng 1: Initial SL (đặt khi vào lệnh)
#   Tầng 2: Protection SL (khi lời >= 0.6%) → SL về breakeven + fee
#   Tầng 3: Trailing SL  (khi lời >= 1.0%) → trailing 0.5% theo peak
#
# Rule: SL chỉ được dịch theo hướng có lợi, KHÔNG BAO GIỜ nới rộng
# Khi đã có lợi nhuận → không bao giờ để lỗ → chỉ lời ít hơn thôi
# ============================================================
def profit_protection_monitor(exchange, notifier):
    """
    Monitor mỗi 1s, check tất cả open positions.
    Áp dụng 3 tầng SL tự động.
    """
    # Per-position state
    # {symbol: {
    #   "side": LONG/SHORT,
    #   "entry": float,
    #   "tier": 1/2/3,           # tầng SL hiện tại
    #   "current_sl": float,     # SL đang đặt trên Binance
    #   "protection_ts": float,  # timestamp khi lời đạt 0.6%
    #   "trailing_ts": float,    # timestamp khi lời đạt 1.0%
    #   "peak_price": float,     # peak price kể từ trailing ON
    #   "trailing_sl": float,    # trailing SL hiện tại
    # }}
    _pp_state: dict = {}

    time.sleep(20)  # đợi bot ổn định
    logger.info("[PP] profit_protection_monitor started")
    # Expose state cho web dashboard
    with lock:
        state["_pp_state"] = _pp_state

    while state["running"]:
        try:
            if not getattr(config, "PROFIT_PROTECTION_ENABLED", True):
                time.sleep(5)
                continue

            pp_trigger    = getattr(config, "PP_TRIGGER_PCT",           0.6)
            pp_timer      = getattr(config, "PP_TIMER_SECS",            15)
            fee_buf       = getattr(config, "PP_FEE_BUFFER_PCT",        0.15)
            trail_trigger = getattr(config, "PP_TRAILING_TRIGGER_PCT",  1.0)
            trail_timer   = getattr(config, "PP_TRAILING_TIMER_SECS",   7)
            trail_dist    = getattr(config, "PP_TRAILING_DISTANCE_PCT", 0.5) / 100
            apply_scan    = getattr(config, "PP_APPLY_SCAN",            True)
            apply_pump    = getattr(config, "PP_APPLY_PUMP",            True)

            with lock:
                open_pos   = [p for p in state.get("open_positions", [])
                              if abs(float(p.get("positionAmt", 0))) > 0]
                pump_syms  = set(state.get("pump_trade_symbols", set()))
                prices_now = dict(state.get("prices", {}))

            logger.debug(f"[PP] tick: {len(open_pos)} positions, enabled={getattr(config,'PROFIT_PROTECTION_ENABLED',True)}")

            # Cleanup state cho position đã đóng
            active_syms = {p["symbol"] for p in open_pos}
            for sym in list(_pp_state.keys()):
                if sym not in active_syms:
                    _pp_state.pop(sym, None)

            for pos in open_pos:
                sym    = pos["symbol"]
                amt    = float(pos.get("positionAmt", 0))
                entry  = float(pos.get("entryPrice", 0))
                if entry <= 0 or amt == 0:
                    continue

                is_long = amt > 0
                side    = "LONG" if is_long else "SHORT"
                is_pump = sym in pump_syms

                # Check apply
                if is_pump and not apply_pump:
                    continue
                if not is_pump and not apply_scan:
                    continue

                # Lấy mark price từ WS (realtime)
                mark = prices_now.get(sym, 0)
                if mark <= 0:
                    continue

                # Tính profit % hiện tại (chưa trừ phí)
                if is_long:
                    profit_pct = (mark - entry) / entry * 100
                else:
                    profit_pct = (entry - mark) / entry * 100

                now = time.time()
                logger.debug(f"[PP] {sym} {side} profit={profit_pct:.2f}% mark={mark:.6f} entry={entry:.6f}")

                # Khởi tạo state nếu chưa có
                if sym not in _pp_state:
                    # Lấy SL hiện tại từ Binance
                    cur_sl = 0.0
                    try:
                        orders = exchange._get("/fapi/v1/openOrders",
                                               {"symbol": sym}, signed=True)
                        sl_orders = [o for o in orders
                                     if o.get("type") in ("STOP_MARKET", "STOP")
                                     and o.get("reduceOnly", False)]
                        if sl_orders:
                            cur_sl = float(sl_orders[0].get("stopPrice", 0))
                    except Exception:
                        pass
                    _pp_state[sym] = {
                        "side":          side,
                        "entry":         entry,
                        "tier":          1,
                        "current_sl":    cur_sl,
                        "protection_ts": 0.0,
                        "trailing_ts":   0.0,
                        "peak_price":    mark,
                        "trailing_sl":   0.0,
                    }
                    logger.info(f"[PP] {sym} initialized tier=1 sl={cur_sl:.6f}")
                    continue  # skip vòng này, xử lý vòng sau

                ps = _pp_state[sym]

                # ── Cập nhật peak price ──────────────────────────────
                if is_long:
                    if mark > ps["peak_price"]:
                        ps["peak_price"] = mark
                else:
                    if mark < ps["peak_price"] or ps["peak_price"] == 0:
                        ps["peak_price"] = mark

                # ── TẦNG 2: PROTECTION SL ───────────────────────────
                if ps["tier"] < 2 and profit_pct >= pp_trigger:
                    if ps["protection_ts"] == 0.0:
                        ps["protection_ts"] = now
                        logger.debug(f"[PP] {sym} profit={profit_pct:.2f}% >= {pp_trigger}% → timer start")
                    elif now - ps["protection_ts"] >= pp_timer:
                        # Timer đủ → đặt Protection SL
                        # Protection SL = breakeven + fee buffer (NET PnL > 0)
                        fee_total = fee_buf / 100  # 0.15% tổng phí
                        if is_long:
                            new_sl = round(entry * (1 + fee_total), 8)
                            # Chỉ update nếu tốt hơn SL cũ
                            if new_sl > ps["current_sl"]:
                                if _update_sl(exchange, sym, side, new_sl, abs(amt)):
                                    ps["tier"]       = 2
                                    ps["current_sl"] = new_sl
                                    logger.info(f"[PP] ✅ {sym} LONG tier2 Protection SL={new_sl:.6f} "
                                                f"(breakeven+{fee_buf}%)")
                                    notifier.telegram.send(
                                        f"🛡 <b>PROTECTION SL</b>: {sym} {side}\n"
                                        f"SL dời về breakeven ${new_sl:.6f}\n"
                                        f"Lời {profit_pct:.2f}% → không bao giờ lỗ\n"
                                        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                    )
                        else:  # SHORT
                            new_sl = round(entry * (1 - fee_total), 8)
                            if new_sl < ps["current_sl"] or ps["current_sl"] == 0:
                                if _update_sl(exchange, sym, side, new_sl, abs(amt)):
                                    ps["tier"]       = 2
                                    ps["current_sl"] = new_sl
                                    logger.info(f"[PP] ✅ {sym} SHORT tier2 Protection SL={new_sl:.6f}")
                                    notifier.telegram.send(
                                        f"🛡 <b>PROTECTION SL</b>: {sym} {side}\n"
                                        f"SL dời về breakeven ${new_sl:.6f}\n"
                                        f"Lời {profit_pct:.2f}% → không bao giờ lỗ\n"
                                        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                    )
                elif profit_pct < pp_trigger * 0.7:
                    # Reset timer nếu giá giảm rõ ràng
                    ps["protection_ts"] = 0.0

                # ── TẦNG 3: TRAILING SL ─────────────────────────────
                if ps["tier"] >= 2 and profit_pct >= trail_trigger:
                    if ps["trailing_ts"] == 0.0:
                        ps["trailing_ts"] = now
                        ps["peak_price"]  = mark  # reset peak từ đây
                        logger.debug(f"[PP] {sym} profit={profit_pct:.2f}% >= {trail_trigger}% → trailing timer")
                    elif now - ps["trailing_ts"] >= trail_timer:
                        # Trailing ON — tính trailing SL từ peak
                        peak = ps["peak_price"]
                        if is_long:
                            new_trail_sl = round(peak * (1 - trail_dist), 8)
                            # Chỉ update nếu tốt hơn SL hiện tại
                            if new_trail_sl > ps["current_sl"]:
                                if _update_sl(exchange, sym, side, new_trail_sl, abs(amt)):
                                    if ps["tier"] < 3:
                                        ps["tier"] = 3
                                        logger.info(f"[PP] ✅ {sym} LONG tier3 TRAILING ON "
                                                    f"peak={peak:.6f} sl={new_trail_sl:.6f}")
                                    ps["current_sl"]  = new_trail_sl
                                    ps["trailing_sl"] = new_trail_sl
                        else:  # SHORT
                            new_trail_sl = round(peak * (1 + trail_dist), 8)
                            if new_trail_sl < ps["current_sl"] or ps["current_sl"] == 0:
                                if _update_sl(exchange, sym, side, new_trail_sl, abs(amt)):
                                    if ps["tier"] < 3:
                                        ps["tier"] = 3
                                        logger.info(f"[PP] ✅ {sym} SHORT tier3 TRAILING ON "
                                                    f"peak={peak:.6f} sl={new_trail_sl:.6f}")
                                    ps["current_sl"]  = new_trail_sl
                                    ps["trailing_sl"] = new_trail_sl

        except Exception as e:
            logger.debug(f"[PP] monitor error: {e}")

        time.sleep(getattr(config, "PP_CHECK_INTERVAL_SECS", 1))


def _update_sl(exchange, symbol: str, side: str, new_sl: float, qty: float) -> bool:
    """
    Update SL trên Binance — cancel SL cũ rồi đặt SL mới.
    Returns True nếu thành công.
    """
    try:
        close_side = "SELL" if side == "LONG" else "BUY"
        # Cancel SL cũ
        orders = exchange._get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
        sl_orders = [o for o in orders
                     if o.get("type") in ("STOP_MARKET", "STOP")
                     and o.get("reduceOnly", False)]
        for o in sl_orders:
            try:
                exchange._delete("/fapi/v1/order",
                                 {"symbol": symbol, "orderId": o["orderId"]})
            except Exception:
                pass
        # Đặt SL mới
        exchange.place_stop_loss_order(symbol, close_side, qty, new_sl)
        return True
    except Exception as e:
        logger.debug(f"[PP] _update_sl {symbol} failed: {e}")
        return False


def partial_tp_monitor(exchange, notifier):
    """
    Mỗi 5s kiểm tra tất cả positions:
    - TP1: lời >= PARTIAL_TP1_PCT → đóng PARTIAL_TP1_CLOSE_PCT% vị thế
    - TP2: lời >= PARTIAL_TP2_PCT → đóng thêm PARTIAL_TP2_CLOSE_PCT%
    - Sau TP1: dời SL về breakeven (entry price)
    """
    # Track trạng thái partial TP cho từng position
    # {symbol: {"tp1_done": bool, "tp2_done": bool}}
    _partial_state: dict = {}

    time.sleep(15)  # đợi bot ổn định

    while state["running"]:
        try:
            if not getattr(config, "PARTIAL_TP_ENABLED", True):
                time.sleep(10)
                continue

            tp1_pct       = getattr(config, "PARTIAL_TP1_PCT",       2.0)
            tp1_close_pct = getattr(config, "PARTIAL_TP1_CLOSE_PCT", 50.0)
            tp2_enabled   = getattr(config, "PARTIAL_TP2_ENABLED",   True)
            tp2_pct       = getattr(config, "PARTIAL_TP2_PCT",       4.0)
            tp2_close_pct = getattr(config, "PARTIAL_TP2_CLOSE_PCT", 30.0)
            move_sl_be    = getattr(config, "PARTIAL_TP_MOVE_SL_BE", True)
            apply_scan    = getattr(config, "PARTIAL_TP_APPLY_SCAN", True)
            apply_pump    = getattr(config, "PARTIAL_TP_APPLY_PUMP", True)

            with lock:
                open_pos = [p for p in state.get("open_positions", [])
                            if abs(float(p.get("positionAmt", 0))) > 0]
                pump_syms = set(state.get("pump_trade_symbols", set()))

            for pos in open_pos:
                symbol = pos["symbol"]
                amt    = float(pos.get("positionAmt", 0))
                entry  = float(pos.get("entryPrice", 0))
                if entry <= 0 or amt == 0:
                    continue

                is_long  = amt > 0
                side_str = "LONG" if is_long else "SHORT"
                is_pump  = symbol in pump_syms

                # Check apply
                if is_pump and not apply_pump:
                    continue
                if not is_pump and not apply_scan:
                    continue

                # Lấy giá mark
                mark = state.get("prices", {}).get(symbol, 0)
                if mark <= 0:
                    try:
                        mark = exchange.get_ticker_price(symbol)
                    except Exception:
                        continue

                # Tính PnL %
                if is_long:
                    pnl_pct = (mark - entry) / entry * 100
                else:
                    pnl_pct = (entry - mark) / entry * 100

                ps = _partial_state.setdefault(symbol, {"tp1_done": False, "tp2_done": False})

                qty_total = abs(amt)
                close_side = "SELL" if is_long else "BUY"

                # ── TP1 ──────────────────────────────────────────
                if not ps["tp1_done"] and pnl_pct >= tp1_pct:
                    try:
                        qty_close = round(qty_total * tp1_close_pct / 100, 8)
                        # Lấy step size
                        try:
                            step, _, decimals, min_notional = exchange.get_qty_precision(symbol)
                            if step >= 1:
                                qty_close = int(qty_close // step) * int(step)
                            else:
                                qty_close = round(int(qty_close / step) * step, decimals)
                        except Exception:
                            pass

                        if qty_close * mark < 5.0:
                            logger.debug(f"[PartialTP] {symbol} TP1 qty too small, skip")
                        else:
                            exchange.place_market_order(symbol, close_side, qty_close)
                            ps["tp1_done"] = True
                            logger.info(f"[PartialTP] ✅ TP1 {symbol} {side_str}: "
                                        f"đóng {tp1_close_pct:.0f}% ({qty_close}) @ lời {pnl_pct:.1f}%")

                            # Dời SL về breakeven
                            if move_sl_be:
                                try:
                                    # Cancel SL cũ
                                    all_orders = exchange._get("/fapi/v1/openOrders",
                                                              {"symbol": symbol}, signed=True)
                                    sl_orders = [o for o in all_orders
                                                 if o.get("type") in ("STOP_MARKET", "STOP")
                                                 and o.get("reduceOnly", False)]
                                    for o in sl_orders:
                                        exchange._delete("/fapi/v1/order",
                                                        {"symbol": symbol, "orderId": o["orderId"]})
                                    # Đặt SL mới tại entry
                                    be_sl = round(entry * (1.001 if is_long else 0.999), 8)
                                    exchange.place_stop_loss_order(symbol, close_side,
                                                                   round(qty_total - qty_close, 8),
                                                                   be_sl)
                                    logger.info(f"[PartialTP] SL dời về BE={be_sl:.6f} cho {symbol}")
                                except Exception as _e:
                                    logger.debug(f"[PartialTP] Move SL BE {symbol}: {_e}")

                            notifier.telegram.send(
                                f"💰 <b>PARTIAL TP1</b>: {symbol} {side_str}\n"
                                f"Chốt {tp1_close_pct:.0f}% vị thế @ lời {pnl_pct:.1f}%\n"
                                f"SL dời về breakeven {entry:.6f}\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            )
                    except Exception as e:
                        logger.error(f"[PartialTP] TP1 {symbol}: {e}")

                # ── TP2 ──────────────────────────────────────────
                elif tp2_enabled and ps["tp1_done"] and not ps["tp2_done"] and pnl_pct >= tp2_pct:
                    try:
                        # Lấy lại qty hiện tại (đã giảm sau TP1)
                        try:
                            cur_pos = exchange._get("/fapi/v2/positionRisk",
                                                   {"symbol": symbol}, signed=True)
                            cur_amt = abs(float(next(
                                (p["positionAmt"] for p in cur_pos
                                 if p["symbol"] == symbol), 0
                            )))
                        except Exception:
                            cur_amt = qty_total * (1 - tp1_close_pct / 100)

                        qty_close2 = round(cur_amt * tp2_close_pct / 100, 8)
                        try:
                            step, _, decimals, _ = exchange.get_qty_precision(symbol)
                            if step >= 1:
                                qty_close2 = int(qty_close2 // step) * int(step)
                            else:
                                qty_close2 = round(int(qty_close2 / step) * step, decimals)
                        except Exception:
                            pass

                        if qty_close2 * mark < 5.0:
                            logger.debug(f"[PartialTP] {symbol} TP2 qty too small, skip")
                        else:
                            exchange.place_market_order(symbol, close_side, qty_close2)
                            ps["tp2_done"] = True
                            logger.info(f"[PartialTP] ✅ TP2 {symbol} {side_str}: "
                                        f"đóng {tp2_close_pct:.0f}% @ lời {pnl_pct:.1f}%")
                            notifier.telegram.send(
                                f"💰 <b>PARTIAL TP2</b>: {symbol} {side_str}\n"
                                f"Chốt thêm {tp2_close_pct:.0f}% @ lời {pnl_pct:.1f}%\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            )
                    except Exception as e:
                        logger.error(f"[PartialTP] TP2 {symbol}: {e}")

                # Reset nếu position đã đóng hoàn toàn
                if abs(amt) == 0:
                    _partial_state.pop(symbol, None)

        except Exception as e:
            logger.debug(f"[PartialTP] monitor error: {e}")

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
                time.sleep(14400)  # 4 tiếng
                continue

            advice_lines = ["📊 <b>PHÂN TÍCH VỊ THẾ (4 tiếng)</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n"]

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

        time.sleep(14400)  # 4 tiếng


# ============================================================
# THREAD 11: Orphan Order Cleanup — mỗi 20 phút xóa SL/TP mồ côi
# ============================================================
def orphan_order_cleanup(exchange, notifier):
    """Nếu coin có SL/TP order nhưng KHÔNG có position → hủy
    NGOẠI TRỪ: BTC, ETH, BNB, XRP — giữ lệnh chờ cho user tự quản lý."""
    time.sleep(60)  # Chờ 60s sau bot start rồi quét ngay

    # Coin không bị auto cancel — user muốn tự hủy tay
    EXCLUDE_AUTO_CANCEL = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT"}

    while state["running"]:
        try:
            all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
            open_syms = {p["symbol"] for p in all_pos
                        if abs(float(p.get("positionAmt", 0))) > 0}
            cancelled = []
            logger.debug(f"[OrphanCleanup] cycle done, cancelled={len(cancelled)}")

            # Algo orders — dùng cancel_all_orders cho coin mồ côi
            try:
                algo_orders = exchange._get("/fapi/v1/openAlgoOrders", signed=True)
                if isinstance(algo_orders, list):
                    orphan_algo_syms = set()
                    for o in algo_orders:
                        sym = o.get("symbol", "")
                        if sym and sym not in open_syms and sym not in EXCLUDE_AUTO_CANCEL:
                            orphan_algo_syms.add(sym)
                    for sym in orphan_algo_syms:
                        try:
                            exchange.cancel_all_orders(sym)
                            cancelled.append(f"{sym} (algo)")
                            logger.info(f"[OrphanCleanup] Cancelled algo orders: {sym}")
                        except Exception as _ae:
                            logger.warning(f"[OrphanCleanup] Cancel algo {sym} failed: {_ae}")
            except Exception as _algo_e:
                logger.warning(f"[OrphanCleanup] openAlgoOrders error: {_algo_e}")

            # Regular reduceOnly orders
            try:
                all_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                for o in all_orders:
                    sym = o.get("symbol", "")
                    if sym in EXCLUDE_AUTO_CANCEL:
                        continue
                    if sym and sym not in open_syms and o.get("reduceOnly", False):
                        try:
                            exchange._delete("/fapi/v1/order", {"symbol": sym, "orderId": o.get("orderId")})
                            cancelled.append(f"{sym} ({o.get('type', '')})")
                            logger.info(f"[OrphanCleanup] Cancelled SL/TP: {sym}")
                        except Exception as _e:
                            logger.debug(f"[OrphanCleanup] Cancel {sym} failed: {_e}")
            except Exception as _e:
                logger.debug(f"[OrphanCleanup] openOrders error: {_e}")

            # ── Huỷ entry LIMIT: trend đổi hoặc > 4h (chỉ khi nút bật) ──
            if state.get("auto_cancel_orphan", False):
                try:
                    all_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                    for o in all_orders:
                        sym = o.get("symbol", "")
                        if sym in EXCLUDE_AUTO_CANCEL:
                            continue
                        if not (sym and sym not in open_syms and not o.get("reduceOnly", False)):
                            continue
                        # Check thời gian
                        order_time_ms = int(o.get("time", 0))
                        order_age_sec = (time.time() * 1000 - order_time_ms) / 1000 if order_time_ms else 0

                        should_cancel = False
                        reason = ""

                        # Max 4h → huỷ
                        if order_age_sec > 14400:
                            should_cancel = True
                            reason = f">{order_age_sec/3600:.1f}h"
                        # > 20 phút → check trend
                        elif order_age_sec > 1200:
                            try:
                                from indicators import calculate_ema
                                kl_4h = exchange.get_klines(sym, "4h", limit=20)
                                df_4h = _klines_to_df(kl_4h)
                                ema9 = calculate_ema(df_4h["close"], 9).iloc[-1]
                                ema21 = calculate_ema(df_4h["close"], 21).iloc[-1]
                                price_4h = df_4h["close"].iloc[-1]
                                ema50 = calculate_ema(df_4h["close"], 50).iloc[-1]
                                order_side = o.get("side", "")
                                if order_side == "BUY" and (ema9 < ema21 or price_4h < ema50):
                                    should_cancel = True
                                    reason = "trend đổi bearish"
                                elif order_side == "SELL" and (ema9 > ema21 or price_4h > ema50):
                                    should_cancel = True
                                    reason = "trend đổi bullish"
                            except Exception:
                                pass

                        if should_cancel:
                            try:
                                exchange._delete("/fapi/v1/order", {"symbol": sym, "orderId": o.get("orderId")})
                                cancelled.append(f"{sym} (entry {reason})")
                                # Xóa khỏi pending_smart_orders
                                with lock:
                                    psm = state.get("pending_smart_orders", {})
                                    for k, v in list(psm.items()):
                                        if v.get("symbol") == sym:
                                            psm.pop(k, None)
                                            break
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

        time.sleep(120)  # 2 phút


def memory_cleanup():
    """Mỗi 30 phút: garbage collect + giới hạn trade_log + clear caches"""
    import gc
    while state["running"]:
        time.sleep(1800)  # 30 phút
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
    Mỗi 4 tiếng (nếu auto_cancel_orphan=True):
    1. Lấy tất cả pending limit orders
    2. Kiểm tra xu hướng hiện tại (EMA, RSI)
    3. Nếu lệnh LONG nhưng xu hướng BEARISH → hủy
    4. Nếu lệnh SHORT nhưng xu hướng BULLISH → hủy
    5. Nếu giá đã đi xa quá (>3%) khỏi entry → hủy

    CHÚ Ý: chỉ chạy khi state["auto_cancel_orphan"] = True
    Nếu False → giữ nguyên tất cả lệnh chờ (manual order mode)
    """
    from indicators import calculate_rsi, calculate_ema
    from scanner import _klines_to_df

    # Đợi 5 phút sau khi bot start
    time.sleep(300)

    # Coin không bị auto cancel pending review
    EXCLUDE_PENDING_REVIEW = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"}

    while state["running"]:
        try:
            # ── Chỉ chạy khi user bật toggle trên web ──────────
            if not state.get("auto_cancel_orphan", False):
                time.sleep(900)  # check lại sau 15 phút
                continue

            # Lấy pending orders
            all_orders = exchange._get("/fapi/v1/openOrders", signed=True)
            limit_orders = [o for o in all_orders if o.get("type") == "LIMIT"
                           and not o.get("reduceOnly", False)]

            if not limit_orders:
                time.sleep(14400)  # 4 tiếng
                continue

            cancelled = []
            for order in limit_orders:
                sym = order.get("symbol", "")
                side = order.get("side", "")
                order_price = float(order.get("price", 0))

                # Skip coin không bị auto cancel
                if sym in EXCLUDE_PENDING_REVIEW:
                    continue

                try:
                    current_price = exchange.get_ticker_price(sym)

                    # Check 1: giá đi xa quá 3%
                    dist_pct = abs(current_price - order_price) / order_price * 100
                    if dist_pct > 3:
                        exchange.cancel_all_orders(sym)
                        cancelled.append(f"{sym} (giá xa {dist_pct:.1f}%)")
                        logger.info(f"[PendingReview] Cancelled {sym}: price moved {dist_pct:.1f}%")
                        continue

                    # Check 2: xu hướng ngược
                    klines = exchange.get_klines(sym, "15m", limit=50)
                    df = _klines_to_df(klines)
                    close = df["close"]
                    rsi = calculate_rsi(close, 14).iloc[-1]
                    ema9 = calculate_ema(close, 9).iloc[-1]
                    ema21 = calculate_ema(close, 21).iloc[-1]

                    if side == "BUY":
                        if rsi > 70 or (ema9 < ema21 and current_price < ema21):
                            exchange.cancel_all_orders(sym)
                            cancelled.append(f"{sym} LONG (xu hướng bearish, RSI={rsi:.0f})")
                    else:
                        if rsi < 30 or (ema9 > ema21 and current_price > ema21):
                            exchange.cancel_all_orders(sym)
                            cancelled.append(f"{sym} SHORT (xu hướng bullish, RSI={rsi:.0f})")

                except Exception as e:
                    logger.debug(f"[PendingReview] Skip {sym}: {e}")

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

        time.sleep(14400)  # 4 tiếng


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

    # Khởi động Liquidation Tracker (websocket — tích lũy theo thời gian)
    from scanner import WATCHLIST as _wl
    liq_tracker = LiquidationTracker(
        symbols  = list(_wl),
        testnet  = config.USE_TESTNET,
        bucket_pct = getattr(config, "LIQ_BUCKET_PCT", 0.001),
    )
    liq_tracker.start()
    state["liq_tracker"] = liq_tracker

    # Khởi tạo pump state
    with lock:
        state["pump_watch_coins"]  = list(getattr(config, "PUMP_WATCH_COINS", []))
        state["pump_nhe_coins"]    = list(getattr(config, "PUMP_NHE_COINS", []))
        state["pump_signals"]      = []
        state["pump_nhe_signals"]  = []
        state["pump_alerts"]       = {}   # {symbol: {pump_pct, price, rsi, ...}}
        state["_pump_alert_cd"]    = {}   # cooldown dict cho pump alerts
        state["pump_limit_orders"] = {}   # {symbol: {entry_price, sl_price, tp1_price, qty, ts}}
        state["pump_scan_status"]  = {"scanning": False, "last_scan": "--:--", "scan_count": 0}
        state["_pump_tick"]        = 0

    # Khởi động Order Book Tracker cho pump coins
    try:
        from orderbook_detector import get_ob_tracker
        _ob_base_ws = "wss://fstream.binance.com" if not config.USE_TESTNET else "wss://stream.binancefuture.com"
        _ob = get_ob_tracker(_ob_base_ws)
        _pump_coins_init = list(getattr(config, "PUMP_WATCH_COINS", []))
        if _pump_coins_init:
            _ob.add_symbols(_pump_coins_init)
            logger.info(f"[OB] Tracker started for {_pump_coins_init}")
        print(f"✅ OrderBook Tracker started ({len(_pump_coins_init)} coins)", flush=True)
    except Exception as _e:
        print(f"⚠️ OrderBook Tracker failed: {_e}", flush=True)
        logger.warning(f"[OB] Tracker init failed: {_e}")

    # Khởi động LiqHeatmapCache (REST API — có data ngay lập tức)
    try:
        from liq_heatmap_api import LiqHeatmapCache
        print("🔄 Starting LiqHeatmapCache...", flush=True)
        liq_api_cache = LiqHeatmapCache(
            symbols  = list(_wl),
            interval = "1h",
            lookback = 24,
        )
        liq_api_cache.start()
        state["liq_api_cache"] = liq_api_cache
        print(f"✅ LiqHeatmapCache started for {len(_wl)} symbols", flush=True)
        logger.info(f"[LiqAPI] Heatmap cache started for {len(_wl)} symbols")
    except Exception as _e:
        print(f"⚠️ LiqAPI Cache start failed: {_e}", flush=True)
        logger.warning(f"[LiqAPI] Cache start failed: {_e}")
        state["liq_api_cache"] = None

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

    def _start_worker_threads():
        """Khởi động lại tất cả worker threads sau khi resume."""
        with lock:
            state["running"] = True

        _t1 = threading.Thread(target=price_updater, args=(exchange,), daemon=True)
        _t1.start()
        _t1ws = threading.Thread(target=price_ws_streamer, daemon=True)
        _t1ws.start()
        _t2a = threading.Thread(target=monitor_engine, args=(exchange, notifier), daemon=True)
        _t2a.start()
        _t2a2 = threading.Thread(target=position_reversal_monitor, args=(exchange, notifier), daemon=True)
        _t2a2.start()
        _t2a3 = threading.Thread(target=scan_position_protector, args=(exchange, notifier), daemon=True)
        _t2a3.start()
        _t_trailing = threading.Thread(target=trailing_profit_lock, args=(exchange, notifier), daemon=True)
        _t_trailing.start()
        _t_mfe_scan = threading.Thread(target=mfe_scan_monitor, args=(exchange, notifier), daemon=True)
        _t_mfe_scan.start()
        _t2b = threading.Thread(target=scan_engine, args=(exchange, notifier), daemon=True)
        _t2b.start()
        _t3 = threading.Thread(target=grid_engine, args=(exchange, notifier), daemon=True)
        _t3.start()
        _t5 = threading.Thread(target=liq_engine, args=(exchange, notifier, liq_tracker), daemon=True)
        _t5.start()
        _t_pump = threading.Thread(target=pump_scan_engine, args=(exchange, notifier), daemon=True)
        _t_pump.start()
        _t7 = threading.Thread(target=limit_order_monitor, args=(exchange, notifier), daemon=True)
        _t7.start()
        # Restart Telegram command handler
        try:
            from telegram_commands import TelegramCommandHandler
            from notifier import NOTIFICATION_CONFIG
            _cmd = TelegramCommandHandler(
                bot_token=NOTIFICATION_CONFIG["telegram"]["bot_token"],
                chat_id=NOTIFICATION_CONFIG["telegram"]["chat_id"],
                state=state, state_lock=lock,
                watchlist=WATCHLIST, config=config
            )
            _t4 = threading.Thread(target=_cmd.run, daemon=True)
            _t4.start()
        except Exception as _e:
            logger.warning(f"Telegram restart failed: {_e}")
        logger.info("✅ All worker threads restarted via web Start Bot")
        notifier.telegram.send("▶️ <b>Bot đã được khởi động lại từ Web Dashboard</b>")

    # Đăng ký restart callback cho web dashboard
    with lock:
        state["_restart_fn"] = _start_worker_threads

    t1 = threading.Thread(target=price_updater, args=(exchange,), daemon=True)
    t1.start()

    # WebSocket price stream (realtime)
    t1ws = threading.Thread(target=price_ws_streamer, daemon=True)
    t1ws.start()

    trade_engine(exchange, notifier)  # send startup notification
    print("✅ trade_engine done", flush=True)

    t2a = threading.Thread(target=monitor_engine, args=(exchange, notifier), daemon=True)
    t2a.start()

    t2b = threading.Thread(target=scan_engine, args=(exchange, notifier), daemon=True)
    t2b.start()

    t3 = threading.Thread(target=grid_engine, args=(exchange, notifier), daemon=True)
    t3.start()

    # Pump scan thread — phát hiện đỉnh pump để SHORT (mỗi 30s)
    t_pump = threading.Thread(target=pump_scan_engine, args=(exchange, notifier), daemon=True)
    t_pump.start()

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
    t_trailing = threading.Thread(target=trailing_profit_lock, args=(exchange, notifier), daemon=True)
    t_trailing.start()
    t_profit_lock = threading.Thread(target=auto_profit_lock, args=(exchange, notifier), daemon=True)
    t_profit_lock.start()
    t_mfe_scan = threading.Thread(target=mfe_scan_monitor, args=(exchange, notifier), daemon=True)
    t_mfe_scan.start()
    t_partial_tp = threading.Thread(target=partial_tp_monitor, args=(exchange, notifier), daemon=True)
    t_partial_tp.start()
    t_pp = threading.Thread(target=profit_protection_monitor, args=(exchange, notifier), daemon=True)
    t_pp.start()

    # AI Analyzer thread — chạy TradingAgents mỗi 4h
    def ai_analyzer_loop():
        import time as _t
        AI_INTERVAL = getattr(config, "AI_ANALYSIS_INTERVAL_HOURS", 4) * 3600
        # Chờ 5s sau khi bot start để ổn định (không block Telegram)
        _t.sleep(5)
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
                # Notify — chỉ log, không gửi Telegram
                logger.info(f"[AI Analyzer] Done: {results}")
            except Exception as e:
                logger.error(f"[AI Analyzer] Error: {e}")
                with lock:
                    state["ai_analyzing"] = False
            _t.sleep(AI_INTERVAL)

    # AI Analyzer thread — chạy LLM (Groq/Gemini/DeepSeek) mỗi 12h
    def ai_analyzer_loop():
        import time as _t
        AI_INTERVAL = 12 * 3600  # 12 tiếng/lần
        _t.sleep(30)  # chờ bot ổn định
        while state["running"]:
            try:
                from ai_analyzer import analyze_all
                import config as _cfg
                coins = list(getattr(_cfg, "FIXED_COINS", []))[:8]  # top 8 coin
                logger.info(f"[AI Analyzer] Starting LLM analysis for {coins}...")
                with lock:
                    state["ai_analyzing"] = True
                results = analyze_all(coins)
                with lock:
                    state["ai_analyzing"] = False
                    state["ai_last_run"] = datetime.now().strftime("%H:%M")
                logger.info(f"[AI Analyzer] Done: { {s: r['bias'] for s,r in results.items()} }")
            except Exception as e:
                logger.error(f"[AI Analyzer] Error: {e}")
                with lock:
                    state["ai_analyzing"] = False
            _t.sleep(AI_INTERVAL)

    # AI Analyzer thread — tắt, dùng dashboard TradingAgents để chạy tay
    # if getattr(config, "AI_AUTO_ANALYSIS", True):
    #     t6 = threading.Thread(target=ai_analyzer_loop, daemon=True)
    #     t6.start()

    try:
        from telegram_commands import TelegramCommandHandler
        from notifier import NOTIFICATION_CONFIG
        cmd = TelegramCommandHandler(
            bot_token=NOTIFICATION_CONFIG["telegram"]["bot_token"],
            chat_id=NOTIFICATION_CONFIG["telegram"]["chat_id"],
            state=state, state_lock=lock,
            watchlist=WATCHLIST, config=config
        )
        # Sync watchlist từ file (user đã add/remove coins) vào state
        if cmd.watchlist:
            with lock:
                state["_watchlist"] = list(cmd.watchlist)
            # Cũng update scanner WATCHLIST để scan đúng coins
            import scanner as _scanner_mod
            _scanner_mod.WATCHLIST[:] = cmd.watchlist
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
