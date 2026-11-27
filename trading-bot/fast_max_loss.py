"""Fast Max Loss guard: Binance pushes fills, mark price checked every second.

The existing Max Loss only learns about a new position from the REST poller
(~9s) and only gets mark ticks for watchlist/pump coins. This guard adds:

- User-data stream (wss://fstream.binance.com/private/ws/<listenKey>):
  ACCOUNT_UPDATE arrives < 1s after any fill (bot, app, manual), so a new
  position is known immediately, including its entry price.
- All-market mark price stream (!markPrice@arr@1s on /market): every open
  position is checked every second, whatever its symbol.

It uses the same MAX_LOSS_ENABLED / MAX_LOSS_PER_POSITION the user sets on the
web (read live each check) and the same close path/guard as the old check, so
both can run together without double closing. It never opens positions and
never places SL/TP orders.
"""
from __future__ import annotations

import json
import logging
import threading
import time

logger = logging.getLogger(__name__)

PRIVATE_WS = "wss://fstream.binance.com/private/ws/"
MARK_WS = "wss://fstream.binance.com/market/ws/!markPrice@arr@1s"
KEEPALIVE_S = 30 * 60
RESYNC_S = 60


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


class FastMaxLossGuard:
    def __init__(self, exchange, config, *, close_callback, skip_symbol=lambda s: False,
                 ws_factory=None, now_fn=time.time):
        self.exchange = exchange
        self.config = config
        self.close_callback = close_callback      # (symbol, amount, pnl, max_loss) -> None
        self.skip_symbol = skip_symbol
        self._ws_factory = ws_factory
        self._now = now_fn
        self._lock = threading.RLock()
        self._positions = {}                       # symbol -> {"amt": float, "entry": float}
        self._marks = {}
        self._fired = {}                           # symbol -> (amt) we already closed
        self._stop = threading.Event()
        self._listen_key = None
        self.stats = {"user_events": 0, "mark_msgs": 0, "closes": 0, "last_user_event": 0.0,
                      "last_mark": 0.0, "user_connected": False, "mark_connected": False}

    # ---------------- positions ----------------
    def seed_positions(self):
        rows = self.exchange._get("/fapi/v2/positionRisk", signed=True) or []
        fresh = {}
        for p in rows:
            amt = _f(p.get("positionAmt"))
            if amt != 0:
                fresh[p["symbol"]] = {"amt": amt, "entry": _f(p.get("entryPrice"))}
        with self._lock:
            self._positions = fresh
            for sym in list(self._fired):
                if sym not in fresh:
                    self._fired.pop(sym, None)

    def on_user_event(self, msg):
        if msg.get("e") != "ACCOUNT_UPDATE":
            if msg.get("e") == "listenKeyExpired":
                self._listen_key = None
            return
        self.stats["user_events"] += 1
        self.stats["last_user_event"] = self._now()
        for p in (msg.get("a") or {}).get("P") or []:
            sym = p.get("s")
            if not sym or str(p.get("ps", "BOTH")).upper() not in ("BOTH", "LONG", "SHORT"):
                continue
            amt = _f(p.get("pa"))
            entry = _f(p.get("ep"))
            with self._lock:
                if amt == 0:
                    self._positions.pop(sym, None)
                    self._fired.pop(sym, None)
                    continue
                self._positions[sym] = {"amt": amt, "entry": entry}
                if self._fired.get(sym) is not None and self._fired[sym] != amt:
                    self._fired.pop(sym, None)  # position changed size: re-arm
            # Check immediately with the freshest mark (or Binance's own uPnL).
            mark = self._marks.get(sym)
            if mark:
                self.check(sym, mark)
            else:
                self._check_pnl(sym, amt, _f(p.get("up")))

    def on_mark_message(self, payload):
        rows = payload if isinstance(payload, list) else [payload]
        self.stats["mark_msgs"] += 1
        self.stats["last_mark"] = self._now()
        with self._lock:
            open_syms = set(self._positions)
        for row in rows:
            sym = row.get("s")
            mark = _f(row.get("p"))
            if not sym or mark <= 0:
                continue
            self._marks[sym] = mark
            if sym in open_syms:
                self.check(sym, mark)

    # ---------------- decision ----------------
    def check(self, sym, mark):
        with self._lock:
            pos = self._positions.get(sym)
        if not pos or pos["entry"] <= 0:
            return False
        pnl = (mark - pos["entry"]) * pos["amt"]
        return self._check_pnl(sym, pos["amt"], pnl)

    def _check_pnl(self, sym, amt, pnl):
        if not getattr(self.config, "MAX_LOSS_ENABLED", True) or self.skip_symbol(sym):
            return False
        max_loss = _f(getattr(self.config, "MAX_LOSS_PER_POSITION", 20.0))
        if max_loss <= 0 or pnl > -max_loss:
            return False
        with self._lock:
            if self._fired.get(sym) == amt:
                return False
            self._fired[sym] = amt
        self.stats["closes"] += 1
        logger.info("[FastMaxLoss] %s pnl=%.2f <= -%.2f → close", sym, pnl, max_loss)
        threading.Thread(target=self._close, args=(sym, amt, pnl, max_loss),
                         name=f"fastml-{sym}", daemon=True).start()
        return True

    def _close(self, sym, amt, pnl, max_loss):
        try:
            ok = self.close_callback(sym, amt, pnl, max_loss)
        except Exception as exc:
            logger.error("[FastMaxLoss] close %s raised: %s", sym, exc)
            ok = False
        if ok is False:
            # Re-arm after a short pause so the next tick retries the close.
            self._stop.wait(1)
            with self._lock:
                if self._fired.get(sym) == amt:
                    self._fired.pop(sym, None)

    # ---------------- streams ----------------
    def _new_listen_key(self):
        url = self.exchange.base_url + "/fapi/v1/listenKey"
        r = self.exchange.session.post(url, timeout=10)
        r.raise_for_status()
        return r.json()["listenKey"]

    def _keepalive(self):
        while not self._stop.wait(KEEPALIVE_S):
            try:
                self.exchange.session.put(self.exchange.base_url + "/fapi/v1/listenKey", timeout=10)
            except Exception as exc:
                logger.warning("[FastMaxLoss] listenKey keepalive failed: %s", exc)

    def _run_ws(self, url_fn, on_msg, flag):
        import websocket as ws_lib
        while not self._stop.is_set():
            try:
                url = url_fn()
                app = ws_lib.WebSocketApp(
                    url,
                    on_open=lambda w: self.stats.__setitem__(flag, True),
                    on_message=lambda w, m: self._safe(on_msg, m),
                    on_close=lambda w, *a: self.stats.__setitem__(flag, False),
                    on_error=lambda w, e: logger.debug("[FastMaxLoss] ws error %s", e),
                )
                app.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                logger.warning("[FastMaxLoss] %s stream error: %s", flag, exc)
            self.stats[flag] = False
            self._stop.wait(3)

    def _safe(self, fn, raw):
        try:
            fn(json.loads(raw))
        except Exception as exc:
            logger.debug("[FastMaxLoss] message error: %s", exc)

    def _user_url(self):
        if not self._listen_key:
            self._listen_key = self._new_listen_key()
        try:
            self.seed_positions()  # catch anything that changed while disconnected
        except Exception as exc:
            logger.warning("[FastMaxLoss] seed failed: %s", exc)
        return PRIVATE_WS + self._listen_key

    def _resync(self):
        while not self._stop.wait(RESYNC_S):
            try:
                self.seed_positions()
            except Exception:
                pass

    def start(self):
        for name, target, args in (
            ("fastml-user", self._run_ws, (self._user_url, self.on_user_event, "user_connected")),
            ("fastml-mark", self._run_ws, (lambda: MARK_WS, self.on_mark_message, "mark_connected")),
            ("fastml-keepalive", self._keepalive, ()),
            ("fastml-resync", self._resync, ()),
        ):
            threading.Thread(target=target, args=args, name=name, daemon=True).start()
        logger.info("[FastMaxLoss] started (user-data + all-market mark price)")

    def stop(self):
        self._stop.set()
