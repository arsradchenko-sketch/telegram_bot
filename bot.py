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
TOKEN = os.environ.get("8943768757:AAEHmn1a9Bo3ryCF8snuy0-IxeMYA_YtGfY")
ADMIN_ID = int(os.environ.get("5160672804"))
API_ID = int(os.environ.get("32287172"))
API_HASH = os.environ.get("be028c5a2368a336042b48a00c017a98")
# ==================================================

# ===== БАЗА ДАННЫХ =====
conn = sqlite3.connect("accounts.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE,
        session_path TEXT,
        status TEXT DEFAULT 'active',
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

def add_account(phone, session_path):
    try:
        cursor.execute('INSERT INTO accounts (phone, session_path, created_at) VALUES (?, ?, ?)',
                      (phone, session_path, datetime.now().isoformat()))
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
os.makedirs("sessions", exist_ok=True)
logging.basicConfig(level=logging.INFO)

# ===== РЕАКЦИЯ НА ПОСТ =====
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
                reaction=[ReactionEmoji(emoticons=[emoji])]
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
        [InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add")],
        [InlineKeyboardButton("📋 Список", callback_data="list")],
        [InlineKeyboardButton("🔥 Реакции", callback_data="reaction")],
        [InlineKeyboardButton("🚀 Абуз TApp", callback_data="abuse")],
    ]
    await update.message.reply_text("👋 Выбери:", reply_markup=InlineKeyboardMarkup(keyboard))

# ===== ОБРАБОТКА ТЕКСТА =====
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    text = update.message.text.strip()
    state = user_states.get(user_id, {})
    
    # === ЖДЕМ ССЫЛКУ ДЛЯ РЕАКЦИЙ ===
    if state.get('step') == 'waiting_reaction_link':
        link = text
        if 't.me' not in link or not re.search(r't\.me/[\w]+/\d+', link):
            await update.message.reply_text("❌ Неверная ссылка! Пример: https://t.me/durov/123")
            return
        await update.message.reply_text("📱 Отправь смайл для реакции (по умолчанию 🔥)")
        user_states[user_id] = {'step': 'waiting_reaction_emoji', 'link': link}
        return
    
    # === ЖДЕМ ЭМОДЗИ ===
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
            acc_id, phone, session_path, status, created_at = acc
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
    
    # === ЖДЕМ ССЫЛКУ ДЛЯ АБУЗА ===
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
            acc_id, phone, session_path, status, created_at = acc
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
    
    # === ЖДЕМ НОМЕР ===
    if state.get('step') == 'waiting_phone':
        if not text.startswith('+') or not text[1:].isdigit():
            await update.message.reply_text("❌ Формат: +71234567890")
            return
        try:
            client = TelegramClient(f"sessions/{text}", API_ID, API_HASH)
            await client.connect()
            await client.send_code_request(text)
            user_states[user_id] = {'step': 'waiting_code', 'phone': text, 'client': client}
            await update.message.reply_text(f"📱 Код на {text}\nВведи код:")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
        return
    
    # === ЖДЕМ КОД ===
    if state.get('step') == 'waiting_code':
        try:
            await state['client'].sign_in(state['phone'], text)
            add_account(state['phone'], f"sessions/{state['phone']}")
            await update.message.reply_text(f"✅ Аккаунт {state['phone']} добавлен!")
            await state['client'].disconnect()
            del user_states[user_id]
        except Exception as e:
            if "password" in str(e).lower():
                user_states[user_id]['step'] = 'waiting_password'
                await update.message.reply_text("🔑 Введи пароль 2FA:")
            else:
                await update.message.reply_text(f"❌ Ошибка: {e}")
        return
    
    # === ЖДЕМ ПАРОЛЬ 2FA ===
    if state.get('step') == 'waiting_password':
        try:
            await state['client'].sign_in(password=text)
            add_account(state['phone'], f"sessions/{state['phone']}")
            await update.message.reply_text(f"✅ Аккаунт {state['phone']} добавлен!")
            await state['client'].disconnect()
            del user_states[user_id]
        except Exception as e:
            await update.message.reply_text(f"❌ Неверный пароль: {e}")
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
        total = get_stats()
        await query.edit_message_text(f"📊 Всего: {total}\n✅ Активных: {len(accounts)}")
    elif query.data == "add":
        await query.edit_message_text("📱 Отправь номер:\n`+71234567890`", parse_mode="Markdown")
        user_states[query.from_user.id] = {'step': 'waiting_phone'}
    elif query.data == "list":
        accounts = get_accounts()
        if not accounts:
            await query.edit_message_text("📋 Пусто")
            return
        text = "📋 Аккаунты:\n"
        for acc in accounts:
            text += f"📱 {acc[1]}\n"
        await query.edit_message_text(text)
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
    app.run_polling()
