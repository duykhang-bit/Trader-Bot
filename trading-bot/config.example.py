# ============================================================
# TRADING BOT CONFIG — Copy file này thành config.py
# và điền API key của bạn vào
# ============================================================
import os

# Pending orders on these symbols are preserved by automatic cleanup.
# The web dashboard persists runtime changes to a JSON sidecar file.
PROTECTED_PENDING_ORDER_COINS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT"
]

# --- Binance API ---
API_KEY    = "YOUR_API_KEY_HERE"
API_SECRET = "YOUR_API_SECRET_HERE"

USE_TESTNET   = False
LIVE_BASE_URL = "https://demo-fapi.binance.com"  # Demo
# LIVE_BASE_URL = "https://fapi.binance.com"     # Live thật

# --- Timeframe ---
SYMBOL       = "BTCUSDT"
INTERVAL     = "15m"
HTF_INTERVAL = "1h"
LEVERAGE     = 10

# --- RSI ---
RSI_PERIOD     = 14
RSI_OVERSOLD   = 35
RSI_OVERBOUGHT = 65

# --- EMA ---
EMA_FAST  = 9
EMA_SLOW  = 21
EMA_TREND = 50

# --- MACD ---
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9

# --- Volume ---
VOLUME_MULTIPLIER = 1.0

# --- ATR ---
ATR_PERIOD        = 14
ATR_SL_MULTIPLIER = 2.0
ATR_TP_MULTIPLIER = 4.0

# --- Risk Management ---
RISK_PER_TRADE     = 0.01
STOP_LOSS_PCT      = 0.02
MAX_OPEN_POSITIONS = 3
MAX_ORDER_USDT     = 15.0
# --- Max Loss Safety Net (đặt trên web) ---
# Web dashboard tự tính gợi ý này theo config + vốn thật, cập nhật mỗi 5 giây.
# Giá trị lệnh = MAX_ORDER_USDT × LEVERAGE. Thanh lý khi giá chạy ngược
# ~(100/LEVERAGE − MMR)%; MMR coin alt bot hay vào 1.5–5% (BTC/ETH 0.4%),
# nên tính MMR 2%. Max Loss = min(trước thanh lý 1.5% cho trượt giá, 3% vốn),
# và không thấp hơn lỗ SL thường (trung vị 1.8% trên 292 lệnh bot).
# Chuẩn quản lý vốn: rủi ro 1–2% vốn/lệnh, tổng margin các lệnh ≤ 50% vốn.
#   $15 × 10x = $150 → thanh lý ~8.0% → Max Loss ≤ $9.8  (và ≤ 3% vốn)
#   $15 × 15x = $225 → thanh lý ~4.7% → Max Loss ≤ $7.1  (vốn $230 → $6.9)
#   $20 × 15x = $300 → thanh lý ~4.7% → Max Loss ≤ $9.5  (và ≤ 3% vốn)
#   $30 × 15x = $450 → thanh lý ~4.7% → Max Loss ≤ $14.3 (và ≤ 3% vốn)
#   $15 × 20x = $300 → thanh lý ~3.0% → Max Loss ≤ $4.5  < SL thường $5.4: leverage quá cao
# Max Loss lớn hơn mức thanh lý (vd $20 ở $15×15x) là vô dụng: sàn thanh lý trước.
# Max Loss áp dụng cho MỌI vị thế, kể cả lệnh tay. Lệnh tay nên có SL riêng.
MAX_LOSS_ENABLED      = True
MAX_LOSS_PER_POSITION = 7.0
TRAILING_STOP      = True
TRAILING_STOP_PCT  = 0.015

# --- Strategy ---
MIN_SCORE           = 70.0
COOLDOWN_AFTER_LOSS = 300
TREND_CONFLICT_SKIP = True  # không vào khi trend 4h và 1h ngược nhau

# --- Bot Settings ---
LOOP_INTERVAL_SECONDS = 60
LOG_LEVEL = "INFO"
LOG_FILE  = "logs/bot.log"

# --- Macro Economic Calendar ---
# Official schedules work without a key. This optional key only enriches consensus/actual values.
MACRO_CALENDAR_ENABLED = True
MACRO_CALENDAR_TIMEZONE = "Asia/Ho_Chi_Minh"
MACRO_CALENDAR_REFRESH_SECONDS = 21600
MACRO_CALENDAR_STALE_SECONDS = 43200
MACRO_CALENDAR_REMINDER_ENABLED = True
MACRO_CALENDAR_REMINDER_HOURS = 1
MACRO_CALENDAR_ODDS_REFRESH_SECONDS = 900
MACRO_CALENDAR_ACTUAL_REFRESH_SECONDS = 60
MACRO_CALENDAR_REMINDER_WINDOW_SECONDS = 3600
MACRO_CALENDAR_REMINDER_ONLY_HIGH = True
MACRO_CALENDAR_CACHE_FILE = "logs/macro_calendar.json"
TRADING_ECONOMICS_API_KEY = os.environ.get("TRADING_ECONOMICS_API_KEY", "")

# --- Pump auto-entry safety (closed 1m BOS required) ---
PUMP_BOS_LOOKBACK_CANDLES = 30
PUMP_BOS_WATCH_TTL_SEC = 180
PUMP_ENTRY_RESERVATION_TTL_SEC = 20
PUMP_ENTRY_COOLDOWN_SEC = 7
PUMP_MARKET_ENTRY_PROXIMITY_PCT = 0.20
PUMP_MAX_ABOVE_ENTRY_PCT = 0.75
PUMP_MAX_RETEST_DISTANCE_PCT = 5.0
