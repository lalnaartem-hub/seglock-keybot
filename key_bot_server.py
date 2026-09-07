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
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters
)

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = "8837984790:AAEv-6X9s1msqK94O0NeBL3pD8WZnLbE8qY"
SECRET_SERVER_KEY = b"SEGLOCK_PRO_ULTIMATE_HMAC_MASTER_KEY_2026_X99"
ADMIN_IDS = [7772296423]  # Admin Telegram User ID
DB_PATH = "keys.db"
HTTP_PORT = 8080

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# Default Telegram Stars Prices (in Stars - XTR)
DEFAULT_STARS_PRICES = {
    "24h": {"hours": 24, "name": "1 День (24 Часа)", "stars": 50},
    "7d": {"hours": 168, "name": "7 Дней", "stars": 222},
    "30d": {"hours": 720, "name": "30 Дней", "stars": 400},
    "life": {"hours": -1, "name": "Навсегда (Lifetime)", "stars": 1000}
}

# ==========================================
# SQLITE DATABASE STORAGE & REVENUE TABLE
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Keys table
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
    
    # Revenue / Payments table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_code TEXT NOT NULL,
            stars_amount INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')
    
    # Dynamic prices table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS price_config (
            plan_code TEXT PRIMARY KEY,
            hours INTEGER NOT NULL,
            name TEXT NOT NULL,
            stars INTEGER NOT NULL
        )
    ''')
    
    # Populate default prices if empty
    cursor.execute('SELECT COUNT(*) FROM price_config')
    if cursor.fetchone()[0] == 0:
        for code, info in DEFAULT_STARS_PRICES.items():
            cursor.execute('INSERT INTO price_config VALUES (?, ?, ?, ?)', (code, info["hours"], info["name"], info["stars"]))
            
    conn.commit()
    conn.close()

def db_get_prices() -> dict:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT plan_code, hours, name, stars FROM price_config')
    rows = cursor.fetchall()
    conn.close()
    
    prices = {}
    for r in rows:
        prices[r[0]] = {"hours": r[1], "name": r[2], "stars": r[3]}
    return prices

def db_update_price(plan_code: str, new_stars: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE price_config SET stars = ? WHERE plan_code = ?', (new_stars, plan_code))
    affected = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return affected

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

def db_save_payment(user_id: int, plan_code: str, stars_amount: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    created_at = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
    cursor.execute('''
        INSERT INTO payments (user_id, plan_code, stars_amount, created_at)
        VALUES (?, ?, ?, ?)
    ''', (user_id, plan_code, stars_amount, created_at))
    conn.commit()
    conn.close()

def db_get_revenue_report() -> dict:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('SELECT SUM(stars_amount), COUNT(*) FROM payments')
    row = cursor.fetchone()
    total_stars = row[0] if row[0] else 0
    total_tx = row[1] if row[1] else 0
    
    cursor.execute('SELECT user_id, plan_code, stars_amount, created_at FROM payments ORDER BY id DESC LIMIT 20')
    recent_payments = cursor.fetchall()
    
    conn.close()
    return {
        "total_stars": total_stars,
        "total_tx": total_tx,
        "recent": recent_payments
    }

def db_get_user_keys(user_id: int) -> list:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT license_key, exp_str, plan_name FROM keys WHERE user_id = ? AND is_active = 1', (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{"key": r[0], "exp_str": r[1], "plan": r[2]} for r in rows]

def db_get_all_keys() -> list:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT license_key, user_id, exp_str, plan_name, created_at, is_active FROM keys ORDER BY exp_timestamp DESC LIMIT 50')
    rows = cursor.fetchall()
    conn.close()
    return [{"key": r[0], "uid": r[1], "exp_str": r[2], "plan": r[3], "created_at": r[4], "active": r[5]} for r in rows]

def db_count_keys() -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM keys')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def db_revoke_key(license_key: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE keys SET is_active = 0 WHERE license_key = ?', (license_key.strip(),))
    affected = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return affected

init_db()

# ==========================================
# CRYPTO KEYGEN & VERIFICATION ENGINE
# ==========================================
def generate_license_key(user_id: int, duration_hours: int, plan_name: str = "custom") -> tuple[str, str]:
    if duration_hours == -1:
        expire_timestamp = int(time.time()) + (3650 * 24 * 3600)
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
            
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT is_active FROM keys WHERE license_key = ?', (license_key.strip(),))
        row = cursor.fetchone()
        conn.close()
        if row and row[0] == 0:
            return {"valid": False, "reason": "🚫 Ключ заблокирован администратором"}

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
        return

def start_http_api_server():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), AppSyncHTTPHandler)
    logging.info(f"[+] HTTP API Sync Server running on port {HTTP_PORT}...")
    server.serve_forever()

http_thread = threading.Thread(target=start_http_api_server, daemon=True)
http_thread.start()

# ==========================================
# TELEGRAM BOT HANDLERS & STARS PAYMENT
# ==========================================
def main_menu_keyboard(user_id: int):
    keyboard = [
        [InlineKeyboardButton("⭐ Купить за Telegram Stars", callback_data="buy_menu"), InlineKeyboardButton("🔑 Мои ключи", callback_data="my_keys")],
        [InlineKeyboardButton("🔍 Проверить ключ", callback_data="verify_prompt"), InlineKeyboardButton("📊 Статус системы", callback_data="system_status")],
        [InlineKeyboardButton("📖 Инструкция", callback_data="instructions"), InlineKeyboardButton("💬 Поддержка", callback_data="support")],
        [InlineKeyboardButton("📢 Канал @SegLock", url="https://t.me/SegLock"), InlineKeyboardButton("👨‍💻 Саппорт @SegLockSupport", url="https://t.me/SegLockSupport")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("👑 Админ Панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    welcome_text = (
        f"👑 **Приветствуем в официальном SegLock Pro KeyBot!** 👑\n\n"
        f"Привет, **{user.first_name}**!\n"
        f"Здесь ты можешь мгновенно приобрести подлинные лицензионные ключи доступа "
        f"для приложения **SegLock Scooter Unlocker** за **Telegram Stars (⭐️)**.\n\n"
        f"🔥 **Преимущества SegLock Pro:**\n"
        f"• Автоматический сканер BLE в реальном времени\n"
        f"• Поддержка протоколов 2026 года (time_slot + master_salt)\n"
        f"• 100% нативный защищенный C++ слой\n"
        f"• Автоматический мгновенный вышив ключа при оплате Звёздами\n\n"
        f"Выбери нужный раздел в меню ниже:"
    )
    await update.message.reply_text(welcome_text, reply_markup=main_menu_keyboard(user.id), parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    
    prices = db_get_prices()
    
    if data == "main_menu":
        await query.edit_message_text(
            "⚡ **Главное меню SegLock Pro**\n\nВыбери нужный раздел ниже:",
            reply_markup=main_menu_keyboard(user_id),
            parse_mode="Markdown"
        )

    elif data == "buy_menu":
        keyboard = [
            [InlineKeyboardButton(f"⏳ 1 День — {prices['24h']['stars']} ⭐️", callback_data="stars_pay_24h"), InlineKeyboardButton(f"🗓 7 Дней — {prices['7d']['stars']} ⭐️", callback_data="stars_pay_7d")],
            [InlineKeyboardButton(f"🚀 30 Дней — {prices['30d']['stars']} ⭐️", callback_data="stars_pay_30d"), InlineKeyboardButton(f"👑 Навсегда — {prices['life']['stars']} ⭐️", callback_data="stars_pay_life")],
            [InlineKeyboardButton("⬅️ Назад в меню", callback_data="main_menu")]
        ]
        await query.edit_message_text(
            "⭐ **Оплата подписки через Telegram Stars (⭐️)**\n\n"
            "Выберите подходящий период подписки. После подтверждения инвойса ключ активируется и выдается мгновенно:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data.startswith("stars_pay_"):
        plan_code = data.replace("stars_pay_", "")
        if plan_code in prices:
            plan_info = prices[plan_code]
            
            await context.bot.send_invoice(
                chat_id=user_id,
                title=f"SegLock Pro — {plan_info['name']}",
                description=f"Лицензионный ключ доступа к SegLock Pro на {plan_info['name']}.",
                payload=f"seglock_pay_{plan_code}_{user_id}",
                provider_token="",  # STRICTLY EMPTY FOR TELEGRAM STARS
                currency="XTR",     # TELEGRAM STARS CURRENCY CODE
                prices=[LabeledPrice(label=f"Подписка ({plan_info['name']})", amount=plan_info["stars"])]
            )

    elif data == "my_keys":
        user_keys = db_get_user_keys(user_id)
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]]
        
        if not user_keys:
            await query.edit_message_text(
                "ℹ️ **У вас пока нет активных ключей.**\n\nПриобрести ключ за Звёзды можно в разделе «⭐ Купить за Telegram Stars».",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            msg = "🔑 **Ваши активные ключи:**\n\n"
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
        revenue = db_get_revenue_report()
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]]
        status_msg = (
            f"📊 **Статус серверов SegLock Pro Network**\n\n"
            f"🟢 **Основной нод (Crypto Core):** Работает\n"
            f"🟢 **База данных SQLite (keys.db):** Подключена\n"
            f"🟢 **Оплата Telegram Stars (XTR):** Активна\n"
            f"🟢 **HTTP API синхронизации:** Online (Port {HTTP_PORT})\n\n"
            f"📈 Всего ключей: **{total_keys}** | Заработано: **{revenue['total_stars']} ⭐️**\n"
            f"👑 Главный админ: `7772296423`"
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
        keyboard = [
            [InlineKeyboardButton("📢 Официальный Канал @SegLock", url="https://t.me/SegLock")],
            [InlineKeyboardButton("💬 Написать в Поддержку @SegLockSupport", url="https://t.me/SegLockSupport")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]
        ]
        supp_msg = (
            "💬 **Служба поддержки & Канал SegLock Pro**\n\n"
            "📢 **Официальный канал:** @SegLock\n"
            "👨‍💻 **Техническая поддержка:** @SegLockSupport\n"
            "👑 **Главный администратор:** [Artem](tg://user?id=7772296423)\n\n"
            "⏱ Время работы поддержки: **24/7**"
        )
        await query.edit_message_text(supp_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    # ADMIN PANEL HANDLERS
    elif data == "admin_panel" and user_id in ADMIN_IDS:
        keyboard = [
            [InlineKeyboardButton("📈 Таблица Доходов", callback_data="admin_revenue_stats"), InlineKeyboardButton("✏️ Изменить Цены", callback_data="admin_change_prices")],
            [InlineKeyboardButton("👑 Создать бесплатный ключ", callback_data="admin_gen_key"), InlineKeyboardButton("📋 Ключи в БД", callback_data="admin_list_keys")],
            [InlineKeyboardButton("🚫 Отозвать ключ", callback_data="admin_revoke_prompt")],
            [InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")]
        ]
        await query.edit_message_text(
            f"👑 **ПАНЕЛЬ АДМИНИСТРАТОРА (ID: {user_id})**\n\n"
            f"Всего ключей в БД: **{db_count_keys()}**\n"
            f"Всего заработано Звёзд: **{db_get_revenue_report()['total_stars']} ⭐️**\n\n"
            f"Выберите действие:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data == "admin_revenue_stats" and user_id in ADMIN_IDS:
        report = db_get_revenue_report()
        keyboard = [[InlineKeyboardButton("⬅️ В админку", callback_data="admin_panel")]]
        
        msg = (
            f"💰 **ТАБЛИЦА ДОХОДОВ И ТРАНЗАКЦИЙ** 💰\n\n"
            f"⭐️ **Общий доход:** `{report['total_stars']} ⭐️`\n"
            f"🛒 **Всего покупок:** `{report['total_tx']}`\n\n"
            f"📋 **Последние транзакции:**\n"
        )
        if not report["recent"]:
            msg += "_Транзакций пока не было._"
        else:
            for tx in report["recent"]:
                msg += f"• UID: `{tx[0]}` | Тариф: **{tx[1]}** | **+{tx[2]} ⭐️** | `{tx[3]}`\n"
                
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "admin_change_prices" and user_id in ADMIN_IDS:
        current_prices = db_get_prices()
        keyboard = [
            [InlineKeyboardButton(f"1 День ({current_prices['24h']['stars']} ⭐️)", callback_data="adm_editprice_24h")],
            [InlineKeyboardButton(f"7 Дней ({current_prices['7d']['stars']} ⭐️)", callback_data="adm_editprice_7d")],
            [InlineKeyboardButton(f"30 Дней ({current_prices['30d']['stars']} ⭐️)", callback_data="adm_editprice_30d")],
            [InlineKeyboardButton(f"Lifetime ({current_prices['life']['stars']} ⭐️)", callback_data="adm_editprice_life")],
            [InlineKeyboardButton("⬅️ В админку", callback_data="admin_panel")]
        ]
        await query.edit_message_text("✏️ **Выберите тариф для изменения цены в Звёздах (⭐️):**", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("adm_editprice_") and user_id in ADMIN_IDS:
        plan_code = data.replace("adm_editprice_", "")
        context.user_data["editing_price_plan"] = plan_code
        keyboard = [[InlineKeyboardButton("⬅️ Отмена", callback_data="admin_change_prices")]]
        await query.edit_message_text(
            f"✏️ **Изменение цены для тарифа `{plan_code}`**\n\n"
            f"Отправьте новую стоимость в Звёздах (числом):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    elif data == "admin_gen_key" and user_id in ADMIN_IDS:
        keyboard = [
            [InlineKeyboardButton("1 День", callback_data="adm_gen_24h"), InlineKeyboardButton("7 Дней", callback_data="adm_gen_7d")],
            [InlineKeyboardButton("30 Дней", callback_data="adm_gen_30d"), InlineKeyboardButton("👑 Lifetime", callback_data="adm_gen_life")],
            [InlineKeyboardButton("⬅️ Назад в админку", callback_data="admin_panel")]
        ]
        await query.edit_message_text("👑 **Выбери срок действия админ-ключа:**", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("adm_gen_") and user_id in ADMIN_IDS:
        plan_code = data.replace("adm_gen_", "")
        plans = db_get_prices()
        if plan_code in plans:
            info = plans[plan_code]
            key, exp_str = generate_license_key(user_id, info["hours"], f"Admin ({info['name']})")
            keyboard = [[InlineKeyboardButton("⬅️ В админку", callback_data="admin_panel")]]
            await query.edit_message_text(
                f"✅ **АДМИН-КЛЮЧ СГЕНЕРИРОВАН:**\n\n"
                f"`{key}`\n\n"
                f"📌 Тариф: **{info['name']}**\n"
                f"⏱ Срок: **{exp_str}**",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

    elif data == "admin_list_keys" and user_id in ADMIN_IDS:
        keys_list = db_get_all_keys()
        keyboard = [[InlineKeyboardButton("⬅️ В админку", callback_data="admin_panel")]]
        if not keys_list:
            await query.edit_message_text("В базе данных пока нет ключей.", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            msg = "📋 **Последние ключи в БД:**\n\n"
            for k in keys_list[:15]:
                status = "🟢" if k["active"] == 1 else "🔴"
                msg += f"{status} `{k['key']}` | UID: `{k['uid']}` | До: {k['exp_str']}\n"
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "admin_revoke_prompt" and user_id in ADMIN_IDS:
        context.user_data["awaiting_revoke_key"] = True
        keyboard = [[InlineKeyboardButton("⬅️ Отмена", callback_data="admin_panel")]]
        await query.edit_message_text(
            "🚫 **Отзыв / Блокировка ключа**\n\nОтправьте мне ключ `SEG-...`, который нужно заблокировать:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

# ==========================================
# TELEGRAM STARS PAYMENT HANDLERS
# ==========================================
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    user_id = update.effective_user.id
    stars_amount = payment.total_amount
    
    parts = payload.split('_')
    plan_code = parts[2] if len(parts) >= 3 else "24h"
    
    prices = db_get_prices()
    plan_info = prices.get(plan_code, {"hours": 24, "name": "1 День (24 Часа)"})
    
    key, exp_str = generate_license_key(user_id, plan_info["hours"], f"Telegram Stars ({plan_info['name']})")
    db_save_payment(user_id, plan_code, stars_amount)
    
    success_msg = (
        f"⭐️ **ОПЛАТА ЗВЁЗДАМИ СОВЕРШЕНА УСПЕШНО!** ⭐️\n\n"
        f"Оплачено: **{stars_amount} ⭐️ Telegram Stars**\n\n"
        f"🔑 **Твой персональный ключ доступа:**\n"
        f"`{key}`\n\n"
        f"📌 **Тариф:** {plan_info['name']}\n"
        f"⏱ **Действителен до:** {exp_str}\n\n"
        f"💡 *Скопируй ключ и вставь его при входе в приложении SegLock Pro.*"
    )
    await update.message.reply_text(success_msg, parse_mode="Markdown")

# ==========================================
# TEXT MESSAGES & ADMIN EDIT HANDLERS
# ==========================================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    if context.user_data.get("editing_price_plan") and user_id in ADMIN_IDS:
        plan_code = context.user_data["editing_price_plan"]
        context.user_data["editing_price_plan"] = None
        try:
            new_stars = int(text)
            if new_stars <= 0:
                raise ValueError()
            db_update_price(plan_code, new_stars)
            keyboard = [[InlineKeyboardButton("⬅️ В админку", callback_data="admin_panel")]]
            await update.message.reply_text(
                f"✅ **Цена для тарифа `{plan_code}` успешно изменена на {new_stars} ⭐️!**",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ Ошибка: Введите корректное число Звёзд.")
        return

    if context.user_data.get("awaiting_revoke_key") and user_id in ADMIN_IDS:
        context.user_data["awaiting_revoke_key"] = False
        revoked = db_revoke_key(text)
        keyboard = [[InlineKeyboardButton("⬅️ В админку", callback_data="admin_panel")]]
        if revoked:
            await update.message.reply_text(f"✅ Ключ `{text}` успешно заблокирован в БД!", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await update.message.reply_text(f"❌ Ключ `{text}` не найден в базе.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if context.user_data.get("awaiting_key"):
        context.user_data["awaiting_key"] = False
        result = verify_license_key(text)
        
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

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    keyboard = [
        [InlineKeyboardButton("📈 Таблица Доходов", callback_data="admin_revenue_stats"), InlineKeyboardButton("✏️ Изменить Цены", callback_data="admin_change_prices")],
        [InlineKeyboardButton("👑 Создать бесплатный ключ", callback_data="admin_gen_key"), InlineKeyboardButton("📋 Ключи в БД", callback_data="admin_list_keys")],
        [InlineKeyboardButton("🚫 Отозвать ключ", callback_data="admin_revoke_prompt")],
        [InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu")]
    ]
    await update.message.reply_text(
        f"👑 **ПАНЕЛЬ АДМИНИСТРАТОРА (ID: {user_id})**\n\n"
        f"Всего ключей в БД: **{db_count_keys()}**\n"
        f"Всего заработано Звёзд: **{db_get_revenue_report()['total_stars']} ⭐️**\n\n"
        f"Выберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

def main():
    print(f"[+] Launching SegLock Pro KeyBot v3.0 (Dynamic Prices & Revenue Analytics)...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    print(f"[+] Admin ID: {ADMIN_IDS} | Prices & Revenue Analytics Loaded | HTTP Port: {HTTP_PORT}")
    app.run_polling()

if __name__ == "__main__":
    main()
