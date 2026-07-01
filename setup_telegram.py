#!/usr/bin/env python3
# ============================================================
# TELEGRAM SETUP WIZARD — chạy 1 lần để lấy token + chat_id
# ============================================================
import requests
import json
import time
import os

print("""
╔══════════════════════════════════════════╗
║     TELEGRAM SETUP WIZARD 🤖             ║
║     Tự động lấy token + chat_id          ║
╚══════════════════════════════════════════╝
""")

# ---- BƯỚC 1: Lấy Bot Token ----
print("📌 BƯỚC 1: Tạo bot trên Telegram")
print("   1. Mở Telegram trên điện thoại")
print("   2. Tìm kiếm: @BotFather")
print("   3. Gõ: /newbot")
print("   4. Đặt tên bất kỳ (vd: SUI Bot)")
print("   5. Copy token nó gửi cho mày\n")

bot_token = input("👉 Paste token vào đây: ").strip()

# Validate token
try:
    resp = requests.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=10)
    data = resp.json()
    if not data.get("ok"):
        print(f"❌ Token không hợp lệ: {data.get('description')}")
        exit(1)
    bot_name = data["result"]["username"]
    print(f"✅ Bot hợp lệ: @{bot_name}\n")
except Exception as e:
    print(f"❌ Lỗi kết nối: {e}")
    exit(1)

# ---- BƯỚC 2: Lấy Chat ID ----
print("📌 BƯỚC 2: Lấy Chat ID của mày")
print(f"   1. Mở Telegram")
print(f"   2. Tìm bot: @{bot_name}")
print(f"   3. Gõ bất kỳ thứ gì (vd: 'hello')")
print(f"   Đang chờ mày nhắn tin...\n")

chat_id = None
for i in range(30):  # Chờ tối đa 30 giây
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getUpdates",
            timeout=10
        )
        updates = resp.json()
        if updates.get("ok") and updates.get("result"):
            last = updates["result"][-1]
            chat_id = str(last["message"]["chat"]["id"])
            username = last["message"]["chat"].get("username", "unknown")
            first_name = last["message"]["chat"].get("first_name", "")
            print(f"✅ Tìm thấy! Chat ID: {chat_id} ({first_name} @{username})")
            break
    except Exception:
        pass
    print(f"   Chờ... ({i+1}/30s)", end="\r")
    time.sleep(1)

if not chat_id:
    print("\n❌ Timeout — mày chưa nhắn tin cho bot. Chạy lại script và nhắn tin trước.")
    exit(1)

# ---- BƯỚC 3: Test gửi tin ----
print("\n📌 BƯỚC 3: Test gửi thông báo...")
test_msg = f"""
🤖 <b>Trading Bot Setup OK!</b>

✅ Bot: @{bot_name}
✅ Chat ID: {chat_id}
✅ Kết nối thành công!

Bot sẽ gửi thông báo về đây khi có tín hiệu trade.
""".strip()

try:
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": test_msg, "parse_mode": "HTML"},
        timeout=10
    )
    if resp.json().get("ok"):
        print("✅ Tin nhắn test đã gửi thành công! Kiểm tra Telegram của mày.\n")
    else:
        print(f"❌ Gửi thất bại: {resp.json()}")
        exit(1)
except Exception as e:
    print(f"❌ Lỗi: {e}")
    exit(1)

# ---- BƯỚC 4: Tự động cập nhật notifier.py ----
print("📌 BƯỚC 4: Tự động cập nhật config...")

notifier_path = os.path.join(os.path.dirname(__file__), "notifier.py")
with open(notifier_path, "r") as f:
    content = f.read()

content = content.replace(
    '"bot_token": "YOUR_TELEGRAM_BOT_TOKEN"',
    f'"bot_token": "{bot_token}"'
)
content = content.replace(
    '"chat_id": "YOUR_CHAT_ID"',
    f'"chat_id": "{chat_id}"'
)

with open(notifier_path, "w") as f:
    f.write(content)

print("✅ notifier.py đã được cập nhật tự động!\n")

# ---- DONE ----
print("""
╔══════════════════════════════════════════╗
║  ✅ SETUP HOÀN TẤT!                      ║
╠══════════════════════════════════════════╣""")
print(f"║  Bot Token : {bot_token[:20]}...  ")
print(f"║  Chat ID   : {chat_id:<30}║")
print("""╠══════════════════════════════════════════╣
║  Chạy bot:  python bot.py                ║
╚══════════════════════════════════════════╝
""")
