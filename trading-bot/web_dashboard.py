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
_ledger = None
_protected_pending_file_lock = threading.Lock()


def _normalize_protected_pending_symbol(raw: str) -> str:
    """Normalize BASE/BASEUSDT/quarterly futures input or raise ValueError."""
    import re
    symbol = str(raw or "").strip().upper()
    if not symbol:
        raise ValueError("Thiếu symbol")
    if "_" not in symbol and not symbol.endswith("USDT"):
        symbol += "USDT"
    if not re.fullmatch(r"[A-Z0-9]{1,20}USDT(?:_[0-9]{6})?", symbol):
        raise ValueError("Symbol không hợp lệ")
    return symbol


def _save_protected_pending_coins_unlocked(coins: list) -> bool:
    """Persist while caller owns ``_protected_pending_file_lock``."""
    import os
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "protected_pending_order_coins.json",
    )
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(coins, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as exc:
        logger.error(f"[OrderProtect] Save failed: {exc}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def _save_protected_pending_coins(coins: list) -> bool:
    """Atomically persist protected symbols beside bot.py/config.py."""
    with _protected_pending_file_lock:
        return _save_protected_pending_coins_unlocked(coins)


def _web_is_protected_pending_symbol(symbol: str) -> bool:
    symbol = str(symbol or "").strip().upper()
    with _lock:
        protected = tuple(_state.get("protected_pending_order_coins", []))
    return any(
        symbol == base or symbol.startswith(f"{base}_")
        for base in protected
    )


def _web_order_is_reduce_only(order: dict) -> bool:
    value = order.get("reduceOnly", False)
    return value.lower() == "true" if isinstance(value, str) else bool(value)


def _web_guarded_symbol_cancel(symbol: str, close_cleanup: bool = False) -> bool:
    """Cancel entries only for explicit bulk actions; close removes exits only."""
    with _lock:
        protected = _web_is_protected_pending_symbol(symbol)
        if not close_cleanup:
            if protected:
                return False
            _exchange.cancel_all_orders(symbol)
            return True

        # A position close must never delete a manual pending entry, protected
        # or otherwise. Remove only stale reduce-only regular/algo exits.
        for order in _exchange._get(
            "/fapi/v1/openOrders", {"symbol": symbol}, signed=True
        ):
            if _web_order_is_reduce_only(order):
                _exchange._delete(
                    "/fapi/v1/order",
                    {"symbol": symbol, "orderId": order.get("orderId")},
                )
        algo_orders = _exchange._get("/fapi/v1/openAlgoOrders", signed=True)
        if isinstance(algo_orders, list):
            for order in algo_orders:
                if (
                    order.get("symbol") == symbol
                    and _web_order_is_reduce_only(order)
                    and order.get("algoId")
                ):
                    _exchange._delete(
                        "/fapi/v1/algoOrder",
                        {"symbol": symbol, "algoId": order.get("algoId")},
                    )
        return False


def _web_append_trade(trade: dict):
    """Append trade vào _state trade_log và save ngay — tránh mất khi restart."""
    from trade_history import save_history
    if _state is None or _lock is None:
        return
    with _lock:
        _state.setdefault("trade_log", []).append(trade)
        snapshot = list(_state["trade_log"])
    save_history(snapshot)


def _legacy_cycle(trade: dict) -> dict:
    """Adapt one legacy CLOSED row without mixing it into Binance data."""
    when = trade.get("close_time") or trade.get("time", "")
    try:
        closed_ms = int(datetime.strptime(when, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
    except Exception:
        closed_ms = 0
    pnl = float(trade.get("pnl_usdt", 0) or 0)
    return {
        "cycle_id": f"legacy:{trade.get('symbol', '')}:{when}",
        "symbol": trade.get("symbol", ""),
        "position_side": "BOTH",
        "side": trade.get("side", ""),
        "opened_at": trade.get("time", ""),
        "opened_at_ms": closed_ms,
        "closed_at": when,
        "closed_at_ms": closed_ms,
        "entry_price": float(trade.get("entry", 0) or 0),
        "close_price": float(trade.get("close", 0) or 0),
        "gross_realized_pnl": pnl,
        "commission": 0.0,
        "funding": 0.0,
        "net_pnl": pnl,
        "commission_complete": True,
        "funding_complete": True,
        "net_complete": True,
        "complete": False,
        "boundary_start": True,
    }


def _cycle_financial_event(cycle: dict) -> dict:
    """Legacy-only event adapter; Binance snapshots provide native events."""
    net = cycle.get("net_pnl")
    return {
        "event_id": f"legacy-event:{cycle.get('cycle_id', '')}",
        "event_type": "legacy_close",
        "time_ms": int(cycle.get("closed_at_ms", 0) or 0),
        "time": cycle.get("closed_at", ""),
        "symbol": cycle.get("symbol", ""),
        "gross": float(cycle.get("gross_realized_pnl", 0) or 0),
        "commission": float(cycle.get("commission", 0) or 0),
        "funding": float(cycle.get("funding", 0) or 0),
        "net": float(net or 0) if net is not None else None,
        "commission_complete": bool(cycle.get("commission_complete", True)),
        "net_complete": bool(cycle.get("net_complete", net is not None)),
        "allocation_status": "legacy",
    }


def _financial_snapshot(tlog: list, state_snapshot: dict) -> dict:
    """Return one unmixed financial dataset; this function never performs I/O."""
    cached = None
    if _ledger is not None:
        cached = _ledger.snapshot()
        if cached.get("ready"):
            return cached
    legacy_cycles = [
        _legacy_cycle(item) for item in tlog if item.get("status") == "CLOSED"
    ]
    legacy_events = [_cycle_financial_event(cycle) for cycle in legacy_cycles]
    legacy_net = sum(float(event["net"] or 0) for event in legacy_events)
    return {
        "ready": False,
        "source": "legacy",
        "stale": True,
        "syncing": bool(cached and cached.get("syncing")),
        "errors": list(cached.get("errors", [])) if cached else ["Binance ledger not started"],
        "synced_at": "",
        "window_start": "",
        "window_start_ms": 0,
        "window_clipped": False,
        "cycles": legacy_cycles,
        "financial_events": legacy_events,
        "transfers": [],
        "aggregate": {
            "gross": legacy_net,
            "commission": 0.0,
            "funding": 0.0,
            "net": legacy_net,
            "transfer": 0.0,
            "commission_complete": True,
            "net_complete": True,
            "incomplete_event_count": 0,
        },
        "non_usdt_commission_assets": [],
        "ambiguous_funding_event_ids": [],
        "financial_warnings": ["Legacy rows are excluded from canonical trade outcomes"],
        "account": {
            # Explicitly legacy/available only; the UI warning prevents this
            # fallback from being mistaken for canonical wallet equity.
            "wallet_balance": float(state_snapshot.get("balance", 0) or 0),
            "margin_balance": float(state_snapshot.get("balance", 0) or 0),
            "available_balance": float(state_snapshot.get("balance", 0) or 0),
            "positions": [],
        },
    }


def _financial_events(financial: dict) -> list:
    events = financial.get("financial_events")
    if isinstance(events, list):
        return sorted(
            [dict(event) for event in events],
            key=lambda event: (int(event.get("time_ms", 0) or 0), str(event.get("event_id", ""))),
        )
    return [_cycle_financial_event(cycle) for cycle in _closed_cycles(financial)]


def _closed_cycles(financial: dict) -> list:
    return sorted(
        [cycle for cycle in financial.get("cycles", []) if cycle.get("closed_at_ms")],
        key=lambda cycle: int(cycle.get("closed_at_ms", 0) or 0),
    )


def _complete_closed_cycles(financial: dict) -> list:
    """Only complete, financially classifiable cycles count as outcomes."""
    return [
        cycle for cycle in _closed_cycles(financial)
        if bool(cycle.get("complete", False))
        and bool(cycle.get("net_complete", cycle.get("net_pnl") is not None))
        and cycle.get("net_pnl") is not None
    ]


def _event_totals(events: list) -> dict:
    gross = sum(float(event.get("gross", 0) or 0) for event in events)
    commission = sum(float(event.get("commission", 0) or 0) for event in events)
    funding = sum(float(event.get("funding", 0) or 0) for event in events)
    net_complete = all(bool(event.get("net_complete", True)) for event in events)
    net = sum(float(event.get("net", 0) or 0) for event in events) if net_complete else None
    return {
        "gross": gross,
        "commission": commission,
        "funding": funding,
        "period_net_pnl": net,
        "commission_complete": all(bool(event.get("commission_complete", True)) for event in events),
        "net_complete": net_complete,
        "incomplete_event_count": sum(1 for event in events if not bool(event.get("net_complete", True))),
    }


def _transfer_totals(transfers: list) -> dict:
    unsupported_assets = sorted({
        str(event.get("asset") or "UNKNOWN").upper()
        for event in transfers
        if abs(float(event.get("amount", 0) or 0)) > 0
        and str(event.get("asset") or "UNKNOWN").upper() != "USDT"
    })
    transfer_usdt = sum(
        float(event.get("amount", 0) or 0) for event in transfers
        if str(event.get("asset") or "UNKNOWN").upper() == "USDT"
    )
    return {
        "transfer": transfer_usdt if not unsupported_assets else None,
        "transfer_excluding_unconverted": transfer_usdt,
        "transfer_complete": not unsupported_assets,
        "unsupported_transfer_assets": unsupported_assets,
    }


def _financial_totals(financial: dict, events: list = None, cycles: list = None) -> dict:
    selected_events = _financial_events(financial) if events is None else list(events)
    all_closed = _closed_cycles(financial)
    outcomes = _complete_closed_cycles(financial) if cycles is None else list(cycles)
    totals = _event_totals(selected_events)
    transfer_totals = _transfer_totals(list(financial.get("transfers", [])))
    wins = sum(1 for cycle in outcomes if float(cycle.get("net_pnl", 0)) > 0)
    losses = sum(1 for cycle in outcomes if float(cycle.get("net_pnl", 0)) < 0)
    totals.update({
        **transfer_totals,
        "closed_cycles": len(outcomes),
        "incomplete_cycles": len(all_closed) - len(_complete_closed_cycles(financial)),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(outcomes) * 100 if outcomes else 0.0,
    })
    return totals

# ── Auth helpers ──────────────────────────────────────────────────────────────
def require_auth(f):
    """
    Decorator bảo vệ route — redirect về /login nếu chưa đăng nhập.

    Trước đây hàm này TỰ set session["authenticated"] = True rồi cho qua
    ("auto-authenticate"), nên dashboard mở cổng 5555 công khai không có
    mật khẩu: ai biết IP:port là vào vào lệnh / đổi config / tắt bot được.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            # Request từ JS (fetch) → trả 401 JSON để frontend tự redirect,
            # không trả HTML login vào chỗ đang chờ JSON.
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized",
                                "login_required": True}), 401
            return redirect(url_for("login"))
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

# ═══════════════════════════════════════════════════════════════
# TIN TỨC THỊ TRƯỜNG CRYPTO + MACRO (Fed, lãi suất, CPI...)
# ═══════════════════════════════════════════════════════════════
# Dùng RSS công khai — KHÔNG cần API key.
# Parse bằng xml.etree (stdlib) để không thêm dependency.
# Đã test 07/09/2026: coindesk 25 item, cointelegraph 30, decrypt 37, newsbtc OK.
NEWS_SOURCES = [
    ("CoinDesk",       "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph",  "https://cointelegraph.com/rss"),
    ("Decrypt",        "https://decrypt.co/feed"),
    ("NewsBTC",        "https://www.newsbtc.com/feed/"),
    # Nguồn macro kinh tế — đã test OK trên VPS (ForexLive/Reuters bị block CloudFront)
    ("Yahoo Finance",  "https://finance.yahoo.com/news/rssindex"),
    ("MarketWatch",    "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"),
    ("CNBC Markets",   "https://www.cnbc.com/id/100727362/device/rss/rss.html"),
]

# Từ khoá gắn nhãn. Ưu tiên theo thứ tự trong list (khớp trước thắng).
NEWS_TAG_RULES = [
    ("MACRO", [
        "fed", "federal reserve", "fomc", "powell", "jerome powell",
        "interest rate", "rate cut", "rate hike", "basis point",
        "inflation", "cpi", "ppi", "pce", "jobs report", "nonfarm",
        "unemployment", "treasury", "yield", "recession", "gdp",
        "monetary policy", "quantitative", "dollar index", "dxy",
        "central bank", "ecb", "boj", "tariff", "stimulus",
    ]),
    ("QUY ĐỊNH", [
        "sec ", "sec's", "cftc", "regulat", "lawsuit", "court", "judge",
        "ban ", "banned", "compliance", "mica", "legislation", "senate",
        "congress", "bill ", "law ", "sanction", "enforcement", "settle",
        "approve", "approval", "license", "framework", "treasury dept",
    ]),
    ("ETF", ["etf", "spot etf", "inflow", "outflow", "blackrock", "ishares",
             "grayscale", "fidelity", "ark invest", "aum"]),
    ("THANH LÝ", ["liquidat", "crash", "plunge", "plummet", "tumble", "selloff",
                  "sell-off", "capitulat", "flash crash", "wipeout", "dump"]),
    ("TĂNG MẠNH", ["surge", "soar", "rally", "all-time high", "ath",
                   "record high", "breakout", "skyrocket", "jump"]),
    ("HACK", ["hack", "exploit", "breach", "stolen", "drain", "rug pull",
              "scam", "phishing", "vulnerability"]),
]

# ── IMPACT SCORING ───────────────────────────────────────────
# HIGH  = sự kiện di chuyển thị trường ngay lập tức (CPI, FOMC, NFP...)
# MEDIUM = quan trọng nhưng không ngay lập tức
# LOW   = mặc định
_IMPACT_HIGH = [
    "cpi", "consumer price index",
    "fomc", "fed decision", "rate decision", "rate cut", "rate hike",
    "nonfarm", "nfp", "jobs report", "unemployment rate",
    "pce", "core pce",
    "gdp", "gross domestic product",
    "ppi", "producer price",
    "powell speech", "fed chair", "jerome powell",
    "ecb decision", "boj decision", "bank of england",
    "emergency", "flash crash", "black swan",
    "liquidat", "wipeout", "capitulat",
    "hack", "exploit", "stolen", "drained",
    "rug pull", "scam alert",
    "sec charges", "sec sues", "sec approves",
    "etf approved", "spot etf",
]

_IMPACT_MEDIUM = [
    "inflation", "interest rate", "monetary policy", "stimulus",
    "treasury yield", "dollar index", "dxy", "tariff",
    "retail sales", "ism ", "pmi ", "consumer confidence",
    "earnings", "revenue", "profit", "quarterly",
    "regulation", "congress", "legislation", "senate bill",
    "whale", "large transfer", "exchange outflow", "exchange inflow",
    "all-time high", "ath", "breakout", "record high",
    "etf", "grayscale", "blackrock", "fidelity",
]


def _news_impact(title: str, summary: str = "") -> str:
    """Tính mức impact: HIGH / MEDIUM / LOW dựa trên từ khoá."""
    blob = (title + " " + summary).lower()
    for kw in _IMPACT_HIGH:
        if kw in blob:
            return "HIGH"
    for kw in _IMPACT_MEDIUM:
        if kw in blob:
            return "MEDIUM"
    return "LOW"


# Coin phổ biến — để lọc theo coin
NEWS_COIN_WORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether", "eth"],
    "SOL": ["solana", "sol"],
    "XRP": ["xrp", "ripple"],
    "BNB": ["bnb", "binance coin"],
    "DOGE": ["dogecoin", "doge"],
}

_news_cache = {"items": [], "ts": 0, "errors": []}
_news_lock = threading.Lock()
_NEWS_TTL = 300          # 5 phút — tin tức không cần realtime
_NEWS_MAX_ITEMS = 80     # tăng lên vì thêm nguồn macro


def _news_parse_date(s):
    """Parse pubDate RSS -> timestamp. Trả 0 nếu không parse được."""
    if not s:
        return 0
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s.strip()).timestamp()
    except Exception:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt).timestamp()
        except Exception:
            continue
    return 0


def _news_tag(title, summary=""):
    """Gắn nhãn theo từ khoá. Trả list nhãn (có thể nhiều)."""
    blob = (title + " " + summary).lower()
    tags = []
    for tag, words in NEWS_TAG_RULES:
        if any(w in blob for w in words):
            tags.append(tag)
    coins = [c for c, words in NEWS_COIN_WORDS.items()
             if any(w in blob for w in words)]
    return tags, coins


def _news_fetch_one(name, url, out, errors):
    """Fetch + parse 1 nguồn RSS. Lỗi 1 nguồn không làm chết các nguồn khác."""
    import xml.etree.ElementTree as ET
    try:
        import requests
        r = requests.get(url, timeout=10, allow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)"})
        if r.status_code != 200:
            errors.append(f"{name}: HTTP {r.status_code}")
            return
        root = ET.fromstring(r.content)
        items = root.findall(".//item") or root.findall(
            ".//{http://www.w3.org/2005/Atom}entry")
        for it in items[:25]:
            title = (it.findtext("title")
                     or it.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            if not title:
                continue
            link = (it.findtext("link") or "").strip()
            if not link:
                le = it.find("{http://www.w3.org/2005/Atom}link")
                if le is not None:
                    link = le.get("href", "")
            desc = (it.findtext("description")
                    or it.findtext("{http://www.w3.org/2005/Atom}summary") or "")
            # bỏ thẻ HTML trong description
            import re as _re
            desc = _re.sub(r"<[^>]+>", "", desc).strip()[:300]
            pub = (it.findtext("pubDate")
                   or it.findtext("published")
                   or it.findtext("{http://www.w3.org/2005/Atom}updated") or "")
            ts = _news_parse_date(pub)
            tags, coins = _news_tag(title, desc)
            impact = _news_impact(title, desc)
            out.append({
                "title": title, "link": link, "source": name,
                "summary": desc, "ts": ts, "tags": tags, "coins": coins,
                "impact": impact,
            })
    except Exception as e:
        errors.append(f"{name}: {type(e).__name__}")


def get_market_news(force=False):
    """Trả tin tức đã cache. Fetch song song 4 nguồn, cache 5 phút."""
    now = time.time()
    with _news_lock:
        fresh = (now - _news_cache["ts"]) < _NEWS_TTL
        if fresh and not force and _news_cache["items"]:
            return _news_cache["items"], _news_cache["ts"], _news_cache["errors"]

    out, errors, threads = [], [], []
    for name, url in NEWS_SOURCES:
        t = threading.Thread(target=_news_fetch_one,
                             args=(name, url, out, errors), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=12)

    # Bỏ tin trùng tiêu đề (nhiều nguồn đưa cùng 1 tin)
    seen, uniq = set(), []
    for it in sorted(out, key=lambda x: x["ts"], reverse=True):
        key = it["title"].lower()[:70]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    uniq = uniq[:_NEWS_MAX_ITEMS]

    with _news_lock:
        # Chỉ ghi đè cache khi lấy được tin — tránh mất tin cũ khi mạng lỗi
        if uniq:
            _news_cache["items"] = uniq
            _news_cache["ts"] = now
        _news_cache["errors"] = errors
        return _news_cache["items"], _news_cache["ts"], errors


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
/* Macro economic calendar */
.macro-cal { border:1px solid #30363d; border-radius:10px; padding:12px; margin-bottom:16px; background:linear-gradient(135deg,rgba(88,166,255,.05),rgba(210,153,34,.04)); }
.macro-cal-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:10px; }
.macro-cal-title { color:#e6edf3; font-size:13px; font-weight:700; }
.macro-cal-status { color:#6e7681; font-size:10px; }
.macro-cal-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(310px,1fr)); gap:8px; }
.macro-event { background:#0d1117; border:1px solid #21262d; border-left:3px solid #d29922; border-radius:8px; padding:10px; min-width:0; }
.macro-event.high { border-left-color:#f85149; box-shadow:inset 0 0 0 1px rgba(248,81,73,.12); }
.macro-event.released { opacity:.82; border-left-color:#6e7681; }
.macro-event-top { display:flex; gap:7px; align-items:flex-start; }
.macro-event-title { color:#e6edf3; font-size:12px; font-weight:700; line-height:1.35; flex:1; }
.macro-badge { display:inline-block; padding:2px 6px; border-radius:4px; font-size:9px; font-weight:800; letter-spacing:.4px; }
.macro-badge.high { color:#f85149; background:rgba(248,81,73,.15); }
.macro-badge.medium { color:#d29922; background:rgba(210,153,34,.15); }
.macro-meta { color:#8b949e; font-size:10px; line-height:1.55; margin-top:5px; }
.macro-values { display:grid; grid-template-columns:repeat(3,1fr); gap:5px; margin-top:8px; }
.macro-value { background:#161b22; border-radius:5px; padding:5px; text-align:center; min-width:0; }
.macro-value span { display:block; color:#6e7681; font-size:8px; text-transform:uppercase; }
.macro-value b { display:block; color:#c9d1d9; font-size:10px; overflow-wrap:anywhere; margin-top:2px; }
.macro-scenarios { margin-top:8px; display:flex; flex-direction:column; gap:4px; }
.macro-scenario { border-radius:5px; padding:6px 7px; background:#161b22; font-size:9.5px; color:#8b949e; line-height:1.4; }
.macro-scenario .bull { color:#3fb950; font-weight:800; }
.macro-scenario .bear { color:#f85149; font-weight:800; }
.macro-scenario .mixed { color:#d29922; font-weight:800; }
.macro-source { color:#58a6ff; text-decoration:none; }
.macro-source:hover { text-decoration:underline; }
.macro-more { width:100%; margin-top:8px; padding:5px; border:1px solid #30363d; border-radius:6px; background:#0d1117; color:#8b949e; cursor:pointer; font-family:inherit; font-size:10px; }
.rss-news-head { display:flex;align-items:center;gap:8px;margin:4px 0 10px;color:#8b949e;font-size:11px;font-weight:700; }
@media (max-width:768px) { .macro-cal-grid{grid-template-columns:1fr}.macro-cal{padding:9px}.macro-event{padding:9px} }
/* Tin tức thị trường */
.news-filters { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
.news-chip { padding:3px 11px; border-radius:20px; font-size:11px; font-weight:600; cursor:pointer;
             border:1px solid #30363d; background:#0d1117; color:#8b949e; transition:all .2s;
             user-select:none; }
.news-chip:hover { border-color:#58a6ff; color:#58a6ff; }
.news-chip.active { background:#1f3a5a; border-color:#58a6ff; color:#58a6ff; }
.news-chip.impact-chip { border-color:rgba(248,81,73,.5); color:#f85149; }
.news-chip.impact-chip.active { background:rgba(248,81,73,.15); border-color:#f85149; color:#f85149; }
.news-list { display:flex; flex-direction:column; gap:1px; }
.news-item { display:flex; gap:10px; align-items:flex-start; padding:8px 10px; border-radius:6px;
             border-left:2px solid #21262d; background:#0d1117; transition:background .15s; }
.news-item:hover { background:#161b22; }
.news-item.macro  { border-left-color:#d29922; background:rgba(210,153,34,.05); }
.news-item.danger { border-left-color:#f85149; background:rgba(248,81,73,.05); }
/* HIGH IMPACT — nổi bật, có pulse border */
.news-item.high-impact {
    border-left: 3px solid #f85149;
    background: rgba(248,81,73,.08);
    box-shadow: inset 0 0 0 1px rgba(248,81,73,.2);
    animation: newsHighPulse 2.5s ease-in-out infinite;
}
.news-item.high-impact .news-title { color:#e6edf3; font-weight:600; }
@keyframes newsHighPulse {
    0%,100% { box-shadow: inset 0 0 0 1px rgba(248,81,73,.2); }
    50%      { box-shadow: inset 0 0 0 1px rgba(248,81,73,.55), 0 0 8px rgba(248,81,73,.15); }
}
/* HIGH IMPACT badge */
.news-impact-badge {
    display:inline-flex; align-items:center; gap:3px;
    font-size:9px; font-weight:800; padding:2px 7px;
    border-radius:4px; letter-spacing:.6px; text-transform:uppercase;
    flex-shrink:0;
}
.news-impact-badge.HIGH   { background:rgba(248,81,73,.2); color:#f85149;
                             border:1px solid rgba(248,81,73,.4); }
.news-impact-badge.MEDIUM { background:rgba(210,153,34,.15); color:#d29922;
                             border:1px solid rgba(210,153,34,.3); }
/* Pulse dot cho HIGH impact */
.impact-dot {
    width:6px; height:6px; border-radius:50%; background:#f85149;
    animation: impactDotPulse 1.2s ease-in-out infinite;
    flex-shrink:0;
}
@keyframes impactDotPulse {
    0%,100% { opacity:1; transform:scale(1); }
    50%      { opacity:.4; transform:scale(1.4); }
}
.news-time { font-size:11px; color:#484f58; min-width:44px; text-align:right; flex-shrink:0;
             padding-top:2px; font-variant-numeric:tabular-nums; }
.news-body { flex:1; min-width:0; }
.news-title { font-size:12.5px; color:#c9d1d9; text-decoration:none; line-height:1.45;
              display:block; }
.news-title:hover { color:#58a6ff; text-decoration:underline; }
.news-meta { display:flex; flex-wrap:wrap; gap:5px; align-items:center; margin-top:4px; }
.news-src { font-size:10px; color:#6e7681; }
.news-tag { font-size:9.5px; font-weight:700; padding:1px 6px; border-radius:4px;
            letter-spacing:.4px; }
.news-tag.MACRO   { background:rgba(210,153,34,.18); color:#d29922; }
.news-tag.REG     { background:rgba(88,166,255,.15); color:#58a6ff; }
.news-tag.ETF     { background:rgba(163,113,247,.15); color:#a371f7; }
.news-tag.DUMP    { background:rgba(248,81,73,.15); color:#f85149; }
.news-tag.PUMP    { background:rgba(63,185,80,.15); color:#3fb950; }
.news-tag.HACK    { background:rgba(219,109,40,.18); color:#db6d28; }
.news-tag.COIN    { background:#21262d; color:#8b949e; }
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
// ── Session hết hạn → tự về trang login ─────────────────────
// Bọc fetch một chỗ để mọi lời gọi API đều được xử lý, khỏi phải
// sửa từng hàm fetch rải khắp file.
(function(){
    const _origFetch = window.fetch;
    let _redirecting = false;
    window.fetch = async function(...args) {
        const res = await _origFetch.apply(this, args);
        if (res.status === 401 && !_redirecting) {
            _redirecting = true;
            window.location.href = '/login';
        }
        return res;
    };
})();

function fmt(n,d=2){return n===null||n===undefined||!Number.isFinite(Number(n))?'—':Number(n).toFixed(d)}
function fmtUsd(n){return n===null||n===undefined||!Number.isFinite(Number(n))?'—':'$'+Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}
function pnlColor(n){return n===null||n===undefined?'muted':(Number(n)>=0?'green':'red')}
function sideHtml(s){return s==='LONG'?'<span class="badge badge-long">LONG</span>':'<span class="badge badge-short">SHORT</span>'}
function ppTierBadgeHtml(ppInfo) {
    if (!ppInfo) return '';
    const tier = Number(ppInfo.tier || 1);
    if (tier >= 5) return '<span style="color:#f0883e;font-size:10px">🔥T5</span>';
    if (tier === 4) return '<span style="color:#58a6ff;font-size:10px">⚡T4</span>';
    if (tier === 3) return '<span style="color:#3fb950;font-size:10px">🎯T3</span>';
    if (tier === 2) return '<span style="color:#d29922;font-size:10px">🛡T2</span>';
    return '<span style="color:#484f58;font-size:10px">T1</span>';
}

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
async function setArmedTTL() {
    const el = document.getElementById('armed-ttl-mins');
    const mins = el ? parseInt(el.value) : 60;
    if (isNaN(mins) || mins < 10 || mins > 480) { toast('TTL phải 10-480 phút', false); return; }
    const r = await apiPost('/api/armed_ttl', {ttl_secs: mins * 60});
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
      <div style="background:#0d1117;border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:20px">
        <iframe id="tv-chart-frame"
          src="https://s.tradingview.com/widgetembed/?frameElementId=tv-chart-frame&symbol=BINANCE:BTCUSDTPERP&interval=15&hidesidetoolbar=0&symboledit=1&theme=dark&style=1&timezone=Asia/Ho_Chi_Minh&withdateranges=1&locale=en"
          style="width:100%;height:500px;border:none" frameborder="0" allowtransparency="true" scrolling="no"></iframe>
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
    if (isNaN(minPct) || minPct < 0.5 || minPct > 50) { toast('Min phải 0.5-50%', false); return; }
    if (isNaN(highPct) || highPct < 5 || highPct > 100) { toast('High phải 5-100%', false); return; }
    if (isNaN(speedPct) || speedPct < 0.1 || speedPct > 10) { toast('Speed phải 0.1-10%/s', false); return; }
    const r = await apiPost('/api/profit_lock', {min_pct: minPct, high_pct: highPct, speed_pct: speedPct});
    if (r && r.msg) toast(r.msg, r.ok !== false);
    refresh();
}
function updateTVChart() {
    const symRaw = document.getElementById('tv-symbol-select')?.value || 'BTCUSDTPERP';
    const interval = document.getElementById('tv-interval-select')?.value || '15';
    const tvFrame = document.getElementById('tv-chart-frame');
    if (tvFrame) {
        const tvInterval = interval.replace('m', '').replace('h', '');
        tvFrame.src = `https://s.tradingview.com/widgetembed/?frameElementId=tv-chart-frame&symbol=BINANCE:${symRaw}&interval=${tvInterval}&hidesidetoolbar=0&symboledit=1&theme=dark&style=1&timezone=Asia/Ho_Chi_Minh&withdateranges=1&locale=en`;
    }
}

// ══════════════════════════════════════════════════════════════════
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
    if (!confirm('Huỷ TẤT CẢ lệnh entry đang chờ (trừ coin được bảo vệ)?')) return;
    await apiPost('/api/cancel_all_pending');
    refresh();
}

let _protectedPendingCoins = [];

function renderProtectedPendingCoins(coins) {
    _protectedPendingCoins = Array.isArray(coins) ? coins : [];
    const root = document.getElementById('protected-pending-coins');
    if (!root) return;
    root.replaceChildren();
    if (_protectedPendingCoins.length === 0) {
        const empty = document.createElement('span');
        empty.style.cssText = 'font-size:11px;color:#f85149';
        empty.textContent = 'Chưa có coin bảo vệ';
        root.appendChild(empty);
        return;
    }
    _protectedPendingCoins.forEach(symbol => {
        const tag = document.createElement('span');
        tag.style.cssText = 'display:inline-flex;align-items:center;gap:4px;background:#2d2208;border:1px solid #6e550d;border-radius:12px;padding:3px 7px;color:#e3b341;font-size:10px;font-weight:700';
        const label = document.createElement('span');
        label.textContent = symbol;
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = '×';
        remove.title = `Bỏ bảo vệ ${symbol}`;
        remove.style.cssText = 'background:none;border:0;color:#f85149;cursor:pointer;padding:0;font-size:14px;line-height:10px';
        remove.addEventListener('click', () => removeProtectedPendingCoin(symbol));
        tag.append(label, remove);
        root.appendChild(tag);
    });
}

async function fetchProtectedPendingCoins() {
    try {
        const response = await fetch('/api/protected-pending-coins');
        const data = await response.json();
        if (data.ok) renderProtectedPendingCoins(data.coins);
    } catch (e) {}
}

async function addProtectedPendingCoin() {
    const input = document.getElementById('protected-order-coin-input');
    const symbol = (input?.value || '').trim().toUpperCase();
    if (!symbol) { toast('Nhập coin cần bảo vệ', false); return; }
    const result = await apiPost('/api/protected-pending-coins/add', {symbol});
    if (result.ok) {
        input.value = '';
        delete _savedInputs['protected-order-coin-input'];
        renderProtectedPendingCoins(result.coins);
    }
}

async function removeProtectedPendingCoin(symbol) {
    if (!confirm(`Bỏ bảo vệ pending order của ${symbol}?`)) return;
    const result = await apiPost('/api/protected-pending-coins/remove', {symbol});
    if (result.ok) renderProtectedPendingCoins(result.coins);
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
            <span style="font-size:12px;color:#d29922">&#x1F6E1; Coin bảo vệ pending order:</span>
            <input id="protected-order-coin-input" placeholder="VD: ADA hoặc ADAUSDT"
                   onkeydown="if(event.key==='Enter') addProtectedPendingCoin()"
                   style="width:155px;background:#0d1117;border:1px solid #30363d;border-radius:5px;padding:5px 7px;color:#c9d1d9;font-size:11px">
            <button class="btn btn-green btn-sm" onclick="addProtectedPendingCoin()">+ Bảo vệ</button>
            <div id="protected-pending-coins" style="display:flex;gap:5px;flex-wrap:wrap;align-items:center">
                <span style="font-size:11px;color:#484f58">Đang tải...</span>
            </div>
            <div style="width:100%;font-size:10px;color:#6e7681">
                Bot không tự hủy order của các coin này khi restart, cleanup hoặc review 4h. Nút hủy từng order vẫn hoạt động.
            </div>
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
            <span>&#x23F0; Armed TTL:</span>
            ${(() => {
                const ttlSecs = d.armed_entry_ttl_secs || 3600;
                const ttlMins = Math.round(ttlSecs / 60);
                return `
                <input id="armed-ttl-mins" type="number" min="10" max="480" step="10" value="${ttlMins}"
                       style="width:50px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#3fb950;text-align:center"
                       title="Thời gian hết hạn armed entry (phút)">
                <span style="font-size:11px;color:#484f58">phút</span>
                <button class="btn btn-sm" onclick="setArmedTTL()" style="font-size:10px;padding:2px 6px;background:#0d1a0d;color:#3fb950;border:1px solid #1a3a1a">Set</button>
                <span style="font-size:11px;color:#3fb950">Coin armed xóa sau ${ttlMins} phút</span>`;
            })()}
        </div>
        <div class="control-row">
            <span>&#x1F4B0; Profit Lock:</span>            ${(() => {
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
                <input id="profit-lock-min" type="number" min="0.5" max="50" step="0.5" value="${minPct}"
                       style="width:44px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#58a6ff;text-align:center"
                       title="Lời tối thiểu (%) để bắt đầu theo dõi dump/pump">
                <span style="font-size:11px;color:#484f58">% High</span>
                <input id="profit-lock-high" type="number" min="5" max="100" step="1" value="${highPct}"
                       style="width:40px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#f85149;text-align:center"
                       title="Lời cao (%) → chốt ngay không cần check tốc độ">
                <span style="font-size:11px;color:#484f58">% Speed</span>
                <input id="profit-lock-speed" type="number" min="0.1" max="10" step="0.1" value="${speedPct}"
                       style="width:44px;font-size:11px;background:#060d14;border:1px solid #1a2a3d;border-radius:4px;padding:2px 4px;color:#d29922;text-align:center"
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

    // Canonical Binance financial cards. Legacy is shown only as an explicit
    // temporary fallback while the first durable ledger snapshot is building.
    const ledgerOk = d.financial_source === 'binance';
    const syncState = d.syncing ? 'Syncing' : (d.stale ? 'Stale' : 'Fresh');
    const syncColor = !ledgerOk || d.stale ? '#d29922' : '#3fb950';
    const syncedLabel = d.synced_at ? new Date(d.synced_at).toLocaleString('vi-VN') : 'chưa sync';
    const financialWarning = (d.financial_warnings || []).join(' · ');
    html += `<div class="stats">
        <div class="card"><div class="label">Wallet</div><div id="stat-wallet" class="value blue">${fmtUsd(d.wallet_balance)}</div></div>
        <div class="card"><div class="label">Available</div><div id="stat-available" class="value blue">${fmtUsd(d.available_balance)}</div></div>
        <div class="card"><div class="label">Margin Equity</div><div id="stat-margin" class="value blue">${fmtUsd(d.margin_balance)}</div></div>
        <div class="card"><div class="label">Today Net</div><div id="stat-today-pnl" class="value ${pnlColor(d.today_net_pnl)}">${fmtUsd(d.today_net_pnl)}</div></div>
        <div class="card"><div class="label">Period Net</div><div id="stat-period-pnl" class="value ${pnlColor(d.period_net_pnl)}">${fmtUsd(d.period_net_pnl)}</div></div>
        <div class="card"><div class="label">Unrealized</div><div id="stat-unrealized" class="value ${pnlColor(d.unrealized)}">${fmtUsd(d.unrealized)}</div></div>
        <div class="card"><div class="label">Win Rate</div><div id="stat-winrate" class="value">${fmt(d.win_rate,0)}%</div></div>
        <div class="card"><div class="label">Closed Trades</div><div id="stat-trades" class="value">${d.closed_cycles}</div></div>
    </div>
    <div id="ledger-detail" class="section" style="padding:10px 14px;margin-top:-8px;font-size:11px;display:flex;gap:14px;flex-wrap:wrap;align-items:center">
        <span>Gross <b id="ledger-gross" class="${pnlColor(d.gross)}">${fmtUsd(d.gross)}</b></span>
        <span>Fees <b id="ledger-fees" class="red">-${fmtUsd(Math.abs(d.commission))}</b></span>
        <span>Funding <b id="ledger-funding" class="${pnlColor(d.funding)}">${fmtUsd(d.funding)}</b></span>
        <span>Transfers <b id="ledger-transfer" class="${pnlColor(d.transfer)}">${fmtUsd(d.transfer)}</b></span>
        <span id="ledger-sync" style="color:${syncColor}">${ledgerOk ? 'Binance ledger' : '⚠ LEGACY FALLBACK'} · ${syncState} · ${syncedLabel}${d.window_start ? ' · từ '+new Date(d.window_start).toLocaleDateString('vi-VN') : ''}${financialWarning ? ' · ⚠ '+financialWarning : ''}</span>
    </div>
    <div id="pnl-stats-section" style="margin-top:8px"><div style="color:#8b949e;font-size:13px">Đang tải PnL...</div></div>`;

    // Market News: macro calendar is cache-only and sits above the existing RSS feed.
    html += `<div class="section" id="news-section">
        <h2>&#x1F4F0; Tin Tức Thị Trường</h2>
        <div class="macro-cal">
          <div class="macro-cal-head">
            <span class="macro-cal-title">&#x1F4C5; Lịch vĩ mô sắp công bố</span>
            <span id="macro-calendar-updated" class="macro-cal-status"></span>
            <span style="flex:1"></span>
            <button onclick="refreshMacroCalendar()" id="macro-calendar-refresh-btn"
                    style="background:#0d1117;border:1px solid #30363d;color:#8b949e;border-radius:6px;padding:3px 10px;font-size:10px;cursor:pointer;font-family:inherit">
              &#x21BB; Cập nhật lịch
            </button>
          </div>
          <div id="macro-calendar-filters" class="news-filters"></div>
          <div class="macro-meta" style="margin:-3px 0 9px">Nguồn lịch chính thức:
            <a class="macro-source" href="https://www.bls.gov/schedule/news_release/bls.ics" target="_blank" rel="noopener noreferrer">BLS</a> ·
            <a class="macro-source" href="https://www.bea.gov/news/schedule" target="_blank" rel="noopener noreferrer">BEA</a> ·
            <a class="macro-source" href="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm" target="_blank" rel="noopener noreferrer">Federal Reserve</a> ·
            <a class="macro-source" href="https://www.dol.gov/ui/data.pdf" target="_blank" rel="noopener noreferrer">DOL</a> ·
            <a class="macro-source" href="https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/" target="_blank" rel="noopener noreferrer">ISM</a> ·
            <a class="macro-source" href="https://www.census.gov/economic-indicators/calendar-listview.html" target="_blank" rel="noopener noreferrer">Census</a>
          </div>
          <div id="macro-calendar-list"><div style="color:#8b949e;font-size:12px">Đang tải lịch vĩ mô...</div></div>
        </div>
    </div>`;

    // Open Positions
    html += `<div class="section"><h2>&#x1F4CC; Open Positions</h2>
        <button class="btn btn-green btn-sm" onclick="autoSetSlTpAll()" style="margin-bottom:8px">&#x1F6E1; Auto Set SL/TP ALL</button>
        <table>
        <tr><th>Coin</th><th>Side</th><th>Entry</th><th>Mark</th><th>PnL</th><th>%</th><th>Lev</th><th></th></tr>
        <tbody id="positions-body">`;
    if (d.open_positions && d.open_positions.length > 0) {
        d.open_positions.forEach(p => {
            const tierBadge = ppTierBadgeHtml(d.pp_state && d.pp_state[p.symbol]);
            html += `<tr><td><b>${p.symbol.replace('USDT','')}</b> ${tierBadge}</td><td>${sideHtml(p.side)}</td>
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

    // ── TV CHART PLACEHOLDER (Open Positions nằm trên, chart nằm dưới) ──
    html += `<div id="tv-chart-placeholder"></div>`;

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

    // ── PHỄU LỌC SCAN — coin đã quét nhưng chưa đủ điều kiện ──
    // Trước đây coin bị loại là mất hẳn khỏi web: khi siết gate thì bảng
    // trắng trơn, không biết bot có quét hay không, coin chết ở đâu.
    const rej = d.scan_rejected || [];
    if (rej.length > 0) {
        const byStage = {};
        rej.forEach(r => { byStage[r.stage] = (byStage[r.stage] || 0) + 1; });
        const stageInfo = {
            'TREND':      ['#8b949e', 'Trend 4H/1H chưa rõ'],
            'REGIME':     ['#a371f7', 'Sideway / chaos'],
            '15m':        ['#d29922', '15m chưa có entry'],
            'VOLUME':     ['#db6d28', 'Volume chưa xác nhận'],
            'LOCATION':   ['#58a6ff', 'Sát kháng cự / hỗ trợ'],
            'PENDING':    ['#3fb950', 'Chờ khớp đa khung'],
            'CONFLUENCE': ['#f85149', 'Chưa đủ tín hiệu'],
            'EDGE':       ['#f85149', 'Chưa vượt trội chiều ngược'],
        };
        const nPass = (d.candidates || []).length;
        html += `<div class="section" style="border-color:#21262d">
          <b style="font-size:12px;color:#8b949e">🔎 Đã quét ${rej.length + nPass} coin —
            <span style="color:#3fb950">${nPass} đủ điều kiện</span>,
            ${rej.length} chưa</b>
          <div style="display:flex;flex-wrap:wrap;gap:5px;margin:8px 0">`;
        Object.keys(byStage).sort((a,b) => byStage[b]-byStage[a]).forEach(st => {
            const inf = stageInfo[st] || ['#6e7681', st];
            html += `<span title="${inf[1]}" style="font-size:10px;font-weight:600;padding:2px 8px;
                     border-radius:10px;background:#0d1117;border:1px solid ${inf[0]}44;color:${inf[0]}">
                     ${st} ${byStage[st]}</span>`;
        });
        html += `</div>
          <div id="scan-rej-wrap" style="display:${_rejOpen?'block':'none'}">
          <table style="margin-top:4px"><tr><th>Coin</th><th>Hướng</th><th>Chặn ở</th><th>Lý do</th></tr>`;
        rej.slice(0, 50).forEach(r => {
            const inf = stageInfo[r.stage] || ['#6e7681', r.stage];
            html += `<tr>
                <td><b>${(r.symbol||'').replace('USDT','')}</b></td>
                <td style="font-size:11px;color:${r.signal==='LONG'?'#3fb950':(r.signal==='SHORT'?'#f85149':'#6e7681')}">${r.signal||'-'}</td>
                <td style="font-size:10px;font-weight:600;color:${inf[0]}">${r.stage}</td>
                <td style="font-size:11px;color:#8b949e">${(r.reason||'').replace(/</g,'&lt;')}</td>
            </tr>`;
        });
        html += `</table></div>
          <div style="text-align:center;margin-top:6px">
            <button onclick="toggleRej()" style="background:#0d1117;border:1px solid #30363d;color:#8b949e;
                    border-radius:6px;padding:3px 12px;font-size:11px;cursor:pointer;font-family:inherit">
              ${_rejOpen ? '▴ Ẩn chi tiết' : '▾ Xem chi tiết ' + rej.length + ' coin'}
            </button>
          </div>
        </div>`;
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

        <!-- ══ GATE LỌC SCAN ══ -->
        <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;margin-top:12px">
          <div style="font-size:12px;color:#58a6ff;margin-bottom:4px;font-weight:600">
            🔎 Gate lọc scan — điều kiện coin phải vượt để vào ARMED
          </div>
          <div style="font-size:10.5px;color:#484f58;margin-bottom:10px;line-height:1.5">
            Đặt càng cao càng ít lệnh. Xem bảng "Đã quét N coin" phía trên để biết gate nào đang chặn nhiều nhất.
          </div>

          <!-- VOLUME -->
          <div style="border-left:2px solid #db6d28;padding-left:10px;margin-bottom:12px">
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;margin-bottom:6px">
              <input type="checkbox" id="p0-vol-enabled" style="width:14px;height:14px">
              <span style="color:#db6d28;font-weight:600">Volume confirm</span>
            </label>
            <div style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end">
              <div>
                <div style="font-size:10px;color:#8b949e;margin-bottom:2px">Entry — volume ≥ ? × MA20</div>
                <input type="number" id="p0-vol-ratio" min="0" max="3" step="0.05"
                       oninput="updateGateHints()"
                       style="width:74px;background:#161b22;border:1px solid #30363d;border-radius:4px;
                              padding:4px 6px;color:#e6edf3;font-size:12px">
                <span id="p0-vol-hint" style="font-size:10px;color:#484f58;margin-left:4px"></span>
              </div>
              <div>
                <div style="font-size:10px;color:#8b949e;margin-bottom:2px">Pullback — volume ≥ ? × MA20 <span style="color:#484f58">(0 = không check)</span></div>
                <input type="number" id="p0-pb-vol-ratio" min="0" max="3" step="0.05"
                       style="width:74px;background:#161b22;border:1px solid #30363d;border-radius:4px;
                              padding:4px 6px;color:#e6edf3;font-size:12px">
              </div>
            </div>
            <div style="font-size:10px;color:#484f58;margin-top:6px;line-height:1.5">
              Đo 8280 nến 15m / 46 coin: trung vị <b style="color:#8b949e">0.81×</b>, trung bình 1.03×.
              Nến qua được: <b style="color:#8b949e">0.8→50%</b> · 1.0→36% · 1.2→26%.
              Đặt 1.0 là đòi volume bùng nổ, không phải xác nhận.
            </div>
          </div>

          <!-- LOCATION -->
          <div style="border-left:2px solid #58a6ff;padding-left:10px;margin-bottom:12px">
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;margin-bottom:6px">
              <input type="checkbox" id="p0-loc-enabled" style="width:14px;height:14px">
              <span style="color:#58a6ff;font-weight:600">Location — room tới kháng cự/hỗ trợ 1H</span>
            </label>
            <div style="font-size:10px;color:#8b949e;margin-bottom:2px">Cần ít nhất ? × ATR 1H</div>
            <input type="number" id="p0-loc-room" min="0" max="5" step="0.1"
                   oninput="updateGateHints()"
                   style="width:74px;background:#161b22;border:1px solid #30363d;border-radius:4px;
                          padding:4px 6px;color:#e6edf3;font-size:12px">
            <span id="p0-loc-hint" style="font-size:10px;color:#484f58;margin-left:4px"></span>
            <div style="font-size:10px;color:#484f58;margin-top:6px;line-height:1.5">
              Đo thật 46 coin: room phân bố <b style="color:#8b949e">0.4–1.4× ATR</b>.
              Đặt 1.5 từng loại 18/46 coin và cho 0 PASS.
            </div>
          </div>

          <!-- CONFLUENCE -->
          <div style="border-left:2px solid #f85149;padding-left:10px;margin-bottom:12px">
            <div style="font-size:12px;color:#f85149;font-weight:600;margin-bottom:6px">Confluence — số tín hiệu đồng thuận</div>
            <div style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end">
              <div>
                <div style="font-size:10px;color:#8b949e;margin-bottom:2px">Tối thiểu</div>
                <input type="number" id="p0-conf-min" min="0" max="10" step="1"
                       style="width:64px;background:#161b22;border:1px solid #30363d;border-radius:4px;
                              padding:4px 6px;color:#e6edf3;font-size:12px">
              </div>
              <div>
                <div style="font-size:10px;color:#8b949e;margin-bottom:2px">Cách chiều ngược ≥</div>
                <input type="number" id="p0-conf-edge" min="0" max="6" step="1"
                       style="width:64px;background:#161b22;border:1px solid #30363d;border-radius:4px;
                              padding:4px 6px;color:#e6edf3;font-size:12px">
              </div>
            </div>
          </div>

          <!-- PULLBACK ELIGIBILITY -->
          <div style="border-left:2px solid #3fb950;padding-left:10px;margin-bottom:12px">
            <div style="font-size:12px;color:#3fb950;font-weight:600;margin-bottom:6px">
              Đường pullback — coin nào được phép dùng
            </div>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;margin-bottom:6px">
              <input type="checkbox" id="p0-pb-self" style="width:14px;height:14px"
                     onchange="updateGateHints()">
              <span style="color:#c9d1d9">So với chính coin đó (khuyên dùng)</span>
            </label>
            <div style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end">
              <div>
                <div style="font-size:10px;color:#8b949e;margin-bottom:2px">ATR ≥ ? × trung vị riêng</div>
                <input type="number" id="p0-pb-self-ratio" min="0" max="2" step="0.05"
                       oninput="updateGateHints()"
                       style="width:74px;background:#161b22;border:1px solid #30363d;border-radius:4px;
                              padding:4px 6px;color:#e6edf3;font-size:12px">
              </div>
              <div>
                <div style="font-size:10px;color:#8b949e;margin-bottom:2px">Ngưỡng tuyệt đối (cách cũ)</div>
                <input type="number" id="p0-pb-abs" min="0" max="20" step="0.5"
                       style="width:74px;background:#161b22;border:1px solid #30363d;border-radius:4px;
                              padding:4px 6px;color:#e6edf3;font-size:12px">
                <span style="font-size:10px;color:#484f58;margin-left:2px">%</span>
              </div>
            </div>
            <div id="p0-pb-hint" style="font-size:10px;color:#484f58;margin-top:6px;line-height:1.5"></div>
          </div>

          <!-- REGIME + TREND -->
          <div style="border-left:2px solid #a371f7;padding-left:10px">
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer;margin-bottom:6px">
              <input type="checkbox" id="p0-regime-enabled" style="width:14px;height:14px">
              <span style="color:#a371f7;font-weight:600">Regime — skip khi sideway (RANGE) / chaos</span>
            </label>
            <label style="display:flex;align-items:center;gap:8px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="p0-trend-conflict" style="width:14px;height:14px">
              <span style="color:#c9d1d9">4H ngược 1H → bỏ coin luôn</span>
            </label>
            <div style="font-size:10px;color:#484f58;margin-top:6px;line-height:1.5">
              Tắt = vẫn theo hướng 4H nhưng hạ xuống MEDIUM. Bật thì loại thêm ~7/46 coin.
            </div>
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
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
              <span style="font-size:12px;color:#8b949e;width:130px">T3 distance:</span>
              <input type="number" id="pp-trail-dist" min="0.1" max="3" step="0.1"
                     style="width:55px;background:#161b22;border:1px solid #30363d;color:#e6edf3;border-radius:4px;padding:3px 6px;font-size:12px">
              <span style="font-size:11px;color:#484f58">%</span>
            </div>
            <div style="border-top:1px solid #21262d;margin:8px 0;padding-top:8px">
              <div style="font-size:11px;color:#58a6ff;margin-bottom:6px;font-weight:600">⚡ Tier 4 — Near TP</div>
              <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
                <span style="font-size:12px;color:#8b949e;width:130px">Trigger lời:</span>
                <input type="number" id="pp-tier4-threshold" min="1" max="20" step="0.5"
                       style="width:55px;background:#161b22;border:1px solid #1a3a5a;color:#58a6ff;border-radius:4px;padding:3px 6px;font-size:12px"
                       title="Khi lời >= X% → vào Tier 4">
                <span style="font-size:11px;color:#484f58">%</span>
              </div>
              <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
                <span style="font-size:12px;color:#8b949e;width:130px">Timer xác nhận:</span>
                <input type="number" id="pp-tier4-timer" min="1" max="30" step="1"
                       style="width:55px;background:#161b22;border:1px solid #1a3a5a;color:#58a6ff;border-radius:4px;padding:3px 6px;font-size:12px">
                <span style="font-size:11px;color:#484f58">giây</span>
              </div>
              <div style="display:flex;align-items:center;gap:6px">
                <span style="font-size:12px;color:#8b949e;width:130px">T4 distance:</span>
                <input type="number" id="pp-tier4-dist" min="0.05" max="2" step="0.05"
                       style="width:55px;background:#161b22;border:1px solid #1a3a5a;color:#58a6ff;border-radius:4px;padding:3px 6px;font-size:12px">
                <span style="font-size:11px;color:#484f58">%</span>
              </div>
            </div>
            <div style="border-top:1px solid #21262d;margin:8px 0;padding-top:8px">
              <div style="font-size:11px;color:#f0883e;margin-bottom:6px;font-weight:600">🔥 Tier 5 — Very Near TP</div>
              <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
                <span style="font-size:12px;color:#8b949e;width:130px">Trigger lời:</span>
                <input type="number" id="pp-tier5-threshold" min="1" max="30" step="0.5"
                       style="width:55px;background:#161b22;border:1px solid #3a2a00;color:#f0883e;border-radius:4px;padding:3px 6px;font-size:12px"
                       title="Khi lời >= X% → vào Tier 5">
                <span style="font-size:11px;color:#484f58">%</span>
              </div>
              <div style="display:flex;align-items:center;gap:6px;margin-bottom:6px">
                <span style="font-size:12px;color:#8b949e;width:130px">Timer xác nhận:</span>
                <input type="number" id="pp-tier5-timer" min="1" max="30" step="1"
                       style="width:55px;background:#161b22;border:1px solid #3a2a00;color:#f0883e;border-radius:4px;padding:3px 6px;font-size:12px">
                <span style="font-size:11px;color:#484f58">giây</span>
              </div>
              <div style="display:flex;align-items:center;gap:6px">
                <span style="font-size:12px;color:#8b949e;width:130px">T5 distance:</span>
                <input type="number" id="pp-tier5-dist" min="0.05" max="1" step="0.05"
                       style="width:55px;background:#161b22;border:1px solid #3a2a00;color:#f0883e;border-radius:4px;padding:3px 6px;font-size:12px">
                <span style="font-size:11px;color:#484f58">%</span>
              </div>
            </div>
            <div style="display:flex;gap:10px;margin-top:8px">
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
let _pnlExpanded = false;        // false = chỉ hiện _PNL_ROW_LIMIT dòng gần nhất
const _PNL_ROW_LIMIT = 7;

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
    const allActiveRows = rows.filter(r => r.trades > 0);
    // Chỉ hiện _PNL_ROW_LIMIT dòng đầu, còn lại ẩn sau nút "Xem thêm".
    // Trước đây render hết -> tab "Theo Ngày" dài cả 30 dòng, phải scroll mãi.
    const _limit = _pnlExpanded ? allActiveRows.length : _PNL_ROW_LIMIT;
    const activeRows = allActiveRows.slice(0, _limit);
    const hiddenCount = allActiveRows.length - activeRows.length;

    const totalPnl    = rows.reduce((s,r) => s + r.pnl, 0);
    const totalTrades = rows.reduce((s,r) => s + r.trades, 0);
    const totalWins   = rows.reduce((s,r) => s + r.wins, 0);
    const wr = totalTrades > 0 ? (totalWins / totalTrades * 100) : 0;
    const pnlColor = v => v >= 0 ? '#3fb950' : '#f85149';

    let html = `
    <div class="pnl-stats-tabs">
        <div class="pnl-tab ${_pnlTab==='equity'?'active':''}" onclick="setPnlTab('equity')">📈 Equity</div>
        <div class="pnl-tab ${_pnlTab==='daily'?'active':''}" onclick="setPnlTab('daily')">Theo Ngày</div>
        <div class="pnl-tab ${_pnlTab==='weekly'?'active':''}" onclick="setPnlTab('weekly')">Theo Tuần</div>
        <div class="pnl-tab ${_pnlTab==='monthly'?'active':''}" onclick="setPnlTab('monthly')">Theo Tháng</div>
        <div class="pnl-tab ${_pnlTab==='by_coin'?'active':''}" onclick="setPnlTab('by_coin')">Theo Coin</div>
        <div style="flex:1"></div>
        <button onclick="clearTradeHistory()" style="padding:5px 14px;border-radius:6px;border:1px solid #f85149;background:transparent;color:#f85149;cursor:pointer;font-size:12px;font-weight:600;transition:all .2s;" onmouseover="this.style.background='#f85149';this.style.color='#fff'" onmouseout="this.style.background='transparent';this.style.color='#f85149'">🗑 Clear Data</button>
    </div>`;

    // ── EQUITY CURVE TAB ─────────────────────────────────────
    if (_pnlTab === 'equity') {
        // Chỉ set innerHTML nếu chưa có wrap (tránh giật khi renderPnlStats gọi lại)
        if (!document.getElementById('equity-curve-wrap')) {
            el.innerHTML = html + '<div id="equity-curve-wrap"><div style="color:#8b949e;font-size:13px;padding:12px 0">Đang tải equity curve...</div></div>';
            fetchEquityCurve(_equityRange);
        } else {
            // Chỉ update tabs (active state) mà không xóa chart
            const tabsEl = el.querySelector('.pnl-stats-tabs');
            if (tabsEl) tabsEl.outerHTML = html.match(/<div class="pnl-stats-tabs">[^]*?<[/]div>/)?.[0] || tabsEl.outerHTML;
        }
        return;
    }

    html += `<div class="pnl-summary-row">
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

        // Nút mở rộng — chỉ hiện khi thật sự có dòng bị ẩn
        if (hiddenCount > 0) {
            const hidPnl = allActiveRows.slice(_PNL_ROW_LIMIT)
                                        .reduce((s,r) => s + r.pnl, 0);
            html += `
            <div style="text-align:center;margin-top:8px">
              <button onclick="togglePnlExpand()"
                      style="background:#0d1117;border:1px solid #30363d;color:#8b949e;
                             border-radius:6px;padding:5px 16px;font-size:12px;cursor:pointer;
                             font-family:inherit;transition:all .2s"
                      onmouseover="this.style.borderColor='#58a6ff';this.style.color='#58a6ff'"
                      onmouseout="this.style.borderColor='#30363d';this.style.color='#8b949e'">
                &#x25BE; Xem thêm ${hiddenCount} dòng
                <span style="color:${pnlColor(hidPnl)}">(${hidPnl>=0?'+':''}$${hidPnl.toFixed(2)})</span>
              </button>
            </div>`;
        } else if (_pnlExpanded && allActiveRows.length > _PNL_ROW_LIMIT) {
            html += `
            <div style="text-align:center;margin-top:8px">
              <button onclick="togglePnlExpand()"
                      style="background:#0d1117;border:1px solid #30363d;color:#8b949e;
                             border-radius:6px;padding:5px 16px;font-size:12px;cursor:pointer;
                             font-family:inherit;transition:all .2s"
                      onmouseover="this.style.borderColor='#58a6ff';this.style.color='#58a6ff'"
                      onmouseout="this.style.borderColor='#30363d';this.style.color='#8b949e'">
                &#x25B4; Thu gọn
              </button>
            </div>`;
        }
    }

    el.innerHTML = html;
}

function togglePnlExpand() {
    _pnlExpanded = !_pnlExpanded;
    renderPnlStats();
}

function setPnlTab(tab) {
    _pnlTab = tab;
    _pnlExpanded = false;
    if (tab === 'equity') {
        fetchEquityCurve(_equityRange);
        renderPnlStats();  // render tabs + placeholder
    } else {
        renderPnlStats();
    }
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

// ── EQUITY CURVE ─────────────────────────────────────────────
let _equityRange = '30d';
let _equityData  = null;

async function fetchEquityCurve(range) {
    _equityRange = range || _equityRange;
    try {
        const r = await fetch('/api/equity_curve?range=' + _equityRange);
        _equityData = await r.json();
        renderEquityCurve();
    } catch(e) {
        const w = document.getElementById('equity-curve-wrap');
        if (w) w.innerHTML = '<div style="color:#f85149;font-size:12px">Lỗi tải equity curve</div>';
    }
}

function renderEquityCurve() {
    const wrap = document.getElementById('equity-curve-wrap');
    if (!wrap || !_equityData) return;
    const { points, start_balance, end_balance, change_usd, change_pct, period_net, trade_count, reconstructed, forced_reconstruction, source } = _equityData;

    const netAvailable = period_net !== null && period_net !== undefined;
    const displayedNet = netAvailable ? Number(period_net) : null;
    const isUp    = !netAvailable || displayedNet >= 0;
    const clrLine = isUp ? '#3fb950' : '#f85149';
    const clrText = isUp ? '#3fb950' : '#f85149';
    const sign    = displayedNet >= 0 ? '+' : '';
    const fmtMoney = v => v >= 1000 ? '$' + v.toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2}) : '$' + v.toFixed(2);

    // Range selector
    let rangeHtml = '<div style="display:flex;gap:6px;margin-bottom:12px">';
    ['1d','7d','30d','90d','all'].forEach(r => {
        const active = r === _equityRange;
        const label  = r === 'all' ? 'Tất cả' : r === '1d' ? '1 ngày' : r === '7d' ? '7 ngày' : r === '30d' ? '30 ngày' : '90 ngày';
        rangeHtml += `<div onclick="fetchEquityCurve('${r}')"
            style="padding:4px 14px;border-radius:6px;border:1px solid ${active?'#3fb950':'#30363d'};
                   background:${active?'rgba(63,185,80,0.12)':'transparent'};
                   color:${active?'#3fb950':'#8b949e'};cursor:pointer;font-size:12px;font-weight:${active?600:400};transition:all .2s">${label}</div>`;
    });
    rangeHtml += '</div>';

    if (!points || points.length < 1) {
        wrap.innerHTML = rangeHtml + '<div style="color:#8b949e;font-size:13px;padding:20px 0;text-align:center">📭 Chưa có đủ dữ liệu</div>';
        return;
    }

    const firstTime = points[0].time.substring(0, 10);
    const lastTime  = points[points.length-1].time.substring(0, 10);
    const fmt10 = s => s.substring(5).replace('-','/');

    // Header shows trading net; wallet transfers remain separate in the bridge.
    const headerHtml = `<div style="margin-bottom:12px">
        <div style="font-size:10px;color:#8b949e;text-transform:uppercase">Period Net PnL</div>
        <div style="font-size:30px;font-weight:700;color:${clrText};line-height:1.1">${sign}${fmtMoney(displayedNet)}</div>
        <div style="font-size:15px;color:${clrText};font-weight:600;margin-top:2px">${sign}${change_pct.toFixed(2)}%</div>
        <div style="font-size:10px;color:${source==='binance'?'#3fb950':'#d29922'};margin-top:4px">${source==='binance'?'Binance event ledger':'⚠ Legacy fallback'} · ${forced_reconstruction?'forced reconstruction from current wallet (not independently reconciled)':(reconstructed?'reconstructed wallet bridge':'estimated')}</div>
    </div>`;

    // SVG
    const W = 500, H = 160, PT = 16, PB = 28, PL = 8, PR = 48;
    const cW = W - PL - PR, cH = H - PT - PB;
    const vals = points.map(p => p.balance);
    const minV = Math.min(...vals), maxV = Math.max(...vals);
    const rng  = maxV - minV || 1;
    const xs   = points.map((_, i) => PL + (i / (points.length - 1)) * cW);
    const ys   = vals.map(v => PT + cH - ((v - minV) / rng) * cH);

    function bpath(xa, ya) {
        let d = 'M ' + xa[0].toFixed(1) + ' ' + ya[0].toFixed(1);
        for (let i = 1; i < xa.length; i++) {
            const cx = (xa[i-1] + xa[i]) / 2;
            d += ' C ' + cx.toFixed(1)+' '+ya[i-1].toFixed(1)+', '+cx.toFixed(1)+' '+ya[i].toFixed(1)+', '+xa[i].toFixed(1)+' '+ya[i].toFixed(1);
        }
        return d;
    }

    const linePath = bpath(xs, ys);
    const fillPath = linePath + ' L ' + xs[xs.length-1].toFixed(1) + ' ' + (PT+cH) + ' L ' + xs[0].toFixed(1) + ' ' + (PT+cH) + ' Z';

    let grid = '';
    for (let i = 0; i <= 2; i++) {
        const gy  = PT + (i / 2) * cH;
        const gv  = maxV - (i / 2) * rng;
        const gLbl = gv >= 1000 ? '$'+(gv/1000).toFixed(1)+'k' : '$'+gv.toFixed(0);
        grid += `<line x1="${PL}" y1="${gy.toFixed(1)}" x2="${W-PR}" y2="${gy.toFixed(1)}" stroke="#21262d" stroke-width="1" stroke-dasharray="3,3"/>
                 <text x="${W-PR+4}" y="${(gy+4).toFixed(1)}" fill="#484f58" font-size="9">${gLbl}</text>`;
    }

    const xLbl = `<text x="${xs[0].toFixed(1)}" y="${(PT+cH+14).toFixed(1)}" fill="#484f58" font-size="9" text-anchor="middle">${fmt10(firstTime)}</text>
                  <text x="${xs[xs.length-1].toFixed(1)}" y="${(PT+cH+14).toFixed(1)}" fill="#484f58" font-size="9" text-anchor="middle">${fmt10(lastTime)}</text>`;

    const uid = 'ec' + Date.now();
    const ptData = JSON.stringify(points.map((p,i) => ({x:xs[i],y:ys[i],balance:p.balance,pnl:p.pnl,pnl_pct:p.pnl_pct,symbol:p.symbol,side:p.side,time:p.time})));

    const svgHtml = `
    <div style="position:relative" id="${uid}wrap">
    <svg id="${uid}" viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block;cursor:crosshair"
         onmousemove="eqMM(event,'${uid}')" onmouseleave="eqML('${uid}')">
      <defs>
        <linearGradient id="${uid}g" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${clrLine}" stop-opacity="0.22"/>
          <stop offset="100%" stop-color="${clrLine}" stop-opacity="0.01"/>
        </linearGradient>
      </defs>
      ${grid}${xLbl}
      <path d="${fillPath}" fill="url(#${uid}g)" stroke="none"/>
      <path d="${linePath}" fill="none" stroke="${clrLine}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${xs[0].toFixed(1)}" cy="${ys[0].toFixed(1)}" r="5" fill="#0d1117" stroke="${clrLine}" stroke-width="2"/>
      <circle cx="${xs[xs.length-1].toFixed(1)}" cy="${ys[ys.length-1].toFixed(1)}" r="5.5" fill="${clrLine}" stroke="${clrLine}" stroke-width="2"/>
      <line id="${uid}x" x1="0" y1="${PT}" x2="0" y2="${PT+cH}" stroke="#484f58" stroke-width="1" stroke-dasharray="3,2" opacity="0"/>
      <circle id="${uid}d" r="5" fill="#0d1117" stroke="${clrLine}" stroke-width="2" opacity="0"/>
    </svg>
    <div style="position:absolute;left:${PL}px;top:2px;font-size:10px;color:#8b949e;line-height:1.4;pointer-events:none">
        <div>${fmt10(firstTime)}</div><div style="color:#c9d1d9;font-weight:600">${fmtMoney(start_balance)}</div>
    </div>
    <div style="position:absolute;right:${PR}px;top:2px;font-size:10px;color:#8b949e;text-align:right;line-height:1.4;pointer-events:none">
        <div>${fmt10(lastTime)}</div><div style="color:${clrLine};font-weight:700">${fmtMoney(end_balance)}</div>
    </div>
    <div id="${uid}t" style="position:absolute;display:none;background:#1c2128;border:1px solid #30363d;border-radius:8px;padding:8px 12px;font-size:12px;pointer-events:none;min-width:148px;box-shadow:0 4px 16px rgba(0,0,0,.5)"></div>
    </div>
    <script>(function(){var d=${ptData};window.__eq=window.__eq||{};window.__eq['${uid}']={pts:d,clr:'${clrLine}',W:${W},H:${H},PT:${PT},PB:${PB},PL:${PL},PR:${PR}};})();<${'/'}script>`;

    const statsHtml = `<div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
        <div style="flex:1;min-width:70px;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:8px 10px;text-align:center">
            <div style="font-size:10px;color:#8b949e">Số lệnh</div>
            <div style="font-size:18px;font-weight:700;color:#c9d1d9">${trade_count ?? 0}</div>
        </div>
        <div style="flex:1;min-width:80px;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:8px 10px;text-align:center">
            <div style="font-size:10px;color:#8b949e">Balance đầu</div>
            <div style="font-size:13px;font-weight:700;color:#c9d1d9">${fmtMoney(start_balance)}</div>
        </div>
        <div style="flex:1;min-width:80px;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:8px 10px;text-align:center">
            <div style="font-size:10px;color:#8b949e">Hiện tại</div>
            <div style="font-size:13px;font-weight:700;color:${clrLine}">${fmtMoney(end_balance)}</div>
        </div>
        <div style="flex:1;min-width:80px;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:8px 10px;text-align:center">
            <div style="font-size:10px;color:#8b949e">Period Net</div>
            <div style="font-size:13px;font-weight:700;color:${clrText}">${sign}${fmtMoney(displayedNet)}</div>
        </div>
    </div>`;

    wrap.innerHTML = rangeHtml + headerHtml + svgHtml + statsHtml;
}

function eqMM(e, uid) {
    const info = window.__eq && window.__eq[uid];
    if (!info) return;
    const svg  = document.getElementById(uid);
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const mx   = (e.clientX - rect.left) * (info.W / rect.width);
    let closest = 0, minD = Infinity;
    info.pts.forEach((p, i) => { const d = Math.abs(p.x - mx); if (d < minD) { minD = d; closest = i; } });
    const pt = info.pts[closest];
    const xh = document.getElementById(uid + 'x');
    const hd = document.getElementById(uid + 'd');
    if (xh) { xh.setAttribute('x1', pt.x); xh.setAttribute('x2', pt.x); xh.setAttribute('opacity', '1'); }
    if (hd) { hd.setAttribute('cx', pt.x); hd.setAttribute('cy', pt.y); hd.setAttribute('opacity', '1'); }
    const tip = document.getElementById(uid + 't');
    if (!tip) return;
    const fm  = v => v >= 1000 ? '$'+v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : '$'+v.toFixed(2);
    const pc  = pt.pnl >= 0 ? '#3fb950' : '#f85149';
    const sg  = pt.pnl >= 0 ? '+' : '';
    let inner = `<div style="color:#8b949e;font-size:10px;margin-bottom:4px">${pt.time.substring(0,16)}</div>
        <div style="font-size:15px;font-weight:700;color:#c9d1d9;margin-bottom:4px">${fm(pt.balance)}</div>`;
    if (pt.symbol) inner += `<div style="font-size:11px;color:${pc}">${sg}${fm(pt.pnl)} (${sg}${pt.pnl_pct.toFixed(2)}%)</div>
        <div style="font-size:10px;color:#8b949e;margin-top:2px">${pt.symbol} ${pt.side}</div>`;
    tip.innerHTML = inner;
    const wEl = document.getElementById(uid + 'wrap');
    const wR  = wEl ? wEl.getBoundingClientRect() : rect;
    let tl = e.clientX - wR.left + 14;
    if (tl + 160 > wR.width) tl = e.clientX - wR.left - 162;
    tip.style.left = tl + 'px';
    tip.style.top  = (e.clientY - wR.top - 20) + 'px';
    tip.style.display = 'block';
}

function eqML(uid) {
    const xh = document.getElementById(uid + 'x');
    const hd = document.getElementById(uid + 'd');
    const tip = document.getElementById(uid + 't');
    if (xh) xh.setAttribute('opacity','0');
    if (hd) hd.setAttribute('opacity','0');
    if (tip) tip.style.display = 'none';
}

// ── MACRO ECONOMIC CALENDAR ─────────────────────────────────
let _macroCalendarData = null;
let _macroCalendarFilter = 'upcoming';
let _macroCalendarExpanded = false;
let _macroCalendarFetching = false;

function _macroEsc(value) {
    return String(value === null || value === undefined ? '' : value)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function _macroHttps(value) {
    try {
        const parsed = new URL(String(value || ''), window.location.href);
        return parsed.protocol === 'https:' ? parsed.href : null;
    } catch (_) { return null; }
}
function _macroDate(value) {
    const parsed = new Date(value || '');
    return Number.isNaN(parsed.getTime()) ? null : parsed;
}
function _macroVnTime(value) {
    const parsed = _macroDate(value);
    if (!parsed) return 'Chưa xác định';
    return new Intl.DateTimeFormat('vi-VN', {
        timeZone:'Asia/Ho_Chi_Minh', weekday:'short', day:'2-digit', month:'2-digit',
        year:'numeric', hour:'2-digit', minute:'2-digit', hour12:false
    }).format(parsed);
}
function _macroCountdown(value) {
    const parsed = _macroDate(value);
    if (!parsed) return 'Chưa xác định';
    const seconds = Math.floor((parsed.getTime() - Date.now()) / 1000);
    const past = seconds < 0;
    const absolute = Math.abs(seconds);
    const days = Math.floor(absolute / 86400);
    const hours = Math.floor((absolute % 86400) / 3600);
    const minutes = Math.floor((absolute % 3600) / 60);
    const secs = absolute % 60;
    const body = days > 0 ? `${days}ng ${hours}h` : (hours > 0 ? `${hours}h ${minutes}p` : `${minutes}p ${secs}gi`);
    return past ? `đã qua ${body}` : `còn ${body}`;
}
function _macroValue(event, key) {
    const value = event[key];
    if (value === null || value === undefined || value === '')
        return key === 'forecast' ? 'Chưa có consensus' : 'Chưa có';
    const text = String(value), unit = String(event.unit || '');
    return unit && text.trim().endsWith(unit) ? text : text + unit;
}
function _macroDirection(direction) {
    const value = String(direction || 'MIXED').toUpperCase();
    if (value === 'BULLISH') return {cls:'bull', icon:'↑', text:'BULLISH'};
    if (value === 'BEARISH') return {cls:'bear', icon:'↓', text:'BEARISH'};
    return {cls:'mixed', icon:'↕', text:'MIXED'};
}
function setMacroCalendarFilter(filter) {
    _macroCalendarFilter = filter;
    renderMacroCalendar();
}
function toggleMacroCalendarRange() {
    _macroCalendarExpanded = !_macroCalendarExpanded;
    renderMacroCalendar();
}
async function fetchMacroCalendar() {
    if (_macroCalendarFetching) return;
    _macroCalendarFetching = true;
    try {
        const response = await fetch('/api/macro-calendar');
        const data = await response.json();
        if (data && Array.isArray(data.events)) {
            _macroCalendarData = data;
            renderMacroCalendar();
        }
    } catch (_) {
        const el = document.getElementById('macro-calendar-list');
        if (el && !_macroCalendarData) el.textContent = 'Không đọc được cache lịch vĩ mô.';
    } finally { _macroCalendarFetching = false; }
}
async function refreshMacroCalendar() {
    const button = document.getElementById('macro-calendar-refresh-btn');
    if (button) { button.disabled = true; button.textContent = '⏳ Đã xếp lịch...'; }
    try {
        const response = await fetch('/api/macro-calendar/refresh', {
            method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
        });
        const data = await response.json();
        if (data && data.snapshot) _macroCalendarData = data.snapshot;
        renderMacroCalendar();
        toast(data.queued ? 'Đã yêu cầu cập nhật nền' : 'Đang có lượt cập nhật nền', true);
    } catch (_) { toast('Không thể yêu cầu cập nhật lịch', false); }
    finally {
        if (button) { button.disabled = false; button.innerHTML = '&#x21BB; Cập nhật lịch'; }
    }
}
function _updateMacroCountdowns() {
    document.querySelectorAll('[data-macro-time]').forEach(el => {
        el.textContent = _macroCountdown(el.getAttribute('data-macro-time'));
    });
}
function renderMacroCalendar() {
    const root = document.getElementById('macro-calendar-list');
    if (!root || !_macroCalendarData) return;
    const all = Array.isArray(_macroCalendarData.events) ? _macroCalendarData.events : [];
    const now = Date.now();
    const horizon = 14 * 86400000;
    const upcoming = all.filter(e => e.status !== 'released');
    const released = all.filter(e => e.status === 'released');
    const counts = {upcoming:upcoming.length, released:released.length, high:all.filter(e => e.impact === 'HIGH').length};
    const filters = document.getElementById('macro-calendar-filters');
    if (filters) filters.innerHTML = [
        ['upcoming','Sắp tới',counts.upcoming], ['released','Đã công bố',counts.released], ['high','🔴 High',counts.high]
    ].map(([key,label,count]) => `<div class="news-chip ${key==='high'?'impact-chip':''} ${_macroCalendarFilter===key?'active':''}" onclick="setMacroCalendarFilter('${key}')">${label} <span style="opacity:.6">${count}</span></div>`).join('');

    let rows = all.filter(event => {
        if (_macroCalendarFilter === 'released' && event.status !== 'released') return false;
        if (_macroCalendarFilter === 'upcoming' && event.status === 'released') return false;
        if (_macroCalendarFilter === 'high' && event.impact !== 'HIGH') return false;
        if (_macroCalendarExpanded) return true;
        const scheduled = _macroDate(event.scheduled_at_utc);
        if (!scheduled) return false;
        const delta = scheduled.getTime() - now;
        return event.status === 'released' ? delta >= -horizon : delta <= horizon && delta >= -3600000;
    }).sort((a,b) => (_macroDate(a.scheduled_at_utc)?.getTime() || 0) - (_macroDate(b.scheduled_at_utc)?.getTime() || 0));

    const status = document.getElementById('macro-calendar-updated');
    if (status) {
        const age = _macroCalendarData.age_seconds;
        const ageLabel = age === null || age === undefined ? 'chưa có cache' : `cache ${_newsAgo(Math.floor(Date.now()/1000)-age)} trước`;
        const flags = [];
        if (_macroCalendarData.refreshing) flags.push('đang cập nhật nền');
        if (_macroCalendarData.stale) flags.push('⚠ dữ liệu cũ');
        const errors = Object.keys(_macroCalendarData.source_errors || {}).length;
        if (errors) flags.push(`⚠ ${errors} nguồn lỗi`);
        status.textContent = [ageLabel, ...flags].join(' · ');
    }
    if (!rows.length) {
        const error = _macroCalendarData.error ? ` ${_macroCalendarData.error}` : '';
        root.innerHTML = `<div style="color:#8b949e;font-size:11px;padding:8px">Không có sự kiện trong phạm vi này.${_macroEsc(error)}</div>` +
            `<button class="macro-more" onclick="toggleMacroCalendarRange()">${_macroCalendarExpanded?'Chỉ xem 14 ngày':'Mở rộng toàn bộ cache'}</button>`;
        return;
    }
    const cards = rows.map(event => {
        const high = event.impact === 'HIGH';
        const tentative = event.status === 'tentative' || event.timing_confirmed === false;
        const sourceUrl = _macroHttps(event.source_url);
        const source = sourceUrl
            ? `<a class="macro-source" target="_blank" rel="noopener noreferrer" href="${_macroEsc(sourceUrl)}">${_macroEsc(event.source || event.provider || 'Nguồn chính thức')}</a>`
            : `<span>${_macroEsc(event.source || 'Không có liên kết nguồn')}</span>`;
        const scenarios = event.scenarios || {};
        const scenarioHtml = ['higher','lower'].map(key => {
            const scenario = scenarios[key] || {};
            const direction = _macroDirection(scenario.btc_direction);
            return `<div class="macro-scenario"><b>${_macroEsc(scenario.label || (key==='higher'?'Actual > Forecast':'Actual < Forecast'))}</b> → BTC <span class="${direction.cls}">${direction.icon} ${direction.text}</span><br>${_macroEsc(scenario.rationale || '')}</div>`;
        }).join('');
        const scheduleLabel = event.schedule_method === 'derived' ? 'giờ suy ra từ chính sách công bố' :
            (event.schedule_method === 'recurring' ? 'lịch định kỳ' : 'lịch chính thức');
        const surprise = event.surprise_direction ? ` · Surprise: ${_macroEsc(event.surprise_direction)}` : '';
        const extras = (event.consensus_extra || []).map(x =>
            `<div>${_macroEsc(x.label || '')}: Forecast <b>${_macroEsc(x.forecast || 'Chưa có')}</b> · Previous <b>${_macroEsc(x.previous || 'Chưa có')}</b></div>`).join('');
        const pct = p => (Number(p) * 100).toFixed(0) + '%';
        const oddsHtml = (event.market_odds || []).map(o => {
            const link = _macroHttps(o.url);
            const head = link
                ? `<a class="macro-source" target="_blank" rel="noopener noreferrer" href="${_macroEsc(link)}">${_macroEsc(o.question || 'Kalshi')}</a>`
                : _macroEsc(o.question || 'Kalshi');
            let body = '';
            if (o.kind === 'outcomes') {
                body = (o.outcomes || []).slice(0, 4).map(x =>
                    `<div>${_macroEsc(x.label)}: <b>${pct(x.prob)}</b></div>`).join('');
            } else {
                body = (o.thresholds || []).slice(0, 6).map(t =>
                    `<div>${_macroEsc(t.label)}: <b>${pct(t.prob)}</b></div>`).join('');
                if (o.most_likely) body += `<div>Nhiều khả năng nhất: <b>${_macroEsc(o.most_likely.label)} (${pct(o.most_likely.prob)})</b></div>`;
                if (o.p_above_forecast !== undefined && o.p_above_forecast !== null) {
                    let t = `Cao hơn Forecast: <b>${pct(o.p_above_forecast)}</b>`;
                    if (o.p_inline_forecast !== undefined && o.p_inline_forecast !== null)
                        t += ` · Đúng: <b>${pct(o.p_inline_forecast)}</b> · Thấp hơn: <b>${pct(o.p_below_forecast)}</b>`;
                    else if (o.p_below_forecast !== undefined && o.p_below_forecast !== null)
                        t += ` · Thấp hơn: <b>${pct(o.p_below_forecast)}</b> <span class="macro-meta">(nội suy)</span>`;
                    if (o.lean && o.lean.btc_direction) {
                        const d = _macroDirection(o.lean.btc_direction);
                        t += ` → BTC <span class="${d.cls}">${d.icon} ${d.text}</span>`;
                    }
                    body += `<div>${t}</div>`;
                }
            }
            return `<div class="macro-scenario"><b>🎲 Thị trường dự đoán</b> · ${head}${body}</div>`;
        }).join('') + ((event.market_odds || []).length && event.market_odds_stale ? '<div class="macro-meta">Tỷ lệ từ lần cập nhật trước</div>' : '');
        const consUrl = _macroHttps(event.enrichment_source_url);
        const consProvider = event.enrichment_provider
            ? (consUrl ? `<a class="macro-source" target="_blank" rel="noopener noreferrer" href="${_macroEsc(consUrl)}">${_macroEsc(event.enrichment_provider)}</a>` : _macroEsc(event.enrichment_provider))
            : '';
        const consensusHtml = (extras || consProvider)
            ? `<div class="macro-meta">${event.consensus_label ? '<div>Forecast/Previous = ' + _macroEsc(event.consensus_label) + '</div>' : ''}${extras}${consProvider ? '<div>Consensus: ' + consProvider + '</div>' : ''}</div>`
            : '';
        return `<article class="macro-event ${high?'high':''} ${event.status==='released'?'released':''}">
          <div class="macro-event-top"><div class="macro-event-title">${_macroEsc(event.title || 'Sự kiện')}</div><span class="macro-badge ${high?'high':'medium'}">${_macroEsc(event.impact || 'MEDIUM')}</span></div>
          <div class="macro-meta">${_macroEsc(event.subtitle || '')}<br><b>${_macroEsc(_macroVnTime(event.scheduled_at_utc))}</b> · <span data-macro-time="${_macroEsc(event.scheduled_at_utc || '')}">${_macroEsc(_macroCountdown(event.scheduled_at_utc))}</span><br>
          ${_macroEsc(event.status || 'upcoming')}${tentative?' · ⚠ thời gian tentative':''} · ${_macroEsc(scheduleLabel)}${surprise}<br>Nguồn: ${source}</div>
          <div class="macro-values"><div class="macro-value"><span>Actual</span><b>${_macroEsc(_macroValue(event,'actual'))}</b></div><div class="macro-value"><span>Forecast</span><b>${_macroEsc(_macroValue(event,'forecast'))}</b></div><div class="macro-value"><span>Previous</span><b>${_macroEsc(_macroValue(event,'previous'))}</b></div></div>
          ${consensusHtml}
          ${oddsHtml ? `<div class="macro-scenarios">${oddsHtml}</div>` : ''}
          <div class="macro-scenarios">${scenarioHtml}</div>
          <div class="macro-meta">Theo dõi DXY/lợi suất Mỹ. Xu hướng thường gặp, không đảm bảo; không phải lời khuyên tài chính.</div>
        </article>`;
    }).join('');
    root.innerHTML = `<div class="macro-cal-grid">${cards}</div><button class="macro-more" onclick="toggleMacroCalendarRange()">${_macroCalendarExpanded?'Chỉ xem 14 ngày':'Mở rộng toàn bộ cache'}</button>`;
    _updateMacroCountdowns();
}

// ── TIN TỨC THỊ TRƯỜNG ──────────────────────────────────────
let _newsFilter = 'all';
let _newsLimit = 12;

// map nhãn từ backend -> class CSS + chữ hiển thị
const _NEWS_TAG_MAP = {
    'MACRO':      {cls:'MACRO', txt:'FED / MACRO'},
    'QUY ĐỊNH':   {cls:'REG',   txt:'QUY ĐỊNH'},
    'ETF':        {cls:'ETF',   txt:'ETF'},
    'THANH LÝ':   {cls:'DUMP',  txt:'XẢ MẠNH'},
    'TĂNG MẠNH':  {cls:'PUMP',  txt:'TĂNG MẠNH'},
    'HACK':       {cls:'HACK',  txt:'HACK'},
};

// Thứ tự ưu tiên sort: HIGH trước MEDIUM trước LOW, cùng impact thì mới nhất trước
const _IMPACT_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2};

function _newsAgo(ts) {
    if (!ts) return '–';
    const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
    if (s < 60)    return s + 'gi';
    if (s < 3600)  return Math.floor(s/60) + 'p';
    if (s < 86400) return Math.floor(s/3600) + 'h';
    return Math.floor(s/86400) + 'ng';
}

async function fetchNews(force) {
    try {
        const r = await fetch('/api/news' + (force ? '?force=1' : ''));
        const d = await r.json();
        if (d && d.items) { _newsData = d; renderNews(); }
    } catch(e) {
        const el = document.getElementById('news-list');
        if (el && !_newsData) el.innerHTML =
            `<div style="color:#8b949e;font-size:13px">Không tải được tin tức</div>`;
    }
}

async function refreshNews() {
    const btn = document.getElementById('news-refresh-btn');
    if (btn) { btn.textContent = '⏳ Đang tải...'; btn.disabled = true; }
    await fetchNews(true);
    if (btn) { btn.innerHTML = '&#x21BB; Làm mới'; btn.disabled = false; }
}

function setNewsFilter(f) {
    _newsFilter = f;
    _newsLimit = 12;
    renderNews();
}

function moreNews() { _newsLimit += 15; renderNews(); }

function renderNews() {
    const el = document.getElementById('news-list');
    if (!el || !_newsData) return;
    const all = _newsData.items || [];

    // Đếm cho từng filter
    const cnt = {
        all:    all.length,
        HIGH:   all.filter(n => n.impact === 'HIGH').length,
        MACRO:  all.filter(n => n.tags.includes('MACRO')).length,
        REG:    all.filter(n => n.tags.includes('QUY ĐỊNH')).length,
        ETF:    all.filter(n => n.tags.includes('ETF')).length,
        RISK:   all.filter(n => n.tags.includes('THANH LÝ') || n.tags.includes('HACK')).length,
        BTC:    all.filter(n => n.coins.includes('BTC')).length,
        ETH:    all.filter(n => n.coins.includes('ETH')).length,
    };

    const chips = [
        ['all',   'Tất cả',            cnt.all,   ''],
        ['HIGH',  '🔴 High Impact',     cnt.HIGH,  'impact-chip'],
        ['MACRO', '🏛 Fed/Macro',       cnt.MACRO, ''],
        ['REG',   '⚖️ Quy định',        cnt.REG,   ''],
        ['ETF',   '📊 ETF',             cnt.ETF,   ''],
        ['RISK',  '⚠️ Rủi ro',          cnt.RISK,  ''],
        ['BTC',   'BTC',               cnt.BTC,   ''],
        ['ETH',   'ETH',               cnt.ETH,   ''],
    ];
    const fEl = document.getElementById('news-filters');
    if (fEl) {
        fEl.innerHTML = chips.map(([k, label, c, extra]) =>
            `<div class="news-chip ${extra} ${_newsFilter===k?'active':''}"
                  onclick="setNewsFilter('${k}')">${label} <span style="opacity:.6">${c}</span></div>`
        ).join('');
    }

    // Lọc
    let rows = all;
    if      (_newsFilter === 'HIGH')  rows = all.filter(n => n.impact === 'HIGH');
    else if (_newsFilter === 'MACRO') rows = all.filter(n => n.tags.includes('MACRO'));
    else if (_newsFilter === 'REG')   rows = all.filter(n => n.tags.includes('QUY ĐỊNH'));
    else if (_newsFilter === 'ETF')   rows = all.filter(n => n.tags.includes('ETF'));
    else if (_newsFilter === 'RISK')  rows = all.filter(n => n.tags.includes('THANH LÝ') || n.tags.includes('HACK'));
    else if (_newsFilter === 'BTC' || _newsFilter === 'ETH')
        rows = all.filter(n => n.coins.includes(_newsFilter));

    // Sort: HIGH impact luôn lên đầu, cùng level thì mới nhất trước
    rows = [...rows].sort((a, b) => {
        const ia = _IMPACT_ORDER[a.impact] ?? 2;
        const ib = _IMPACT_ORDER[b.impact] ?? 2;
        if (ia !== ib) return ia - ib;
        return (b.ts || 0) - (a.ts || 0);
    });

    // Cập nhật thời điểm + cảnh báo nguồn lỗi
    const uEl = document.getElementById('news-updated');
    if (uEl) {
        let t = _newsData.updated ? ('cập nhật ' + _newsAgo(_newsData.updated) + ' trước') : '';
        if (_newsData.errors && _newsData.errors.length)
            t += ` · ⚠️ ${_newsData.errors.length} nguồn lỗi`;
        uEl.textContent = t;
    }

    if (rows.length === 0) {
        el.innerHTML = `<div style="color:#8b949e;font-size:13px;padding:10px 0">Không có tin nào khớp bộ lọc</div>`;
        return;
    }

    const shown = rows.slice(0, _newsLimit);
    let html = `<div class="news-list">`;
    shown.forEach(n => {
        const impact   = n.impact || 'LOW';
        const isMacro  = n.tags.includes('MACRO');
        const isDanger = n.tags.includes('THANH LÝ') || n.tags.includes('HACK');
        const isHigh   = impact === 'HIGH';

        // Class cho news-item: high-impact > danger > macro
        const cls = isHigh ? 'high-impact' : (isDanger ? 'danger' : (isMacro ? 'macro' : ''));

        // Impact badge — chỉ hiện HIGH và MEDIUM
        let impactBadge = '';
        if (impact === 'HIGH') {
            impactBadge = `<span class="news-impact-badge HIGH"><span class="impact-dot"></span>HIGH</span>`;
        } else if (impact === 'MEDIUM') {
            impactBadge = `<span class="news-impact-badge MEDIUM">MED</span>`;
        }

        let badges = n.tags.map(t => {
            const m = _NEWS_TAG_MAP[t];
            return m ? `<span class="news-tag ${m.cls}">${m.txt}</span>` : '';
        }).join('');
        badges += n.coins.map(c => `<span class="news-tag COIN">${c}</span>`).join('');

        const safeTitle = (n.title||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        html += `
        <div class="news-item ${cls}">
            <div class="news-time">${_newsAgo(n.ts)}</div>
            <div class="news-body">
                <a class="news-title" href="${n.link}" target="_blank" rel="noopener noreferrer">${safeTitle}</a>
                <div class="news-meta">
                    <span class="news-src">${n.source}</span>
                    ${impactBadge}
                    ${badges}
                </div>
            </div>
        </div>`;
    });
    html += `</div>`;

    if (rows.length > shown.length) {
        html += `
        <div style="text-align:center;margin-top:8px">
          <button onclick="moreNews()"
                  style="background:#0d1117;border:1px solid #30363d;color:#8b949e;border-radius:6px;
                         padding:5px 16px;font-size:12px;cursor:pointer;font-family:inherit">
            &#x25BE; Xem thêm ${rows.length - shown.length} tin
          </button>
        </div>`;
    }
    el.innerHTML = html;
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

// Pump Nhẹ Radar auto-refresh mỗi 10s
setInterval(fetchPumpNhe, 10000);
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

// Pump radar auto-refresh riêng — 5s
setInterval(fetchPump, 5000);
fetchPump();

// PnL stats refresh mỗi 30s (không cần nhanh)
setInterval(fetchPnlStats, 30000);
fetchPnlStats();
// Macro calendar: snapshot-only API every 60s, request coalescing, local countdown every second.
setInterval(fetchMacroCalendar, 60000);
fetchMacroCalendar();
setInterval(_updateMacroCountdowns, 1000);
// Tin RSS đã gỡ khỏi web theo yêu cầu: không còn polling /api/news.

// TradingAgents — check kết quả cũ khi load trang
taCheckLastResult();

function updateClock(){document.getElementById('clock').textContent=new Date().toLocaleTimeString()}

// Lưu state input để không bị reset khi refresh
let _savedInputs = {};
function saveInputs() {
    ['order-symbol','order-side','order-usdt','order-sl','order-tp','order-lev','set-max-usdt','set-leverage','set-max-positions','add-coin-input','pump-coin-input','protected-order-coin-input'].forEach(id => {
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
let _rejOpen = false;        // bảng phễu lọc scan đang mở hay thu gọn

// Đổi hiển thị bảng phễu lọc bằng DOM trực tiếp (không render lại cả dashboard)
function toggleRej() {
    _rejOpen = !_rejOpen;
    const w = document.getElementById('scan-rej-wrap');
    if (w) w.style.display = _rejOpen ? 'block' : 'none';
    const b = w && w.parentElement ? w.parentElement.querySelector('button') : null;
    if (b) {
        const n = w ? w.querySelectorAll('tr').length - 1 : 0;
        b.textContent = _rejOpen ? '▴ Ẩn chi tiết' : '▾ Xem chi tiết ' + n + ' coin';
    }
}

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
            // Di chuyển TV chart vào placeholder (nằm ngay dưới Open Positions)
            (function() {
                const placeholder = document.getElementById('tv-chart-placeholder');
                const chartEl = document.getElementById('tv-chart-section');
                if (placeholder && chartEl) {
                    placeholder.appendChild(chartEl);
                    chartEl.style.margin = '0 0 12px 0';
                }
            })();
            _pumpRendered = false;  // pump-radar-root vừa được tạo lại → cần render lại
            restoreInputs();
            _firstRender = false;
            fetchProtectedPendingCoins();
            // Các section render riêng vừa bị dựng lại rỗng → vẽ lại từ data đã có,
            // không gọi API lần nữa (tránh chờ và tránh thêm tải).
            if (_macroCalendarData) renderMacroCalendar();
            if (_newsData) renderNews();
            if (_pnlData)  renderPnlStats();
            updatePPMonitor(d);  // vẽ tier progression ngay lần load đầu
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

    // Canonical account/PnL cards — patch textContent không flash.
    const statIds = ['stat-wallet','stat-available','stat-margin','stat-today-pnl','stat-period-pnl','stat-unrealized','stat-winrate','stat-trades'];
    const statVals = [
        fmtUsd(d.wallet_balance),
        fmtUsd(d.available_balance),
        fmtUsd(d.margin_balance),
        fmtUsd(d.today_net_pnl),
        fmtUsd(d.period_net_pnl),
        fmtUsd(d.unrealized),
        fmt(d.win_rate,0)+'%',
        String(d.closed_cycles)
    ];
    statIds.forEach((id,i) => _setText(id, statVals[i]));
    _setText('ledger-gross', fmtUsd(d.gross));
    _setText('ledger-fees', '-' + fmtUsd(Math.abs(d.commission)));
    _setText('ledger-funding', fmtUsd(d.funding));
    _setText('ledger-transfer', fmtUsd(d.transfer));
    const ledgerOk = d.financial_source === 'binance';
    const syncState = d.syncing ? 'Syncing' : (d.stale ? 'Stale' : 'Fresh');
    const syncedLabel = d.synced_at ? new Date(d.synced_at).toLocaleString('vi-VN') : 'chưa sync';
    const financialWarning = (d.financial_warnings || []).join(' · ');
    _setText('ledger-sync', `${ledgerOk ? 'Binance ledger' : '⚠ LEGACY FALLBACK'} · ${syncState} · ${syncedLabel}${d.window_start ? ' · từ '+new Date(d.window_start).toLocaleDateString('vi-VN') : ''}${financialWarning ? ' · ⚠ '+financialWarning : ''}`);

    // Scan info
    _setText('scan-info', `Scan #${d.scan_no} | Last: ${d.last_scan}`);

    // Open positions — rebuild nhỏ hơn
    const posEl = document.getElementById('positions-body');
    if (posEl) {
        let rows = '';
        if (d.open_positions && d.open_positions.length > 0) {
            d.open_positions.forEach(p => {
                const ppInfo = d.pp_state && d.pp_state[p.symbol];
                const tierBadge = ppTierBadgeHtml(ppInfo);
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
setInterval(refresh, 5000);  // 5s - đủ nhanh, giảm tải browser
updateClock();
refresh();

// Init chart + WebSocket after DOM loaded
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => updateTVChart());
} else {
    updateTVChart();
}

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
        set('pp-tier4-threshold', s.tier4_trigger_pct);
        set('pp-tier4-timer',   s.tier4_timer_secs);
        set('pp-tier4-dist',    s.tier4_trail_dist_pct);
        set('pp-tier5-threshold', s.tier5_trigger_pct);
        set('pp-tier5-timer',   s.tier5_timer_secs);
        set('pp-tier5-dist',    s.tier5_trail_dist_pct);
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
    const cfg = d.pp_settings || {
        t2_trigger_pct:0.6, t2_timer_secs:5, t2_lock_pct:0.4,
        t3_trigger_pct:1.0, t3_timer_secs:3, t3_distance_pct:0.5,
        t4_trigger_pct:2.0, t4_timer_secs:3, t4_distance_pct:0.3,
        t5_trigger_pct:3.0, t5_timer_secs:3, t5_distance_pct:0.15
    };
    const serverTs = Number(d.server_ts || (Date.now()/1000));
    const tierDefs = {
        2:{label:'T2 Protection', icon:'🛡', color:'#d29922', trigger:Number(cfg.t2_trigger_pct), timer:Number(cfg.t2_timer_secs), distance:null},
        3:{label:'T3 Trailing',   icon:'🎯', color:'#3fb950', trigger:Number(cfg.t3_trigger_pct), timer:Number(cfg.t3_timer_secs), distance:Number(cfg.t3_distance_pct)},
        4:{label:'T4 Tight',      icon:'⚡', color:'#58a6ff', trigger:Number(cfg.t4_trigger_pct), timer:Number(cfg.t4_timer_secs), distance:Number(cfg.t4_distance_pct)},
        5:{label:'T5 Max',        icon:'🔥', color:'#f0883e', trigger:Number(cfg.t5_trigger_pct), timer:Number(cfg.t5_timer_secs), distance:Number(cfg.t5_distance_pct)}
    };

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
        const tierColors = {1:'#484f58', 2:'#d29922', 3:'#3fb950', 4:'#58a6ff', 5:'#f0883e'};
        const tierIcons  = {1:'T1', 2:'T2🛡', 3:'T3🎯', 4:'T4⚡', 5:'T5🔥'};
        const tierColor  = tierColors[tier] || '#484f58';
        const tierLabel  = tierIcons[tier]  || `T${tier}`;
        const tierBadge  = `<span style="color:${tierColor};font-weight:700">${tierLabel}</span>`;

        const sl = ps.current_sl || 0;
        const peak = ps.peak || 0;
        const trailSL = ps.trailing_sl || 0;
        const profitColor = profit >= 0 ? '#3fb950' : '#f85149';

        const fmt = (v) => v >= 1 ? '$'+v.toFixed(4) : (v > 0 ? '$'+v.toFixed(6) : '-');

        // ── Current policy + exact progress to next tier ──
        const activeDef = tierDefs[tier];
        let activePolicy = 'Initial SL đang bảo vệ';
        if (tier === 2) {
            activePolicy = `Protection SL · khóa entry ${Number(cfg.t2_lock_pct).toFixed(2)}%`;
        } else if (tier >= 3 && activeDef) {
            activePolicy = `Trailing active · cách peak ${activeDef.distance.toFixed(2)}%`;
        }
        const lastUpdateAge = Number(ps.sl_last_update_ts || 0) > 0
            ? Math.max(0, serverTs - Number(ps.sl_last_update_ts)).toFixed(0)
            : null;
        const activeHtml = `
            <div style="font-size:10px;color:${tierColor};font-weight:700">${activePolicy}</div>
            ${lastUpdateAge !== null ? `<div style="font-size:9px;color:#6e7681">SL update ${lastUpdateAge}s trước</div>` : ''}`;

        let progressHtml = '';
        if (tier >= 5) {
            progressHtml = `
                <div style="font-size:10px;color:#f0883e;font-weight:700">🔥 MAX TIER — T5 đang chạy</div>
                <div style="font-size:9px;color:#8b949e">Trigger ${tierDefs[5].trigger.toFixed(2)}% · timer ${tierDefs[5].timer}s · dist ${tierDefs[5].distance.toFixed(2)}%</div>`;
        } else {
            const nextTier = tier + 1;
            const next = tierDefs[nextTier];
            const timerFields = {2:'protection_ts', 3:'trailing_ts', 4:'tier4_ts', 5:'tier5_ts'};
            const timerStarted = Number(ps[timerFields[nextTier]] || 0);
            const reached = profit >= next.trigger;
            const remaining = Math.max(0, next.trigger - profit);
            const profitPct = Math.max(0, Math.min(100, next.trigger > 0 ? profit / next.trigger * 100 : 0));
            const elapsed = timerStarted > 0 ? Math.max(0, serverTs - timerStarted) : 0;
            const timerPct = reached && timerStarted > 0
                ? Math.max(0, Math.min(100, next.timer > 0 ? elapsed / next.timer * 100 : 100)) : 0;
            let statusText = `Còn ${remaining.toFixed(2)}% lợi nhuận để kích hoạt`;
            let statusColor = profit >= 0 ? '#8b949e' : '#f85149';
            if (reached && timerStarted <= 0) {
                statusText = 'Đã đạt ngưỡng · chờ monitor bắt đầu timer';
                statusColor = '#d29922';
            } else if (reached && elapsed < next.timer) {
                statusText = `Đang xác nhận ${elapsed.toFixed(1)}s / ${next.timer}s`;
                statusColor = '#d29922';
            } else if (reached) {
                statusText = `Timer đủ ${next.timer}s · chờ SL mới hợp lệ để pass`;
                statusColor = '#3fb950';
            }
            const nextPolicy = nextTier === 2
                ? `Protection lock +${Number(cfg.t2_lock_pct).toFixed(2)}% trên entry`
                : `Trailing distance ${next.distance.toFixed(2)}% từ peak`;
            progressHtml = `
                <div style="min-width:210px">
                    <div style="display:flex;justify-content:space-between;gap:8px;font-size:10px">
                        <b style="color:${next.color}">${next.icon} Tiếp theo: ${next.label}</b>
                        <span style="color:#c9d1d9">${profit.toFixed(2)}% / ${next.trigger.toFixed(2)}%</span>
                    </div>
                    <div style="background:#21262d;border-radius:4px;height:6px;margin-top:3px;overflow:hidden">
                        <div style="background:${next.color};height:6px;width:${profitPct}%;transition:width .4s"></div>
                    </div>
                    <div style="font-size:9px;color:${statusColor};margin-top:3px">${statusText}</div>
                    ${reached ? `<div style="background:#21262d;border-radius:3px;height:3px;margin-top:2px;overflow:hidden"><div style="background:#d29922;height:3px;width:${timerPct}%"></div></div>` : ''}
                    <div style="font-size:9px;color:#6e7681;margin-top:2px">${nextPolicy}</div>
                </div>`;
        }

        rows += `<tr style="border-bottom:1px solid #21262d">
            <td style="padding:3px 6px"><b>${p.symbol.replace('USDT','')}</b></td>
            <td style="padding:3px 6px">${p.side === 'LONG' ? '<span style="color:#3fb950">LONG</span>' : '<span style="color:#f85149">SHORT</span>'}</td>
            <td style="padding:3px 6px;color:${profitColor}"><b>${profit.toFixed(2)}%</b></td>
            <td style="padding:5px 8px;min-width:155px">${tierBadge}${activeHtml}</td>
            <td style="padding:5px 8px;min-width:225px">${progressHtml}</td>
            <td style="padding:3px 6px;color:#f85149">${fmt(sl)}</td>
            <td style="padding:3px 6px;color:#58a6ff">${fmt(peak)}</td>
            <td style="padding:3px 6px;color:#d29922">${trailSL > 0 ? fmt(trailSL) : '-'}</td>
        </tr>`;
    });

    if (!rows) {
        el.innerHTML = '<span style="color:#484f58">Chưa có position nào kích hoạt PP</span>';
        return;
    }

    const ladderHtml = `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
        ${[2,3,4,5].map(t => {
            const x = tierDefs[t];
            const policy = t === 2 ? `lock +${Number(cfg.t2_lock_pct).toFixed(2)}% entry` : `dist ${x.distance.toFixed(2)}%`;
            return `<span style="font-size:9px;padding:3px 6px;border:1px solid ${x.color}55;border-radius:5px;color:${x.color};background:${x.color}10"><b>${x.icon} T${t}</b> ≥${x.trigger.toFixed(2)}% · ${x.timer}s · ${policy}</span>`;
        }).join('')}
    </div>`;
    el.innerHTML = `${ladderHtml}<div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
      <table style="width:100%;min-width:900px;border-collapse:collapse">
        <tr style="color:#484f58;font-size:10px">
            <th style="text-align:left;padding:2px 6px">Coin</th>
            <th style="padding:2px 6px">Side</th>
            <th style="padding:2px 6px">Lợi hiện tại</th>
            <th style="padding:2px 6px">Tier đang chạy</th>
            <th style="padding:2px 6px">Điều kiện pass tier kế</th>
            <th style="padding:2px 6px">SL hiện tại</th>
            <th style="padding:2px 6px">Peak</th>
            <th style="padding:2px 6px">Trailing SL</th>
        </tr>
        ${rows}
      </table>
    </div>`;
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
        tier4_trigger_pct:     parseFloat(get('pp-tier4-threshold')),
        tier4_timer_secs:      parseInt(get('pp-tier4-timer')),
        tier4_trail_dist_pct:  parseFloat(get('pp-tier4-dist')),
        tier5_trigger_pct:     parseFloat(get('pp-tier5-threshold')),
        tier5_timer_secs:      parseInt(get('pp-tier5-timer')),
        tier5_trail_dist_pct:  parseFloat(get('pp-tier5-dist')),
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
        // Gate lọc scan
        set('p0-vol-enabled',    s.entry_vol_confirm_enabled);
        set('p0-vol-ratio',      s.entry_min_vol_ratio);
        set('p0-pb-vol-ratio',   s.pullback_min_vol_ratio);
        set('p0-loc-enabled',    s.location_filter_enabled);
        set('p0-loc-room',       s.location_min_room_atr);
        set('p0-regime-enabled', s.regime_filter_enabled);
        set('p0-trend-conflict', s.trend_conflict_skip);
        set('p0-conf-min',       s.entry_min_confluence);
        set('p0-conf-edge',      s.entry_min_confluence_edge);
        set('p0-pb-self',        s.pullback_self_relative);
        set('p0-pb-self-ratio',  s.pullback_self_min_ratio);
        set('p0-pb-abs',         s.pullback_vol_threshold);
        updateGateHints();
        updateRiskNote();
    } catch(e) {}
}

// Gợi ý theo số đo thật: đặt ngưỡng này thì bao nhiêu % nến / coin qua được
function updateGateHints() {
    // volume/MA20 — phân vị đo từ 8280 nến 15m trên 46 coin
    const volTable = [[0.5,79],[0.6,69],[0.7,60],[0.8,50],[0.9,43],[1.0,36],[1.2,26]];
    const v = parseFloat(document.getElementById('p0-vol-ratio')?.value);
    const vh = document.getElementById('p0-vol-hint');
    if (vh && !isNaN(v)) {
        let best = volTable[0];
        volTable.forEach(t => { if (Math.abs(t[0]-v) < Math.abs(best[0]-v)) best = t; });
        const warn = v >= 1.0;
        vh.innerHTML = `≈ <b style="color:${warn?'#d29922':'#3fb950'}">${best[1]}%</b> nến qua`
                     + (warn ? ' <span style="color:#d29922">(chặt)</span>' : '');
    }
    // room tới S/R — phân bố đo thật 0.4-1.4×ATR
    const rm = parseFloat(document.getElementById('p0-loc-room')?.value);
    const rh = document.getElementById('p0-loc-hint');
    if (rh && !isNaN(rm)) {
        if (rm >= 1.5)      rh.innerHTML = '<span style="color:#f85149">quá chặt — từng cho 0 PASS</span>';
        else if (rm >= 1.2) rh.innerHTML = '<span style="color:#d29922">chặt</span>';
        else if (rm >= 0.6) rh.innerHTML = '<span style="color:#3fb950">hợp lý</span>';
        else                rh.innerHTML = '<span style="color:#8b949e">lỏng</span>';
    }
    // pullback: giải thích chế độ đang chọn
    const selfOn = document.getElementById('p0-pb-self')?.checked;
    const ratio  = parseFloat(document.getElementById('p0-pb-self-ratio')?.value);
    const absv   = parseFloat(document.getElementById('p0-pb-abs')?.value);
    const ph = document.getElementById('p0-pb-hint');
    if (ph) {
        if (selfOn) {
            ph.innerHTML = `Đang so với <b style="color:#3fb950">chính coin đó</b>: `
              + `coin được dùng pullback nếu ATR hiện tại ≥ <b>${isNaN(ratio)?'?':ratio}</b>× `
              + `trung vị ATR riêng của nó. Ô tuyệt đối bên cạnh không dùng.<br>`
              + `Coin biến động thấp (TSLA 0.8%, XAU 1.1%, SPCX 1.2%) không còn bị khoá vĩnh viễn.`;
        } else {
            ph.innerHTML = `<span style="color:#d29922">Đang dùng ngưỡng tuyệt đối ${isNaN(absv)?'?':absv}%</span> `
              + `(cách cũ). Đo thật: khoá 8 coin, <b>5 con trong đó CÓ setup pullback hợp lệ</b>. `
              + `Coin ATR thấp bị khoá vĩnh viễn vì không bao giờ chạm nổi ngưỡng.`;
        }
    }
}

function updateRiskNote() {
    const note = document.getElementById('p0-risk-note');
    if (!note) return;
    const riskPct  = parseFloat(document.getElementById('p0-risk-pct')?.value) || 1.0;
    const maxOrder = parseFloat(document.getElementById('p0-max-order')?.value) || 50;
    // Lấy balance từ dashboard
    const balEl = document.getElementById('stat-wallet');
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
        // Gate lọc scan
        entry_vol_confirm_enabled: get('p0-vol-enabled'),
        entry_min_vol_ratio:       parseFloat(get('p0-vol-ratio')),
        pullback_min_vol_ratio:    parseFloat(get('p0-pb-vol-ratio')),
        location_filter_enabled:   get('p0-loc-enabled'),
        location_min_room_atr:     parseFloat(get('p0-loc-room')),
        regime_filter_enabled:     get('p0-regime-enabled'),
        trend_conflict_skip:       get('p0-trend-conflict'),
        entry_min_confluence:      parseInt(get('p0-conf-min')),
        entry_min_confluence_edge: parseInt(get('p0-conf-edge')),
        pullback_self_relative:    get('p0-pb-self'),
        pullback_self_min_ratio:   parseFloat(get('p0-pb-self-ratio')),
        pullback_vol_threshold:    parseFloat(get('p0-pb-abs')),
    };
    // Bỏ field NaN để không ghi rác vào config khi input trống
    Object.keys(payload).forEach(k => {
        if (typeof payload[k] === 'number' && isNaN(payload[k])) delete payload[k];
    });
    const r = await apiPost('/api/p0/settings', payload);
    const msg = document.getElementById('p0-save-msg');
    if (msg) {
        msg.textContent = r.ok ? '✅ Đã lưu' : '❌ ' + (r.msg || 'Lỗi');
        msg.style.color = r.ok ? '#3fb950' : '#f85149';
        setTimeout(() => { if (msg) msg.textContent = ''; }, 3000);
    }
    if (r.ok) updateGateHints();
}

// ══════════════════════════════════════════════════════════════════
// COINGLASS LIQUIDATION HEATMAP
// ══════════════════════════════════════════════════════════════════

</script>
</body>
</html>"""


@app.before_request
def check_auth():
    """
    Chặn mọi request chưa đăng nhập (trừ /login, /logout, /static).

    Trước đây hàm này set session["authenticated"] = True cho MỌI request
    → mật khẩu vô hiệu hoàn toàn. Đây là lớp chặn chính, đặt ở đây để
    route nào quên gắn @require_auth vẫn được bảo vệ.
    """
    if session.get("authenticated"):
        return None
    p = request.path or "/"
    if p.startswith("/login") or p.startswith("/logout") or p.startswith("/static"):
        return None
    if p.startswith("/api/"):
        return jsonify({"ok": False, "error": "unauthorized",
                        "login_required": True}), 401
    return redirect(url_for("login"))
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

    financial = _financial_snapshot(tlog, s)
    all_closed = _closed_cycles(financial)
    outcomes = _complete_closed_cycles(financial)
    financial_events = _financial_events(financial)
    totals = _financial_totals(financial, financial_events, outcomes)
    today = datetime.now().strftime("%Y-%m-%d")
    today_events = [
        event for event in financial_events
        if str(event.get("time", "")).startswith(today)
    ]
    today_totals = _event_totals(today_events)
    today_net_pnl = today_totals["period_net_pnl"]
    unrealized = sum(p.get("_pnl", 0) for p in open_pos)
    account = financial.get("account", {})

    open_fmt = []
    for p in open_pos:
        amt = float(p.get("positionAmt", 0))
        open_fmt.append({"symbol": p.get("symbol",""), "side": "LONG" if amt > 0 else "SHORT",
            "entry": float(p.get("entryPrice",0)), "mark": p.get("_mark",0),
            "pnl": p.get("_pnl",0), "pct": p.get("_pct",0), "lev": p.get("_lev",10)})

    # Pending orders — đọc từ state cache (không cần lock riêng)
    pending_orders = list(s.get("pending_orders_cache", []))

    # Keep incomplete rows visible and labelled, but never include them in the
    # canonical complete-outcome denominator or win rate.
    recent = list(reversed(all_closed[-15:]))
    trades_fmt = [{
        "symbol": c.get("symbol", ""), "side": c.get("side", ""),
        "entry": c.get("entry_price", 0), "close": c.get("close_price", 0),
        "pnl": c.get("net_pnl"), "pct": 0,
        "time": c.get("closed_at", ""), "complete": bool(c.get("complete", False)),
        "net_complete": bool(c.get("net_complete", c.get("net_pnl") is not None)),
        "incomplete_reason": (
            "window_boundary" if c.get("boundary_start")
            else "funding_or_commission" if not c.get("net_complete", True)
            else ""
        ),
        "cycle_id": c.get("cycle_id", ""),
    } for c in recent]

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
        "balance": account.get("wallet_balance", 0),
        "wallet_balance": account.get("wallet_balance", 0),
        "margin_balance": account.get("margin_balance", 0),
        "available_balance": account.get("available_balance", 0),
        "today_pnl": today_net_pnl,
        "today_net_pnl": today_net_pnl,
        "total_pnl": totals["period_net_pnl"],
        "period_net_pnl": totals["period_net_pnl"],
        "gross": totals["gross"],
        "commission": totals["commission"],
        "funding": totals["funding"],
        "transfer": totals["transfer"],
        "unrealized": unrealized,
        "win_rate": totals["win_rate"],
        "wins": totals["wins"], "losses": totals["losses"],
        "total_trades": totals["closed_cycles"],
        "closed_cycles": totals["closed_cycles"],
        "incomplete_cycles": totals["incomplete_cycles"],
        "net_complete": totals["net_complete"],
        "commission_complete": totals["commission_complete"],
        "incomplete_financial_events": totals["incomplete_event_count"],
        "financial_source": financial.get("source", "legacy"),
        "source": financial.get("source", "legacy"),
        "synced_at": financial.get("synced_at", ""),
        "stale": bool(financial.get("stale", True)),
        "syncing": bool(financial.get("syncing", False)),
        "errors": financial.get("errors", []),
        "window_start": financial.get("window_start", ""),
        "window_clipped": bool(financial.get("window_clipped", False)),
        "non_usdt_commission_assets": financial.get("non_usdt_commission_assets", []),
        "ambiguous_funding_event_ids": financial.get("ambiguous_funding_event_ids", []),
        "financial_warnings": financial.get("financial_warnings", []),
        # Profit Protection presentation snapshot. This mirrors the exact
        # runtime config used by bot.py; it does not alter tier logic.
        "server_ts": time.time(),
        "pp_settings": {
            "t2_trigger_pct": getattr(_config, "PP_TRIGGER_PCT", 0.6),
            "t2_timer_secs": getattr(_config, "PP_TIMER_SECS", 5),
            "t2_lock_pct": (
                getattr(_config, "PP_FEE_BUFFER_PCT", 0.15)
                + getattr(_config, "PP_PROTECTION_BUFFER_PCT", 0.25)
            ),
            "t3_trigger_pct": getattr(_config, "PP_TRAILING_TRIGGER_PCT", 1.0),
            "t3_timer_secs": getattr(_config, "PP_TRAILING_TIMER_SECS", 3),
            "t3_distance_pct": getattr(_config, "PP_TRAILING_DISTANCE_PCT", 0.5),
            "t4_trigger_pct": getattr(_config, "PP_TIER4_TRIGGER_PCT", 2.0),
            "t4_timer_secs": getattr(_config, "PP_TIER4_TIMER_SECS", 3),
            "t4_distance_pct": getattr(_config, "PP_TIER4_TRAIL_DIST_PCT", 0.3),
            "t5_trigger_pct": getattr(_config, "PP_TIER5_TRIGGER_PCT", 3.0),
            "t5_timer_secs": getattr(_config, "PP_TIER5_TIMER_SECS", 3),
            "t5_distance_pct": getattr(_config, "PP_TIER5_TRAIL_DIST_PCT", 0.15),
        },
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
                         "peak_price": v.get("peak_price",0),
                         "tp": v.get("tp", 0),
                         "protection_ts": v.get("protection_ts",0),
                         "trailing_ts": v.get("trailing_ts",0),
                         "tier4_ts": v.get("tier4_ts",0),
                         "tier5_ts": v.get("tier5_ts",0),
                         "sl_last_update_ts": v.get("sl_last_update_ts",0)}
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
        "armed_entry_ttl_secs":     getattr(_config, "ARMED_ENTRY_TTL_SECS", 3600),
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
        "profit_lock_high_pct":       getattr(_config, "PROFIT_LOCK_HIGH_PCT", 30.0),
        "profit_lock_speed_pct":      getattr(_config, "PROFIT_LOCK_SPEED_PCT", 1.5),
        "max_loss_enabled":         getattr(_config, "MAX_LOSS_ENABLED", True),
        "max_loss_value":           getattr(_config, "MAX_LOSS_PER_POSITION", 20.0),
        "candidates": [{"symbol": c.symbol, "signal": c.signal, "score": c.score,
                         "rsi": c.rsi, "trend": c.trend, "reason": c.reason,
                         "price": prices.get(c.symbol, 0)}
                        for c in candidates[:10]] if candidates else [],
        # Phễu lọc: coin đã quét nhưng bị loại + lý do. Chỉ để xem.
        "scan_rejected": list(_state.get("scan_rejected", []))[:60],
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


@app.route("/api/protected-pending-coins", methods=["GET"])
@require_auth
def api_protected_pending_coins():
    """Return symbols whose manual pending orders automatic cleanup preserves."""
    if _state is None or _lock is None:
        return jsonify({"ok": False, "msg": "Bot chưa khởi động"}), 503
    with _lock:
        coins = list(_state.get("protected_pending_order_coins", []))
    return jsonify({"ok": True, "coins": coins})


@app.route("/api/protected-pending-coins/add", methods=["POST"])
@require_auth
def api_add_protected_pending_coin():
    if _state is None or _lock is None:
        return jsonify({"ok": False, "msg": "Bot chưa khởi động"}), 503
    try:
        symbol = _normalize_protected_pending_symbol(
            (request.get_json() or {}).get("symbol", "")
        )
    except ValueError as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 400

    with _protected_pending_file_lock:
        with _lock:
            current = list(_state.get("protected_pending_order_coins", []))
            if symbol in current:
                return jsonify({"ok": False, "msg": f"{symbol} đã được bảo vệ"})
            updated = current + [symbol]
            if not _save_protected_pending_coins_unlocked(updated):
                return jsonify({"ok": False, "msg": "Không lưu được danh sách"}), 500
            _state["protected_pending_order_coins"] = updated
            _config.PROTECTED_PENDING_ORDER_COINS = list(updated)

    logger.info(f"[OrderProtect] Added {symbol}")
    return jsonify({"ok": True, "msg": f"🛡 Đã bảo vệ order {symbol}", "coins": updated})


@app.route("/api/protected-pending-coins/remove", methods=["POST"])
@require_auth
def api_remove_protected_pending_coin():
    if _state is None or _lock is None:
        return jsonify({"ok": False, "msg": "Bot chưa khởi động"}), 503
    try:
        symbol = _normalize_protected_pending_symbol(
            (request.get_json() or {}).get("symbol", "")
        )
    except ValueError as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 400

    with _protected_pending_file_lock:
        with _lock:
            current = list(_state.get("protected_pending_order_coins", []))
            if symbol not in current:
                return jsonify({"ok": False, "msg": f"{symbol} không có trong danh sách"})
            updated = [coin for coin in current if coin != symbol]
            if not _save_protected_pending_coins_unlocked(updated):
                return jsonify({"ok": False, "msg": "Không lưu được danh sách"}), 500
            _state["protected_pending_order_coins"] = updated
            _config.PROTECTED_PENDING_ORDER_COINS = list(updated)

    logger.info(f"[OrderProtect] Removed {symbol}")
    return jsonify({"ok": True, "msg": f"Đã bỏ bảo vệ {symbol}", "coins": updated})


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

            # Bulk cleanup never overrides the protected list. Exact row-level
            # cancel remains the explicit manual override.
            with _lock:
                if _web_is_protected_pending_symbol(sym):
                    kept.append(f"{sym} {otype} (protected)")
                    continue

                # Chỉ huỷ đúng ENTRY LIMIT không có vị thế. Keep the
                # protection lock held across check + destructive request so
                # an add request cannot return before an older delete finishes.
                if otype == "LIMIT" and not reduce and sym not in open_syms:
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
               + (f"\n✅ Giữ lại {len(kept)} lệnh protected/có vị thế" if kept else ""))
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
    """Pause/Resume bot trading.

    Model: chỉ bật/tắt cờ `paused`. KHÔNG đụng `running` (running=True suốt vòng đời process).
    - paused=True  → các thread trade skip (không vào lệnh mới), thread bảo vệ/hạ tầng vẫn chạy.
    - paused=False → trade lại bình thường.
    """
    with _lock:
        current = _state.get("running", True)
        is_paused = _state.get("paused", False)

    if not current or is_paused:
        # Đang tắt/paused → START: gọi callback tạo lại TẤT CẢ thread (bao gồm Telegram)
        restart_fn = _state.get("_restart_fn")
        if restart_fn:
            try:
                with _lock:
                    _state["running"] = True
                    _state["paused"]  = False
                restart_fn()
                return jsonify({"ok": True, "msg": "Bot started ✅", "running": True})
            except Exception as e:
                return jsonify({"ok": False, "msg": f"Start failed: {e}", "running": False})
        else:
            with _lock:
                _state["running"] = True
                _state["paused"]  = False
            return jsonify({"ok": True, "msg": "Bot resumed", "running": True})
    else:
        # Đang chạy → PAUSE: tắt HẾT thread (trade + protect + telegram)
        with _lock:
            _state["running"] = False
            _state["paused"]  = True
        logger.info("Bot paused via web (running=False → all threads stop)")
        return jsonify({"ok": True, "msg": "Bot paused ⏸ (tắt hết)", "running": False})


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
        from trade_history import save_history
        save_history(list(_state.get("trade_log", [])))

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
        from trade_history import save_history
        save_history(list(_state.get("trade_log", [])))

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
            _exchange.place_market_order(
                symbol, close_side, batch, reduce_only=True
            )
            remaining -= batch

        broadly_cancelled = _web_guarded_symbol_cancel(
            symbol, close_cleanup=True
        )
        if not broadly_cancelled:
            logger.info(
                f"[WebClose] Preserved protected entries and cleaned stale exits for {symbol}"
            )

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
                _state.setdefault("trade_log", []).append({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": symbol, "side": side_pos,
                    "entry": entry, "close": close_price,
                    "qty": qty, "status": "CLOSED",
                    "pnl_usdt": round(pnl_usd, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "note": "closed_web"
                })
                from trade_history import save_history
                save_history(list(_state.get("trade_log", [])))

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
        from trade_history import save_history
        save_history(list(_state.get("trade_log", [])))

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

@app.route("/api/armed_ttl", methods=["POST"])
@require_auth
def api_armed_ttl():
    """Config thời gian hết hạn Armed Entry (giây)."""
    data = request.get_json() or {}
    try:
        ttl = int(data.get("ttl_secs", 3600))
        ttl = max(600, min(28800, ttl))  # 10 phút → 8 giờ
        _config.ARMED_ENTRY_TTL_SECS = ttl
        # Persist vào config.py
        import os, re as _re
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
            content = _re.sub(r"(?m)^ARMED_ENTRY_TTL_SECS\s*=\s*.+$",
                               f"ARMED_ENTRY_TTL_SECS = {ttl}", content)
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as ex:
            logger.warning(f"[ArmedTTL] Cannot persist: {ex}")
        mins = ttl // 60
        return jsonify({"ok": True, "msg": f"⏰ Armed TTL: {mins} phút", "ttl_secs": ttl})
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
            _config.PROFIT_LOCK_MIN_PCT = max(0.5, min(50.0, float(data["min_pct"])))
        if "high_pct" in data:
            _config.PROFIT_LOCK_HIGH_PCT = max(5.0, min(100.0, float(data["high_pct"])))
        if "speed_pct" in data:
            _config.PROFIT_LOCK_SPEED_PCT = max(0.1, min(10.0, float(data["speed_pct"])))
        
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
        high_pct = getattr(_config, "PROFIT_LOCK_HIGH_PCT", 30.0)
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
            # ── Gate lọc scan (chỉnh được từ web) ──
            "entry_vol_confirm_enabled": getattr(_cfg, "ENTRY_VOL_CONFIRM_ENABLED", True),
            "entry_min_vol_ratio":       getattr(_cfg, "ENTRY_MIN_VOL_RATIO",       0.8),
            "pullback_min_vol_ratio":    getattr(_cfg, "PULLBACK_MIN_VOL_RATIO",    0.8),
            "location_filter_enabled":   getattr(_cfg, "LOCATION_FILTER_ENABLED",   True),
            "location_min_room_atr":     getattr(_cfg, "LOCATION_MIN_ROOM_ATR",     0.8),
            "regime_filter_enabled":     getattr(_cfg, "REGIME_FILTER_ENABLED",     True),
            "trend_conflict_skip":       getattr(_cfg, "TREND_CONFLICT_SKIP",       False),
            "entry_min_confluence":      getattr(_cfg, "ENTRY_MIN_CONFLUENCE",      5),
            "entry_min_confluence_edge": getattr(_cfg, "ENTRY_MIN_CONFLUENCE_EDGE", 2),
            # Điều kiện mở đường pullback
            "pullback_self_relative":    getattr(_cfg, "PULLBACK_SELF_RELATIVE",   True),
            "pullback_self_min_ratio":   getattr(_cfg, "PULLBACK_SELF_MIN_RATIO",  0.7),
            "pullback_vol_threshold":    getattr(_cfg, "PULLBACK_VOL_THRESHOLD",   4.0),
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

        # ── Gate lọc scan ─────────────────────────────────────────────
        if "entry_vol_confirm_enabled" in data:
            _cfg.ENTRY_VOL_CONFIRM_ENABLED = bool(data["entry_vol_confirm_enabled"])
        if "entry_min_vol_ratio" in data:
            _cfg.ENTRY_MIN_VOL_RATIO    = max(0.0, min(3.0, float(data["entry_min_vol_ratio"])))
        if "pullback_min_vol_ratio" in data:
            _cfg.PULLBACK_MIN_VOL_RATIO = max(0.0, min(3.0, float(data["pullback_min_vol_ratio"])))
        if "location_filter_enabled" in data:
            _cfg.LOCATION_FILTER_ENABLED = bool(data["location_filter_enabled"])
        if "location_min_room_atr" in data:
            _cfg.LOCATION_MIN_ROOM_ATR  = max(0.0, min(5.0, float(data["location_min_room_atr"])))
        if "regime_filter_enabled" in data:
            _cfg.REGIME_FILTER_ENABLED  = bool(data["regime_filter_enabled"])
        if "trend_conflict_skip" in data:
            _cfg.TREND_CONFLICT_SKIP    = bool(data["trend_conflict_skip"])
        if "entry_min_confluence" in data:
            _cfg.ENTRY_MIN_CONFLUENCE   = max(0, min(10, int(data["entry_min_confluence"])))
        if "entry_min_confluence_edge" in data:
            _cfg.ENTRY_MIN_CONFLUENCE_EDGE = max(0, min(6, int(data["entry_min_confluence_edge"])))
        if "pullback_self_relative" in data:
            _cfg.PULLBACK_SELF_RELATIVE  = bool(data["pullback_self_relative"])
        if "pullback_self_min_ratio" in data:
            _cfg.PULLBACK_SELF_MIN_RATIO = max(0.0, min(2.0, float(data["pullback_self_min_ratio"])))
        if "pullback_vol_threshold" in data:
            _cfg.PULLBACK_VOL_THRESHOLD  = max(0.0, min(20.0, float(data["pullback_vol_threshold"])))

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
            # Gate lọc scan
            "ENTRY_VOL_CONFIRM_ENABLED": str(getattr(_cfg, "ENTRY_VOL_CONFIRM_ENABLED", True)),
            "ENTRY_MIN_VOL_RATIO":       str(round(getattr(_cfg, "ENTRY_MIN_VOL_RATIO", 0.8), 2)),
            "PULLBACK_MIN_VOL_RATIO":    str(round(getattr(_cfg, "PULLBACK_MIN_VOL_RATIO", 0.8), 2)),
            "LOCATION_FILTER_ENABLED":   str(getattr(_cfg, "LOCATION_FILTER_ENABLED", True)),
            "LOCATION_MIN_ROOM_ATR":     str(round(getattr(_cfg, "LOCATION_MIN_ROOM_ATR", 0.8), 2)),
            "REGIME_FILTER_ENABLED":     str(getattr(_cfg, "REGIME_FILTER_ENABLED", True)),
            "TREND_CONFLICT_SKIP":       str(getattr(_cfg, "TREND_CONFLICT_SKIP", False)),
            "ENTRY_MIN_CONFLUENCE":      str(getattr(_cfg, "ENTRY_MIN_CONFLUENCE", 5)),
            "ENTRY_MIN_CONFLUENCE_EDGE": str(getattr(_cfg, "ENTRY_MIN_CONFLUENCE_EDGE", 2)),
            "PULLBACK_SELF_RELATIVE":    str(getattr(_cfg, "PULLBACK_SELF_RELATIVE", True)),
            "PULLBACK_SELF_MIN_RATIO":   str(round(getattr(_cfg, "PULLBACK_SELF_MIN_RATIO", 0.7), 2)),
            "PULLBACK_VOL_THRESHOLD":    str(round(getattr(_cfg, "PULLBACK_VOL_THRESHOLD", 4.0), 2)),
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
            "protection_buffer_pct": getattr(_cfg, "PP_PROTECTION_BUFFER_PCT",   0.25),
            "trailing_trigger_pct":  getattr(_cfg, "PP_TRAILING_TRIGGER_PCT",    1.0),
            "trailing_timer_secs":   getattr(_cfg, "PP_TRAILING_TIMER_SECS",     7),
            "trailing_distance_pct": getattr(_cfg, "PP_TRAILING_DISTANCE_PCT",   0.5),
            "tier4_trigger_pct":     getattr(_cfg, "PP_TIER4_TRIGGER_PCT",       2.0),
            "tier4_timer_secs":      getattr(_cfg, "PP_TIER4_TIMER_SECS",        3),
            "tier4_trail_dist_pct":  getattr(_cfg, "PP_TIER4_TRAIL_DIST_PCT",    0.3),
            "tier5_trigger_pct":     getattr(_cfg, "PP_TIER5_TRIGGER_PCT",       3.0),
            "tier5_timer_secs":      getattr(_cfg, "PP_TIER5_TIMER_SECS",        3),
            "tier5_trail_dist_pct":  getattr(_cfg, "PP_TIER5_TRAIL_DIST_PCT",   0.15),
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
        if "tier4_trigger_pct"     in data: _cfg.PP_TIER4_TRIGGER_PCT       = max(0.5, min(20.0, float(data["tier4_trigger_pct"])))
        if "tier4_timer_secs"      in data: _cfg.PP_TIER4_TIMER_SECS        = max(1,   min(30,   int(data["tier4_timer_secs"])))
        if "tier4_trail_dist_pct"  in data: _cfg.PP_TIER4_TRAIL_DIST_PCT    = max(0.05,min(2.0,  float(data["tier4_trail_dist_pct"])))
        if "tier5_trigger_pct"     in data: _cfg.PP_TIER5_TRIGGER_PCT       = max(0.5, min(30.0, float(data["tier5_trigger_pct"])))
        if "tier5_timer_secs"      in data: _cfg.PP_TIER5_TIMER_SECS        = max(1,   min(30,   int(data["tier5_timer_secs"])))
        if "tier5_trail_dist_pct"  in data: _cfg.PP_TIER5_TRAIL_DIST_PCT    = max(0.05,min(1.0,  float(data["tier5_trail_dist_pct"])))
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
            "PP_TIER4_TRIGGER_PCT":       str(round(_cfg.PP_TIER4_TRIGGER_PCT, 2)),
            "PP_TIER4_TIMER_SECS":        str(_cfg.PP_TIER4_TIMER_SECS),
            "PP_TIER4_TRAIL_DIST_PCT":    str(round(_cfg.PP_TIER4_TRAIL_DIST_PCT, 2)),
            "PP_TIER5_TRIGGER_PCT":       str(round(_cfg.PP_TIER5_TRIGGER_PCT, 2)),
            "PP_TIER5_TIMER_SECS":        str(_cfg.PP_TIER5_TIMER_SECS),
            "PP_TIER5_TRAIL_DIST_PCT":    str(round(_cfg.PP_TIER5_TRAIL_DIST_PCT, 2)),
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
    """Start web dashboard and its Binance ledger background cache."""
    global _state, _lock, _config, _exchange, _ledger
    _state = state
    _lock = lock
    _config = config
    _exchange = exchange

    # Start exactly one account/ledger worker. It serves any durable cache
    # immediately and performs all Binance history/account I/O off Flask threads.
    if exchange is not None:
        existing_ledger = state.get("_binance_ledger")
        if existing_ledger is None:
            from binance_trade_ledger import BinanceTradeLedger
            with lock:
                legacy_history = list(state.get("trade_log", []))
            existing_ledger = BinanceTradeLedger(
                exchange,
                legacy_history=legacy_history,
                refresh_seconds=getattr(config, "BINANCE_LEDGER_REFRESH_SECONDS", 60),
                retention_days=getattr(config, "BINANCE_LEDGER_RETENTION_DAYS", 365),
            )
            with lock:
                state["_binance_ledger"] = existing_ledger
        _ledger = existing_ledger
        _ledger.start()

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
        if "protected_pending_order_coins" not in state:
            state["protected_pending_order_coins"] = list(getattr(
                config, "PROTECTED_PENDING_ORDER_COINS",
                ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT"],
            ))
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


@app.route("/api/macro-calendar", methods=["GET"])
@require_auth
def api_macro_calendar():
    """Return the service's in-memory/disk snapshot; never perform network I/O."""
    with _lock:
        service = _state.get("_macro_calendar") if _state is not None else None
    if service is None:
        return jsonify({
            "schema_version": 1, "events": [], "enabled": False, "stale": True,
            "refreshing": False, "source_errors": {},
            "error": "Macro calendar service is not initialized",
        })
    return jsonify(service.snapshot())


@app.route("/api/macro-calendar/refresh", methods=["POST"])
@require_auth
def api_macro_calendar_refresh():
    """Coalesce an asynchronous refresh request and return the current snapshot."""
    with _lock:
        service = _state.get("_macro_calendar") if _state is not None else None
    if service is None:
        return jsonify({"ok": False, "queued": False, "error": "Macro calendar service is not initialized"}), 503
    queued = service.request_refresh()
    return jsonify({"ok": True, "queued": queued, "snapshot": service.snapshot()}), 202


@app.route("/api/news", methods=["GET"])
@require_auth
def api_news():
    """
    Tin tức crypto + macro (Fed, lãi suất, CPI...).
    Query: ?force=1 để bỏ qua cache.
    Trả: { items:[{title,link,source,summary,ts,tags,coins}], updated, errors }
    """
    try:
        force = request.args.get("force") == "1"
        items, ts, errors = get_market_news(force=force)
        return jsonify({
            "ok": True,
            "items": items,
            "updated": ts,
            "age_secs": int(time.time() - ts) if ts else None,
            "errors": errors,
            "sources": [n for n, _ in NEWS_SOURCES],
        })
    except Exception as e:
        logger.error(f"[News] api_news failed: {e}")
        return jsonify({"ok": False, "items": [], "error": str(e)}), 200


@app.route("/api/pnl_stats", methods=["GET"])
def api_pnl_stats():
    """Return event-basis PnL and complete-cycle outcome counts."""
    from datetime import datetime, timedelta
    import collections

    with _lock:
        tlog = list(_state.get("trade_log", []))
        state_snapshot = dict(_state)
    financial = _financial_snapshot(tlog, state_snapshot)
    events = _financial_events(financial)
    outcomes = _complete_closed_cycles(financial)

    def event_dt(event):
        timestamp_ms = int(event.get("time_ms", 0) or 0)
        return datetime.fromtimestamp(timestamp_ms / 1000) if timestamp_ms else None

    def cycle_dt(cycle):
        timestamp_ms = int(cycle.get("closed_at_ms", 0) or 0)
        return datetime.fromtimestamp(timestamp_ms / 1000) if timestamp_ms else None

    def build_stats(event_key, cycle_key, keys):
        buckets = collections.defaultdict(lambda: {
            "gross": 0.0, "commission": 0.0, "funding": 0.0,
            "net": 0.0, "net_complete": True, "trades": 0, "wins": 0,
        })
        for event in events:
            dt = event_dt(event)
            key = event_key(event, dt) if dt else None
            if key is None:
                continue
            bucket = buckets[key]
            bucket["gross"] += float(event.get("gross", 0) or 0)
            bucket["commission"] += float(event.get("commission", 0) or 0)
            bucket["funding"] += float(event.get("funding", 0) or 0)
            if bool(event.get("net_complete", True)):
                bucket["net"] += float(event.get("net", 0) or 0)
            else:
                bucket["net_complete"] = False
        for cycle in outcomes:
            dt = cycle_dt(cycle)
            key = cycle_key(cycle, dt) if dt else None
            if key is None:
                continue
            buckets[key]["trades"] += 1
            buckets[key]["wins"] += 1 if float(cycle.get("net_pnl", 0)) > 0 else 0
        result = []
        for key in keys(buckets):
            value = buckets[key]
            result.append({
                "key": key,
                "pnl": round(value["net"], 2) if value["net_complete"] else None,
                "gross": round(value["gross"], 2),
                "commission": round(value["commission"], 2),
                "funding": round(value["funding"], 2),
                "net_complete": value["net_complete"],
                "trades": value["trades"],
                "wins": value["wins"],
            })
        return result

    daily = build_stats(
        lambda _event, dt: dt.strftime("%Y-%m-%d"),
        lambda _cycle, dt: dt.strftime("%Y-%m-%d"),
        lambda buckets: sorted(buckets.keys(), reverse=True),
    )
    today_str = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    for item in daily:
        key = item.pop("key")
        item["label"] = (
            "Hôm nay" if key == today_str else "Hôm qua" if key == yesterday_str
            else datetime.strptime(key, "%Y-%m-%d").strftime("%d/%m/%y")
        )

    weekly = build_stats(
        lambda _event, dt: dt.strftime("%G-W%V"),
        lambda _cycle, dt: dt.strftime("%G-W%V"),
        lambda buckets: sorted(buckets.keys())[-8:],
    )
    current_week = datetime.now().strftime("%G-W%V")
    for item in weekly:
        key = item.pop("key")
        item["label"] = "Tuần này" if key == current_week else f"T{key.split('W')[1]}/{key.split('-')[0][2:]}"

    monthly = build_stats(
        lambda _event, dt: dt.strftime("%Y-%m"),
        lambda _cycle, dt: dt.strftime("%Y-%m"),
        lambda buckets: sorted(buckets.keys())[-6:],
    )
    current_month = datetime.now().strftime("%Y-%m")
    for item in monthly:
        key = item.pop("key")
        year, month = key.split("-")
        item["label"] = "Tháng này" if key == current_month else f"T{int(month)}/{year[2:]}"

    by_coin = build_stats(
        lambda event, _dt: str(event.get("symbol") or "???"),
        lambda cycle, _dt: str(cycle.get("symbol") or "???"),
        lambda buckets: sorted(
            buckets.keys(),
            key=lambda key: buckets[key]["net"] if buckets[key]["net_complete"] else float("-inf"),
            reverse=True,
        ),
    )
    for item in by_coin:
        item["label"] = item.pop("key").replace("USDT", "")

    totals = _financial_totals(financial, events, outcomes)
    return jsonify({
        "daily": daily, "weekly": weekly, "monthly": monthly, "by_coin": by_coin,
        "period_net": totals["period_net_pnl"],
        "gross": totals["gross"],
        "commission": totals["commission"],
        "funding": totals["funding"],
        "net_complete": totals["net_complete"],
        "commission_complete": totals["commission_complete"],
        "incomplete_cycles": totals["incomplete_cycles"],
        "financial_warnings": financial.get("financial_warnings", []),
        "source": financial.get("source", "legacy"),
        "synced_at": financial.get("synced_at", ""),
        "stale": bool(financial.get("stale", True)),
        "errors": financial.get("errors", []),
        "trade_count": totals["closed_cycles"],
    })


@app.route("/api/equity_curve", methods=["GET"])
def api_equity_curve():
    """Build an explicitly forced wallet reconstruction from financial events."""
    from datetime import datetime

    range_param = request.args.get("range", "30d")
    range_days = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - range_days[range_param] * 86_400_000 if range_param in range_days else None

    with _lock:
        tlog = list(_state.get("trade_log", []))
        state_snapshot = dict(_state)
    financial = _financial_snapshot(tlog, state_snapshot)
    financial_events = [
        event for event in _financial_events(financial)
        if cutoff_ms is None or int(event.get("time_ms", 0) or 0) >= cutoff_ms
    ]
    transfers = [
        event for event in financial.get("transfers", [])
        if cutoff_ms is None or int(event.get("time_ms", 0) or 0) >= cutoff_ms
    ]
    outcomes = [
        cycle for cycle in _complete_closed_cycles(financial)
        if cutoff_ms is None or int(cycle.get("closed_at_ms", 0) or 0) >= cutoff_ms
    ]
    totals = _financial_totals(financial, financial_events, outcomes)
    period_net = totals["period_net_pnl"]
    selected_transfer_totals = _transfer_totals(transfers)
    transfer_total = selected_transfer_totals["transfer"]
    current_wallet = float(financial.get("account", {}).get("wallet_balance", 0) or 0)

    common = {
        "period_net": round(period_net, 8) if period_net is not None else None,
        "change_usd": round(period_net, 8) if period_net is not None else None,
        "transfer": round(transfer_total, 8) if transfer_total is not None else None,
        "transfer_complete": selected_transfer_totals["transfer_complete"],
        "unsupported_transfer_assets": selected_transfer_totals["unsupported_transfer_assets"],
        "gross": round(totals["gross"], 8),
        "commission": round(totals["commission"], 8),
        "funding": round(totals["funding"], 8),
        "net_complete": totals["net_complete"],
        "commission_complete": totals["commission_complete"],
        "trade_count": len(outcomes),
        "incomplete_cycles": totals["incomplete_cycles"],
        "source": financial.get("source", "legacy"),
        "synced_at": financial.get("synced_at", ""),
        "stale": bool(financial.get("stale", True)),
        "reconstructed": True,
        "estimated": True,
        "forced_reconstruction": True,
        "reconstruction_method": "backsolved_from_current_wallet",
        "independent_reconciliation": False,
        "reconciles_to_wallet": False,
        "financial_warnings": financial.get("financial_warnings", []),
    }
    if period_net is None or transfer_total is None:
        return jsonify({
            **common,
            "points": [], "point_count": 0,
            "start_balance": None, "end_balance": round(current_wallet, 2),
            "change_pct": None, "wallet_change": None,
        })

    start_wallet = current_wallet - period_net - transfer_total
    event_times = [int(event.get("time_ms", 0) or 0) for event in financial_events + transfers]
    start_time_ms = cutoff_ms if cutoff_ms is not None else (
        int(financial.get("window_start_ms", 0) or 0) or (min(event_times) if event_times else now_ms)
    )
    replay_events = [{
        "time_ms": int(event.get("time_ms", 0) or 0),
        "pnl": float(event.get("net", 0) or 0),
        "symbol": str(event.get("symbol", "")).replace("USDT", ""),
        "side": "",
        "event_type": str(event.get("event_type", "financial")),
        "event_id": str(event.get("event_id", "")),
    } for event in financial_events]
    replay_events.extend({
        "time_ms": int(event.get("time_ms", 0) or 0),
        "pnl": float(event.get("amount", 0) or 0),
        "symbol": "TRANSFER", "side": "", "event_type": "transfer",
        "event_id": str(event.get("event_id", "")),
    } for event in transfers)
    replay_events.sort(key=lambda item: (item["time_ms"], item["event_type"], item["event_id"]))

    running = start_wallet
    points = [{
        "time": datetime.fromtimestamp(start_time_ms / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "balance": round(start_wallet, 2), "pnl": 0.0, "symbol": "", "side": "",
        "pnl_pct": 0.0, "event_type": "forced_start",
    }]
    for event in replay_events:
        running += event["pnl"]
        points.append({
            "time": datetime.fromtimestamp(event["time_ms"] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
            "balance": round(running, 2), "pnl": round(event["pnl"], 8),
            "symbol": event["symbol"], "side": event["side"], "pnl_pct": 0.0,
            "event_type": event["event_type"],
        })
    points.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "balance": round(current_wallet, 2), "pnl": 0.0, "symbol": "", "side": "",
        "pnl_pct": 0.0, "event_type": "current_wallet_anchor",
    })
    change_pct = period_net / start_wallet * 100 if start_wallet > 0 else 0.0
    return jsonify({
        **common,
        "points": points,
        "start_balance": round(start_wallet, 2),
        "end_balance": round(current_wallet, 2),
        "change_pct": round(change_pct, 2),
        "wallet_change": round(period_net + transfer_total, 8),
        "point_count": len(points),
    })


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


@app.route("/api/trade_history_file", methods=["GET"])
@require_auth
def api_trade_history_file():
    """Đọc trực tiếp file trade_history.json từ disk."""
    try:
        from trade_history import HISTORY_FILE
        import json
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r') as f:
                history = json.load(f)
            return jsonify({
                "ok": True,
                "count": len(history),
                "trades": history,
                "file": HISTORY_FILE
            })
        else:
            return jsonify({"ok": False, "msg": "File không tồn tại", "file": HISTORY_FILE})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


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
