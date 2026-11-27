"""Telegram discipline alerts when positions are large vs account equity.

Reads the live Binance account every CYCLE seconds (all positions, from any
source: bot, copy, grid or manual in the app) and warns when:
- one position's notional is large vs equity (effective leverage),
- the exchange leverage setting on a position is high,
- the loss at the position's SL is a large % of equity, or it has no SL,
- the whole account's notional / margin usage is high.

Risk scale used everywhere (100% = whole account):
- "Giá đi ngược X% là mất 100% vốn": X = equity / notional * 100.
  Effective leverage 10x -> a 10% adverse move wipes the account.
- Margin usage % = (position margin + open-order margin) / equity.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import time

logger = logging.getLogger(__name__)

PERSISTENCE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "risk_alert.json"
)
CYCLE = 20
NO_SL_GRACE_S = 90
DEFAULTS = {
    "enabled": True,
    "pos_lev_warn": 2.0,     # one position notional > 2x equity
    "total_lev_warn": 4.0,   # all positions notional > 4x equity
    "margin_warn_pct": 60.0, # margin used (positions + orders) > 60% equity
    "sl_risk_warn_pct": 5.0, # loss at SL > 5% equity
    "leverage_warn": 20,     # exchange leverage setting >= 20x
    "repeat_minutes": 60,
}
LEVELS = ("AN TOÀN", "CAO", "RẤT CAO", "CỰC CAO")
ICONS = ("🟢", "🟡", "🟠", "🔴")


def _f(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def lev_level(x):
    return 0 if x <= 2 else 1 if x <= 5 else 2 if x <= 10 else 3


def margin_level(pct):
    return 0 if pct <= 30 else 1 if pct <= 60 else 2 if pct <= 85 else 3


def sl_level(pct):
    return 0 if pct <= 2 else 1 if pct <= 5 else 2 if pct <= 10 else 3


def wipe_move_pct(equity, notional):
    """Adverse price move (%) that loses 100% of equity."""
    return equity / notional * 100.0 if notional > 0 else float("inf")


def analyze(account, algo_orders, open_orders):
    equity = _f(account.get("totalMarginBalance")) or _f(account.get("totalWalletBalance"))
    stops = {}
    for order in list(algo_orders or []) + list(open_orders or []):
        kind = str(order.get("orderType") or order.get("type") or "").upper()
        reduce = str(order.get("reduceOnly", "")).lower() == "true" or order.get("reduceOnly") is True \
            or str(order.get("closePosition", "")).lower() == "true"
        if "STOP" in kind and reduce:
            price = _f(order.get("triggerPrice")) or _f(order.get("stopPrice"))
            if price > 0:
                stops.setdefault(order.get("symbol"), []).append(price)
    positions = []
    total_notional = 0.0
    for p in account.get("positions") or []:
        amt = _f(p.get("positionAmt"))
        if amt == 0:
            continue
        entry = _f(p.get("entryPrice"))
        notional = abs(_f(p.get("notional"))) or abs(amt) * entry
        lev = int(_f(p.get("leverage"), 1))
        side = "LONG" if amt > 0 else "SHORT"
        symbol = p.get("symbol")
        sl_list = stops.get(symbol) or []
        sl = None
        if sl_list:
            sl = max(sl_list) if side == "LONG" else min(sl_list)
        sl_loss = abs(entry - sl) * abs(amt) if sl else None
        total_notional += notional
        eff = notional / equity if equity > 0 else float("inf")
        positions.append({
            "symbol": symbol, "side": side, "notional": round(notional, 2), "leverage": lev,
            "margin": round(_f(p.get("initialMargin")) or notional / max(lev, 1), 2),
            "eff_lev": round(eff, 2), "pct_equity": round(eff * 100, 1),
            "wipe_move_pct": round(wipe_move_pct(equity, notional), 2),
            "sl": sl, "sl_loss": round(sl_loss, 2) if sl_loss is not None else None,
            "sl_loss_pct": round(sl_loss / equity * 100, 2) if (sl_loss is not None and equity > 0) else None,
            "upnl": round(_f(p.get("unrealizedProfit")), 2),
        })
    used = _f(account.get("totalPositionInitialMargin")) + _f(account.get("totalOpenOrderInitialMargin"))
    margin_pct = used / equity * 100 if equity > 0 else 0.0
    total_eff = total_notional / equity if equity > 0 else 0.0
    sl_total = sum(p["sl_loss"] or 0 for p in positions)
    no_sl = [p["symbol"] for p in positions if p["sl"] is None]
    level = max([lev_level(total_eff), margin_level(margin_pct),
                 sl_level(sl_total / equity * 100 if equity > 0 else 0)]
                + ([2] if no_sl else []))
    return {
        "equity": round(equity, 2), "available": round(_f(account.get("availableBalance")), 2),
        "total_notional": round(total_notional, 2), "total_eff_lev": round(total_eff, 2),
        "total_pct_equity": round(total_eff * 100, 1),
        "wipe_move_pct": round(wipe_move_pct(equity, total_notional), 2) if total_notional else None,
        "margin_used": round(used, 2), "margin_pct": round(margin_pct, 1),
        "sl_total_loss": round(sl_total, 2),
        "sl_total_pct": round(sl_total / equity * 100, 2) if equity > 0 else 0.0,
        "no_sl": no_sl, "level": level, "level_text": LEVELS[level],
        "positions": positions,
    }


class RiskAlertService:
    def __init__(self, exchange, notifier, *, now_fn=time.time, persistence_file=PERSISTENCE_FILE):
        self.exchange = exchange
        self.notifier = notifier
        self._now = now_fn
        self._file = persistence_file
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._data = {"settings": dict(DEFAULTS), "last": {}, "alerts": [], "sent": {}, "seen": {},
                      "no_sl_since": {}, "notified_level": -1}
        self._load()

    def _load(self):
        try:
            with open(self._file, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                self._data.update({k: v for k, v in saved.items() if k in self._data})
                merged = dict(DEFAULTS)
                merged.update(self._data.get("settings") or {})
                self._data["settings"] = merged
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("[RiskAlert] load failed: %s", exc)

    def _persist_locked(self):
        try:
            os.makedirs(os.path.dirname(self._file), exist_ok=True)
            tmp = self._file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False)
            os.replace(tmp, self._file)
        except Exception as exc:
            logger.warning("[RiskAlert] persist failed: %s", exc)

    def snapshot(self):
        with self._lock:
            snap = copy.deepcopy(self._data)
        snap.pop("sent", None)
        snap.pop("seen", None)
        snap["alerts"] = snap["alerts"][-10:]
        return snap

    def update_settings(self, values):
        clean = {}
        try:
            if "enabled" in values:
                clean["enabled"] = bool(values["enabled"])
            for key, lo, hi in (("pos_lev_warn", 0.2, 50), ("total_lev_warn", 0.2, 100),
                                ("margin_warn_pct", 5, 100), ("sl_risk_warn_pct", 0.5, 100),
                                ("leverage_warn", 2, 125), ("repeat_minutes", 5, 1440)):
                if key in values and values[key] not in (None, ""):
                    v = float(values[key])
                    if not (lo <= v <= hi):
                        return {"ok": False, "error": f"{key} phải trong {lo}–{hi}"}
                    clean[key] = int(v) if key in ("leverage_warn", "repeat_minutes") else v
        except (TypeError, ValueError):
            return {"ok": False, "error": "Giá trị không hợp lệ"}
        with self._lock:
            self._data["settings"].update(clean)
            self._persist_locked()
        return {"ok": True}

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name="risk-alert", daemon=True)
            self._thread.start()
            return self._thread

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def _worker(self):
        self._stop.wait(30)
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:
                logger.warning("[RiskAlert] check failed: %s", exc)
            self._stop.wait(CYCLE)

    def _fetch(self):
        ex = self.exchange
        account = ex._get("/fapi/v2/account", signed=True)
        # A failed read reuses the last good list so a transient Binance error
        # never looks like "position has no SL".
        try:
            algo = ex._get("/fapi/v1/openAlgoOrders", signed=True)
            algo = algo if isinstance(algo, list) else (algo or {}).get("orders", []) if isinstance(algo, dict) else []
            self._last_algo = algo
        except Exception:
            algo = getattr(self, "_last_algo", [])
        try:
            orders = ex._get("/fapi/v1/openOrders", signed=True) or []
            self._last_orders = orders
        except Exception:
            orders = getattr(self, "_last_orders", [])
        return account, algo, orders

    def check_once(self):
        account, algo, orders = self._fetch()
        report = analyze(account, algo, orders)
        now = self._now()
        with self._lock:
            settings = dict(self._data["settings"])
            report["ts"] = now
            self._data["last"] = report
            seen = self._data.setdefault("seen", {})
            current = {p["symbol"] for p in report["positions"]}
            for sym in list(seen):
                if sym not in current:
                    seen.pop(sym, None)
            self._persist_locked()
        if not settings.get("enabled", True):
            return report
        messages = []
        levels = []
        current_syms = {p["symbol"] for p in report["positions"]}
        for p in report["positions"]:
            key = p["symbol"]
            with self._lock:
                self._data["seen"].setdefault(key, now)
                no_sl = self._data.setdefault("no_sl_since", {})
                if p["sl"] is None:
                    no_sl.setdefault(key, now)
                else:
                    no_sl.pop(key, None)
                missing_for = now - no_sl[key] if key in no_sl else 0
            reasons = []
            if p["eff_lev"] > settings["pos_lev_warn"]:
                reasons.append(f"lệnh to = {p['pct_equity']:.0f}% vốn ({p['eff_lev']:.1f}x vốn)")
            if p["leverage"] >= settings["leverage_warn"]:
                reasons.append(f"đòn bẩy {p['leverage']}x")
            # SL can vanish for a few seconds while a monitor replaces it.
            if p["sl"] is None and missing_for >= NO_SL_GRACE_S:
                reasons.append("KHÔNG có SL")
            elif p["sl_loss_pct"] is not None and p["sl_loss_pct"] > settings["sl_risk_warn_pct"]:
                reasons.append(f"dính SL mất {p['sl_loss_pct']:.1f}% vốn (${p['sl_loss']:.2f})")
            if reasons:
                level = self._position_level(p, missing_for)
                levels.append(level)
                messages.append(self._position_text(p, report, reasons, level))
        with self._lock:
            tracked = self._data.setdefault("no_sl_since", {})
            for sym in list(tracked):
                if sym not in current_syms:
                    tracked.pop(sym, None)
            self._persist_locked()
        account_reasons = []
        if report["total_eff_lev"] > settings["total_lev_warn"]:
            account_reasons.append(f"tổng lệnh = {report['total_pct_equity']:.0f}% vốn ({report['total_eff_lev']:.1f}x)")
        if report["margin_pct"] > settings["margin_warn_pct"]:
            account_reasons.append(f"margin đang dùng {report['margin_pct']:.0f}% vốn (gồm lệnh chờ)")
        if report["sl_total_pct"] > settings["sl_risk_warn_pct"] * 2:
            account_reasons.append(f"dính hết SL mất {report['sl_total_pct']:.1f}% vốn")
        # With one position the position alert already says it all; only send
        # the account alert for several positions or high pending-order margin.
        single_dup = (len(report["positions"]) <= 1 and report["margin_pct"] <= settings["margin_warn_pct"])
        acct_level = max(lev_level(report["total_eff_lev"]), margin_level(report["margin_pct"]),
                         sl_level(report["sl_total_pct"]))
        if account_reasons and not single_dup:
            levels.append(acct_level)
            messages.append(self._account_text(report, account_reasons))
        # Notify once when the account enters a warning level, then only when
        # it rises to a higher level. Falling back is silent but re-arms, so a
        # later rise is reported again. No time-based repeats.
        current = max(levels) if messages else -1
        if self._level_changed_up(current):
            self._send("\n\n".join(messages))
        return report

    def _position_level(self, p, missing_for=0):
        no_sl = p["sl"] is None and missing_for >= NO_SL_GRACE_S
        return max(lev_level(p["eff_lev"]), 2 if no_sl else 0,
                   sl_level(p["sl_loss_pct"] or 0), 2 if p["leverage"] >= 20 else 0)

    def _level_changed_up(self, current):
        with self._lock:
            prev = int(self._data.get("notified_level", -1))
            self._data["notified_level"] = int(current)
            self._persist_locked()
        return current > prev and current >= 0

    def _should_send(self, key, level, settings, now):
        """Once per key per repeat window; earlier only if the level escalates."""
        with self._lock:
            sent = self._data.setdefault("sent", {})
            prev = sent.get(key)
            if isinstance(prev, dict):
                recent = now - _f(prev.get("ts")) < settings["repeat_minutes"] * 60
                if recent and level <= int(prev.get("level", 0)):
                    return False
            sent[key] = {"ts": now, "level": int(level)}
            for k in list(sent):
                v = sent[k]
                ts = _f(v.get("ts")) if isinstance(v, dict) else _f(v)
                if now - ts > 86400:
                    sent.pop(k, None)
            self._persist_locked()
            return True

    @staticmethod
    def _scale(equity, notional):
        if notional <= 0 or equity <= 0:
            return ""
        parts = []
        for loss in (10, 50, 100):
            move = loss / 100 * equity / notional * 100
            parts.append(f"mất {loss}% vốn khi giá ngược {move:.2f}%")
        return " · ".join(parts)

    def _position_text(self, p, report, reasons, lvl):
        sl_line = (f"SL {p['sl']:.8g} → mất ${p['sl_loss']:.2f} = {p['sl_loss_pct']:.1f}% vốn"
                   if p["sl"] is not None else "⚠ CHƯA CÓ SL")
        return (f"{ICONS[lvl]} <b>CẢNH BÁO KỶ LUẬT · {LEVELS[lvl]}</b>\n"
                f"{p['symbol']} {p['side']} · notional ${p['notional']:.2f} · {p['leverage']}x · margin ${p['margin']:.2f}\n"
                f"Vốn ${report['equity']:.2f} → lệnh này = <b>{p['pct_equity']:.0f}% vốn</b>\n"
                f"Lý do: {', '.join(reasons)}\n"
                f"{sl_line}\n"
                f"Thang rủi ro: {self._scale(report['equity'], p['notional'])}\n"
                f"Gợi ý: 1 lệnh ≤100–200% vốn, rủi ro tại SL ≤1–2% vốn.")

    def _account_text(self, report, reasons):
        lvl = report["level"]
        return (f"{ICONS[lvl]} <b>CẢNH BÁO TỔNG TÀI KHOẢN · {LEVELS[lvl]}</b>\n"
                f"Vốn ${report['equity']:.2f} · {len(report['positions'])} vị thế · tổng notional ${report['total_notional']:.2f}\n"
                f"Lý do: {', '.join(reasons)}\n"
                f"Margin dùng ${report['margin_used']:.2f} = {report['margin_pct']:.0f}% vốn · "
                f"dính hết SL: ${report['sl_total_loss']:.2f} = {report['sl_total_pct']:.1f}% vốn"
                + (f" · chưa SL: {', '.join(report['no_sl'])}" if report["no_sl"] else "") + "\n"
                f"Thang rủi ro: {self._scale(report['equity'], report['total_notional'])}\n"
                f"Gợi ý: tổng lệnh ≤300–400% vốn, tổng rủi ro tại SL ≤5% vốn.")

    def _send(self, text):
        with self._lock:
            self._data["alerts"].append({"ts": self._now(), "text": text})
            self._data["alerts"] = self._data["alerts"][-50:]
            self._persist_locked()
        try:
            if self.notifier is not None:
                self.notifier.telegram.send(text)
        except Exception as exc:
            logger.warning("[RiskAlert] telegram failed: %s", exc)
