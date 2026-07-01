# ============================================================
# DAILY HTML REPORT GENERATOR
# Sinh report tổng hợp lời lỗ theo ngày, gửi qua Telegram
# ============================================================
import os
import json
import logging
import requests
from datetime import datetime
from typing import List

logger = logging.getLogger(__name__)

REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
os.makedirs(REPORT_DIR, exist_ok=True)


def generate_html_report(trade_log: List[dict],
                          balance: float,
                          open_positions: List[dict] = None,
                          split_positions: dict = None) -> str:
    """
    Sinh HTML report từ trade_log.
    Trả về path tới file HTML đã lưu.
    """
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")

    # Lọc lệnh đã đóng hôm nay
    today_closed = [
        t for t in trade_log
        if t.get("status") == "CLOSED"
        and t.get("time", "").startswith(date_str)
        and abs(t.get("pnl_usdt", 0)) > 0.001
    ]

    # Tổng hợp all time
    all_closed = [
        t for t in trade_log
        if t.get("status") == "CLOSED"
        and abs(t.get("pnl_usdt", 0)) > 0.001
    ]

    # Stats hôm nay
    today_pnl    = sum(t.get("pnl_usdt", 0) for t in today_closed)
    today_wins   = sum(1 for t in today_closed if t.get("pnl_usdt", 0) > 0)
    today_losses = len(today_closed) - today_wins
    today_wr     = today_wins / len(today_closed) * 100 if today_closed else 0

    # Stats all time
    total_pnl    = sum(t.get("pnl_usdt", 0) for t in all_closed)
    total_wins   = sum(1 for t in all_closed if t.get("pnl_usdt", 0) > 0)
    total_losses = len(all_closed) - total_wins
    total_wr     = total_wins / len(all_closed) * 100 if all_closed else 0

    # Best / Worst trade
    best_trade  = max(all_closed, key=lambda t: t.get("pnl_usdt", 0)) if all_closed else None
    worst_trade = min(all_closed, key=lambda t: t.get("pnl_usdt", 0)) if all_closed else None

    # Unrealized
    unrealized = 0.0
    if open_positions:
        unrealized = sum(p.get("_pnl", 0) for p in open_positions)

    # --- Build HTML ---
    pnl_color_today = "#00c853" if today_pnl >= 0 else "#ff1744"
    pnl_color_total = "#00c853" if total_pnl >= 0 else "#ff1744"

    # Trade rows
    today_rows = ""
    for i, t in enumerate(sorted(today_closed, key=lambda x: x.get("time", ""), reverse=True), 1):
        pnl = t.get("pnl_usdt", 0)
        pct = t.get("pnl_pct", 0)
        color = "#00c853" if pnl > 0 else "#ff1744"
        icon = "&#10004;" if pnl > 0 else "&#10008;"
        sym = t.get("symbol", "").replace("USDT", "")
        today_rows += f"""
        <tr>
            <td>{i}</td>
            <td><b>{sym}</b></td>
            <td>{t.get('side', '')}</td>
            <td>${t.get('entry', 0):.4f}</td>
            <td>${t.get('close', 0):.4f}</td>
            <td style="color:{color}"><b>${pnl:+.2f}</b></td>
            <td style="color:{color}">{pct:+.2f}%</td>
            <td>{t.get('time', '')[11:16]}</td>
        </tr>"""

    # All time rows (last 20)
    all_rows = ""
    recent_all = sorted(all_closed, key=lambda x: x.get("time", ""), reverse=True)[:20]
    for i, t in enumerate(recent_all, 1):
        pnl = t.get("pnl_usdt", 0)
        pct = t.get("pnl_pct", 0)
        color = "#00c853" if pnl > 0 else "#ff1744"
        sym = t.get("symbol", "").replace("USDT", "")
        all_rows += f"""
        <tr>
            <td>{i}</td>
            <td><b>{sym}</b></td>
            <td>{t.get('side', '')}</td>
            <td>${t.get('entry', 0):.4f}</td>
            <td>${t.get('close', 0):.4f}</td>
            <td style="color:{color}"><b>${pnl:+.2f}</b></td>
            <td style="color:{color}">{pct:+.2f}%</td>
            <td>{t.get('time', '')[:16]}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trading Report - {date_str}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
        background: #1a1a2e;
        color: #e0e0e0;
        padding: 20px;
        line-height: 1.6;
    }}
    .container {{ max-width: 800px; margin: 0 auto; }}
    .header {{
        text-align: center;
        padding: 20px;
        background: linear-gradient(135deg, #16213e, #0f3460);
        border-radius: 12px;
        margin-bottom: 20px;
    }}
    .header h1 {{ color: #00d4ff; font-size: 24px; }}
    .header .date {{ color: #888; margin-top: 5px; }}
    .stats-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 12px;
        margin-bottom: 20px;
    }}
    .stat-card {{
        background: #16213e;
        border-radius: 10px;
        padding: 15px;
        text-align: center;
        border: 1px solid #0f3460;
    }}
    .stat-card .value {{ font-size: 22px; font-weight: bold; margin: 5px 0; }}
    .stat-card .label {{ font-size: 12px; color: #888; text-transform: uppercase; }}
    .section {{
        background: #16213e;
        border-radius: 10px;
        padding: 20px;
        margin-bottom: 20px;
        border: 1px solid #0f3460;
    }}
    .section h2 {{
        color: #00d4ff;
        margin-bottom: 15px;
        font-size: 16px;
        border-bottom: 1px solid #0f3460;
        padding-bottom: 8px;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
    }}
    th {{
        background: #0f3460;
        padding: 8px;
        text-align: left;
        color: #00d4ff;
    }}
    td {{
        padding: 6px 8px;
        border-bottom: 1px solid #0f3460;
    }}
    tr:hover {{ background: #1a2744; }}
    .profit {{ color: #00c853; }}
    .loss {{ color: #ff1744; }}
    .footer {{
        text-align: center;
        color: #555;
        font-size: 11px;
        margin-top: 20px;
    }}
</style>
</head>
<body>
<div class="container">

<div class="header">
    <h1>&#128202; TRADING REPORT</h1>
    <div class="date">{date_str} &bull; Generated at {time_str}</div>
</div>

<!-- TODAY STATS -->
<div class="stats-grid">
    <div class="stat-card">
        <div class="label">Balance</div>
        <div class="value">${balance:,.2f}</div>
    </div>
    <div class="stat-card">
        <div class="label">Today PnL</div>
        <div class="value" style="color:{pnl_color_today}">${today_pnl:+.2f}</div>
    </div>
    <div class="stat-card">
        <div class="label">Total PnL</div>
        <div class="value" style="color:{pnl_color_total}">${total_pnl:+.2f}</div>
    </div>
    <div class="stat-card">
        <div class="label">Unrealized</div>
        <div class="value" style="color:{'#00c853' if unrealized >= 0 else '#ff1744'}">${unrealized:+.2f}</div>
    </div>
</div>

<div class="stats-grid">
    <div class="stat-card">
        <div class="label">Today Trades</div>
        <div class="value">{len(today_closed)}</div>
    </div>
    <div class="stat-card">
        <div class="label">Today Win Rate</div>
        <div class="value">{today_wr:.0f}%</div>
    </div>
    <div class="stat-card">
        <div class="label">Total Trades</div>
        <div class="value">{len(all_closed)}</div>
    </div>
    <div class="stat-card">
        <div class="label">Total Win Rate</div>
        <div class="value">{total_wr:.0f}%</div>
    </div>
</div>

<!-- TODAY TRADES -->
<div class="section">
    <h2>&#128197; Today's Trades ({date_str})</h2>
    {"<p style='color:#888'>No trades today</p>" if not today_closed else f'''
    <table>
        <tr><th>#</th><th>Coin</th><th>Side</th><th>Entry</th><th>Close</th><th>PnL</th><th>%</th><th>Time</th></tr>
        {today_rows}
    </table>
    <p style="margin-top:10px; color:#888">
        &#10004; {today_wins}W &nbsp; &#10008; {today_losses}L &nbsp; WR: {today_wr:.0f}% &nbsp;
        PnL: <span style="color:{pnl_color_today}"><b>${today_pnl:+.2f}</b></span>
    </p>'''}
</div>

<!-- ALL TIME TRADES (recent 20) -->
<div class="section">
    <h2>&#128203; All Time (Recent 20 Trades)</h2>
    {"<p style='color:#888'>No trades yet</p>" if not all_closed else f'''
    <table>
        <tr><th>#</th><th>Coin</th><th>Side</th><th>Entry</th><th>Close</th><th>PnL</th><th>%</th><th>Time</th></tr>
        {all_rows}
    </table>
    <p style="margin-top:10px; color:#888">
        &#10004; {total_wins}W &nbsp; &#10008; {total_losses}L &nbsp; WR: {total_wr:.0f}% &nbsp;
        Total: <span style="color:{pnl_color_total}"><b>${total_pnl:+.2f}</b></span>
    </p>'''}
</div>

<!-- BEST / WORST -->
<div class="section">
    <h2>&#127942; Best & Worst</h2>
    <table>
        <tr><th></th><th>Coin</th><th>Side</th><th>PnL</th><th>%</th><th>Time</th></tr>
        {"" if not best_trade else f'''
        <tr>
            <td>&#127942; Best</td>
            <td><b>{best_trade.get("symbol","").replace("USDT","")}</b></td>
            <td>{best_trade.get("side","")}</td>
            <td class="profit"><b>${best_trade.get("pnl_usdt",0):+.2f}</b></td>
            <td class="profit">{best_trade.get("pnl_pct",0):+.2f}%</td>
            <td>{best_trade.get("time","")[:16]}</td>
        </tr>'''}
        {"" if not worst_trade else f'''
        <tr>
            <td>&#128128; Worst</td>
            <td><b>{worst_trade.get("symbol","").replace("USDT","")}</b></td>
            <td>{worst_trade.get("side","")}</td>
            <td class="loss"><b>${worst_trade.get("pnl_usdt",0):+.2f}</b></td>
            <td class="loss">{worst_trade.get("pnl_pct",0):+.2f}%</td>
            <td>{worst_trade.get("time","")[:16]}</td>
        </tr>'''}
    </table>
</div>

<div class="footer">
    Multi-Coin Trading Bot &bull; Liquidation Strategy &bull; Report auto-generated
</div>

</div>
</body>
</html>"""

    # Save to file
    filename = f"report_{date_str}_{now.strftime('%H%M%S')}.html"
    filepath = os.path.join(REPORT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info(f"Report saved: {filepath}")
    return filepath


def send_report_telegram(filepath: str, bot_token: str, chat_id: str,
                          caption: str = None):
    """Gửi file HTML report qua Telegram."""
    if not caption:
        caption = f"📊 Daily Trading Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(url, data={
                "chat_id": chat_id,
                "caption": caption,
                "parse_mode": "HTML",
            }, files={"document": f}, timeout=30)
        resp.raise_for_status()
        logger.info(f"Report sent via Telegram: {filepath}")
        return True
    except Exception as e:
        logger.error(f"Send report failed: {e}")
        return False


