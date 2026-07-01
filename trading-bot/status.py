#!/usr/bin/env python3
# ============================================================
# DASHBOARD — Multi-coin | LONG/SHORT | Position | History
# ============================================================
import time, os, sys
sys.path.insert(0, os.path.dirname(__file__))

import config
from exchange import BinanceFutures
from scanner import scan_market, score_coin, _klines_to_df, WATCHLIST
from trade_history import load_history, get_stats

# ── Colors ───────────────────────────────────────────────────
def c(text, code): return f"\033[{code}m{text}\033[0m"
G  = lambda t: c(t, "92")   # green
R  = lambda t: c(t, "91")   # red
Y  = lambda t: c(t, "93")   # yellow
CY = lambda t: c(t, "96")   # cyan
B  = lambda t: c(t, "1")    # bold
DIM= lambda t: c(t, "2")    # dim

W = 62  # box width

def box_line(text="", fill="─"):
    pad = W - 2 - len(_strip(text))
    return f"║ {text}{' ' * max(pad,0)} ║"

def _strip(s):
    import re
    return re.sub(r'\033\[[0-9;]*m', '', s)

def header(title):
    t = f"  {title}  "
    side = (W - len(t)) // 2
    return f"╠{'═'*side}{t}{'═'*(W-side-len(t))}╣"

def top():    return f"╔{'═'*W}╗"
def bottom(): return f"╚{'═'*W}╝"
def divider(): return f"╠{'═'*W}╣"

