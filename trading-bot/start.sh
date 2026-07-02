#!/bin/bash
# Kill bot cũ + free port 5555 trước khi start
pkill -f "Python.*bot.py" 2>/dev/null
lsof -i :5555 -t 2>/dev/null | xargs kill 2>/dev/null
sleep 1
cd /Users/leduykhang/Documents/Trading/trading-bot/trading-bot
python3 bot.py
