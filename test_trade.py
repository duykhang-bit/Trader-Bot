#!/usr/bin/env python3
# ============================================================
# TEST TRADE — Force vào 1 lệnh LONG nhỏ để test flow
# ============================================================
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import config
from exchange import BinanceFutures
from indicators import calculate_atr
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

    print("=" * 50)
    print("  TEST TRADE — BTCUSDT LONG (size nhỏ nhất)")
    print("=" * 50)

    # Lấy giá và ATR
    klines = ex.get_klines(config.SYMBOL, "15m", limit=50)
    df = klines_to_df(klines)
    price = df["close"].iloc[-1]
    atr   = calculate_atr(df["high"], df["low"], df["close"]).iloc[-1]

    sl = round(price - atr * 1.5, 1)
    tp = round(price + atr * 3.0, 1)
    qty = 0.001  # BTC min lot — nhỏ nhất có thể

    print(f"\n  Price  : ${price:,.2f}")
    print(f"  SL     : ${sl:,.2f}  (-{((price-sl)/price*100):.2f}%)")
    print(f"  TP     : ${tp:,.2f}  (+{((tp-price)/price*100):.2f}%)")
    print(f"  Size   : {qty} BTC (min lot)")
    print(f"  RR     : 1:{(tp-price)/(price-sl):.1f}")

    confirm = input("\n  Xác nhận vào lệnh LONG test? (y/n): ")
    if confirm.lower() != 'y':
        print("  Cancelled.")
        return

    print("\n  Đặt lệnh market LONG...")
    ex.set_margin_type(config.SYMBOL, "ISOLATED")
    ex.set_leverage(config.SYMBOL, config.LEVERAGE)
    ex.place_market_order(config.SYMBOL, "BUY", qty)
    print(f"  ✅ LONG {qty} BTC @ ~${price:,.2f}")

    print(f"  Đặt Stop Loss @ ${sl:,.2f}...")
    ex.place_stop_loss_order(config.SYMBOL, "SELL", qty, sl)
    print(f"  ✅ SL set")

    print(f"  Đặt Take Profit @ ${tp:,.2f}...")
    ex.place_take_profit_order(config.SYMBOL, "SELL", qty, tp)
    print(f"  ✅ TP set")

    print("\n  ✅ Lệnh đã vào! Vào Binance Demo để xem:")
    print("  https://demo.binance.com/en/futures/BTCUSDT")
    print("\n  Chạy 'python3 status.py' để theo dõi PnL realtime")

if __name__ == "__main__":
    main()
