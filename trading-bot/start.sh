#!/bin/bash
# Kill TẤT CẢ bot cũ trước khi start — đảm bảo chỉ 1 instance
pkill -9 -f "Python.*bot.py" 2>/dev/null
pkill -9 -f "python3.*bot.py" 2>/dev/null
sleep 2

# Verify không còn bot nào
COUNT=$(ps aux | grep "bot.py" | grep -v grep | wc -l)
if [ "$COUNT" -gt "0" ]; then
    echo "⚠️ Vẫn còn bot chạy, force kill..."
    ps aux | grep "bot.py" | grep -v grep | awk '{print $2}' | xargs kill -9 2>/dev/null
    sleep 2
fi

# Free port 5555
lsof -i :5555 -t 2>/dev/null | xargs kill -9 2>/dev/null
sleep 1

cd /Users/leduykhang/Documents/Trading/trading-bot/trading-bot
python3 bot.py
