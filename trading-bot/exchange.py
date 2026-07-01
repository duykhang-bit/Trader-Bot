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
        self.base_url = (
            "https://testnet.binancefuture.com" if testnet
            else "https://demo-fapi.binance.com"
        )
        self.session = requests.Session()
        self.session.headers.update({
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/json"
        })
        # Binance Demo Futures dùng self-signed cert → tắt SSL verify
        if not testnet:
            self.session.verify = False
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                logger.error(f"GET {endpoint} failed: {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)  # backoff: 1s, 2s, 4s
                else:
                    raise

    def _post(self, endpoint: str, params: dict = None):
        params = params or {}
        params = self._sign(params)
        try:
            resp = self.session.post(
                f"{self.base_url}{endpoint}", params=params, timeout=10
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"POST {endpoint} failed: {e}")
            raise

    def _delete(self, endpoint: str, params: dict = None):
        params = params or {}
        params = self._sign(params)
        try:
            resp = self.session.delete(f"{self.base_url}{endpoint}", params=params)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"DELETE {endpoint} failed: {e}")
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

    def get_account_balance(self) -> float:
        """Lấy số dư USDT available"""
        data = self._get("/fapi/v2/balance", signed=True)
        for asset in data:
            if asset["asset"] == "USDT":
                return float(asset["availableBalance"])
        return 0.0

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

    def set_leverage(self, symbol: str, leverage: int):
        """Set đòn bẩy"""
        result = self._post("/fapi/v1/leverage", {
            "symbol": symbol,
            "leverage": leverage
        })
        logger.info(f"Leverage set to {leverage}x for {symbol}")
        return result

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

    def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        """
        Đặt lệnh market
        side: 'BUY' hoặc 'SELL'
        """
        result = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity
        })
        logger.info(f"Market order placed: {side} {quantity} {symbol} @ market")
        return result

    def _round_price(self, price: float) -> float:
        """Làm tròn giá đúng theo độ lớn của coin"""
        if price >= 10000:
            return round(price, 1)   # BTC: tick 0.1
        elif price >= 1000:
            return round(price, 2)   # ETH: tick 0.01
        elif price >= 10:
            return round(price, 2)   # SOL, BNB: tick 0.01
        elif price >= 1:
            return round(price, 4)
        elif price >= 0.1:
            return round(price, 4)
        else:
            return round(price, 6)

    def place_stop_loss_order(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        """SL — dùng STOP_MARKET với closePosition"""
        price = self._round_price(stop_price)
        result = self._post("/fapi/v1/order", {
            "symbol": symbol, "side": side,
            "type": "STOP_MARKET",
            "stopPrice": price,
            "closePosition": "true",
            "workingType": "MARK_PRICE"
        })
        logger.info(f"SL (STOP_MARKET) {side} @ {price}")
        return result

    def place_take_profit_order(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        """TP — dùng TAKE_PROFIT_MARKET với closePosition"""
        price = self._round_price(stop_price)
        result = self._post("/fapi/v1/order", {
            "symbol": symbol, "side": side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": price,
            "closePosition": "true",
            "workingType": "MARK_PRICE"
        })
        logger.info(f"TP (TAKE_PROFIT_MARKET) {side} @ {price}")
        return result

    def cancel_all_orders(self, symbol: str):
        """Hủy tất cả lệnh đang mở"""
        result = self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
        logger.info(f"All open orders cancelled for {symbol}")
        return result

    def close_position(self, symbol: str, position: dict):
        """Đóng position hiện tại bằng market order"""
        amt = float(position["positionAmt"])
        if amt == 0:
            return
        side = "SELL" if amt > 0 else "BUY"
        quantity = abs(amt)
        return self.place_market_order(symbol, side, quantity)
