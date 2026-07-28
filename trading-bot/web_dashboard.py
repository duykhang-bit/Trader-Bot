# ============================================================
# WEB DASHBOARD — Real-time Trading Bot Dashboard
# http://localhost:5555
# Features: Start/Stop, Add/Remove coins, Manual order
# ============================================================
import threading
import logging
import json
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["TESTING"] = False

# Set from bot.py
_state = None
_lock = None
_config = None
_exchange = None

# Cache pending orders — chỉ fetch Binance mỗi 10 giây thay vì mỗi 2s
_pending_orders_cache = []
_pending_orders_last_fetch = 0
_PENDING_ORDERS_TTL = 10  # giây

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot Dashboard</title>
<style>
/* ── Base ── */
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'JetBrains Mono', 'Fira Code', monospace; background: #0d1117; color: #c9d1d9; min-height: 100vh; }
.container { max-width: 1200px; margin: 0 auto; padding: 16px; }
.header { display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; background: #161b22; border: 1px solid #30363d; border-radius: 12px; margin-bottom: 16px; }
.header h1 { font-size: 18px; color: #58a6ff; }
.header .status { display: flex; gap: 12px; align-items: center; font-size: 13px; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 4px; }
.dot-green { background: #3fb950; box-shadow: 0 0 6px #3fb950; }
.dot-red { background: #f85149; box-shadow: 0 0 6px #f85149; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 14px; text-align: center; }
.card .label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }
.card .value { font-size: 22px; font-weight: bold; margin-top: 4px; }
.green { color: #3fb950; } .red { color: #f85149; } .blue { color: #58a6ff; } .yellow { color: #d29922; }
.section { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px; margin-bottom: 16px; }
.section h2 { font-size: 14px; color: #58a6ff; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #30363d; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; padding: 8px; color: #8b949e; border-bottom: 1px solid #30363d; }
td { padding: 6px 8px; border-bottom: 1px solid #21262d; }
tr:hover { background: #1c2128; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.badge-long { background: rgba(63,185,80,0.15); color: #3fb950; }
.badge-short { background: rgba(248,81,73,0.15); color: #f85149; }
.prices-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }
.price-item { background: #0d1117; border: 1px solid #21262d; border-radius: 8px; padding: 10px; text-align: center; }
.price-item .coin { font-size: 11px; color: #8b949e; }
.price-item .price { font-size: 15px; font-weight: bold; color: #c9d1d9; margin-top: 2px; }
/* ── Controls ── */
.btn { padding: 8px 16px; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; font-weight: 600; transition: 0.2s; }
.btn-green { background: #238636; color: #fff; } .btn-green:hover { background: #2ea043; }
.btn-red { background: #da3633; color: #fff; } .btn-red:hover { background: #f85149; }
.btn-blue { background: #1f6feb; color: #fff; } .btn-blue:hover { background: #388bfd; }
.btn-sm { padding: 4px 10px; font-size: 11px; }
input, select { background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; padding: 8px 12px; border-radius: 6px; font-size: 12px; font-family: inherit; }
input:focus, select:focus { outline: none; border-color: #58a6ff; }
.control-row { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
.coin-tag { display: inline-flex; align-items: center; gap: 4px; background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 4px 10px; font-size: 12px; }
.coin-tag .remove { cursor: pointer; color: #f85149; font-weight: bold; margin-left: 4px; }
.coin-tag .remove:hover { color: #ff6b6b; }
.liq-bar { height: 6px; background: #21262d; border-radius: 3px; overflow: hidden; margin-top: 4px; }
.liq-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
.footer { text-align: center; color: #484f58; font-size: 11px; padding: 16px; }
.toast { position: fixed; top: 20px; right: 20px; padding: 12px 20px; border-radius: 8px; font-size: 13px; z-index: 9999; animation: fadeIn 0.3s; }
.toast-ok { background: #238636; color: #fff; } .toast-err { background: #da3633; color: #fff; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: translateY(0); } }
@media (max-width: 768px) { .stats { grid-template-columns: repeat(2, 1fr); } .prices-grid { grid-template-columns: repeat(2, 1fr); } }
/* ── Pump Radar ── */
.pump-radar-wrap { background: linear-gradient(135deg,#0d1117 0%,#110a14 100%); border: 1px solid #3d1a1a; border-radius: 12px; padding: 16px; position: relative; overflow: hidden; }
.pump-radar-wrap::before { content:''; position:absolute; inset:0; background:radial-gradient(ellipse at top left,rgba(248,81,73,.04) 0%,transparent 70%); pointer-events:none; }
.pump-header-row { display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:12px; margin-bottom:14px; }
.pump-title-block { display:flex; align-items:center; gap:12px; }
.pump-controls { text-align:right; }
.pump-radar-icon { position:relative; }
.pump-radar-icon.spinning svg { animation: radarSpin 3s linear infinite; }
@keyframes radarSpin { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
.radar-arm { transform-origin:30px 30px; animation:armSpin 3s linear infinite; }
@keyframes armSpin { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }
.pulse-dot { display:inline-block; width:7px; height:7px; background:#f85149; border-radius:50%; margin-left:4px; vertical-align:middle; animation:pulseDot 1s ease-in-out infinite; }
@keyframes pulseDot { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.3;transform:scale(.6)} }
.scan-blink { animation:blinkAnim 1.2s step-end infinite; }
@keyframes blinkAnim { 0%,100%{opacity:1} 50%{opacity:0} }
.pump-alert-banner { background:rgba(248,81,73,.12); border:1px solid rgba(248,81,73,.4); border-radius:8px; padding:8px 12px; font-size:12px; color:#f85149; margin-bottom:12px; display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
.pump-alert-tag { background:rgba(248,81,73,.2); border:1px solid #f85149; border-radius:4px; padding:2px 8px; font-size:11px; font-weight:700; }
.pump-coin-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:10px; }
.pump-coin-card { background:#0d1117; border:1px solid #21262d; border-radius:10px; padding:12px; transition:border-color .3s; }
.pump-coin-card:hover { border-color:#30363d; }
.pump-coin-alert { border-color:rgba(248,81,73,.5)!important; background:rgba(248,81,73,.04)!important; animation:alertPulse 2s ease-in-out infinite; }
@keyframes alertPulse { 0%,100%{box-shadow:0 0 0 rgba(248,81,73,0)} 50%{box-shadow:0 0 12px rgba(248,81,73,.25)} }
@media (max-width:768px) { .pump-coin-grid{grid-template-columns:repeat(2,1fr)} .pump-header-row{flex-direction:column} .pump-controls{text-align:left} }
</style>
</head>
<body>
<div class="container" id="app">
    <div class="header">
        <h1>&#x1F916; Trading Bot</h1>
        <div class="status">
            <span id="bot-status"></span>
            <span id="clock">--:--:--</span>
        </div>
    </div>
    <div id="content">Loading...</div>
</div>
<div id="toast-container"></div>

<script>
function fmt(n,d=2){return Number(n).toFixed(d)}
function fmtUsd(n){return '$'+Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}
function pnlColor(n){return n>=0?'green':'red'}
function sideHtml(s){return s==='LONG'?'<span class="badge badge-long">LONG</span>':'<span class="badge badge-short">SHORT</span>'}

function toast(msg, ok=true) {
    const el = document.createElement('div');
    el.className = 'toast ' + (ok ? 'toast-ok' : 'toast-err');
    el.textContent = msg;
    document.getElementById('toast-container').appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

async function apiPost(url, body={}) {
    try {
        const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
        const d = await r.json();
        if (d.ok) toast(d.msg || 'OK'); else toast(d.msg || 'Error', false);
        return d;
    } catch(e) { toast('Request failed', false); return {ok:false}; }
}

async function toggleBot() { await apiPost('/api/toggle'); refresh(); }
async function toggleOrphan(enabled) {
    await apiPost('/api/set_auto_cancel', {enabled: enabled});
    refresh();
}
async function cancelAllPending() {
    if (!confirm('Huỷ TẤT CẢ lệnh entry đang chờ (không có vị thế)?')) return;
    await apiPost('/api/cancel_all_pending');
    refresh();
}
async function addCoin() {
    const inp = document.getElementById('add-coin-input');
    let sym = inp.value.trim().toUpperCase();
    if (!sym) return;
    if (!sym.endsWith('USDT')) sym += 'USDT';
    const r = await apiPost('/api/coins/add', {symbol: sym});
    if (r.ok) { inp.value = ''; delete _savedInputs['add-coin-input']; }
    refresh();
}
async function removeCoin(sym) { await apiPost('/api/coins/remove', {symbol: sym}); refresh(); }
async function placeOrder() {
    const sym = document.getElementById('order-symbol').value;
    const side = document.getElementById('order-side').value;
    const usdt = parseFloat(document.getElementById('order-usdt').value);
    const sl = parseFloat(document.getElementById('order-sl').value) || 0;
    const tp = parseFloat(document.getElementById('order-tp').value) || 0;
    const lev = parseInt(document.getElementById('order-lev').value) || 10;
    if (!sym || !side || !usdt || usdt <= 0) { toast('Fill all fields', false); return; }
    await apiPost('/api/order', {symbol: sym, side: side, usdt: usdt, sl: sl, tp: tp, leverage: lev});
    refresh();
}
async function updateSettings() {
    const maxUsdt = parseFloat(document.getElementById('set-max-usdt').value);
    const lev = parseInt(document.getElementById('set-leverage').value);
    if (!maxUsdt || maxUsdt <= 0 || !lev || lev < 1) { toast('Invalid', false); return; }
    await apiPost('/api/settings', {max_order_usdt: maxUsdt, leverage: lev});
    refresh();
}
async function closePosition(sym) {
    if (!confirm('Close position ' + sym + '?')) return;
    await apiPost('/api/close', {symbol: sym});
    refresh();
}
async function runAI() {
    toast('AI Analysis started... (2-5 min per coin)');
    await apiPost('/api/ai/run');
    refresh();
}
async function cancelOrder(sym, orderId) {
    if (!confirm('Cancel order?')) return;
    await apiPost('/api/cancel_order', {symbol: sym, order_id: orderId});
    refresh();
}
async function autoSetSlTp(sym) {
    toast('Setting SL/TP for ' + sym + '...');
    const r = await apiPost('/api/auto_sltp', {symbol: sym});
    if (r && r.msg) toast(r.msg, r.ok);
    refresh();
}
async function autoSetSlTpAll() {
    toast('Setting SL/TP for ALL positions...');
    const r = await apiPost('/api/auto_sltp', {symbol: 'ALL'});
    if (r && r.msg) toast(r.msg, r.ok);
    refresh();
}

function renderDashboard(d) {
    // Bot status
    const running = d.running;
    document.getElementById('bot-status').innerHTML = running
        ? '<span class="dot dot-green"></span> Running'
        : '<span class="dot dot-red"></span> Paused';

    let html = '';

    // Control Panel
    html += `<div class="section"><h2>&#x2699; Controls</h2>
        <div class="control-row">
            <button class="btn ${running ? 'btn-red' : 'btn-green'}" onclick="toggleBot()">
                ${running ? '&#x23F8; Pause Bot' : '&#x25B6; Start Bot'}
            </button>
            <button class="btn btn-blue" onclick="runAI()">&#x1F9E0; Run AI Analysis</button>
            <span id="scan-info" style="color:#8b949e;font-size:12px">Scan #${d.scan_no} | Last: ${d.last_scan}${d.ai_last_run ? ' | AI: '+d.ai_last_run : ''}${d.ai_analyzing ? ' ⏳ AI analyzing...' : ''}</span>
        </div>
        <div class="control-row" style="margin-top:8px;align-items:center;gap:12px">
            <label style="font-size:12px;color:#8b949e;display:flex;align-items:center;gap:6px;cursor:pointer">
                <input type="checkbox" id="toggle-orphan" ${d.auto_cancel_orphan ? 'checked' : ''}
                    onchange="toggleOrphan(this.checked)"
                    style="width:14px;height:14px;cursor:pointer">
                <span>🧹 Tự động huỷ lệnh entry chờ không có vị thế</span>
            </label>
            <button class="btn btn-red btn-sm" onclick="cancelAllPending()" style="margin-left:8px">
                &#x1F5D1; Huỷ tất cả lệnh chờ ngay
            </button>
        </div>
    </div>`;

    // ── PUMP RADAR SECTION ──────────────────────────────────
    html += `<div class="section" style="padding:0;border-color:#3d1a1a">
      <div id="pump-radar-root" style="padding:16px">
        <div style="text-align:center;padding:24px;color:#484f58">
          <div style="font-size:28px;margin-bottom:6px">📡</div>
          <div>Đang tải Pump Radar...</div>
        </div>
      </div>
    </div>`;

    // Watchlist Management
    html += `<div class="section"><h2>&#x1F4CB; Watchlist (${d.watchlist.length} coins)</h2>
        <div class="control-row">
            <input id="add-coin-input" placeholder="e.g. XRPUSDT" style="width:140px" onkeydown="if(event.key==='Enter')addCoin()">
            <button class="btn btn-blue btn-sm" onclick="addCoin()">+ Add</button>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">`;
    d.watchlist.forEach(sym => {
        const name = sym.replace('USDT','');
        html += `<div class="coin-tag">${name} <span class="remove" onclick="removeCoin('${sym}')">x</span></div>`;
    });
    html += `</div></div>`;

    // Manual Order
    html += `<div class="section"><h2>&#x1F4B0; Manual Order</h2>
        <div class="control-row">
            <select id="order-symbol">`;
    d.watchlist.forEach(sym => { html += `<option value="${sym}">${sym.replace('USDT','')}</option>`; });
    html += `</select>
            <select id="order-side">
                <option value="LONG">LONG</option>
                <option value="SHORT">SHORT</option>
            </select>
            <input id="order-usdt" type="number" placeholder="Margin $" value="${d.settings.max_order_usdt}" style="width:80px">
            <input id="order-lev" type="number" placeholder="Lev" value="${d.settings.leverage}" style="width:55px">
        </div>
        <div class="control-row">
            <input id="order-sl" type="number" placeholder="SL price (optional)" style="width:150px" step="any">
            <input id="order-tp" type="number" placeholder="TP price (optional)" style="width:150px" step="any">
            <button class="btn btn-green" onclick="placeOrder()">Place Order</button>
        </div>
    </div>`;

    // Bot Settings
    html += `<div class="section"><h2>&#x2699; Bot Settings</h2>
        <div class="control-row">
            <label style="font-size:12px;color:#8b949e">USD/order:</label>
            <input id="set-max-usdt" type="number" value="${d.settings.max_order_usdt}" style="width:80px" step="any">
            <label style="font-size:12px;color:#8b949e">Leverage:</label>
            <input id="set-leverage" type="number" value="${d.settings.leverage}" style="width:55px">
            <button class="btn btn-blue btn-sm" onclick="updateSettings()">Save</button>
            <span style="font-size:11px;color:#8b949e">Bot dùng giá trị này khi tự động vào lệnh</span>
        </div>
    </div>`;

    // Stats
    html += `<div class="stats">
        <div class="card"><div class="label">Balance</div><div id="stat-balance" class="value blue">${fmtUsd(d.balance)}</div></div>
        <div class="card"><div class="label">Today PnL</div><div id="stat-today-pnl" class="value ${pnlColor(d.today_pnl)}">${fmtUsd(d.today_pnl)}</div></div>
        <div class="card"><div class="label">Total PnL</div><div id="stat-total-pnl" class="value ${pnlColor(d.total_pnl)}">${fmtUsd(d.total_pnl)}</div></div>
        <div class="card"><div class="label">Unrealized</div><div id="stat-unrealized" class="value ${pnlColor(d.unrealized)}">${fmtUsd(d.unrealized)}</div></div>
        <div class="card"><div class="label">Win Rate</div><div id="stat-winrate" class="value">${fmt(d.win_rate,0)}%</div></div>
        <div class="card"><div class="label">Trades</div><div id="stat-trades" class="value">${d.total_trades}</div></div>
    </div>`;

    // Open Positions
    html += `<div class="section"><h2>&#x1F4CC; Open Positions</h2>
        <button class="btn btn-green btn-sm" onclick="autoSetSlTpAll()" style="margin-bottom:8px">&#x1F6E1; Auto Set SL/TP ALL</button>
        <table>
        <tr><th>Coin</th><th>Side</th><th>Entry</th><th>Mark</th><th>PnL</th><th>%</th><th>Lev</th><th></th></tr>
        <tbody id="positions-body">`;
    if (d.open_positions && d.open_positions.length > 0) {
        d.open_positions.forEach(p => {
            html += `<tr><td><b>${p.symbol.replace('USDT','')}</b></td><td>${sideHtml(p.side)}</td>
                <td>${fmtUsd(p.entry)}</td><td>${fmtUsd(p.mark)}</td>
                <td class="${pnlColor(p.pnl)}"><b>${fmtUsd(p.pnl)}</b></td>
                <td class="${pnlColor(p.pct)}">${fmt(p.pct,1)}%</td><td>${p.lev}x</td>
                <td><button class="btn btn-green btn-sm" onclick="autoSetSlTp('${p.symbol}')" title="Auto SL/TP">&#x1F6E1;</button>
                <button class="btn btn-red btn-sm" onclick="closePosition('${p.symbol}')">Close</button></td></tr>`;
        });
    } else {
        html += `<tr><td colspan="8" style="color:#484f58;text-align:center;padding:12px">Không có lệnh mở</td></tr>`;
    }
    html += `</tbody></table></div>`;

    // Pending Orders (lệnh chờ khớp)
    if (d.pending_orders && d.pending_orders.length > 0) {
        html += `<div class="section"><h2>&#x23F3; Pending Orders (${d.pending_orders.length})</h2><table>
            <tr><th>Coin</th><th>Side</th><th>Type</th><th>Price</th><th>Qty</th><th></th></tr>`;
        d.pending_orders.forEach(o => {
            const name = o.symbol.replace('USDT','');
            const pStr = o.price >= 1000 ? fmtUsd(o.price) : '$'+fmt(o.price, o.price>=1?3:5);
            const sideClass = o.side === 'BUY' ? 'green' : 'red';
            html += `<tr>
                <td><b>${name}</b></td>
                <td class="${sideClass}">${o.side}</td>
                <td>${o.type}</td>
                <td>${pStr}</td>
                <td>${o.qty}</td>
                <td><button class="btn btn-red btn-sm" onclick="cancelOrder('${o.symbol}','${o.order_id}')">Cancel</button></td>
            </tr>`;
        });
        html += `</table></div>`;
    }

    // Scan Status — coin đang quét + signals
    html += `<div class="section"><h2>&#x1F50D; Scan Status</h2>`;

    // Trend overview cho từng coin
    html += `<div style="margin-bottom:12px"><b style="font-size:12px;color:#8b949e">COIN TREND:</b></div>`;
    html += `<div class="prices-grid" style="margin-bottom:12px">`;
    d.watchlist.forEach(sym => {
        const name = sym.replace('USDT','');
        const price = d.prices[sym] || 0;
        // Find candidate for this coin
        const cand = (d.candidates || []).find(c => c.symbol === sym);
        let trendIcon = '&#x26AA;'; // neutral
        let trendText = 'HOLD';
        let trendCls = '';
        if (cand) {
            if (cand.signal === 'LONG') { trendIcon = '&#x1F7E2;'; trendText = 'LONG'; trendCls = 'green'; }
            else if (cand.signal === 'SHORT') { trendIcon = '&#x1F534;'; trendText = 'SHORT'; trendCls = 'red'; }
        }
        // Check pending watch
        const pending = (d.pending_watch || {})[sym];
        if (!cand && pending) {
            if (pending.signal === 'LONG') { trendIcon = '&#x1F7E1;'; trendText = `PENDING L WR=${pending.win_rate.toFixed(0)}% #${pending.retry}`; trendCls = 'yellow'; }
            else if (pending.signal === 'SHORT') { trendIcon = '&#x1F7E1;'; trendText = `PENDING S WR=${pending.win_rate.toFixed(0)}% #${pending.retry}`; trendCls = 'yellow'; }
        }
        let pStr = price >= 1000 ? fmtUsd(price) : '$' + fmt(price, price >= 1 ? 2 : 5);

        // AI Bias
        const aiBias = (d.ai_bias || {})[sym] || '';
        let aiHtml = '';
        if (aiBias) {
            const aiCls = aiBias === 'LONG' ? 'green' : (aiBias === 'SHORT' ? 'red' : 'yellow');
            aiHtml = `<div style="font-size:9px;margin-top:2px"><span class="${aiCls}">AI: <b>${aiBias}</b></span></div>`;
        }

        // Entry targets from liq tracker
        const targets = (d.entry_targets || {})[sym] || {};
        let targetHtml = '';
        if (targets.short_entry) {
            const sp = targets.short_entry >= 1000 ? fmtUsd(targets.short_entry) : '$'+fmt(targets.short_entry, targets.short_entry>=1?2:5);
            targetHtml += `<div style="font-size:10px;color:#f85149;margin-top:3px"><b>SHORT</b> @ ${sp}</div>`;
        }
        if (targets.long_entry) {
            const lp = targets.long_entry >= 1000 ? fmtUsd(targets.long_entry) : '$'+fmt(targets.long_entry, targets.long_entry>=1?2:5);
            targetHtml += `<div style="font-size:10px;color:#3fb950"><b>LONG</b> @ ${lp}</div>`;
        }

        html += `<div class="price-item">
            <div class="coin">${trendIcon} ${name}</div>
            <div class="price">${pStr}</div>
            <div style="font-size:11px;margin-top:2px" class="${trendCls}"><b>${trendText}</b></div>
            ${aiHtml}
            ${targetHtml}
        </div>`;
    });
    html += `</div>`;

    // Signal details table
    if (d.candidates && d.candidates.length > 0) {
        html += `<table><tr><th>Coin</th><th>Signal</th><th>Score</th><th>Now</th><th>Entry Target</th><th>RSI</th><th>Reason</th></tr>`;
        d.candidates.forEach(c => {
            const filled = Math.round(c.score / 10);
            const bar = '&#x2588;'.repeat(filled) + '&#x2591;'.repeat(10 - filled);
            const pStr = c.price >= 1000 ? fmtUsd(c.price) : '$' + fmt(c.price, c.price >= 1 ? 3 : 5);
            // Entry target: từ entry_targets
            const targets = (d.entry_targets || {})[c.symbol] || {};
            let entryStr = '-';
            if (c.signal === 'LONG' && targets.long_entry) {
                const ep = targets.long_entry >= 1000 ? fmtUsd(targets.long_entry) : '$'+fmt(targets.long_entry, targets.long_entry>=1?2:5);
                entryStr = `<span style="color:#3fb950">${ep}</span>`;
            } else if (c.signal === 'SHORT' && targets.short_entry) {
                const ep = targets.short_entry >= 1000 ? fmtUsd(targets.short_entry) : '$'+fmt(targets.short_entry, targets.short_entry>=1?2:5);
                entryStr = `<span style="color:#f85149">${ep}</span>`;
            }
            html += `<tr>
                <td><b>${c.symbol.replace('USDT','')}</b></td>
                <td>${sideHtml(c.signal)}</td>
                <td>${bar} <b>${fmt(c.score,0)}%</b></td>
                <td>${pStr}</td>
                <td><b>${entryStr}</b></td>
                <td>${fmt(c.rsi,0)}</td>
                <td style="font-size:11px;color:#8b949e;max-width:200px;overflow:hidden;text-overflow:ellipsis">${c.reason}</td>
            </tr>`;
        });
        html += `</table>`;
    } else {
        html += `<p style="color:#8b949e;font-size:12px">&#x23F3; Bot đang quét mỗi 60s. Chưa có coin nào đủ score ≥ 50%</p>`;
        html += `<p style="color:#8b949e;font-size:11px;margin-top:4px">Điều kiện vào lệnh: RSI + EMA + MACD + Volume + MTF trend phải đồng thuận</p>`;
    }

    // Trigger prices - hiện rõ giá cụ thể bot sẽ vào lệnh
    html += `<div style="margin-top:12px;padding-top:12px;border-top:1px solid #30363d">`;
    html += `<b style="font-size:12px;color:#58a6ff">&#x1F3AF; TRIGGER PRICES — Vùng liq bot sẽ vào lệnh:</b>`;
    html += `<table style="margin-top:8px"><tr><th>Coin</th><th style="color:#f85149">SHORT khi giá pump lên ≥ (liq LONG zone)</th><th style="color:#3fb950">LONG khi giá dump xuống ≤ (liq SHORT zone)</th><th>Current</th><th>Khoảng cách</th></tr>`;
    d.watchlist.forEach(sym => {
        const name = sym.replace('USDT','');
        const p = d.prices[sym] || 0;
        const targets = (d.entry_targets || {})[sym] || {};
        const shortP = targets.short_entry || 0;
        const longP = targets.long_entry || 0;
        const hasReal = targets.has_real_data || false;
        const dataTag = hasReal ? '' : ' <span style="color:#d29922;font-size:9px">(đang thu thập...)</span>';
        const shortStr = shortP > 0 ? (shortP >= 1000 ? fmtUsd(shortP) : '$'+fmt(shortP, shortP>=1?2:6)) : '⏳';
        const longStr  = longP  > 0 ? (longP  >= 1000 ? fmtUsd(longP)  : '$'+fmt(longP,  longP >=1?2:6)) : '⏳';
        const curStr   = p >= 1000 ? fmtUsd(p) : '$'+fmt(p, p>=1?2:5);
        const shortGap = shortP > 0 ? fmt((shortP-p)/p*100,2)+'%' : '-';
        const longGap  = longP  > 0 ? fmt((p-longP)/p*100,2)+'%'  : '-';
        html += `<tr>
            <td><b>${name}</b></td>
            <td style="color:#f85149">${shortStr}${dataTag} <span style="font-size:10px;color:#8b949e">(+${shortGap})</span></td>
            <td style="color:#3fb950">${longStr}${dataTag} <span style="font-size:10px;color:#8b949e">(-${longGap})</span></td>
            <td>${curStr}</td>
            <td style="font-size:10px;color:#8b949e">SHORT: ${shortGap} | LONG: ${longGap}</td>
        </tr>`;
    });
    html += `</table></div>`;

    // Liq Strategy pending entries
    if (d.split_positions_web && d.split_positions_web.length > 0) {
        html += `<h2 style="margin-top:16px;font-size:13px;color:#58a6ff;border-top:1px solid #30363d;padding-top:12px">&#x26A1; Pending Liq Entries (bot will auto-enter at these prices)</h2>`;
        html += `<table><tr><th>Coin</th><th>Dir</th><th>Entry1 (35%)</th><th>Entry2 (65%)</th><th>SL</th><th>TP</th><th>Status</th></tr>`;
        d.split_positions_web.forEach(p => {
            const s1 = p.filled1 ? '&#x2705;' : '&#x23F3;';
            const s2 = p.filled2 ? '&#x2705;' : '&#x23F3;';
            html += `<tr>
                <td><b>${p.symbol.replace('USDT','')}</b></td>
                <td>${sideHtml(p.direction)}</td>
                <td>${s1} ${fmtUsd(p.entry1)}</td>
                <td>${s2} ${fmtUsd(p.entry2)}</td>
                <td style="color:#f85149">${fmtUsd(p.sl)}</td>
                <td style="color:#3fb950">${fmtUsd(p.tp)}</td>
                <td>${p.filled1 && p.filled2 ? '&#x2705; Both' : p.filled1 ? '&#x23F3; Wait E2' : '&#x23F3; Wait E1'}</td>
            </tr>`;
        });
        html += `</table>`;
    }
    html += `</div>`;

    // Prices
    if (d.prices && Object.keys(d.prices).length > 0) {
        html += `<div class="section"><h2>&#x1F4B9; Prices</h2><div class="prices-grid">`;
        for (const [sym, price] of Object.entries(d.prices)) {
            const name = sym.replace('USDT','');
            let pStr = price >= 1000 ? fmtUsd(price) : '$' + fmt(price, price >= 1 ? 3 : 5);
            html += `<div class="price-item"><div class="coin">${name}</div><div id="price-${sym}" class="price">${pStr}</div></div>`;
        }
        html += `</div></div>`;
    }

    // Trade History
    if (d.trades_history && d.trades_history.length > 0) {
        html += `<div class="section"><h2>&#x1F4CB; Recent Trades</h2><table>
            <tr><th>#</th><th>Coin</th><th>Side</th><th>Entry</th><th>Close</th><th>PnL</th><th>%</th><th>Time</th></tr>`;
        d.trades_history.forEach((t,i) => {
            html += `<tr><td>${i+1}</td><td><b>${t.symbol.replace('USDT','')}</b></td><td>${sideHtml(t.side)}</td>
                <td>$${fmt(t.entry,4)}</td><td>$${fmt(t.close,4)}</td>
                <td class="${pnlColor(t.pnl)}"><b>${fmtUsd(t.pnl)}</b></td>
                <td class="${pnlColor(t.pct)}">${fmt(t.pct,2)}%</td><td>${t.time.substring(11,16)}</td></tr>`;
        });
        html += `</table></div>`;
    }

    html += `<div class="footer">Auto-refresh 1s</div>`;
    return html;
}

// ── PUMP RADAR ───────────────────────────────────────────────
let _pumpData = null;

async function fetchPump() {
    try {
        const r = await fetch('/api/pump');
        _pumpData = await r.json();
        renderPumpRadar(_pumpData);
    } catch(e) {}
}

async function addPumpCoin() {
    const inp = document.getElementById('pump-coin-input');
    let sym = (inp.value || '').trim().toUpperCase();
    if (!sym) return;
    if (!sym.endsWith('USDT')) sym += 'USDT';
    const r = await apiPost('/api/pump/coins/add', {symbol: sym});
    if (r.ok) { inp.value = ''; fetchPump(); }
}

async function removePumpCoin(sym) {
    await apiPost('/api/pump/coins/remove', {symbol: sym});
    fetchPump();
}

async function pumpManualShort(sym) {
    // Lấy giá hiện tại từ state
    const price = (_pumpData && _pumpData.coins)
        ? ((_pumpData.coins.find(c=>c.symbol===sym)||{}).price || 0)
        : 0;
    const priceStr = price > 0 ? ` @ $${price.toPrecision(5)}` : '';
    if (!confirm(`SHORT tay ${sym}${priceStr}?\n\nDùng MAX_ORDER_USDT + LEVERAGE từ config.`)) return;
    const r = await apiPost('/api/order', {
        symbol: sym,
        side:   'SHORT',
        usdt:   0,   // 0 = dùng MAX_ORDER_USDT từ config
        sl:     0,
        tp:     0,
        leverage: 0  // 0 = dùng LEVERAGE từ config
    });
    if (r.ok) fetchPump();
}

async function toggleAutoShort(enabled) {
    const r = await apiPost('/api/pump/toggle_auto', {enabled: enabled});
    if (r && r.msg) toast(r.msg, r.ok);
}

function scoreColor(s) {
    if (s >= 80) return '#f85149';
    if (s >= 60) return '#ff9500';
    if (s >= 40) return '#d29922';
    return '#8b949e';
}

function renderPumpRadar(d) {
    const el = document.getElementById('pump-radar-root');
    if (!el || !d) return;

    const status    = d.status   || {};
    const coins     = d.coins    || [];
    const history   = d.history  || [];
    const autoShort = d.auto_short || false;
    const minScore  = d.min_score  || 60;
    const scanning  = status.scanning   || false;
    const scanCount = status.scan_count || 0;
    const lastScan  = status.last_scan  || '--:--';
    const alertCoins = coins.filter(c => c.score >= minScore);

    // Vị trí blip trên radar
    const CX = 110, CY = 110, R = 85;
    const blips = coins.map((c, i) => {
        const angle = (i / Math.max(coins.length, 1)) * 360 - 90;
        const rad   = angle * Math.PI / 180;
        const dist  = R * (0.3 + 0.7 * (1 - c.score / 100));
        const x = CX + dist * Math.cos(rad);
        const y = CY + dist * Math.sin(rad);
        const isAlert = c.score >= minScore;
        const isNear  = c.score >= 40 && !isAlert;
        const col = isAlert ? '#3fb950' : isNear ? '#d29922' : '#2d5a6a';
        const sz  = isAlert ? 7 : isNear ? 5 : 3.5;
        const lbl = c.symbol.replace('USDT','');
        const anim = isAlert ? `<animate attributeName="r" values="${sz};${sz+3};${sz}" dur="1.2s" repeatCount="indefinite"/>` : '';
        return `<g style="cursor:pointer" onclick="scrollToCoin('${c.symbol}')">
          <circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${sz}" fill="${col}" opacity="0.9">${anim}</circle>
          <text x="${(x+sz+3).toFixed(1)}" y="${(y+4).toFixed(1)}" font-size="9" fill="${col}" opacity="0.8" font-family="monospace">${lbl}</text>
        </g>`;
    }).join('');

    const sweepX = (CX + R * Math.sin(Math.PI * 0.3)).toFixed(0);
    const sweepY = (CY - R * Math.cos(Math.PI * 0.3)).toFixed(0);

    // Build SVG radar
    const svgRadar = `<svg width="220" height="220" viewBox="0 0 220 220" style="background:#060d14;border-radius:50%;border:1px solid #1a3a2a">
      <defs>
        <radialGradient id="swg" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="#3fb950" stop-opacity="0.3"/>
          <stop offset="100%" stop-color="#3fb950" stop-opacity="0"/>
        </radialGradient>
      </defs>
      <circle cx="110" cy="110" r="90" fill="none" stroke="#1a3a2a" stroke-width="1"/>
      <circle cx="110" cy="110" r="60" fill="none" stroke="#1a3a2a" stroke-width="0.7" stroke-dasharray="4,4"/>
      <circle cx="110" cy="110" r="30" fill="none" stroke="#1a3a2a" stroke-width="0.7" stroke-dasharray="4,4"/>
      <line x1="110" y1="22" x2="110" y2="198" stroke="#1a3a2a" stroke-width="0.5"/>
      <line x1="22" y1="110" x2="198" y2="110" stroke="#1a3a2a" stroke-width="0.5"/>
      <g style="transform-origin:110px 110px;animation:armSpin 4s linear infinite">
        <path d="M110,110 L110,20 A90,90 0 0,1 ${sweepX},${sweepY} Z" fill="url(#swg)" opacity="0.7"/>
        <line x1="110" y1="110" x2="110" y2="22" stroke="#3fb950" stroke-width="1.5" stroke-linecap="round" opacity="0.9"/>
      </g>
      <circle cx="110" cy="110" r="3" fill="#3fb950"/>
      ${blips}
    </svg>`;

    let html = `
    <div style="background:#060d14;border:1px solid #1a3a2a;border-radius:12px;padding:16px">

      <!-- Top bar -->
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:10px">
        <div style="display:flex;align-items:center;gap:10px">
          <div style="width:9px;height:9px;border-radius:50%;background:#3fb950;box-shadow:0 0 8px #3fb950;
                      animation:pulseDot 1.2s ease-in-out infinite"></div>
          <span style="color:#3fb950;font-size:14px;font-weight:700;letter-spacing:2px">PUMP RADAR</span>
          <span style="color:#1a4a2a;font-size:11px">Scan #${scanCount} · ${lastScan}</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <input id="pump-coin-input" placeholder="BANKUSDT"
                 style="width:110px;font-size:11px;background:#060d14;border-color:#1a3a2a;color:#3fb950"
                 onkeydown="if(event.key==='Enter')addPumpCoin()">
          <button class="btn btn-sm" onclick="addPumpCoin()"
                  style="background:#0d2a1a;color:#3fb950;border:1px solid #1a4a2a">+ Add</button>
          <label style="font-size:11px;display:flex;align-items:center;gap:5px;cursor:pointer">
            <input type="checkbox" id="pump-auto-short" ${autoShort?'checked':''}
                   onchange="toggleAutoShort(this.checked)" style="accent-color:#f85149">
            <span style="color:${autoShort?'#f85149':'#2a5a3a'}">${autoShort?'🔴 AUTO SHORT':'⏸ Alert only'}</span>
          </label>
        </div>
      </div>

      <!-- Alert banner -->
      ${alertCoins.length > 0 ? `
      <div style="background:rgba(63,185,80,.1);border:1px solid rgba(63,185,80,.4);border-radius:6px;
                  padding:8px 12px;margin-bottom:12px;font-size:12px;color:#3fb950">
        🚨 <b>SẮP VÀO LỆNH:</b>
        ${alertCoins.map(c=>`<span style="background:rgba(63,185,80,.15);border:1px solid #3fb950;border-radius:4px;padding:2px 8px;margin-left:4px;font-weight:700">${c.symbol.replace('USDT','')} ${c.score}/100</span>`).join('')}
      </div>` : ''}

      <!-- Radar + Coin list -->
      <div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap">

        <!-- SVG Radar -->
        <div style="flex-shrink:0;text-align:center">
          ${svgRadar}
          <div style="font-size:10px;color:#1a4a2a;margin-top:4px">${coins.length} coin đang quét</div>
        </div>

        <!-- Coin list -->
        <div style="flex:1;min-width:220px;display:flex;flex-direction:column;gap:6px">
          ${coins.length === 0 ? `
            <div style="text-align:center;padding:40px 16px;color:#1a3a2a;border:1px dashed #1a3a2a;border-radius:8px;font-size:12px">
              📡 Thêm coin dev hay pump<br><span style="font-size:10px;color:#0d2a1a">BANK · LAB · SIREN · MAGMA...</span>
            </div>` :
          coins.map(c => {
            const name    = c.symbol.replace('USDT','');
            const isAlert = c.score >= minScore;
            const isNear  = c.score >= 40 && !isAlert;
            const col     = isAlert ? '#3fb950' : isNear ? '#d29922' : '#2d5a4a';
            const bg      = isAlert ? 'rgba(63,185,80,.08)' : isNear ? 'rgba(210,153,34,.05)' : 'transparent';
            const bdr     = isAlert ? '1px solid rgba(63,185,80,.4)' : isNear ? '1px solid rgba(210,153,34,.3)' : '1px solid #0d2020';
            const pStr    = c.price > 0 ? (c.price >= 1 ? '$'+c.price.toFixed(4) : '$'+c.price.toFixed(6)) : '—';
            const status  = isAlert ? '🟢 SẮP VÀO LỆNH' : isNear ? '🟡 Đang gần' : '⚫ Đang quét';
            const ageSec  = c.ts ? Math.round((Date.now()/1000) - c.ts) : null;
            const ageStr  = ageSec !== null && ageSec < 3600 ? (ageSec<60?`${ageSec}s`:`${Math.floor(ageSec/60)}m`) : '';
            return `
            <div id="coin-${c.symbol}"
                 style="background:${bg};border:${bdr};border-radius:8px;padding:10px 12px;
                        ${isAlert?'box-shadow:0 0 10px rgba(63,185,80,.15)':''}">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div style="display:flex;align-items:center;gap:8px">
                  <span style="font-size:14px;font-weight:700;color:${col}">${name}</span>
                  <span style="font-size:11px;color:#1a5a3a">${pStr}</span>
                  <span style="font-size:10px;color:${col}">${status}</span>
                </div>
                <div style="display:flex;align-items:center;gap:6px">
                  ${ageStr ? `<span style="font-size:10px;color:#0d3a2a">${ageStr}</span>` : ''}
                  <button onclick="pumpManualShort('${c.symbol}')"
                          style="background:#7a1a1a;color:#ff6b6b;border:1px solid #aa2a2a;border-radius:4px;
                                 padding:2px 8px;font-size:10px;font-weight:700;cursor:pointer">▼ SHORT</button>
                  <button onclick="removePumpCoin('${c.symbol}')"
                          style="background:none;border:none;color:#1a4a3a;cursor:pointer;font-size:15px;padding:0">×</button>
                </div>
              </div>
              <div style="margin-top:6px">
                <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:2px">
                  <span style="color:#0d3a2a">SCORE</span>
                  <span style="color:${col};font-weight:700">${c.score}/100</span>
                </div>
                <div style="background:#0a1a10;border-radius:3px;height:5px;overflow:hidden">
                  <div style="width:${Math.min(c.score,100)}%;height:100%;background:${col};border-radius:3px;transition:width .6s;
                              ${isAlert?'box-shadow:0 0 5px '+col:''}"></div>
                </div>
              </div>
              <div style="display:flex;gap:8px;margin-top:5px;font-size:10px;flex-wrap:wrap">
                ${c.pump_pct > 0 ? `<span style="color:#d29922">↑${c.pump_pct.toFixed(1)}%</span>` : ''}
                ${c.rsi > 0 ? `<span style="color:${c.rsi>70?'#f85149':'#1a6a4a'}">RSI ${c.rsi.toFixed(0)}</span>` : ''}
                ${c.vol_ratio > 0 ? `<span style="color:#1a5a7a">Vol ${c.vol_ratio.toFixed(1)}×</span>` : ''}
                ${isAlert && c.entry > 0 ? `
                  <span style="color:#3fb950;font-weight:600">Entry $${c.entry.toPrecision(4)}</span>
                  <span style="color:#f85149">SL $${c.sl.toPrecision(4)}</span>
                  <span style="color:#3fb950">TP $${c.tp1.toPrecision(4)}</span>` : ''}
              </div>
            </div>`;
          }).join('')}
        </div>
      </div>

      <!-- History -->
      ${history.filter(h=>h.is_pump_top).length > 0 ? `
      <div style="margin-top:12px;padding-top:10px;border-top:1px solid #0d2a1a">
        <div style="font-size:10px;color:#1a4a2a;margin-bottom:6px">📋 Tín hiệu gần nhất:</div>
        <div style="display:flex;flex-wrap:wrap;gap:5px">
          ${history.filter(h=>h.is_pump_top).slice(-6).reverse().map(h=>{
            const t=new Date(h.timestamp*1000);
            const tStr=t.toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
            const name=(h.symbol||'').replace('USDT','');
            return `<div style="background:rgba(63,185,80,.07);border:1px solid rgba(63,185,80,.25);border-radius:5px;padding:4px 8px;font-size:10px">
              <span style="color:#3fb950;font-weight:700">${name}</span>
              <span style="color:#d29922;margin-left:3px">+${(h.pump_pct||0).toFixed(1)}%</span>
              <span style="color:#1a5a3a;margin-left:3px">s=${h.score||0}</span>
              <span style="color:#0d3a2a;margin-left:3px">${tStr}</span>
            </div>`;
          }).join('')}
        </div>
      </div>` : ''}

    </div>`;

    el.innerHTML = html;
}

function scrollToCoin(sym) {
    const el = document.getElementById('coin-'+sym);
    if (el) el.scrollIntoView({behavior:'smooth', block:'nearest'});
}

// Pump radar auto-refresh riêng — nhanh hơn main (2s)
setInterval(fetchPump, 2000);
fetchPump();

function updateClock(){document.getElementById('clock').textContent=new Date().toLocaleTimeString()}

// Lưu state input để không bị reset khi refresh
let _savedInputs = {};
function saveInputs() {
    ['order-symbol','order-side','order-usdt','order-sl','order-tp','order-lev','set-max-usdt','set-leverage','add-coin-input','pump-coin-input'].forEach(id => {
        const el = document.getElementById(id);
        if (el) _savedInputs[id] = el.value;
    });
}
function restoreInputs() {
    for (const [id, val] of Object.entries(_savedInputs)) {
        const el = document.getElementById(id);
        if (el && val !== undefined) el.value = val;
    }
}

let _firstRender = true;
let _refreshPaused = false;  // dừng refresh khi bot tắt

async function refresh(){
    try{
        const r = await fetch('/api/state');
        const d = await r.json();

        // Backend chưa init xong (bot đang khởi động)
        if (d.error) {
            document.getElementById('content').innerHTML =
                '<p style="color:#8b949e;text-align:center;padding:40px">⏳ Bot đang khởi động... (' + d.error + ')</p>';
            _firstRender = true;
            return;
        }

        // Luôn render dashboard dù bot đang paused hay running
        if (_firstRender) {
            saveInputs();
            document.getElementById('content').innerHTML = renderDashboard(d);
            restoreInputs();
            _firstRender = false;
        } else {
            _patchDashboard(d);
        }

        // Update trạng thái pause/resume
        if (!d.running) {
            _refreshPaused = true;
        } else if (_refreshPaused) {
            _refreshPaused = false;
        }
    }
    catch(e){
        document.getElementById('content').innerHTML='<p style="color:#f85149;text-align:center;padding:40px">⚠️ Connection lost — đang thử lại...</p>';
        _firstRender = true;
    }
}

function _setText(id, val) {
    const el = document.getElementById(id);
    if (el && el.textContent !== val) el.textContent = val;
}
function _setHtml(id, val) {
    const el = document.getElementById(id);
    if (el && el.innerHTML !== val) el.innerHTML = val;
}

function _patchDashboard(d) {
    // Clock — đã update riêng bởi updateClock()

    // Bot status
    const running = d.running;
    document.getElementById('bot-status').innerHTML = running
        ? '<span class="dot dot-green"></span> Running'
        : '<span class="dot dot-red"></span> Paused';

    // Stats cards — patch textContent không flash
    const statIds = ['stat-balance','stat-today-pnl','stat-total-pnl','stat-unrealized','stat-winrate','stat-trades'];
    const statVals = [
        fmtUsd(d.balance),
        fmtUsd(d.today_pnl),
        fmtUsd(d.total_pnl),
        fmtUsd(d.unrealized),
        fmt(d.win_rate,0)+'%',
        String(d.total_trades)
    ];
    statIds.forEach((id,i) => _setText(id, statVals[i]));

    // Scan info
    _setText('scan-info', `Scan #${d.scan_no} | Last: ${d.last_scan}${d.ai_last_run?' | AI: '+d.ai_last_run:''}${d.ai_analyzing?' ⏳ AI analyzing...':''}`);

    // Open positions — rebuild nhỏ hơn
    const posEl = document.getElementById('positions-body');
    if (posEl) {
        let rows = '';
        if (d.open_positions && d.open_positions.length > 0) {
            d.open_positions.forEach(p => {
                rows += `<tr><td><b>${p.symbol.replace('USDT','')}</b></td><td>${sideHtml(p.side)}</td>
                    <td>${fmtUsd(p.entry)}</td><td>${fmtUsd(p.mark)}</td>
                    <td class="${pnlColor(p.pnl)}"><b>${fmtUsd(p.pnl)}</b></td>
                    <td class="${pnlColor(p.pct)}">${fmt(p.pct,1)}%</td><td>${p.lev}x</td>
                    <td><button class="btn btn-green btn-sm" onclick="autoSetSlTp('${p.symbol}')">&#x1F6E1;</button>
                    <button class="btn btn-red btn-sm" onclick="closePosition('${p.symbol}')">Close</button></td></tr>`;
            });
        } else {
            rows = '<tr><td colspan="8" style="color:#484f58;text-align:center">Không có lệnh mở</td></tr>';
        }
        if (posEl.innerHTML !== rows) posEl.innerHTML = rows;
    }

    // Giá coin — patch từng ô
    if (d.prices) {
        Object.entries(d.prices).forEach(([sym, price]) => {
            const el = document.getElementById('price-'+sym);
            if (el) {
                const pStr = price >= 1000 ? fmtUsd(price) : '$' + fmt(price, price >= 1 ? 3 : 5);
                if (el.textContent !== pStr) el.textContent = pStr;
            }
        });
    }
}

setInterval(updateClock,1000);
setInterval(refresh, 2000);
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
    if _state is None:
        return jsonify({"error": "not initialized"})

    with _lock:
        s = dict(_state)
        tlog = list(_state.get("trade_log", []))
        open_pos = list(_state.get("open_positions", []))
        splits = dict(_state.get("split_positions", {}))
        prices = dict(_state.get("prices", {}))
        liq_data = dict(_state.get("liq_data", {}))
        watchlist = list(_state.get("_watchlist", []))
        candidates = list(_state.get("candidates", []))

    today = datetime.now().strftime("%Y-%m-%d")
    closed = [t for t in tlog if t.get("status") == "CLOSED" and abs(t.get("pnl_usdt", 0)) > 0.001]
    today_closed = [t for t in closed if t.get("time", "").startswith(today)]
    today_pnl = sum(t.get("pnl_usdt", 0) for t in today_closed)
    total_pnl = sum(t.get("pnl_usdt", 0) for t in closed)
    wins = sum(1 for t in closed if t.get("pnl_usdt", 0) > 0)
    wr = wins / len(closed) * 100 if closed else 0
    unrealized = sum(p.get("_pnl", 0) for p in open_pos)

    open_fmt = []
    for p in open_pos:
        amt = float(p.get("positionAmt", 0))
        open_fmt.append({"symbol": p.get("symbol",""), "side": "LONG" if amt > 0 else "SHORT",
            "entry": float(p.get("entryPrice",0)), "mark": p.get("_mark",0),
            "pnl": p.get("_pnl",0), "pct": p.get("_pct",0), "lev": p.get("_lev",10)})

    # Pending orders (lệnh chờ khớp) — cache 10s để tránh rate limit
    pending_orders = []
    global _pending_orders_cache, _pending_orders_last_fetch
    import time as _time
    now_ts = _time.time()
    if now_ts - _pending_orders_last_fetch > _PENDING_ORDERS_TTL:
        try:
            if _exchange:
                all_orders = _exchange._get("/fapi/v1/openOrders", signed=True)
                _pending_orders_cache = [{
                    "symbol": o.get("symbol", ""),
                    "side": o.get("side", ""),
                    "type": o.get("type", ""),
                    "qty": float(o.get("origQty", 0)),
                    "price": float(o.get("price", 0) or o.get("stopPrice", 0)),
                    "order_id": str(o.get("orderId", "")),
                } for o in all_orders]
                _pending_orders_last_fetch = now_ts
        except Exception:
            pass
    pending_orders = _pending_orders_cache

    recent = sorted(closed, key=lambda t: t.get("time",""), reverse=True)[:15]
    trades_fmt = [{"symbol":t.get("symbol",""),"side":t.get("side",""),"entry":t.get("entry",0),
        "close":t.get("close",0),"pnl":t.get("pnl_usdt",0),"pct":t.get("pnl_pct",0),
        "time":t.get("time","")} for t in recent]

    # Entry targets — vùng liq THẬT từ WS real-time (giống Coinglass)
    # Chỉ hiện khi liq tracker đã có đủ data thật, không fallback giá fake
    entry_targets = {}
    liq_tracker = _state.get("liq_tracker") if _state else None
    for sym in watchlist:
        p = prices.get(sym, 0)
        if p <= 0:
            continue
        short_trigger = None
        long_trigger  = None
        has_real_data = False

        if liq_tracker and liq_tracker.total_liq_usd(sym) > 0:
            try:
                heatmap = liq_tracker.get_liq_heatmap(sym) or {}
                if heatmap:
                    # SHORT trigger: vùng liq LONG xa nhất phía trên (real data)
                    above = [(pr, usd) for pr, usd in heatmap.items() if pr > p and usd >= 50_000]
                    below = [(pr, usd) for pr, usd in heatmap.items() if pr < p and usd >= 50_000]
                    if above:
                        short_trigger = max(above, key=lambda x: x[0])[0]
                    if below:
                        long_trigger = min(below, key=lambda x: x[0])[0]
                    has_real_data = True
            except Exception:
                pass

        if not has_real_data:
            # Chưa có data thật → dùng ±1% tạm thời, đánh dấu là estimate
            short_trigger = round(p * 1.01, 2 if p >= 100 else 6)
            long_trigger  = round(p * 0.99, 2 if p >= 100 else 6)

        entry_targets[sym] = {
            "short_entry": float(short_trigger) if short_trigger else 0,
            "long_entry":  float(long_trigger)  if long_trigger  else 0,
            "has_real_data": has_real_data
        }

    resp = jsonify({
        "running": s.get("running", False),
        "auto_cancel_orphan": s.get("auto_cancel_orphan", False),
        "balance": s.get("balance", 0),
        "today_pnl": today_pnl, "total_pnl": total_pnl, "unrealized": unrealized,
        "win_rate": wr, "total_trades": len(closed),
        "scan_no": s.get("scan_no", 0), "last_scan": s.get("last_scan", "--:--"),
        "liq_connected": s.get("liq_connected", False),
        "ai_analyzing": s.get("ai_analyzing", False),
        "ai_last_run": s.get("ai_last_run", ""),
        "open_positions": open_fmt, "pending_orders": pending_orders,
        "prices": prices,
        "liq_data": liq_data, "trades_history": trades_fmt,
        "watchlist": watchlist,
        "settings": {
            "max_order_usdt": getattr(_config, "MAX_ORDER_USDT", 15),
            "leverage": getattr(_config, "LEVERAGE", 10),
        },
        "candidates": [{"symbol": c.symbol, "signal": c.signal, "score": c.score,
                         "rsi": c.rsi, "trend": c.trend, "reason": c.reason,
                         "price": prices.get(c.symbol, 0)}
                        for c in candidates[:10]] if candidates else [],
        "pending_watch": _get_pending_watch_safe(),        "split_positions_web": [{
            "symbol": sym, "direction": sp.direction,
            "entry1": sp.entry1, "entry2": sp.entry2,
            "sl": sp.sl, "tp": sp.tp,
            "filled1": sp.filled1, "filled2": sp.filled2,
        } for sym, sp in splits.items()],
        "entry_targets": entry_targets,
        "ai_bias": _get_ai_bias_safe(),
    })
    return resp


@app.route("/api/set_auto_cancel", methods=["POST"])
def api_set_auto_cancel():
    """Bật/tắt tự động huỷ lệnh entry chờ không có vị thế."""
    data = request.get_json() or {}
    enabled = bool(data.get("enabled", False))
    with _lock:
        _state["auto_cancel_orphan"] = enabled
    msg = "✅ Bật tự động huỷ lệnh chờ không có vị thế" if enabled else "⏸ Tắt tự động huỷ — lệnh manual được giữ"
    logger.info(f"[AutoCancel] {msg}")
    return jsonify({"ok": True, "msg": msg, "enabled": enabled})


@app.route("/api/cancel_all_pending", methods=["POST"])
def api_cancel_all_pending():
    """Huỷ ngay tất cả lệnh LIMIT entry đang chờ không có vị thế."""
    if not _exchange:
        return jsonify({"ok": False, "msg": "Exchange not connected"})
    try:
        # Lấy positions đang mở
        all_pos = _exchange._get("/fapi/v2/positionRisk", signed=True)
        open_syms = {p["symbol"] for p in all_pos
                     if abs(float(p.get("positionAmt", 0))) > 0}

        # Lấy tất cả lệnh đang chờ
        all_orders = _exchange._get("/fapi/v1/openOrders", signed=True)

        cancelled = []
        kept = []
        for o in all_orders:
            sym      = o.get("symbol", "")
            otype    = o.get("type", "")
            reduce   = o.get("reduceOnly", False)
            order_id = o.get("orderId")

            # Chỉ huỷ lệnh ENTRY (không phải SL/TP reduceOnly)
            # và coin đó không có position
            if not reduce and sym not in open_syms:
                try:
                    _exchange._delete("/fapi/v1/order",
                                      {"symbol": sym, "orderId": order_id})
                    cancelled.append(f"{sym} {otype}")
                except Exception as e:
                    logger.error(f"Cancel order {sym} {order_id}: {e}")
            else:
                kept.append(f"{sym} {otype}")

        msg = (f"🗑 Đã huỷ {len(cancelled)} lệnh chờ:\n"
               + "\n".join(f"• {c}" for c in cancelled[:10])
               + (f"\n⚠️ Còn {len(cancelled)-10} lệnh..." if len(cancelled) > 10 else "")
               + (f"\n✅ Giữ lại {len(kept)} lệnh có vị thế" if kept else ""))
        logger.info(f"[CancelPending] {msg}")
        return jsonify({"ok": True, "msg": msg, "cancelled": len(cancelled)})
    except Exception as e:
        logger.error(f"cancel_all_pending error: {e}")
        return jsonify({"ok": False, "msg": str(e)})


def _get_ai_bias_safe():
    try:
        from ai_analyzer import load_bias
        return load_bias()
    except Exception:
        return {}


def _get_pending_watch_safe():
    try:
        from scanner import _pending_watch
        return {sym: {"signal": v["signal"], "win_rate": v.get("win_rate", 0),
                      "retry": v.get("retry", 0), "score": v.get("score", 0)}
                for sym, v in _pending_watch.items()}
    except Exception:
        return {}


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    """Pause/Resume bot trading."""
    with _lock:
        current = _state.get("running", True)

    if not current:
        # Đang paused → gọi restart callback (đăng ký từ bot.py)
        restart_fn = _state.get("_restart_fn")
        if restart_fn:
            try:
                restart_fn()
                return jsonify({"ok": True, "msg": "Bot restarted ✅", "running": True})
            except Exception as e:
                return jsonify({"ok": False, "msg": f"Restart failed: {e}", "running": False})
        else:
            # Fallback: chỉ set running=True (thread còn sống)
            with _lock:
                _state["running"] = True
            return jsonify({"ok": True, "msg": "Bot resumed", "running": True})
    else:
        # Đang chạy → pause
        with _lock:
            _state["running"] = False
        logger.info("Bot paused via web")
        return jsonify({"ok": True, "msg": "Bot paused ⏸", "running": False})


def _save_coins_to_config(coins: list):
    """Ghi danh sách coins vào config.py để persist khi restart."""
    import os
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
        # Replace FIXED_COINS block
        import re
        new_block = "FIXED_COINS = [\n"
        for c in coins:
            new_block += f'    "{c}",\n'
        new_block += "]"
        content = re.sub(
            r'FIXED_COINS\s*=\s*\[.*?\]',
            new_block,
            content,
            flags=re.DOTALL
        )
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"Config saved: FIXED_COINS = {coins}")
    except Exception as e:
        logger.error(f"Failed to save config: {e}")


@app.route("/api/coins/add", methods=["POST"])
def api_add_coin():
    """Add coin to watchlist + save to config.py."""
    data = request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()
    if not symbol or not symbol.endswith("USDT"):
        return jsonify({"ok": False, "msg": "Symbol must end with USDT"})

    with _lock:
        wl = _state.get("_watchlist", [])
        if symbol in wl:
            return jsonify({"ok": False, "msg": f"{symbol} already in watchlist"})
        wl.append(symbol)
        _state["_watchlist"] = wl

    # Update scanner WATCHLIST
    try:
        from scanner import WATCHLIST
        if symbol not in WATCHLIST:
            WATCHLIST.append(symbol)
        # Cập nhật config.FIXED_COINS trong memory để scan_market dùng ngay
        import config as _cfg
        if hasattr(_cfg, "FIXED_COINS") and symbol not in _cfg.FIXED_COINS:
            _cfg.FIXED_COINS.append(symbol)
    except Exception:
        pass

    # Save to config.py
    _save_coins_to_config(wl)

    logger.info(f"Coin added: {symbol}")
    return jsonify({"ok": True, "msg": f"Added {symbol}"})


@app.route("/api/coins/remove", methods=["POST"])
def api_remove_coin():
    """Remove coin from watchlist + save to config.py."""
    data = request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()

    with _lock:
        wl = _state.get("_watchlist", [])
        if symbol not in wl:
            return jsonify({"ok": False, "msg": f"{symbol} not in watchlist"})
        wl.remove(symbol)
        _state["_watchlist"] = wl

    try:
        from scanner import WATCHLIST
        if symbol in WATCHLIST:
            WATCHLIST.remove(symbol)
        # Cập nhật config.FIXED_COINS trong memory để scan_market dùng ngay
        import config as _cfg
        if hasattr(_cfg, "FIXED_COINS") and symbol in _cfg.FIXED_COINS:
            _cfg.FIXED_COINS.remove(symbol)
    except Exception:
        pass

    logger.info(f"Coin removed: {symbol}")
    _save_coins_to_config(wl)
    return jsonify({"ok": True, "msg": f"Removed {symbol}"})


@app.route("/api/order", methods=["POST"])
def api_place_order():
    """Manual order: LONG/SHORT a coin with X USDT margin, optional SL/TP/Leverage."""
    data = request.get_json() or {}
    symbol = data.get("symbol", "").upper()
    side = data.get("side", "").upper()  # LONG or SHORT
    usdt = float(data.get("usdt", 0))
    sl = float(data.get("sl", 0))
    tp = float(data.get("tp", 0))
    leverage = int(data.get("leverage", getattr(_config, "LEVERAGE", 10)))
    # 0 = dùng config mặc định
    if usdt <= 0:
        usdt = float(getattr(_config, "MAX_ORDER_USDT", 15))
    if leverage <= 0:
        leverage = int(getattr(_config, "LEVERAGE", 10))

    if not symbol or side not in ("LONG", "SHORT") or usdt <= 0:
        return jsonify({"ok": False, "msg": "Invalid params"})

    if _exchange is None:
        return jsonify({"ok": False, "msg": "Exchange not initialized"})

    try:
        price = _exchange.get_ticker_price(symbol)

        # Tính qty dùng stepSize thật từ Binance
        from qty_utils import calc_qty_precise
        qty, _qty_info = calc_qty_precise(_exchange, symbol, usdt, leverage, price)

        # Smart entry: tìm giá tốt hơn từ chart 1m
        from smart_entry import find_optimal_entry, place_smart_order
        entry_info = find_optimal_entry(_exchange, symbol, side, _config)

        # Override SL/TP nếu user nhập
        if sl > 0:
            entry_info["sl"] = sl
        if tp > 0:
            entry_info["tp"] = tp

        result = place_smart_order(_exchange, symbol, side, qty, entry_info, _config,
                                    bot_state=_state, bot_lock=_lock)

        with _lock:
            _state["trade_log"].append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol, "side": side,
                "entry": result["price"], "sl": entry_info["sl"], "tp": entry_info["tp"],
                "qty": qty, "status": "OPEN",
                "note": f"web_{result['type'].lower()}"
            })

        sl_tp_msg = ""
        if entry_info["sl"]: sl_tp_msg += f" SL=${entry_info['sl']:.4f}"
        if entry_info["tp"]: sl_tp_msg += f" TP=${entry_info['tp']:.4f}"
        order_type = "LIMIT (chờ khớp)" if result["type"] == "LIMIT" else "MARKET"

        logger.info(f"Smart order: {side} {symbol} qty={qty} {order_type}{sl_tp_msg}")
        return jsonify({"ok": True, "msg": f"{side} {symbol} @ ${result['price']:.4f} [{order_type}] qty={qty}{sl_tp_msg}"})

    except Exception as e:
        logger.error(f"Manual order failed: {e}")
        return jsonify({"ok": False, "msg": str(e)[:200]})


def _round_qty(symbol: str, qty: float, price: float) -> float:
    """Round qty theo stepSize — fallback khi không có exchange instance."""
    # Dùng price-based estimate nếu không có exchange
    if price >= 10000: return round(int(qty / 0.001) * 0.001, 3)
    if price >= 1000:  return round(int(qty / 0.001) * 0.001, 3)
    if price >= 100:   return round(int(qty / 0.01) * 0.01, 2)
    if price >= 10:    return round(int(qty / 0.1) * 0.1, 1)
    if price >= 1:     return float(int(qty))
    if price >= 0.01:  return float(int(qty))
    return float(int(qty))


@app.route("/api/ai/run", methods=["POST"])
def api_ai_run():
    """Manually trigger AI analysis."""
    import threading as _t

    def _run():
        try:
            from ai_analyzer import analyze_all
            with _lock:
                wl = list(_state.get("_watchlist", []))
                _state["ai_analyzing"] = True
            analyze_all(wl)
            with _lock:
                _state["ai_analyzing"] = False
                _state["ai_last_run"] = datetime.now().strftime("%H:%M")
        except Exception as e:
            logger.error(f"Manual AI analysis error: {e}")
            with _lock:
                _state["ai_analyzing"] = False

    _t.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "msg": "AI Analysis started (2-5 min/coin)..."})


@app.route("/api/cancel_order", methods=["POST"])
def api_cancel_order():
    """Cancel a specific pending order."""
    data = request.get_json() or {}
    symbol = data.get("symbol", "").upper()
    order_id = data.get("order_id", "")
    if not symbol or not order_id:
        return jsonify({"ok": False, "msg": "Missing symbol or order_id"})
    if _exchange is None:
        return jsonify({"ok": False, "msg": "Exchange not initialized"})
    try:
        _exchange._delete("/fapi/v1/order", {"symbol": symbol, "orderId": int(order_id)})
        return jsonify({"ok": True, "msg": f"Cancelled order {symbol}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)[:200]})


@app.route("/api/close", methods=["POST"])
def api_close_position():
    """Close a specific position by symbol."""
    data = request.get_json() or {}
    symbol = data.get("symbol", "").upper()
    if not symbol:
        return jsonify({"ok": False, "msg": "No symbol"})
    if _exchange is None:
        return jsonify({"ok": False, "msg": "Exchange not initialized"})

    try:
        all_pos = _exchange._get("/fapi/v2/positionRisk", signed=True)
        pos = [p for p in all_pos if p["symbol"] == symbol and abs(float(p.get("positionAmt", 0))) > 0]
        if not pos:
            return jsonify({"ok": False, "msg": f"No open position for {symbol}"})

        p = pos[0]
        amt = float(p["positionAmt"])
        entry = float(p.get("entryPrice", 0))
        side_pos = "LONG" if amt > 0 else "SHORT"
        close_side = "SELL" if amt > 0 else "BUY"
        qty = abs(amt)
        if qty == int(qty):
            qty = int(qty)
        close_price = _exchange.get_ticker_price(symbol)

        # Binance MARKET_LOT_SIZE maxQty = 100000 cho một số coin
        # Chia nhỏ nếu qty > 100000
        max_market_qty = 100000
        remaining = qty
        while remaining > 0:
            batch = min(remaining, max_market_qty)
            if batch == int(batch):
                batch = int(batch)
            _exchange.place_market_order(symbol, close_side, batch)
            remaining -= batch

        _exchange.cancel_all_orders(symbol)

        # Tính PnL
        if side_pos == "LONG":
            pnl_usd = qty * (close_price - entry)
            pnl_pct = (close_price - entry) / entry * 100
        else:
            pnl_usd = qty * (entry - close_price)
            pnl_pct = (entry - close_price) / entry * 100

        # Ghi vào trade_log
        with _lock:
            # Tìm lệnh OPEN tương ứng và update
            found = False
            for t in reversed(_state.get("trade_log", [])):
                if t.get("symbol") == symbol and t.get("status") == "OPEN":
                    t.update({
                        "status": "CLOSED",
                        "close": close_price,
                        "pnl_usdt": round(pnl_usd, 2),
                        "pnl_pct": round(pnl_pct, 2),
                    })
                    found = True
                    break
            if not found:
                # Thêm mới nếu không tìm thấy (lệnh mở từ trước khi bot chạy)
                _state.setdefault("trade_log", []).append({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": symbol, "side": side_pos,
                    "entry": entry, "close": close_price,
                    "qty": qty, "status": "CLOSED",
                    "pnl_usdt": round(pnl_usd, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "note": "closed_web"
                })

        # Save to file
        try:
            from trade_history import save_history
            save_history(_state["trade_log"])
        except Exception:
            pass

        icon = "✅" if pnl_usd >= 0 else "❌"
        logger.info(f"Closed position: {symbol} qty={qty} pnl=${pnl_usd:+.2f}")
        return jsonify({"ok": True, "msg": f"{icon} Closed {symbol} PnL: ${pnl_usd:+.2f} ({pnl_pct:+.1f}%)"})
    except Exception as e:
        logger.error(f"Close position failed: {e}")
        return jsonify({"ok": False, "msg": str(e)[:200]})


@app.route("/api/auto_sltp", methods=["POST"])
def api_auto_sltp():
    """Auto set SL/TP for a position (or ALL) using chart analysis."""
    data = request.get_json() or {}
    symbol = data.get("symbol", "").upper()
    if not symbol:
        return jsonify({"ok": False, "msg": "No symbol"})
    if _exchange is None:
        return jsonify({"ok": False, "msg": "Exchange not initialized"})

    try:
        from auto_sltp import get_positions_without_sltp, auto_set_sltp
        liq_tracker = _state.get("liq_tracker") if _state else None
        unprotected = get_positions_without_sltp(_exchange)

        if symbol == "ALL":
            if not unprotected:
                return jsonify({"ok": True, "msg": "All positions already have SL/TP"})
            results = []
            for pos in unprotected:
                r = auto_set_sltp(_exchange, pos["symbol"], pos["side"],
                                  pos["entry"], pos["qty"], liq_tracker)
                results.append(f"{pos['symbol']}: {'OK' if r['ok'] else 'FAILED'}")
            msg = "Set SL/TP:\n" + "\n".join(results)
            return jsonify({"ok": True, "msg": msg})
        else:
            pos = next((p for p in unprotected if p["symbol"] == symbol), None)
            if not pos:
                return jsonify({"ok": True, "msg": f"{symbol} already has SL/TP or no position"})
            r = auto_set_sltp(_exchange, pos["symbol"], pos["side"],
                              pos["entry"], pos["qty"], liq_tracker)
            return jsonify({"ok": r["ok"], "msg": r["msg"]})

    except Exception as e:
        logger.error(f"Auto SL/TP failed: {e}")
        return jsonify({"ok": False, "msg": str(e)[:200]})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    """Update bot settings: MAX_ORDER_USDT, LEVERAGE."""
    data = request.get_json() or {}
    max_usdt = data.get("max_order_usdt")
    leverage = data.get("leverage")

    msgs = []
    if max_usdt is not None and float(max_usdt) > 0:
        _config.MAX_ORDER_USDT = float(max_usdt)
        msgs.append(f"USD/order=${max_usdt}")
    if leverage is not None and 1 <= int(leverage) <= 125:
        _config.LEVERAGE = int(leverage)
        msgs.append(f"Leverage={leverage}x")

    if not msgs:
        return jsonify({"ok": False, "msg": "No valid settings"})

    logger.info(f"Settings updated: {', '.join(msgs)}")
    return jsonify({"ok": True, "msg": f"Updated: {', '.join(msgs)}"})


@app.route("/api/pump", methods=["GET"])
def api_pump_state():
    """Trả về trạng thái pump radar: danh sách coin đang theo dõi + signals gần nhất."""
    if _state is None:
        return jsonify({"ok": False})
    with _lock:
        watch   = list(_state.get("pump_watch_coins", []))
        signals = list(_state.get("pump_signals", []))
        status  = dict(_state.get("pump_scan_status", {}))
        prices  = dict(_state.get("prices", {}))

    # Build coin rows với pump score nếu có
    rows = []
    for sym in watch:
        price = prices.get(sym, 0)
        # Tìm signal gần nhất cho coin này
        sig = next((s for s in reversed(signals) if s.get("symbol") == sym), None)
        rows.append({
            "symbol":      sym,
            "price":       price,
            "pump_pct":    sig["pump_pct"]    if sig else 0,
            "score":       sig["score"]       if sig else 0,
            "is_top":      sig["is_pump_top"] if sig else False,
            "rsi":         sig["rsi"]         if sig else 0,
            "vol_ratio":   sig["volume_ratio"] if sig else 0,
            "entry":       sig["entry_price"] if sig else 0,
            "sl":          sig["sl_price"]    if sig else 0,
            "tp1":         sig["tp1_price"]   if sig else 0,
            "signals":     sig["signals"]     if sig else [],
            "ts":          sig["timestamp"]   if sig else 0,
        })

    return jsonify({
        "ok":       True,
        "status":   status,
        "coins":    rows,
        "history":  signals[-20:],   # 20 tín hiệu gần nhất
        "auto_short": getattr(_config, "PUMP_AUTO_SHORT", False),
        "min_score":  getattr(_config, "PUMP_TOP_MIN_SCORE", 60),
    })


@app.route("/api/pump/coins/add", methods=["POST"])
def api_pump_add_coin():
    """Thêm coin vào danh sách pump watch (quét riêng, nhanh hơn)."""
    data   = request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"ok": False, "msg": "Thiếu symbol"})
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    with _lock:
        watch = _state.get("pump_watch_coins", [])
        if symbol in watch:
            return jsonify({"ok": False, "msg": f"{symbol} đã có trong danh sách"})
        watch.append(symbol)
        _state["pump_watch_coins"] = watch

    # Sync vào config memory để pump_scan_engine đọc
    try:
        import config as _cfg
        if not hasattr(_cfg, "PUMP_WATCH_COINS"):
            _cfg.PUMP_WATCH_COINS = []
        if symbol not in _cfg.PUMP_WATCH_COINS:
            _cfg.PUMP_WATCH_COINS.append(symbol)
    except Exception:
        pass

    # Lưu vào config.py file
    _save_pump_coins_to_config(watch)
    logger.info(f"[PumpRadar] Added pump coin: {symbol}")
    return jsonify({"ok": True, "msg": f"Đã thêm {symbol} vào Pump Radar ✅"})


@app.route("/api/pump/coins/remove", methods=["POST"])
def api_pump_remove_coin():
    """Xóa coin khỏi danh sách pump watch."""
    data   = request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()

    with _lock:
        watch = _state.get("pump_watch_coins", [])
        if symbol not in watch:
            return jsonify({"ok": False, "msg": f"{symbol} không có trong danh sách"})
        watch.remove(symbol)
        _state["pump_watch_coins"] = watch
        # Xóa signals cũ của coin này
        _state["pump_signals"] = [s for s in _state.get("pump_signals", [])
                                   if s.get("symbol") != symbol]

    try:
        import config as _cfg
        if hasattr(_cfg, "PUMP_WATCH_COINS") and symbol in _cfg.PUMP_WATCH_COINS:
            _cfg.PUMP_WATCH_COINS.remove(symbol)
    except Exception:
        pass

    _save_pump_coins_to_config(watch)
    logger.info(f"[PumpRadar] Removed pump coin: {symbol}")
    return jsonify({"ok": True, "msg": f"Đã xóa {symbol} khỏi Pump Radar"})


@app.route("/api/pump/toggle_auto", methods=["POST"])
def api_pump_toggle_auto():
    """Bật/tắt PUMP_AUTO_SHORT."""
    data    = request.get_json() or {}
    enabled = bool(data.get("enabled", False))
    try:
        import config as _cfg
        _cfg.PUMP_AUTO_SHORT = enabled
    except Exception:
        pass
    msg = "🔴 AUTO SHORT bật — bot sẽ tự vào lệnh khi phát hiện đỉnh pump" if enabled \
          else "⏸ AUTO SHORT tắt — chỉ gửi Telegram alert"
    logger.info(f"[PumpRadar] PUMP_AUTO_SHORT = {enabled}")
    return jsonify({"ok": True, "msg": msg, "enabled": enabled})


def _save_pump_coins_to_config(coins: list):
    """Ghi PUMP_WATCH_COINS vào config.py."""
    import os, re
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
        new_block = "PUMP_WATCH_COINS = [\n"
        for c in coins:
            new_block += f'    "{c}",\n'
        new_block += "]"
        content = re.sub(
            r'PUMP_WATCH_COINS\s*=\s*\[.*?\]',
            new_block,
            content,
            flags=re.DOTALL
        )
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[PumpRadar] Config saved: PUMP_WATCH_COINS = {coins}")
    except Exception as e:
        logger.error(f"[PumpRadar] Save config failed: {e}")


def start_web_dashboard(state, lock, config, port=5555, exchange=None):
    """Start web dashboard in background thread."""
    global _state, _lock, _config, _exchange
    _state = state
    _lock = lock
    _config = config
    _exchange = exchange

    # Store watchlist in state for web access
    from scanner import WATCHLIST
    with lock:
        state["_watchlist"] = list(WATCHLIST)
        # Khởi tạo pump watch list nếu chưa có
        if "pump_watch_coins" not in state:
            state["pump_watch_coins"] = list(getattr(config, "PUMP_WATCH_COINS", []))
        if "pump_signals" not in state:
            state["pump_signals"] = []   # list PumpSignal gần nhất
        if "pump_scan_status" not in state:
            state["pump_scan_status"] = {"scanning": False, "last_scan": "--:--", "scan_count": 0}

    def run():
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.WARNING)
        try:
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
        except OSError as e:
            logger.warning(f"Web dashboard port {port} error: {e}")
        except Exception as e:
            logger.warning(f"Web dashboard error: {e}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    logger.info(f"Web dashboard started at http://localhost:{port}")
    return t
