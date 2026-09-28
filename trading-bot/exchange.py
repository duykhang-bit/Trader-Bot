# ============================================================
# BINANCE FUTURES API WRAPPER
# ============================================================
import logging
import time
from typing import Optional
import requests
import hmac
import hashlib
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


class BinanceFutures:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.api_key = api_key
        self.api_secret = api_secret
        if testnet:
            self.base_url = "https://testnet.binancefuture.com"
        else:
            import config as _cfg
            self.base_url = getattr(_cfg, "LIVE_BASE_URL", "https://fapi.binance.com")
        self.session = requests.Session()
        self.session.headers.update({
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/json"
        })
        self._tick_cache: dict = {}   # symbol -> tick size (float), load 1 lần từ exchangeInfo

    def _sign(self, params: dict) -> dict:
        """Ký request với HMAC SHA256"""
        params["timestamp"] = int(time.time() * 1000)
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _get(self, endpoint: str, params: dict = None, signed: bool = False, retries: int = 3):
        params = params or {}
        if signed:
            params = self._sign(params)
        for attempt in range(retries):
            try:
                resp = self.session.get(
                    f"{self.base_url}{endpoint}", params=params, timeout=10
                )
                if resp.status_code == 418 or resp.status_code == 429:
                    wait = min(60, 15 * (attempt + 1))
                    logger.warning(f"Rate limited ({resp.status_code}), waiting {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                if "418" in str(e) or "429" in str(e):
                    wait = min(60, 15 * (attempt + 1))
                    logger.warning(f"Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"GET {endpoint} failed: {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise

    def _post(self, endpoint: str, params: dict = None, retries: int = 3):
        """POST with retries only for transient failures.

        Binance 4xx business errors are deterministic and must return
        immediately.  Re-sign each retry so rate-limit waits do not reuse an
        expired timestamp/signature.
        """
        base_params = dict(params or {})
        for attempt in range(retries):
            try:
                request_params = self._sign(dict(base_params))
                resp = self.session.post(
                    f"{self.base_url}{endpoint}", params=request_params, timeout=10
                )
                if resp.status_code in (418, 429):
                    if attempt >= retries - 1:
                        resp.raise_for_status()
                    retry_after = resp.headers.get("Retry-After", "")
                    try:
                        wait = max(1, min(60, int(float(retry_after))))
                    except (TypeError, ValueError):
                        wait = min(60, 15 * (attempt + 1))
                    logger.warning(
                        f"Rate limited POST ({resp.status_code}), waiting {wait}s..."
                    )
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    logger.error(
                        f"POST {endpoint} failed ({resp.status_code}): {resp.text[:300]}"
                    )
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                # Do not retry deterministic client/business errors.  Checking
                # the numeric status avoids false 429 matches in signed URLs.
                if status is not None and 400 <= status < 500:
                    raise
                logger.error(
                    f"POST {endpoint} failed (attempt {attempt+1}/{retries}): {e}"
                )
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise

    def _post_url(self, url: str, params: dict = None, retries: int = 3):
        """POST to absolute URL (for Portfolio Margin papi.binance.com)"""
        params = params or {}
        params = self._sign(params)
        for attempt in range(retries):
            try:
                resp = self.session.post(url, params=params, timeout=10)
                if resp.status_code == 418 or resp.status_code == 429:
                    wait = min(60, 15 * (attempt + 1))
                    logger.warning(f"Rate limited POST_URL ({resp.status_code}), waiting {wait}s...")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    logger.error(f"POST {url} failed ({resp.status_code}): {resp.text[:300]}")
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                if "418" in str(e) or "429" in str(e):
                    wait = min(60, 15 * (attempt + 1))
                    logger.warning(f"Rate limited POST_URL, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"POST {url} failed (attempt {attempt+1}/{retries}): {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise

    def _delete(self, endpoint: str, params: dict = None, retries: int = 3):
        params = params or {}
        params = self._sign(params)
        for attempt in range(retries):
            try:
                resp = self.session.delete(f"{self.base_url}{endpoint}", params=params, timeout=10)
                if resp.status_code == 418 or resp.status_code == 429:
                    wait = min(60, 15 * (attempt + 1))
                    logger.warning(f"Rate limited DELETE ({resp.status_code}), waiting {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                if "418" in str(e) or "429" in str(e):
                    wait = min(60, 15 * (attempt + 1))
                    logger.warning(f"Rate limited DELETE, waiting {wait}s...")
                    time.sleep(wait)
                    continue
                logger.error(f"DELETE {endpoint} failed (attempt {attempt+1}/{retries}): {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise

    # ---- Market Data ----

    def get_klines(self, symbol: str, interval: str, limit: int = 200) -> list:
        """Lấy candlestick data"""
        data = self._get("/fapi/v1/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        })
        return data

    def get_ticker_price(self, symbol: str) -> float:
        """Lấy giá hiện tại"""
        data = self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def get_mark_price(self, symbol: str) -> float:
        """Lấy mark price (dùng cho futures)"""
        data = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        return float(data["markPrice"])

    # ---- Account ----

    def get_qty_precision(self, symbol: str) -> tuple:
        """Lấy stepSize và maxQty từ Binance API cho từng coin"""
        try:
            info = self._get("/fapi/v1/exchangeInfo")
            for s in info.get("symbols", []):
                if s["symbol"] == symbol:
                    step = 1.0
                    max_q = 10000.0
                    decimals = 0
                    min_notional = 5.0
                    for f in s.get("filters", []):
                        # LOT_SIZE: stepSize cho tất cả order types
                        if f["filterType"] == "LOT_SIZE":
                            step = float(f.get("stepSize", 1))
                            max_q = float(f.get("maxQty", 10000))
                            if step >= 1:
                                decimals = 0
                            else:
                                decimals = len(str(step).rstrip('0').split('.')[-1])
                        # MARKET_LOT_SIZE: giới hạn riêng cho market order — ưu tiên hơn
                        elif f["filterType"] == "MARKET_LOT_SIZE":
                            market_max = float(f.get("maxQty", 0))
                            if market_max > 0:
                                max_q = min(max_q, market_max)
                        # MIN_NOTIONAL: giá trị tối thiểu của lệnh
                        elif f["filterType"] == "MIN_NOTIONAL":
                            min_notional = float(f.get("notional", 5.0))
                    return step, max_q, decimals, min_notional
        except Exception:
            pass
        return 1.0, 10000.0, 0, 5.0  # fallback

    def get_account_balance(self) -> float:
        """Lấy số dư USDT available"""
        data = self._get("/fapi/v2/balance", signed=True)
        for asset in data:
            if asset["asset"] == "USDT":
                return float(asset["availableBalance"])
        return 0.0

    def get_total_equity(self) -> float:
        """Lấy Margin Balance từ Futures account
        = totalMarginBalance = walletBalance + unrealizedPnL
        Khớp với 'Số dư ký quỹ' hiển thị trên Binance Futures app"""
        try:
            data = self._get("/fapi/v2/account", signed=True)
            margin_bal = float(data.get("totalMarginBalance", 0))
            return margin_bal if margin_bal > 0 else self.get_account_balance()
        except Exception:
            return self.get_account_balance()

    def get_position(self, symbol: str) -> Optional[dict]:
        """Lấy thông tin position hiện tại"""
        data = self._get("/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        for pos in data:
            if pos["symbol"] == symbol and float(pos["positionAmt"]) != 0:
                return pos
        return None

    def get_open_orders(self, symbol: str) -> list:
        """Lấy danh sách lệnh đang mở"""
        return self._get("/fapi/v1/openOrders", {"symbol": symbol}, signed=True)

    # ---- Trading ----

    @staticmethod
    def _binance_error_code(exc) -> Optional[int]:
        """Extract Binance's numeric error code from an HTTP exception."""
        try:
            payload = exc.response.json()
            return int(payload.get("code")) if isinstance(payload, dict) else None
        except (AttributeError, TypeError, ValueError):
            return None

    def set_leverage(self, symbol: str, leverage: int):
        """Set leverage and return the confirmed value, or ``None`` on failure.

        Only Binance ``-4028`` (unsupported leverage) permits lower-leverage
        fallback. Insufficient margin (``-2028``) is terminal: retrying lower
        values is slow and cannot fix the account constraint.
        """
        try:
            result = self._post("/fapi/v1/leverage", {
                "symbol": symbol,
                "leverage": leverage,
            })
            actual = int(result.get("leverage", leverage)) if isinstance(result, dict) else leverage
            logger.info(f"Leverage set to {actual}x for {symbol}")
            return actual
        except Exception as exc:
            code = self._binance_error_code(exc)
            if code == -2028:
                logger.warning(
                    f"Leverage unchanged for {symbol}: insufficient margin balance"
                )
                return None
            if code != -4028:
                logger.error(f"set_leverage {symbol} failed: {exc}")
                return None

        # Requested leverage is unsupported. Each lower value is attempted once
        # by _post because deterministic 4xx responses now fail immediately.
        for try_lev in (10, 7, 5, 3, 2, 1):
            if try_lev >= leverage:
                continue
            try:
                result = self._post("/fapi/v1/leverage", {
                    "symbol": symbol,
                    "leverage": try_lev,
                })
                actual = int(result.get("leverage", try_lev)) if isinstance(result, dict) else try_lev
                logger.info(
                    f"Leverage fallback to {actual}x for {symbol} "
                    f"(requested {leverage}x)"
                )
                return actual
            except Exception as exc:
                code = self._binance_error_code(exc)
                if code == -2028:
                    logger.warning(
                        f"Leverage unchanged for {symbol}: insufficient margin balance"
                    )
                    return None
                if code != -4028:
                    logger.error(f"set_leverage fallback {symbol} failed: {exc}")
                    return None

        logger.error(f"No supported leverage found for {symbol} (requested {leverage}x)")
        return None

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        """Set margin type: ISOLATED hoặc CROSSED"""
        try:
            result = self._post("/fapi/v1/marginType", {
                "symbol": symbol,
                "marginType": margin_type
            })
            logger.info(f"Margin type set to {margin_type} for {symbol}")
            return result
        except Exception as e:
            # Binance trả lỗi nếu margin type đã được set rồi hoặc Demo không support
            err_str = str(e)
            if "No need to change margin type" in err_str or "400" in err_str:
                logger.debug(f"Margin type skipped: {e}")
            else:
                raise

    def place_market_order(self, symbol: str, side: str, quantity: float,
                           reduce_only: bool = False,
                           client_order_id: str = "") -> dict:
        """
        Đặt lệnh market.

        Args:
            side: ``BUY`` hoặc ``SELL``.
            reduce_only: Bắt buộc lệnh chỉ được giảm/đóng vị thế hiện tại.
                Dùng ``True`` cho mọi close path để dữ liệu quantity cũ không
                thể vô tình đảo SHORT thành LONG (hoặc ngược lại).
        """
        # Convert to int if whole number (Binance rejects "113295.0" for some coins)
        if quantity == int(quantity):
            quantity = int(quantity)
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        result = self._post("/fapi/v1/order", params)
        tag = " reduce-only" if reduce_only else ""
        logger.info(f"Market{tag} order placed: {side} {quantity} {symbol} @ market")
        return result

    def _round_price(self, price: float, symbol: str = "", *, round_up: bool = False) -> float:
        """Round to the symbol tick; ``round_up`` is safe for SELL maker orders."""
        import math

        tick = self._tick_cache.get(symbol) if symbol else None
        if tick is None and symbol:
            try:
                info = self._get("/fapi/v1/exchangeInfo", signed=False)
                for item in info.get("symbols", []):
                    for price_filter in item.get("filters", []):
                        if price_filter.get("filterType") == "PRICE_FILTER":
                            tick_size = float(price_filter.get("tickSize", 0))
                            if tick_size > 0:
                                self._tick_cache[item["symbol"]] = tick_size
                tick = self._tick_cache.get(symbol)
            except Exception:
                tick = None

        if not tick or tick <= 0:
            if price >= 10000:
                decimals = 1
            elif price >= 10:
                decimals = 2
            elif price >= 1:
                decimals = 3
            elif price >= 0.1:
                decimals = 4
            elif price >= 0.01:
                decimals = 5
            else:
                decimals = 6
            tick = 10 ** -decimals
        else:
            decimals = max(0, -int(math.floor(math.log10(tick))))

        units = price / tick
        rounded_units = math.ceil(units - 1e-12) if round_up else round(units)
        return round(rounded_units * tick, decimals)

    def place_limit_order(self, symbol: str, side: str, quantity: float,
                          price: float, *, post_only: bool = False,
                          client_order_id: str = "") -> dict:
        """Place a LIMIT, optionally as a Binance Futures post-only GTX order.

        Post-only SELLs are rounded upward and checked against a fresh best bid
        immediately before submission. Binance GTX is the final race-safe guard:
        a quote move that would make the order marketable causes rejection.
        """
        if quantity == int(quantity):
            quantity = int(quantity)
        side = side.upper()
        limit_price = self._round_price(
            price, symbol, round_up=post_only and side == "SELL"
        )
        if post_only:
            book = self._get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
            best_bid = float(book.get("bidPrice", 0) or 0)
            best_ask = float(book.get("askPrice", 0) or 0)
            if best_bid <= 0 or best_ask <= 0:
                raise ValueError(f"invalid fresh book for post-only {symbol}")
            if side == "SELL" and limit_price <= best_bid:
                raise ValueError(
                    f"post-only SELL {symbol} {limit_price} is marketable at bid {best_bid}"
                )
            if side == "BUY" and limit_price >= best_ask:
                raise ValueError(
                    f"post-only BUY {symbol} {limit_price} is marketable at ask {best_ask}"
                )

        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "quantity": quantity,
            "price": limit_price,
            "timeInForce": "GTX" if post_only else "GTC",
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        result = self._post("/fapi/v1/order", params)
        if isinstance(result, dict):
            result.setdefault("price", str(limit_price))
            result.setdefault("origClientOrderId", client_order_id)
        logger.info(
            f"Limit order placed: {side} {quantity} {symbol} @ {limit_price} "
            f"({'GTX' if post_only else 'GTC'})"
        )
        return result

    def place_stop_loss_order(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        """SL — dùng Algo Conditional Order API, fallback sang order thường"""
        price = self._round_price(stop_price, symbol)
        if quantity == int(quantity):
            quantity = int(quantity)
        try:
            result = self._post("/fapi/v1/algoOrder", {
                "symbol": symbol, "side": side,
                "algotype": "CONDITIONAL",
                "type": "STOP_MARKET",
                "triggerPrice": str(price),
                "quantity": str(quantity),
                "reduceOnly": "true",
                "workingType": "MARK_PRICE"
            })
            logger.info(f"SL placed (algo): {side} {symbol} qty={quantity} @ {price}")
            return result
        except Exception as e:
            logger.debug(f"SL algo failed for {symbol}: {e} — fallback to regular order")
            result = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": side,
                "type": "STOP_MARKET",
                "stopPrice": str(price),
                "quantity": str(quantity),
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
                "timeInForce": "GTC"
            })
            logger.info(f"SL placed (regular): {side} {symbol} qty={quantity} @ {price}")
            return result

    def place_take_profit_order(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        """TP — dùng Algo Conditional Order API, fallback sang order thường"""
        price = self._round_price(stop_price, symbol)
        if quantity == int(quantity):
            quantity = int(quantity)
        try:
            result = self._post("/fapi/v1/algoOrder", {
                "symbol": symbol, "side": side,
                "algotype": "CONDITIONAL",
                "type": "TAKE_PROFIT_MARKET",
                "triggerPrice": str(price),
                "quantity": str(quantity),
                "reduceOnly": "true",
                "workingType": "MARK_PRICE"
            })
            logger.info(f"TP placed (algo): {side} {symbol} qty={quantity} @ {price}")
            return result
        except Exception as e:
            logger.debug(f"TP algo failed for {symbol}: {e} — fallback to regular order")
            result = self._post("/fapi/v1/order", {
                "symbol": symbol, "side": side,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": str(price),
                "quantity": str(quantity),
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
                "timeInForce": "GTC"
            })
            logger.info(f"TP placed (regular): {side} {symbol} qty={quantity} @ {price}")
            return result

    def cancel_all_orders(self, symbol: str):
        """Hủy tất cả lệnh đang mở — bao gồm cả algo orders (TP/SL Market)"""
        result = self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
        # Cancel thêm algo orders (TP/SL Market) vì allOpenOrders không cancel được
        try:
            algo_orders = self._get("/fapi/v1/openAlgoOrders", signed=True)
            if isinstance(algo_orders, list):
                for o in algo_orders:
                    if o.get("symbol") == symbol:
                        try:
                            self._delete("/fapi/v1/algoOrder", {"algoId": o.get("algoId", ""), "symbol": symbol})
                        except Exception:
                            pass
        except Exception:
            pass
        logger.info(f"All open orders cancelled for {symbol}")
        return result

    def close_position(self, symbol: str, position: dict):
        """Đóng position hiện tại bằng market order"""
        amt = float(position["positionAmt"])
        if amt == 0:
            return
        side = "SELL" if amt > 0 else "BUY"
        quantity = abs(amt)
        return self.place_market_order(symbol, side, quantity, reduce_only=True)

    def get_realized_pnl(self, symbol: str, start_time_ms: int) -> float:
        """
        Lấy PnL realized chính xác từ Binance /fapi/v1/income.
        Đây là số liệu khớp với app Binance (đã trừ phí).

        Args:
            symbol: VD "BTCUSDT"
            start_time_ms: timestamp mở lệnh (milliseconds) để lọc đúng lệnh này

        Returns:
            float: tổng realized PnL (USDT), đã trừ phí
        """
        try:
            records = self._get("/fapi/v1/income", {
                "symbol":     symbol,
                "incomeType": "REALIZED_PNL",
                "startTime":  start_time_ms,
                "limit":      50,
            }, signed=True)

            if not records:
                return 0.0

            total = sum(float(r.get("income", 0)) for r in records)
            return total
        except Exception as e:
            logger.warning(f"get_realized_pnl {symbol} failed: {e}")
            return 0.0
