from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import logging
import sqlite3
import os
import re
import asyncio
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.functions.messages import SendReactionRequest, RequestWebViewRequest
from telethon.tl.types import ReactionEmoji

# ==================================================
# 🔥 ДАННЫЕ БЕРУТСЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ 🔥
# ==================================================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
# ==================================================

# ===== СОЗДАЁМ ПАПКИ =====
os.makedirs("data", exist_ok=True)
os.makedirs("data/sessions", exist_ok=True)

# ===== БАЗА ДАННЫХ =====
conn = sqlite3.connect("data/accounts.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE,
        session_path TEXT,
        status TEXT DEFAULT 'active',
        name TEXT DEFAULT 'Неизвестно',
        status_info TEXT DEFAULT 'Активен',
        created_at TEXT
    )
''')
cursor.execute('''
    CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER,
        action_type TEXT,
        target TEXT,
        status TEXT,
        created_at TEXT
    )
''')
conn.commit()

def add_account(phone, session_path, name="Неизвестно", status_info="Активен"):
    try:
        cursor.execute('INSERT INTO accounts (phone, session_path, name, status_info, created_at) VALUES (?, ?, ?, ?, ?)',
                      (phone, session_path, name, status_info, datetime.now().isoformat()))
        conn.commit()
        return True
    except:
        return False

def get_accounts():
    cursor.execute('SELECT * FROM accounts WHERE status = "active"')
    return cursor.fetchall()

def get_stats():
    total = cursor.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
    return total

def add_action(account_id, action_type, target, status):
    cursor.execute('INSERT INTO actions (account_id, action_type, target, status, created_at) VALUES (?, ?, ?, ?, ?)',
                  (account_id, action_type, target, status, datetime.now().isoformat()))
    conn.commit()

# ===== СОСТОЯНИЯ =====
user_states = {}
logging.basicConfig(level=logging.INFO)

# ===== ПРОВЕРКА СТАТУСА =====
async def check_account_status(session_path):
    try:
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return {'status': '❌ Забанен', 'name': 'Неизвестно'}
        me = await client.get_me()
        await client.disconnect()
        return {'status': '✅ Активен', 'name': me.first_name or me.username or 'Без имени'}
    except Exception as e:
        return {'status': f'❌ Ошибка', 'name': 'Неизвестно'}

# ===== РЕАКЦИЯ =====
async def set_reaction(session_path, link, emoji="🔥"):
    try:
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return {'success': False, 'error': 'Сессия не активна'}
        match = re.search(r't\.me/([^/]+)/(\d+)', link)
        if not match:
            return {'success': False, 'error': 'Неверный формат ссылки'}
        channel_username = match.group(1)
        post_id = int(match.group(2))
        entity = await client.get_entity(f"@{channel_username}")
        message = await client.get_messages(entity, ids=post_id)
        if not message:
            return {'success': False, 'error': f'Пост {post_id} не найден'}
        try:
            await client(SendReactionRequest(
                peer=entity,
                msg_id=post_id,
                reaction=[ReactionEmoji(emoticon=emoji)]
            ))
            await client.disconnect()
            return {'success': True, 'emoji': emoji, 'channel': channel_username, 'post': post_id}
        except Exception as e:
            await client.disconnect()
            return {'success': False, 'error': f'Ошибка реакции: {e}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

# ===== АБУЗ TAPP =====
async def open_tapp(session_path, link):
    try:
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return {'success': False, 'error': 'Сессия не активна'}
        match = re.search(r't\.me/([^/]+)/app\?startapp=([^&]+)', link)
        if not match:
            return {'success': False, 'error': 'Неверный формат ссылки'}
        bot_username = match.group(1)
        start_param = match.group(2)
        bot = await client.get_entity(f"@{bot_username}")
        await client.send_message(bot, f"/start {start_param}")
        await asyncio.sleep(2)
        try:
            webview = await client.invoke(RequestWebViewRequest(
                peer=bot,
                bot=bot,
                platform='android',
                url='https://t.me/...',
                from_background=False
            ))
            await client.disconnect()
            return {'success': True, 'url': webview.url, 'bot': bot_username}
        except Exception as e:
            await client.disconnect()
            return {'success': False, 'error': str(e)}
    except Exception as e:
        return {'success': False, 'error': str(e)}

# ===== КОМАНДА /start =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Доступ запрещен!")
        return
    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🔄 Обновить статусы", callback_data="refresh")],
        [InlineKeyboardButton("📂 Загрузить сессию", callback_data="upload_session")],
        [InlineKeyboardButton("🔥 Реакции", callback_data="reaction")],
        [InlineKeyboardButton("🚀 Абуз TApp", callback_data="abuse")],
    ]
    await update.message.reply_text("👋 Выбери:", reply_markup=InlineKeyboardMarkup(keyboard))

# ===== ОБРАБОТКА ФАЙЛОВ =====
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    state = user_states.get(user_id, {})
    if state.get('step') != 'waiting_session_file':
        await update.message.reply_text("❌ Сначала нажми кнопку 'Загрузить сессию' в меню!")
        return
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ Отправь файл, а не текст!")
        return
    if not document.file_name.endswith('.session'):
        await update.message.reply_text("❌ Файл должен иметь расширение `.session`")
        return
    await update.message.reply_text("⏳ Загружаю файл на сервер...")
    try:
        file = await document.get_file()
        session_path = f"data/sessions/{document.file_name}"
        os.makedirs("data/sessions", exist_ok=True)
        await file.download_to_drive(session_path)
        
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            phone = me.phone
        else:
            phone = document.file_name.replace('.session', '')
        await client.disconnect()
        
        if not phone.startswith('+'):
            phone = '+' + phone
        
        cursor.execute('INSERT INTO accounts (phone, session_path, created_at) VALUES (?, ?, ?)',
                      (phone, session_path, datetime.now().isoformat()))
        conn.commit()
        await update.message.reply_text(f"✅ Аккаунт {phone} добавлен!")
        del user_states[user_id]
    except sqlite3.IntegrityError:
        await update.message.reply_text(f"❌ Аккаунт уже существует в базе!")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ===== ОБРАБОТКА ТЕКСТА =====
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    text = update.message.text.strip()
    state = user_states.get(user_id, {})
    
    if state.get('step') == 'waiting_reaction_link':
        link = text
        if 't.me' not in link or not re.search(r't\.me/[\w]+/\d+', link):
            await update.message.reply_text("❌ Неверная ссылка! Пример: https://t.me/durov/123")
            return
        await update.message.reply_text("📱 Отправь смайл для реакции (по умолчанию 🔥)")
        user_states[user_id] = {'step': 'waiting_reaction_emoji', 'link': link}
        return
    
    if state.get('step') == 'waiting_reaction_emoji':
        emoji = text.strip() if text.strip() else "🔥"
        link = state.get('link')
        accounts = get_accounts()
        if not accounts:
            await update.message.reply_text("❌ Нет активных аккаунтов!")
            del user_states[user_id]
            return
        await update.message.reply_text(f"🚀 Ставлю {emoji} на {len(accounts)} аккаунтах...")
        for acc in accounts:
            acc_id, phone, session_path, status, name, status_info, created_at = acc
            await update.message.reply_text(f"🔄 {phone}...")
            result = await set_reaction(session_path, link, emoji)
            if result['success']:
                add_action(acc_id, 'reaction', link, 'success')
                await update.message.reply_text(f"✅ {phone} — {result['emoji']}")
            else:
                add_action(acc_id, 'reaction', link, 'error')
                await update.message.reply_text(f"❌ {phone} — {result['error']}")
            await asyncio.sleep(3)
        await update.message.reply_text(f"✅ Готово!")
        del user_states[user_id]
        return
    
    if state.get('step') == 'waiting_link':
        link = text
        if 't.me' not in link or 'startapp' not in link:
            await update.message.reply_text("❌ Неверная ссылка! Пример: https://t.me/notcoin_bot/app?startapp=ref123")
            return
        accounts = get_accounts()
        if not accounts:
            await update.message.reply_text("❌ Нет активных аккаунтов!")
            del user_states[user_id]
            return
        await update.message.reply_text(f"🚀 Запускаю абуз на {len(accounts)} аккаунтах...")
        for acc in accounts:
            acc_id, phone, session_path, status, name, status_info, created_at = acc
            await update.message.reply_text(f"🔄 {phone}...")
            result = await open_tapp(session_path, link)
            if result['success']:
                add_action(acc_id, 'tapp', link, 'success')
                await update.message.reply_text(f"✅ {phone} — успешно! @{result['bot']}")
            else:
                add_action(acc_id, 'tapp', link, 'error')
                await update.message.reply_text(f"❌ {phone} — {result['error']}")
            await asyncio.sleep(3)
        await update.message.reply_text(f"✅ Готово!")
        del user_states[user_id]
        return

# ===== КНОПКИ =====
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Доступ запрещен!", show_alert=True)
        return
    await query.answer()
    
    if query.data == "stats":
        accounts = get_accounts()
        if not accounts:
            await query.edit_message_text("📊 Аккаунтов пока нет")
            return
        text = "📊 **Статистика аккаунтов:**\n\n"
        for acc in accounts:
            acc_id, phone, session_path, status, name, status_info, created_at = acc
            text += f"📱 **{phone}**\n"
            text += f"   • Имя: {name}\n"
            text += f"   • Статус: {status_info}\n"
            text += f"   • Добавлен: {created_at[:10]}\n\n"
        await query.edit_message_text(text, parse_mode="Markdown")
    
    elif query.data == "refresh":
        await query.edit_message_text("🔄 Обновляю статусы...")
        accounts = get_accounts()
        for acc in accounts:
            acc_id, phone, session_path, status, name, status_info, created_at = acc
            check = await check_account_status(session_path)
            cursor.execute('UPDATE accounts SET name = ?, status_info = ? WHERE id = ?', 
                          (check['name'], check['status'], acc_id))
            conn.commit()
        await query.edit_message_text("✅ Статусы обновлены!")
    
    elif query.data == "upload_session":
        await query.edit_message_text(
            "📂 **Загрузка сессии**\n\n"
            "Отправь файл сессии (с расширением `.session`).\n"
            "Бот сам определит номер телефона.",
            parse_mode="Markdown"
        )
        user_states[query.from_user.id] = {'step': 'waiting_session_file'}
    
    elif query.data == "reaction":
        await query.edit_message_text("🔥 Отправь ссылку на пост:\n`https://t.me/durov/123`", parse_mode="Markdown")
        user_states[query.from_user.id] = {'step': 'waiting_reaction_link'}
    
    elif query.data == "abuse":
        await query.edit_message_text("🚀 Отправь ссылку TApp:\n`https://t.me/bot/app?startapp=ref`", parse_mode="Markdown")
        user_states[query.from_user.id] = {'step': 'waiting_link'}

# ===== ЗАПУСК =====
if __name__ == "__main__":
    print("🚀 БОТ ЗАПУЩЕН!")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()
