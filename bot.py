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

# ===== ОБРАБОТКА ФАЙЛОВ (ПРИНИМАЕТ НЕСКОЛЬКО) =====
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    state = user_states.get(user_id, {})
    if state.get('step') != 'waiting_session_file':
        await update.message.reply_text("❌ Сначала нажми кнопку 'Загрузить сессию' в меню!")
        return
    
    # Получаем все файлы из сообщения (если их несколько)
    documents = []
    if update.message.document:
        documents.append(update.message.document)
    
    if not documents:
        await update.message.reply_text("❌ Отправь файл .session!")
        return
    
    # Обрабатываем все файлы
    added_count = 0
    error_count = 0
    report = []
    
    for doc in documents:
        if not doc.file_name.endswith('.session'):
            report.append(f"❌ {doc.file_name} — не .session файл")
            error_count += 1
            continue
        
        try:
            file = await doc.get_file()
            session_path = f"data/sessions/{doc.file_name}"
            os.makedirs("data/sessions", exist_ok=True)
            await file.download_to_drive(session_path)
            
            # Достаём номер из сессии
            client = TelegramClient(session_path, API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                phone = me.phone
            else:
                phone = doc.file_name.replace('.session', '')
            await client.disconnect()
            
            if not phone.startswith('+'):
                phone = '+' + phone
            
            # Добавляем в базу
            cursor.execute('INSERT INTO accounts (phone, session_path, created_at) VALUES (?, ?, ?)',
                          (phone, session_path, datetime.now().isoformat()))
            conn.commit()
            report.append(f"✅ {phone} — добавлен")
            added_count += 1
            
        except sqlite3.IntegrityError:
            report.append(f"❌ {doc.file_name} — уже существует в базе")
            error_count += 1
        except Exception as e:
            report.append(f"❌ {doc.file_name} — ошибка: {e}")
            error_count += 1
    
    # Отчёт
    result_text = f"📊 **Результат загрузки:**\n\n"
    result_text += f"✅ Добавлено: {added_count}\n"
    result_text += f"❌ Ошибок: {error_count}\n\n"
    result_text += "Детали:\n" + "\n".join(report[:15])
    if len(report) > 15:
        result_text += f"\n... и еще {len(report) - 15} файлов"
    
    await update.message.reply_text(result_text, parse_mode="Markdown")
    del user_states[user_id]

# ===== ВЫБОР АККАУНТОВ =====
async def select_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE, action_type: str, link: str = None, emoji: str = None):
    accounts = get_accounts()
    if not accounts:
        await update.message.reply_text("❌ Нет активных аккаунтов!")
        return
    
    keyboard = []
    for acc in accounts:
        acc_id, phone, session_path, status, name, status_info, created_at = acc
        keyboard.append([InlineKeyboardButton(f"☑️ {phone} — {name}", callback_data=f"select_{acc_id}")])
    
    keyboard.append([InlineKeyboardButton("✅ Продолжить с выбранными", callback_data=f"confirm_{action_type}")])
    keyboard.append([InlineKeyboardButton("❌ Отменить", callback_data="cancel_selection")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📱 **Выбери аккаунты для действия:**\n(нажимай на номера, чтобы отметить/снять)", parse_mode="Markdown", reply_markup=reply_markup)
    
    user_states[update.effective_user.id] = {
        'step': 'selecting_accounts',
        'action_type': action_type,
        'link': link,
        'emoji': emoji,
        'selected': []
    }

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
        await select_accounts(update, context, 'reaction', link, emoji)
        return
    
    if state.get('step') == 'waiting_link':
        link = text
        if 't.me' not in link or 'startapp' not in link:
            await update.message.reply_text("❌ Неверная ссылка! Пример: https://t.me/notcoin_bot/app?startapp=ref123")
            return
        await select_accounts(update, context, 'abuse', link)
        return

# ===== ОБРАБОТКА КНОПОК =====
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID:
        await query.answer("Доступ запрещен!", show_alert=True)
        return
    await query.answer()
    
    data = query.data
    state = user_states.get(user_id, {})
    
    if data.startswith('select_'):
        acc_id = int(data.split('_')[1])
        if acc_id in state.get('selected', []):
            state['selected'].remove(acc_id)
        else:
            state['selected'].append(acc_id)
        user_states[user_id] = state
        
        accounts = get_accounts()
        keyboard = []
        for acc in accounts:
            acc_id_db, phone, session_path, status, name, status_info, created_at = acc
            if acc_id_db in state['selected']:
                keyboard.append([InlineKeyboardButton(f"✅ {phone} — {name}", callback_data=f"select_{acc_id_db}")])
            else:
                keyboard.append([InlineKeyboardButton(f"☑️ {phone} — {name}", callback_data=f"select_{acc_id_db}")])
        keyboard.append([InlineKeyboardButton("✅ Продолжить с выбранными", callback_data=f"confirm_{state['action_type']}")])
        keyboard.append([InlineKeyboardButton("❌ Отменить", callback_data="cancel_selection")])
        
        await query.edit_message_text("📱 **Выбери аккаунты для действия:**\n(нажимай на номера, чтобы отметить/снять)", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    if data.startswith('confirm_'):
        action_type = data.split('_')[1]
        selected = state.get('selected', [])
        if not selected:
            await query.edit_message_text("❌ Ты не выбрал ни одного аккаунта!")
            return
        accounts = get_accounts()
        selected_accounts = [acc for acc in accounts if acc[0] in selected]
        await query.edit_message_text(f"🚀 Запускаю на {len(selected_accounts)} выбранных аккаунтах...")
        
        if action_type == 'reaction':
            emoji = state.get('emoji', '🔥')
            link = state.get('link')
            await run_reaction(update, context, selected_accounts, link, emoji)
        elif action_type == 'abuse':
            link = state.get('link')
            await run_abuse(update, context, selected_accounts, link)
        
        del user_states[user_id]
        return
    
    if data == "cancel_selection":
        del user_states[user_id]
        await query.edit_message_text("❌ Выбор отменён.")
        return
    
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
        return
    
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
        return
    
    elif query.data == "upload_session":
        await query.edit_message_text(
            "📂 **Загрузка сессии**\n\n"
            "Отправь один или несколько файлов `.session`.\n"
            "Бот добавит все аккаунты автоматически.",
            parse_mode="Markdown"
        )
        user_states[query.from_user.id] = {'step': 'waiting_session_file'}
        return
    
    elif query.data == "reaction":
        await query.edit_message_text("🔥 Отправь ссылку на пост:\n`https://t.me/durov/123`", parse_mode="Markdown")
        user_states[query.from_user.id] = {'step': 'waiting_reaction_link'}
        return
    
    elif query.data == "abuse":
        await query.edit_message_text("🚀 Отправь ссылку TApp:\n`https://t.me/bot/app?startapp=ref`", parse_mode="Markdown")
        user_states[query.from_user.id] = {'step': 'waiting_link'}
        return

# ===== ЗАПУСК РЕАКЦИЙ =====
async def run_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE, accounts, link, emoji):
    success_count = 0
    error_count = 0
    report = []
    
    for acc in accounts:
        acc_id, phone, session_path, status, name, status_info, created_at = acc
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"🔄 {phone}...")
        result = await set_reaction(session_path, link, emoji)
        
        if result['success']:
            success_count += 1
            add_action(acc_id, 'reaction', link, 'success')
            report.append(f"✅ {phone} — {result['emoji']}")
        else:
            error_count += 1
            add_action(acc_id, 'reaction', link, 'error')
            report.append(f"❌ {phone} — {result['error']}")
        
        await asyncio.sleep(3)
    
    total = success_count + error_count
    report_text = f"📊 **Отчет по реакциям:**\n\n"
    report_text += f"✅ Успешно: {success_count}\n"
    report_text += f"❌ Ошибок: {error_count}\n"
    report_text += f"📌 Всего: {total}\n\n"
    report_text += "Детали:\n" + "\n".join(report[:10])
    if len(report) > 10:
        report_text += f"\n... и еще {len(report) - 10} аккаунтов"
    
    await context.bot.send_message(chat_id=update.effective_user.id, text=report_text, parse_mode="Markdown")

# ===== ЗАПУСК АБУЗА =====
async def run_abuse(update: Update, context: ContextTypes.DEFAULT_TYPE, accounts, link):
    success_count = 0
    error_count = 0
    report = []
    
    for acc in accounts:
        acc_id, phone, session_path, status, name, status_info, created_at = acc
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"🔄 {phone}...")
        result = await open_tapp(session_path, link)
        
        if result['success']:
            success_count += 1
            add_action(acc_id, 'tapp', link, 'success')
            report.append(f"✅ {phone} — успешно! @{result['bot']}")
        else:
            error_count += 1
            add_action(acc_id, 'tapp', link, 'error')
            report.append(f"❌ {phone} — ошибка: {result['error']}")
        
        await asyncio.sleep(3)
    
    total = success_count + error_count
    report_text = f"📊 **Отчет по абузу:**\n\n"
    report_text += f"✅ Успешно: {success_count}\n"
    report_text += f"❌ Ошибок: {error_count}\n"
    report_text += f"📌 Всего: {total}\n\n"
    report_text += "Детали:\n" + "\n".join(report[:10])
    if len(report) > 10:
        report_text += f"\n... и еще {len(report) - 10} аккаунтов"
    
    await context.bot.send_message(chat_id=update.effective_user.id, text=report_text, parse_mode="Markdown")

# ===== ЗАПУСК =====
if __name__ == "__main__":
    print("🚀 БОТ ЗАПУЩЕН!")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()
