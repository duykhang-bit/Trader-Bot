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
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "NEARUSDT",
    "HYPEUSDT",
    "SPCXUSDT",
    "ZECUSDT",
    "LINKUSDT",
    "TLMUSDT",
    "VANRYUSDT",
    "AIAUSDT",
    "DEXEUSDT",
    "ONUSDT",
    "BEATUSDT",
    "UBUSDT",
    "BOTUSDT",
]

# --- Binance API ---
import os
API_KEY    = os.environ.get("BINANCE_API_KEY", "4nvrEO0C9JJaRLsvbQP4Foq6ZZqSrY3GFgxiBBBqWTllKm2UwNYqdgkGY093SX2J")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "FwTPwL0tgVlfDDQpZIdjaUZyyJ28OaD5RxSfMeqCwztIAuRm2bMFc04RwOoY7lSc")

USE_TESTNET   = False
LIVE_BASE_URL = os.environ.get("BINANCE_BASE_URL", "https://fapi.binance.com")

# --- Timeframe ---
SYMBOL       = "BTCUSDT"
INTERVAL     = "15m"
HTF_INTERVAL = "1h"
LEVERAGE     = 15

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
MAX_OPEN_POSITIONS = 6
MAX_ORDER_USDT     = 15.0
TRAILING_STOP      = True
TRAILING_STOP_PCT  = 0.015

# Trailing Profit Lock — dời SL lên lock lãi khi gần TP
TRAILING_LOCK_ENABLED = True
MAX_LOSS_PER_POSITION = 20.0
MAX_LOSS_PCT_PER_POSITION = 0.20
MAX_LOSS_ENABLED = True  # Bật/tắt safety net — tự đóng lệnh khi lỗ vượt MAX_LOSS

# Bật/tắt Position Reversal Monitor — tự động chốt lời khi phát hiện đảo chiều
# True  = bot tự đóng khi có 2/3 tín hiệu đảo chiều
# False = chỉ cảnh báo Telegram, không tự đóng
REVERSAL_MONITOR_ENABLED = True
REVERSAL_ALERT_ONLY      = False   # True = chỉ alert, không đóng lệnh

# ============================================================
# AUTO PROFIT LOCK — Tự chốt lời khi coin bay mạnh mà TP còn xa
# ============================================================
# Khi LONG mà coin bay lên mạnh bất ngờ nhưng TP xa → giá quay đầu mất lời
# Khi SHORT mà coin dump nhanh nhưng TP xa → giá hồi lại mất lời
# Feature này phát hiện tốc độ bay + unrealized profit cao → chốt ngay

# Bật/tắt Auto Profit Lock
PROFIT_LOCK_ENABLED = True

# Ngưỡng % lời TỐI THIỂU để bắt đầu xét chốt (unrealized PnL %)
# Ví dụ: 3.0 = cần lời ít nhất 3% (đã nhân leverage) mới xét
PROFIT_LOCK_MIN_PCT = 3.0

# Ngưỡng % lời CAO để chốt ngay khi có dấu hiệu yếu (không cần đủ 2/3 signal)
# Ví dụ: 5.0 = lời >= 5% (đã nhân leverage) → chốt luôn không cần tín hiệu
PROFIT_LOCK_HIGH_PCT = 5.0

# Tốc độ bay: giá thay đổi >= X% trong 3 nến 1m gần nhất → coi là "bay mạnh"
# Ví dụ: 0.5 = giá tăng/giảm >= 0.5% trong 3 giây (× lev 15x = 7.5% PnL)
# Coin altcoin bay 0.5% trong 3s là rất nhanh rồi
PROFIT_LOCK_SPEED_PCT = 0.5

# Cửa sổ dài hơn: giá bay >= X% trong 10 giây → cũng trigger
# TUT bay +9% trong vài phút → mỗi 10s khoảng +1%
PROFIT_LOCK_SPEED_10S_PCT = 1.0

# Tỷ lệ TP đã đạt được (%) — nếu giá đã đi được >= X% khoảng cách entry→TP
# mà đang quay đầu → chốt. Ví dụ: 40 = đã đi được 40% quãng đường đến TP
PROFIT_LOCK_TP_PROGRESS_PCT = 40.0

# Interval check (giây) — mỗi bao lâu check 1 lần
PROFIT_LOCK_INTERVAL = 5

# --- Strategy ---
MIN_SCORE           = 50.0
COOLDOWN_AFTER_LOSS = 300

# --- Bot Settings ---
LOOP_INTERVAL_SECONDS = 60
LOG_LEVEL = "INFO"
LOG_FILE  = "logs/bot.log"

# --- Web Dashboard ---
WEB_DASHBOARD_PORT = int(os.environ.get("PORT", 5555))

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
# PUMP DETECTOR CONFIG
# ============================================================

# Bật/tắt pump detector (scan đỉnh pump để SHORT)
PUMP_DETECTOR_ENABLED = True

# % tăng tối thiểu từ đáy để coi là "đang pump"
# 15 = coin phải tăng ít nhất 15% từ đáy → mới bắt đầu check đỉnh
PUMP_PRICE_RISE_PCT = 15.0

# Số nến 1m nhìn lại để tìm đáy gần nhất
PUMP_LOOKBACK_CANDLES = 20

