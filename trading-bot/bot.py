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
    "running":        True,
    "_watchlist":     list(WATCHLIST),  # sync với scanner WATCHLIST
    # --- Liquidation strategy state ---
    "split_positions": {},
    "liq_data":       {},
    "pending_smart_orders": {},
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
                        sl_price = round(ws_high * 1.02, 8)
                        # TP: -10% từ entry (pump thường xả nhanh 10-20%)
                        tp_price = round(cur_price * 0.90, 8)

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

    def on_message(wsapp, message):
        try:
            data    = _json.loads(message)
            payload = data.get("data", {})
            sym     = payload.get("s", "")
            mark    = float(payload.get("p", 0))
            if sym and mark > 0:
                with lock:
                    state["prices"][sym] = mark

                # ── PUMP SPIKE CHECK — chỉ coin trong pump_watch_coins ──
                with lock:
                    pump_watch = set(state.get("pump_watch_coins", []))
                if sym in pump_watch:
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

            # ── Max loss check: đóng lệnh nếu lỗ > $20 ──
            max_loss = getattr(config, "MAX_LOSS_PER_POSITION", 20.0)
            for p in open_pos:
                pnl = p.get("_pnl", 0)
                sym = p["symbol"]
                amt = float(p.get("positionAmt", 0))

                # Bỏ qua nếu position = 0 (đã đóng)
                if abs(amt) == 0:
                    continue

                if pnl < -max_loss:
                    # ── Chỉ tự đóng nếu KHÔNG có SL order trên Binance ──
                    # Nếu có SL → để Binance tự đóng, không can thiệp
                    try:
                        all_orders = exchange._get("/fapi/v1/openOrders", signed=True)
                        has_sl = any(
                            o.get("symbol") == sym
                            and o.get("type") in ("STOP_MARKET", "STOP")
                            and o.get("reduceOnly", False)
                            for o in all_orders
                        )
                        if has_sl:
                            logger.debug(f"[MAX LOSS] {sym} pnl=${pnl:.2f} — SL tồn tại trên Binance, để Binance đóng")
                            continue
                    except Exception:
                        pass  # Không lấy được orders → vẫn chạy safety net

                    amt = float(p.get("positionAmt", 0))
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
                        logger.info(f"[MAX LOSS] Closed {sym} pnl=${pnl:.2f} (no SL on Binance, exceeded -${max_loss})")
                        try:
                            notifier_inst = state.get("_notifier")
                            if notifier_inst:
                                notifier_inst.telegram.send(
                                    f"🚨 <b>MAX LOSS SAFETY NET</b>\n"
                                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                                    f"📊 {sym} (không có SL trên Binance)\n"
                                    f"💵 PnL: <b>${pnl:.2f}</b> (exceeded -${max_loss})\n"
                                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                                )
                        except Exception:
                            pass
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
        time.sleep(3)  # update giá mỗi 3s

