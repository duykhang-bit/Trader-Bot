#!/usr/bin/env python3
"""
Test full flow: gửi Telegram confirm → bấm nút → vào lệnh thật
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import config
from exchange import BinanceFutures
from indicators import calculate_atr
from notifier import Notifier
import pandas as pd

def klines_to_df(klines):
    df = pd.DataFrame(klines, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_volume","trades",
        "taker_buy_base","taker_buy_quote","ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df

def main():
    ex = BinanceFutures(config.API_KEY, config.API_SECRET, config.USE_TESTNET)
    notifier = Notifier()

    # Lấy giá thật
    klines = ex.get_klines(config.SYMBOL, "15m", limit=50)
    df = klines_to_df(klines)
    price = df["close"].iloc[-1]
    atr   = calculate_atr(df["high"], df["low"], df["close"]).iloc[-1]
    balance = ex.get_account_balance()

    sl = round(price - atr * 1.5, 1)
    tp = round(price + atr * 3.0, 1)
    qty = 0.001  # min lot BTC

    print(f"Giá hiện tại: ${price:,.2f}")
    print(f"SL: ${sl:,.2f} | TP: ${tp:,.2f}")
    print("Đang gửi Telegram xin xác nhận...")

    # Gửi confirm lên Telegram
    confirmed = notifier.request_confirm(
        signal="LONG",
        symbol=config.SYMBOL,
        price=price,
        sl=sl,
        tp=tp,
        balance=balance,
        reason="Test full flow"
    )

    if not confirmed:
        print("❌ Không xác nhận — không vào lệnh.")
        return

    # Vào lệnh thật
    print(f"✅ Đã xác nhận! Đang vào lệnh LONG {qty} BTC...")
    ex.set_leverage(config.SYMBOL, config.LEVERAGE)
    ex.place_market_order(config.SYMBOL, "BUY", qty)
    ex.place_stop_loss_order(config.SYMBOL, "SELL", qty, sl)
    ex.place_take_profit_order(config.SYMBOL, "SELL", qty, tp)

    print(f"✅ LONG {qty} BTC @ ~${price:,.2f}")
    print(f"✅ SL: ${sl:,.2f} | TP: ${tp:,.2f}")

    # Gửi thông báo đã vào lệnh
    notifier.notify_signal("LONG", config.SYMBOL, price, sl, tp, balance, "Test flow OK")
    print("\nXem lệnh tại: https://demo.binance.com/en/futures/BTCUSDT")

if __name__ == "__main__":
    main()
