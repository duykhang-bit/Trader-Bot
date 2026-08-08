# ============================================================
# WEB DASHBOARD — Real-time Trading Bot Dashboard
# http://localhost:5555
# Features: Start/Stop, Add/Remove coins, Manual order
# ============================================================
import threading
import logging
import json
import time
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request, session, redirect, url_for
from functools import wraps

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["TESTING"] = False
app.config["SECRET_KEY"] = "changeme"  # sẽ được override trong start_web_dashboard

# Set from bot.py
_state = None
_lock = None
_config = None
_exchange = None

# ── Auth helpers ──────────────────────────────────────────────────────────────
def require_auth(f):
    """Decorator bảo vệ route — redirect về login nếu chưa đăng nhập."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            # API endpoints trả 401, page endpoints redirect login
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "msg": "Unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


LOGIN_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trading Bot — Login</title>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body {
      background: #0d1117;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      font-family: 'Segoe UI', sans-serif;
    }
    .card {
      background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
      border: 1px solid #30363d;
      border-radius: 16px;
      padding: 40px 36px;
      width: 100%;
      max-width: 380px;
      box-shadow: 0 8px 32px rgba(0,0,0,.6);
    }
    .logo {
      text-align: center;
      margin-bottom: 28px;
    }
    .logo .icon { font-size: 36px; }
    .logo h1 {
      color: #e6edf3;
      font-size: 20px;
      font-weight: 700;
      margin-top: 8px;
      letter-spacing: 1px;
    }
    .logo p { color: #484f58; font-size: 12px; margin-top: 4px; }
    .form-group { margin-bottom: 18px; }
    label { color: #8b949e; font-size: 12px; display: block; margin-bottom: 6px; }
    input[type=password] {
      width: 100%;
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 8px;
      color: #e6edf3;
      font-size: 15px;
      padding: 10px 14px;
      outline: none;
      transition: border-color .2s;
    }
    input[type=password]:focus { border-color: #388bfd; }
    .btn-login {
      width: 100%;
      background: linear-gradient(135deg, #238636, #2ea043);
      color: #fff;
      border: none;
      border-radius: 8px;
      padding: 11px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      transition: opacity .2s;
      letter-spacing: 1px;
    }
    .btn-login:hover { opacity: .88; }
    .error {
      background: rgba(248,81,73,.12);
      border: 1px solid rgba(248,81,73,.4);
      color: #f85149;
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 12px;
      margin-bottom: 16px;
      text-align: center;
    }
    .dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: #3fb950;
      display: inline-block;
      box-shadow: 0 0 6px #3fb950;
      margin-right: 6px;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <div class="icon">🤖</div>
      <h1><span class="dot"></span>Trading Bot</h1>
      <p>Nhập mật khẩu để truy cập dashboard</p>
    </div>
    {% if error %}
    <div class="error">❌ {{ error }}</div>
    {% endif %}
    <form method="POST" action="/login">
      <div class="form-group">
        <label>MẬT KHẨU</label>
        <input type="password" name="password" placeholder="••••••••••"
               autofocus autocomplete="current-password">
      </div>
      <button type="submit" class="btn-login">🔓 ĐĂNG NHẬP</button>
    </form>
  </div>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pwd = request.form.get("password", "")
        correct = getattr(_config, "WEB_PASSWORD", "Cr7naldojk")
        if pwd == correct:
            session["authenticated"] = True
            session.permanent = True
            return redirect("/")
        else:
            error = "Sai mật khẩu, thử lại."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

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
/* PnL Stats */
.pnl-stats-tabs { display: flex; gap: 6px; margin-bottom: 12px; }
.pnl-tab { padding: 5px 16px; border-radius: 6px; border: 1px solid #30363d; background: #0d1117; color: #8b949e; cursor: pointer; font-size: 13px; transition: all .2s; }
.pnl-tab.active { background: #1f6feb; border-color: #1f6feb; color: #fff; font-weight: 600; }
.pnl-bar-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.pnl-bar-label { width: 90px; font-size: 12px; color: #8b949e; flex-shrink: 0; text-align: right; }
.pnl-bar-wrap { flex: 1; background: #161b22; border-radius: 4px; height: 20px; overflow: hidden; position: relative; }
.pnl-bar-fill { height: 100%; border-radius: 4px; transition: width .4s; }
.pnl-bar-val { position: absolute; right: 6px; top: 50%; transform: translateY(-50%); font-size: 12px; font-weight: 600; }
.pnl-bar-meta { width: 80px; font-size: 11px; color: #8b949e; flex-shrink: 0; text-align: right; }
.pnl-summary-row { display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.pnl-summary-card { flex: 1; min-width: 100px; background: #0d1117; border: 1px solid #21262d; border-radius: 8px; padding: 10px 14px; text-align: center; }
.pnl-summary-card .lbl { font-size: 11px; color: #8b949e; margin-bottom: 4px; }
.pnl-summary-card .val { font-size: 18px; font-weight: 700; }
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
/* ── Pump Nhẹ Radar ── */
.pnhe-wrap { background: linear-gradient(135deg,#0d1117 0%,#0a0d14 100%); border: 1px solid #1a2a3d; border-radius: 12px; padding: 16px; }
.pnhe-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; flex-wrap:wrap; gap:10px; }
.pnhe-title { display:flex; align-items:center; gap:10px; }
.pnhe-dot { width:9px; height:9px; border-radius:50%; background:#388bfd; box-shadow:0 0 8px #388bfd; animation:pulseDot 1.4s ease-in-out infinite; }
.pnhe-coin-list { display:flex; flex-direction:column; gap:6px; }
.pnhe-card { background:#0d1117; border:1px solid #1a2a3d; border-radius:8px; padding:10px 12px; transition:border-color .3s; }
.pnhe-card:hover { border-color:#30363d; }
.pnhe-card.strong  { border-color:rgba(248,81,73,.45); background:rgba(248,81,73,.05); }
.pnhe-card.medium  { border-color:rgba(210,153,34,.45); background:rgba(210,153,34,.04); }
.pnhe-card.soft    { border-color:rgba(56,139,253,.4);  background:rgba(56,139,253,.04); }
.pnhe-card.dump    { border-color:rgba(139,73,248,.35); background:rgba(139,73,248,.04); }
.pnhe-card.flat    { border-color:#1a2a3d; }
.pnhe-bar-wrap { background:#0a1420; border-radius:3px; height:5px; overflow:hidden; margin-top:5px; }
.pnhe-bar-fill { height:100%; border-radius:3px; transition:width .5s; }
.pnhe-empty { text-align:center; padding:32px 16px; color:#1a3a5a; border:1px dashed #1a2a3d; border-radius:8px; font-size:12px; }
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
            <a href="/logout" title="Đăng xuất"
               style="color:#484f58;text-decoration:none;border:1px solid #30363d;border-radius:5px;
                      padding:2px 8px;font-size:11px;margin-left:8px;transition:color .2s"
               onmouseover="this.style.color='#f85149';this.style.borderColor='#f85149'"
               onmouseout="this.style.color='#484f58';this.style.borderColor='#30363d'">
              🔓 Logout
            </a>
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
        const r = await fetch(url, {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify(body),
            signal: AbortSignal.timeout(8000)  // 8s timeout
        });
        const d = await r.json();
        if (d.ok) toast(d.msg || 'OK'); else toast(d.msg || 'Error', false);
        return d;
    } catch(e) {
        const msg = e.name === 'TimeoutError' ? 'Request timeout — thử lại' : 'Request failed';
        toast(msg, false);
        return {ok:false};
    }
}

async function toggleBot() { await apiPost('/api/toggle'); refresh(); }
async function quickShort() {
    const sym = document.getElementById('qs-symbol').value.trim().toUpperCase();
    if (!sym) { toast('Nhập coin!', false); return; }
    const r = await apiPost('/api/quick_trade', {symbol: sym, side: 'SHORT'});
    if (r && r.ok) { toast(r.msg); refresh(); } else { toast(r?.msg || 'Lỗi', false); }
}
async function quickLong() {
    const sym = document.getElementById('qs-symbol').value.trim().toUpperCase();
    if (!sym) { toast('Nhập coin!', false); return; }
    const r = await apiPost('/api/quick_trade', {symbol: sym, side: 'LONG'});
    if (r && r.ok) { toast(r.msg); refresh(); } else { toast(r?.msg || 'Lỗi', false); }
}
async function toggleOrphan(enabled) {
    await apiPost('/api/set_auto_cancel', {enabled: enabled});
    refresh();
}
async function toggleReversalMonitor(mode) {
    // mode: 'off' | 'alert' | 'auto'
    let payload = {};
    if (mode === 'off')   payload = {enabled: false, alert_only: false};
    if (mode === 'alert') payload = {enabled: true,  alert_only: true};
    if (mode === 'auto')  payload = {enabled: true,  alert_only: false};
    const r = await apiPost('/api/reversal_monitor', payload);
    if (r && r.msg) toast(r.msg, r.ok);
    refresh();
}
async function toggleScanProtector(enabled) {
    const r = await apiPost('/api/scan_protector', {enabled});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    refresh();
}
async function toggleProfitLock(enabled) {
    const r = await apiPost('/api/profit_lock', {enabled});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    refresh();
}
async function toggleTrailingLock(enabled) {
    const r = await apiPost('/api/trailing_lock', {enabled});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    refresh();
}
async function toggleMaxLoss(enabled) {
    const r = await apiPost('/api/max_loss', {enabled});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    refresh();
}
async function setMaxLoss() {
    const val = parseFloat(document.getElementById('max-loss-input').value);
    if (!val || val < 5) { toast('Min $5', false); return; }
    const r = await apiPost('/api/max_loss', {enabled: true, value: val});
    if (r && r.msg) toast(r.msg, r.ok !== false);
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

    // ── Quick SHORT/LONG — bấm 1 nút vào ngay ──
    html += `<div class="section" style="background:linear-gradient(135deg,#0d1117,#1a0a0a);border:1px solid #5a1a1a;border-radius:12px;padding:14px;margin-bottom:12px">
        <h2 style="font-size:14px;margin:0 0 10px 0;color:#f85149">&#x26A1; Quick Trade</h2>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
            <input id="qs-symbol" placeholder="HEIUSDT" style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:6px 10px;color:#e6edf3;font-size:13px;width:130px">
            <button onclick="quickShort()" style="background:#7a1a1a;color:#ff6b6b;border:1px solid #aa2a2a;border-radius:6px;padding:6px 14px;font-weight:700;font-size:13px;cursor:pointer">&#x1F534; SHORT</button>
            <button onclick="quickLong()" style="background:#0d2a0d;color:#3fb950;border:1px solid #1a5a1a;border-radius:6px;padding:6px 14px;font-weight:700;font-size:13px;cursor:pointer">&#x1F7E2; LONG</button>
            <span style="font-size:11px;color:#8b949e">$${d.settings?.max_order_usdt||15} × ${d.settings?.leverage||15}x</span>
        </div>
    </div>`;

    // Control Panel
    html += `<div class="section"><h2>&#x2699; Controls</h2>
        <div class="control-row">
            <button id="toggle-bot-btn" class="btn ${running ? 'btn-red' : 'btn-green'}" onclick="toggleBot()">
                ${running ? '&#x23F8; Pause Bot' : '&#x25B6; Start Bot'}
            </button>
            <button class="btn btn-blue" onclick="runAI()">&#x1F9E0; Run AI Analysis</button>
            <span id="scan-info" style="color:#8b949e;font-size:12px">Scan #${d.scan_no} | Last: ${d.last_scan}${d.ai_last_run ? ' | AI: '+d.ai_last_run : ''}${d.ai_analyzing ? ' ⏳ AI analyzing...' : ''}</span>
        </div>
        <div class="control-row" style="margin-top:8px;align-items:center;gap:8px;flex-wrap:wrap">
            <label style="font-size:12px;color:#8b949e;display:flex;align-items:center;gap:6px;cursor:pointer">
                <input type="checkbox" id="toggle-orphan" ${d.auto_cancel_orphan ? 'checked' : ''}
                    onchange="toggleOrphan(this.checked)"
                    style="width:14px;height:14px;cursor:pointer">
                <span>&#x1F9F9; Tự động huỷ lệnh entry chờ không có vị thế</span>
            </label>
            <button class="btn btn-red btn-sm" onclick="cancelAllPending()" style="margin-left:8px">
                &#x1F5D1; Huỷ tất cả lệnh chờ ngay
            </button>
        </div>
        <div class="control-row" style="margin-top:8px;align-items:center;gap:8px;flex-wrap:wrap">
            <span style="font-size:12px;color:#8b949e">&#x1F504; Reversal Monitor:</span>
            ${(() => {
                const en  = d.reversal_monitor_enabled;
                const al  = d.reversal_alert_only;
                const mode = !en ? 'off' : (al ? 'alert' : 'auto');
                return `
                <button class="btn btn-sm ${mode==='auto'  ? 'btn-green' : ''}" onclick="toggleReversalMonitor('auto')"
                        style="${mode==='auto'  ? '' : 'background:#21262d;color:#8b949e'}">&#x2705; Tự đóng</button>
                <button class="btn btn-sm ${mode==='alert' ? 'btn-blue'  : ''}" onclick="toggleReversalMonitor('alert')"
                        style="${mode==='alert' ? '' : 'background:#21262d;color:#8b949e'}">&#x1F514; Chỉ alert</button>
                <button class="btn btn-sm ${mode==='off'   ? 'btn-red'   : ''}" onclick="toggleReversalMonitor('off')"
                        style="${mode==='off'   ? '' : 'background:#21262d;color:#8b949e'}">&#x23F8; Tắt</button>
                <span style="font-size:11px;color:${mode==='auto'?'#3fb950':mode==='alert'?'#58a6ff':'#f85149'}">
                    ${mode==='auto'?'Đang tự chốt lời khi đảo chiều':mode==='alert'?'Chỉ gửi alert':'Đã tắt'}
                </span>`;
            })()}
        </div>
        <div class="control-row" style="margin-top:8px;align-items:center;gap:8px;flex-wrap:wrap">
            <span style="font-size:12px;color:#8b949e">&#x1F6E1; Scan Protector:</span>
            ${(() => {
                const en = d.scan_protect_enabled !== false;
                return `
                <button class="btn btn-sm ${en ? 'btn-green' : ''}" onclick="toggleScanProtector(true)"
                        style="${en ? '' : 'background:#21262d;color:#8b949e'}">&#x2705; Bật</button>
                <button class="btn btn-sm ${!en ? 'btn-red' : ''}" onclick="toggleScanProtector(false)"
                        style="${!en ? '' : 'background:#21262d;color:#8b949e'}">&#x23F8; Tắt</button>
                <span style="font-size:11px;color:${en?'#3fb950':'#f85149'}">
                    ${en?'Đang chốt lời sớm khi lệnh scan đảo chiều':'Đã tắt'}
                </span>`;
            })()}
        </div>
        <div class="control-row" style="margin-top:8px;align-items:center;gap:8px;flex-wrap:wrap">
            <span style="font-size:12px;color:#8b949e">&#x1F512; Profit Lock:</span>
            ${(() => {
                const en = d.profit_lock_enabled !== false;
                return `
                <button class="btn btn-sm ${en ? 'btn-green' : ''}" onclick="toggleProfitLock(true)"
                        style="${en ? '' : 'background:#21262d;color:#8b949e'}">&#x2705; Bật</button>
                <button class="btn btn-sm ${!en ? 'btn-red' : ''}" onclick="toggleProfitLock(false)"
                        style="${!en ? '' : 'background:#21262d;color:#8b949e'}">&#x23F8; Tắt</button>
                <span style="font-size:11px;color:${en?'#3fb950':'#f85149'}">
                    ${en?'Đang tự chốt lời khi coin bay mạnh mà TP xa':'Đã tắt'}
                </span>`;
            })()}
        </div>
        <div class="control-row">
            <span>&#x1F4C8; Trailing Lock:</span>
            ${(() => {
                const en = d.trailing_lock_enabled !== false;
                return `
                <button class="btn btn-sm ${en ? 'btn-green' : ''}" onclick="toggleTrailingLock(true)"
                        style="${en ? '' : 'background:#21262d;color:#8b949e'}">&#x2705; Bật</button>
                <button class="btn btn-sm ${!en ? 'btn-red' : ''}" onclick="toggleTrailingLock(false)"
                        style="${!en ? '' : 'background:#21262d;color:#8b949e'}">&#x23F8; Tắt</button>
                <span style="font-size:11px;color:${en?'#3fb950':'#f85149'}">
                    ${en?'Dời SL lên lock lãi khi gần TP':'Đã tắt'}
                </span>`;
            })()}
        </div>
        <div class="control-row">
            <span>&#x1F6A8; Max Loss:</span>
            ${(() => {
                const en = d.max_loss_enabled !== false;
                const val = d.max_loss_value || 20;
                return `
                <button class="btn btn-sm ${en ? 'btn-green' : ''}" onclick="toggleMaxLoss(true)"
                        style="${en ? '' : 'background:#21262d;color:#8b949e'}">&#x2705; Bật</button>
                <button class="btn btn-sm ${!en ? 'btn-red' : ''}" onclick="toggleMaxLoss(false)"
                        style="${!en ? '' : 'background:#21262d;color:#8b949e'}">&#x23F8; Tắt</button>
                <input id="max-loss-input" type="number" value="${val}" min="5" max="100" step="5"
                       style="width:60px;background:#161b22;border:1px solid #30363d;border-radius:4px;padding:2px 6px;color:#e6edf3;font-size:12px;margin-left:6px">
                <button class="btn btn-sm" onclick="setMaxLoss()" style="margin-left:4px;font-size:11px">Set $</button>
                <span style="font-size:11px;color:${en?'#f85149':'#8b949e'}">
                    ${en?'Tự đóng khi lỗ > $'+val:'Đã tắt'}
                </span>`;
            })()}
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

    // ── PUMP NHẸ RADAR SECTION ───────────────────────────────
    html += `<div class="section" style="padding:0;border-color:#1a2a3d">
      <div id="pump-nhe-root" style="padding:16px">
        <div style="text-align:center;padding:24px;color:#484f58">
          <div style="font-size:24px;margin-bottom:6px">🔵</div>
          <div>Đang tải Pump Nhẹ Radar...</div>
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

    // Stats + PnL Chart gộp chung 1 section
    html += `<div class="stats">
        <div class="card"><div class="label">Balance</div><div id="stat-balance" class="value blue">${fmtUsd(d.balance)}</div></div>
        <div class="card"><div class="label">Today PnL</div><div id="stat-today-pnl" class="value ${pnlColor(d.today_pnl)}">${fmtUsd(d.today_pnl)}</div></div>
        <div class="card"><div class="label">Total PnL</div><div id="stat-total-pnl" class="value ${pnlColor(d.total_pnl)}">${fmtUsd(d.total_pnl)}</div></div>
        <div class="card"><div class="label">Unrealized</div><div id="stat-unrealized" class="value ${pnlColor(d.unrealized)}">${fmtUsd(d.unrealized)}</div></div>
        <div class="card"><div class="label">Win Rate</div><div id="stat-winrate" class="value">${fmt(d.win_rate,0)}%</div></div>
        <div class="card"><div class="label">Trades</div><div id="stat-trades" class="value">${d.total_trades}</div></div>
    </div>
    <div id="pnl-stats-section" style="margin-top:8px"><div style="color:#8b949e;font-size:13px">Đang tải PnL...</div></div>`;

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

    // Trade History
    if (d.trades_history && d.trades_history.length > 0) {
        html += `<div class="section">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
                <h2 style="margin:0">&#x1F4CB; Recent Trades</h2>
                <button onclick="clearTradeHistory()" style="background:#da3633;border:none;color:#fff;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:12px">🗑 Clear Data</button>
            </div>
            <table>
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

// ── PNL STATISTICS ───────────────────────────────────────────
let _pnlTab = 'daily';  // daily | weekly | monthly
let _pnlData = null;

async function fetchPnlStats() {
    try {
        const r = await fetch('/api/pnl_stats');
        _pnlData = await r.json();
        renderPnlStats();
    } catch(e) {
        const el = document.getElementById('pnl-stats-section');
        if (el) el.innerHTML = `<div style="color:#8b949e;font-size:13px">Không tải được dữ liệu</div>`;
    }
}

function renderPnlStats() {
    const el = document.getElementById('pnl-stats-section');
    if (!el) return;
    if (!_pnlData) { el.innerHTML = `<div style="color:#8b949e;font-size:13px">Đang tải...</div>`; return; }

    const rows = _pnlData[_pnlTab] || [];
    // Lọc ngày/tuần/tháng có trade
    const activeRows = rows.filter(r => r.trades > 0);

    const totalPnl    = rows.reduce((s,r) => s + r.pnl, 0);
    const totalTrades = rows.reduce((s,r) => s + r.trades, 0);
    const totalWins   = rows.reduce((s,r) => s + r.wins, 0);
    const wr = totalTrades > 0 ? (totalWins / totalTrades * 100) : 0;
    const pnlColor = v => v >= 0 ? '#3fb950' : '#f85149';

    let html = `
    <div class="pnl-stats-tabs">
        <div class="pnl-tab ${_pnlTab==='daily'?'active':''}" onclick="setPnlTab('daily')">Theo Ngày</div>
        <div class="pnl-tab ${_pnlTab==='weekly'?'active':''}" onclick="setPnlTab('weekly')">Theo Tuần</div>
        <div class="pnl-tab ${_pnlTab==='monthly'?'active':''}" onclick="setPnlTab('monthly')">Theo Tháng</div>
        <div class="pnl-tab ${_pnlTab==='by_coin'?'active':''}" onclick="setPnlTab('by_coin')">Theo Coin</div>
    </div>
    <div class="pnl-summary-row">
        <div class="pnl-summary-card">
            <div class="lbl">Tổng PnL</div>
            <div class="val" style="color:${pnlColor(totalPnl)}">${totalPnl>=0?'+':''}$${totalPnl.toFixed(2)}</div>
        </div>
        <div class="pnl-summary-card">
            <div class="lbl">Lệnh</div>
            <div class="val" style="color:#c9d1d9">${totalTrades}</div>
        </div>
        <div class="pnl-summary-card">
            <div class="lbl">Win Rate</div>
            <div class="val" style="color:${wr>=50?'#3fb950':'#f85149'}">${wr.toFixed(0)}%</div>
        </div>
        <div class="pnl-summary-card">
            <div class="lbl">Win / Loss</div>
            <div class="val" style="color:#c9d1d9"><span style="color:#3fb950">${totalWins}W</span> / <span style="color:#f85149">${totalTrades-totalWins}L</span></div>
        </div>
    </div>`;

    // Bar chart
    if (activeRows.length === 0) {
        html += `<div style="color:#8b949e;font-size:13px;padding:12px 0">📭 Chưa có lệnh nào được đóng</div>`;
    } else {
        const maxAbs = Math.max(...activeRows.map(r => Math.abs(r.pnl)), 0.01);
        html += `<div>`;
        activeRows.forEach(r => {
            const pct = Math.min(Math.abs(r.pnl) / maxAbs * 100, 100);
            const color = r.pnl >= 0 ? '#238636' : '#da3633';
            const textColor = r.pnl >= 0 ? '#3fb950' : '#f85149';
            const sign = r.pnl >= 0 ? '+' : '';
            const wrTxt = r.trades > 0 ? `${(r.wins/r.trades*100).toFixed(0)}% · ${r.trades}L` : '–';
            html += `
            <div class="pnl-bar-row">
                <div class="pnl-bar-label">${r.label}</div>
                <div class="pnl-bar-wrap">
                    <div class="pnl-bar-fill" style="width:${pct}%;background:${color}"></div>
                    <div class="pnl-bar-val" style="color:${textColor}">${sign}$${r.pnl.toFixed(2)}</div>
                </div>
                <div class="pnl-bar-meta">${wrTxt}</div>
            </div>`;
        });
        html += `</div>`;
    }

    el.innerHTML = html;
}

function setPnlTab(tab) {
    _pnlTab = tab;
    renderPnlStats();
}

async function clearTradeHistory() {
    if (!confirm('Xoá toàn bộ lịch sử lệnh? Không thể hoàn tác.')) return;
    try {
        const r = await fetch('/api/clear_trade_history', {method:'POST'});
        const d = await r.json();
        if (d.ok) {
            _pnlData = null;
            alert('✅ Đã xoá lịch sử lệnh');
            fetchPnlStats();
        }
    } catch(e) { alert('Lỗi: ' + e); }
}

// ── PUMP NHẸ RADAR ──────────────────────────────────────────
let _pumpNheData = null;

async function togglePumpNheAutoShort(enabled) {
    const r = await apiPost('/api/pump-nhe/toggle_auto', {enabled: enabled});
    if (r && r.msg) toast(r.msg, r.ok);
    fetchPumpNhe();
}

async function savePumpNheConfig() {
    const score = parseInt(document.getElementById('pnhe-score-input')?.value || 60);
    const rise  = parseFloat(document.getElementById('pnhe-rise-input')?.value || 10);
    const r = await apiPost('/api/pump-nhe/config', {min_score: score, min_rise: rise});
    const msgEl = document.getElementById('pnhe-config-msg');
    if (msgEl) {
        msgEl.textContent = r.ok ? '✅ Đã lưu' : ('❌ ' + r.msg);
        msgEl.style.color = r.ok ? '#3fb950' : '#f85149';
        setTimeout(() => { if(msgEl) msgEl.textContent = ''; }, 3000);
    }
    if (r.ok) toast(r.msg, true);
    fetchPumpNhe();
}

async function fetchPumpNhe() {
    try {
        const r = await fetch('/api/pump-nhe/state');
        _pumpNheData = await r.json();
        renderPumpNhe(_pumpNheData);
    } catch(e) {}
}

async function addPumpNheCoin() {
    const inp = document.getElementById('pnhe-coin-input');
    let sym = (inp.value || '').trim().toUpperCase();
    if (!sym) return;
    if (!sym.endsWith('USDT')) sym += 'USDT';
    const r = await apiPost('/api/pump-nhe/add', {symbol: sym});
    if (r.ok) { inp.value = ''; fetchPumpNhe(); }
}

async function removePumpNheCoin(sym) {
    await apiPost('/api/pump-nhe/remove', {symbol: sym});
    fetchPumpNhe();
}

async function pumpNheManualLong(sym) {
    const coin = (_pumpNheData && _pumpNheData.coins || []).find(c => c.symbol === sym);
    const priceStr = coin && coin.price > 0 ? ` @ $${coin.price.toPrecision(5)}` : '';
    if (!confirm(`▲ LONG tay ${sym}${priceStr}?\nSL/TP tự động từ chart. Dùng MAX_ORDER_USDT + LEVERAGE từ config.`)) return;
    const r = await apiPost('/api/pump/coins/manual_long', {symbol: sym, usdt: 0, leverage: 0});
    if (r.ok) toast(r.msg, true);
}

async function pumpNheManualShort(sym) {
    const coin = (_pumpNheData && _pumpNheData.coins || []).find(c => c.symbol === sym);
    const priceStr = coin && coin.price > 0 ? ` @ $${coin.price.toPrecision(5)}` : '';
    if (!confirm(`▼ SHORT tay ${sym}${priceStr}?\nDùng MAX_ORDER_USDT + LEVERAGE từ config.`)) return;
    const r = await apiPost('/api/order', {symbol: sym, side: 'SHORT', usdt: 0, sl: 0, tp: 0, leverage: 0});
    if (r.ok) toast(r.msg, true);
}

function renderPumpNhe(d) {
    const el = document.getElementById('pump-nhe-root');
    if (!el || !d) return;

    const coins = d.coins || [];

    // level → màu / icon / text
    const meta = {
        strong: { col: '#f85149', bg: 'rgba(248,81,73,.1)',  icon: '🔴', txt: 'Pump mạnh'  },
        medium: { col: '#d29922', bg: 'rgba(210,153,34,.1)', icon: '🟡', txt: 'Pump vừa'   },
        soft:   { col: '#388bfd', bg: 'rgba(56,139,253,.1)', icon: '🔵', txt: 'Pump nhẹ'   },
        dump:   { col: '#a371f7', bg: 'rgba(163,113,247,.1)',icon: '🟣', txt: 'Đang dump'  },
        flat:   { col: '#484f58', bg: 'transparent',          icon: '⚫', txt: 'Đi ngang'   },
    };

    let html = `<div class="pnhe-wrap">`;

    // Header
    html += `
    <div class="pnhe-header">
      <div class="pnhe-title">
        <div class="pnhe-dot"></div>
        <span style="color:#388bfd;font-size:14px;font-weight:700;letter-spacing:2px">PUMP NHẸ RADAR</span>
        <span style="color:#1a3a5a;font-size:11px">${coins.length} coin · refresh 5s</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <label style="font-size:11px;display:flex;align-items:center;gap:5px;cursor:pointer">
          <input type="checkbox" id="pnhe-auto-short"
                 ${d.auto_short ? 'checked' : ''}
                 onchange="togglePumpNheAutoShort(this.checked)"
                 style="accent-color:#f85149">
          <span id="pnhe-auto-label" style="color:${d.auto_short ? '#f85149' : '#1a3a5a'}">
            ${d.auto_short ? '🔴 AUTO SHORT (nhẹ)' : '⏸ Alert only'}
          </span>
        </label>
        <span style="font-size:10px;color:#1a2a3d">score≥${d.min_score || 50} | rise≥${d.min_rise || 10}%</span>
        <input id="pnhe-coin-input" placeholder="BEATUSDT"
               style="width:110px;font-size:11px;background:#0a0d14;border-color:#1a2a3d;color:#388bfd"
               onkeydown="if(event.key==='Enter')addPumpNheCoin()">
        <button class="btn btn-sm" onclick="addPumpNheCoin()"
                style="background:#0d1a2a;color:#388bfd;border:1px solid #1a3a5a">+ Add</button>
      </div>
    </div>`;

    // Legend
    html += `
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;font-size:10px">
      <span style="color:#f85149">🔴 ≥20% pump mạnh</span>
      <span style="color:#d29922">🟡 10-20% pump vừa</span>
      <span style="color:#388bfd">🔵 3-10% pump nhẹ</span>
      <span style="color:#a371f7">🟣 dump</span>
      <span style="color:#484f58">⚫ đi ngang</span>
    </div>

    <!-- Config panel -->
    <div style="background:#0a0d14;border:1px solid #1a2a3d;border-radius:7px;padding:8px 12px;
                margin-bottom:12px;display:flex;flex-wrap:wrap;align-items:center;gap:10px;font-size:11px">
      <span style="color:#484f58">⚙️ Config:</span>
      <label style="color:#1a3a5a;display:flex;align-items:center;gap:5px">
        Score ≥
        <input id="pnhe-score-input" type="number" min="30" max="90" value="${d.min_score || 60}"
               style="width:48px;background:#0d1117;border:1px solid #1a2a3d;color:#388bfd;
                      border-radius:4px;padding:2px 5px;font-size:11px;text-align:center">
      </label>
      <label style="color:#1a3a5a;display:flex;align-items:center;gap:5px">
        Rise ≥
        <input id="pnhe-rise-input" type="number" min="3" max="50" step="0.5" value="${d.min_rise || 10}"
               style="width:48px;background:#0d1117;border:1px solid #1a2a3d;color:#388bfd;
                      border-radius:4px;padding:2px 5px;font-size:11px;text-align:center">%
      </label>
      <button onclick="savePumpNheConfig()"
              style="background:#0d1a2a;color:#388bfd;border:1px solid #1a3a5a;border-radius:4px;
                     padding:2px 10px;font-size:11px;cursor:pointer;font-weight:700">💾 Lưu</button>
      <span id="pnhe-config-msg" style="font-size:10px;color:#3fb950"></span>
    </div>`;

    if (coins.length === 0) {
        html += `
        <div class="pnhe-empty">
          🔵 Thêm coin để theo dõi pump nhẹ<br>
          <span style="font-size:10px;color:#0d2040">BEAT · XRP · SOL · BNB · DOGE...</span>
        </div>`;
    } else {
        html += `<div class="pnhe-coin-list">`;

        coins.forEach(c => {
            const m        = meta[c.level] || meta.flat;
            const name     = c.symbol.replace('USDT', '');
            const pStr     = c.price > 0 ? (c.price >= 1 ? '$' + c.price.toFixed(4) : '$' + c.price.toFixed(6)) : '—';
            const chgSign  = c.change_pct >= 0 ? '+' : '';
            const chgStr   = `${chgSign}${c.change_pct.toFixed(2)}%`;
            const lowStr   = c.pump_from_low > 0 ? `↑${c.pump_from_low.toFixed(1)}% từ đáy` : '';
            // Bar width: dựa trên % thay đổi, max 50% → full bar
            const barPct   = Math.min(Math.abs(c.change_pct) / 50 * 100, 100);
            const barCol   = c.level === 'dump' ? '#a371f7' : m.col;
            const volStr   = c.volume_24h > 0
                ? (c.volume_24h >= 1e9 ? `$${(c.volume_24h/1e9).toFixed(1)}B`
                 : c.volume_24h >= 1e6 ? `$${(c.volume_24h/1e6).toFixed(0)}M`
                 : `$${(c.volume_24h/1e3).toFixed(0)}K`)
                : '';

            html += `
            <div class="pnhe-card ${c.level}">
              <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">

                <!-- Left: coin info -->
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                  <span style="font-size:13px;font-weight:700;color:${m.col};min-width:52px">${name}</span>
                  <span style="font-size:11px;color:#1a4a6a">${pStr}</span>
                  <span style="font-size:12px;font-weight:700;color:${m.col}">${chgStr}</span>
                  ${lowStr ? `<span style="font-size:10px;color:#1a5a3a">${lowStr}</span>` : ''}
                  <span style="font-size:10px;color:${m.col};background:${m.bg};padding:1px 6px;border-radius:3px">${m.icon} ${m.txt}</span>
                  ${volStr ? `<span style="font-size:10px;color:#1a3a5a">Vol ${volStr}</span>` : ''}
                </div>

                <!-- Right: buttons -->
                <div style="display:flex;align-items:center;gap:5px">
                  ${c.level !== 'dump' && c.level !== 'flat' ? `
                  <button onclick="pumpNheManualLong('${c.symbol}')"
                          style="background:#0d2a0d;color:#3fb950;border:1px solid #1a5a1a;border-radius:4px;
                                 padding:2px 8px;font-size:10px;font-weight:700;cursor:pointer">▲ LONG</button>` : ''}
                  <button onclick="pumpNheManualShort('${c.symbol}')"
                          style="background:#2a0d0d;color:#f85149;border:1px solid #5a1a1a;border-radius:4px;
                                 padding:2px 8px;font-size:10px;font-weight:700;cursor:pointer">▼ SHORT</button>
                  <button onclick="removePumpNheCoin('${c.symbol}')"
                          style="background:none;border:none;color:#1a3a5a;cursor:pointer;font-size:15px;padding:0 2px">×</button>
                </div>
              </div>

              <!-- Progress bar: % pump từ đáy -->
              <div class="pnhe-bar-wrap">
                <div class="pnhe-bar-fill" style="width:${barPct}%;background:${barCol}"></div>
              </div>

              <!-- 24h high/low -->
              <div style="display:flex;gap:10px;margin-top:4px;font-size:10px;color:#1a3a5a;flex-wrap:wrap">
                ${c.high_24h > 0 ? `<span>H: $${c.high_24h >= 1 ? c.high_24h.toFixed(4) : c.high_24h.toFixed(6)}</span>` : ''}
                ${c.low_24h  > 0 ? `<span>L: $${c.low_24h  >= 1 ? c.low_24h.toFixed(4)  : c.low_24h.toFixed(6)}</span>`  : ''}
              </div>
            </div>`;
        });

        html += `</div>`;
    }

    html += `</div>`;
    el.innerHTML = html;
}

// Pump Nhẹ Radar auto-refresh mỗi 5s
setInterval(fetchPumpNhe, 5000);
fetchPumpNhe();

// ── PUMP RADAR ───────────────────────────────────────────────
let _pumpData = null;
let _pumpRendered = false;  // track nếu đã render full lần đầu

async function fetchPump() {
    try {
        const r = await fetch('/api/pump');
        _pumpData = await r.json();
        if (!_pumpRendered) {
            renderPumpRadar(_pumpData);
            _pumpRendered = true;
        } else {
            patchPumpRadar(_pumpData);  // chỉ update data, không rebuild SVG
        }
    } catch(e) {}
}

// Patch nhẹ — chỉ update giá + score + status từng coin card, không động SVG
function patchPumpRadar(d) {
    if (!d) return;
    const coins      = d.coins      || [];
    const minScore   = d.min_score  || 60;
    const status     = d.status     || {};
    const autoShort  = d.auto_short || false;
    const softShort  = d.soft_short || false;

    // Sort realtime: pump top → alert → score cao → pump_pct cao
    coins.sort((a, b) => {
        if (a.is_top !== b.is_top) return b.is_top - a.is_top;
        if (a.is_alert !== b.is_alert) return b.is_alert - a.is_alert;
        if (b.score !== a.score) return b.score - a.score;
        if ((b.change_24h||0) !== (a.change_24h||0)) return (b.change_24h||0) - (a.change_24h||0);
        return (b.pump_pct || 0) - (a.pump_pct || 0);
    });

    // Update scan counter + time
    const scanEl = document.getElementById('pump-scan-info');
    if (scanEl) scanEl.textContent = `Scan #${status.scan_count||0} · ${status.last_scan||'--:--'}`;

    // Update AUTO SHORT checkboxes
    const asCb = document.getElementById('pump-auto-short');
    if (asCb) asCb.checked = autoShort;
    const ssCb = document.getElementById('pump-soft-short');
    if (ssCb) ssCb.checked = softShort;
    const ssLbl = document.getElementById('pump-soft-label');
    if (ssLbl) {
        ssLbl.textContent = softShort ? '🟡 Nhẹ (bật)' : '🟡 Nhẹ (tắt)';
        ssLbl.style.color = softShort ? '#d29922' : '#2a5a3a';
    }

    // Update từng coin card
    let needFullRender = false;
    coins.forEach(c => {
        const card = document.getElementById('pump-card-' + c.symbol);
        if (!card) { needFullRender = true; return; }

        const pStr = c.price > 0 ? (c.price >= 1 ? '$'+c.price.toFixed(4) : '$'+c.price.toFixed(6)) : '—';
        const priceEl = document.getElementById('pump-price-' + c.symbol);
        if (priceEl && priceEl.textContent !== pStr) priceEl.textContent = pStr;

        // Update badge 24h
        const badgeEl = document.getElementById('pump-badge24h-' + c.symbol);
        if (badgeEl && c.change_24h !== undefined) {
            const chg = c.change_24h || 0;
            if (Math.abs(chg) >= 3) {
                badgeEl.textContent = (chg >= 0 ? '+' : '') + chg.toFixed(1) + '%';
                badgeEl.style.color = chg >= 0 ? '#3fb950' : '#f85149';
                badgeEl.style.background = chg >= 0 ? 'rgba(63,185,80,.12)' : 'rgba(248,81,73,.12)';
                badgeEl.style.display = 'inline-block';
            } else {
                badgeEl.style.display = 'none';
            }
        }

        const scoreEl = document.getElementById('pump-score-' + c.symbol);
        if (scoreEl) {
            const chg24p = c.change_24h || 0;
            const ds = c.score > 0 ? c.score : chg24p >= 30 ? 55 : chg24p >= 20 ? 40 : chg24p >= 10 ? 25 : chg24p >= 5 ? 12 : 0;
            const col = c.is_top ? '#f85149' : c.is_alert ? '#3fb950' : (ds >= minScore ? '#3fb950' : ds >= 40 ? '#d29922' : ds > 0 ? '#388bfd' : '#484f58');
            scoreEl.textContent = ds + '/100';
            scoreEl.style.color = col;
        }
        const barEl = document.getElementById('pump-bar-' + c.symbol);
        if (barEl) {
            const chg24p = c.change_24h || 0;
            const ds = c.score > 0 ? c.score : chg24p >= 30 ? 55 : chg24p >= 20 ? 40 : chg24p >= 10 ? 25 : chg24p >= 5 ? 12 : 0;
            const col = c.is_top ? '#f85149' : c.is_alert ? '#3fb950' : (ds >= minScore ? '#3fb950' : ds >= 40 ? '#d29922' : ds > 0 ? '#388bfd' : '#21262d');
            barEl.style.width = Math.min(ds, 100) + '%';
            barEl.style.background = col;
        }
        const statusEl = document.getElementById('pump-status-' + c.symbol);
        if (statusEl) {
            const chg24p = c.change_24h || 0;
            const isStale = c.is_stale || false;
            const isPumpingP = (c.pump_pct > 2 || chg24p >= 5) && !isStale && !c.is_alert && !c.is_top;
            const displayPct = c.pump_pct > 0 ? c.pump_pct : chg24p;
            if (c.is_top)                              statusEl.textContent = '🔴 ĐỈnh — SẮP SHORT';
            else if (c.is_alert && !isStale)           statusEl.textContent = '🚀 Đang pump!';
            else if (isPumpingP && !isStale)           statusEl.textContent = `🔵 Pump +${displayPct.toFixed(1)}%`;
            else if (isStale)                          statusEl.textContent = '⚫ Đã xả — theo dõi';
            else                                       statusEl.textContent = '⚫ Đang quét';
        }

        // Cập nhật pump alert banner bên dưới card nếu có
        const alertEl = document.getElementById('pump-alert-' + c.symbol);
        if (alertEl) {
            if (c.is_alert && c.alert_reason) {
                alertEl.style.display = 'block';
                alertEl.textContent   = '🚀 ' + c.alert_reason;
            } else {
                alertEl.style.display = 'none';
            }
        }
    });

    // Update blip positions trên SVG nếu score thay đổi
    coins.forEach((c, i) => {
        const blip = document.getElementById('pump-blip-' + c.symbol);
        if (!blip) return;
        const isAlert = c.score >= minScore;
        const isNear  = c.score >= 40 && !isAlert;
        const col = isAlert ? '#3fb950' : isNear ? '#d29922' : '#2d5a6a';
        blip.setAttribute('fill', col);
    });

    // Nếu có coin mới hoặc bị xóa → rebuild chỉ coin list (KHÔNG đụng SVG)
    if (needFullRender) {
        const listEl = document.getElementById('pump-coin-list');
        if (listEl) {
            // Re-render toàn bộ coin list (không đụng SVG cha)
            _pumpRendered = false;
            // Giữ nguyên SVG, chỉ cập nhật phần coin list
            const pumpRoot = document.getElementById('pump-radar-root');
            if (pumpRoot) {
                // Xóa nội dung cũ và render lại toàn bộ radar (SVG sẽ bị reset 1 lần, chấp nhận được khi có thay đổi coin)
                _pumpRendered = false;
            }
        }
    }
}

async function addPumpCoin() {
    const inp = document.getElementById('pump-coin-input');
    let sym = (inp.value || '').trim().toUpperCase();
    if (!sym) return;
    if (!sym.endsWith('USDT')) sym += 'USDT';
    const r = await apiPost('/api/pump/coins/add', {symbol: sym});
    if (r.ok) { inp.value = ''; _pumpRendered = false; fetchPump(); }
}

async function removePumpCoin(sym) {
    await apiPost('/api/pump/coins/remove', {symbol: sym});
    _pumpRendered = false;
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

async function pumpManualLong(sym) {
    const price = (_pumpData && _pumpData.coins)
        ? ((_pumpData.coins.find(c=>c.symbol===sym)||{}).price || 0)
        : 0;
    const priceStr = price > 0 ? ` @ $${price.toPrecision(5)}` : '';
    if (!confirm(`▲ LONG tay ${sym}${priceStr}?\n\nSL/TP tự động từ chart.\nDùng MAX_ORDER_USDT + LEVERAGE từ config.`)) return;
    const r = await apiPost('/api/pump/coins/manual_long', {
        symbol: sym, usdt: 0, leverage: 0
    });
    if (r.ok) fetchPump();
}

async function toggleAutoShort(enabled) {
    const r = await apiPost('/api/pump/toggle_auto', {enabled: enabled});
    if (r && r.msg) toast(r.msg, r.ok);
    // Nếu bật hard mode → tắt soft mode checkbox
    if (enabled) {
        const cb = document.getElementById('pump-soft-short');
        if (cb) cb.checked = false;
    }
}

async function toggleSoftShort(enabled) {
    const r = await apiPost('/api/pump/toggle_soft', {enabled: enabled});
    if (r && r.msg) toast(r.msg, r.ok);
    // Nếu bật soft mode → tắt hard mode checkbox
    if (enabled) {
        const cb = document.getElementById('pump-auto-short');
        if (cb) cb.checked = false;
    }
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
    const softShort = d.soft_short || false;
    const minScore  = d.min_score  || 60;
    const scanning  = status.scanning   || false;
    const scanCount = status.scan_count || 0;
    const lastScan  = status.last_scan  || '--:--';
    const alertCoins = coins.filter(c => c.score >= minScore);

    // Sort: pump top → alert → score cao → pump_pct cao → còn lại
    coins.sort((a, b) => {
        if (a.is_top !== b.is_top) return b.is_top - a.is_top;
        if (a.is_alert !== b.is_alert) return b.is_alert - a.is_alert;
        if (b.score !== a.score) return b.score - a.score;
        if ((b.change_24h||0) !== (a.change_24h||0)) return (b.change_24h||0) - (a.change_24h||0);
        return (b.pump_pct || 0) - (a.pump_pct || 0);
    });

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
          <circle id="pump-blip-${c.symbol}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${sz}" fill="${col}" opacity="0.9">${anim}</circle>
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
          <span id="pump-scan-info" style="color:#1a4a2a;font-size:11px">Scan #${scanCount} · ${lastScan}</span>
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
          <label style="font-size:11px;display:flex;align-items:center;gap:5px;cursor:pointer;margin-left:4px">
            <input type="checkbox" id="pump-soft-short" ${softShort?'checked':''}
                   onchange="toggleSoftShort(this.checked)" style="accent-color:#d29922">
            <span id="pump-soft-label" style="color:${softShort?'#d29922':'#2a5a3a'}">${softShort?'🟡 Nhẹ (bật)':'🟡 Nhẹ (tắt)'}</span>
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
            const isTop   = c.is_top;
            const isAlert = c.is_alert && !isTop;
            const isStale = c.is_stale || false;
            const isNear  = c.score >= 40 && !isTop && !isAlert && !isStale;

            // Màu theo trạng thái:
            // isTop   → đỏ (đỉnh pump, cần SHORT)
            // isAlert → xanh lá (đang pump, có thể LONG)
            // isNear  → vàng (gần ngưỡng) — CHỈ khi không stale
            const pStr = c.price > 0 ? (c.price >= 1 ? '$'+c.price.toFixed(4) : '$'+c.price.toFixed(6)) : '—';
            const chg24 = c.change_24h || 0;
            const isPumping = (c.pump_pct > 2 || chg24 >= 5) && !isStale && !isAlert && !isTop;
            // Score hiển thị: dùng score thật nếu có, fallback tính từ % 24h
            const displayScore = c.score > 0 ? c.score
                               : chg24 >= 30 ? 55
                               : chg24 >= 20 ? 40
                               : chg24 >= 10 ? 25
                               : chg24 >= 5  ? 12 : 0;
            const displayPumpPct = c.pump_pct > 0 ? c.pump_pct : (chg24 > 0 ? chg24 : 0);
            const col = isTop      ? '#f85149'
                      : isAlert    ? '#3fb950'
                      : isPumping && c.pump_pct > 5 ? '#d29922'
                      : isPumping  ? '#388bfd'
                      : isNear     ? '#d29922'
                      : c.score > 0 ? '#388bfd'
                      :              '#484f58';
            const bg  = isTop      ? 'rgba(248,81,73,.08)'
                      : isAlert    ? 'rgba(63,185,80,.08)'
                      : isPumping && c.pump_pct > 5 ? 'rgba(210,153,34,.07)'
                      : isPumping  ? 'rgba(56,139,253,.06)'
                      : isNear     ? 'rgba(210,153,34,.05)'
                      :              'transparent';
            const bdr = isTop      ? '1px solid rgba(248,81,73,.4)'
                      : isAlert    ? '1px solid rgba(63,185,80,.4)'
                      : isPumping && c.pump_pct > 5 ? '1px solid rgba(210,153,34,.4)'
                      : isPumping  ? '1px solid rgba(56,139,253,.3)'
                      : isNear     ? '1px solid rgba(210,153,34,.3)'
                      :              '1px solid #0d2020';
            const shadow = isTop     ? 'box-shadow:0 0 10px rgba(248,81,73,.2)'
                         : isAlert   ? 'box-shadow:0 0 10px rgba(63,185,80,.15)'
                         : isPumping ? 'box-shadow:0 0 8px rgba(56,139,253,.2)'
                         :             '';
            const statusTxt = isTop      ? '🔴 Đỉnh — Vào SHORT!'
                            : isAlert    ? '🚀 Đang pump!'
                            : isPumping  ? '🔵 Pump +' + displayPumpPct.toFixed(1) + '%'
                            : isNear     ? '🟡 Đang gần'
                            : isStale    ? '⚫ Đã xả — theo dõi'
                            :              '⚫ Đang quét';
            const ageSec = c.ts ? Math.round((Date.now()/1000) - c.ts) : null;
            const ageStr = ageSec !== null && ageSec < 3600 ? (ageSec<60?`${ageSec}s`:`${Math.floor(ageSec/60)}m`) : '';
            return `
            <div id="pump-card-${c.symbol}"
                 style="background:${bg};border:${bdr};border-radius:8px;padding:10px 12px;${shadow}">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div style="display:flex;align-items:center;gap:8px">
                  <span style="font-size:12px;font-weight:700;color:${col}">${name}</span>
                  <span id="pump-price-${c.symbol}" style="font-size:11px;color:#1a5a3a">${pStr}</span>
                  ${(c.change_24h && Math.abs(c.change_24h) >= 3) ? `<span id="pump-badge24h-${c.symbol}" style="font-size:10px;font-weight:700;color:${c.change_24h>=0?'#3fb950':'#f85149'};background:${c.change_24h>=0?'rgba(63,185,80,.12)':'rgba(248,81,73,.12)'};padding:1px 5px;border-radius:3px">${c.change_24h>=0?'+':''}${c.change_24h.toFixed(1)}%</span>` : `<span id="pump-badge24h-${c.symbol}" style="display:none"></span>`}
                  <span id="pump-status-${c.symbol}" style="font-size:10px;color:${col}">${statusTxt}</span>
                </div>
                <div style="display:flex;align-items:center;gap:5px">
                  ${ageStr ? `<span style="font-size:10px;color:#0d3a2a">${ageStr}</span>` : ''}
                  ${(isAlert && !isStale) ? `<button onclick="pumpManualLong('${c.symbol}')"
                          style="background:#0d2a0d;color:#3fb950;border:1px solid #1a5a1a;border-radius:4px;
                                 padding:2px 8px;font-size:10px;font-weight:700;cursor:pointer">▲ LONG</button>` : ''}
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
                  <span id="pump-score-${c.symbol}" style="color:${col};font-weight:700">${displayScore}/100</span>
                </div>
                <div style="background:#0a1a10;border-radius:3px;height:5px;overflow:hidden">
                  <div id="pump-bar-${c.symbol}" style="width:${Math.min(displayScore,100)}%;height:100%;background:${col};border-radius:3px;transition:width .6s;
                              ${(isTop||isAlert)?'box-shadow:0 0 5px '+col:''}"></div>
                </div>
              </div>
              <div style="display:flex;gap:8px;margin-top:5px;font-size:10px;flex-wrap:wrap">
                ${displayPumpPct > 0 ? `<span style="color:${isAlert?'#3fb950':isPumping?'#388bfd':'#d29922'}">↑${displayPumpPct.toFixed(1)}%</span>` : ''}
                ${c.rsi > 0 ? `<span style="color:${c.rsi>70?'#f85149':c.rsi>60?'#d29922':'#1a6a4a'}">RSI ${c.rsi.toFixed(0)}</span>` : ''}
                ${c.vol_ratio > 0 ? `<span style="color:#1a5a7a">Vol ${c.vol_ratio.toFixed(1)}×</span>` : ''}
                ${isTop && c.entry > 0 ? `
                  <span style="color:#f85149;font-weight:600">Entry $${c.entry.toPrecision(4)}</span>
                  <span style="color:#f85149">SL $${c.sl.toPrecision(4)}</span>
                  <span style="color:#3fb950">TP $${c.tp1.toPrecision(4)}</span>` : ''}
              </div>
              ${isAlert && c.alert_reason ? `
              <div id="pump-alert-${c.symbol}"
                   style="margin-top:6px;padding:4px 8px;background:rgba(63,185,80,.1);
                          border:1px solid rgba(63,185,80,.3);border-radius:4px;
                          font-size:10px;color:#3fb950;line-height:1.4">
                🚀 ${c.alert_reason}
              </div>` : `<div id="pump-alert-${c.symbol}" style="display:none"></div>`}
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

    // ── INLINE SCAN STATUS dưới pump radar ──────────────────
    const cands = window._dashData ? (window._dashData.candidates || []) : [];
    const etargets = window._dashData ? (window._dashData.entry_targets || {}) : {};
    if (cands.length > 0) {
        let scanHtml = '<div style="margin-top:16px;padding-top:12px;border-top:1px solid #0d2a1a">';
        scanHtml += '<div style="font-size:11px;color:#3fb950;font-weight:700;margin-bottom:8px">📊 SCAN STATUS</div>';
        scanHtml += '<div style="display:flex;flex-direction:column;gap:6px">';
        for (let i = 0; i < Math.min(cands.length, 8); i++) {
            const c = cands[i];
            const isLong = c.signal === 'LONG';
            const sigCol = isLong ? '#3fb950' : '#f85149';
            const sigTxt = isLong ? '▲ LONG' : '▼ SHORT';
            const pNow = c.price ? (c.price >= 1 ? '$' + c.price.toFixed(3) : '$' + c.price.toFixed(5)) : '—';
            const et = etargets[c.symbol] || {};
            const entryFinal = isLong ? (et.long_entry || 0) : (et.short_entry || 0);
            const pEntry = entryFinal > 0 ? (entryFinal >= 1 ? '$' + entryFinal.toFixed(3) : '$' + entryFinal.toFixed(5)) : '—';
            const scoreNum = Math.round(c.score || 0);
            const rsiVal = c.rsi ? c.rsi.toFixed(0) : '—';
            const rsiCol = c.rsi > 65 ? '#f85149' : (c.rsi < 35 ? '#3fb950' : '#d29922');
            const coinName = c.symbol.replace('USDT', '');
            const reasonText = (c.reason || '').split('|')[0].trim().slice(0, 60);

            scanHtml += '<div style="background:#0a1a10;border:1px solid #1a3a1a;border-radius:6px;padding:8px 10px">';
            scanHtml += '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">';
            scanHtml += '<span style="color:#e6edf3;font-weight:700;min-width:52px">' + coinName + '</span>';
            scanHtml += '<span style="color:' + sigCol + ';font-weight:700;min-width:58px">' + sigTxt + '</span>';
            scanHtml += '<div style="flex:1;min-width:80px"><div style="display:flex;align-items:center;gap:6px">';
            scanHtml += '<div style="flex:1;background:#0d2a1a;border-radius:3px;height:6px;min-width:60px">';
            scanHtml += '<div style="width:' + scoreNum + '%;height:100%;background:' + sigCol + ';border-radius:3px"></div>';
            scanHtml += '</div><span style="color:#d29922;font-size:11px;white-space:nowrap">' + scoreNum + '%</span>';
            scanHtml += '</div></div>';
            scanHtml += '<span style="color:#8b949e;font-size:11px">' + pNow + '</span>';
            scanHtml += '<span style="color:' + sigCol + ';font-weight:600;font-size:11px">' + pEntry + '</span>';
            scanHtml += '<span style="color:' + rsiCol + ';font-size:11px">RSI ' + rsiVal + '</span>';
            scanHtml += '</div>';
            if (reasonText) {
                scanHtml += '<div style="margin-top:4px;font-size:10px;color:#1a5a3a">' + reasonText + '</div>';
            }
            scanHtml += '</div>';
        }
        scanHtml += '</div></div>';
        html += scanHtml;
    }

    el.innerHTML = html;
}

function scrollToCoin(sym) {
    const el = document.getElementById('pump-card-'+sym);
    if (el) el.scrollIntoView({behavior:'smooth', block:'nearest'});
}

// Pump radar auto-refresh riêng — nhanh hơn main (2s)
setInterval(fetchPump, 2000);
fetchPump();

// PnL stats refresh mỗi 30s (không cần nhanh)
setInterval(fetchPnlStats, 30000);
fetchPnlStats();

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
            _pumpRendered = false;  // pump-radar-root vừa được tạo lại → cần render lại
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

    // Bot status dot
    const running = d.running;
    document.getElementById('bot-status').innerHTML = running
        ? '<span class="dot dot-green"></span> Running'
        : '<span class="dot dot-red"></span> Paused';

    // Nút Pause/Start Bot
    const toggleBtn = document.getElementById('toggle-bot-btn');
    if (toggleBtn) {
        toggleBtn.className = 'btn ' + (running ? 'btn-red' : 'btn-green');
        toggleBtn.innerHTML = running ? '&#x23F8; Pause Bot' : '&#x25B6; Start Bot';
    }

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


@app.before_request
def check_auth():
    """Bảo vệ toàn bộ dashboard — chỉ cho qua /login và /logout."""
    if request.path in ("/login", "/logout"):
        return None  # public
    if not session.get("authenticated"):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "msg": "Unauthorized"}), 401
        return redirect("/login")


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

        # Fallback: dùng liq_api_cache (REST API — có data ngay)
        if not has_real_data:
            liq_api = _state.get("liq_api_cache") if _state else None
            if liq_api and liq_api.is_ready(sym):
                try:
                    heatmap = liq_api.get_heatmap(sym) or {}
                    if heatmap:
                        above = [(pr, usd) for pr, usd in heatmap.items() if pr > p and usd >= 10_000]
                        below = [(pr, usd) for pr, usd in heatmap.items() if pr < p and usd >= 10_000]
                        if above:
                            short_trigger = max(above, key=lambda x: x[1])[0]
                        if below:
                            long_trigger = max(below, key=lambda x: x[1])[0]
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
        "reversal_monitor_enabled": getattr(_config, "REVERSAL_MONITOR_ENABLED", True),
        "reversal_alert_only":      getattr(_config, "REVERSAL_ALERT_ONLY", False),
        "scan_protect_enabled":     getattr(_config, "SCAN_PROTECT_ENABLED", True),
        "profit_lock_enabled":      getattr(_config, "PROFIT_LOCK_ENABLED", True),
        "trailing_lock_enabled":    getattr(_config, "TRAILING_LOCK_ENABLED", True),
        "max_loss_enabled":         getattr(_config, "MAX_LOSS_ENABLED", True),
        "max_loss_value":           getattr(_config, "MAX_LOSS_PER_POSITION", 20.0),
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
    """Ghi danh sách coins vào watchlist.json để persist khi restart (không bị git pull ghi đè)."""
    import os, json
    wl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
    try:
        with open(wl_path, "w", encoding="utf-8") as f:
            json.dump(coins, f)
        logger.info(f"Watchlist saved to watchlist.json: {coins}")
    except Exception as e:
        logger.error(f"Failed to save watchlist.json: {e}")


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


@app.route("/api/quick_trade", methods=["POST"])
def api_quick_trade():
    """Quick SHORT/LONG — market order ngay, dùng config USDT + leverage."""
    data   = request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()
    side   = data.get("side", "").upper().strip()
    if not symbol or side not in ("LONG", "SHORT"):
        return jsonify({"ok": False, "msg": "Thiếu symbol hoặc side"})
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    if _exchange is None:
        return jsonify({"ok": False, "msg": "Exchange not connected"})
    try:
        usdt = float(getattr(_config, "MAX_ORDER_USDT", 15))
        leverage = int(getattr(_config, "LEVERAGE", 15))
        price = _exchange.get_ticker_price(symbol)
        if not price or float(price) <= 0:
            return jsonify({"ok": False, "msg": f"Không lấy được giá {symbol}"})

        # Set leverage (tự giảm nếu coin không hỗ trợ)
        actual_lev = _exchange.set_leverage(symbol, leverage)
        if actual_lev and actual_lev < leverage:
            leverage = actual_lev

        # Tính qty giữ position size = usdt × config.LEVERAGE gốc
        from qty_utils import calc_qty_precise
        target_lev = int(getattr(_config, "LEVERAGE", 15))
        target_usdt = usdt * target_lev / leverage  # tăng margin nếu lev giảm
        qty, _ = calc_qty_precise(_exchange, symbol, target_usdt, leverage, price)
        if qty * price < 5.0:
            return jsonify({"ok": False, "msg": f"Qty quá nhỏ"})

        order_side = "BUY" if side == "LONG" else "SELL"
        _exchange.place_market_order(symbol, order_side, qty)

        # Auto SL/TP
        import time as _t; _t.sleep(0.5)
        sl = tp = 0
        try:
            from auto_sltp import suggest_sltp
            s = suggest_sltp(_exchange, symbol, side, price, liq_tracker=None)
            sl, tp = s["sl"], s["tp"]
            close_side = "SELL" if side == "LONG" else "BUY"
            try: _exchange.place_stop_loss_order(symbol, close_side, qty, sl)
            except: pass
            try: _exchange.place_take_profit_order(symbol, close_side, qty, tp)
            except: pass
        except: pass

        with _lock:
            _state["trade_log"].append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol, "side": side,
                "entry": price, "sl": sl, "tp": tp,
                "qty": qty, "status": "OPEN", "note": "quick_trade",
            })

        msg = f"{'🔴' if side=='SHORT' else '🟢'} {side} {symbol} @ ${price:.6g} qty={qty} lev={leverage}x"
        logger.info(f"[QuickTrade] {msg}")
        if _notifier:
            try: _notifier.telegram.send(f"⚡ <b>QUICK {side}</b>\n🪙 {symbol} @ ${price:,.6g}\n📦 qty={qty} {leverage}x")
            except: pass
        return jsonify({"ok": True, "msg": msg})
    except Exception as e:
        logger.error(f"[QuickTrade] {symbol} {side}: {e}")
        return jsonify({"ok": False, "msg": str(e)[:200]})


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
    try:
        return _api_pump_state_inner()
    except Exception as e:
        logger.error(f"[api/pump] Error: {e}", exc_info=True)
        return jsonify({"ok": True, "status": {}, "coins": [], "history": [],
                        "auto_short": False, "soft_short": False, "min_score": 60,
                        "pump_alerts": {}, "error": str(e)})

def _api_pump_state_inner():
    with _lock:
        watch   = list(_state.get("pump_watch_coins", []))
        signals = list(_state.get("pump_signals", []))
        status  = dict(_state.get("pump_scan_status", {}))
        prices  = dict(_state.get("prices", {}))
        pump_alerts = dict(_state.get("pump_alerts", {}))  # {symbol: {...}}

    # Lấy % thay đổi 24h cho tất cả pump coins (1 API call, cache 60s)
    _now = time.time()
    cache = getattr(_api_pump_state_inner, "_ticker_cache", {})
    cache_ts = getattr(_api_pump_state_inner, "_ticker_ts", 0)
    if _now - cache_ts > 60:
        try:
            import requests as _req
            base = getattr(_config, "LIVE_BASE_URL", "https://fapi.binance.com")
            resp = _req.get(f"{base}/fapi/v1/ticker/24hr", timeout=5)
            if resp.ok:
                for t in resp.json():
                    s = t.get("symbol", "")
                    if s in watch:
                        cache[s] = {
                            "change_pct": float(t.get("priceChangePercent", 0)),
                            "low":        float(t.get("lowPrice", 0)),
                            "high":       float(t.get("highPrice", 0)),
                        }
                _api_pump_state_inner._ticker_cache = cache
                _api_pump_state_inner._ticker_ts    = _now
        except Exception:
            pass
    _api_pump_state_inner._ticker_cache = cache
    _api_pump_state_inner._ticker_ts    = cache_ts if _now - cache_ts <= 60 else _now

    # Build coin rows với pump score nếu có
    rows = []
    for sym in watch:
        # Lấy giá từ state prices (WebSocket realtime)
        # WS đã subscribe cả pump_watch_coins trong price_ws_streamer
        # nên prices dict luôn có giá mới nhất cho pump coins
        price = prices.get(sym, 0)
        # Tìm signal gần nhất cho coin này
        sig_d = next((s for s in reversed(signals) if s.get("symbol") == sym), None)
        # Ưu tiên pump_alerts (pump đang lên) nếu chưa có confirmed top
        alert_d = pump_alerts.get(sym)

        # ── Reset score nếu giá đã giảm xa khỏi đỉnh ──────────────
        effective_score = 0
        effective_pump_pct = 0
        is_stale = False
        if sig_d:
            entry_p   = sig_d.get("entry_price", 0)
            sig_score = sig_d.get("score", 0)
            sig_pump  = sig_d.get("pump_pct", 0)
            sig_ts    = sig_d.get("timestamp", 0)
            age_min   = (time.time() - sig_ts) / 60 if sig_ts else 999

            price_dropped  = entry_p > 0 and price > 0 and price < entry_p * 0.95
            timed_out      = age_min > 30 and not sig_d.get("is_pump_top", False)
            is_stale       = price_dropped or timed_out

            if is_stale:
                effective_score    = 0
                effective_pump_pct = 0
            else:
                effective_score    = sig_score
                effective_pump_pct = sig_pump

        # Xóa pump_alert nếu stale — tránh hiện "Đang pump!" khi giá đã giảm
        if is_stale:
            pump_alerts.pop(sym, None)
        # Cũng check alert_d: nếu giá đã giảm > 5% từ giá alert → stale alert
        if alert_d and price > 0:
            alert_price = alert_d.get("price", 0)
            alert_ts    = alert_d.get("ts", 0)
            alert_age   = (time.time() - alert_ts) / 60 if alert_ts else 999
            if (alert_price > 0 and price < alert_price * 0.95) or alert_age > 15:
                alert_d = None  # bỏ qua alert cũ này

        rows.append({
            "symbol":      sym,
            "price":       price,
            "pump_pct":    effective_pump_pct if sig_d else (alert_d["pump_pct"] if alert_d else 0),
            "score":       effective_score if sig_d else (alert_d["score"] if alert_d else 0),
            "change_24h":  cache.get(sym, {}).get("change_pct", 0),
            "change_raw":  cache.get(sym, {}).get("change_pct", 0),
            "is_top":      sig_d["is_pump_top"] if sig_d and not is_stale else False,
            # is_alert = True khi có signal pump bất kỳ (dù chưa là top)
            "is_alert":    (not sig_d["is_pump_top"] and effective_pump_pct > 2) if sig_d and not is_stale else bool(alert_d and not is_stale),
            "is_stale":    is_stale,            "rsi":         sig_d["rsi"]            if sig_d else (alert_d["rsi"]         if alert_d else 0),
            "vol_ratio":   sig_d["volume_ratio"]   if sig_d else (alert_d.get("vol_ratio", 0) if alert_d else 0),
            "entry":       sig_d["entry_price"]    if sig_d else (alert_d["price"]       if alert_d else 0),
            "sl":          sig_d["sl_price"]       if sig_d else 0,
            "tp1":         sig_d["tp1_price"]      if sig_d else 0,
            "signals":     sig_d["signals"]        if sig_d and not is_stale else ([alert_d["reason"]] if alert_d else []),
            "ts":          sig_d["timestamp"]      if sig_d else (alert_d["ts"]          if alert_d else 0),
            "alert_reason": alert_d["reason"] if alert_d and not (sig_d and sig_d.get("is_pump_top")) else "",
        })

    return jsonify({
        "ok":         True,
        "status":     status,
        "coins":      rows,
        "history":    signals[-20:],
        "auto_short": getattr(_config, "PUMP_AUTO_SHORT", False),
        "soft_short": getattr(_config, "PUMP_AUTO_SHORT_SOFT", False),
        "min_score":  getattr(_config, "PUMP_TOP_MIN_SCORE", 60),
        "pump_alerts": pump_alerts,
    })


@app.route("/api/pump/coins/add", methods=["POST"])
def api_pump_add_coin():
    """Thêm coin vào danh sách pump watch (quét riêng, nhanh hơn)."""
    try:
        data   = request.get_json() or {}
        symbol = data.get("symbol", "").upper().strip()
        if not symbol:
            return jsonify({"ok": False, "msg": "Thiếu symbol"})
        if not symbol.endswith("USDT"):
            symbol += "USDT"

        # Validate coin tồn tại trên Binance Futures
        if _exchange:
            try:
                test_price = _exchange.get_ticker_price(symbol)
                if not test_price or float(test_price) <= 0:
                    return jsonify({"ok": False, "msg": f"❌ {symbol} không tồn tại trên Binance Futures"})
            except Exception:
                return jsonify({"ok": False, "msg": f"❌ {symbol} không có trên Futures — chỉ có Spot"})

        if _state is None or _lock is None:
            return jsonify({"ok": False, "msg": "Bot chưa khởi động"})

        with _lock:
            watch = _state.get("pump_watch_coins", [])
            if symbol in watch:
                return jsonify({"ok": False, "msg": f"⚠️ {symbol} đã có trong Pump Radar rồi"})
            watch.append(symbol)
            _state["pump_watch_coins"] = watch

        try:
            import config as _cfg
            if not hasattr(_cfg, "PUMP_WATCH_COINS"):
                _cfg.PUMP_WATCH_COINS = []
            if symbol not in _cfg.PUMP_WATCH_COINS:
                _cfg.PUMP_WATCH_COINS.append(symbol)
        except Exception:
            pass

        _save_pump_coins_to_config(watch)
        logger.info(f"[PumpRadar] Added pump coin: {symbol}")
        return jsonify({"ok": True, "msg": f"Đã thêm {symbol} vào Pump Radar ✅"})

    except Exception as e:
        logger.error(f"[PumpRadar] add_coin error: {e}")
        return jsonify({"ok": False, "msg": f"Lỗi: {str(e)[:100]}"})


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


@app.route("/api/pump/toggle_auto", methods=["POST"])
def api_pump_toggle_auto():
    """Bật/tắt PUMP_AUTO_SHORT."""
    data    = request.get_json() or {}
    enabled = bool(data.get("enabled", False))
    try:
        import config as _cfg
        _cfg.PUMP_AUTO_SHORT = enabled
        # Tắt soft mode khi bật hard mode
        if enabled:
            _cfg.PUMP_AUTO_SHORT_SOFT = False
    except Exception:
        pass
    msg = "🔴 AUTO SHORT (Mạnh) bật — score≥75, pump≥20%, RSI≥72" if enabled \
          else "⏸ AUTO SHORT tắt — chỉ gửi Telegram alert"
    logger.info(f"[PumpRadar] PUMP_AUTO_SHORT = {enabled}")
    return jsonify({"ok": True, "msg": msg, "enabled": enabled})


@app.route("/api/pump/coins/manual_long", methods=["POST"])
def api_pump_manual_long():
    """Vào lệnh LONG tay từ Pump Radar — dùng MAX_ORDER_USDT + LEVERAGE từ config."""
    data   = request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"ok": False, "msg": "Thiếu symbol"})
    if _exchange is None:
        return jsonify({"ok": False, "msg": "Exchange not connected"})

    try:
        usdt     = float(data.get("usdt", 0)) or float(getattr(_config, "MAX_ORDER_USDT", 15))
        leverage = int(data.get("leverage", 0)) or int(getattr(_config, "LEVERAGE", 10))

        price = _exchange.get_ticker_price(symbol)
        if not price or float(price) <= 0:
            return jsonify({"ok": False, "msg": f"Không lấy được giá {symbol}"})

        # Tính qty
        from qty_utils import calc_qty_precise
        qty, _info = calc_qty_precise(_exchange, symbol, usdt, leverage, price)

        # Smart entry (SL/TP tự động từ chart)
        from smart_entry import find_optimal_entry, place_smart_order
        entry_info = find_optimal_entry(_exchange, symbol, "LONG", _config)

        result = place_smart_order(
            _exchange, symbol, "LONG", qty, entry_info, _config,
            bot_state=_state, bot_lock=_lock
        )

        with _lock:
            from datetime import datetime as _dt
            _state["trade_log"].append({
                "time":   _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": symbol, "side": "LONG",
                "entry":  result["price"],
                "sl":     entry_info.get("sl", 0),
                "tp":     entry_info.get("tp", 0),
                "qty":    qty, "status": "OPEN",
                "note":   f"pump_manual_long_{result['type'].lower()}",
            })

        order_type = "LIMIT (chờ khớp)" if result["type"] == "LIMIT" else "MARKET"
        sl_str = f" SL=${entry_info['sl']:.4f}" if entry_info.get("sl") else ""
        tp_str = f" TP=${entry_info['tp']:.4f}" if entry_info.get("tp") else ""
        logger.info(f"[PumpLONG] {symbol} @ ${result['price']:.4f} [{order_type}] qty={qty}{sl_str}{tp_str}")
        return jsonify({
            "ok":  True,
            "msg": f"▲ LONG {symbol} @ ${result['price']:.4f} [{order_type}] qty={qty}{sl_str}{tp_str}"
        })

    except Exception as e:
        logger.error(f"[PumpLONG] {symbol} failed: {e}")
        return jsonify({"ok": False, "msg": str(e)[:200]})


@app.route("/api/pump/toggle_soft", methods=["POST"])
def api_pump_toggle_soft():
    """Bật/tắt PUMP_AUTO_SHORT_SOFT — ngưỡng nhẹ hơn cho coin thường."""
    data    = request.get_json() or {}
    enabled = bool(data.get("enabled", False))
    try:
        import config as _cfg
        _cfg.PUMP_AUTO_SHORT_SOFT = enabled
        # Tắt hard mode khi bật soft mode
        if enabled:
            _cfg.PUMP_AUTO_SHORT = False
    except Exception:
        pass
    msg = "🟡 AUTO SHORT (Nhẹ) bật — score≥60, pump≥15%, RSI≥65 — coin thường" if enabled \
          else "⏸ AUTO SHORT (Nhẹ) tắt"
    logger.info(f"[PumpRadar] PUMP_AUTO_SHORT_SOFT = {enabled}")
    return jsonify({"ok": True, "msg": msg, "enabled": enabled})


@app.route("/api/scan_protector", methods=["POST"])
def api_scan_protector():
    """Bật/tắt Scan Position Protector."""
    data    = request.get_json() or {}
    enabled = data.get("enabled", True)
    try:
        import config as _cfg
        _cfg.SCAN_PROTECT_ENABLED = bool(enabled)
    except Exception:
        pass
    status = "bật" if enabled else "tắt"
    return jsonify({
        "ok":  True,
        "msg": f"Scan Protector: {status}",
        "enabled": bool(enabled),
    })


@app.route("/api/profit_lock", methods=["POST"])
@require_auth
def api_profit_lock():
    """Bật/tắt Auto Profit Lock — tự chốt lời khi coin bay mạnh mà TP xa."""
    data    = request.get_json() or {}
    enabled = data.get("enabled", True)
    try:
        import config as _cfg
        _cfg.PROFIT_LOCK_ENABLED = bool(enabled)
    except Exception:
        pass
    status = "bật" if enabled else "tắt"
    return jsonify({
        "ok":  True,
        "msg": f"Profit Lock: {status}",
        "enabled": bool(enabled),
    })

@app.route("/api/trailing_lock", methods=["POST"])
@require_auth
def api_trailing_lock():
    """Bật/tắt Trailing Profit Lock — dời SL lên lock lãi khi gần TP."""
    data    = request.get_json() or {}
    enabled = data.get("enabled", True)
    try:
        import config as _cfg
        _cfg.TRAILING_LOCK_ENABLED = bool(enabled)
    except Exception:
        pass
    status = "bật" if enabled else "tắt"
    return jsonify({"ok": True, "msg": f"Trailing Lock: {status}", "enabled": bool(enabled)})

@app.route("/api/max_loss", methods=["POST"])
@require_auth
def api_max_loss():
    """Bật/tắt Max Loss Safety Net + config số tiền."""
    data    = request.get_json() or {}
    enabled = data.get("enabled", True)
    value   = data.get("value", None)
    try:
        import config as _cfg
        _cfg.MAX_LOSS_ENABLED = bool(enabled)
        if value is not None:
            _cfg.MAX_LOSS_PER_POSITION = float(value)
    except Exception:
        pass
    val = getattr(_config, "MAX_LOSS_PER_POSITION", 20.0)
    status = f"bật (${val:.0f})" if enabled else "tắt"
    return jsonify({"ok": True, "msg": f"Max Loss: {status}", "enabled": bool(enabled), "value": val})

@app.route("/api/reversal_monitor", methods=["POST"])
def api_reversal_monitor():
    """Bật/tắt Position Reversal Monitor."""
    data       = request.get_json() or {}
    enabled    = data.get("enabled")     # True/False/None
    alert_only = data.get("alert_only")  # True/False/None
    try:
        import config as _cfg
        if enabled is not None:
            _cfg.REVERSAL_MONITOR_ENABLED = bool(enabled)
        if alert_only is not None:
            _cfg.REVERSAL_ALERT_ONLY = bool(alert_only)
    except Exception:
        pass
    mode = "tắt" if not getattr(_config, "REVERSAL_MONITOR_ENABLED", True) else \
           ("chỉ alert" if getattr(_config, "REVERSAL_ALERT_ONLY", False) else "tự động đóng")
    return jsonify({
        "ok": True,
        "msg": f"Reversal Monitor: {mode}",
        "enabled":    getattr(_config, "REVERSAL_MONITOR_ENABLED", True),
        "alert_only": getattr(_config, "REVERSAL_ALERT_ONLY", False),
    })



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


# ============================================================
# PUMP NHẸ RADAR — API endpoints (hoàn toàn độc lập pump radar cũ)
# ============================================================

def _save_pump_nhe_coins(coins: list):
    """Ghi PUMP_NHE_COINS vào config.py để persist khi restart."""
    import os, re
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
        new_block = "PUMP_NHE_COINS = [\n"
        for c in coins:
            new_block += f'    "{c}",\n'
        new_block += "]"
        content = re.sub(
            r'PUMP_NHE_COINS\s*=\s*\[.*?\]',
            new_block,
            content,
            flags=re.DOTALL
        )
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[PumpNhe] Config saved: PUMP_NHE_COINS = {coins}")
    except Exception as e:
        logger.error(f"[PumpNhe] Save config failed: {e}")


@app.route("/api/pump-nhe/state", methods=["GET"])
def api_pump_nhe_state():
    """
    Trả về danh sách coin pump nhẹ + % thay đổi 24h + giá realtime.
    Fetch ticker 24h từ Binance mỗi 30s (cache).
    """
    if _state is None:
        return jsonify({"ok": False, "coins": []})

    with _lock:
        coins = list(_state.get("pump_nhe_coins", []))
        prices = dict(_state.get("prices", {}))

    now_ts = time.time()
    cache    = getattr(api_pump_nhe_state, "_cache", {})
    cache_ts = getattr(api_pump_nhe_state, "_cache_ts", 0)

    if now_ts - cache_ts > 30:
        try:
            import requests as _req
            base = getattr(_config, "LIVE_BASE_URL", "https://fapi.binance.com")
            resp = _req.get(f"{base}/fapi/v1/ticker/24hr", timeout=6)
            if resp.ok:
                for t in resp.json():
                    s = t.get("symbol", "")
                    cache[s] = {
                        "change_pct": float(t.get("priceChangePercent", 0)),
                        "high":       float(t.get("highPrice", 0)),
                        "low":        float(t.get("lowPrice", 0)),
                        "volume":     float(t.get("quoteVolume", 0)),
                    }
                api_pump_nhe_state._cache    = cache
                api_pump_nhe_state._cache_ts = now_ts
        except Exception as e:
            logger.debug(f"[PumpNhe] ticker fetch error: {e}")

    api_pump_nhe_state._cache    = cache
    api_pump_nhe_state._cache_ts = cache_ts if now_ts - cache_ts <= 30 else now_ts

    rows = []
    for sym in coins:
        price   = prices.get(sym, 0)
        td      = cache.get(sym, {})
        chg_pct = td.get("change_pct", 0)
        high24  = td.get("high", 0)
        low24   = td.get("low", 0)
        vol24   = td.get("volume", 0)

        # Tính pump từ đáy 24h → giá hiện tại
        pump_from_low = 0.0
        if low24 > 0 and price > 0:
            pump_from_low = (price - low24) / low24 * 100

        # Phân loại mức pump
        if chg_pct >= 20:
            level = "strong"   # 🔴 pump mạnh
        elif chg_pct >= 10:
            level = "medium"   # 🟡 pump vừa
        elif chg_pct >= 3:
            level = "soft"     # 🔵 pump nhẹ
        elif chg_pct <= -5:
            level = "dump"     # 🟣 đang dump
        else:
            level = "flat"     # ⚫ đi ngang

        rows.append({
            "symbol":         sym,
            "price":          price,
            "change_pct":     round(chg_pct, 2),
            "pump_from_low":  round(pump_from_low, 2),
            "high_24h":       high24,
            "low_24h":        low24,
            "volume_24h":     vol24,
            "level":          level,
        })

    # Sort: pump mạnh nhất lên đầu
    rows.sort(key=lambda r: r["change_pct"], reverse=True)

    # ── Noti Telegram khi TOÀN BỘ coin trong list đều full đỏ (≥20%) ──
    # "Full đỏ" = tất cả coin ≥20%, không phải từng coin riêng lẻ
    if rows and all(r["change_pct"] >= 20 for r in rows):
        _noti_cache = getattr(api_pump_nhe_state, "_noti_cache", {})
        _last_full_red = _noti_cache.get("__full_red__", 0)
        if now_ts - _last_full_red > 1800:  # cooldown 30 phút
            _noti_cache["__full_red__"] = now_ts
            api_pump_nhe_state._noti_cache = _noti_cache
            try:
                notifier = _state.get("_notifier") if _state else None
                if notifier:
                    top3 = rows[:3]
                    coins_str = "\n".join(
                        f"🔴 {r['symbol'].replace('USDT','')} +{r['change_pct']:.1f}%"
                        for r in top3
                    )
                    notifier.telegram.send(
                        f"🔴 <b>PUMP NHẸ RADAR — FULL ĐỎ</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Tất cả {len(rows)} coin đều ≥20%\n\n"
                        f"{coins_str}\n"
                        f"⚠️ Thị trường đang pump mạnh — cân nhắc SHORT đỉnh"
                    )
            except Exception as _ne:
                logger.debug(f"[PumpNhe] full-red noti error: {_ne}")
    else:
        if not hasattr(api_pump_nhe_state, "_noti_cache"):
            api_pump_nhe_state._noti_cache = {}

    return jsonify({
        "ok":        True,
        "coins":     rows,
        "auto_short": getattr(_config, "PUMP_NHE_AUTO_SHORT", False),
        "min_score":  getattr(_config, "PUMP_NHE_MIN_SCORE", 50),
        "min_rise":   getattr(_config, "PUMP_NHE_PRICE_RISE_PCT", 10.0),
    })


@app.route("/api/pump-nhe/add", methods=["POST"])
def api_pump_nhe_add():
    """Thêm coin vào PUMP NHẸ RADAR."""
    data   = request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"ok": False, "msg": "Thiếu symbol"})
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    # Validate coin tồn tại trên Binance Futures
    if _exchange:
        try:
            p = _exchange.get_ticker_price(symbol)
            if not p or float(p) <= 0:
                return jsonify({"ok": False, "msg": f"❌ {symbol} không tồn tại trên Futures"})
        except Exception:
            return jsonify({"ok": False, "msg": f"❌ {symbol} không có trên Futures"})

    with _lock:
        coins = _state.get("pump_nhe_coins", [])
        if symbol in coins:
            return jsonify({"ok": False, "msg": f"⚠️ {symbol} đã có trong Pump Nhẹ Radar"})
        coins.append(symbol)
        _state["pump_nhe_coins"] = coins

    try:
        import config as _cfg
        if not hasattr(_cfg, "PUMP_NHE_COINS"):
            _cfg.PUMP_NHE_COINS = []
        if symbol not in _cfg.PUMP_NHE_COINS:
            _cfg.PUMP_NHE_COINS.append(symbol)
    except Exception:
        pass

    # Sync vào state pump_nhe_coins để pump_scan_engine đọc ngay
    if _state is not None and _lock is not None:
        with _lock:
            _state["pump_nhe_coins"] = coins

    _save_pump_nhe_coins(coins)
    logger.info(f"[PumpNhe] Added: {symbol}")
    return jsonify({"ok": True, "msg": f"Đã thêm {symbol} ✅"})


@app.route("/api/pump-nhe/config", methods=["POST"])
def api_pump_nhe_config():
    """Cập nhật config Pump Nhẹ: min_score và min_rise từ web UI."""
    data      = request.get_json() or {}
    min_score = data.get("min_score")
    min_rise  = data.get("min_rise")
    try:
        import config as _cfg
        if min_score is not None:
            _cfg.PUMP_NHE_MIN_SCORE = int(min_score)
        if min_rise is not None:
            _cfg.PUMP_NHE_PRICE_RISE_PCT = float(min_rise)
        # Persist vào file config.py
        import os, re
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
        if min_score is not None:
            content = re.sub(r'PUMP_NHE_MIN_SCORE\s*=\s*\d+',
                             f'PUMP_NHE_MIN_SCORE = {int(min_score)}', content)
        if min_rise is not None:
            content = re.sub(r'PUMP_NHE_PRICE_RISE_PCT\s*=\s*[\d.]+',
                             f'PUMP_NHE_PRICE_RISE_PCT = {float(min_rise)}', content)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[PumpNhe] Config updated: score={min_score} rise={min_rise}")
        return jsonify({"ok": True,
                        "msg": f"✅ Đã lưu: score≥{int(min_score) if min_score else '—'} | rise≥{float(min_rise) if min_rise else '—'}%"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"❌ Lỗi: {e}"})


@app.route("/api/pump-nhe/toggle_auto", methods=["POST"])
def api_pump_nhe_toggle_auto():
    """Bật/tắt PUMP_NHE_AUTO_SHORT — độc lập PUMP_AUTO_SHORT."""
    data    = request.get_json() or {}
    enabled = bool(data.get("enabled", False))
    try:
        import config as _cfg
        _cfg.PUMP_NHE_AUTO_SHORT = enabled
    except Exception:
        pass
    msg = (f"🔴 PUMP NHẸ AUTO SHORT bật — score≥{getattr(_config,'PUMP_NHE_MIN_SCORE',50)}, "
           f"rise≥{getattr(_config,'PUMP_NHE_PRICE_RISE_PCT',10)}%") if enabled \
          else "⏸ PUMP NHẸ AUTO SHORT tắt — chỉ alert"
    logger.info(f"[PumpNhe] PUMP_NHE_AUTO_SHORT = {enabled}")
    return jsonify({"ok": True, "msg": msg, "enabled": enabled})


@app.route("/api/pump-nhe/remove", methods=["POST"])
def api_pump_nhe_remove():
    """Xóa coin khỏi PUMP NHẸ RADAR."""
    data   = request.get_json() or {}
    symbol = data.get("symbol", "").upper().strip()

    with _lock:
        coins = _state.get("pump_nhe_coins", [])
        if symbol not in coins:
            return jsonify({"ok": False, "msg": f"{symbol} không có trong danh sách"})
        coins.remove(symbol)
        _state["pump_nhe_coins"] = coins

    try:
        import config as _cfg
        if hasattr(_cfg, "PUMP_NHE_COINS") and symbol in _cfg.PUMP_NHE_COINS:
            _cfg.PUMP_NHE_COINS.remove(symbol)
    except Exception:
        pass

    # Sync vào state
    if _state is not None and _lock is not None:
        with _lock:
            _state["pump_nhe_coins"] = coins

    _save_pump_nhe_coins(coins)
    logger.info(f"[PumpNhe] Removed: {symbol}")
    return jsonify({"ok": True, "msg": f"Đã xóa {symbol}"})


def start_web_dashboard(state, lock, config, port=5555, exchange=None):
    """Start web dashboard in background thread."""
    global _state, _lock, _config, _exchange
    _state = state
    _lock = lock
    _config = config
    _exchange = exchange

    # Set secret key từ config — session hết hạn khi restart bot
    app.config["SECRET_KEY"] = getattr(config, "WEB_SECRET_KEY", "fallback-secret-key-change-me")
    # Session timeout 24h
    from datetime import timedelta
    app.permanent_session_lifetime = timedelta(hours=24)

    # Store watchlist in state for web access
    from scanner import WATCHLIST
    with lock:
        state["_watchlist"] = list(WATCHLIST)
        # Khởi tạo pump watch list nếu chưa có
        if "pump_watch_coins" not in state:
            state["pump_watch_coins"] = list(getattr(config, "PUMP_WATCH_COINS", []))
        if "pump_nhe_coins" not in state:
            state["pump_nhe_coins"] = list(getattr(config, "PUMP_NHE_COINS", []))
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


@app.route("/api/pnl_stats", methods=["GET"])
def api_pnl_stats():
    """
    Trả về thống kê PnL theo ngày / tuần / tháng.
    Mỗi entry: { label, pnl, trades, wins }
    """
    from datetime import datetime, timedelta, timezone
    import collections

    with _lock:
        tlog = list(_state.get("trade_log", []))

    # Chỉ lấy lệnh đã đóng có PnL thực
    closed = [t for t in tlog
              if t.get("status") == "CLOSED"
              and abs(t.get("pnl_usdt", 0)) > 0.001]

    def parse_time(t):
        try:
            return datetime.strptime(t["time"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    # ── DAILY: tất cả các ngày có trade ─────────────────────
    day_map = collections.defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
    for t in closed:
        dt = parse_time(t)
        if dt is None: continue
        dk = dt.strftime("%Y-%m-%d")
        day_map[dk]["pnl"]    += t.get("pnl_usdt", 0)
        day_map[dk]["trades"] += 1
        day_map[dk]["wins"]   += 1 if t.get("pnl_usdt", 0) > 0 else 0

    today_str = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    sorted_days = sorted(day_map.keys(), reverse=True)  # mới nhất lên đầu
    daily = []
    for dk in sorted_days:
        v = day_map[dk]
        if dk == today_str:
            label = "Hôm nay"
        elif dk == yesterday_str:
            label = "Hôm qua"
        else:
            # dd/mm/yy
            dt = datetime.strptime(dk, "%Y-%m-%d")
            label = dt.strftime("%d/%m/%y")
        daily.append({"label": label, "pnl": round(v["pnl"], 2),
                      "trades": v["trades"], "wins": v["wins"]})

    # ── WEEKLY: 8 tuần gần nhất ──────────────────────────────
    def week_key(dt):
        if dt is None: return None
        # ISO week: năm-tuần
        return dt.strftime("%G-W%V")

    week_map = collections.defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0, "start": None})
    for t in closed:
        dt = parse_time(t)
        if dt is None: continue
        wk = week_key(dt)
        week_map[wk]["pnl"]    += t.get("pnl_usdt", 0)
        week_map[wk]["trades"] += 1
        week_map[wk]["wins"]   += 1 if t.get("pnl_usdt", 0) > 0 else 0
        if week_map[wk]["start"] is None or dt < week_map[wk]["start"]:
            week_map[wk]["start"] = dt

    # Sort theo tuần, lấy 8 tuần gần nhất
    sorted_weeks = sorted(week_map.keys())[-8:]
    current_week = datetime.now().strftime("%G-W%V")
    weekly = []
    for wk in sorted_weeks:
        v = week_map[wk]
        label = "Tuần này" if wk == current_week else f"T{wk.split('W')[1]}/{wk.split('-')[0][2:]}"
        weekly.append({"label": label, "pnl": round(v["pnl"], 2), "trades": v["trades"], "wins": v["wins"]})

    # ── MONTHLY: 6 tháng gần nhất ────────────────────────────
    month_map = collections.defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
    for t in closed:
        dt = parse_time(t)
        if dt is None: continue
        mk = dt.strftime("%Y-%m")
        month_map[mk]["pnl"]    += t.get("pnl_usdt", 0)
        month_map[mk]["trades"] += 1
        month_map[mk]["wins"]   += 1 if t.get("pnl_usdt", 0) > 0 else 0

    sorted_months = sorted(month_map.keys())[-6:]
    current_month = datetime.now().strftime("%Y-%m")
    monthly = []
    for mk in sorted_months:
        v = month_map[mk]
        y, m = mk.split("-")
        label = "Tháng này" if mk == current_month else f"T{int(m)}/{y[2:]}"
        monthly.append({"label": label, "pnl": round(v["pnl"], 2), "trades": v["trades"], "wins": v["wins"]})

    # ── BY COIN: PnL từng coin ──────────────────────────────
    coin_map = collections.defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
    for t in closed:
        sym = t.get("symbol", "???")
        coin_map[sym]["pnl"]    += t.get("pnl_usdt", 0)
        coin_map[sym]["trades"] += 1
        coin_map[sym]["wins"]   += 1 if t.get("pnl_usdt", 0) > 0 else 0

    by_coin = []
    for sym, v in sorted(coin_map.items(), key=lambda x: x[1]["pnl"], reverse=True):
        by_coin.append({"label": sym.replace("USDT", ""), "pnl": round(v["pnl"], 2),
                        "trades": v["trades"], "wins": v["wins"]})

    return jsonify({"daily": daily, "weekly": weekly, "monthly": monthly, "by_coin": by_coin})


@app.route("/api/clear_trade_history", methods=["POST"])
def api_clear_trade_history():
    """Xoá toàn bộ trade log (closed trades). Open positions không bị ảnh hưởng."""
    try:
        with _lock:
            tlog = _state.get("trade_log", [])
            _state["trade_log"] = [t for t in tlog if t.get("status") != "CLOSED"]
        try:
            from trade_history import save_history
            with _lock:
                save_history(_state["trade_log"])
        except Exception:
            pass
        logger.info("[Dashboard] Trade history cleared by user")
        return jsonify({"ok": True, "msg": "Đã xoá lịch sử lệnh"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