def main():
    ex = BinanceFutures(config.API_KEY, config.API_SECRET, config.USE_TESTNET)
    scan_count = 0

    while True:
        try:
            os.system('clear')
            now = time.strftime("%H:%M:%S")

            # ── Lấy data ──────────────────────────────────────
            balance = ex.get_account_balance()
            history = load_history()
            stats   = get_stats(history)

            # Scan tất cả coins
            longs, shorts = [], []
            for symbol in WATCHLIST:
                try:
                    klines = ex.get_klines(symbol, config.INTERVAL, limit=200)
                    df     = _klines_to_df(klines)
                    scored = score_coin(symbol, df, config)
                    if scored and scored.score >= 55:
                        if scored.signal == "LONG":
                            longs.append(scored)
                        elif scored.signal == "SHORT":
                            shorts.append(scored)
                except Exception:
                    pass

            longs.sort(key=lambda x: x.score, reverse=True)
            shorts.sort(key=lambda x: x.score, reverse=True)
            scan_count += 1

            # ── RENDER ────────────────────────────────────────
            print(top())
            print(box_line(B(f"  🤖 MULTI-COIN BOT DASHBOARD  —  {now}")))
            print(box_line(
                f"  💼 Balance: {G(f'${balance:,.2f}')}  "
                f"│  📊 Scan #{scan_count}  "
                f"│  🎯 WR: {G(str(stats['winrate'])+'%') if stats['winrate']>=50 else R(str(stats['winrate'])+'%')}"
            ))
            print(divider())

            # ── POSITION ĐANG MỞ ──────────────────────────────
            print(box_line(B("  📌  CÁC LỆNH ĐANG MỞ")))

            # Lấy tất cả positions từ Binance
            import requests as req, hmac, hashlib
            from urllib.parse import urlencode
            params = {'timestamp': int(time.time()*1000)}
            qs  = urlencode(params)
            sig = hmac.new(config.API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
            params['signature'] = sig
            sess = req.Session()
            sess.headers.update({'X-MBX-APIKEY': config.API_KEY})
            r = sess.get('https://demo-fapi.binance.com/fapi/v2/positionRisk', params=params)
            all_positions = [p for p in r.json() if float(p.get('positionAmt', 0)) != 0]

            if all_positions:
                total_pnl = sum(float(p['unRealizedProfit']) for p in all_positions)
                total_pnl_c = G(f"+${total_pnl:.2f}") if total_pnl >= 0 else R(f"${total_pnl:.2f}")
                print(box_line(f"  {len(all_positions)} lệnh đang mở  │  Tổng PnL: {total_pnl_c}"))
                print(box_line("  " + "─"*56))
                for p in all_positions:
                    sym   = p['symbol']
                    amt   = float(p['positionAmt'])
                    entry = float(p['entryPrice'])
                    pnl   = float(p['unRealizedProfit'])
                    mark  = float(p.get('markPrice', entry))
                    side  = "▲LONG " if amt > 0 else "▼SHORT"
                    side_c= G(side) if amt > 0 else R(side)
                    pnl_c = G(f"+${pnl:.2f}") if pnl >= 0 else R(f"${pnl:.2f}")
                    pnl_pct = (mark-entry)/entry*100 if amt > 0 else (entry-mark)/entry*100
                    pct_c = G(f"+{pnl_pct:.2f}%") if pnl_pct >= 0 else R(f"{pnl_pct:.2f}%")
                    print(box_line(
                        f"  {side_c} {CY(sym):<14}  "
                        f"Entry:{B(f'${entry:.4f}')}  "
                        f"Now:{B(f'${mark:.4f}')}  "
                        f"{pnl_c}({pct_c})"
                    ))
            else:
                print(box_line(f"  {Y('  Không có lệnh nào đang mở')}"))
            print(divider())

            # ── TOP LONG ──────────────────────────────────────
            print(box_line(G(B("  🟢  TOP LONG SIGNALS"))))
            if longs:
                for s in longs[:5]:
                    bar = "█" * int(s.score/10) + "░" * (10 - int(s.score/10))
                    line = f"  {s.symbol:<12} ▲LONG   [{bar}] {s.score:.0f}đ  RSI={s.rsi}"
                    print(box_line(G(line) if s.score >= 70 else box_line(line)[2:-2]))
            else:
                print(box_line(DIM("  Chưa có tín hiệu LONG đủ điểm")))
            print(divider())

            # ── TOP SHORT ─────────────────────────────────────
            print(box_line(R(B("  🔴  TOP SHORT SIGNALS"))))
            if shorts:
                for s in shorts[:5]:
                    bar = "█" * int(s.score/10) + "░" * (10 - int(s.score/10))
                    line = f"  {s.symbol:<12} ▼SHORT  [{bar}] {s.score:.0f}đ  RSI={s.rsi}"
                    print(box_line(R(line) if s.score >= 70 else line))
            else:
                print(box_line(DIM("  Chưa có tín hiệu SHORT đủ điểm")))
            print(divider())

            # ── THỐNG KÊ ──────────────────────────────────────
            print(box_line(B("  📈  THỐNG KÊ GIAO DỊCH")))
            if stats["total"] > 0:
                print(box_line(
                    f"  Tổng: {B(str(stats['total']))}  │  "
                    f"✅ {G(str(stats['win']))}  │  "
                    f"❌ {R(str(stats['loss']))}  │  "
                    f"WR: {G(str(stats['winrate'])+'%')}"
                ))
                pnl_c = G(f"+${stats['total_pnl']}") if stats['total_pnl'] >= 0 else R(f"${stats['total_pnl']}")
                print(box_line(
                    f"  PnL: {pnl_c}  │  "
                    f"Best: {G('+$'+str(stats['best']))}  │  "
                    f"Worst: {R('-$'+str(abs(stats['worst'])))}"
                ))
            else:
                print(box_line(DIM("  Chưa có lệnh nào được đóng")))
            print(divider())

            # ── LỊCH SỬ LỆNH ──────────────────────────────────
            print(box_line(B("  🕐  LỊCH SỬ LỆNH GẦN NHẤT")))
            recent = history[-5:][::-1] if history else []
            if recent:
                for t in recent:
                    icon  = "✅" if t["result"] == "WIN" else "❌"
                    pnl_c = G(f"+${t['pnl_usd']}") if t['pnl_usd'] >= 0 else R(f"${t['pnl_usd']}")
                    line  = f"  {icon} {t['symbol']:<10} {t['side']:<5} {pnl_c}  {DIM(t['time'])}"
                    print(box_line(line))
            else:
                print(box_line(DIM("  Chưa có lệnh nào")))

            print(bottom())
            print(f"  {DIM('Refresh mỗi 30s  |  Ctrl+C để thoát')}")

            time.sleep(30)

        except KeyboardInterrupt:
            print("\nDashboard stopped.")
            break
        except Exception as e:
            print(R(f"Error: {e}"))
            time.sleep(10)

if __name__ == "__main__":
    main()
