import os
import time
import hmac
import hashlib
import base64
import json
import sqlite3
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = "8837984790:AAEv-6X9s1msqK94O0NeBL3pD8WZnLbE8qY"
SECRET_SERVER_KEY = b"SEGLOCK_PRO_ULTIMATE_HMAC_MASTER_KEY_2026_X99"
DB_PATH = "keys.db"
HTTP_PORT = 8080

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==========================================
# SQLITE DATABASE STORAGE
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS keys (
            license_key TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            exp_timestamp INTEGER NOT NULL,
            exp_str TEXT NOT NULL,
            plan_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        )
    ''')
    conn.commit()
    conn.close()

def db_save_key(license_key: str, user_id: int, exp_timestamp: int, exp_str: str, plan_name: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    created_at = datetime.now().strftime('%d.%m.%Y %H:%M')
    cursor.execute('''
        INSERT OR REPLACE INTO keys (license_key, user_id, exp_timestamp, exp_str, plan_name, created_at, is_active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
    ''', (license_key, user_id, exp_timestamp, exp_str, plan_name, created_at))
    conn.commit()
    conn.close()

def db_get_user_keys(user_id: int) -> list:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT license_key, exp_str, plan_name FROM keys WHERE user_id = ? AND is_active = 1', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{"key": r[0], "exp_str": r[1], "plan": r[2]} for r in rows]

def db_count_keys() -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM keys')
    count = cursor.fetchone()[0]
    conn.close()
    return count

# Initialize SQLite database immediately
init_db()

# ==========================================
# CRYPTO KEYGEN & VERIFICATION ENGINE
# ==========================================
def generate_license_key(user_id: int, duration_hours: int, plan_name: str = "custom") -> tuple[str, str]:
    if duration_hours == -1:
        expire_timestamp = int(time.time()) + (3650 * 24 * 3600)  # ~10 years
        exp_str = "Навсегда (Lifetime)"
    else:
        expire_timestamp = int(time.time()) + (duration_hours * 3600)
        exp_str = datetime.fromtimestamp(expire_timestamp).strftime('%d.%m.%Y %H:%M MSK')

    payload = {
        "uid": user_id,
        "exp": expire_timestamp,
        "nonce": os.urandom(4).hex()
    }
    
    payload_json = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode('utf-8').rstrip('=')
    signature = hmac.new(SECRET_SERVER_KEY, payload_b64.encode('utf-8'), hashlib.sha256).hexdigest()[:12].upper()
    
    license_key = f"SEG-{payload_b64}-{signature}"
    
    # Save key persistently into SQLite DB
    db_save_key(license_key, user_id, expire_timestamp, exp_str, plan_name)
    
    return license_key, exp_str

def verify_license_key(license_key: str) -> dict:
    try:
        parts = license_key.strip().split('-')
        if len(parts) != 3 or parts[0] != "SEG":
            return {"valid": False, "reason": "❌ Неверный формат ключа"}
        
        payload_b64 = parts[1]
        signature = parts[2].upper()
        
        padded_b64 = payload_b64 + '=' * (-len(payload_b64) % 4)
        expected_sig = hmac.new(SECRET_SERVER_KEY, payload_b64.encode('utf-8'), hashlib.sha256).hexdigest()[:12].upper()
        
        if signature != expected_sig:
            return {"valid": False, "reason": "❌ Подпись ключа недействительна (Ключ подделан!)"}
            
        payload = json.loads(base64.urlsafe_b64decode(padded_b64).decode('utf-8'))
        
        if time.time() > payload["exp"]:
            return {"valid": False, "reason": "⏳ Срок действия ключа истёк"}
            
        exp_formatted = datetime.fromtimestamp(payload["exp"]).strftime('%d.%m.%Y %H:%M MSK')
        return {
            "valid": True,
            "uid": payload["uid"],
            "expires_at": exp_formatted,
            "nonce": payload["nonce"]
        }
    except Exception as e:
        return {"valid": False, "reason": f"❌ Ошибка проверки: {str(e)}"}

# ==========================================
# HTTP API SERVER FOR ANDROID APP SYNC
# ==========================================
class AppSyncHTTPHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/api/verify_key":
            query_params = parse_qs(parsed_path.query)
            key = query_params.get("key", [None])[0]
            
            if not key:
                response = {"valid": False, "reason": "Missing key parameter"}
            else:
                response = verify_license_key(key)
                
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "Endpoint not found"}')

    def log_message(self, format, *args):
        return  # Silence standard HTTP logs

def start_http_api_server():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), AppSyncHTTPHandler)
    logging.info(f"[+] HTTP API Sync Server running on port {HTTP_PORT}...")
    server.serve_forever()

# Start HTTP Server in background thread
http_thread = threading.Thread(target=start_http_api_server, daemon=True)
http_thread.start()

# ==========================================
# TELEGRAM BOT HANDLERS
# ==========================================
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("⚡ Купить подписку", callback_data="buy_menu"), InlineKeyboardButton("🔑 Мои ключи", callback_data="my_keys")],
        [InlineKeyboardButton("🔍 Проверить ключ", callback_data="verify_prompt"), InlineKeyboardButton("📊 Статус системы", callback_data="system_status")],
        [InlineKeyboardButton("📖 Инструкция", callback_data="instructions"), InlineKeyboardButton("💬 Поддержка", callback_data="support")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    welcome_text = (
        f"👑 **Приветствуем в официальном SegLock Pro KeyBot!** 👑\n\n"
        f"Привет, **{user.first_name}**!\n"
        f"Здесь ты можешь мгновенно получить подлинные лицензионные ключи доступа "
        f"для приложения **SegLock Scooter Unlocker**.\n\n"
        f"🔥 **Преимущества SegLock Pro:**\n"
        f"• Автоматический сканер BLE в реальном времени\n"
        f"• Поддержка всех актуальных прошивок и протоколов 2026 года\n"
        f"• Вычисление time_slot и master_salt на лету\n"
        f"• 100% зашифрованный нативный код без задержек\n\n"
        f"Выбери нужный раздел в меню ниже:"
    )
    await update.message.reply_text(welcome_text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    
    if data == "main_menu":
        await query.edit_message_text(
            "⚡ **Главное меню SegLock Pro**\n\nВыбери нужный раздел ниже:",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )

    elif data == "buy_menu":
        keyboard = [
            [InlineKeyboardButton("🕒 1 Час — 150 ₽", callback_data="buy_1h"), InlineKeyboardButton("⏳ 24 Часа — 490 ₽", callback_data="buy_24h")],
            [InlineKeyboardButton("📅 3 Дня — 990 ₽", callback_data="buy_3d"), InlineKeyboardButton("🗓 7 Дней — 1890 ₽", callback_data="buy_7d")],
            [InlineKeyboardButton("🚀 30 Дней — 3490 ₽", callback_data="buy_30d"), InlineKeyboardButton("👑 Навсегда — 7990 ₽", callback_data="buy_life")],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="main_menu")]
        ]
        await query.edit_message_text(
            "💳 **Выбор тарифа подписки SegLock Pro**\n\n"
            "После оплаты ключ генерируется автоматически и сохраняется в базе данных.\n\n"
            "Выбери подходящий период:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data.startswith("buy_"):
        plan_code = data.replace("buy_", "")
        plans = {
            "1h": (1, "1 Час"),
            "24h": (24, "24 Часа"),
            "3d": (72, "3 Дня"),
            "7d": (168, "7 Дней"),
            "30d": (720, "30 Дней"),
            "life": (-1, "Навсегда (Lifetime)")
        }
        
        if plan_code in plans:
            hours, plan_name = plans[plan_code]
            key, exp_str = generate_license_key(user_id, hours, plan_name)
            
            keyboard = [[InlineKeyboardButton("⬅️ В главное меню", callback_data="main_menu")]]
            
            msg = (
                f"🎉 **ПОДПИСКА УСПЕШНО АКТИВИРОВАНА!** 🎉\n\n"
                f"🔑 **Твой персональный ключ доступа:**\n"
                f"`{key}`\n\n"
                f"📌 **Тариф:** {plan_name}\n"
                f"⏱ **Действителен до:** {exp_str}\n\n"
                f"💡 *Скопируй ключ (нажатием на него) и вставь на экране входа в приложении SegLock Pro.*"
            )
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "my_keys":
        user_keys = db_get_user_keys(user_id)
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]]
        
        if not user_keys:
            await query.edit_message_text(
                "ℹ️ **У вас пока нет активных ключей.**\n\nПриобрести ключ можно в разделе «⚡ Купить подписку».",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            msg = "🔑 **Ваши сохраненные ключи (из БД):**\n\n"
            for item in user_keys:
                msg += f"• Тариф: **{item['plan']}**\n  Ключ: `{item['key']}`\n  Действует до: `{item['exp_str']}`\n\n"
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "verify_prompt":
        context.user_data["awaiting_key"] = True
        keyboard = [[InlineKeyboardButton("⬅️ Отмена", callback_data="main_menu")]]
        await query.edit_message_text(
            "🔍 **Проверка лицензионного ключа**\n\nОтправьте мне ключ формата `SEG-...` отдельным сообщением:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data == "system_status":
        total_keys = db_count_keys()
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]]
        status_msg = (
            f"📊 **Статус серверов SegLock Pro Network**\n\n"
            f"🟢 **Основной нод (Crypto Core):** Работает\n"
            f"🟢 **База данных SQLite (keys.db):** Подключена\n"
            f"🟢 **HTTP API синхронизации приложения:** Online (Port {HTTP_PORT})\n\n"
            f"📈 Всего выписано и сохранено ключей: **{total_keys}**\n"
            f"🔒 Шифрование: **HMAC-SHA256 / 2026 Standard**"
        )
        await query.edit_message_text(status_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "instructions":
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]]
        inst_msg = (
            "📖 **Инструкция по запуску и настройке SegLock Pro:**\n\n"
            "1️⃣ Скачайте и установите собранный APK-файл SegLock Pro.\n"
            "2️⃣ При старте приложения разрешите доступ к **Bluetooth** и **Геолокации**.\n"
            "3️⃣ В появившемся окне активации вставьте полученный ключ `SEG-...`.\n"
            "4️⃣ Нажмите **VERIFY KEY** — статус сменится на «Подключено».\n"
            "5️⃣ Перейдите на вкладку **AutoScan** для поиска девайсов или введите данные в **Manual Key**."
        )
        await query.edit_message_text(inst_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "support":
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]]
        supp_msg = (
            "💬 **Служба поддержки SegLock Pro**\n\n"
            "По всем вопросам работы софта, оплаты и техническим проблемам обращайтесь в поддержку:\n\n"
            "👨‍💻 **Администратор:** @SegLockSupport\n"
            "⏱ Время работы: **24/7**"
        )
        await query.edit_message_text(supp_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_key"):
        context.user_data["awaiting_key"] = False
        user_key = update.message.text.strip()
        result = verify_license_key(user_key)
        
        keyboard = [[InlineKeyboardButton("⬅️ В главное меню", callback_data="main_menu")]]
        if result["valid"]:
            msg = (
                f"✅ **КЛЮЧ ПОДЛИНЕН И АКТИВЕН!** ✅\n\n"
                f"👤 UID Владельца: `{result['uid']}`\n"
                f"⏱ Истекает: `{result['expires_at']}`\n"
                f"🔐 Nonce: `{result['nonce']}`\n\n"
                f"Этот ключ полностью готов к использованию в приложении."
            )
        else:
            msg = f"❌ **ОШИБКА ПРОВЕРКИ КЛЮЧА**\n\nПричина: **{result['reason']}**"
            
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

def main():
    print(f"[+] Launching SegLock Pro KeyBot with SQLite DB and HTTP API Sync Server...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    print("[+] Bot & HTTP Sync API online!")
    app.run_polling()

if __name__ == "__main__":
    main()
