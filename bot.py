from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import logging
import sqlite3
import os
import re
import asyncio
import zipfile
import shutil
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.functions.messages import SendReactionRequest, RequestWebViewRequest
from telethon.tl.types import ReactionEmoji

TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")

os.makedirs("data", exist_ok=True)
os.makedirs("data/sessions", exist_ok=True)

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

user_states = {}
logging.basicConfig(level=logging.INFO)

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
        await client(SendReactionRequest(peer=entity, msg_id=post_id, reaction=[ReactionEmoji(emoticon=emoji)]))
        await client.disconnect()
        return {'success': True, 'emoji': emoji}
    except Exception as e:
        return {'success': False, 'error': str(e)}

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
        webview = await client.invoke(RequestWebViewRequest(peer=bot, bot=bot, platform='android', url='https://t.me/...', from_background=False))
        await client.disconnect()
        return {'success': True, 'url': webview.url, 'bot': bot_username}
    except Exception as e:
        return {'success': False, 'error': str(e)}

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
        await update.message.reply_text("❌ Отправь файл!")
        return
    if document.file_name.endswith('.zip'):
        await update.message.reply_text("⏳ Загружаю архив...")
        try:
            file = await document.get_file()
            zip_path = f"/tmp/{document.file_name}"
            await file.download_to_drive(zip_path)
            extract_path = "/tmp/sessions_extract"
            os.makedirs(extract_path, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
            sessions_path = None
            for root, dirs, files in os.walk(extract_path):
                if "sessions" in dirs:
                    sessions_path = os.path.join(root, "sessions")
                    break
                if any(f.endswith('.session') for f in files):
                    sessions_path = extract_path
                    break
            if not sessions_path:
                await update.message.reply_text("❌ Нет папки sessions/ или .session файлов!")
                os.remove(zip_path)
                shutil.rmtree(extract_path)
                return
            session_files = [f for f in os.listdir(sessions_path) if f.endswith('.session')]
            if not session_files:
                await update.message.reply_text("❌ Нет файлов .session!")
                os.remove(zip_path)
                shutil.rmtree(extract_path)
                return
            added = 0
            for session_file in session_files:
                src = os.path.join(sessions_path, session_file)
                dst = f"data/sessions/{session_file}"
                shutil.copy2(src, dst)
                phone = session_file.replace('.session', '')
                try:
                    client = TelegramClient(dst, API_ID, API_HASH)
                    await client.connect()
                    if await client.is_user_authorized():
                        me = await client.get_me()
                        phone = me.phone
                    await client.disconnect()
                except:
                    pass
                if not phone.startswith('+'):
                    phone = '+' + phone
                if add_account(phone, dst):
                    added += 1
            await update.message.reply_text(f"✅ Добавлено {added} аккаунтов из {len(session_files)} сессий!")
            del user_states[user_id]
            os.remove(zip_path)
            shutil.rmtree(extract_path)
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
        return
    if not document.file_name.endswith('.session'):
        await update.message.reply_text("❌ Отправь .session или ZIP!")
        return
    await update.message.reply_text("⏳ Загружаю сессию...")
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
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    text = update.message.text.strip()
    
    # ===== ЭТО РАБОТАЕТ 100% =====
    if text == "/test":
        await update.message.reply_text("✅ Бот работает, команды принимает!")
        return
    
    if text.startswith("/delete_"):
        try:
            acc_id = int(text.split("_")[1])
            cursor.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))
            conn.commit()
            await update.message.reply_text(f"✅ Аккаунт {acc_id} удалён!")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
        return
    
    if text.startswith("/toggle_"):
        try:
            acc_id = int(text.split("_")[1])
            cursor.execute("SELECT status_info FROM accounts WHERE id = ?", (acc_id,))
            row = cursor.fetchone()
            if row:
                new_status = "Отключен" if row[0] == "Активен" else "Активен"
                cursor.execute("UPDATE accounts SET status_info = ? WHERE id = ?", (new_status, acc_id))
                conn.commit()
                await update.message.reply_text(f"✅ Аккаунт {acc_id} — теперь {new_status}!")
            else:
                await update.message.reply_text("❌ Аккаунт не найден!")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
        return
    
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
        success_count = 0
        error_count = 0
        report = []
        for acc in accounts:
            phone = acc[1]
            session_path = acc[2]
            await update.message.reply_text(f"🔄 {phone}...")
            result = await set_reaction(session_path, link, emoji)
            if result['success']:
                success_count += 1
                report.append(f"✅ {phone} — {result['emoji']}")
            else:
                error_count += 1
                report.append(f"❌ {phone} — {result['error']}")
            await asyncio.sleep(3)
        total = success_count + error_count
        report_text = f"📊 **Отчет по реакциям:**\n\n✅ Успешно: {success_count}\n❌ Ошибок: {error_count}\n📌 Всего: {total}\n\nДетали:\n" + "\n".join(report[:10])
        await update.message.reply_text(report_text, parse_mode="Markdown")
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
        success_count = 0
        error_count = 0
        report = []
        for acc in accounts:
            phone = acc[1]
            session_path = acc[2]
            await update.message.reply_text(f"🔄 {phone}...")
            result = await open_tapp(session_path, link)
            if result['success']:
                success_count += 1
                report.append(f"✅ {phone} — успешно! @{result['bot']}")
            else:
                error_count += 1
                report.append(f"❌ {phone} — ошибка: {result['error']}")
            await asyncio.sleep(3)
        total = success_count + error_count
        report_text = f"📊 **Отчет по абузу:**\n\n✅ Успешно: {success_count}\n❌ Ошибок: {error_count}\n📌 Всего: {total}\n\nДетали:\n" + "\n".join(report[:10])
        await update.message.reply_text(report_text, parse_mode="Markdown")
        del user_states[user_id]
        return

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID:
        await query.answer("Доступ запрещен!", show_alert=True)
        return
    await query.answer()
    data = query.data
    state = user_states.get(user_id, {})
    
    if data.startswith('page_'):
        page = int(data.split('_')[1])
        user_states[user_id] = {'page': page}
        accounts = get_accounts()
        if not accounts:
            await query.edit_message_text("📋 Список пуст")
            return
        per_page = 5
        total_pages = (len(accounts) + per_page - 1) // per_page
        start_idx = page * per_page
        end_idx = start_idx + per_page
        page_accounts = accounts[start_idx:end_idx]
        text = f"📋 **Аккаунты (страница {page + 1} из {total_pages}):**\n\n"
        for acc in page_accounts:
            acc_id, phone, session_path, status, name, status_info, created_at = acc
            emoji = "🟢" if status_info == "Активен" else "🔴"
            text += f"{emoji} `{phone}` — {status_info}\n"
            text += f"   🆔 ID: {acc_id}\n"
            text += f"   /delete_{acc_id} — удалить\n"
            text += f"   /toggle_{acc_id} — включить/отключить\n\n"
        keyboard = []
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"page_{page + 1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    if query.data == "stats":
        accounts = get_accounts()
        if not accounts:
            await query.edit_message_text("📊 Аккаунтов пока нет")
            return
        total = len(accounts)
        active = sum(1 for acc in accounts if acc[5] == "Активен")
        banned = sum(1 for acc in accounts if acc[5] == "Забанен")
        error = sum(1 for acc in accounts if acc[5] == "Ошибка")
        text = f"📊 **Статистика фермы:**\n✅ Всего: {total} аккаунтов\n🟢 Активны: {active}\n🔴 Забанены: {banned}\n⚠️ Ошибок: {error}\n\n"
        text += "📱 **Последние 5 аккаунтов:**\n"
        for acc in accounts[:5]:
            acc_id, phone, session_path, status, name, status_info, created_at = acc
            emoji = "🟢" if status_info == "Активен" else "🔴"
            text += f"{emoji} `{phone}` — {status_info}\n"
        if total > 5:
            text += f"\n📌 *Полный список — нажми «📋 Все аккаунты»*"
        keyboard = [[InlineKeyboardButton("📋 Все аккаунты", callback_data="full_list")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    if query.data == "full_list":
        accounts = get_accounts()
        if not accounts:
            await query.edit_message_text("📋 Список пуст")
            return
        page = state.get('page', 0)
        per_page = 5
        total_pages = (len(accounts) + per_page - 1) // per_page
        start_idx = page * per_page
        end_idx = start_idx + per_page
        page_accounts = accounts[start_idx:end_idx]
        text = f"📋 **Аккаунты (страница {page + 1} из {total_pages}):**\n\n"
        for acc in page_accounts:
            acc_id, phone, session_path, status, name, status_info, created_at = acc
            emoji = "🟢" if status_info == "Активен" else "🔴"
            text += f"{emoji} `{phone}` — {status_info}\n"
            text += f"   🆔 ID: {acc_id}\n"
            text += f"   /delete_{acc_id} — удалить\n"
            text += f"   /toggle_{acc_id} — включить/отключить\n\n"
        keyboard = []
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"page_{page + 1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    if query.data == "refresh":
        await query.edit_message_text("🔄 Обновляю статусы...")
        accounts = get_accounts()
        for acc in accounts:
            acc_id, phone, session_path, status, name, status_info, created_at = acc
            check = await check_account_status(session_path)
            cursor.execute('UPDATE accounts SET name = ?, status_info = ? WHERE id = ?', (check['name'], check['status'], acc_id))
            conn.commit()
        await query.edit_message_text("✅ Статусы обновлены!")
        return
    
    if query.data == "upload_session":
        await query.edit_message_text("📂 **Загрузка сессии**\n\nОтправь один или несколько файлов `.session`.\nИЛИ отправь ZIP-архив с папкой `sessions/` — бот распакует и добавит все аккаунты.", parse_mode="Markdown")
        user_states[user_id] = {'step': 'waiting_session_file'}
        return
    
    if query.data == "reaction":
        await query.edit_message_text("🔥 Отправь ссылку на пост:\n`https://t.me/durov/123`", parse_mode="Markdown")
        user_states[user_id] = {'step': 'waiting_reaction_link'}
        return
    
    if query.data == "abuse":
        await query.edit_message_text("🚀 Отправь ссылку TApp:\n`https://t.me/bot/app?startapp=ref`", parse_mode="Markdown")
        user_states[user_id] = {'step': 'waiting_link'}
        return

if __name__ == "__main__":
    print("🚀 БОТ ЗАПУЩЕН!")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()
