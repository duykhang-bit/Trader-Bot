# ============================================================
# WEB DASHBOARD — Real-time Trading Bot Dashboard
# Chạy tại http://localhost:5555
# Auto-refresh mỗi 3 giây
# ============================================================
import threading
import logging
from datetime import datetime
from flask import Flask, jsonify, render_template_string

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["TESTING"] = False

# Sẽ được set từ bot.py
_state = None
_lock = None
_config = None

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    background: #0d1117;
    color: #c9d1d9;
    min-height: 100vh;
}
.container { max-width: 1200px; margin: 0 auto; padding: 16px; }

/* Header */
.header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 16px 20px;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 12px;
    margin-bottom: 16px;
}
.header h1 { font-size: 18px; color: #58a6ff; }
.header .status { display: flex; gap: 16px; align-items: center; font-size: 13px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 4px; }
.dot-green { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
.dot-red { background: #f85149; box-shadow: 0 0 6px #f85149; }
.dot-yellow { background: #d29922; box-shadow: 0 0 6px #d29922; }

/* Stats Cards */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 16px; }
.card {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 16px; text-align: center;
}
.card .label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }
.card .value { font-size: 24px; font-weight: bold; margin-top: 4px; }
.green { color: #3fb950; }
.red { color: #f85149; }
.blue { color: #58a6ff; }

/* Tables */
.section {
    background: #161b22; border: 1px solid #30363d; border-radius: 10px;
    padding: 16px; margin-bottom: 16px;
}
.section h2 { font-size: 14px; color: #58a6ff; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #30363d; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; padding: 8px; color: #8b949e; border-bottom: 1px solid #30363d; font-weight: 600; }
td { padding: 6px 8px; border-bottom: 1px solid #21262d; }
tr:hover { background: #1c2128; }
.badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;
}
.badge-long { background: rgba(63,185,80,0.15); color: #3fb950; }
.badge-short { background: rgba(248,81,73,0.15); color: #f85149; }

/* Prices Grid */
.prices-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }
.price-item {
    background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
    padding: 10px; text-align: center;
}
.price-item .coin { font-size: 11px; color: #8b949e; }
.price-item .price { font-size: 15px; font-weight: bold; color: #c9d1d9; margin-top: 2px; }

/* Liq Info */
.liq-bar { height: 6px; background: #21262d; border-radius: 3px; overflow: hidden; margin-top: 4px; }
.liq-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }

/* Footer */
.footer { text-align: center; color: #484f58; font-size: 11px; padding: 16px; }

/* Responsive */
@media (max-width: 768px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
    .prices-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
</head>
<body>
<div class="container" id="app">
    <div class="header">
        <h1>&#x1F916; Trading Bot Dashboard</h1>
        <div class="status">
            <span><span class="dot dot-green"></span> Live</span>
            <span id="clock">--:--:--</span>
        </div>
    </div>
    <div id="content">Loading...</div>
</div>

<script>
function fmt(n, d=2) { return Number(n).toFixed(d); }
function fmtUsd(n) { return '$' + Number(n).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}); }
function pnlColor(n) { return n >= 0 ? 'green' : 'red'; }
function pnlIcon(n) { return n >= 0 ? '&#x1F4C8;' : '&#x1F4C9;'; }
function sideHtml(s) { return s === 'LONG' ? '<span class="badge badge-long">LONG</span>' : '<span class="badge badge-short">SHORT</span>'; }

function renderDashboard(d) {
    const bal = d.balance;
    const todayPnl = d.today_pnl;
    const totalPnl = d.total_pnl;
    const unrealized = d.unrealized;
    const wr = d.win_rate;
    const trades = d.total_trades;

    let html = `
    <div class="stats">
        <div class="card"><div class="label">Balance</div><div class="value blue">${fmtUsd(bal)}</div></div>
        <div class="card"><div class="label">Today PnL</div><div class="value ${pnlColor(todayPnl)}">${fmtUsd(todayPnl)}</div></div>
        <div class="card"><div class="label">Total PnL</div><div class="value ${pnlColor(totalPnl)}">${fmtUsd(totalPnl)}</div></div>
        <div class="card"><div class="label">Unrealized</div><div class="value ${pnlColor(unrealized)}">${fmtUsd(unrealized)}</div></div>
        <div class="card"><div class="label">Win Rate</div><div class="value">${fmt(wr,0)}%</div></div>
        <div class="card"><div class="label">Trades</div><div class="value">${trades}</div></div>
    </div>`;

    // Open Positions
    if (d.open_positions && d.open_positions.length > 0) {
        html += `<div class="section"><h2>&#x1F4CC; Open Positions</h2><table>
            <tr><th>Coin</th><th>Side</th><th>Entry</th><th>Mark</th><th>PnL</th><th>%</th><th>Lev</th></tr>`;
        d.open_positions.forEach(p => {
            html += `<tr>
                <td><b>${p.symbol.replace('USDT','')}</b></td>
                <td>${sideHtml(p.side)}</td>
                <td>${fmtUsd(p.entry)}</td>
                <td>${fmtUsd(p.mark)}</td>
                <td class="${pnlColor(p.pnl)}"><b>${fmtUsd(p.pnl)}</b></td>
                <td class="${pnlColor(p.pct)}">${fmt(p.pct,1)}%</td>
                <td>${p.lev}x</td>
            </tr>`;
        });
        html += `</table></div>`;
    }

    // Split Positions (Liq Strategy)
    if (d.split_positions && d.split_positions.length > 0) {
        html += `<div class="section"><h2>&#x26A1; Liq Strategy Positions</h2><table>
            <tr><th>Coin</th><th>Dir</th><th>Entry1</th><th>Entry2</th><th>SL</th><th>TP</th><th>Status</th></tr>`;
        d.split_positions.forEach(p => {
            const s1 = p.filled1 ? '&#x2705;' : '&#x23F3;';
            const s2 = p.filled2 ? '&#x2705;' : '&#x23F3;';
            html += `<tr>
                <td><b>${p.symbol.replace('USDT','')}</b></td>
                <td>${sideHtml(p.direction)}</td>
                <td>${s1} ${fmtUsd(p.entry1)}</td>
                <td>${s2} ${fmtUsd(p.entry2)}</td>
                <td>${fmtUsd(p.sl)}</td>
                <td>${fmtUsd(p.tp)}</td>
                <td>${p.filled1 && p.filled2 ? 'Both Filled' : p.filled1 ? 'Order1 Filled' : 'Waiting'}</td>
            </tr>`;
        });
        html += `</table></div>`;
    }

    // Prices
    if (d.prices && Object.keys(d.prices).length > 0) {
        html += `<div class="section"><h2>&#x1F4B9; Prices</h2><div class="prices-grid">`;
        for (const [sym, price] of Object.entries(d.prices)) {
            const name = sym.replace('USDT','');
            let pStr = price >= 1000 ? fmtUsd(price) : '$' + fmt(price, price >= 1 ? 3 : 5);
            html += `<div class="price-item"><div class="coin">${name}</div><div class="price">${pStr}</div></div>`;
        }
        html += `</div></div>`;
    }

    // Liq Tracker Info
    if (d.liq_data && Object.keys(d.liq_data).length > 0) {
        html += `<div class="section"><h2>&#x1F4A7; Liquidation Data</h2><table>
            <tr><th>Coin</th><th>Total Liq USD</th><th></th></tr>`;
        for (const [sym, usd] of Object.entries(d.liq_data)) {
            const pct = Math.min(usd / 5000000 * 100, 100);
            html += `<tr>
                <td><b>${sym.replace('USDT','')}</b></td>
                <td>$${(usd/1e6).toFixed(2)}M</td>
                <td style="width:40%"><div class="liq-bar"><div class="liq-fill" style="width:${pct}%;background:linear-gradient(90deg,#58a6ff,#3fb950)"></div></div></td>
            </tr>`;
        }
        html += `</table></div>`;
    }

    // Trade History (last 15)
    if (d.trades_history && d.trades_history.length > 0) {
        html += `<div class="section"><h2>&#x1F4CB; Recent Trades</h2><table>
            <tr><th>#</th><th>Coin</th><th>Side</th><th>Entry</th><th>Close</th><th>PnL</th><th>%</th><th>Time</th></tr>`;
        d.trades_history.forEach((t, i) => {
            html += `<tr>
                <td>${i+1}</td>
                <td><b>${t.symbol.replace('USDT','')}</b></td>
                <td>${sideHtml(t.side)}</td>
                <td>$${fmt(t.entry, 4)}</td>
                <td>$${fmt(t.close, 4)}</td>
                <td class="${pnlColor(t.pnl)}"><b>${fmtUsd(t.pnl)}</b></td>
                <td class="${pnlColor(t.pct)}">${fmt(t.pct,2)}%</td>
                <td>${t.time.substring(11,16)}</td>
            </tr>`;
        });
        html += `</table></div>`;
    }

    // AI Bias
    if (d.ai_bias && Object.keys(d.ai_bias).length > 0) {
        html += `<div class="section"><h2>&#x1F9E0; AI Bias (TradingAgents)</h2><div class="prices-grid">`;
        for (const [sym, bias] of Object.entries(d.ai_bias)) {
            const name = sym.replace('USDT','');
            const cls = bias === 'LONG' ? 'green' : (bias === 'SHORT' ? 'red' : '');
            html += `<div class="price-item"><div class="coin">${name}</div><div class="price ${cls}">${bias}</div></div>`;
        }
        html += `</div></div>`;
    }

    html += `<div class="footer">Auto-refresh every 3s &bull; Liquidation Strategy Bot</div>`;
    return html;
}

function updateClock() {
    document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}

async function refresh() {
    try {
        const resp = await fetch('/api/state');
        const data = await resp.json();
        document.getElementById('content').innerHTML = renderDashboard(data);
    } catch(e) {
        document.getElementById('content').innerHTML = '<p style="color:#f85149">Connection lost. Retrying...</p>';
    }
}

setInterval(updateClock, 1000);
setInterval(refresh, 3000);
updateClock();
refresh();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/state")
def api_state():
    """JSON endpoint — dashboard fetches này mỗi 3s."""
    if _state is None:
        return jsonify({"error": "not initialized"})

    with _lock:
        s = dict(_state)
        tlog = list(_state.get("trade_log", []))
        open_pos = list(_state.get("open_positions", []))
        splits = dict(_state.get("split_positions", {}))
        prices = dict(_state.get("prices", {}))
        liq_data = dict(_state.get("liq_data", {}))

    today = datetime.now().strftime("%Y-%m-%d")

    # Closed trades
    closed = [t for t in tlog if t.get("status") == "CLOSED" and abs(t.get("pnl_usdt", 0)) > 0.001]
    today_closed = [t for t in closed if t.get("time", "").startswith(today)]

    today_pnl = sum(t.get("pnl_usdt", 0) for t in today_closed)
    total_pnl = sum(t.get("pnl_usdt", 0) for t in closed)
    wins = sum(1 for t in closed if t.get("pnl_usdt", 0) > 0)
    wr = wins / len(closed) * 100 if closed else 0
    unrealized = sum(p.get("_pnl", 0) for p in open_pos)

    # Format open positions
    open_fmt = []
    for p in open_pos:
        amt = float(p.get("positionAmt", 0))
        open_fmt.append({
            "symbol": p.get("symbol", ""),
            "side": "LONG" if amt > 0 else "SHORT",
            "entry": float(p.get("entryPrice", 0)),
            "mark": p.get("_mark", 0),
            "pnl": p.get("_pnl", 0),
            "pct": p.get("_pct", 0),
            "lev": p.get("_lev", 10),
        })

    # Format split positions
    splits_fmt = []
    for sym, sp in splits.items():
        splits_fmt.append({
            "symbol": sym,
            "direction": sp.direction,
            "entry1": sp.entry1,
            "entry2": sp.entry2,
            "sl": sp.sl,
            "tp": sp.tp,
            "filled1": sp.filled1,
            "filled2": sp.filled2,
        })

    # Recent trades (last 15)
    recent = sorted(closed, key=lambda t: t.get("time", ""), reverse=True)[:15]
    trades_fmt = [{
        "symbol": t.get("symbol", ""),
        "side": t.get("side", ""),
        "entry": t.get("entry", 0),
        "close": t.get("close", 0),
        "pnl": t.get("pnl_usdt", 0),
        "pct": t.get("pnl_pct", 0),
        "time": t.get("time", ""),
    } for t in recent]

    # AI Bias
    ai_bias = {}
    try:
        from ai_analyzer import load_bias
        ai_bias = load_bias()
    except Exception:
        pass

    return jsonify({
        "balance": s.get("balance", 0),
        "today_pnl": today_pnl,
        "total_pnl": total_pnl,
        "unrealized": unrealized,
        "win_rate": wr,
        "total_trades": len(closed),
        "scan_no": s.get("scan_no", 0),
        "last_scan": s.get("last_scan", "--:--"),
        "liq_connected": s.get("liq_connected", False),
        "open_positions": open_fmt,
        "split_positions": splits_fmt,
        "prices": prices,
        "liq_data": liq_data,
        "trades_history": trades_fmt,
        "ai_bias": ai_bias,
    })


def start_web_dashboard(state, lock, config, port=5555):
    """Khởi động web dashboard trong background thread."""
    global _state, _lock, _config
    _state = state
    _lock = lock
    _config = config

    def run():
        # Tắt Flask log spam
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.WARNING)
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    logger.info(f"Web dashboard started at http://localhost:{port}")
    return t
