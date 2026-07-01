# ============================================================
# GRID TRADING STRATEGY
# Tự động mua thấp bán cao trong một khoảng giá
# ============================================================
import logging
import math

logger = logging.getLogger(__name__)


class GridBot:
    """
    Grid Trading Bot:
    - Chia khoảng [lower, upper] thành N lưới
    - Đặt lệnh BUY ở mỗi mức giá thấp hơn giá hiện tại
    - Đặt lệnh SELL ở mỗi mức giá cao hơn giá hiện tại
    - Khi BUY hit → đặt SELL ở lưới trên
    - Khi SELL hit → đặt BUY ở lưới dưới
    """

    def __init__(self, symbol: str, lower: float, upper: float,
                 grids: int, total_usdt: float, exchange, notifier):
        self.symbol      = symbol
        self.lower       = lower
        self.upper       = upper
        self.grids       = grids
        self.total_usdt  = total_usdt   # Tổng vốn cho grid này
        self.exchange    = exchange
        self.notifier    = notifier

        # Tính các mức giá lưới
        self.grid_prices = self._calc_grid_prices()
        self.usdt_per_grid = total_usdt / grids
        self.active_orders = {}   # {price: order_id}
        self.total_profit  = 0.0
        self.trade_count   = 0

        logger.info(f"Grid Bot {symbol}: {lower}-{upper} | {grids} grids | ${total_usdt} USDT")
        logger.info(f"Grid prices: {[f'${p:.4f}' for p in self.grid_prices]}")

    def _calc_grid_prices(self) -> list:
        """Tính các mức giá lưới (arithmetic)"""
        step = (self.upper - self.lower) / self.grids
        return [round(self.lower + i * step, 6) for i in range(self.grids + 1)]

    def _calc_qty(self, price: float) -> float:
        """Tính số lượng mỗi lệnh, đảm bảo không về 0 và đủ min notional $5"""
        qty = self.usdt_per_grid / price
        # Làm tròn theo bước tối thiểu của từng coin
        if price >= 10000:  qty = round(qty, 3)   # BTC
        elif price >= 1000: qty = round(qty, 3)   # ETH, BNB
        elif price >= 100:  qty = round(qty, 2)
        elif price >= 1:    qty = round(qty, 1)
        else:               qty = round(qty, 0)
        # Đảm bảo notional tối thiểu $5 (Binance demo)
        min_notional = 5.0
        if qty * price < min_notional:
            qty = round(min_notional / price * 1.05, 3)  # +5% buffer
        return max(qty, 0.001)

    def setup(self, current_price: float):
        """
        Khởi tạo grid: đặt lệnh BUY dưới giá hiện tại
        và lệnh SELL trên giá hiện tại
        """
        logger.info(f"Setting up grid for {self.symbol} @ ${current_price:.4f}")

        buy_prices  = [p for p in self.grid_prices if p < current_price]
        sell_prices = [p for p in self.grid_prices if p > current_price]

        # Đặt lệnh BUY limit ở các mức dưới
        for price in buy_prices[-5:]:  # Chỉ đặt 5 lệnh gần nhất
            qty = self._calc_qty(price)
            try:
                order = self.exchange._post("/fapi/v1/order", {
                    "symbol": self.symbol,
                    "side": "BUY",
                    "type": "LIMIT",
                    "price": round(price, 4),
                    "quantity": qty,
                    "timeInForce": "GTC"
                })
                self.active_orders[price] = order.get("orderId")
                logger.info(f"Grid BUY @ ${price:.4f} qty={qty}")
            except Exception as e:
                logger.error(f"Grid BUY order failed @ {price}: {e}")

        # Đặt lệnh SELL limit ở các mức trên
        for price in sell_prices[:5]:  # Chỉ đặt 5 lệnh gần nhất
            qty = self._calc_qty(price)
            try:
                order = self.exchange._post("/fapi/v1/order", {
                    "symbol": self.symbol,
                    "side": "SELL",
                    "type": "LIMIT",
                    "price": round(price, 4),
                    "quantity": qty,
                    "timeInForce": "GTC",
                    "reduceOnly": "false"
                })
                self.active_orders[price] = order.get("orderId")
                logger.info(f"Grid SELL @ ${price:.4f} qty={qty}")
            except Exception as e:
                logger.error(f"Grid SELL order failed @ {price}: {e}")

        msg = (f"🔲 GRID BOT STARTED\n\n"
               f"📊 {self.symbol}\n"
               f"📉 Lower: ${self.lower}\n"
               f"📈 Upper: ${self.upper}\n"
               f"🔢 Grids: {self.grids}\n"
               f"💰 Vốn: ${self.total_usdt} USDT\n"
               f"💵 Mỗi lưới: ${self.usdt_per_grid:.2f}")
        self.notifier.telegram.send(msg)

    def check_filled(self):
        """
        Kiểm tra lệnh nào đã được khớp
        Nếu BUY hit → đặt SELL ở lưới trên
        Nếu SELL hit → đặt BUY ở lưới dưới
        """
        try:
            open_orders = self.exchange.get_open_orders(self.symbol)
            open_ids = {o["orderId"] for o in open_orders}

            for price, order_id in list(self.active_orders.items()):
                if order_id not in open_ids:
                    # Lệnh đã khớp
                    self._on_order_filled(price)
                    del self.active_orders[price]

        except Exception as e:
            logger.error(f"Grid check_filled error: {e}")

    def _on_order_filled(self, filled_price: float):
        """Xử lý khi lệnh được khớp"""
        self.trade_count += 1
        idx = self.grid_prices.index(min(self.grid_prices, key=lambda x: abs(x - filled_price)))

        # Tìm lệnh gốc là BUY hay SELL
        # Nếu giá fill thấp → là BUY → đặt SELL ở lưới trên
        # Nếu giá fill cao → là SELL → đặt BUY ở lưới dưới
        current_price = self.exchange.get_ticker_price(self.symbol)

        if filled_price < current_price:
            # BUY đã hit → đặt SELL ở lưới trên
            if idx + 1 < len(self.grid_prices):
                sell_price = self.grid_prices[idx + 1]
                qty = self._calc_qty(sell_price)
                profit_per_grid = (sell_price - filled_price) * qty
                try:
                    order = self.exchange._post("/fapi/v1/order", {
                        "symbol": self.symbol,
                        "side": "SELL",
                        "type": "LIMIT",
                        "price": round(sell_price, 4),
                        "quantity": qty,
                        "timeInForce": "GTC"
                    })
                    self.active_orders[sell_price] = order.get("orderId")
                    logger.info(f"Grid: BUY filled @ {filled_price:.4f} → SELL @ {sell_price:.4f} (profit ~${profit_per_grid:.4f})")
                except Exception as e:
                    logger.error(f"Grid SELL after BUY failed: {e}")
        else:
            # SELL đã hit → đặt BUY ở lưới dưới
            if idx - 1 >= 0:
                buy_price = self.grid_prices[idx - 1]
                qty = self._calc_qty(buy_price)
                profit_per_grid = (filled_price - buy_price) * qty
                self.total_profit += profit_per_grid
                try:
                    order = self.exchange._post("/fapi/v1/order", {
                        "symbol": self.symbol,
                        "side": "BUY",
                        "type": "LIMIT",
                        "price": round(buy_price, 4),
                        "quantity": qty,
                        "timeInForce": "GTC"
                    })
                    self.active_orders[buy_price] = order.get("orderId")
                    logger.info(f"Grid: SELL filled @ {filled_price:.4f} → BUY @ {buy_price:.4f} (profit ~${profit_per_grid:.4f})")

                    self.notifier.telegram.send(
                        f"🔲 GRID PROFIT\n"
                        f"📊 {self.symbol}\n"
                        f"✅ SELL @ ${filled_price:.4f} → BUY @ ${buy_price:.4f}\n"
                        f"💵 Profit: ~${profit_per_grid:.4f}\n"
                        f"📈 Tổng: ${self.total_profit:.4f} ({self.trade_count} trades)"
                    )
                except Exception as e:
                    logger.error(f"Grid BUY after SELL failed: {e}")

    def stop(self):
        """Dừng grid, hủy tất cả lệnh"""
        try:
            self.exchange.cancel_all_orders(self.symbol)
            logger.info(f"Grid {self.symbol} stopped. Total profit: ${self.total_profit:.4f}")
            self.notifier.telegram.send(
                f"⛔ GRID BOT STOPPED\n"
                f"📊 {self.symbol}\n"
                f"💵 Tổng profit: ${self.total_profit:.4f}\n"
                f"🔢 Trades: {self.trade_count}"
            )
        except Exception as e:
            logger.error(f"Grid stop error: {e}")

    def get_status(self) -> dict:
        return {
            "symbol":       self.symbol,
            "lower":        self.lower,
            "upper":        self.upper,
            "grids":        self.grids,
            "total_usdt":   self.total_usdt,
            "active_orders": len(self.active_orders),
            "trade_count":  self.trade_count,
            "total_profit": round(self.total_profit, 4),
        }