# Volume exhaustion: volume nến hiện tại <= X% đỉnh volume đợt pump → kiệt sức
# 0.45 = volume hiện tại <= 45% đỉnh → dấu hiệu xả hàng xong
VOL_EXHAUST_RATIO = 0.45

# Số nến cuối để tính volume trung bình khi check exhaustion
VOL_EXHAUST_CANDLES = 5

# Wick rejection: bóng nến trên >= X lần thân nến → bị reject mạnh
WICK_REJECT_RATIO = 1.8

# RSI divergence: số nến nhìn lại để tìm RSI peak trước
RSI_DIV_LOOKBACK = 10

# Price deceleration: số nến cuối để đo đà giảm tốc
DECEL_CANDLES = 3

# Score tối thiểu để xác nhận đỉnh pump (0-100)
# 65 = cần đủ 65/100 điểm từ 6 tín hiệu mới gửi SHORT alert
# Nâng lên 65 (từ 60) để lọc bớt false positive
PUMP_TOP_MIN_SCORE = 50

# Cooldown: không spam signal cho cùng 1 coin (giây)
PUMP_SIGNAL_COOLDOWN_S = 5     # 5 giây — đủ cho Binance confirm, không bỏ lỡ pump tiếp

# Danh sách coin đặc biệt cần theo dõi pump (ngoài watchlist thường)
# Thêm coin kiểu BANK, LAB, v.v. mà dev hay bơm vào đây
# ← Để trống, tự add qua web dashboard
PUMP_WATCH_COINS = []

# ── PUMP NHẸ RADAR — theo dõi coin pump nhẹ/vừa (5-30%) ───────
# Hoàn toàn độc lập với PUMP_WATCH_COINS và pump radar cũ
# Add/remove qua web dashboard (section PUMP NHẸ RADAR)
PUMP_NHE_COINS = []

# Tự động vào SHORT khi PUMP NHẸ RADAR phát hiện đỉnh
# Dùng cùng pump_detector.py nhưng ngưỡng thấp hơn (score >= PUMP_NHE_MIN_SCORE)
# Bật/tắt độc lập với PUMP_AUTO_SHORT — có thể bật cả 2 cùng lúc
PUMP_NHE_AUTO_SHORT = False

# Ngưỡng score tối thiểu cho PUMP NHẸ RADAR (thấp hơn pump mạnh)
# 50 = nhẹ hơn pump radar cũ (75), bắt được nhiều setup hơn
PUMP_NHE_MIN_SCORE = 60

# % tăng tối thiểu từ đáy để xem xét SHORT (thấp hơn pump mạnh)
PUMP_NHE_PRICE_RISE_PCT = 10.0

# Interval quét pump riêng (giây) — chạy nhanh hơn main loop
# 30s để bắt đỉnh kịp thời, không phải đợi 60s main loop
PUMP_SCAN_INTERVAL_SECONDS = 30

# Tự động vào SHORT khi phát hiện đỉnh pump
# True  → bot TỰ vào lệnh SHORT (nguy hiểm, dùng khi đã test kỹ)
# False → chỉ gửi Telegram alert, mày quyết định vào tay
PUMP_AUTO_SHORT = False

# AUTO SHORT nhẹ — dùng ngưỡng thấp hơn (score>=60, pump>=15%, RSI>=65)
# Bật khi muốn bot bắt pump ở coin thường (ít pump hơn coin dev)
# Không dùng chung với PUMP_AUTO_SHORT
PUMP_AUTO_SHORT_SOFT = False


# ============================================================
# AI ANALYSIS (TradingAgents) CONFIG
# ============================================================

# Bật/tắt tự động chạy AI phân tích
AI_AUTO_ANALYSIS = True

# Interval: mỗi bao lâu chạy lại (giờ)
AI_ANALYSIS_INTERVAL_HOURS = 4
#Run bot 
#cd /Users/leduykhang/Documents/Trading/trading-bot/trading-bot python3 bot.py

#https://railway.com/project/bfd60fcc-b141-4ac3-91ce-b086b7ef0ea1
#https://web-production-0847b.up.railway.app/
#pkill -9 -f "Python.*bot.py"; sleep 2; cd /Users/leduykhang/Documents/Trading/trading-bot/trading-bot && caffeinate -s nohup python3 bot.py > /tmp/bot.log 2>&1 &

#nohup python3 bot.py > /tmp/bot.log 2>&1 &
#echo "Bot started!"

  #http://159.65.136.39:5555
#ssh root@159.65.136.39
#cd /root/Trader-Bot && git pull && pkill -9 -f bot.py; sleep 2; nohup /root/start_bot.sh > /tmp/bot.log 2>&1 &
#ATR×1.5, RR 1:3) —
# phân tích theo chart telegram
#ssh root@159.65.136.39
#cd /root/Trader-Bot && git pull && pkill -9 -f bot.py; sleep 2; nohup /root/start_bot.sh > /tmp/bot.log 2>&1 &
#BOT7

# ── Web Dashboard Security ──────────────────────────────────
# Mật khẩu đăng nhập web dashboard
# Đổi thành mật khẩu của mày trước khi deploy
WEB_PASSWORD = "Cr7ronaldojk"

# Secret key cho Flask session (random mỗi lần start — session hết hạn khi restart bot)
import secrets as _secrets
WEB_SECRET_KEY = _secrets.token_hex(32)