# ============================================================
# THREAD 2: Trade engine mỗi 60 giây
# ============================================================
def calc_qty(balance, entry, sl, symbol="", exchange=None):
    # Dùng MAX_ORDER_USDT cố định từ config — đơn giản, nhất quán
    qty = (config.MAX_ORDER_USDT * config.LEVERAGE) / entry if entry > 0 else 1.0

    # Lấy stepSize + maxQty + min_notional từ Binance API
    step         = 1.0
    max_qty      = None
    decimals     = 0
    min_notional = 5.0
    if exchange and symbol:
        try:
            step, max_qty, decimals, min_notional = exchange.get_qty_precision(symbol)
        except Exception:
            pass

    # Fallback cap nếu không lấy được từ API
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

    # Hard cap: margin không vượt MAX_ORDER_USDT × LEVERAGE
    max_margin_qty = (config.MAX_ORDER_USDT * config.LEVERAGE) / entry
    qty = min(qty, max_margin_qty)

    # Round theo stepSize
    if step >= 1:
        qty = int(qty // step) * int(step)
    else:
        qty = round(int(qty / step) * step, decimals)

    # Đảm bảo notional >= min_notional (tránh lỗi 400)
    import math as _math
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

                # Chỉ check khi đang có lời >= 0.5%
                if pnl_pct < 0.5:
                    continue

                try:
                    # Lấy klines 1m để tính indicators
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

                    # 1. RSI đảo chiều
                    if side == "SHORT":
                        # SHORT đang lời: RSI đã xuống thấp rồi bật lên (oversold bounce)
                        if rsi_prev < 38 and rsi_now > rsi_prev + 4:
                            signals.append(f"RSI bounce {rsi_prev:.0f}→{rsi_now:.0f}")
                    else:
                        # LONG đang lời: RSI đã lên cao rồi quay xuống (overbought drop)
                        if rsi_prev > 62 and rsi_now < rsi_prev - 4:
                            signals.append(f"RSI drop {rsi_prev:.0f}→{rsi_now:.0f}")

                    # 2. EMA cross ngược chiều
                    if side == "SHORT" and ema9 > ema21:
                        signals.append(f"EMA cross UP (9>{21:.0f})")
                    elif side == "LONG" and ema9 < ema21:
                        signals.append(f"EMA cross DOWN (9<21)")

                    # 3. Pullback mạnh sau khi đã đi được lời tốt
                    if profit_travel >= 1.5 and pullback >= 35:
                        signals.append(f"Pullback {pullback:.0f}% sau khi profit_travel={profit_travel:.1f}%")

                    _prev_rsi[symbol] = rsi_now

                    # Cần >= 2 tín hiệu mới đóng (tránh false positive)
                    if len(signals) < 2:
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

                    actual_pnl = qty * (entry - cur_price) if side == "SHORT" else qty * (cur_price - entry)
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
    """
    import time as _time
    _time.sleep(30)  # Đợi bot ổn định
    logger.info("[ScanProtector] Started — protecting scan positions")

    _prev_rsi    = {}
    _max_price   = {}  # LONG
    _min_price   = {}  # SHORT
    _last_klines = {}  # cache klines để giảm API calls

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

                try:
                    # Dùng 15m thay vì 1m — ít noise hơn cho lệnh scan
                    klines = exchange.get_klines(symbol, "15m", limit=30)
                    df = _klines_to_df(klines)
                    if df is None or len(df) < 15:
                        continue

                    from indicators import calculate_rsi, calculate_ema
                    rsi_series = calculate_rsi(df["close"], 14)
                    rsi_now    = rsi_series.iloc[-1]
                    rsi_prev   = _prev_rsi.get(symbol, rsi_now)
                    ema9       = calculate_ema(df["close"], 9).iloc[-1]
                    ema21      = calculate_ema(df["close"], 21).iloc[-1]

                    # Track giá cực trị
                    if side == "LONG":
                        if symbol not in _max_price or mark_price > _max_price[symbol]:
                            _max_price[symbol] = mark_price
                        max_reached    = _max_price[symbol]
                        profit_travel  = (max_reached - entry) / entry * 100
                        pullback       = (max_reached - mark_price) / max(max_reached, 0.0001) * 100
                    else:
                        if symbol not in _min_price or mark_price < _min_price[symbol]:
                            _min_price[symbol] = mark_price
                        min_reached    = _min_price[symbol]
                        profit_travel  = (entry - min_reached) / entry * 100
                        pullback       = (mark_price - min_reached) / max(min_reached, 0.0001) * 100

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

                    # Không đóng nếu config tắt
                    if not getattr(config, "SCAN_PROTECT_ENABLED", True):
                        continue

                    # Không đóng nếu đang lỗ (đã bị reversal qua entry)
                    # Trường hợp đó để SL Binance tự xử lý
                    if pnl_pct <= 0:
                        continue

                    # ── Đóng position ──────────────────────────
                    qty        = abs(amt)
                    close_side = "SELL" if side == "LONG" else "BUY"
                    cur_price  = exchange.get_ticker_price(symbol)
                    actual_pnl = qty * (cur_price - entry) if side == "LONG" else qty * (entry - cur_price)
                    icon       = "✅" if actual_pnl >= 0 else "⚠️"

                    exchange.place_market_order(symbol, close_side, qty)
                    exchange.cancel_all_orders(symbol)

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
            exchange.place_market_order(symbol, "BUY", qty)
            return

        try:
            exchange.place_take_profit_order(symbol, "BUY", qty, sig.tp1_price)
        except Exception:
            pass

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
            exchange.place_market_order(symbol, "SELL", qty)
            return

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


def scan_engine(exchange, notifier):
    _spike_symbol = None   # coin được wake up sớm bởi spike detector

    while state["running"]:
        try:
            with lock:
                last_loss_time = state.get("last_loss_time", 0)
            cooldown = getattr(config, "COOLDOWN_AFTER_LOSS", 180)
            if time.time() - last_loss_time < cooldown:
                wait = int(cooldown - (time.time() - last_loss_time))
                logger.info(f"Cooldown sau lỗ: còn {wait}s")
                _spike_symbol = _wait_or_spike(config.LOOP_INTERVAL_SECONDS)
                continue

            with lock:
                n_open = len(state.get("open_positions", []))
            if n_open >= config.MAX_OPEN_POSITIONS:
                logger.info(f"Max positions ({n_open}/{config.MAX_OPEN_POSITIONS}), skip scan")
                _spike_symbol = _wait_or_spike(config.LOOP_INTERVAL_SECONDS)
                continue

            with lock:
                state["scan_no"] += 1
                state["last_scan"] = datetime.now().strftime("%H:%M")

            # ── Fast path: nếu spike detector phát hiện coin cụ thể ──────────
            # Chỉ scan coin đó ngay, không scan toàn bộ watchlist
            if _spike_symbol:
                logger.info(f"[ScanEngine] ⚡ Fast scan spike coin: {_spike_symbol}")
                _spike_symbol = _fast_spike_scan(_spike_symbol, exchange, notifier)
                # Reset và tiếp tục vòng lặp bình thường (không full scan ngay)
                _spike_symbol = None
                _spike_symbol = _wait_or_spike(config.LOOP_INTERVAL_SECONDS)
                continue

            best = scan_market(exchange, config, min_score=config.MIN_SCORE, notifier=notifier)
            with lock:
                state["candidates"] = list(getattr(scan_market, "_last_candidates", []))

            if best:
                with lock:
                    open_syms = {p["symbol"] for p in state.get("open_positions", [])
                                 if abs(float(p.get("positionAmt", 0))) > 0}
                if best.symbol in open_syms:
                    logger.info(f"Skip {best.symbol}: already has open position")
                    time.sleep(config.LOOP_INTERVAL_SECONDS)
                    continue
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
                df     = _klines_to_df(klines)
                price  = df["close"].iloc[-1]
                atr    = calculate_atr(df["high"], df["low"], df["close"]).iloc[-1]
                bal    = exchange.get_account_balance()
                try: exchange.set_leverage(best.symbol, config.LEVERAGE)
                except: pass

                liq_inst   = state.get("liq_tracker")
                side       = "BUY"  if best.signal == "LONG" else "SELL"
                close_side = "SELL" if best.signal == "LONG" else "BUY"
                entry_price = price
                sl = tp = 0.0
                order_type_used = "SKIP"
                skip_reason = None

                # Filter 1: Score >= 65
                if best.score < 65:
                    skip_reason = f"Score {best.score} < 65"

                # Filter 2: MACD momentum
                if not skip_reason:
                    try:
                        from indicators import calculate_macd
                        _, _, histogram = calculate_macd(df["close"])
                        hist_val = histogram.iloc[-1]
                        if best.signal == "LONG" and hist_val <= 0:
                            skip_reason = f"MACD hist={hist_val:.5f} (cần dương cho LONG)"
                        elif best.signal == "SHORT" and hist_val >= 0:
                            skip_reason = f"MACD hist={hist_val:.5f} (cần âm cho SHORT)"
                    except Exception:
                        pass

                # Filter Q1: Correlation — không vào 2 coin cùng nhóm cùng chiều
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

                # Filter Q2: Order Flow — delta/CVD xác nhận
                if not skip_reason:
                    try:
                        from quant_orderflow import get_orderflow_signal, orderflow_confirms
                        klines_of = exchange.get_klines(best.symbol, "15m", limit=50)
                        df_of     = _klines_to_df(klines_of)
                        of_result = get_orderflow_signal(df_of, bias=best.signal)
                        if not orderflow_confirms(of_result, best.signal):
                            skip_reason = (f"OrderFlow: {of_result['pressure']} ngược "
                                           f"{best.signal} | {of_result['reason']}")
                        else:
                            logger.info(f"[OF] {best.symbol}: {of_result['pressure']} "
                                        f"score={of_result['score']} CVD={of_result['cvd']:+.0f} "
                                        f"ratio={of_result['buy_ratio']:.0%}")
                    except Exception as _e:
                        logger.debug(f"OrderFlow skip: {_e}")

                # Filter Q3: Volume Profile — VWAP/POC xác nhận
                if not skip_reason:
                    try:
                        from quant_volume_profile import get_vp_signal, vp_confirms
                        klines_vp = exchange.get_klines(best.symbol, "1h", limit=100)
                        df_vp     = _klines_to_df(klines_vp)
                        vp_result = get_vp_signal(df_vp, bias=best.signal, window=50)
                        ok, vp_reason = vp_confirms(vp_result, best.signal)
                        if not ok:
                            skip_reason = f"VP: {vp_reason}"
                        else:
                            logger.info(f"[VP] {best.symbol}: {vp_result['price_vs']} "
                                        f"VWAP={vp_result['vwap']:.4f} POC={vp_result['poc']:.4f} "
                                        f"score={vp_result['score']}")
                    except Exception as _e:
                        logger.debug(f"VolumeProfile skip: {_e}")

                # Filter 3: Liquidity Cluster Entry
                if not skip_reason:
                    # Ưu tiên: websocket tracker (nếu có data)
                    # Fallback: REST API cache (có data ngay từ đầu)
                    liq_source = None
                    if liq_inst and liq_inst.is_connected() and liq_inst.total_liq_usd(best.symbol) > 0:
                        liq_source = liq_inst
                        logger.debug(f"[LiqSource] {best.symbol}: dùng WS tracker")
                    else:
                        liq_api = state.get("liq_api_cache")
                        if liq_api and liq_api.is_ready(best.symbol):
                            liq_source = liq_api
                            logger.debug(f"[LiqSource] {best.symbol}: dùng REST API cache")

                    if liq_source:
                        cur_price = exchange.get_ticker_price(best.symbol)

                        cluster = liq_source.get_best_entry_cluster(
                            symbol        = best.symbol,
                            current_price = cur_price,
                            direction     = best.signal,
                            min_usd       = 30_000,
                            cluster_gap_pct = 0.008,
                        )
                        if not cluster:
                            cluster = liq_source.get_best_entry_cluster(
                                symbol        = best.symbol,
                                current_price = cur_price,
                                direction     = best.signal,
                                min_usd       = 10_000,
                                cluster_gap_pct = 0.012,
                            )

                        if not cluster:
                            skip_reason = "Không tìm được cluster liq"
                        elif cluster["dist_pct"] > 10.0:
                            skip_reason = f"Cluster quá xa {cluster['dist_pct']:.1f}% > 10%"
                        else:
                            # ── Check giá đang TIẾN VỀ cluster không ────
                            # Lấy giá 3 nến 15m gần nhất để xem momentum
                            klines_check = exchange.get_klines(best.symbol, "15m", limit=5)
                            df_check = _klines_to_df(klines_check)
                            price_3ago = df_check["close"].iloc[-4]
                            price_now  = df_check["close"].iloc[-1]

                            if best.signal == "LONG":
                                # Giá đang giảm về cluster phía dưới → đúng hướng
                                price_moving_toward = price_now < price_3ago
                                # Hoặc giá đã ở gần cluster (trong 1%)
                                near_cluster = cluster["dist_pct"] <= 1.0
                            else:  # SHORT
                                # Giá đang tăng về cluster phía trên → đúng hướng
                                price_moving_toward = price_now > price_3ago
                                near_cluster = cluster["dist_pct"] <= 1.0

                            if not price_moving_toward and not near_cluster:
                                # Giá đang đi ngược → pending, chờ lần scan sau
                                _pending_watch.pop(best.symbol, None)
                                skip_reason = (f"Giá chưa tiến về cluster "
                                               f"({'↗' if price_now > price_3ago else '↘'} "
                                               f"vs cluster {cluster['dist_pct']:.1f}% away)")
                            else:
                                # ── Entry: tại ĐÚNG vùng liq ──────────────
                                # SHORT: entry = đáy cluster (giá pump lên chạm là vào)
                                # LONG:  entry = đỉnh cluster (giá dump xuống chạm là vào)
                                entry_price = cluster["entry"]

                                # ── SL: ngoài cluster + buffer ────────────
                                sl = cluster["sl_zone"]
                                if best.signal == "LONG":
                                    # Min SL 2% dưới entry, max 5%
                                    sl = round(max(sl, entry_price * 0.95), 8)
                                    if entry_price - sl < entry_price * 0.02:
                                        sl = round(entry_price * 0.98, 8)
                                else:
                                    # Min SL 2% trên entry, max 5%
                                    sl = round(min(sl, entry_price * 1.05), 8)
                                    if sl - entry_price < entry_price * 0.02:
                                        sl = round(entry_price * 1.02, 8)

                                # ── TP: cluster lớn nhất USD phía target ──
                                heatmap = liq_source.get_liq_heatmap(best.symbol) or {}
                                if best.signal == "LONG":
                                    tp_cluster = liq_source.get_best_entry_cluster(
                                        symbol        = best.symbol,
                                        current_price = entry_price,
                                        direction     = "SHORT",
                                        min_usd       = 10_000,
                                        cluster_gap_pct = 0.012,
                                    )
                                    if tp_cluster and tp_cluster["entry"] > entry_price:
                                        tp = round(tp_cluster["cluster_low"] * 0.999, 8)
                                    else:
                                        above = [(p, u) for p, u in heatmap.items()
                                                 if p > cur_price and u >= 10_000]
                                        if above:
                                            liq_tp = max(above, key=lambda x: x[1])[0]
                                            tp = round(liq_tp * 0.998, 8)
                                        else:
                                            tp = round(entry_price + (entry_price - sl) * 3.0, 8)
                                else:  # SHORT
                                    tp_cluster = liq_source.get_best_entry_cluster(
                                        symbol        = best.symbol,
                                        current_price = entry_price,
                                        direction     = "LONG",
                                        min_usd       = 10_000,
                                        cluster_gap_pct = 0.012,
                                    )
                                    if tp_cluster and tp_cluster["entry"] < entry_price:
                                        tp = round(tp_cluster["cluster_high"] * 1.001, 8)
                                    else:
                                        below = [(p, u) for p, u in heatmap.items()
                                                 if p < cur_price and u >= 10_000]
                                        if below:
                                            liq_tp = min(below, key=lambda x: -x[1])[0]
                                            tp = round(liq_tp * 1.002, 8)
                                        else:
                                            tp = round(entry_price - (sl - entry_price) * 3.0, 8)

                                # ── Validate RR ──────────────────────────
                                risk   = abs(entry_price - sl)
                                reward = abs(tp - entry_price)
                                rr = reward / risk if risk > 0 else 0

                                if rr < 1.5:
                                    skip_reason = f"RR={rr:.1f} < 1.5"
                                else:
                                    order_type_used = "LIMIT"
                                    sweep_done = (
                                        df["low"].iloc[-1]  <= cluster["cluster_low"]  * 1.002
                                        if best.signal == "LONG" else
                                        df["high"].iloc[-1] >= cluster["cluster_high"] * 0.998
                                    )
                                    mode = "SWEEP" if sweep_done else ("TOWARD" if price_moving_toward else "NEAR")
                                    logger.info(
                                        f"[ClusterEntry] {best.signal} {best.symbol} {mode} | "
                                        f"cluster=[{cluster['cluster_low']:.4f}-{cluster['cluster_high']:.4f}] "
                                        f"${cluster['total_usd']/1e3:.0f}k | "
                                        f"entry={entry_price:.6f} dist={cluster['dist_pct']:.1f}% | "
                                        f"SL={sl:.6f} TP={tp:.6f} RR=1:{rr:.1f} | "
                                        f"price {'↘' if price_now < price_3ago else '↗'} toward={'Y' if price_moving_toward else 'N'}"
                                    )

                    else:
                        # Cả WS lẫn REST API cache đều không có data → ATR fallback
                        if best.score >= 65:
                            try:
                                from auto_sltp import suggest_sltp
                                suggestion = suggest_sltp(
                                    exchange, best.symbol, best.signal,
                                    price, liq_tracker=None
                                )
                                sl = suggestion["sl"]
                                tp = suggestion["tp"]
                                logger.info(
                                    f"[FallbackSLTP] {best.symbol} {best.signal} | "
                                    f"SL={sl} TP={tp} RR=1:{suggestion['rr']} | {suggestion['method']}"
                                )
                            except Exception as _e:
                                logger.warning(f"[FallbackSLTP] suggest_sltp failed: {_e}, using ATR")
                                if best.signal == "LONG":
                                    sl = price - max(atr * 2.0, price * config.STOP_LOSS_PCT)
                                    tp = price + (price - sl) * 3.0
                                else:
                                    sl = price + max(atr * 2.0, price * config.STOP_LOSS_PCT)
                                    tp = price - (sl - price) * 3.0
                            order_type_used = "MARKET"
                        else:
                            skip_reason = f"Không có liq data và score {best.score} < 70"

                if skip_reason or order_type_used == "SKIP":
                    logger.info(f"[Sweep] SKIP {best.symbol} {best.signal}: {skip_reason}")
                    time.sleep(config.LOOP_INTERVAL_SECONDS)
                    continue

                # ── 2 lệnh LIMIT đồng thời ──────────────────────────────
                # Lệnh 1 (near zone): cluster gần nhất                → 50% size
                # Lệnh 2 (deep zone): cluster tối ưu đã tính ở trên  → 100% size
                cur_p = exchange.get_ticker_price(best.symbol)

                if best.signal == "LONG":
                    near_cluster = (liq_inst.get_best_entry_cluster(
                        best.symbol, cur_p, "LONG",
                        min_usd=10_000, cluster_gap_pct=0.012
                    ) if (liq_inst and liq_inst.is_connected()) else None)
                    near_zone = (round(near_cluster["entry"], 8)
                                 if near_cluster and near_cluster["entry"] > entry_price
                                 else None)
                    deep_zone = entry_price
                else:
                    near_cluster = (liq_inst.get_best_entry_cluster(
                        best.symbol, cur_p, "SHORT",
                        min_usd=10_000, cluster_gap_pct=0.012
                    ) if (liq_inst and liq_inst.is_connected()) else None)
                    near_zone = (round(near_cluster["entry"], 8)
                                 if near_cluster and near_cluster["entry"] < entry_price
                                 else None)
                    deep_zone = entry_price

                # Tính qty full (cho deep zone = $20)
                qty_full = calc_qty(bal, entry_price, sl, symbol=best.symbol, exchange=exchange)
                if qty_full * entry_price < 5.0:
                    qty_full = round(5.0 / entry_price + 0.001, 3)

                orders_placed = []

                # ── Lệnh 1: near zone, 50% size ──
                if near_zone and near_zone != deep_zone:
                    if best.signal == "LONG":
                        near_entry = round(near_zone * 1.001, 8)
                    else:
                        near_entry = round(near_zone * 0.999, 8)
                    qty_near = round(qty_full * 0.5, 8)
                    if qty_near * near_entry >= 5.0:
                        try:
                            exchange.place_limit_order(best.symbol, side, qty_near, near_entry)
                            orders_placed.append(f"NEAR ${near_entry:.6f} qty={qty_near} (50%)")
                            logger.info(f"[DualLimit] NEAR {side} {best.symbol} @ {near_entry:.6f} qty={qty_near}")
                        except Exception as e:
                            logger.error(f"[DualLimit] NEAR order failed: {e}")

                # ── Lệnh 2: deep zone, full size ──
                qty_deep = qty_full
                try:
                    exchange.place_limit_order(best.symbol, side, qty_deep, deep_zone)
                    orders_placed.append(f"DEEP ${deep_zone:.6f} qty={qty_deep} (100%)")
                    logger.info(f"[DualLimit] DEEP {side} {best.symbol} @ {deep_zone:.6f} qty={qty_deep}")
                    order_type_used = "LIMIT"
                except Exception as e:
                    logger.error(f"[DualLimit] DEEP order failed → MARKET: {e}")
                    exchange.place_market_order(best.symbol, side, qty_deep)
                    order_type_used = "MARKET"

                if not orders_placed:
                    # fallback market
                    exchange.place_market_order(best.symbol, side, qty_full)
                    order_type_used = "MARKET"

                qty = qty_full  # dùng qty_full cho SL/TP

                # ── SL/TP: chỉ đặt ngay khi MARKET order ────────────
                # LIMIT order: chờ limit_order_monitor phát hiện khớp → đặt SL/TP
                # Tránh duplicate: không đặt SL/TP trước khi lệnh khớp
                if order_type_used == "MARKET":
                    time.sleep(1)
                    try: exchange.place_stop_loss_order(best.symbol, close_side, qty, sl)
                    except Exception as e: logger.error(f"SL failed: {e}")
                    try: exchange.place_take_profit_order(best.symbol, close_side, qty, tp)
                    except Exception as e: logger.error(f"TP failed: {e}")
                else:
                    # LIMIT: lưu order IDs + sl/tp vào pending_smart_orders để limit_order_monitor xử lý
                    # limit_order_monitor sẽ poll từng order_id, khi FILLED → đặt SL/TP ngay
                    try:
                        open_orders = exchange._get("/fapi/v1/openOrders",
                                                    {"symbol": best.symbol}, signed=True)
                        # Lấy tất cả LIMIT entry orders vừa đặt (non-reduceOnly)
                        entry_orders = [o for o in open_orders
                                        if not o.get("reduceOnly", False)
                                        and o.get("type") == "LIMIT"]
                        with lock:
                            psm = state.setdefault("pending_smart_orders", {})
                            for o in entry_orders:
                                oid = str(o["orderId"])
                                psm[oid] = {
                                    "symbol":   best.symbol,
                                    "side":     best.signal,
                                    "qty":      float(o.get("origQty", qty)),
                                    "sl":       sl,
                                    "tp":       tp,
                                    "ts":       time.time(),
                                }
                        logger.info(f"[SL/TP] {len(entry_orders)} LIMIT order(s) registered → "
                                    f"limit_order_monitor sẽ đặt SL={sl} TP={tp} khi fill")
                    except Exception as _e:
                        logger.error(f"[SL/TP] Failed to register pending orders: {_e}")

                with lock:
                    state["position"]  = best.signal
                    state["symbol"]    = best.symbol
                    state["entry"]     = entry_price
                    state["sl"]        = sl
                    state["tp"]        = tp
                    state["qty"]       = qty
                    state["trail_ext"] = entry_price
                    state["trade_log"].append({
                        "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "symbol": best.symbol, "side": best.signal,
                        "entry":  entry_price, "sl": sl, "tp": tp,
                        "qty":    qty, "status": "OPEN",
                        "note":   f"scan_{order_type_used.lower()}"
                    })

                icon      = "🟢" if best.signal == "LONG" else "🔴"
                margin    = qty * entry_price / config.LEVERAGE
                order_tag = "⏳ DUAL LIMIT" if order_type_used == "LIMIT" else "⚡ MARKET"
                rr_actual = abs(tp - entry_price) / abs(entry_price - sl) if abs(entry_price - sl) > 0 else 0
                orders_str = "\n".join([f"  📌 {o}" for o in orders_placed]) if orders_placed else f"  📌 {entry_price:.6f}"
                notifier.telegram.send(
                    f"{icon} <b>🤖 AUTO | {best.signal} {best.symbol}</b> [{order_tag}]\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"{orders_str}\n"
                    f"🛑 SL     : <b>${sl:.6f}</b>  ({abs(entry_price-sl)/entry_price*100:.2f}%)\n"
                    f"🎯 TP     : <b>${tp:.6f}</b>  ({abs(tp-entry_price)/entry_price*100:.2f}%)\n"
                    f"📐 RR     : <b>1:{rr_actual:.1f}</b>\n"
                    f"💵 Full   : ${qty_full*entry_price:,.2f} | Half: ${qty_full*0.5*entry_price:,.2f}\n"
                    f"⭐ Score  : {best.score}đ | {best.reason}\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                )
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Scan engine: {e}", exc_info=True)
            notifier.telegram.send(f"⚠️ Bot error: {e}")
            time.sleep(60)

        # ── Interruptible sleep: thay vì sleep 60s cứng,
        # check giá WS mỗi 2s — nếu coin nào spike mạnh thì wake up ngay ──
        _spike_wake = _wait_or_spike(config.LOOP_INTERVAL_SECONDS, check_interval=2)
        if _spike_wake:
            logger.info(f"[ScanEngine] ⚡ Spike detected: {_spike_wake} — wake up early")

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
                detector.cfg["PUMP_TOP_MIN_SCORE"] = 60
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
                        # Telegram alert ngay
                        try:
                            notifier.telegram.send(sig.to_telegram())
                        except Exception as te:
                            logger.warning(f"[PumpEngine] Telegram failed: {te}")

                except Exception as e:
                    logger.debug(f"[PumpEngine] {symbol} scan error: {e}")

            # ── Cũng quét FIXED_COINS nhưng chậm hơn (mỗi slow_interval) ──
            # Chỉ chạy khi interval == slow_interval (không có pump coins)
            # hoặc mỗi 6 vòng (30s) khi đang chạy 5s
            with lock:
                _scan_tick = state.get("_pump_tick", 0) + 1
                state["_pump_tick"] = _scan_tick

            fixed_coins = [c for c in list(getattr(config, "FIXED_COINS", WATCHLIST))
                           if c not in pump_coins]
            should_scan_fixed = (_scan_tick % max(1, slow_interval // interval) == 0)

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
                                notifier.telegram.send(sig.to_telegram())
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

                for symbol in pump_shorts:
                    # Tìm signal vừa scan cho coin này
                    sig_dict = next(
                        (s for s in reversed(state.get("pump_signals", []))
                         if s.get("symbol") == symbol), None
                    )
                    if not sig_dict:
                        continue

                    pump_pct  = sig_dict.get("pump_pct", 0)
                    score     = sig_dict.get("score", 0)

                    # Điều kiện đóng SHORT sớm khi có dấu hiệu pump lên lại:
                    # - Đang có lời (giá < entry): đóng ngay khi pump_pct >= 3% hoặc score >= 25
                    # - Chưa có lời / đang lỗ ít: đóng khi pump_pct >= 7% và score >= 40 (tránh noise)
                    try:
                        cur_price = exchange.get_ticker_price(symbol)
                        pos_entry = next(
                            (float(p.get("entryPrice", 0)) for p in open_positions
                             if p["symbol"] == symbol), 0
                        )
                        in_profit = pos_entry > 0 and cur_price < pos_entry

                        if in_profit:
                            # Đang có lời → nhạy hơn, bắt đảo chiều sớm
                            should_exit = pump_pct >= 3.0 or score >= 25
                        else:
                            # Chưa có lời → cần tín hiệu mạnh hơn mới đóng
                            should_exit = pump_pct >= 7.0 and score >= 40
                    except Exception:
                        should_exit = pump_pct >= 5.0 and score >= 30

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

                            # Đóng SHORT → BUY
                            exchange.place_market_order(symbol, "BUY", qty)
                            exchange.cancel_all_orders(symbol)

                            pnl = qty * (entry - close_price)

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

                            icon = "✅" if pnl >= 0 else "⚠️"
                            profit_tag = "Chốt lời" if pnl >= 0 else "Cắt lỗ sớm"
                            notifier.telegram.send(
                                f"🔄 <b>PUMP REVERSAL EXIT — {profit_tag}</b>\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"🪙 {symbol} có dấu hiệu pump lên lại +{pump_pct:.1f}%\n"
                                f"⚡ Đóng SHORT ngay trước khi bị squeeze\n"
                                f"💰 Entry: ${entry:.6f} → Close: ${close_price:.6f}\n"
                                f"{icon} PnL: <b>${pnl:+.2f}</b>\n"
                                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                            )
                            logger.info(
                                f"[PumpEngine] REVERSAL EXIT: {symbol} "
                                f"pump={pump_pct:.1f}% pnl=${pnl:+.2f}"
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
                                logger.warning(f"[PumpEngine] SL attempt {_attempt+1} {symbol}: {e}")
                                time.sleep(0.5)
                        if not sl_ok:
                            logger.error(f"[PumpEngine] ⚠️ SL FAILED after 3 attempts for {symbol} — closing position for safety")
                            try:
                                exchange.place_market_order(symbol, "BUY", qty)  # đóng ngay nếu không đặt được SL
                            except Exception as ce:
                                logger.error(f"[PumpEngine] Emergency close failed: {ce}")
                            continue

                        # Đặt TP (không bắt buộc, lỗi thì bỏ qua)
                        try:
                            exchange.place_take_profit_order(symbol, "BUY", qty, sig.tp1_price)
                        except Exception as e:
                            logger.warning(f"[PumpEngine] TP {symbol}: {e}")

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

                            time.sleep(0.5)
                            try:
                                exchange.place_stop_loss_order(sym, "BUY", qty, sl_price)
                                logger.info(f"[PumpLimit] SL placed: {sym} @ {sl_price}")
                            except Exception as e:
                                logger.error(f"[PumpLimit] SL failed {sym}: {e}")
                            try:
                                exchange.place_take_profit_order(sym, "BUY", qty, tp_price)
                                logger.info(f"[PumpLimit] TP placed: {sym} @ {tp_price}")
                            except Exception as e:
                                logger.error(f"[PumpLimit] TP failed {sym}: {e}")

                            fill_price = float(pos.get("entryPrice", info["entry_price"]))
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

                        elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                            logger.info(f"[LimitMonitor] {info['symbol']} order {status}, removing")
                            with lock:
                                state.get("pending_smart_orders", {}).pop(str(order_id), None)

                    except Exception as e:
                        logger.debug(f"[LimitMonitor] Check order {order_id}: {e}")

            # ── B. Auto SL/TP cho positions mới (mỗi 30s) ──
            # Chỉ đặt nếu position THỰC SỰ không có SL/TP trên Binance
            # VÀ không có pending entry order chưa khớp (tránh đặt trùng)
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

                    for pos in unprotected:
                        sym = pos["symbol"]
                        # Bỏ qua nếu còn pending entry order chưa khớp
                        if sym in pending_entry_syms:
                            logger.debug(f"[AutoSLTP] Skip {sym}: còn pending entry order")
                            continue
                        logger.info(f"[AutoSLTP] Detected unprotected: {sym} {pos['side']}")
                        result = auto_set_sltp(exchange, sym, pos["side"],
                                               pos["entry"], pos["qty"], liq_tracker)
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

                    # Auto cancel entry orders nếu được bật
                    if (sym and sym not in open_syms
                            and not o.get("reduceOnly", False)
                            and state.get("auto_cancel_orphan", False)):
                        try:
                            exchange._delete("/fapi/v1/order", {"symbol": sym, "orderId": o.get("orderId")})
                            cancelled.append(f"{sym} ({o.get('type','')} entry-orphan)")
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
        state["pump_signals"]      = []
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