def generate_and_send(trade_log: List[dict], balance: float,
                       open_positions: List[dict] = None,
                       split_positions: dict = None,
                       bot_token: str = None, chat_id: str = None):
    """
    One-call: sinh report + gửi Telegram.
    Dùng trong bot.py khi dừng bot.
    """
    filepath = generate_html_report(trade_log, balance, open_positions, split_positions)

    if bot_token and chat_id:
        # Tổng PnL hôm nay
        date_str = datetime.now().strftime("%Y-%m-%d")
        today_closed = [
            t for t in trade_log
            if t.get("status") == "CLOSED"
            and t.get("time", "").startswith(date_str)
            and abs(t.get("pnl_usdt", 0)) > 0.001
        ]
        today_pnl = sum(t.get("pnl_usdt", 0) for t in today_closed)
        total_pnl = sum(
            t.get("pnl_usdt", 0) for t in trade_log
            if t.get("status") == "CLOSED" and abs(t.get("pnl_usdt", 0)) > 0.001
        )
        icon = "📈" if today_pnl >= 0 else "📉"
        caption = (
            f"{icon} <b>Daily Report — {date_str}</b>\n"
            f"Today: <b>${today_pnl:+.2f}</b> | Total: <b>${total_pnl:+.2f}</b>\n"
            f"Balance: <b>${balance:,.2f}</b>"
        )
        send_report_telegram(filepath, bot_token, chat_id, caption)

    return filepath
