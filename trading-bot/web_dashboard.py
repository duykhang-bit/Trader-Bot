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
        # Auto-authenticate - không cần login
        session["authenticated"] = True
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

# Cache pending orders — fetch thường xuyên hơn để UI realtime
_pending_orders_cache = []
_pending_orders_last_fetch = 0
_PENDING_ORDERS_TTL = 30  # giây — tăng lên 30s để tránh rate limit

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
.pnl-stats-tabs { display: flex; align-items: center; gap: 6px; margin-bottom: 12px; }
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
@media (max-width: 768px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .prices-grid { grid-template-columns: repeat(2, 1fr); }
  .control-row { flex-wrap: wrap; gap: 4px; font-size: 11px; }
  .control-row span { min-width: 120px; }
  .btn { padding: 4px 8px; font-size: 11px; }
  .btn-sm { padding: 2px 6px; font-size: 10px; }
  .section { padding: 10px; margin-bottom: 8px; }
  table { font-size: 11px; }
  th, td { padding: 4px 6px; }
  #tv-chart-section { margin: 0 0 8px 0 !important; }
  .container { padding: 0 6px; }
  h2 { font-size: 13px; }
}
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
/* ── TradingAgents AI Analysis ── */
.ta-wrap { background: linear-gradient(135deg,#0d1117 0%,#0a1120 100%); border: 1px solid #1a3a5a; border-radius: 12px; padding: 16px; }
.ta-header { display:flex; align-items:center; gap:10px; margin-bottom:14px; }
.ta-dot { width:9px; height:9px; border-radius:50%; background:#58a6ff; box-shadow:0 0 8px #58a6ff; animation:pulseDot 1.4s ease-in-out infinite; }
.ta-form { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:6px; }
.ta-result { background:#0d1117; border:1px solid #21262d; border-radius:8px; padding:14px; margin-top:10px; font-size:13px; line-height:1.7; }
.ta-rating-buy  { color:#3fb950; font-size:20px; font-weight:800; }
.ta-rating-sell { color:#f85149; font-size:20px; font-weight:800; }
.ta-rating-hold { color:#d29922; font-size:20px; font-weight:800; }
.ta-field { margin-bottom:8px; }
.ta-field .lbl { color:#8b949e; font-size:11px; text-transform:uppercase; letter-spacing:1px; }
.ta-field .val { color:#e6edf3; font-size:13px; margin-top:2px; }
.ta-spinner { display:inline-block; width:14px; height:14px; border:2px solid #30363d; border-top:2px solid #58a6ff; border-radius:50%; animation:spin .8s linear infinite; vertical-align:middle; margin-right:5px; }
@keyframes spin { to{transform:rotate(360deg)} }
.ta-progress { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:8px 12px; font-size:12px; color:#8b949e; margin-top:8px; }
.ta-analyst-chip { padding:3px 10px; border-radius:20px; font-size:11px; font-weight:600; border:1px solid #30363d; background:#0d1117; color:#8b949e; cursor:pointer; transition:all .2s; user-select:none; display:inline-block; margin:3px 2px; }
.ta-analyst-chip.active { background:#1f3a5a; border-color:#58a6ff; color:#58a6ff; }
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
    <div id="tv-chart-section" class="section" style="padding:12px;margin:0 12px 12px"></div>
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
            signal: AbortSignal.timeout(15000)  // 15s timeout
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
async function setPumpReversalConfig() {
    const floor = parseFloat(document.getElementById('pump-rev-floor')?.value || 0.3);
    const r = await apiPost('/api/pump_reversal_config', {floor});
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
async function toggleMfeScan(enabled) {
    const r = await apiPost('/api/mfe_scan', {enabled});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    refresh();
}
let _chartInitialized = false;
function initTVChart(watchlist) {
    const el = document.getElementById('tv-chart-section');
    if (!el || el.dataset.loaded) return;
    el.dataset.loaded = '1';
    const chartSym = watchlist.length > 0 ? watchlist[0].replace('USDT','') + 'USDTPERP' : 'BTCUSDTPERP';
    const watchlistOpts = watchlist.map(s => `<option value="${s.replace('USDT','')+'USDTPERP'}" data-sym="${s}">${s.replace('USDT','')}</option>`).join('');
    el.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
        <span style="font-size:13px;color:#58a6ff;font-weight:600">📈 Chart</span>
        <select id="tv-symbol-select" onchange="updateTVChart()"
                style="background:#0d1117;border:1px solid #1a3a5a;color:#c9d1d9;font-size:12px;padding:3px 8px;border-radius:4px">
          ${watchlistOpts}
        </select>
        <select id="tv-interval-select" onchange="updateTVChart()"
                style="background:#0d1117;border:1px solid #1a3a5a;color:#c9d1d9;font-size:12px;padding:3px 8px;border-radius:4px">
          <option value="1">1m</option><option value="5">5m</option>
          <option value="15" selected>15m</option><option value="60">1h</option><option value="240">4h</option>
        </select>
        <span style="margin-left:auto;font-size:13px;color:#f85149;font-weight:600">⚡ Quick Trade</span>
        <select id="qs-symbol-select" onchange="document.getElementById('qs-symbol').value=this.value; document.getElementById('tv-symbol-select').value=this.value.replace('USDT','')+'USDTPERP'; updateTVChart();"
                style="background:#0d1117;border:1px solid #5a1a1a;color:#f85149;font-size:12px;padding:3px 8px;border-radius:4px">
          ${watchlist.map(s => `<option value="${s}">${s.replace('USDT','')}</option>`).join('')}
        </select>
        <input id="qs-symbol" placeholder="SYMBOL" value="${watchlist[0]||''}"
               style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:4px 8px;color:#e6edf3;font-size:12px;width:100px">
        <button onclick="quickShort()" style="background:#7a1a1a;color:#ff6b6b;border:1px solid #aa2a2a;border-radius:6px;padding:5px 12px;font-weight:700;font-size:12px;cursor:pointer">🔴 SHORT</button>
        <button onclick="quickLong()" style="background:#0d2a0d;color:#3fb950;border:1px solid #1a5a1a;border-radius:6px;padding:5px 12px;font-weight:700;font-size:12px;cursor:pointer">🟢 LONG</button>
      </div>
      <div style="height:500px;border-radius:6px;overflow:hidden">
        <iframe id="tv-chart-frame"
          src="https://www.tradingview.com/widgetembed/?frameElementId=tv-chart-frame&symbol=BINANCE%3A${chartSym}&interval=15&hidesidetoolbar=0&theme=dark&style=1&timezone=Asia%2FHo_Chi_Minh&withdateranges=1&locale=vi"
          style="width:100%;height:500px;border:none" allowtransparency="true" scrolling="no"></iframe>
      </div>`;
}
async function toggleEntryOffset(enabled) {
    const r = await apiPost('/api/entry_offset', {enabled});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    refresh();
}
async function setEntryOffset() {
    const pct = parseFloat(document.getElementById('entry-offset-pct')?.value || 0.3);
    if (isNaN(pct) || pct < 0.1 || pct > 5.0) { toast('Offset phải 0.1-5.0%', false); return; }
    const r = await apiPost('/api/entry_offset', {pct: pct / 100});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    refresh();
}
async function toggleProfitLock(enabled) {
    const r = await apiPost('/api/profit_lock', {enabled});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    refresh();
}
async function setProfitLock() {
    const minEl = document.getElementById('profit-lock-min');
    const highEl = document.getElementById('profit-lock-high');
    const speedEl = document.getElementById('profit-lock-speed');
    const minPct = minEl ? parseFloat(minEl.value) : 2.0;
    const highPct = highEl ? parseFloat(highEl.value) : 15.0;
    const speedPct = speedEl ? parseFloat(speedEl.value) : 1.5;
    if (isNaN(minPct) || minPct < 0.5 || minPct > 10) { toast('Min phải 0.5-10%', false); return; }
    if (isNaN(highPct) || highPct < 5 || highPct > 50) { toast('High phải 5-50%', false); return; }
    if (isNaN(speedPct) || speedPct < 0.5 || speedPct > 5) { toast('Speed phải 0.5-5%/s', false); return; }
    const r = await apiPost('/api/profit_lock', {min_pct: minPct, high_pct: highPct, speed_pct: speedPct});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    refresh();
}
function updateTVChart() {
    const sym = document.getElementById('tv-symbol-select')?.value || 'BTCUSDT.P';
    const interval = document.getElementById('tv-interval-select')?.value || '15';
    const frame = document.getElementById('tv-chart-frame');
    if (frame) {
        frame.src = `https://www.tradingview.com/widgetembed/?frameElementId=tv-chart-frame&symbol=BINANCE%3A${sym}&interval=${interval}&hidesidetoolbar=0&symboledit=1&saveimage=0&toolbarbg=f1f3f6&studies=[]&theme=dark&style=1&timezone=Asia%2FHo_Chi_Minh&withdateranges=1&showpopupbutton=1&locale=vi`;
    }
}
async function toggleBreakevenExit(enabled) {
    const r = await apiPost('/api/breakeven_exit', {enabled});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    refresh();
}
async function setBreakevenHold() {
    const pumpEl = document.getElementById('breakeven-pump-hold');
    const scanEl = document.getElementById('breakeven-scan-hold');
    const pump = pumpEl ? parseInt(pumpEl.value) : 180;
    const scan = scanEl ? parseInt(scanEl.value) : 300;
    if (isNaN(pump) || pump < 0 || pump > 3600 || isNaN(scan) || scan < 0 || scan > 3600) { toast('Delay phải 0-3600 giây', false); return; }
    const r1 = await apiPost('/api/breakeven_exit/hold', {pump_seconds: pump, scan_seconds: scan});
    if (r1 && r1.msg) toast(r1.msg, r1.ok !== false);
    refresh();
}
async function setBreakevenAdvanced() {
    const pumpPeak  = parseFloat(document.getElementById('be-pump-peak')?.value || 3.0);
    const pumpFloor = parseFloat(document.getElementById('be-pump-floor')?.value || 1.0);
    const scanPeak  = parseFloat(document.getElementById('be-scan-peak')?.value || 2.0);
    const scanFloor = parseFloat(document.getElementById('be-scan-floor')?.value || 0.7);
    const revN      = parseInt(document.getElementById('be-rev-confirm')?.value || 2);
    const r = await apiPost('/api/breakeven_exit/advanced', {
        pump_peak: pumpPeak, pump_floor: pumpFloor,
        scan_peak: scanPeak, scan_floor: scanFloor,
        reversal_confirm: revN
    });
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
    if (!val || val < 1) { toast('Min $1', false); return; }
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
    const maxPos = parseInt(document.getElementById('set-max-positions').value);
    if (!maxUsdt || maxUsdt <= 0 || !lev || lev < 1 || !maxPos || maxPos < 1) { toast('Invalid', false); return; }
    await apiPost('/api/settings', {max_order_usdt: maxUsdt, leverage: lev, max_open_positions: maxPos});
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

// ── TradingAgents AI Analysis ─────────────────────────────────────────────
const _taAnalysts = ['market', 'news', 'social', 'fundamentals'];
let _taActiveAnalysts = new Set(['market', 'news', 'social']);
let _taPolling = null;

// Model presets theo provider
const _taModelPresets = {
    'openrouter':  { deep: 'nvidia/nemotron-3-ultra-550b-a55b:free', quick: 'openai/gpt-oss-20b:free' },
    'groq':        { deep: 'llama-3.3-70b-versatile',                quick: 'llama-3.1-8b-instant' },
    'google':      { deep: 'gemini-2.0-flash',                       quick: 'gemini-2.0-flash' },
    'deepseek':    { deep: 'deepseek-v4-pro',                        quick: 'deepseek-v4-flash' },
    'openai':      { deep: 'gpt-4o',                                 quick: 'gpt-4o-mini' },
    'anthropic':   { deep: 'claude-opus-4-5',                        quick: 'claude-haiku-4-5' },
    'ollama':      { deep: 'llama3.2',                               quick: 'llama3.2' },
};

function taUpdateModels(provider) {
    const preset = _taModelPresets[provider] || { deep: '', quick: '' };
    document.getElementById('ta-deep-model').value  = preset.deep;
    document.getElementById('ta-quick-model').value = preset.quick;
}

// Per-slot model presets (quick model cho analyst/researcher, deep cho manager)
const _taSlotModels = {
    'deepseek':    { analyst: 'deepseek-v4-flash',      researcher: 'deepseek-v4-flash',      manager: 'deepseek-v4-pro' },
    'groq':        { analyst: 'openai/gpt-oss-20b',     researcher: 'openai/gpt-oss-20b',    manager: 'openai/gpt-oss-20b' },
    'google':      { analyst: 'gemini-3.6-flash',       researcher: 'gemini-3.6-flash',       manager: 'gemini-3.6-flash' },
    'openai':      { analyst: 'gpt-4o-mini',            researcher: 'gpt-4o-mini',            manager: 'gpt-4o' },
    'anthropic':   { analyst: 'claude-haiku-4-5',       researcher: 'claude-haiku-4-5',       manager: 'claude-sonnet-4-5' },
    'openrouter':  { analyst: 'openai/gpt-oss-20b:free',researcher: 'openai/gpt-oss-20b:free',manager: 'nvidia/nemotron-3-ultra-550b-a55b:free' },
};

function taUpdateSlotModel(slot, provider) {
    const presets = _taSlotModels[provider] || {};
    const modelEl = document.getElementById('ta-model-' + slot);
    if (modelEl && presets[slot]) modelEl.value = presets[slot];
}

function taToggleAnalyst(key) {
    if (_taActiveAnalysts.has(key)) {
        if (_taActiveAnalysts.size > 1) _taActiveAnalysts.delete(key);
        else { toast('Phải chọn ít nhất 1 analyst', false); return; }
    } else {
        _taActiveAnalysts.add(key);
    }
    document.querySelectorAll('.ta-analyst-chip').forEach(el => {
        el.classList.toggle('active', _taActiveAnalysts.has(el.dataset.key));
    });
}

function _taRatingClass(rating) {
    if (!rating) return '';
    const r = rating.toLowerCase();
    if (r.includes('buy') || r.includes('overweight')) return 'ta-rating-buy';
    if (r.includes('sell') || r.includes('underweight')) return 'ta-rating-sell';
    return 'ta-rating-hold';
}

function _taRatingIcon(rating) {
    if (!rating) return '⬜';
    const r = rating.toLowerCase();
    if (r.includes('buy')) return '🟢';
    if (r.includes('overweight')) return '🔼';
    if (r.includes('sell')) return '🔴';
    if (r.includes('underweight')) return '🔽';
    return '🟡';
}

async function taAnalyze() {
    const ticker = document.getElementById('ta-ticker').value.trim().toUpperCase() || 'BTC-USD';
    const dateEl = document.getElementById('ta-date');
    const date   = dateEl && dateEl.value ? dateEl.value : new Date().toISOString().slice(0,10);
    const analysts  = [..._taActiveAnalysts];

    // Multi-provider slots
    const analystProv  = document.getElementById('ta-prov-analyst')?.value    || 'deepseek';
    const analystModel = document.getElementById('ta-model-analyst')?.value   || 'deepseek-v4-flash';
    const resProv      = document.getElementById('ta-prov-researcher')?.value || 'groq';
    const resModel     = document.getElementById('ta-model-researcher')?.value|| 'llama-3.3-70b-versatile';
    const mgrProv      = document.getElementById('ta-prov-manager')?.value    || 'google';
    const mgrModel     = document.getElementById('ta-model-manager')?.value   || 'gemini-2.0-flash';

    const resultEl = document.getElementById('ta-result');
    resultEl.innerHTML = `<div class="ta-progress"><span class="ta-spinner"></span>Đang phân tích <b>${ticker}</b> ngày <b>${date}</b>...<br><span style="font-size:10px;color:#484f58">Analyst: ${analystProv}/${analystModel} · Researcher: ${resProv}/${resModel} · Manager: ${mgrProv}/${mgrModel}</span></div>`;

    const r = await apiPost('/api/ta/analyze', {
        ticker, date, analysts,
        multi_provider: {
            analyst:    { provider: analystProv, model: analystModel },
            researcher: { provider: resProv,     model: resModel },
            manager:    { provider: mgrProv,     model: mgrModel },
        },
    });
    if (!r || !r.ok) {
        resultEl.innerHTML = `<div style="color:#f85149">❌ ${r?.msg || 'Lỗi không xác định'}</div>`;
        return;
    }

    // bắt đầu poll status
    if (_taPolling) clearInterval(_taPolling);
    _taPolling = setInterval(async () => {
        try {
            const s = await fetch('/api/ta/status');
            const sd = await s.json();
            if (!sd.running) {
                clearInterval(_taPolling);
                _taPolling = null;
                _taShowResult(sd.last_result);
            } else {
                const elapsed = sd.elapsed_sec || 0;
                const mins = Math.floor(elapsed / 60);
                const secs = elapsed % 60;
                const timeStr = mins > 0 ? `${mins}m${secs}s` : `${secs}s`;
                const log = (sd.agent_log || []).slice(-4).join('<br>');
                resultEl.innerHTML = `
                    <div class="ta-progress">
                        <span class="ta-spinner"></span>
                        <span style="color:#58a6ff;font-weight:700">${sd.step || 'Đang chạy...'}</span>
                        <span style="color:#484f58;font-size:10px;margin-left:8px">⏱ ${timeStr}</span>
                    </div>
                    ${log ? `<div style="font-size:10px;color:#484f58;margin-top:6px;font-family:monospace;line-height:1.6">${log}</div>` : ''}`;
            }
        } catch(e) {}
    }, 3000);
}

function _taShowResult(res) {
    const el = document.getElementById('ta-result');
    if (!el) return;
    if (!res) { el.innerHTML = `<div style="color:#8b949e">Chưa có kết quả</div>`; return; }

    if (res.error) {
        el.innerHTML = `<div style="color:#f85149">❌ ${res.error}</div>`; return;
    }

    const ratingClass = _taRatingClass(res.rating);
    const ratingIcon  = _taRatingIcon(res.rating);

    let html = `<div class="ta-field">
        <div class="lbl">Phán quyết</div>
        <div class="${ratingClass}">${ratingIcon} ${res.rating || '—'}</div>
    </div>`;

    if (res.entry_price) html += `<div class="ta-field">
        <div class="lbl">Entry Price</div>
        <div class="val" style="color:#58a6ff;font-size:15px;font-weight:700">$${Number(res.entry_price).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}</div>
    </div>`;

    if (res.stop_loss) html += `<div class="ta-field">
        <div class="lbl">Stop Loss</div>
        <div class="val" style="color:#f85149">$${Number(res.stop_loss).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}</div>
    </div>`;

    if (res.price_target) html += `<div class="ta-field">
        <div class="lbl">Price Target</div>
        <div class="val" style="color:#3fb950">$${Number(res.price_target).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:4})}</div>
    </div>`;

    if (res.position_sizing) html += `<div class="ta-field">
        <div class="lbl">Position Sizing</div>
        <div class="val">${res.position_sizing}</div>
    </div>`;

    if (res.executive_summary) html += `<div class="ta-field">
        <div class="lbl">Tóm tắt</div>
        <div class="val" style="color:#c9d1d9">${res.executive_summary}</div>
    </div>`;

    if (res.investment_thesis) html += `<div class="ta-field" style="margin-top:8px;padding-top:8px;border-top:1px solid #21262d">
        <div class="lbl">Luận điểm</div>
        <div class="val" style="color:#8b949e;font-size:12px">${res.investment_thesis}</div>
    </div>`;

    if (res.time_horizon) html += `<div class="ta-field">
        <div class="lbl">Time Horizon</div>
        <div class="val">${res.time_horizon}</div>
    </div>`;

    if (res.ticker && res.date) html += `<div style="margin-top:10px;font-size:10px;color:#484f58">
        Phân tích: ${res.ticker} · ${res.date} · Analysts: ${(res.analysts||[]).join(', ')}
    </div>`;

    el.innerHTML = `<div class="ta-result">${html}</div>`;
}

async function taCheckLastResult() {
    try {
        const s = await fetch('/api/ta/status');
        const sd = await s.json();
        if (sd.running) {
            document.getElementById('ta-result').innerHTML =
                `<div class="ta-progress"><span class="ta-spinner"></span>${sd.step || 'Đang phân tích...'}</div>`;
            if (!_taPolling) {
                _taPolling = setInterval(async () => {
                    try {
                        const s2 = await fetch('/api/ta/status');
                        const sd2 = await s2.json();
                        if (!sd2.running) { clearInterval(_taPolling); _taPolling = null; _taShowResult(sd2.last_result); }
                        else {
                            const p = document.getElementById('ta-result');
                            if (p) p.innerHTML = `<div class="ta-progress"><span class="ta-spinner"></span>${sd2.step || '...'}</div>`;
                        }
                    } catch(e) {}
                }, 3000);
            }
        } else if (sd.last_result) {
            _taShowResult(sd.last_result);
        }
    } catch(e) {}
}

function renderDashboard(d) {
    // Bot status
    const running = d.running;
    document.getElementById('bot-status').innerHTML = running
        ? '<span class="dot dot-green"></span> Running'
        : '<span class="dot dot-red"></span> Paused';

    let html = '';

    // ── Quick SHORT/LONG — bấm 1 nút vào ngay ──
    // Control Panel
    html += `<div class="section"><h2>&#x2699; Controls</h2>
        <div class="control-row">
            <button id="toggle-bot-btn" class="btn ${running ? 'btn-red' : 'btn-green'}" onclick="toggleBot()">
                ${running ? '&#x23F8; Pause Bot' : '&#x25B6; Start Bot'}
            </button>
            <button class="btn btn-blue" style="display:none">&#x1F9E0; Run AI Analysis</button>
            <span id="scan-info" style="color:#8b949e;font-size:12px">Scan #${d.scan_no} | Last: ${d.last_scan}</span>
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
                </span>
                <span style="font-size:10px;color:#484f58;margin-left:8px">Floor≤</span>
                <input id="pump-rev-floor" type="number" min="0" max="5" step="0.1" value="${d.pump_reversal_floor_pct??0.3}"
                       style="width:40px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#d29922;text-align:center">
                <span style="font-size:10px;color:#484f58">%</span>
                <button class="btn btn-sm" onclick="setPumpReversalConfig()" style="font-size:10px;padding:2px 6px;background:#0d2a1a;color:#3fb950;border:1px solid #1a4a2a">Set</button>`;
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
                <input id="max-loss-input" type="number" value="${val}" min="1" max="100" step="1"
                       style="width:60px;background:#161b22;border:1px solid #30363d;border-radius:4px;padding:2px 6px;color:#e6edf3;font-size:12px;margin-left:6px">
                <button class="btn btn-sm" onclick="setMaxLoss()" style="margin-left:4px;font-size:11px">Set $</button>
                <span style="font-size:11px;color:${en?'#f85149':'#8b949e'}">
                    ${en?'Tự đóng khi lỗ > $'+val:'Đã tắt'}
                </span>`;
            })()}
        </div>
        <div class="control-row">
            <span>&#x1F4C8; Peak Profit Trailing:</span>
            ${(() => {
                const en = d.breakeven_exit_enabled !== false;
                const pumpSecs = d.breakeven_pump_hold_seconds ?? 180;
                const scanSecs = d.breakeven_scan_hold_seconds ?? 300;
                const pumpPeak = d.breakeven_pump_peak_pct ?? 3.0;
                const scanPeak = d.breakeven_scan_peak_pct ?? 2.0;
                const pumpFloor = d.breakeven_pump_pnl_floor ?? 1.0;
                const scanFloor = d.breakeven_scan_pnl_floor ?? 0.7;
                const revConfirm = d.breakeven_reversal_confirm ?? 2;
                return `
                <button class="btn btn-sm ${en ? 'btn-green' : ''}" onclick="toggleBreakevenExit(true)"
                        style="${en ? '' : 'background:#21262d;color:#8b949e'}">&#x2705; Bật</button>
                <button class="btn btn-sm ${!en ? 'btn-red' : ''}" onclick="toggleBreakevenExit(false)"
                        style="${!en ? '' : 'background:#21262d;color:#8b949e'}">&#x23F8; Tắt</button>
                <span style="font-size:11px;color:#484f58;margin-left:6px">Pump</span>
                <input id="breakeven-pump-hold" type="number" min="0" max="3600" value="${pumpSecs}"
                       style="width:40px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#f85149;text-align:center"
                       title="Giây chờ sau khi vào lệnh pump">
                <span style="font-size:11px;color:#484f58">s Scan</span>
                <input id="breakeven-scan-hold" type="number" min="0" max="3600" value="${scanSecs}"
                       style="width:40px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#58a6ff;text-align:center"
                       title="Giây chờ sau khi vào lệnh scan/armed">
                <span style="font-size:11px;color:#484f58">s</span>
                <button class="btn btn-sm" onclick="setBreakevenHold()" style="font-size:10px;padding:2px 6px;background:#0d1a2d;color:#58a6ff;border:1px solid #1a3a5a">Set</button>
                <br style="margin:4px 0">
                <span style="font-size:10px;color:#484f58">Peak Pump≥</span>
                <input id="be-pump-peak" type="number" min="0.5" max="20" step="0.5" value="${pumpPeak}"
                       style="width:36px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#f85149;text-align:center">
                <span style="font-size:10px;color:#484f58">% Floor≤</span>
                <input id="be-pump-floor" type="number" min="0" max="10" step="0.1" value="${pumpFloor}"
                       style="width:36px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#f85149;text-align:center">
                <span style="font-size:10px;color:#484f58">% | Peak Scan≥</span>
                <input id="be-scan-peak" type="number" min="0.5" max="20" step="0.5" value="${scanPeak}"
                       style="width:36px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#58a6ff;text-align:center">
                <span style="font-size:10px;color:#484f58">% Floor≤</span>
                <input id="be-scan-floor" type="number" min="0" max="10" step="0.1" value="${scanFloor}"
                       style="width:36px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#58a6ff;text-align:center">
                <span style="font-size:10px;color:#484f58">% Rev×</span>
                <input id="be-rev-confirm" type="number" min="1" max="5" value="${revConfirm}"
                       style="width:30px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#d29922;text-align:center">
                <button class="btn btn-sm" onclick="setBreakevenAdvanced()" style="font-size:10px;padding:2px 6px;background:#1a1400;color:#d29922;border:1px solid #3a2a00">Set</button>
                <span style="font-size:11px;color:${en?'#3fb950':'#8b949e'}">
                    ${en?'Peak Profit Trailing':'Đã tắt'}
                </span>`;
            })()}
        </div>
        <div class="control-row">
            <span>&#x1F4CA; MFE Scan Exit:</span>
            ${(() => {
                const en2 = d.mfe_scan_enabled !== false;
                const pct = Math.round((d.mfe_retrace_pct || 0.40) * 100);
                return `
                <button class="btn btn-sm ${en2 ? 'btn-green' : ''}" onclick="toggleMfeScan(true)"
                        style="${en2 ? '' : 'background:#21262d;color:#8b949e'}">&#x2705; Bật</button>
                <button class="btn btn-sm ${!en2 ? 'btn-red' : ''}" onclick="toggleMfeScan(false)"
                        style="${!en2 ? '' : 'background:#21262d;color:#8b949e'}">&#x23F8; Tắt</button>
                <span style="font-size:11px;color:${en2?'#3fb950':'#8b949e'}">
                    ${en2?'Chốt lời scan/quick/app khi hồi '+pct+'% từ đỉnh':'Đã tắt'}
                </span>`;
            })()}
        </div>
        <div class="control-row">
            <span>&#x1F3AF; Entry Offset:</span>
            ${(() => {
                const eo = d.entry_offset_enabled === true;
                const pct = ((d.entry_offset_pct || 0.003) * 100).toFixed(1);
                return `
                <button class="btn btn-sm ${eo ? 'btn-green' : ''}" onclick="toggleEntryOffset(true)"
                        style="${eo ? '' : 'background:#21262d;color:#8b949e'}">&#x2705; Bật</button>
                <button class="btn btn-sm ${!eo ? 'btn-red' : ''}" onclick="toggleEntryOffset(false)"
                        style="${!eo ? '' : 'background:#21262d;color:#8b949e'}">&#x23F8; Tắt</button>
                <input id="entry-offset-pct" type="number" min="0.1" max="5.0" step="0.1" value="${pct}"
                       style="width:44px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#d29922;text-align:center">
                <span style="font-size:11px;color:#484f58">%</span>
                <button class="btn btn-sm" onclick="setEntryOffset()" style="font-size:10px;padding:2px 6px;background:#1a1400;color:#d29922;border:1px solid #3a2a00">Set</button>
                <span style="font-size:11px;color:${eo?'#d29922':'#8b949e'}">
                    ${eo?'LONG −'+pct+'% | SHORT +'+pct+'%':'Đã tắt — vào đúng giá liq'}
                </span>`;
            })()}
        </div>
        <div class="control-row">
            <span>&#x1F4B0; Profit Lock:</span>
            ${(() => {
                const en = d.profit_lock_enabled !== false;
                const minPct = (d.profit_lock_min_pct || 2.0).toFixed(1);
                const highPct = (d.profit_lock_high_pct || 15.0).toFixed(1);
                const speedPct = (d.profit_lock_speed_pct || 1.5).toFixed(1);
                return `
                <button class="btn btn-sm ${en ? 'btn-green' : ''}" onclick="toggleProfitLock(true)"
                        style="${en ? '' : 'background:#21262d;color:#8b949e'}">&#x2705; Bật</button>
                <button class="btn btn-sm ${!en ? 'btn-red' : ''}" onclick="toggleProfitLock(false)"
                        style="${!en ? '' : 'background:#21262d;color:#8b949e'}">&#x23F8; Tắt</button>
                <span style="font-size:11px;color:#484f58;margin-left:6px">Min</span>
                <input id="profit-lock-min" type="number" min="0.5" max="10" step="0.5" value="${minPct}"
                       style="width:40px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#58a6ff;text-align:center"
                       title="Lời tối thiểu (%) để bắt đầu theo dõi dump/pump">
                <span style="font-size:11px;color:#484f58">% High</span>
                <input id="profit-lock-high" type="number" min="5" max="50" step="1" value="${highPct}"
                       style="width:40px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#f85149;text-align:center"
                       title="Lời cao (%) → chốt ngay không cần check tốc độ">
                <span style="font-size:11px;color:#484f58">% Speed</span>
                <input id="profit-lock-speed" type="number" min="0.5" max="5" step="0.1" value="${speedPct}"
                       style="width:40px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#d29922;text-align:center"
                       title="Tốc độ giá (%) thay đổi trong 1s → coi là dump/pump mạnh">
                <span style="font-size:11px;color:#484f58">%/s</span>
                <button class="btn btn-sm" onclick="setProfitLock()" style="font-size:10px;padding:2px 6px;background:#1a1400;color:#58a6ff;border:1px solid #1a3a5a">Set</button>
                <span style="font-size:11px;color:${en?'#58a6ff':'#8b949e'}">
                    ${en?'Min:'+minPct+'% High:'+highPct+'% Speed:'+speedPct+'%/s':'Đã tắt'}
                </span>`;
            })()}
        </div>
    </div>`;

    // ── TRADINGVIEW CHART ────────────────────────────────────
    // Chart render vào div cố định bên ngoài, không reload theo dashboard
    if (!_chartInitialized) {
        _chartInitialized = true;
        setTimeout(() => initTVChart(d.watchlist || []), 100);
    }

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

    // ── TRADINGAGENTS AI ANALYSIS SECTION ──────────────────────
    html += `<div class="section" style="padding:0;border-color:#1a3a5a">
      <div class="ta-wrap" id="ta-root">
        <div class="ta-header">
          <div class="ta-dot"></div>
          <span style="color:#58a6ff;font-size:14px;font-weight:700;letter-spacing:2px">&#x1F9E0; TRADINGAGENTS AI ANALYSIS</span>
          <span style="color:#1a3a5a;font-size:11px">Multi-agent · Multi-provider</span>
        </div>

        <!-- Row 1: ticker / date / analyze -->
        <div class="ta-form">
          <input id="ta-ticker" placeholder="BTC-USD / ETH-USD / NVDA" value="BTC-USD"
                 style="width:160px;background:#0d1117;border:1px solid #1a3a5a;color:#58a6ff;font-weight:700;letter-spacing:1px">
          <input id="ta-date" type="date" value="${new Date().toISOString().slice(0,10)}"
                 style="background:#0d1117;border:1px solid #1a3a5a;color:#c9d1d9">
          <button onclick="taAnalyze()"
                  style="background:linear-gradient(135deg,#1f6feb,#388bfd);color:#fff;border:none;border-radius:6px;
                         padding:8px 18px;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:1px">
            &#x1F50D; Phân tích
          </button>
        </div>

        <!-- Multi-provider grid: 3 slots -->
        <div style="margin:8px 0 6px;font-size:11px;color:#388bfd;font-weight:700;letter-spacing:1px">
          &#x26A1; MULTI-PROVIDER ROUTING — tránh rate limit
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:10px">

          <!-- Slot 1: Analysts -->
          <div style="background:#0d1117;border:1px solid #1a3a5a;border-radius:6px;padding:6px 8px">
            <div style="font-size:10px;color:#388bfd;font-weight:700;margin-bottom:4px">
              &#x1F4CA; ANALYSTS <span style="color:#484f58;font-weight:400">(4 calls)</span>
            </div>
            <div style="font-size:9px;color:#484f58;margin-bottom:3px">market · social · news · fundamentals</div>
            <select id="ta-prov-analyst" onchange="taUpdateSlotModel('analyst',this.value)"
                    style="width:100%;background:#161b22;border:1px solid #21262d;color:#c9d1d9;font-size:11px;border-radius:3px;padding:2px 4px;margin-bottom:3px">
              <option value="google">Google (Free)</option>
              <option value="groq">Groq (Free)</option>
              <option value="deepseek">DeepSeek</option>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="openrouter">OpenRouter</option>
            </select>
            <input id="ta-model-analyst" placeholder="model" value="gemini-3.6-flash"
                   style="width:100%;background:#161b22;border:1px solid #21262d;color:#8b949e;font-size:10px;border-radius:3px;padding:2px 4px;box-sizing:border-box">
          </div>

          <!-- Slot 2: Researchers -->
          <div style="background:#0d1117;border:1px solid #1a3a5a;border-radius:6px;padding:6px 8px">
            <div style="font-size:10px;color:#f0883e;font-weight:700;margin-bottom:4px">
              &#x1F50D; RESEARCHERS <span style="color:#484f58;font-weight:400">(6 calls)</span>
            </div>
            <div style="font-size:9px;color:#484f58;margin-bottom:3px">bull · bear · trader · risk×3</div>
            <select id="ta-prov-researcher" onchange="taUpdateSlotModel('researcher',this.value)"
                    style="width:100%;background:#161b22;border:1px solid #21262d;color:#c9d1d9;font-size:11px;border-radius:3px;padding:2px 4px;margin-bottom:3px">
              <option value="google">Google (Free)</option>
              <option value="groq">Groq (Free)</option>
              <option value="deepseek">DeepSeek</option>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="openrouter">OpenRouter</option>
            </select>
            <input id="ta-model-researcher" placeholder="model" value="gemini-3.6-flash"
                   style="width:100%;background:#161b22;border:1px solid #21262d;color:#8b949e;font-size:10px;border-radius:3px;padding:2px 4px;box-sizing:border-box">
          </div>

          <!-- Slot 3: Managers -->
          <div style="background:#0d1117;border:1px solid #1a3a5a;border-radius:6px;padding:6px 8px">
            <div style="font-size:10px;color:#3fb950;font-weight:700;margin-bottom:4px">
              &#x1F9E0; MANAGERS <span style="color:#484f58;font-weight:400">(2 calls)</span>
            </div>
            <div style="font-size:9px;color:#484f58;margin-bottom:3px">research mgr · portfolio mgr</div>
            <select id="ta-prov-manager" onchange="taUpdateSlotModel('manager',this.value)"
                    style="width:100%;background:#161b22;border:1px solid #21262d;color:#c9d1d9;font-size:11px;border-radius:3px;padding:2px 4px;margin-bottom:3px">
              <option value="google">Google (Free)</option>
              <option value="groq">Groq (Free)</option>
              <option value="deepseek">DeepSeek</option>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="openrouter">OpenRouter</option>
            </select>
            <input id="ta-model-manager" placeholder="model" value="gemini-3.6-flash"
                   style="width:100%;background:#161b22;border:1px solid #21262d;color:#8b949e;font-size:10px;border-radius:3px;padding:2px 4px;box-sizing:border-box">
          </div>
        </div>

        <!-- Analysts selector -->
        <div style="margin-bottom:10px">
          <span style="font-size:11px;color:#484f58;margin-right:4px">Analysts:</span>
          <span class="ta-analyst-chip active" data-key="market" onclick="taToggleAnalyst('market')">&#x1F4C8; Market</span>
          <span class="ta-analyst-chip active" data-key="news"   onclick="taToggleAnalyst('news')">&#x1F4F0; News</span>
          <span class="ta-analyst-chip active" data-key="social" onclick="taToggleAnalyst('social')">&#x1F4AC; Social</span>
          <span class="ta-analyst-chip" data-key="fundamentals" onclick="taToggleAnalyst('fundamentals')">&#x1F4CA; Fundamentals</span>
        </div>

        <div id="ta-result" style="color:#484f58;font-size:12px;padding:10px 0">
          Mỗi slot dùng provider khác nhau → tránh rate limit. Mỗi lần chạy 3–10 phút.
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
            <label style="font-size:12px;color:#8b949e">Max Positions:</label>
            <input id="set-max-positions" type="number" value="${d.settings.max_open_positions || 6}" style="width:55px" min="1" max="20">
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

    // Signal candidates table
    if (d.candidates && d.candidates.length > 0) {
        html += `<div class="section"><table><tr><th>Coin</th><th>Signal</th><th>Score</th><th>Now</th><th>Entry Target</th><th>RSI</th><th>Reason</th></tr>`;
        d.candidates.forEach(c => {
            const filled = Math.round(c.score / 10);
            const bar = '&#x2588;'.repeat(filled) + '&#x2591;'.repeat(10 - filled);
            const pStr = c.price >= 1000 ? fmtUsd(c.price) : '$' + fmt(c.price, c.price >= 1 ? 3 : 5);
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
        html += `</table></div>`;
    }

    // Armed Entries — lệnh đang chờ giá tới zone
    const armed = d.armed_entries || {};
    const armedKeys = Object.keys(armed);
    if (armedKeys.length > 0) {
        const offsetOn  = d.entry_offset_enabled === true;
        const offsetPct = ((d.entry_offset_pct || 0.003) * 100).toFixed(1);
        html += `<div class="section">`;
        html += `<b style="font-size:12px;color:#58a6ff">🎯 ARMED — Chờ giá tới zone (MARKET ngay khi chạm):</b>`
              + (offsetOn ? `<span style="font-size:11px;color:#d29922;margin-left:8px">⚡ Entry Offset ${offsetPct}% bật</span>` : '');
        html += `<table style="margin-top:6px"><tr><th>Coin</th><th>Signal</th><th>Entry</th><th>SL</th><th>TP</th><th>RR</th><th>TTL</th></tr>`;
        armedKeys.forEach(sym => {
            const a = armed[sym];
            const ttl = Math.max(0, 900 - Math.round(Date.now()/1000 - a.ts));
            const ttlStr = ttl > 60 ? Math.floor(ttl/60)+'m' : ttl+'s';
            const ep  = a.entry_price >= 1 ? '$'+a.entry_price.toFixed(4) : '$'+a.entry_price.toFixed(6);
            const slp = a.sl >= 1 ? '$'+a.sl.toFixed(4) : '$'+a.sl.toFixed(6);
            const tpp = a.tp >= 1 ? '$'+a.tp.toFixed(4) : '$'+a.tp.toFixed(6);
            // Hiển thị giá gốc → giá sau offset
            const rawEp = (a.raw_entry || a.entry_price);
            const hasOffset = offsetOn && Math.abs(rawEp - a.entry_price) > 0.000001;
            const rawFmt = rawEp >= 1 ? '$'+rawEp.toFixed(4) : '$'+rawEp.toFixed(6);
            const entryDisplay = hasOffset
                ? `<span style="color:#8b949e;text-decoration:line-through;font-size:10px">${rawFmt}</span> <b style="color:#d29922">${ep}</b>`
                : `<b>${ep}</b>`;
            html += `<tr>
                <td><b>${sym.replace('USDT','')}</b></td>
                <td>${a.signal === 'LONG' ? '<span class="green">LONG</span>' : '<span class="red">SHORT</span>'}</td>
                <td>${entryDisplay}</td>
                <td style="color:#f85149">${slp}</td>
                <td style="color:#3fb950">${tpp}</td>
                <td>1:${a.rr.toFixed(1)}</td>
                <td style="color:#d29922">${ttlStr}</td>
            </tr>`;
        });
        html += `</table></div>`;
    }

    html += `<div class="footer">Auto-refresh 1s</div>`;

    // ── P0 SCAN SETTINGS ─────────────────────────────────────
    html += `
    <div class="section" id="p0-settings-section" style="margin-top:12px">
      <h2 style="cursor:pointer" onclick="toggleP0Settings()">
        &#x2699; Scan Engine P0 Settings
        <span id="p0-arrow" style="font-size:11px;color:#8b949e">&#x25BC;</span>
      </h2>
      <div id="p0-settings-body" style="display:none">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px">

          <!-- BTC Filter -->
          <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px">
            <div style="font-size:12px;color:#58a6ff;margin-bottom:8px;font-weight:600">📡 BTC Context Filter</div>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;margin-bottom:6px">
              <input type="checkbox" id="p0-btc-enabled" style="width:14px;height:14px">
              <span style="color:#c9d1d9">Bật BTC Filter</span>
            </label>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;margin-bottom:6px">
              <input type="checkbox" id="p0-btc-block" style="width:14px;height:14px">
              <span style="color:#c9d1d9">Block cứng khi BTC strong ngược chiều</span>
            </label>
            <div style="font-size:11px;color:#484f58;margin-top:4px">Tắt nếu watchlist là coin dev/low cap</div>
          </div>

          <!-- Daily Kill Switch -->
          <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px">
            <div style="font-size:12px;color:#f85149;margin-bottom:8px;font-weight:600">🛑 Daily Kill Switch</div>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;margin-bottom:6px">
              <input type="checkbox" id="p0-kill-enabled" style="width:14px;height:14px">
              <span style="color:#c9d1d9">Bật Kill Switch</span>
            </label>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:110px">Max loss/ngày:</span>
              <input type="number" id="p0-max-daily-loss" min="0.5" max="10" step="0.5"
                     style="width:60px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">% account</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:12px;color:#8b949e;width:110px">Lỗ liên tiếp:</span>
              <input type="number" id="p0-max-consec" min="2" max="10" step="1"
                     style="width:60px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">lần → pause</span>
            </div>
          </div>

          <!-- Position Sizing -->
          <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px">
            <div style="font-size:12px;color:#3fb950;margin-bottom:8px;font-weight:600">📐 Position Sizing</div>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:110px">Risk/lệnh:</span>
              <input type="number" id="p0-risk-pct" min="0.1" max="3" step="0.1"
                     style="width:60px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px"
                     oninput="updateRiskNote()">
              <span style="font-size:11px;color:#484f58">% balance</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
              <span style="font-size:12px;color:#8b949e;width:110px">Max order:</span>
              <input type="number" id="p0-max-order" min="5" max="200" step="5"
                     style="width:60px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px"
                     oninput="updateRiskNote()">
              <span style="font-size:11px;color:#484f58">USDT notional</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
              <span style="font-size:12px;color:#8b949e;width:110px">Max positions:</span>
              <input type="number" id="p0-max-positions" min="1" max="20" step="1"
                     style="width:60px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">coin đồng thời</span>
            </div>
            <!-- Risk note realtime -->
            <div id="p0-risk-note" style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px;font-size:11px;color:#8b949e;line-height:1.6">
              —
            </div>
          </div>

          <!-- Regime + RR -->
          <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px">
            <div style="font-size:12px;color:#d29922;margin-bottom:8px;font-weight:600">📊 Regime + RR</div>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:110px">Min RR:</span>
              <input type="number" id="p0-min-rr" min="1" max="5" step="0.1"
                     style="width:60px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">reward:risk</span>
            </div>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;margin-bottom:6px">
              <input type="checkbox" id="p0-sl-struct" style="width:14px;height:14px">
              <span style="color:#c9d1d9">SL theo structure (swing)</span>
            </label>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="p0-kill-chaos" style="width:14px;height:14px">
              <span style="color:#c9d1d9">Skip CHAOS regime</span>
            </label>
          </div>

        </div>

        <div style="margin-top:10px;display:flex;align-items:center;gap:10px">
          <button class="btn btn-green" onclick="saveP0Settings()" style="font-size:13px">
            💾 Lưu Settings
          </button>
          <span id="p0-save-msg" style="font-size:12px;color:#3fb950"></span>
        </div>
      </div>
    </div>

    <!-- PARTIAL TP SECTION -->
    <div class="section" id="partial-tp-section" style="margin-top:12px">
      <h2 style="cursor:pointer" onclick="togglePartialTP()">
        💰 Partial TP — Chốt từng phần
        <span id="partial-tp-arrow" style="font-size:11px;color:#8b949e">▼</span>
      </h2>
      <div id="partial-tp-body" style="display:none">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:10px">

          <!-- TP1 -->
          <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px">
            <div style="font-size:12px;color:#3fb950;margin-bottom:8px;font-weight:600">🎯 TP1 — Chốt lần đầu</div>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;margin-bottom:8px">
              <input type="checkbox" id="ptp-enabled" style="width:14px;height:14px">
              <span style="color:#c9d1d9">Bật Partial TP</span>
            </label>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:120px">Kích hoạt khi lời:</span>
              <input type="number" id="ptp-tp1-pct" min="0.5" max="20" step="0.5"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">%</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:120px">Đóng bao nhiêu:</span>
              <input type="number" id="ptp-tp1-close" min="10" max="90" step="10"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">% vị thế</span>
            </div>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="ptp-move-sl" style="width:14px;height:14px">
              <span style="color:#c9d1d9">Dời SL về breakeven sau TP1</span>
            </label>
          </div>

          <!-- TP2 -->
          <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px">
            <div style="font-size:12px;color:#d29922;margin-bottom:8px;font-weight:600">🎯 TP2 — Chốt lần 2</div>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;margin-bottom:8px">
              <input type="checkbox" id="ptp-tp2-enabled" style="width:14px;height:14px">
              <span style="color:#c9d1d9">Bật TP2</span>
            </label>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:120px">Kích hoạt khi lời:</span>
              <input type="number" id="ptp-tp2-pct" min="1" max="30" step="0.5"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">%</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:120px">Đóng bao nhiêu:</span>
              <input type="number" id="ptp-tp2-close" min="10" max="90" step="10"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">% vị thế còn lại</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px">
              <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
                <input type="checkbox" id="ptp-apply-scan" style="width:14px;height:14px">
                <span style="color:#c9d1d9">Scan</span>
              </label>
              <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;margin-left:10px">
                <input type="checkbox" id="ptp-apply-pump" style="width:14px;height:14px">
                <span style="color:#c9d1d9">Pump</span>
              </label>
            </div>
          </div>

        </div>
        <div style="margin-top:10px;display:flex;align-items:center;gap:10px">
          <button class="btn btn-green" onclick="savePartialTP()" style="font-size:13px">
            💾 Lưu Partial TP
          </button>
          <span id="ptp-save-msg" style="font-size:12px;color:#3fb950"></span>
        </div>
      </div>
    </div>

    <!-- PROFIT PROTECTION + TRAILING SL SECTION -->
    <div class="section" style="margin-top:12px">
      <h2 style="cursor:pointer" onclick="togglePP()">
        🛡 Profit Protection + Trailing SL
        <span id="pp-arrow" style="font-size:11px;color:#8b949e">▼</span>
      </h2>
      <div id="pp-body" style="display:none">
        <div style="font-size:11px;color:#8b949e;margin-bottom:10px">
          Flow: Initial SL → Protection SL (+0.6%) → Trailing SL (+1.0%) | SL chỉ dịch 1 chiều, không bao giờ lỗ khi đã có lợi nhuận
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">

          <!-- Bật/tắt + Protection -->
          <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px">
            <div style="font-size:12px;color:#58a6ff;margin-bottom:8px;font-weight:600">🛡 Profit Protection</div>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;margin-bottom:8px">
              <input type="checkbox" id="pp-enabled" style="width:14px;height:14px">
              <span style="color:#c9d1d9">Bật Profit Protection</span>
            </label>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:130px">Trigger lời:</span>
              <input type="number" id="pp-trigger-pct" min="0.1" max="5" step="0.1"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">%</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:130px">Timer xác nhận:</span>
              <input type="number" id="pp-timer" min="5" max="60" step="5"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">giây</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:12px;color:#8b949e;width:130px">Fee buffer:</span>
              <input type="number" id="pp-fee-buf" min="0.05" max="0.5" step="0.05"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">% (phí+slip)</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;margin-top:6px">
              <span style="font-size:12px;color:#8b949e;width:130px">Protection buffer:</span>
              <input type="number" id="pp-protection-buf" min="0" max="1" step="0.05"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px"
                     title="SL trên entry bao nhiêu % (0=sát entry, 0.2=an toàn)">
              <span style="font-size:11px;color:#484f58">% (SL+entry)</span>
            </div>
          </div>

          <!-- Trailing -->
          <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px">
            <div style="font-size:12px;color:#3fb950;margin-bottom:8px;font-weight:600">📈 Trailing SL</div>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:130px">Trigger lời:</span>
              <input type="number" id="pp-trail-trigger" min="0.5" max="10" step="0.1"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">%</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:130px">Timer xác nhận:</span>
              <input type="number" id="pp-trail-timer" min="3" max="30" step="1"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">giây</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
              <span style="font-size:12px;color:#8b949e;width:130px">Trailing distance:</span>
              <input type="number" id="pp-trail-dist" min="0.1" max="3" step="0.1"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">%</span>
            </div>
            <div style="display:flex;gap:10px">
              <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
                <input type="checkbox" id="pp-apply-scan" style="width:14px;height:14px">
                <span style="color:#c9d1d9">Scan</span>
              </label>
              <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
                <input type="checkbox" id="pp-apply-pump" style="width:14px;height:14px">
                <span style="color:#c9d1d9">Pump</span>
              </label>
            </div>
          </div>

        </div>
        <div style="margin-top:10px;display:flex;align-items:center;gap:10px">
          <button class="btn btn-green" onclick="savePP()" style="font-size:13px">
            💾 Lưu Profit Protection
          </button>
          <span id="pp-save-msg" style="font-size:12px;color:#3fb950"></span>
        </div>

        <!-- PP Monitor realtime -->
        <div id="pp-monitor" style="margin-top:14px">
          <div style="font-size:11px;color:#58a6ff;font-weight:600;margin-bottom:6px">📡 Monitor realtime</div>
          <div id="pp-monitor-table" style="font-size:11px;color:#8b949e">Chưa có position nào kích hoạt PP</div>
        </div>
      </div>
    </div>`;

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
        <div style="flex:1"></div>
        <button onclick="clearTradeHistory()" style="padding:5px 14px;border-radius:6px;border:1px solid #f85149;background:transparent;color:#f85149;cursor:pointer;font-size:12px;font-weight:600;transition:all .2s;" onmouseover="this.style.background='#f85149';this.style.color='#fff'" onmouseout="this.style.background='transparent';this.style.color='#f85149'">🗑 Clear Data</button>
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
        const data = await r.json();
        // Nếu API trả lỗi (unauthorized, server error) → không render, giữ nguyên UI cũ
        if (!data || data.ok === false) return;
        _pumpData = data;
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
    toast(r.msg || (r.ok ? 'Đã thêm' : 'Lỗi'), r.ok);
    if (r.ok) {
        inp.value = '';
        // Fetch lại với delay nhỏ để backend kịp cập nhật state
        setTimeout(async () => {
            _pumpRendered = false;
            await fetchPump();
        }, 300);
    }
}

async function removePumpCoin(sym) {
    await apiPost('/api/pump/coins/remove', {symbol: sym});
    setTimeout(async () => {
        _pumpRendered = false;
        await fetchPump();
    }, 300);
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
async function setPumpMinScore() {
    const val = parseInt(document.getElementById('pump-score-min-input')?.value || 50);
    if (val < 30 || val > 90) { toast('Score phải 30-90', false); return; }
    const r = await apiPost('/api/pump/set_min_score', {score: val});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    fetchPumpData();
}

async function setPumpCooldown() {
    const val = parseInt(document.getElementById('pump-cooldown-input')?.value || 5);
    if (val < 1 || val > 300) { toast('Cooldown phải 1-300 giây', false); return; }
    const r = await apiPost('/api/pump/set_cooldown', {cooldown: val});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    fetchPumpData();
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
          <span style="font-size:10px;color:#484f58;margin-left:8px">score≥</span>
          <input id="pump-score-min-input" type="number" min="30" max="90" value="${d.min_score || 50}"
                 style="width:40px;font-size:11px;background:#060d14;border:1px solid #1a3a2a;border-radius:4px;padding:2px 4px;color:#3fb950;text-align:center">
          <button class="btn btn-sm" onclick="setPumpMinScore()" style="font-size:10px;padding:2px 6px;background:#0d2a1a;color:#3fb950;border:1px solid #1a4a2a">Set</button>
          <span style="font-size:10px;color:#484f58;margin-left:8px">⏱cd</span>
          <input id="pump-cooldown-input" type="number" min="1" max="300" value="${d.pump_signal_cooldown || 5}"
                 style="width:42px;font-size:11px;background:#060d14;border:1px solid #1a3a2a;border-radius:4px;padding:2px 4px;color:#d29922;text-align:center"
                 title="Cooldown giây sau mỗi lần auto-short cùng coin">
          <span style="font-size:10px;color:#484f58">s</span>
          <button class="btn btn-sm" onclick="setPumpCooldown()" style="font-size:10px;padding:2px 6px;background:#1a1400;color:#d29922;border:1px solid #3a2a00">Set</button>
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
setInterval(fetchPump, 3000);
fetchPump();

// PnL stats refresh mỗi 30s (không cần nhanh)
setInterval(fetchPnlStats, 30000);
fetchPnlStats();

// TradingAgents — check kết quả cũ khi load trang
taCheckLastResult();

function updateClock(){document.getElementById('clock').textContent=new Date().toLocaleTimeString()}

// Lưu state input để không bị reset khi refresh
let _savedInputs = {};
function saveInputs() {
    ['order-symbol','order-side','order-usdt','order-sl','order-tp','order-lev','set-max-usdt','set-leverage','set-max-positions','add-coin-input','pump-coin-input'].forEach(id => {
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
let _lastP0Load = 0;         // timestamp lần cuối load P0 settings

async function refresh(){
    try{
        const r = await fetch('/api/state');
        const d = await r.json();

        // Backend busy — nếu dashboard chưa render thì hiện waiting, nếu đã render thì skip
        if (d.error) {
            if (_firstRender) {
                document.getElementById('content').innerHTML =
                    '<p style="color:#8b949e;text-align:center;padding:40px">⏳ Đang kết nối...</p>';
            }
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
        // Không xóa dashboard khi 1 request fail — chỉ thử lại lần sau
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

    // PP Monitor realtime
    updatePPMonitor(d);

    // Auto-refresh P0 Settings nếu panel đang mở (throttle mỗi 10 giây để tránh spam API)
    const p0Body = document.getElementById('p0-settings-body');
    const now = Date.now();
    if (p0Body && p0Body.style.display !== 'none' && now - _lastP0Load > 10000) {
        _lastP0Load = now;
        loadP0Settings();  // Tự động load P0 settings khi panel mở (10s/lần)
    }

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
    _setText('scan-info', `Scan #${d.scan_no} | Last: ${d.last_scan}`);

    // Open positions — rebuild nhỏ hơn
    const posEl = document.getElementById('positions-body');
    if (posEl) {
        let rows = '';
        if (d.open_positions && d.open_positions.length > 0) {
            d.open_positions.forEach(p => {
                const ppInfo = d.pp_state && d.pp_state[p.symbol];
                const tierBadge = ppInfo ? (
                    ppInfo.tier === 3 ? '<span style="color:#3fb950;font-size:10px">🛡T3</span>' :
                    ppInfo.tier === 2 ? '<span style="color:#d29922;font-size:10px">🛡T2</span>' :
                    '<span style="color:#484f58;font-size:10px">T1</span>'
                ) : '';
                rows += `<tr><td><b>${p.symbol.replace('USDT','')}</b> ${tierBadge}</td><td>${sideHtml(p.side)}</td>
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
setInterval(refresh, 3000);  // Giữ nguyên 3s - đừng làm giật UI
updateClock();
refresh();

// ── P0 SETTINGS ──────────────────────────────────────────────
function toggleP0Settings() {
    const body  = document.getElementById('p0-settings-body');
    const arrow = document.getElementById('p0-arrow');
    if (!body) return;
    const hidden = body.style.display === 'none';
    body.style.display = hidden ? 'block' : 'none';
    if (arrow) arrow.innerHTML = hidden ? '&#x25B2;' : '&#x25BC;';
    if (hidden) loadP0Settings();
}

function togglePP() {
    const body = document.getElementById('pp-body');
    const arrow = document.getElementById('pp-arrow');
    if (!body) return;
    const hidden = body.style.display === 'none';
    body.style.display = hidden ? 'block' : 'none';
    if (arrow) arrow.textContent = hidden ? '▲' : '▼';
    if (hidden) loadPP();
}

async function loadPP() {
    try {
        const r = await fetch('/api/pp/settings');
        const d = await r.json();
        if (!d.ok) return;
        const s = d.settings;
        const set = (id, val) => { const el = document.getElementById(id); if (el) { if (el.type==='checkbox') el.checked=!!val; else el.value=val; } };
        set('pp-enabled',       s.enabled);
        set('pp-trigger-pct',   s.trigger_pct);
        set('pp-timer',         s.timer_secs);
        set('pp-fee-buf',       s.fee_buffer_pct);
        set('pp-protection-buf', s.protection_buffer_pct);
        set('pp-trail-trigger', s.trailing_trigger_pct);
        set('pp-trail-timer',   s.trailing_timer_secs);
        set('pp-trail-dist',    s.trailing_distance_pct);
        set('pp-apply-scan',    s.apply_scan);
        set('pp-apply-pump',    s.apply_pump);
    } catch(e) {}
}

function updatePPMonitor(d) {
    const el = document.getElementById('pp-monitor-table');
    if (!el) return;
    const ppState = d.pp_state || {};
    const positions = d.open_positions || [];
    const prices = d.prices || {};

    if (!positions.length || !Object.keys(ppState).length) {
        el.innerHTML = '<span style="color:#484f58">Chưa có position nào kích hoạt PP</span>';
        return;
    }

    let rows = '';
    positions.forEach(p => {
        const ps = ppState[p.symbol];
        if (!ps) return;
        const mark = prices[p.symbol] || p.mark || 0;
        const entry = p.entry || 0;
        const isLong = p.side === 'LONG';
        const profit = entry > 0 ? ((isLong ? (mark - entry) : (entry - mark)) / entry * 100) : 0;

        const tier = ps.tier || 1;
        const tierBadge = tier === 3
            ? '<span style="color:#3fb950;font-weight:700">T3🛡</span>'
            : tier === 2
            ? '<span style="color:#d29922;font-weight:700">T2🛡</span>'
            : '<span style="color:#484f58">T1</span>';

        const sl = ps.current_sl || 0;
        const peak = ps.peak || 0;
        const trailSL = ps.trailing_sl || 0;
        const profitColor = profit >= 0 ? '#3fb950' : '#f85149';

        const fmt = (v) => v >= 1 ? '$'+v.toFixed(4) : (v > 0 ? '$'+v.toFixed(6) : '-');

        // ── Progress to next tier ──
        const nowTs = Date.now() / 1000;
        const ppTrigger = parseFloat(document.getElementById('pp-trigger-pct')?.value || 0.6);
        const ppTimer   = parseFloat(document.getElementById('pp-timer')?.value || 5);
        const trailTrigger = parseFloat(document.getElementById('pp-trail-trigger')?.value || 1.0);
        const trailTimer   = parseFloat(document.getElementById('pp-trail-timer')?.value || 3);

        let progressHtml = '';
        if (tier === 1) {
            // Progress tới Tier 2
            const pct = Math.max(0, Math.min(100, profit / ppTrigger * 100));
            const barColor = profit >= ppTrigger ? '#d29922' : (profit >= 0 ? '#3fb950' : '#f85149');
            const timerElapsed = ps.protection_ts > 0 ? Math.min(nowTs - ps.protection_ts, ppTimer) : 0;
            const timerPct = ps.protection_ts > 0 ? Math.min(100, timerElapsed / ppTimer * 100) : 0;
            const timerStr = ps.protection_ts > 0
                ? `⏱ ${timerElapsed.toFixed(0)}s/${ppTimer}s`
                : `${profit.toFixed(2)}% / ${ppTrigger}%`;
            progressHtml = `
                <div style="font-size:10px;color:#484f58">${timerStr}</div>
                <div style="background:#21262d;border-radius:3px;height:5px;width:80px;margin-top:2px">
                    <div style="background:${barColor};height:5px;border-radius:3px;width:${pct}%;transition:width 0.5s"></div>
                </div>
                ${ps.protection_ts > 0 && timerPct < 100 ? `<div style="background:#21262d;border-radius:3px;height:3px;width:80px;margin-top:1px"><div style="background:#d29922;height:3px;border-radius:3px;width:${timerPct}%"></div></div>` : ''}`;
        } else if (tier === 2) {
            // Progress tới Tier 3
            const pct = Math.max(0, Math.min(100, profit / trailTrigger * 100));
            const timerElapsed = ps.trailing_ts > 0 ? Math.min(nowTs - ps.trailing_ts, trailTimer) : 0;
            const timerPct = ps.trailing_ts > 0 ? Math.min(100, timerElapsed / trailTimer * 100) : 0;
            const timerStr = ps.trailing_ts > 0
                ? `⏱ ${timerElapsed.toFixed(0)}s/${trailTimer}s → T3`
                : `${profit.toFixed(2)}% / ${trailTrigger}%`;
            progressHtml = `
                <div style="font-size:10px;color:#d29922">${timerStr}</div>
                <div style="background:#21262d;border-radius:3px;height:5px;width:80px;margin-top:2px">
                    <div style="background:#3fb950;height:5px;border-radius:3px;width:${pct}%;transition:width 0.5s"></div>
                </div>
                ${ps.trailing_ts > 0 && timerPct < 100 ? `<div style="background:#21262d;border-radius:3px;height:3px;width:80px;margin-top:1px"><div style="background:#3fb950;height:3px;border-radius:3px;width:${timerPct}%"></div></div>` : ''}`;
        } else {
            progressHtml = `<div style="font-size:10px;color:#3fb950">✅ Trailing ON</div>`;
        }

        rows += `<tr style="border-bottom:1px solid #21262d">
            <td style="padding:3px 6px"><b>${p.symbol.replace('USDT','')}</b></td>
            <td style="padding:3px 6px">${p.side === 'LONG' ? '<span style="color:#3fb950">LONG</span>' : '<span style="color:#f85149">SHORT</span>'}</td>
            <td style="padding:3px 6px;color:${profitColor}"><b>${profit.toFixed(2)}%</b></td>
            <td style="padding:3px 6px">${tierBadge}</td>
            <td style="padding:3px 6px">${progressHtml}</td>
            <td style="padding:3px 6px;color:#f85149">${fmt(sl)}</td>
            <td style="padding:3px 6px;color:#58a6ff">${fmt(peak)}</td>
            <td style="padding:3px 6px;color:#d29922">${trailSL > 0 ? fmt(trailSL) : '-'}</td>
        </tr>`;
    });

    if (!rows) {
        el.innerHTML = '<span style="color:#484f58">Chưa có position nào kích hoạt PP</span>';
        return;
    }

    el.innerHTML = `<table style="width:100%;border-collapse:collapse">
        <tr style="color:#484f58;font-size:10px">
            <th style="text-align:left;padding:2px 6px">Coin</th>
            <th style="padding:2px 6px">Side</th>
            <th style="padding:2px 6px">Lợi</th>
            <th style="padding:2px 6px">Tier</th>
            <th style="padding:2px 6px">Progress</th>
            <th style="padding:2px 6px">SL hiện tại</th>
            <th style="padding:2px 6px">Peak</th>
            <th style="padding:2px 6px">Trailing SL</th>
        </tr>
        ${rows}
    </table>`;
}

async function savePP() {
    const get = (id) => { const el = document.getElementById(id); return el ? (el.type==='checkbox' ? el.checked : el.value) : null; };
    const payload = {
        enabled:               get('pp-enabled'),
        trigger_pct:           parseFloat(get('pp-trigger-pct')),
        timer_secs:            parseInt(get('pp-timer')),
        fee_buffer_pct:        parseFloat(get('pp-fee-buf')),
        protection_buffer_pct: parseFloat(get('pp-protection-buf')),
        trailing_trigger_pct:  parseFloat(get('pp-trail-trigger')),
        trailing_timer_secs:   parseInt(get('pp-trail-timer')),
        trailing_distance_pct: parseFloat(get('pp-trail-dist')),
        apply_scan:            get('pp-apply-scan'),
        apply_pump:            get('pp-apply-pump'),
    };
    const r = await apiPost('/api/pp/settings', payload);
    const msg = document.getElementById('pp-save-msg');
    if (msg) {
        msg.textContent = r.ok ? '✅ Đã lưu' : '❌ ' + (r.msg || 'Lỗi');
        msg.style.color = r.ok ? '#3fb950' : '#f85149';
        setTimeout(() => { if (msg) msg.textContent = ''; }, 3000);
    }
}

function togglePartialTP() {
    const body  = document.getElementById('partial-tp-body');
    const arrow = document.getElementById('partial-tp-arrow');
    if (!body) return;
    const hidden = body.style.display === 'none';
    body.style.display = hidden ? 'block' : 'none';
    if (arrow) arrow.textContent = hidden ? '▲' : '▼';
    if (hidden) loadPartialTP();
}

async function loadPartialTP() {
    try {
        const r = await fetch('/api/partial_tp/settings');
        const d = await r.json();
        if (!d.ok) return;
        const s = d.settings;
        const set = (id, val) => { const el = document.getElementById(id); if (el) { if (el.type==='checkbox') el.checked=!!val; else el.value=val; } };
        set('ptp-enabled',    s.enabled);
        set('ptp-tp1-pct',    s.tp1_pct);
        set('ptp-tp1-close',  s.tp1_close_pct);
        set('ptp-move-sl',    s.move_sl_be);
        set('ptp-tp2-enabled',s.tp2_enabled);
        set('ptp-tp2-pct',    s.tp2_pct);
        set('ptp-tp2-close',  s.tp2_close_pct);
        set('ptp-apply-scan', s.apply_scan);
        set('ptp-apply-pump', s.apply_pump);
    } catch(e) {}
}

async function savePartialTP() {
    const get = (id) => { const el = document.getElementById(id); return el ? (el.type==='checkbox' ? el.checked : el.value) : null; };
    const payload = {
        enabled:       get('ptp-enabled'),
        tp1_pct:       parseFloat(get('ptp-tp1-pct')),
        tp1_close_pct: parseFloat(get('ptp-tp1-close')),
        move_sl_be:    get('ptp-move-sl'),
        tp2_enabled:   get('ptp-tp2-enabled'),
        tp2_pct:       parseFloat(get('ptp-tp2-pct')),
        tp2_close_pct: parseFloat(get('ptp-tp2-close')),
        apply_scan:    get('ptp-apply-scan'),
        apply_pump:    get('ptp-apply-pump'),
    };
    const r = await apiPost('/api/partial_tp/settings', payload);
    const msg = document.getElementById('ptp-save-msg');
    if (msg) {
        msg.textContent = r.ok ? '✅ Đã lưu' : '❌ ' + (r.msg || 'Lỗi');
        msg.style.color = r.ok ? '#3fb950' : '#f85149';
        setTimeout(() => { if (msg) msg.textContent = ''; }, 3000);
    }
}

async function loadP0Settings() {
    try {
        const r = await fetch('/api/p0/settings');
        const d = await r.json();
        if (!d.ok) return;
        const s = d.settings;
        const set = (id, val) => { const el = document.getElementById(id); if (el) { if (el.type === 'checkbox') el.checked = !!val; else el.value = val; } };
        set('p0-btc-enabled',    s.btc_filter_enabled);
        set('p0-btc-block',      s.btc_strong_block);
        set('p0-kill-enabled',   s.daily_kill_switch_enabled);
        set('p0-max-daily-loss', (s.max_daily_loss_pct * 100).toFixed(1));
        set('p0-max-consec',     s.max_consecutive_losses);
        set('p0-risk-pct',       (s.risk_per_trade_pct * 100).toFixed(1));
        set('p0-max-order',      s.risk_max_order_usdt);
        set('p0-max-positions',  s.max_open_positions || 6);
        set('p0-min-rr',         s.min_rr);
        set('p0-sl-struct',      s.sl_structure_enabled);
        set('p0-kill-chaos',     s.chaos_skip_enabled);
        updateRiskNote();
    } catch(e) {}
}

function updateRiskNote() {
    const note = document.getElementById('p0-risk-note');
    if (!note) return;
    const riskPct  = parseFloat(document.getElementById('p0-risk-pct')?.value) || 1.0;
    const maxOrder = parseFloat(document.getElementById('p0-max-order')?.value) || 50;
    // Lấy balance từ dashboard
    const balEl = document.getElementById('stat-balance');
    const balStr = balEl ? balEl.textContent.replace(/[^0-9.]/g,'') : '0';
    const balance = parseFloat(balStr) || 0;

    if (balance <= 0) { note.innerHTML = '— (chưa có balance)'; return; }

    const riskUsdt   = balance * riskPct / 100;
    const sl2pct     = riskUsdt / 0.02;   // notional nếu SL=2%
    const autoMax    = balance * 0.5;     // max notional tự động = 50% balance
    const maxCap     = maxOrder > 0 ? Math.min(maxOrder, autoMax) : autoMax;
    const notional   = Math.min(sl2pct, maxCap);
    const lev        = parseInt(document.getElementById('set-leverage')?.value) || 5;
    const margin     = notional / lev;

    note.innerHTML =
        `💰 Balance: <b style="color:#e6edf3">$${balance.toFixed(2)}</b> &nbsp;|&nbsp; ` +
        `Risk ${riskPct}% = <b style="color:#f85149">$${riskUsdt.toFixed(2)}</b><br>` +
        `📐 Notional (SL=2%): <b style="color:#58a6ff">$${sl2pct.toFixed(1)}</b> → cap tại $${maxCap.toFixed(0)} (50% balance)<br>` +
        `🎯 Notional thực tế: <b style="color:#3fb950">$${notional.toFixed(1)}</b> &nbsp;|&nbsp; ` +
        `Margin (${lev}x): <b style="color:#3fb950">$${margin.toFixed(2)}</b><br>` +
        `<span style="color:${notional>=maxCap?'#d29922':'#484f58'}">` +
        `${notional>=maxCap?'⚠️ Đang bị cap — tự scale theo balance':'✅ Không bị cap'}</span>`;
}

async function saveP0Settings() {
    const get = (id) => { const el = document.getElementById(id); return el ? (el.type === 'checkbox' ? el.checked : el.value) : null; };
    const payload = {
        btc_filter_enabled:       get('p0-btc-enabled'),
        btc_strong_block:         get('p0-btc-block'),
        daily_kill_switch_enabled:get('p0-kill-enabled'),
        max_daily_loss_pct:       parseFloat(get('p0-max-daily-loss')) / 100,
        max_consecutive_losses:   parseInt(get('p0-max-consec')),
        risk_per_trade_pct:       parseFloat(get('p0-risk-pct')) / 100,
        risk_max_order_usdt:      parseFloat(get('p0-max-order')),
        max_open_positions:       parseInt(get('p0-max-positions')) || 6,
        min_rr:                   parseFloat(get('p0-min-rr')),
        sl_structure_enabled:     get('p0-sl-struct'),
        chaos_skip_enabled:       get('p0-kill-chaos'),
    };
    const r = await apiPost('/api/p0/settings', payload);
    const msg = document.getElementById('p0-save-msg');
    if (msg) {
        msg.textContent = r.ok ? '✅ Đã lưu' : '❌ ' + (r.msg || 'Lỗi');
        msg.style.color = r.ok ? '#3fb950' : '#f85149';
        setTimeout(() => { if (msg) msg.textContent = ''; }, 3000);
    }
}
</script>
</body>
</html>"""


@app.before_request
def check_auth():
    """Auto-authenticate mọi request."""
    session["authenticated"] = True
    return None
@app.route("/")
@require_auth
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/state")
def api_state():
    if _state is None:
        return jsonify({"error": "not initialized"})

    # Dùng timeout để không block mãi khi bot đang giữ lock
    acquired = _lock.acquire(timeout=5)
    if not acquired:
        # Trả về data cũ từ cache nếu có, không để dashboard trắng
        return jsonify({"error": "not initialized"})
    try:
        s = dict(_state)
        tlog = list(_state.get("trade_log", []))
        open_pos = list(_state.get("open_positions", []))
        splits = dict(_state.get("split_positions", {}))
        prices = dict(_state.get("prices", {}))
        liq_data = dict(_state.get("liq_data", {}))
        watchlist = list(_state.get("_watchlist", []))
        candidates = list(_state.get("candidates", []))
    finally:
        _lock.release()

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

    # Pending orders (lệnh chờ khớp) — đọc từ state (được update bởi limit_order_monitor)
    pending_orders = []
    with _lock:
        pending_orders = list(_state.get("pending_orders_cache", []))

    recent = sorted(closed, key=lambda t: t.get("time",""), reverse=True)[:15]
    trades_fmt = [{"symbol":t.get("symbol",""),"side":t.get("side",""),"entry":t.get("entry",0),
        "close":t.get("close",0),"pnl":t.get("pnl_usdt",0),"pct":t.get("pnl_pct",0),
        "time":t.get("time","")} for t in recent]

    # Entry targets — vùng liq THẬT từ WS real-time (giống Coinglass)
    # Chỉ hiện khi liq tracker đã có đủ data thật, không fallback giá fake
    # Entry targets — cache 10s để không tính lại mỗi request 3s
    _et_cache = getattr(api_state, "_entry_targets_cache", {})
    _et_ts    = getattr(api_state, "_entry_targets_ts", 0)
    if time.time() - _et_ts > 10:
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
                short_trigger = round(p * 1.01, 2 if p >= 100 else 6)
                long_trigger  = round(p * 0.99, 2 if p >= 100 else 6)

            entry_targets[sym] = {
                "short_entry": float(short_trigger) if short_trigger else 0,
                "long_entry":  float(long_trigger)  if long_trigger  else 0,
                "has_real_data": has_real_data,
            }
        api_state._entry_targets_cache = entry_targets
        api_state._entry_targets_ts    = time.time()
    else:
        entry_targets = _et_cache

    resp = jsonify({
        "running": s.get("running", False) and not s.get("paused", False),
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
        # Giảm liq_data: chỉ trả về top 10 coins có volume lớn nhất
        "liq_data": dict(sorted(liq_data.items(), key=lambda x: x[1].get("total_vol", 0), reverse=True)[:10]) if liq_data else {},
        "trades_history": trades_fmt,
        "watchlist": watchlist,
        "pp_state": {k: {"tier": v.get("tier",1), "current_sl": v.get("current_sl",0),
                         "trailing_sl": v.get("trailing_sl",0), "peak": v.get("peak_price",0),
                         "protection_ts": v.get("protection_ts",0), "trailing_ts": v.get("trailing_ts",0)}
                     for k, v in s.get("_pp_state", {}).items()},
        "settings": {
            "max_order_usdt": getattr(_config, "MAX_ORDER_USDT", 15),
            "leverage": getattr(_config, "LEVERAGE", 10),
            "max_open_positions": getattr(_config, "MAX_OPEN_POSITIONS", 6),
        },
        "reversal_monitor_enabled": getattr(_config, "REVERSAL_MONITOR_ENABLED", True),
        "reversal_alert_only":      getattr(_config, "REVERSAL_ALERT_ONLY", False),
        "pump_reversal_floor_pct":      getattr(_config, "PUMP_REVERSAL_FLOOR_PCT", 0.3),
        "scan_protect_enabled":     getattr(_config, "SCAN_PROTECT_ENABLED", True),
        "profit_lock_enabled":      getattr(_config, "PROFIT_LOCK_ENABLED", True),
        "trailing_lock_enabled":    getattr(_config, "TRAILING_LOCK_ENABLED", True),
        "mfe_scan_enabled":         getattr(_config, "MFE_SCAN_ENABLED", True),
        "mfe_retrace_pct":          getattr(_config, "MFE_RETRACE_PCT", 0.40),
        "entry_offset_enabled":     getattr(_config, "ENTRY_OFFSET_ENABLED", False),
        "entry_offset_pct":         getattr(_config, "ENTRY_OFFSET_PCT", 0.003),
        "breakeven_exit_enabled":     getattr(_config, "BREAKEVEN_EXIT_ENABLED", True),
        "breakeven_pump_hold_seconds": getattr(_config, "BREAKEVEN_PUMP_HOLD_SECONDS", 180),
        "breakeven_scan_hold_seconds": getattr(_config, "BREAKEVEN_SCAN_HOLD_SECONDS", 300),
        "breakeven_pump_peak_pct":    getattr(_config, "BREAKEVEN_PUMP_PEAK_PCT", 3.0),
        "breakeven_scan_peak_pct":    getattr(_config, "BREAKEVEN_SCAN_PEAK_PCT", 2.0),
        "breakeven_pump_pnl_floor":   getattr(_config, "BREAKEVEN_PUMP_PNL_FLOOR", 1.0),
        "breakeven_scan_pnl_floor":   getattr(_config, "BREAKEVEN_SCAN_PNL_FLOOR", 0.7),
        "breakeven_reversal_confirm": getattr(_config, "BREAKEVEN_REVERSAL_CONFIRM", 2),
        "profit_lock_enabled":        getattr(_config, "PROFIT_LOCK_ENABLED", True),
        "profit_lock_min_pct":        getattr(_config, "PROFIT_LOCK_MIN_PCT", 15.0),
        "profit_lock_high_pct":       getattr(_config, "PROFIT_LOCK_HIGH_PCT", 50.0),
        "profit_lock_speed_pct":      getattr(_config, "PROFIT_LOCK_SPEED_PCT", 1.5),
        "max_loss_enabled":         getattr(_config, "MAX_LOSS_ENABLED", True),
        "max_loss_value":           getattr(_config, "MAX_LOSS_PER_POSITION", 20.0),
        "candidates": [{"symbol": c.symbol, "signal": c.signal, "score": c.score,
                         "rsi": c.rsi, "trend": c.trend, "reason": c.reason,
                         "price": prices.get(c.symbol, 0)}
                        for c in candidates[:10]] if candidates else [],
        "pending_watch": _get_pending_watch_safe(),
        "armed_entries": {sym: {"signal": v["signal"], "entry_price": v["entry_price"],
                                "raw_entry": v.get("raw_entry", v["entry_price"]),
                                "sl": v["sl"], "tp": v["tp"], "rr": v["rr"],
                                "score": v["score"], "ts": v["ts"],
                                "reason": v.get("reason","")}
                          for sym, v in _state.get("armed_entries", {}).items()} if _state else {},        "split_positions_web": [{
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
        is_paused = _state.get("paused", False)

    if not current or is_paused:
        # Đang paused → gọi restart callback (đăng ký từ bot.py)
        restart_fn = _state.get("_restart_fn")
        if restart_fn:
            try:
                with _lock:
                    _state["paused"] = False
                restart_fn()
                return jsonify({"ok": True, "msg": "Bot restarted ✅", "running": True})
            except Exception as e:
                return jsonify({"ok": False, "msg": f"Restart failed: {e}", "running": False})
        else:
            # Fallback: chỉ set running=True (thread còn sống)
            with _lock:
                _state["running"] = True
                _state["paused"] = False
            return jsonify({"ok": True, "msg": "Bot resumed", "running": True})
    else:
        # Đang chạy → pause
        with _lock:
            _state["running"] = False
            _state["paused"] = True
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
        if hasattr(_config, "FIXED_COINS") and symbol not in _config.FIXED_COINS:
            _config.FIXED_COINS.append(symbol)
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
        try:
            _noti = _state.get("_notifier") if _state else None
            if _noti:
                _noti.telegram.send(f"⚡ <b>QUICK {side}</b>\n🪙 {symbol} @ ${price:,.6g}\n📦 qty={qty} {leverage}x")
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
    """Update bot settings: MAX_ORDER_USDT, LEVERAGE, MAX_OPEN_POSITIONS."""
    data = request.get_json() or {}
    max_usdt = data.get("max_order_usdt")
    leverage = data.get("leverage")
    max_positions = data.get("max_open_positions")

    msgs = []
    if max_usdt is not None and float(max_usdt) > 0:
        _config.MAX_ORDER_USDT = float(max_usdt)
        msgs.append(f"USD/order=${max_usdt}")
    if leverage is not None and 1 <= int(leverage) <= 125:
        _config.LEVERAGE = int(leverage)
        msgs.append(f"Leverage={leverage}x")
    if max_positions is not None and 1 <= int(max_positions) <= 20:
        _config.MAX_OPEN_POSITIONS = int(max_positions)
        msgs.append(f"Max Positions={max_positions}")

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

    # Lấy % thay đổi 24h cho tất cả pump coins (1 API call, cache 120s)
    # Chạy trong background thread — không block request handler
    _now = time.time()
    cache = getattr(_api_pump_state_inner, "_ticker_cache", {})
    cache_ts = getattr(_api_pump_state_inner, "_ticker_ts", 0)
    if _now - cache_ts > 120 and not getattr(_api_pump_state_inner, "_ticker_fetching", False):
        _api_pump_state_inner._ticker_fetching = True
        def _fetch_ticker():
            try:
                import requests as _req
                base = getattr(_config, "LIVE_BASE_URL", "https://fapi.binance.com")
                resp = _req.get(f"{base}/fapi/v1/ticker/24hr", timeout=5)
                if resp.ok:
                    new_cache = dict(getattr(_api_pump_state_inner, "_ticker_cache", {}))
                    _w = set(getattr(_api_pump_state_inner, "_last_watch", []))
                    for t in resp.json():
                        s = t.get("symbol", "")
                        if not _w or s in _w:
                            new_cache[s] = {
                                "change_pct": float(t.get("priceChangePercent", 0)),
                                "low":        float(t.get("lowPrice", 0)),
                                "high":       float(t.get("highPrice", 0)),
                            }
                    _api_pump_state_inner._ticker_cache = new_cache
                    _api_pump_state_inner._ticker_ts    = time.time()
            except Exception:
                pass
            finally:
                _api_pump_state_inner._ticker_fetching = False
        import threading as _th
        _th.Thread(target=_fetch_ticker, daemon=True).start()
    _api_pump_state_inner._last_watch = watch
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
        "pump_signal_cooldown": getattr(_config, "PUMP_SIGNAL_COOLDOWN_S", 5),
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


@app.route("/api/pump/set_min_score", methods=["POST"])
@require_auth
def api_pump_set_min_score():
    """Set min score cho pump mạnh radar từ web UI."""
    data  = request.get_json() or {}
    score = int(data.get("score", 50))
    score = max(30, min(90, score))
    try:
        import config as _cfg
        _cfg.PUMP_TOP_MIN_SCORE = score
    except Exception:
        pass
    return jsonify({"ok": True, "msg": f"Pump min score = {score}", "score": score})


@app.route("/api/pump/set_cooldown", methods=["POST"])
@require_auth
def api_pump_set_cooldown():
    """Set PUMP_SIGNAL_COOLDOWN_S — thời gian chờ trước khi auto-short lại cùng coin."""
    data     = request.get_json() or {}
    cooldown = int(data.get("cooldown", 5))
    cooldown = max(1, min(300, cooldown))
    try:
        import config as _cfg, os as _os, re as _re
        _cfg.PUMP_SIGNAL_COOLDOWN_S = cooldown
        # Ghi vào file để persist
        config_path = _os.path.join(_os.path.dirname(__file__), "config.py")
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
        content = _re.sub(r'PUMP_SIGNAL_COOLDOWN_S\s*=\s*\d+',
                         f'PUMP_SIGNAL_COOLDOWN_S = {cooldown}', content)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[PumpRadar] PUMP_SIGNAL_COOLDOWN_S = {cooldown}s")
    except Exception as e:
        return jsonify({"ok": False, "msg": f"❌ Lỗi: {e}"})
    return jsonify({"ok": True, "msg": f"⏱ Cooldown auto-short = {cooldown}s", "cooldown": cooldown})


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


@app.route("/api/breakeven_exit", methods=["POST"])
@require_auth
def api_breakeven_exit():
    """Bật/tắt Breakeven Exit — đóng sớm khi sắp về entry."""
    data    = request.get_json() or {}
    enabled = data.get("enabled", True)
    try:
        import config as _cfg
        _cfg.BREAKEVEN_EXIT_ENABLED = bool(enabled)
    except Exception:
        pass
    return jsonify({"ok": True, "msg": f"Breakeven Exit: {'bật' if enabled else 'tắt'}", "enabled": bool(enabled)})

@app.route("/api/breakeven_exit/hold", methods=["POST"])
@require_auth
def api_breakeven_exit_hold():
    """Set thời gian delay riêng cho pump và scan."""
    data = request.get_json() or {}
    pump_s = max(0, min(3600, int(data.get("pump_seconds", 180))))
    scan_s = max(0, min(3600, int(data.get("scan_seconds", 300))))
    try:
        import config as _cfg
        _cfg.BREAKEVEN_PUMP_HOLD_SECONDS = pump_s
        _cfg.BREAKEVEN_SCAN_HOLD_SECONDS = scan_s
    except Exception:
        pass
    return jsonify({"ok": True, "msg": f"Breakeven delay: Pump={pump_s}s Scan={scan_s}s"})

@app.route("/api/breakeven_exit/advanced", methods=["POST"])
@require_auth
def api_breakeven_exit_advanced():
    """Set Peak Profit Trailing params."""
    data = request.get_json() or {}
    try:
        import config as _cfg
        _cfg.BREAKEVEN_PUMP_PEAK_PCT    = float(data.get("pump_peak", 3.0))
        _cfg.BREAKEVEN_PUMP_PNL_FLOOR   = float(data.get("pump_floor", 1.0))
        _cfg.BREAKEVEN_SCAN_PEAK_PCT    = float(data.get("scan_peak", 2.0))
        _cfg.BREAKEVEN_SCAN_PNL_FLOOR   = float(data.get("scan_floor", 0.7))
        _cfg.BREAKEVEN_REVERSAL_CONFIRM = int(data.get("reversal_confirm", 2))
    except Exception:
        pass
    return jsonify({"ok": True, "msg": f"Breakeven advanced: Pump peak={data.get('pump_peak')}% floor={data.get('pump_floor')}% | Scan peak={data.get('scan_peak')}% floor={data.get('scan_floor')}% | Rev×{data.get('reversal_confirm')}"})

@app.route("/api/mfe_scan", methods=["POST"])
@require_auth
def api_mfe_scan():
    """Bật/tắt MFE Scan Exit cho lệnh scan/quick/app."""
    data    = request.get_json() or {}
    enabled = data.get("enabled", True)
    try:
        import config as _cfg
        _cfg.MFE_SCAN_ENABLED = bool(enabled)
    except Exception:
        pass
    status = "bật" if enabled else "tắt"
    return jsonify({"ok": True, "msg": f"MFE Scan: {status}", "enabled": bool(enabled)})

@app.route("/api/entry_offset", methods=["POST"])
@require_auth
def api_entry_offset():
    """Bật/tắt và set Entry Offset % cho scan engine."""
    data = request.get_json() or {}
    try:
        if "enabled" in data:
            _config.ENTRY_OFFSET_ENABLED = bool(data["enabled"])
        if "pct" in data:
            _config.ENTRY_OFFSET_PCT = max(0.001, min(0.05, float(data["pct"])))
        enabled = getattr(_config, "ENTRY_OFFSET_ENABLED", False)
        pct     = getattr(_config, "ENTRY_OFFSET_PCT", 0.003)
        status  = f"bật {pct*100:.1f}%" if enabled else "tắt"
        return jsonify({"ok": True, "msg": f"Entry Offset: {status}", "enabled": enabled, "pct": pct})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/profit_lock", methods=["POST"])
@require_auth
def api_profit_lock():
    """Bật/tắt và config Profit Lock (min%, high%, speed%)."""
    data = request.get_json() or {}
    try:
        if "enabled" in data:
            _config.PROFIT_LOCK_ENABLED = bool(data["enabled"])
        if "min_pct" in data:
            _config.PROFIT_LOCK_MIN_PCT = max(0.5, min(10.0, float(data["min_pct"])))
        if "high_pct" in data:
            _config.PROFIT_LOCK_HIGH_PCT = max(5.0, min(50.0, float(data["high_pct"])))
        if "speed_pct" in data:
            _config.PROFIT_LOCK_SPEED_PCT = max(0.5, min(5.0, float(data["speed_pct"])))
        
        # Ghi persistent vào config.py
        import os, re as _re
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            for key, val_str in [
                ("PROFIT_LOCK_ENABLED",   str(_config.PROFIT_LOCK_ENABLED)),
                ("PROFIT_LOCK_MIN_PCT",   str(round(_config.PROFIT_LOCK_MIN_PCT, 1))),
                ("PROFIT_LOCK_HIGH_PCT",  str(round(_config.PROFIT_LOCK_HIGH_PCT, 1))),
                ("PROFIT_LOCK_SPEED_PCT", str(round(_config.PROFIT_LOCK_SPEED_PCT, 1))),
            ]:
                content = _re.sub(rf"(?m)^{key}\s*=\s*.+$", f"{key} = {val_str}", content)
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"[ProfitLock] Config saved to {config_path}")
        except Exception as ex:
            logger.warning(f"[ProfitLock] Cannot persist config: {ex}")
        
        enabled = getattr(_config, "PROFIT_LOCK_ENABLED", True)
        min_pct = getattr(_config, "PROFIT_LOCK_MIN_PCT", 15.0)
        high_pct = getattr(_config, "PROFIT_LOCK_HIGH_PCT", 50.0)
        speed_pct = getattr(_config, "PROFIT_LOCK_SPEED_PCT", 1.5)
        status = f"bật Min:{min_pct:.1f}% High:{high_pct:.1f}% Speed:{speed_pct:.1f}%/s" if enabled else "tắt"
        return jsonify({"ok": True, "msg": f"Profit Lock: {status}", "enabled": enabled,
                        "min_pct": min_pct, "high_pct": high_pct, "speed_pct": speed_pct})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

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
        # Ghi persistent vào config.py
        import os, re as _re
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            for key, val_str in [
                ("MAX_LOSS_ENABLED",      str(_cfg.MAX_LOSS_ENABLED)),
                ("MAX_LOSS_PER_POSITION", str(round(_cfg.MAX_LOSS_PER_POSITION, 1))),
            ]:
                pattern = rf'^({key}\s*=\s*).*$'
                new_content, n = _re.subn(pattern, f'{key:<28}= {val_str}', content, flags=_re.MULTILINE)
                if n > 0:
                    content = new_content
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as _e:
            logger.warning(f"[MaxLoss] Config write failed: {_e}")
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

@app.route("/api/pump_reversal_config", methods=["POST"])
@require_auth
def api_pump_reversal_config():
    """Set Floor cho Pump Reversal Exit."""
    data = request.get_json() or {}
    try:
        import config as _cfg
        if "floor" in data:
            _cfg.PUMP_REVERSAL_FLOOR_PCT = max(0.0, min(5.0, float(data["floor"])))
    except Exception:
        pass
    return jsonify({"ok": True, "msg": f"Pump Reversal: Floor≤{getattr(_config,'PUMP_REVERSAL_FLOOR_PCT',0.3)}%"})


@app.route("/api/p0/settings", methods=["GET"])
def api_p0_settings_get():
    """Trả về P0 scan settings hiện tại."""
    try:
        import config as _cfg
        return jsonify({"ok": True, "settings": {
            "btc_filter_enabled":        getattr(_cfg, "BTC_FILTER_ENABLED",        True),
            "btc_strong_block":          getattr(_cfg, "BTC_STRONG_BLOCK",          True),
            "daily_kill_switch_enabled": getattr(_cfg, "DAILY_KILL_SWITCH_ENABLED", True),
            "max_daily_loss_pct":        getattr(_cfg, "MAX_DAILY_LOSS_PCT",        0.03),
            "max_consecutive_losses":    getattr(_cfg, "MAX_CONSECUTIVE_LOSSES",    3),
            "risk_per_trade_pct":        getattr(_cfg, "RISK_PER_TRADE_PCT",        0.01),
            "risk_max_order_usdt":       getattr(_cfg, "RISK_MAX_ORDER_USDT",       50.0),
            "max_open_positions":        getattr(_cfg, "MAX_OPEN_POSITIONS",        6),
            "min_rr":                    getattr(_cfg, "MIN_RR",                    1.5),
            "sl_structure_enabled":      getattr(_cfg, "SL_STRUCTURE_ENABLED",      True),
            "chaos_skip_enabled":        getattr(_cfg, "CHAOS_ATR_MULT",            2.5) > 0,
        }})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/p0/settings", methods=["POST"])
@require_auth
def api_p0_settings_save():
    """Lưu P0 scan settings vào config runtime + ghi persistent vào config.py."""
    data = request.get_json() or {}
    try:
        import config as _cfg

        if "btc_filter_enabled" in data:
            _cfg.BTC_FILTER_ENABLED        = bool(data["btc_filter_enabled"])
        if "btc_strong_block" in data:
            _cfg.BTC_STRONG_BLOCK          = bool(data["btc_strong_block"])
        if "daily_kill_switch_enabled" in data:
            _cfg.DAILY_KILL_SWITCH_ENABLED = bool(data["daily_kill_switch_enabled"])
        if "max_daily_loss_pct" in data:
            _cfg.MAX_DAILY_LOSS_PCT        = max(0.005, min(0.2, float(data["max_daily_loss_pct"])))
        if "max_consecutive_losses" in data:
            _cfg.MAX_CONSECUTIVE_LOSSES    = max(1, min(10, int(data["max_consecutive_losses"])))
        if "risk_per_trade_pct" in data:
            _cfg.RISK_PER_TRADE_PCT        = max(0.001, min(0.05, float(data["risk_per_trade_pct"])))
        if "risk_max_order_usdt" in data:
            _cfg.RISK_MAX_ORDER_USDT       = max(0.0, min(500.0, float(data["risk_max_order_usdt"])))
        if "max_open_positions" in data:
            _cfg.MAX_OPEN_POSITIONS        = max(1, min(20, int(data["max_open_positions"])))
        if "min_rr" in data:
            _cfg.MIN_RR                    = max(1.0, min(5.0, float(data["min_rr"])))
        if "sl_structure_enabled" in data:
            _cfg.SL_STRUCTURE_ENABLED      = bool(data["sl_structure_enabled"])
        if "chaos_skip_enabled" in data:
            _cfg.CHAOS_ATR_MULT = 2.5 if bool(data["chaos_skip_enabled"]) else 999.0

        # ── Ghi persistent vào config.py ──────────────────────────────
        import os, re as _re
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
        p0_map = {
            "BTC_FILTER_ENABLED":        str(_cfg.BTC_FILTER_ENABLED),
            "BTC_STRONG_BLOCK":          str(_cfg.BTC_STRONG_BLOCK),
            "DAILY_KILL_SWITCH_ENABLED": str(_cfg.DAILY_KILL_SWITCH_ENABLED),
            "MAX_DAILY_LOSS_PCT":        str(round(_cfg.MAX_DAILY_LOSS_PCT, 4)),
            "MAX_CONSECUTIVE_LOSSES":    str(_cfg.MAX_CONSECUTIVE_LOSSES),
            "RISK_PER_TRADE_PCT":        str(round(_cfg.RISK_PER_TRADE_PCT, 4)),
            "RISK_MAX_ORDER_USDT":       str(round(_cfg.RISK_MAX_ORDER_USDT, 1)),
            "MAX_OPEN_POSITIONS":        str(getattr(_cfg, "MAX_OPEN_POSITIONS", 6)),
            "MIN_RR":                    str(round(_cfg.MIN_RR, 1)),
            "SL_STRUCTURE_ENABLED":      str(_cfg.SL_STRUCTURE_ENABLED),
            "CHAOS_ATR_MULT":            str(round(_cfg.CHAOS_ATR_MULT, 1)),
        }
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            for key, val in p0_map.items():
                # Nếu key đã tồn tại → replace, không thì append
                pattern = rf'^({key}\s*=\s*).*$'
                replacement = f'{key:<28}= {val}'
                new_content, n = _re.subn(pattern, replacement, content,
                                          flags=_re.MULTILINE)
                if n > 0:
                    content = new_content
                else:
                    content += f'\n{replacement}'
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"[P0] Settings persistent saved to config.py")
            msg = "✅ P0 settings đã lưu (runtime + config.py)"
        except Exception as _e:
            logger.warning(f"[P0] Config write failed: {_e}")
            msg = f"✅ Runtime saved (config.py write failed: {_e})"

        logger.info(f"[P0] Settings saved: {data}")
        return jsonify({"ok": True, "msg": msg})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/partial_tp/settings", methods=["GET"])
def api_partial_tp_get():
    """Lấy Partial TP settings hiện tại."""
    try:
        import config as _cfg
        return jsonify({"ok": True, "settings": {
            "enabled":       getattr(_cfg, "PARTIAL_TP_ENABLED",      True),
            "tp1_pct":       getattr(_cfg, "PARTIAL_TP1_PCT",         2.0),
            "tp1_close_pct": getattr(_cfg, "PARTIAL_TP1_CLOSE_PCT",   50.0),
            "move_sl_be":    getattr(_cfg, "PARTIAL_TP_MOVE_SL_BE",   True),
            "tp2_enabled":   getattr(_cfg, "PARTIAL_TP2_ENABLED",     True),
            "tp2_pct":       getattr(_cfg, "PARTIAL_TP2_PCT",         4.0),
            "tp2_close_pct": getattr(_cfg, "PARTIAL_TP2_CLOSE_PCT",   30.0),
            "apply_scan":    getattr(_cfg, "PARTIAL_TP_APPLY_SCAN",   True),
            "apply_pump":    getattr(_cfg, "PARTIAL_TP_APPLY_PUMP",   True),
        }})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/partial_tp/settings", methods=["POST"])
@require_auth
def api_partial_tp_save():
    """Lưu Partial TP settings vào config runtime + config.py."""
    data = request.get_json() or {}
    try:
        import config as _cfg
        if "enabled"       in data: _cfg.PARTIAL_TP_ENABLED      = bool(data["enabled"])
        if "tp1_pct"       in data: _cfg.PARTIAL_TP1_PCT         = max(0.5, min(20.0, float(data["tp1_pct"])))
        if "tp1_close_pct" in data: _cfg.PARTIAL_TP1_CLOSE_PCT   = max(10.0, min(90.0, float(data["tp1_close_pct"])))
        if "move_sl_be"    in data: _cfg.PARTIAL_TP_MOVE_SL_BE   = bool(data["move_sl_be"])
        if "tp2_enabled"   in data: _cfg.PARTIAL_TP2_ENABLED     = bool(data["tp2_enabled"])
        if "tp2_pct"       in data: _cfg.PARTIAL_TP2_PCT         = max(1.0, min(30.0, float(data["tp2_pct"])))
        if "tp2_close_pct" in data: _cfg.PARTIAL_TP2_CLOSE_PCT   = max(10.0, min(90.0, float(data["tp2_close_pct"])))
        if "apply_scan"    in data: _cfg.PARTIAL_TP_APPLY_SCAN   = bool(data["apply_scan"])
        if "apply_pump"    in data: _cfg.PARTIAL_TP_APPLY_PUMP   = bool(data["apply_pump"])

        # Ghi persistent vào config.py
        import os, re as _re
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
        ptp_map = {
            "PARTIAL_TP_ENABLED":    str(_cfg.PARTIAL_TP_ENABLED),
            "PARTIAL_TP1_PCT":       str(_cfg.PARTIAL_TP1_PCT),
            "PARTIAL_TP1_CLOSE_PCT": str(_cfg.PARTIAL_TP1_CLOSE_PCT),
            "PARTIAL_TP_MOVE_SL_BE": str(_cfg.PARTIAL_TP_MOVE_SL_BE),
            "PARTIAL_TP2_ENABLED":   str(_cfg.PARTIAL_TP2_ENABLED),
            "PARTIAL_TP2_PCT":       str(_cfg.PARTIAL_TP2_PCT),
            "PARTIAL_TP2_CLOSE_PCT": str(_cfg.PARTIAL_TP2_CLOSE_PCT),
            "PARTIAL_TP_APPLY_SCAN": str(_cfg.PARTIAL_TP_APPLY_SCAN),
            "PARTIAL_TP_APPLY_PUMP": str(_cfg.PARTIAL_TP_APPLY_PUMP),
        }
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            for key, val in ptp_map.items():
                pattern = rf'^({key}\s*=\s*).*$'
                new_content, n = _re.subn(pattern, f'{key:<28}= {val}', content, flags=_re.MULTILINE)
                if n > 0:
                    content = new_content
                else:
                    content += f'\n{key:<28}= {val}'
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as _e:
            logger.warning(f"[PartialTP] Config write failed: {_e}")

        logger.info(f"[PartialTP] Settings saved: {data}")
        return jsonify({"ok": True, "msg": "✅ Partial TP đã lưu"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/pp/settings", methods=["GET"])
def api_pp_settings_get():
    """Lấy Profit Protection settings."""
    try:
        import config as _cfg
        return jsonify({"ok": True, "settings": {
            "enabled":               getattr(_cfg, "PROFIT_PROTECTION_ENABLED",  True),
            "trigger_pct":           getattr(_cfg, "PP_TRIGGER_PCT",             0.6),
            "timer_secs":            getattr(_cfg, "PP_TIMER_SECS",              15),
            "fee_buffer_pct":        getattr(_cfg, "PP_FEE_BUFFER_PCT",          0.15),
            "protection_buffer_pct": getattr(_cfg, "PP_PROTECTION_BUFFER_PCT",   0.2),
            "trailing_trigger_pct":  getattr(_cfg, "PP_TRAILING_TRIGGER_PCT",    1.0),
            "trailing_timer_secs":   getattr(_cfg, "PP_TRAILING_TIMER_SECS",     7),
            "trailing_distance_pct": getattr(_cfg, "PP_TRAILING_DISTANCE_PCT",   0.5),
            "apply_scan":            getattr(_cfg, "PP_APPLY_SCAN",              True),
            "apply_pump":            getattr(_cfg, "PP_APPLY_PUMP",              True),
        }})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/pp/settings", methods=["POST"])
@require_auth
def api_pp_settings_save():
    """Lưu Profit Protection settings vào runtime + config.py."""
    data = request.get_json() or {}
    try:
        import config as _cfg
        if "enabled"               in data: _cfg.PROFIT_PROTECTION_ENABLED  = bool(data["enabled"])
        if "trigger_pct"           in data: _cfg.PP_TRIGGER_PCT             = max(0.1, min(5.0,  float(data["trigger_pct"])))
        if "timer_secs"            in data: _cfg.PP_TIMER_SECS              = max(5,   min(60,   int(data["timer_secs"])))
        if "fee_buffer_pct"        in data: _cfg.PP_FEE_BUFFER_PCT          = max(0.05,min(0.5,  float(data["fee_buffer_pct"])))
        if "protection_buffer_pct" in data: _cfg.PP_PROTECTION_BUFFER_PCT   = max(0.0, min(1.0,  float(data["protection_buffer_pct"])))
        if "trailing_trigger_pct"  in data: _cfg.PP_TRAILING_TRIGGER_PCT    = max(0.5, min(10.0, float(data["trailing_trigger_pct"])))
        if "trailing_timer_secs"   in data: _cfg.PP_TRAILING_TIMER_SECS     = max(3,   min(30,   int(data["trailing_timer_secs"])))
        if "trailing_distance_pct" in data: _cfg.PP_TRAILING_DISTANCE_PCT   = max(0.1, min(3.0,  float(data["trailing_distance_pct"])))
        if "apply_scan"            in data: _cfg.PP_APPLY_SCAN              = bool(data["apply_scan"])
        if "apply_pump"            in data: _cfg.PP_APPLY_PUMP              = bool(data["apply_pump"])

        # Ghi persistent
        import os, re as _re
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
        pp_map = {
            "PROFIT_PROTECTION_ENABLED":  str(_cfg.PROFIT_PROTECTION_ENABLED),
            "PP_TRIGGER_PCT":             str(round(_cfg.PP_TRIGGER_PCT, 2)),
            "PP_TIMER_SECS":              str(_cfg.PP_TIMER_SECS),
            "PP_FEE_BUFFER_PCT":          str(round(_cfg.PP_FEE_BUFFER_PCT, 3)),
            "PP_PROTECTION_BUFFER_PCT":   str(round(_cfg.PP_PROTECTION_BUFFER_PCT, 2)),
            "PP_TRAILING_TRIGGER_PCT":    str(round(_cfg.PP_TRAILING_TRIGGER_PCT, 2)),
            "PP_TRAILING_TIMER_SECS":     str(_cfg.PP_TRAILING_TIMER_SECS),
            "PP_TRAILING_DISTANCE_PCT":   str(round(_cfg.PP_TRAILING_DISTANCE_PCT, 2)),
            "PP_APPLY_SCAN":              str(_cfg.PP_APPLY_SCAN),
            "PP_APPLY_PUMP":              str(_cfg.PP_APPLY_PUMP),
        }
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            for key, val in pp_map.items():
                pattern = rf'^({key}\s*=\s*).*$'
                new_content, n = _re.subn(pattern, f'{key:<32}= {val}', content, flags=_re.MULTILINE)
                content = new_content if n > 0 else content + f'\n{key:<32}= {val}'
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as _e:
            logger.warning(f"[PP] Config write failed: {_e}")

        logger.info(f"[PP] Settings saved: {data}")
        return jsonify({"ok": True, "msg": "✅ Profit Protection đã lưu"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})



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
    # Session timeout 30 ngày — không bị mất khi đóng tab
    from datetime import timedelta
    app.permanent_session_lifetime = timedelta(days=365)
    app.config["SESSION_COOKIE_PERMANENT"] = True
    app.config["SESSION_REFRESH_EACH_REQUEST"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = False  # HTTP không cần Secure
    app.config["SESSION_COOKIE_HTTPONLY"] = True

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
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
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


# ── TradingAgents AI Analysis endpoints ──────────────────────────────────────
import threading as _threading
import time as _time

_ta_state = {
    "running": False,
    "step": "",
    "elapsed_sec": 0,
    "last_result": None,
    "start_ts": 0,
    "agent_log": [],   # list các bước đã qua
}
_ta_lock = _threading.Lock()


def _ta_run_analysis(ticker: str, date: str, analysts: list, multi_provider: dict):
    """Run TradingAgents analysis in background thread."""
    import sys, os

    # Point Python to TradingAgents-main sibling directory
    ta_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "TradingAgents-main")
    )
    if ta_path not in sys.path:
        sys.path.insert(0, ta_path)

    # Load .env từ TradingAgents-main (chứa API keys)
    env_file = os.path.join(ta_path, ".env")
    if os.path.exists(env_file):
        with open(env_file) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    if _v.strip():
                        os.environ[_k.strip()] = _v.strip()

    # Kiểm tra API keys cho tất cả provider được dùng
    _api_key_map = {
        "openrouter": "OPENROUTER_API_KEY",
        "groq":       "GROQ_API_KEY",
        "deepseek":   "DEEPSEEK_API_KEY",
        "google":     "GOOGLE_API_KEY",
        "openai":     "OPENAI_API_KEY",
        "anthropic":  "ANTHROPIC_API_KEY",
    }
    seen_providers = set()
    for slot_name, slot_cfg in multi_provider.items():
        prov = slot_cfg.get("provider", "")
        if prov and prov not in seen_providers:
            seen_providers.add(prov)
            env_key = _api_key_map.get(prov)
            if env_key and not os.environ.get(env_key):
                raise ValueError(
                    f"Thiếu API key cho slot '{slot_name}' provider '{prov}'. "
                    f"Hãy điền {env_key} vào TradingAgents-main/.env"
                )

    def _set_step(msg):
        with _ta_lock:
            elapsed = int(_time.time() - _ta_state["start_ts"])
            _ta_state["step"] = f"{msg} ({elapsed}s)"
            _ta_state["elapsed_sec"] = elapsed
            _ta_state["agent_log"].append(f"[{elapsed:>4}s] {msg}")
            logger.info("[TradingAgents] %s (%ds)", msg, elapsed)

    try:
        slot_summary = " | ".join(
            f"{k}: {v['provider']}/{v['model']}" for k, v in multi_provider.items()
        )
        _set_step(f"Khởi tạo multi-provider LLMs")
        logger.info("[TradingAgents] Multi-provider: %s", slot_summary)

        from tradingagents.graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG
        from langchain_core.callbacks import BaseCallbackHandler

        # Callback để track từng agent node đang chạy
        class _StepTracker(BaseCallbackHandler):
            _AGENT_LABELS = {
                "market":       "📈 Market Analyst",
                "social":       "💬 Social Analyst",
                "news":         "📰 News Analyst",
                "fundamentals": "📊 Fundamentals Analyst",
                "Bull":         "🐂 Bull Researcher",
                "Bear":         "🐻 Bear Researcher",
                "Research":     "🧠 Research Manager",
                "Trader":       "💹 Trader",
                "Aggressive":   "⚡ Risk (Aggressive)",
                "Conservative": "🛡️ Risk (Conservative)",
                "Neutral":      "⚖️ Risk (Neutral)",
                "Portfolio":    "📋 Portfolio Manager",
            }
            def on_chat_model_start(self, serialized, messages, **kwargs):
                # Đoán agent từ messages nếu có thể
                pass
            def on_llm_start(self, serialized, prompts, **kwargs):
                name = (serialized or {}).get("name", "")
                label = next((v for k, v in self._AGENT_LABELS.items() if k.lower() in name.lower()), f"🤖 {name}" if name else "🤖 LLM call")
                _set_step(label)

        tracker = _StepTracker()

        # Dùng analyst slot làm primary provider để backward compat
        analyst_cfg    = multi_provider.get("analyst",    {})
        researcher_cfg = multi_provider.get("researcher", {})
        manager_cfg    = multi_provider.get("manager",    {})

        primary_provider = analyst_cfg.get("provider", "deepseek")
        primary_quick    = analyst_cfg.get("model", "deepseek-v4-flash")
        primary_deep     = manager_cfg.get("model", primary_quick)

        config = DEFAULT_CONFIG.copy()
        config.update({
            "llm_provider":   primary_provider,
            "quick_think_llm": primary_quick,
            "deep_think_llm":  primary_deep,
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
            # Multi-provider slots — mỗi slot là 1 chain fallback
            # Thứ tự: provider được chọn trước, sau đó tự fallback sang provider kia
            "multi_provider": {
                "analyst": {
                    "chain": [
                        {"provider": analyst_cfg["provider"],    "model": analyst_cfg["model"]},
                        {"provider": researcher_cfg["provider"], "model": researcher_cfg["model"]},
                        {"provider": manager_cfg["provider"],    "model": manager_cfg["model"]},
                    ]
                },
                "researcher": {
                    "chain": [
                        {"provider": researcher_cfg["provider"], "model": researcher_cfg["model"]},
                        {"provider": manager_cfg["provider"],    "model": manager_cfg["model"]},
                        {"provider": analyst_cfg["provider"],    "model": analyst_cfg["model"]},
                    ]
                },
                "manager": {
                    "chain": [
                        {"provider": manager_cfg["provider"],    "model": manager_cfg["model"]},
                        {"provider": analyst_cfg["provider"],    "model": analyst_cfg["model"]},
                        {"provider": researcher_cfg["provider"], "model": researcher_cfg["model"]},
                    ]
                },
            },
        })

        ta = TradingAgentsGraph(
            selected_analysts=analysts,
            debug=False,
            config=config,
            callbacks=[tracker],
        )

        _set_step(f"Đang phân tích {ticker} ({', '.join(analysts)})...")
        _, decision = ta.propagate(ticker, date)

        # ── Parse kết quả từ decision string ─────────────────────────────────
        result = {
            "ticker": ticker,
            "date": date,
            "analysts": analysts,
            "raw": decision,
            "rating": None,
            "entry_price": None,
            "stop_loss": None,
            "price_target": None,
            "position_sizing": None,
            "executive_summary": None,
            "investment_thesis": None,
            "time_horizon": None,
        }

        import re

        def _extract(pattern, text, cast=None):
            m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if not m:
                return None
            val = m.group(1).strip()
            if cast:
                try:
                    return cast(val.replace(",", "").replace("$", ""))
                except Exception:
                    return None
            return val

        result["rating"]            = _extract(r"\*\*Rating\*\*[:\s]+([^\n]+)", decision)
        result["entry_price"]       = _extract(r"\*\*Entry Price\*\*[:\s]+\$?([\d,\.]+)", decision, float)
        result["stop_loss"]         = _extract(r"\*\*Stop Loss\*\*[:\s]+\$?([\d,\.]+)", decision, float)
        result["price_target"]      = _extract(r"\*\*Price Target\*\*[:\s]+\$?([\d,\.]+)", decision, float)
        result["position_sizing"]   = _extract(r"\*\*Position Sizing\*\*[:\s]+([^\n]+)", decision)
        result["time_horizon"]      = _extract(r"\*\*Time Horizon\*\*[:\s]+([^\n]+)", decision)
        result["executive_summary"] = _extract(r"\*\*Executive Summary\*\*[:\s]+(.+?)(?=\n\*\*|\Z)", decision)
        result["investment_thesis"] = _extract(r"\*\*Investment Thesis\*\*[:\s]+(.+?)(?=\n\*\*|\Z)", decision)

        # Fallback: tìm FINAL TRANSACTION PROPOSAL nếu không có Rating
        if not result["rating"]:
            m2 = re.search(r"FINAL TRANSACTION PROPOSAL.*?\*\*(BUY|SELL|HOLD)\*\*", decision, re.IGNORECASE)
            if m2:
                result["rating"] = m2.group(1).capitalize()

        with _ta_lock:
            _ta_state["last_result"] = result
            _ta_state["running"] = False
            _ta_state["step"] = "Hoàn thành"
            _ta_state["elapsed_sec"] = int(_time.time() - _ta_state["start_ts"])

        logger.info("[TradingAgents] Analysis done: %s %s → %s", ticker, date, result.get("rating"))

    except Exception as e:
        logger.error("[TradingAgents] Error: %s", e, exc_info=True)
        with _ta_lock:
            _ta_state["last_result"] = {"error": str(e)[:400], "ticker": ticker, "date": date}
            _ta_state["running"] = False
            _ta_state["step"] = f"Lỗi: {str(e)[:200]}"


@app.route("/api/ta/analyze", methods=["POST"])
@require_auth
def api_ta_analyze():
    """Kick off TradingAgents analysis for a ticker."""
    data = request.get_json() or {}
    ticker   = data.get("ticker", "BTC-USD").strip().upper()
    date     = data.get("date", "") or __import__("datetime").date.today().isoformat()
    analysts = data.get("analysts", ["market", "news", "social"])
    if not isinstance(analysts, list) or not analysts:
        analysts = ["market", "news", "social"]
    valid_analysts = {"market", "news", "social", "fundamentals"}
    analysts = [a for a in analysts if a in valid_analysts] or ["market", "news"]

    # Multi-provider slots — fallback về defaults nếu không truyền
    raw_mp = data.get("multi_provider") or {}
    _default_slots = {
        "analyst":    {"provider": "google", "model": "gemini-3.6-flash"},
        "researcher": {"provider": "google", "model": "gemini-3.6-flash"},
        "manager":    {"provider": "google", "model": "gemini-3.6-flash"},
    }
    multi_provider = {}
    for slot, default in _default_slots.items():
        slot_data = raw_mp.get(slot) or {}
        multi_provider[slot] = {
            "provider": (slot_data.get("provider") or default["provider"]).strip(),
            "model":    (slot_data.get("model")    or default["model"]).strip(),
        }

    with _ta_lock:
        if _ta_state["running"]:
            return jsonify({"ok": False, "msg": "Đang chạy phân tích, vui lòng đợi..."})
        _ta_state["running"] = True
        _ta_state["step"] = "Đang khởi động..."
        _ta_state["elapsed_sec"] = 0
        _ta_state["start_ts"] = _time.time()
        _ta_state["agent_log"] = []

    t = _threading.Thread(
        target=_ta_run_analysis,
        args=(ticker, date, analysts, multi_provider),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "msg": f"Bắt đầu phân tích {ticker} ({date})..."})


@app.route("/api/ta/status", methods=["GET"])
@require_auth
def api_ta_status():
    """Return current TradingAgents run status + last result."""
    with _ta_lock:
        return jsonify({
            "running":     _ta_state["running"],
            "step":        _ta_state["step"],
            "elapsed_sec": _ta_state["elapsed_sec"],
            "last_result": _ta_state["last_result"],
            "agent_log":   _ta_state["agent_log"][-8:],  # 8 bước gần nhất
        })
