# ============================================================
# MULTI-COIN TRADING BOT — Dashboard + Auto Trade
# ============================================================
import time, logging, os, sys, threading
import pandas as pd
from datetime import datetime

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
}
lock = threading.Lock()

# ============================================================
# DASHBOARD
# ============================================================
def clear(): os.system("clear")

def print_dashboard():
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
    sys.stdout.flush()

# ============================================================
# THREAD 0: Dashboard refresh mỗi 1 giây (độc lập)
# ============================================================
def dashboard_updater():
    while state["running"]:
        try:
            print_dashboard()
        except Exception:
            pass
        time.sleep(1)

# ============================================================
# THREAD 1: Giá realtime mỗi 3 giây
# ============================================================
def price_updater(exchange):
    consecutive_errors = 0
    while state["running"]:
        try:
            new_prices = {}
            for sym in WATCHLIST:
                try: new_prices[sym] = exchange.get_ticker_price(sym)
                except: pass
            consecutive_errors = 0  # reset khi thành công

            # Lấy tất cả positions đang mở từ Binance
            try:
                all_pos = exchange._get("/fapi/v2/positionRisk", signed=True)
                open_pos = [p for p in all_pos if abs(float(p.get("positionAmt", 0))) > 0]
                for p in open_pos:
                    sym   = p["symbol"]
                    amt   = float(p.get("positionAmt", 0))
                    entry = float(p.get("entryPrice", 0))
                    mark  = new_prices.get(sym, float(p.get("markPrice", entry)))
                    lev   = int(float(p.get("leverage", config.LEVERAGE)))
                    side  = "LONG" if amt > 0 else "SHORT"
                    pnl   = abs(amt) * (mark - entry) if side == "LONG" else abs(amt) * (entry - mark)
                    pct   = (mark - entry) / entry * 100 if side == "LONG" else (entry - mark) / entry * 100
                    p["_mark"] = mark
                    p["_pnl"]  = pnl
                    p["_pct"]  = pct
                    p["_lev"]  = lev
            except:
                open_pos = []

            with lock:
                state["prices"].update(new_prices)
                state["balance"] = exchange.get_account_balance()
                state["open_positions"] = open_pos

        except Exception as e:
            consecutive_errors += 1
            wait = min(30, 5 * consecutive_errors)
            logger.error(f"Price updater: {e} — retry in {wait}s ({consecutive_errors} errors)")
            time.sleep(wait)
            continue
        time.sleep(3)

# ============================================================
# THREAD 2: Trade engine mỗi 60 giây
# ============================================================
def calc_qty(balance, entry, sl):
    risk = balance * config.RISK_PER_TRADE
    dist = abs(entry - sl)
    if dist == 0: return 0.1
    qty_by_risk = risk / dist

    # MAX_ORDER_USDT = margin tối đa → notional = margin × leverage
    max_notional = config.MAX_ORDER_USDT * config.LEVERAGE
    qty_by_cap   = max_notional / entry

    qty = min(qty_by_risk, qty_by_cap)
    # Đảm bảo notional tối thiểu $5
    if qty * entry < 5.0:
        qty = 5.0 / entry
    # Hard cap: margin không vượt MAX_ORDER_USDT
    if qty * entry / config.LEVERAGE > config.MAX_ORDER_USDT:
        qty = config.MAX_ORDER_USDT * config.LEVERAGE / entry
    return max(round(qty, 3), 0.001)

def trade_engine(exchange, notifier):
    # Startup noti
    bal = exchange.get_account_balance()
    with lock: state["balance"] = bal
    notifier.telegram.send(
        f"🚀 <b>MULTI-COIN BOT STARTED</b>\n"
        f"💼 Balance: <b>${bal:,.2f} USDT</b>\n"
        f"⚡ Leverage: <b>{config.LEVERAGE}x</b>\n"
        f"📊 Scanning <b>{len(WATCHLIST)} coins</b> mỗi {config.LOOP_INTERVAL_SECONDS}s\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}"
    )

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
                # Không vào lệnh trùng symbol đang mở
                with lock:
                    current_sym = state.get("symbol")
                if best.symbol == current_sym:
                    time.sleep(config.LOOP_INTERVAL_SECONDS)
                    continue

                klines = exchange.get_klines(best.symbol, config.INTERVAL, limit=200)
                df = _klines_to_df(klines)
                price = df["close"].iloc[-1]
                atr = calculate_atr(df["high"], df["low"], df["close"]).iloc[-1]
                bal = exchange.get_account_balance()

                try: exchange.set_leverage(best.symbol, config.LEVERAGE)
                except: pass

                if best.signal == "LONG":
                    sl = price - max(atr * 1.5, price * config.STOP_LOSS_PCT)
                    tp = price + (price - sl) * 3   # RR 1:3
                    side = "BUY"
                else:
                    sl = price + max(atr * 1.5, price * config.STOP_LOSS_PCT)
                    tp = price - (sl - price) * 3   # RR 1:3
                    side = "SELL"

                qty = calc_qty(bal, price, sl)
                # Đảm bảo notional tối thiểu $5 (Binance demo requirement)
                min_notional = 5.0
                if qty * price < min_notional:
                    qty = round(min_notional / price + 0.001, 3)
                exchange.place_market_order(best.symbol, side, qty)

                with lock:
                    state["position"]  = best.signal
                    state["symbol"]    = best.symbol
                    state["entry"]     = price
                    state["sl"]        = sl
                    state["tp"]        = tp
                    state["qty"]       = qty
                    state["trail_ext"] = price
                    state["trade_log"].append({
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "symbol": best.symbol, "side": best.signal,
                        "entry": price, "sl": sl, "tp": tp,
                        "qty": qty, "status": "OPEN"
                    })

                icon = "🟢" if best.signal=="LONG" else "🔴"
                margin = qty * price / config.LEVERAGE
                notifier.telegram.send(
                    f"{icon} <b>{best.signal} {best.symbol}</b>\n"
                    f"💰 Entry  : <b>${price:.4f}</b>\n"
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

    # Auto-start grid cho top 5 coin, mỗi coin $10, 10 lưới, range ±2%
    GRID_COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]
    for sym in GRID_COINS:
        try:
            price = exchange.get_ticker_price(sym)
            lower = round(price * 0.98, 4)
            upper = round(price * 1.02, 4)
            from grid_strategy import GridBot
            g = GridBot(sym, lower, upper, 10, 10, exchange, notifier)
            g.setup(price)
            state["grids"][sym] = g
            logger.info(f"Auto-started grid {sym}: ${lower}-${upper}")
        except Exception as e:
            logger.warning(f"Auto grid {sym} skipped: {e}")

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        state["running"] = False
        clear()
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
        # Dừng tất cả grids
        for g in state.get("grids", {}).values():
            try: g.stop()
            except: pass
        # Đóng position nếu đang mở
        with lock:
            sym = state["symbol"]
            pos = state["position"]
            qty = state["qty"]
        if pos and sym and qty:
            try:
                close_side = "SELL" if pos == "LONG" else "BUY"
                exchange.place_market_order(sym, close_side, qty)
                print(f"✅ Đã đóng lệnh {sym}")
            except Exception as e:
                print(f"⚠️ Không đóng được lệnh: {e}")
