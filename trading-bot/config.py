# ============================================================
# TRADING BOT CONFIG — Copy file này thành config.py
# và điền API key của bạn vào
# ============================================================

# --- Watchlist Mode ---
# "fixed"   → chỉ quét đúng các coin trong FIXED_COINS bên dưới
# "dynamic" → tự động lấy top coin theo volume từ Binance (chế độ cũ)
WATCHLIST_MODE = "fixed"

# Danh sách coin khi dùng WATCHLIST_MODE = "fixed"
# Thêm/bớt coin tùy ý, phải có hậu tố USDT
FIXED_COINS = [
    "BTCUSDT",
    "SOLUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "NFPUSDT",
    "4USDT",
    "SIRENUSDT",
    "PONDUSDT",
]

# --- Binance API ---
API_KEY    = "qVexTMqcOXpOn1hhCUYIft0mTC02mUG6AjqcAH5qhm0oelCXHQY0Gf5Pa3sbnNFs"
API_SECRET = "bSeITXHc8BPDH8Ih3xrozBbhBAurTvJUP5jsI2whqi8SLrKvrlIFFDaJJRmDZo8X"

USE_TESTNET   = True
LIVE_BASE_URL = "https://testnet.binancefuture.com"  # Testnet
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
TRAILING_STOP      = True
TRAILING_STOP_PCT  = 0.015
MAX_LOSS_PER_POSITION = 10.0   # Lỗ tối đa $10/lệnh → tự đóng ngay

# --- Strategy ---
MIN_SCORE           = 50.0
COOLDOWN_AFTER_LOSS = 300

# --- Bot Settings ---
LOOP_INTERVAL_SECONDS = 60
LOG_LEVEL = "INFO"
LOG_FILE  = "logs/bot.log"

# --- Web Dashboard ---
WEB_DASHBOARD_PORT = 5555   # Mở http://localhost:5555 để xem

# ============================================================
# LIQUIDATION STRATEGY CONFIG
# ============================================================

# Bật/tắt liquidation strategy
# True  → dùng liq strategy (2 lệnh split theo heatmap)
# False → chỉ dùng strategy cũ (scan + signal)
LIQ_STRATEGY_ENABLED = True

# Bucket size: 0.001 = 0.1% mỗi bucket
# BTC/ETH dùng 0.001 (giá cao, cần bucket nhỏ)
# Altcoins dùng 0.002 (0.2%)
LIQ_BUCKET_PCT = 0.001

# Ngưỡng USD tối thiểu để 1 vùng được coi là "liquidation zone"
# Tăng lên nếu muốn chỉ trade vùng liq rất lớn (ít lệnh hơn, chắc hơn)
# Giảm xuống nếu muốn nhiều setup hơn
LIQ_MIN_USD    = 100_000   # $100k minimum tại vùng entry

# Ngưỡng USD cho vùng TP — cần lớn hơn entry vì đây là "mục tiêu"
LIQ_MIN_TP_USD = 200_000   # $200k minimum tại vùng TP

# SL đặt cách đỉnh vùng liq entry2 bao nhiêu %
# 0.02 = 2% (như mày yêu cầu)
LIQ_SL_BUFFER_PCT = 0.02

# Entry offset: vào lệnh cách vùng liq bao nhiêu %
# 0.001 = 0.1% (vào ngay sát vùng liq)
LIQ_ENTRY_OFFSET_PCT = 0.001

# Khoảng cách tối thiểu giữa entry1 và entry2
# 0.005 = 0.5% (entry2 phải xa entry1 ít nhất 0.5%)
LIQ_ENTRY2_MIN_GAP = 0.005

# Confidence tối thiểu để vào lệnh (0-100)
# 40 = thấp (nhiều lệnh hơn), 60 = cao (ít lệnh, chắc hơn)
LIQ_MIN_CONFIDENCE = 40

# Timeout setup: nếu lệnh 1 chưa khớp sau X giờ → huỷ setup
LIQ_SETUP_TIMEOUT_HOURS = 6


# ============================================================
# AI ANALYSIS (TradingAgents) CONFIG
# ============================================================

# Bật/tắt tự động chạy AI phân tích
AI_AUTO_ANALYSIS = True

# Interval: mỗi bao lâu chạy lại (giờ)
AI_ANALYSIS_INTERVAL_HOURS = 4
#Run bot 
#cd /Users/leduykhang/Documents/Trading/trading-bot/trading-bot python3 bot.py

#http://localhost:5555/