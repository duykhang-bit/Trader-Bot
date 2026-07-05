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

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Bot Dashboard</title>
<style>
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
</style>
<style>
/* Controls */
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
            <span style="color:#8b949e;font-size:12px">Scan #${d.scan_no} | Last: ${d.last_scan}${d.ai_last_run ? ' | AI: '+d.ai_last_run : ''}${d.ai_analyzing ? ' ⏳ AI analyzing...' : ''}</span>
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
        <div class="card"><div class="label">Balance</div><div class="value blue">${fmtUsd(d.balance)}</div></div>
        <div class="card"><div class="label">Today PnL</div><div class="value ${pnlColor(d.today_pnl)}">${fmtUsd(d.today_pnl)}</div></div>
        <div class="card"><div class="label">Total PnL</div><div class="value ${pnlColor(d.total_pnl)}">${fmtUsd(d.total_pnl)}</div></div>
        <div class="card"><div class="label">Unrealized</div><div class="value ${pnlColor(d.unrealized)}">${fmtUsd(d.unrealized)}</div></div>
        <div class="card"><div class="label">Win Rate</div><div class="value">${fmt(d.win_rate,0)}%</div></div>
        <div class="card"><div class="label">Trades</div><div class="value">${d.total_trades}</div></div>
    </div>`;

    // Open Positions
    if (d.open_positions && d.open_positions.length > 0) {
        html += `<div class="section"><h2>&#x1F4CC; Open Positions</h2>
            <button class="btn btn-green btn-sm" onclick="autoSetSlTpAll()" style="margin-bottom:8px">&#x1F6E1; Auto Set SL/TP ALL</button>
            <table>
            <tr><th>Coin</th><th>Side</th><th>Entry</th><th>Mark</th><th>PnL</th><th>%</th><th>Lev</th><th></th></tr>`;
        d.open_positions.forEach(p => {
            html += `<tr><td><b>${p.symbol.replace('USDT','')}</b></td><td>${sideHtml(p.side)}</td>
                <td>${fmtUsd(p.entry)}</td><td>${fmtUsd(p.mark)}</td>
                <td class="${pnlColor(p.pnl)}"><b>${fmtUsd(p.pnl)}</b></td>
                <td class="${pnlColor(p.pct)}">${fmt(p.pct,1)}%</td><td>${p.lev}x</td>
                <td><button class="btn btn-green btn-sm" onclick="autoSetSlTp('${p.symbol}')" title="Auto SL/TP">&#x1F6E1;</button>
                <button class="btn btn-red btn-sm" onclick="closePosition('${p.symbol}')">Close</button></td></tr>`;
        });
        html += `</table></div>`;
    }

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
        let trendText = 'SCANNING';
        let trendCls = '';
        if (cand) {
            if (cand.signal === 'LONG') { trendIcon = '&#x1F7E2;'; trendText = 'LONG'; trendCls = 'green'; }
            else if (cand.signal === 'SHORT') { trendIcon = '&#x1F534;'; trendText = 'SHORT'; trendCls = 'red'; }
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
    html += `<b style="font-size:12px;color:#58a6ff">&#x1F3AF; TRIGGER PRICES (bot vào lệnh khi giá đạt):</b>`;
    html += `<table style="margin-top:8px"><tr><th>Coin</th><th style="color:#f85149">SHORT khi giá ≥</th><th style="color:#3fb950">LONG khi giá ≤</th><th>Current</th><th>Gap</th></tr>`;
    d.watchlist.forEach(sym => {
        const name = sym.replace('USDT','');
        const p = d.prices[sym] || 0;
        const targets = (d.entry_targets || {})[sym] || {};
        const shortP = targets.short_entry || 0;
        const longP = targets.long_entry || 0;
        const shortStr = shortP >= 1000 ? fmtUsd(shortP) : '$'+fmt(shortP, shortP>=1?2:5);
        const longStr = longP >= 1000 ? fmtUsd(longP) : '$'+fmt(longP, longP>=1?2:5);
        const curStr = p >= 1000 ? fmtUsd(p) : '$'+fmt(p, p>=1?2:5);
        const shortGap = shortP > 0 ? fmt((shortP-p)/p*100,2)+'%' : '-';
        const longGap = longP > 0 ? fmt((p-longP)/p*100,2)+'%' : '-';
        html += `<tr>
            <td><b>${name}</b></td>
            <td style="color:#f85149">${shortStr} <span style="font-size:10px;color:#8b949e">(+${shortGap})</span></td>
            <td style="color:#3fb950">${longStr} <span style="font-size:10px;color:#8b949e">(-${longGap})</span></td>
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
            html += `<div class="price-item"><div class="coin">${name}</div><div class="price">${pStr}</div></div>`;
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

function updateClock(){document.getElementById('clock').textContent=new Date().toLocaleTimeString()}

// Lưu state input để không bị reset khi refresh
let _savedInputs = {};
function saveInputs() {
    ['order-symbol','order-side','order-usdt','order-sl','order-tp','order-lev','set-max-usdt','set-leverage','add-coin-input'].forEach(id => {
        const el = document.getElementById(id);
        if (el) _savedInputs[id] = el.value;
    });
}
function restoreInputs() {
    for (const [id, val] of Object.entries(_savedInputs)) {
        const el = document.getElementById(id);
        if (el && val) el.value = val;
    }
}

async function refresh(){
    try{
        saveInputs();
        const r=await fetch('/api/state'); const d=await r.json();
        document.getElementById('content').innerHTML=renderDashboard(d);
        restoreInputs();
    }
    catch(e){ document.getElementById('content').innerHTML='<p style="color:#f85149">Connection lost...</p>'; }
}
setInterval(updateClock,1000); setInterval(refresh,2000); updateClock(); refresh();
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

    # Pending orders (lệnh chờ khớp)
    pending_orders = []
    try:
        if _exchange:
            all_orders = _exchange._get("/fapi/v1/openOrders", signed=True)
            for o in all_orders:
                pending_orders.append({
                    "symbol": o.get("symbol", ""),
                    "side": o.get("side", ""),
                    "type": o.get("type", ""),
                    "qty": float(o.get("origQty", 0)),
                    "price": float(o.get("price", 0) or o.get("stopPrice", 0)),
                    "order_id": str(o.get("orderId", "")),
                })
    except Exception:
        pass

    recent = sorted(closed, key=lambda t: t.get("time",""), reverse=True)[:15]
    trades_fmt = [{"symbol":t.get("symbol",""),"side":t.get("side",""),"entry":t.get("entry",0),
        "close":t.get("close",0),"pnl":t.get("pnl_usdt",0),"pct":t.get("pnl_pct",0),
        "time":t.get("time","")} for t in recent]

    # Add entry targets - dùng smart_entry tìm giá entry tối ưu
    entry_targets = {}
    liq_tracker = _state.get("liq_tracker") if _state else None
    for sym in watchlist:
        p = prices.get(sym, 0)
        if p <= 0:
            continue
        above = None
        below = None
        if liq_tracker:
            try:
                above = liq_tracker.get_nearest_liq_above(sym, p, min_usd=1000)
                below = liq_tracker.get_nearest_liq_below(sym, p, min_usd=1000)
            except Exception:
                pass
        # Fallback: dùng ±1% estimate
        if above is None:
            above = round(p * 1.01, 2 if p >= 100 else 6)
        if below is None:
            below = round(p * 0.99, 2 if p >= 100 else 6)
        entry_targets[sym] = {"short_entry": float(above), "long_entry": float(below)}

    resp = jsonify({
        "running": s.get("running", False),
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
        "split_positions_web": [{
            "symbol": sym, "direction": sp.direction,
            "entry1": sp.entry1, "entry2": sp.entry2,
            "sl": sp.sl, "tp": sp.tp,
            "filled1": sp.filled1, "filled2": sp.filled2,
        } for sym, sp in splits.items()],
        "entry_targets": entry_targets,
        "ai_bias": _get_ai_bias_safe(),
    })
    return resp


def _get_ai_bias_safe():
    try:
        from ai_analyzer import load_bias
        return load_bias()
    except Exception:
        return {}


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    """Pause/Resume bot trading."""
    with _lock:
        current = _state.get("running", True)
        _state["running"] = not current
    new_status = "running" if not current else "paused"
    logger.info(f"Bot toggled: {new_status}")
    return jsonify({"ok": True, "msg": f"Bot {new_status}", "running": not current})


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

    if not symbol or side not in ("LONG", "SHORT") or usdt <= 0:
        return jsonify({"ok": False, "msg": "Invalid params"})

    if _exchange is None:
        return jsonify({"ok": False, "msg": "Exchange not initialized"})

    try:
        price = _exchange.get_ticker_price(symbol)
        notional = usdt * leverage
        qty = notional / price

        # Round qty theo stepSize của Binance
        qty = _round_qty(symbol, qty, price)

        # Min notional $5
        if qty * price < 5.0:
            qty = _round_qty(symbol, 5.0 / price + 0.01, price)

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
    """Round qty theo stepSize phù hợp với giá coin."""
    # Binance stepSize rules (phổ biến):
    # BTC: 0.001, ETH: 0.001, SOL: 1, BNB: 0.01, altcoins nhỏ: 1 hoặc 0.1
    if price >= 10000:   # BTC
        return round(qty, 3)
    elif price >= 1000:  # ETH
        return round(qty, 3)
    elif price >= 100:   # BNB, SOL
        return round(qty, 2)
    elif price >= 10:    # SOL, mid-cap
        step = 0.1
        return round(int(qty / step) * step, 1)
    elif price >= 1:     # altcoins
        return round(qty, 1)
    elif price >= 0.01:  # small altcoins
        return round(qty, 0)
    else:                # very small
        return round(qty, 0)


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
