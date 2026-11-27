"""Copy Binance lead traders (public positions) and list top Binance grid bots.

Data comes from Binance's public copy-trading web endpoints (unofficial,
unauthenticated). They can change without notice; every fetch failure is
treated as "no information" and never closes a copied position.

Rules:
- Daily scan ranks lead traders by copier PnL (180D) and marks a leader
  "pass" only if: positions are public, running >= MIN_DAYS, MDD <= MAX_MDD,
  copier PnL > 0, enough copiers.
- The user picks leaders to follow. Positions the leader already had when
  following started are ignored; only NEW leader positions are copied.
- A copy opens with the user's fixed margin, leverage = min(leader, cap), and
  an exchange SL at sl_pct from entry. It is closed when the leader's position
  is confirmed gone/flipped on two consecutive successful polls.
- Binance grid bots cannot be created through the public API; they are listed
  for manual copy in the Binance app.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

PERSISTENCE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "copy_trading.json"
)
BASE = "https://www.binance.com/bapi/futures/v1"
LIST_URL = BASE + "/friendly/future/copy-trade/home-page/query-list"
DETAIL_URL = BASE + "/friendly/future/copy-trade/lead-portfolio/detail?portfolioId="
POSITIONS_URL = BASE + "/friendly/future/copy-trade/lead-data/positions?portfolioId="
BOTS_URL = BASE + "/public/future/common/strategy/landing-page/queryTopStrategy"

MIN_DAYS = 90
MAX_MDD = 30.0
MIN_COPIERS = 20
SCAN_INTERVAL = 24 * 3600
POLL_INTERVAL = 10
HARD_MAX_LEVERAGE = 10
CLOSE_CONFIRMATIONS = 2


def _f(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _http_json(url, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _position_key(symbol, side):
    return f"{symbol}|{side}"


def parse_leader_positions(payload):
    """Return {key: {symbol, side, entry, leverage, amount}} for nonzero rows.

    Returns None if the payload is not a successful response, so callers can
    distinguish "leader has no positions" from "fetch failed".
    """
    if not isinstance(payload, dict) or str(payload.get("code")) != "000000":
        return None
    rows = payload.get("data")
    if not isinstance(rows, list):
        return None
    out = {}
    for row in rows:
        amount = _f(row.get("positionAmount"))
        symbol = str(row.get("symbol") or "")
        if amount == 0 or not symbol.endswith("USDT"):
            continue
        pside = str(row.get("positionSide") or "BOTH").upper()
        side = pside if pside in ("LONG", "SHORT") else ("LONG" if amount > 0 else "SHORT")
        out[_position_key(symbol, side)] = {
            "symbol": symbol, "side": side,
            "entry": _f(row.get("entryPrice")),
            "leverage": int(_f(row.get("leverage"), 1)) or 1,
            "amount": abs(amount),
        }
    return out


class CopyTradingService:
    def __init__(self, config, exchange, *, open_callback=None, close_callback=None,
                 position_callback=None, pnl_callback=None, is_trading_enabled=lambda: False,
                 http_json=_http_json, now_fn=time.time, persistence_file=PERSISTENCE_FILE):
        self.config = config
        self.exchange = exchange
        self.open_callback = open_callback
        self.close_callback = close_callback
        self.position_callback = position_callback
        self.pnl_callback = pnl_callback
        self.is_trading_enabled = is_trading_enabled
        self._http = http_json
        self._now = now_fn
        self._file = persistence_file
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._scan_thread = None
        self._data = {
            "enabled": False,
            "margin_usdt": 2.0,
            "max_leverage": 5,
            "sl_pct": 3.0,
            "max_copies": 3,
            "max_entry_drift_pct": 1.0,
            "leaders": [],
            "bots": [],
            "following": {},
            "copies": {},
            "closed": [],
            "events": [],
            "scan": {"running": False, "last_scan": 0.0, "progress": "", "error": ""},
        }
        self._load()
        with self._lock:
            self._data["scan"]["running"] = False

    # ---------------- persistence ----------------
    def _load(self):
        try:
            with open(self._file, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                for key, value in saved.items():
                    if key in self._data:
                        self._data[key] = value
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("[CopyTrade] load failed: %s", exc)

    def _persist_locked(self):
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            tmp = self._file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False)
            os.replace(tmp, self._file)
        except Exception as exc:
            logger.warning("[CopyTrade] persist failed: %s", exc)

    def _event_locked(self, kind, message, **extra):
        self._data["events"].append({"ts": self._now(), "kind": kind, "msg": message, **extra})
        self._data["events"] = self._data["events"][-100:]

    def snapshot(self):
        with self._lock:
            snap = copy.deepcopy(self._data)
        snap["stats"] = self._stats(snap.get("closed") or [])
        return snap

    def _stats(self, closed):
        now_local = time.localtime(self._now())
        today = time.strftime("%Y-%m-%d", now_local)
        month = time.strftime("%Y-%m", now_local)
        out = {"trades": 0, "wins": 0, "losses": 0, "pending": 0, "net": 0.0,
               "today": 0.0, "month": 0.0, "by_leader": {}, "by_day": {}}
        for item in closed:
            if not item.get("pnl_final"):
                out["pending"] += 1
                continue
            net = _f(item.get("net_pnl"))
            day = time.strftime("%Y-%m-%d", time.localtime(_f(item.get("closed_at"))))
            out["trades"] += 1
            out["wins"] += net > 0
            out["losses"] += net <= 0
            out["net"] += net
            out["today"] += net if day == today else 0.0
            out["month"] += net if day.startswith(month) else 0.0
            out["by_day"][day] = round(out["by_day"].get(day, 0.0) + net, 4)
            name = item.get("nickname") or item.get("leader") or "?"
            row = out["by_leader"].setdefault(name, {"trades": 0, "wins": 0, "net": 0.0})
            row["trades"] += 1
            row["wins"] += net > 0
            row["net"] = round(row["net"] + net, 4)
        out["win_rate"] = round(out["wins"] / out["trades"] * 100, 1) if out["trades"] else 0.0
        for key in ("net", "today", "month"):
            out[key] = round(out[key], 4)
        return out

    def _settle_closed(self):
        """Fill realized PnL + fees from Binance income for closed copies."""
        if self.pnl_callback is None:
            return
        now = self._now()
        with self._lock:
            pending = [(i, copy.deepcopy(x)) for i, x in enumerate(self._data["closed"])
                       if not x.get("pnl_final") and now - _f(x.get("closed_at")) >= 15
                       and int(x.get("pnl_attempts", 0)) < 20]
        for index, item in pending:
            try:
                res = self.pnl_callback(item["symbol"], int(_f(item.get("opened_at")) * 1000) - 5000,
                                        int(_f(item.get("closed_at")) * 1000) + 120000) or {}
                ok = res.get("ok", True)
            except Exception:
                res, ok = {}, False
            with self._lock:
                closed = self._data["closed"]
                if index >= len(closed) or closed[index].get("opened_at") != item.get("opened_at"):
                    continue
                live = closed[index]
                live["pnl_attempts"] = int(live.get("pnl_attempts", 0)) + 1
                if ok and (res.get("records") or live["pnl_attempts"] >= 4):
                    live["realized_pnl"] = round(_f(res.get("realized")), 6)
                    live["fee"] = round(_f(res.get("fee")), 6)
                    live["net_pnl"] = round(live["realized_pnl"] + live["fee"], 6)
                    live["pnl_final"] = True
                self._persist_locked()

    # ---------------- lifecycle ----------------
    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="copy-trading", daemon=True)
            self._thread.start()
            return self._thread

    def stop(self, timeout=5):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    # ---------------- user actions ----------------
    def toggle(self, enabled):
        with self._lock:
            self._data["enabled"] = bool(enabled)
            self._event_locked("toggle", "Copy REAL BẬT" if enabled else "Copy REAL TẮT (lệnh copy đang mở vẫn giữ SL)")
            self._persist_locked()
        self._wake.set()
        return {"ok": True, "enabled": bool(enabled)}

    def update_settings(self, margin_usdt, max_leverage, sl_pct, max_copies):
        try:
            margin = float(margin_usdt)
            lev = int(max_leverage)
            sl = float(sl_pct)
            copies = int(max_copies)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Giá trị không hợp lệ"}
        if not (1 <= margin <= 500):
            return {"ok": False, "error": "Margin 1–500 USDT"}
        if not (1 <= lev <= HARD_MAX_LEVERAGE):
            return {"ok": False, "error": f"Leverage 1–{HARD_MAX_LEVERAGE}x"}
        if not (0.5 <= sl <= 10):
            return {"ok": False, "error": "SL 0.5–10% giá"}
        if not (1 <= copies <= 10):
            return {"ok": False, "error": "Số lệnh copy 1–10"}
        with self._lock:
            self._data.update({"margin_usdt": margin, "max_leverage": lev,
                               "sl_pct": sl, "max_copies": copies})
            self._persist_locked()
        return {"ok": True}

    def follow(self, portfolio_id, enabled):
        pid = str(portfolio_id or "").strip()
        if not pid.isdigit():
            return {"ok": False, "error": "portfolioId không hợp lệ"}
        if not enabled:
            with self._lock:
                info = self._data["following"].get(pid)
                open_copies = self._open_copies_locked(pid)
                if info is not None and open_copies:
                    # Stop new copies but keep closing the open ones with the leader.
                    info["active"] = False
                    self._event_locked("unfollow", f"Bỏ copy {info.get('nickname', pid)}: không mở lệnh mới; {open_copies} lệnh đang mở vẫn đóng theo trader")
                else:
                    self._data["following"].pop(pid, None)
                    self._event_locked("unfollow", f"Bỏ copy {pid}")
                self._persist_locked()
            return {"ok": True, "following": False}
        try:
            detail = (self._http(DETAIL_URL + pid) or {}).get("data") or {}
        except Exception as exc:
            return {"ok": False, "error": f"Không lấy được trader: {exc}"}
        if not detail.get("positionShow"):
            return {"ok": False, "error": "Trader này ẩn vị thế, không copy tự động được"}
        try:
            current = parse_leader_positions(self._http(POSITIONS_URL + pid))
        except Exception:
            current = None
        if current is None:
            return {"ok": False, "error": "Không đọc được vị thế trader, thử lại sau"}
        with self._lock:
            previous = self._data["following"].get(pid) or {}
            self._data["following"][pid] = {
                "active": True,
                "nickname": detail.get("nickname") or pid,
                "since": self._now(),
                # Existing leader positions are never copied (late entry).
                "baseline": sorted(current.keys()),
                "last_positions": current,
                "missing": previous.get("missing", {}),
                "last_poll": self._now(),
                "error": "",
            }
            self._event_locked("follow", f"Theo dõi {detail.get('nickname') or pid}; bỏ qua {len(current)} vị thế cũ")
            self._persist_locked()
        self._wake.set()
        return {"ok": True, "following": True, "ignored_existing": len(current)}

    def _open_copies_locked(self, pid):
        return sum(1 for x in self._data["copies"].values()
                   if x.get("leader") == pid and x.get("status") == "OPEN")

    def manual_close(self, copy_id):
        with self._lock:
            item = self._data["copies"].get(str(copy_id or ""))
            if not item or item.get("status") != "OPEN":
                return {"ok": False, "error": "Không tìm thấy lệnh copy đang mở"}
        self._close_copy(str(copy_id), "Đóng tay trên web")
        with self._lock:
            still_open = str(copy_id) in self._data["copies"]
        return {"ok": not still_open, "error": "Đóng lỗi, xem nhật ký" if still_open else ""}

    def start_scan(self):
        with self._lock:
            if self._data["scan"].get("running"):
                return {"ok": False, "error": "Đang quét"}
            self._data["scan"].update({"running": True, "progress": "Đang lấy danh sách...", "error": ""})
            self._persist_locked()
        self._scan_thread = threading.Thread(target=self._scan_worker, name="copy-scan", daemon=True)
        self._scan_thread.start()
        return {"ok": True}

    # ---------------- scanning ----------------
    def _scan_worker(self):
        try:
            leaders = self._scan_leaders()
            bots = self._scan_bots()
            with self._lock:
                self._data["leaders"] = leaders
                self._data["bots"] = bots
                self._data["scan"].update({"running": False, "last_scan": self._now(),
                                           "progress": f"{sum(1 for x in leaders if x['pass'])} trader pass · {len(bots)} bot",
                                           "error": ""})
                self._persist_locked()
        except Exception as exc:
            logger.warning("[CopyTrade] scan failed: %s", exc)
            with self._lock:
                self._data["scan"].update({"running": False, "error": str(exc)[:200],
                                           "last_scan": self._now()})
                self._persist_locked()

    def _scan_leaders(self):
        now = self._now()
        rows = []
        for page in range(1, 6):
            payload = self._http(LIST_URL, {
                "pageNumber": page, "pageSize": 20, "timeRange": "180D",
                "dataType": "COPIER_PNL", "favoriteOnly": False, "hideFull": False,
                "nickname": "", "order": "DESC", "userAsset": 0, "portfolioType": "PUBLIC",
            })
            items = ((payload or {}).get("data") or {}).get("list") or []
            rows.extend(items)
            if len(items) < 20:
                break
        leaders = []
        for item in rows:
            pid = str(item.get("leadPortfolioId") or "")
            days = (now - _f(item.get("startTime")) / 1000.0) / 86400.0 if item.get("startTime") else 0.0
            leader = {
                "id": pid, "nickname": str(item.get("nickname") or pid),
                "roi": round(_f(item.get("roi")), 2), "pnl": round(_f(item.get("pnl")), 2),
                "mdd": round(_f(item.get("mdd")), 2), "win_rate": round(_f(item.get("winRate")), 1),
                "copier_pnl": round(_f(item.get("copierPnl")), 2), "aum": round(_f(item.get("aum")), 2),
                "copiers": int(_f(item.get("currentCopyCount"))), "days": round(days, 0),
                "position_show": False, "pass": False, "reasons": [],
            }
            reasons = []
            if days < MIN_DAYS:
                reasons.append(f"chạy {days:.0f}<{MIN_DAYS} ngày")
            if leader["mdd"] > MAX_MDD:
                reasons.append(f"MDD {leader['mdd']:.0f}%>{MAX_MDD:.0f}%")
            if leader["copier_pnl"] <= 0:
                reasons.append("người copy đang lỗ")
            if leader["copiers"] < MIN_COPIERS:
                reasons.append(f"<{MIN_COPIERS} người copy")
            if not reasons:
                try:
                    detail = (self._http(DETAIL_URL + pid) or {}).get("data") or {}
                    leader["position_show"] = bool(detail.get("positionShow"))
                    leader["profit_share"] = _f(detail.get("profitSharingRate"))
                except Exception:
                    leader["position_show"] = False
                if not leader["position_show"]:
                    reasons.append("ẩn vị thế")
            leader["reasons"] = reasons
            leader["pass"] = not reasons
            leaders.append(leader)
        leaders.sort(key=lambda x: (not x["pass"], -x["copier_pnl"]))
        return leaders

    def _scan_bots(self):
        payload = self._http(BOTS_URL, {
            "page": 1, "rows": 20, "direction": "", "strategyType": 2, "symbol": "",
            "zone": "", "runningTimeMin": 30 * 86400, "runningTimeMax": None, "sort": "roi",
        })
        bots = []
        for item in (payload or {}).get("data") or []:
            params = item.get("strategyParams") or {}
            bots.append({
                "id": item.get("strategyId"), "symbol": item.get("symbol"),
                "roi": round(_f(item.get("roi")), 2), "pnl": round(_f(item.get("pnl")), 2),
                "days": round(_f(item.get("runningTime")) / 86400.0, 1),
                "mdd_7d": round(_f(item.get("sevenDayMdd")), 2),
                "min_investment": round(_f(item.get("minInvestment")), 2),
                "copy_count": int(_f(item.get("copyCount"))),
                "lower": params.get("lowerLimit"), "upper": params.get("upperLimit"),
                "grids": params.get("gridCount"), "leverage": params.get("leverage"),
                "type": params.get("type"),
            })
        return bots

    # ---------------- mirroring ----------------
    def _worker(self):
        while not self._stop.is_set():
            try:
                with self._lock:
                    last = _f(self._data["scan"].get("last_scan"))
                    scanning = self._data["scan"].get("running")
                if not scanning and self._now() - last >= SCAN_INTERVAL:
                    self.start_scan()
                self.poll_once()
                self._settle_closed()
            except Exception as exc:
                logger.warning("[CopyTrade] worker error: %s", exc)
            self._wake.wait(POLL_INTERVAL)
            self._wake.clear()

    def poll_once(self):
        self._reconcile_own_positions()
        with self._lock:
            following = list(self._data["following"].keys())
        for pid in following:
            try:
                current = parse_leader_positions(self._http(POSITIONS_URL + pid))
            except Exception as exc:
                current = None
                err = str(exc)[:120]
            else:
                err = "" if current is not None else "Binance trả dữ liệu lỗi"
            if current is None:
                with self._lock:
                    if pid in self._data["following"]:
                        self._data["following"][pid]["error"] = err
                        self._persist_locked()
                continue
            self._handle_leader(pid, current)

    def _handle_leader(self, pid, current):
        with self._lock:
            info = self._data["following"].get(pid)
            if info is None:
                return
            info["last_poll"] = self._now()
            info["error"] = ""
            info["last_positions"] = current
            baseline = set(info.get("baseline") or [])
            # A baseline position that disappears becomes copyable next time.
            baseline &= set(current.keys())
            info["baseline"] = sorted(baseline)
            missing = info.setdefault("missing", {})
            to_close = []
            for cid, item in self._data["copies"].items():
                if item.get("leader") != pid or item.get("status") != "OPEN":
                    continue
                key = _position_key(item["symbol"], item["side"])
                if key in current:
                    missing.pop(key, None)
                else:
                    missing[key] = int(missing.get(key, 0)) + 1
                    if missing[key] >= CLOSE_CONFIRMATIONS:
                        to_close.append(cid)
            open_keys = {
                _position_key(x["symbol"], x["side"])
                for x in self._data["copies"].values()
                if x.get("leader") == pid and x.get("status") in ("OPEN", "OPENING")
            }
            # Forget attempts for leader positions that are gone, so a later
            # re-open by the leader is copied again.
            tried = {k: v for k, v in (info.get("tried") or {}).items() if k in current}
            info["tried"] = tried
            new = [p for k, p in current.items()
                   if k not in baseline and k not in open_keys and k not in tried]
            if not info.get("active", True):
                new = []
            self._persist_locked()
        for cid in to_close:
            self._close_copy(cid, "Trader đã đóng")
        for pos in new:
            self._open_copy(pid, pos)
        with self._lock:
            info = self._data["following"].get(pid)
            if info is not None and not info.get("active", True) and not self._open_copies_locked(pid):
                self._data["following"].pop(pid, None)
                self._event_locked("unfollow", f"{info.get('nickname', pid)}: hết lệnh copy, đã gỡ khỏi danh sách")
                self._persist_locked()

    def _open_copy(self, pid, pos):
        key = _position_key(pos["symbol"], pos["side"])
        with self._lock:
            info = self._data["following"].get(pid) or {}
            info.setdefault("tried", {})[key] = self._now()
            enabled = bool(self._data["enabled"])
            active = sum(1 for x in self._data["copies"].values() if x.get("status") in ("OPEN", "OPENING"))
            settings = {k: self._data[k] for k in ("margin_usdt", "max_leverage", "sl_pct", "max_entry_drift_pct")}
            limit = int(self._data["max_copies"])
            nickname = info.get("nickname", pid)
            self._persist_locked()
        reason = ""
        if not info.get("active", True):
            reason = "Đã bỏ copy trader này"
        elif not enabled:
            reason = "Copy REAL đang tắt"
        elif not self.is_trading_enabled():
            reason = "Bot đang Pause"
        elif active >= limit:
            reason = f"Đã đủ {limit} lệnh copy"
        elif self.open_callback is None:
            reason = "Chưa nối lệnh thật"
        if reason:
            with self._lock:
                self._event_locked("skip", f"{nickname}: {pos['symbol']} {pos['side']} bỏ qua — {reason}")
                self._persist_locked()
            return
        leverage = max(1, min(int(pos.get("leverage") or 1), int(settings["max_leverage"]), HARD_MAX_LEVERAGE))
        signal = {
            "symbol": pos["symbol"], "side": pos["side"], "leader_entry": pos["entry"],
            "margin_usdt": float(settings["margin_usdt"]), "leverage": leverage,
            "sl_pct": float(settings["sl_pct"]),
            "max_entry_drift_pct": float(settings["max_entry_drift_pct"]),
        }
        try:
            result = self.open_callback(signal) or {}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:200]}
        with self._lock:
            if result.get("ok"):
                cid = str(result.get("client_order_id") or f"copy_{int(self._now()*1000):x}")
                self._data["copies"][cid] = {
                    "leader": pid, "nickname": nickname, "symbol": pos["symbol"],
                    "side": pos["side"], "qty": _f(result.get("qty")),
                    "entry": _f(result.get("entry")), "sl": _f(result.get("sl")),
                    "leverage": int(result.get("leverage") or leverage),
                    "margin": float(settings["margin_usdt"]), "leader_entry": pos["entry"],
                    "opened_at": self._now(), "status": "OPEN",
                    "protected": bool(result.get("protected", True)),
                    "sl_order": result.get("sl_order") or {},
                }
                self._event_locked("open", f"COPY {nickname}: {pos['side']} {pos['symbol']} @ {_f(result.get('entry')):.8g}")
            else:
                self._event_locked("error", f"{nickname}: {pos['symbol']} {pos['side']} không vào — {result.get('error') or 'lỗi'}")
            self._persist_locked()

    def _close_copy(self, cid, why):
        with self._lock:
            item = copy.deepcopy(self._data["copies"].get(cid))
        if not item or item.get("status") != "OPEN":
            return
        try:
            result = self.close_callback(item) if self.close_callback else {"ok": False, "error": "Chưa nối lệnh đóng"}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:200]}
        with self._lock:
            live = self._data["copies"].get(cid)
            if live is None:
                return
            if result.get("ok"):
                live["status"] = "CLOSED"
                live["closed_at"] = self._now()
                live["close_reason"] = why if not result.get("already_closed") else "Đã đóng trên Binance (SL/khác)"
                live["exit"] = _f(result.get("exit"))
                self._data["closed"].append(live)
                self._data["closed"] = self._data["closed"][-200:]
                self._data["copies"].pop(cid, None)
                self._event_locked("close", f"ĐÓNG copy {item['symbol']} {item['side']} — {live['close_reason']}")
            else:
                self._event_locked("error", f"Đóng copy {item['symbol']} lỗi — {result.get('error')}; sẽ thử lại")
            self._persist_locked()

    def _reconcile_own_positions(self):
        """Mark copies closed when our Binance position is already gone (SL hit)."""
        if self.position_callback is None:
            return
        with self._lock:
            items = [(cid, copy.deepcopy(x)) for cid, x in self._data["copies"].items() if x.get("status") == "OPEN"]
        for cid, item in items:
            try:
                live = self.position_callback(item["symbol"])
            except Exception:
                continue
            amount = _f((live or {}).get("positionAmt"))
            same_side = (amount > 0 and item["side"] == "LONG") or (amount < 0 and item["side"] == "SHORT")
            if live is None or amount == 0 or not same_side:
                with self._lock:
                    current = self._data["copies"].pop(cid, None)
                    if current:
                        current.update({"status": "CLOSED", "closed_at": self._now(),
                                        "close_reason": "Đã đóng trên Binance (SL/bot/khác)"})
                        self._data["closed"].append(current)
                        self._data["closed"] = self._data["closed"][-200:]
                        self._event_locked("close", f"Copy {item['symbol']} {item['side']} đã đóng trên Binance")
                        self._persist_locked()
            else:
                with self._lock:
                    current = self._data["copies"].get(cid)
                    if current:
                        current["mark"] = _f(live.get("markPrice"))
                        current["pnl"] = _f(live.get("unRealizedProfit"))
