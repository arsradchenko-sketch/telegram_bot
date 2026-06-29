from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import logging
import sqlite3
import os
import re
import asyncio
import zipfile
import shutil
import random
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.functions.messages import SendReactionRequest, GetMessagesViewsRequest
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

# ───────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ───────────────────────────────────────────

def parse_reaction_input(text):
    """
    Разбирает строку вида '🔥3 ❤️2 ⚡2' или '🔥 3, ❤️ 2, ⚡ 2'
    Возвращает список [(emoji, count), ...] или None при ошибке.
    """
    # Удаляем лишние символы-разделители
    text = text.replace(',', ' ').replace(';', ' ')
    # Ищем пары: эмодзи + число (с пробелом или без)
    pattern = r'([\U00010000-\U0010ffff][\ufe0f\u20e3]?|[\u2600-\u27BF][\ufe0f]?)\s*(\d+)'
    matches = re.findall(pattern, text)
    if not matches:
        return None
    result = []
    for emoji, count in matches:
        result.append((emoji.strip(), int(count)))
    return result

async def view_post(session_path, channel_username, post_id):
    """Открывает пост — засчитывается просмотр."""
    try:
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return {'success': False, 'error': 'Сессия не активна'}
        entity = await client.get_entity(f"@{channel_username}")
        await client(GetMessagesViewsRequest(
            peer=entity,
            id=[post_id],
            increment=True
        ))
        await client.disconnect()
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}

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
        await client(SendReactionRequest(
            peer=entity,
            msg_id=post_id,
            reaction=[ReactionEmoji(emoticon=emoji)]
        ))
        await client.disconnect()
        return {'success': True, 'emoji': emoji}
    except Exception as e:
        return {'success': False, 'error': str(e)}

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
        return {'status': '❌ Ошибка', 'name': 'Неизвестно'}

async def open_tapp(session_path, link):
    try:
        from telethon.tl.functions.messages import RequestWebViewRequest
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
        webview = await client.invoke(RequestWebViewRequest(
            peer=bot, bot=bot, platform='android',
            url=f'https://t.me/{bot_username}', from_background=False
        ))
        await client.disconnect()
        return {'success': True, 'url': webview.url, 'bot': bot_username}
    except Exception as e:
        return {'success': False, 'error': str(e)}

# ───────────────────────────────────────────
#  ЛОГИКА РАСШИРЕННЫХ РЕАКЦИЙ
# ───────────────────────────────────────────

async def run_advanced_reactions(update, user_id, link, reaction_plan, view_accounts):
    """
    reaction_plan: [(emoji, [session_path, ...]), ...]
    view_accounts: [session_path, ...]
    """
    match = re.search(r't\.me/([^/]+)/(\d+)', link)
    channel_username = match.group(1)
    post_id = int(match.group(2))

    total_reaction_accs = sum(len(accs) for _, accs in reaction_plan)
    total_view_accs = len(view_accounts)

    await update.message.reply_text(
        f"🚀 **Запуск:**\n"
        f"👁 Просмотры: {total_view_accs} аккаунтов\n"
        f"🔥 Реакции: {total_reaction_accs} аккаунтов\n"
        f"⏱ Сначала просмотры, потом реакции...",
        parse_mode="Markdown"
    )

    # ── Шаг 1: просмотры (случайный порядок + случайная задержка) ──
    view_success = 0
    view_errors = 0
    shuffled_viewers = list(view_accounts)
    random.shuffle(shuffled_viewers)

    for session_path in shuffled_viewers:
        result = await view_post(session_path, channel_username, post_id)
        if result['success']:
            view_success += 1
        else:
            view_errors += 1
        delay = random.uniform(2, 6)
        await asyncio.sleep(delay)

    await update.message.reply_text(
        f"👁 **Просмотры готовы:**\n"
        f"✅ Успешно: {view_success}\n"
        f"❌ Ошибок: {view_errors}\n\n"
        f"⏳ Небольшая пауза перед реакциями...",
        parse_mode="Markdown"
    )
    # Пауза между просмотрами и реакциями (5–15 сек)
    await asyncio.sleep(random.uniform(5, 15))

    # ── Шаг 2: реакции (случайный порядок внутри каждой группы) ──
    reaction_success = 0
    reaction_errors = 0
    reaction_report = []

    # Перемешиваем порядок групп реакций для естественности
    shuffled_plan = list(reaction_plan)
    random.shuffle(shuffled_plan)

    for emoji, sessions in shuffled_plan:
        random.shuffle(sessions)
        for session_path in sessions:
            result = await set_reaction(session_path, link, emoji)
            if result['success']:
                reaction_success += 1
                reaction_report.append(f"✅ {emoji} — успешно")
            else:
                reaction_errors += 1
                reaction_report.append(f"❌ {emoji} — {result['error']}")
            # Случайная задержка 3–10 сек между реакциями
            delay = random.uniform(3, 10)
            await asyncio.sleep(delay)

    # ── Итоговый отчёт ──
    report_lines = "\n".join(reaction_report[:15])
    if len(reaction_report) > 15:
        report_lines += f"\n...и ещё {len(reaction_report) - 15} действий"

    summary = (
        f"📊 **Итоговый отчёт:**\n\n"
        f"👁 Просмотры: ✅{view_success} / ❌{view_errors}\n"
        f"💬 Реакции: ✅{reaction_success} / ❌{reaction_errors}\n"
        f"📌 Всего аккаунтов: {total_view_accs + total_reaction_accs}\n\n"
        f"**Детали реакций:**\n{report_lines}"
    )
    await update.message.reply_text(summary, parse_mode="Markdown")

# ───────────────────────────────────────────
#  HANDLERS
# ───────────────────────────────────────────

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
    await update.message.reply_text("👋 Выбери действие:", reply_markup=InlineKeyboardMarkup(keyboard))

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
    state = user_states.get(user_id, {})

    # ── Шаг 1: ссылка на пост ──
    if state.get('step') == 'waiting_reaction_link':
        link = text
        if 't.me' not in link or not re.search(r't\.me/[\w]+/\d+', link):
            await update.message.reply_text("❌ Неверная ссылка!\nПример: https://t.me/durov/123")
            return
        accounts = get_accounts()
        total = len(accounts)
        if total == 0:
            await update.message.reply_text("❌ Нет активных аккаунтов!")
            del user_states[user_id]
            return
        user_states[user_id] = {'step': 'waiting_reaction_count', 'link': link, 'total': total}
        await update.message.reply_text(
            f"✅ Ссылка принята!\n\n"
            f"📱 Всего активных аккаунтов: **{total}**\n\n"
            f"Сколько аккаунтов поставят реакции?\n"
            f"Введи число (от 1 до {total}):",
            parse_mode="Markdown"
        )
        return

    # ── Шаг 2: количество аккаунтов для реакций ──
    if state.get('step') == 'waiting_reaction_count':
        if not text.isdigit():
            await update.message.reply_text("❌ Введи число!")
            return
        count = int(text)
        total = state['total']
        if count < 1 or count > total:
            await update.message.reply_text(f"❌ Число должно быть от 1 до {total}!")
            return
        view_count = total - count
        user_states[user_id] = {
            'step': 'waiting_reaction_distribution',
            'link': state['link'],
            'reaction_count': count,
            'view_count': view_count,
            'total': total
        }
        await update.message.reply_text(
            f"👍 {count} аккаунтов поставят реакции\n"
            f"👁 {view_count} аккаунтов дадут просмотры\n\n"
            f"Теперь задай распределение реакций.\n"
            f"Сумма должна равняться **{count}**.\n\n"
            f"Формат: `🔥3 ❤️2 ⚡2`\n"
            f"Поддерживаемые: 🔥 ❤️ ⚡ 👍 👎 🎉 🤩 😢 💯 🤮",
            parse_mode="Markdown"
        )
        return

    # ── Шаг 3: распределение реакций ──
    if state.get('step') == 'waiting_reaction_distribution':
        reaction_count = state['reaction_count']
        parsed = parse_reaction_input(text)
        if not parsed:
            await update.message.reply_text(
                "❌ Не могу разобрать формат!\n"
                "Пример: `🔥3 ❤️2 ⚡2`",
                parse_mode="Markdown"
            )
            return
        total_assigned = sum(c for _, c in parsed)
        if total_assigned != reaction_count:
            await update.message.reply_text(
                f"❌ Сумма реакций {total_assigned} ≠ {reaction_count}!\n"
                f"Исправь распределение.",
                parse_mode="Markdown"
            )
            return

        # Раздаём аккаунты по группам
        accounts = get_accounts()
        random.shuffle(accounts)  # перемешиваем для случайности

        reaction_accs = accounts[:reaction_count]
        view_accs = accounts[reaction_count:]

        # Строим plan: [(emoji, [session_path, ...]), ...]
        reaction_plan = []
        idx = 0
        for emoji, count in parsed:
            group_sessions = [reaction_accs[i][2] for i in range(idx, idx + count)]
            reaction_plan.append((emoji, group_sessions))
            idx += count

        view_sessions = [acc[2] for acc in view_accs]

        # Показываем план и просим подтверждение
        plan_text = "📋 **План действий:**\n\n"
        plan_text += f"👁 Просмотры: {len(view_sessions)} аккаунтов\n"
        for emoji, sessions in reaction_plan:
            plan_text += f"{emoji} Реакция: {len(sessions)} аккаунтов\n"
        plan_text += "\n⏱ Задержка: 2–6 сек между просмотрами, 3–10 сек между реакциями\n\n"
        plan_text += "Подтверди запуск — напиши **да** или **нет**"

        user_states[user_id] = {
            'step': 'waiting_reaction_confirm',
            'link': state['link'],
            'reaction_plan': reaction_plan,
            'view_sessions': view_sessions
        }
        await update.message.reply_text(plan_text, parse_mode="Markdown")
        return

    # ── Шаг 4: подтверждение ──
    if state.get('step') == 'waiting_reaction_confirm':
        if text.lower() in ('да', 'yes', 'go', 'старт', '+'):
            link = state['link']
            reaction_plan = state['reaction_plan']
            view_sessions = state['view_sessions']
            del user_states[user_id]
            await run_advanced_reactions(update, user_id, link, reaction_plan, view_sessions)
        else:
            del user_states[user_id]
            await update.message.reply_text("❌ Отменено. Нажми /start чтобы начать заново.")
        return

    # ── Старый flow абуза TApp ──
    if state.get('step') == 'waiting_link':
        link = text
        if 't.me' not in link or 'startapp' not in link:
            await update.message.reply_text("❌ Неверная ссылка!\nПример: https://t.me/notcoin_bot/app?startapp=ref123")
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
            await asyncio.sleep(random.uniform(3, 8))
        total = success_count + error_count
        report_text = (
            f"📊 **Отчет по абузу:**\n\n"
            f"✅ Успешно: {success_count}\n❌ Ошибок: {error_count}\n📌 Всего: {total}\n\n"
            f"Детали:\n" + "\n".join(report[:10])
        )
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

    if data.startswith('delete_'):
        acc_id = int(data.split('_')[1])
        cursor.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))
        conn.commit()
        await query.edit_message_text(f"✅ Аккаунт {acc_id} удалён!")
        return

    if data.startswith('toggle_'):
        acc_id = int(data.split('_')[1])
        cursor.execute("SELECT status_info FROM accounts WHERE id = ?", (acc_id,))
        row = cursor.fetchone()
        if row:
            new_status = "Отключен" if row[0] == "Активен" else "Активен"
            cursor.execute("UPDATE accounts SET status_info = ? WHERE id = ?", (new_status, acc_id))
            conn.commit()
            await query.edit_message_text(f"✅ Аккаунт {acc_id} — теперь {new_status}!")
        else:
            await query.edit_message_text("❌ Аккаунт не найден!")
        return

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
            emoji = "🟢" if ("Активен" in status_info) else "🔴"
            text += f"{emoji} `{phone}` — {status_info}\n   🆔 ID: {acc_id}\n\n"
        keyboard = []
        for acc in page_accounts:
            acc_id = acc[0]
            keyboard.append([
                InlineKeyboardButton(f"🗑️ Удалить {acc_id}", callback_data=f"delete_{acc_id}"),
                InlineKeyboardButton(f"⛔ Откл. {acc_id}", callback_data=f"toggle_{acc_id}")
            ])
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"page_{page + 1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "stats":
        accounts = get_accounts()
        if not accounts:
            await query.edit_message_text("📊 Аккаунтов пока нет")
            return
        total = len(accounts)
        active = sum(1 for acc in accounts if "Активен" in acc[5])
        banned = sum(1 for acc in accounts if "Забанен" in acc[5])
        error = sum(1 for acc in accounts if "Ошибка" in acc[5])
        text = (
            f"📊 **Статистика фермы:**\n"
            f"✅ Всего: {total}\n🟢 Активны: {active}\n"
            f"🔴 Забанены: {banned}\n⚠️ Ошибок: {error}\n\n"
            f"📱 **Последние 5:**\n"
        )
        for acc in accounts[:5]:
            acc_id, phone, session_path, status, name, status_info, created_at = acc
            e = "🟢" if ("Активен" in status_info) else "🔴"
            text += f"{e} `{phone}` — {status_info}\n"
        keyboard = [[InlineKeyboardButton("📋 Все аккаунты", callback_data="full_list")]]
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "full_list":
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
        text = f"📋 **Аккаунты (стр. {page + 1}/{total_pages}):**\n\n"
        for acc in page_accounts:
            acc_id, phone, session_path, status, name, status_info, created_at = acc
            e = "🟢" if ("Активен" in status_info) else "🔴"
            text += f"{e} `{phone}` — {status_info}\n   🆔 ID: {acc_id}\n\n"
        keyboard = []
        for acc in page_accounts:
            acc_id = acc[0]
            keyboard.append([
                InlineKeyboardButton(f"🗑️ Удалить {acc_id}", callback_data=f"delete_{acc_id}"),
                InlineKeyboardButton(f"⛔ Откл. {acc_id}", callback_data=f"toggle_{acc_id}")
            ])
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"page_{page + 1}"))
        if nav_buttons:
            keyboard.append(nav_buttons)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "refresh":
        await query.edit_message_text("🔄 Обновляю статусы...")
        accounts = get_accounts()
        for acc in accounts:
            acc_id, phone, session_path, status, name, status_info, created_at = acc
            check = await check_account_status(session_path)
            cursor.execute('UPDATE accounts SET name = ?, status_info = ? WHERE id = ?',
                           (check['name'], check['status'], acc_id))
            conn.commit()
        keyboard = [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
        await query.edit_message_text("✅ Статусы обновлены!", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "main_menu":
        keyboard = [
            [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
            [InlineKeyboardButton("🔄 Обновить статусы", callback_data="refresh")],
            [InlineKeyboardButton("📂 Загрузить сессию", callback_data="upload_session")],
            [InlineKeyboardButton("🔥 Реакции", callback_data="reaction")],
            [InlineKeyboardButton("🚀 Абуз TApp", callback_data="abuse")],
        ]
        await query.edit_message_text("👋 Выбери действие:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "upload_session":
        await query.edit_message_text(
            "📂 **Загрузка сессии**\n\n"
            "Отправь один или несколько файлов `.session`.\n"
            "ИЛИ ZIP-архив с папкой `sessions/`.",
            parse_mode="Markdown"
        )
        user_states[user_id] = {'step': 'waiting_session_file'}
        return

    if data == "reaction":
        accounts = get_accounts()
        total = len(accounts)
        if total == 0:
            await query.edit_message_text("❌ Нет активных аккаунтов!")
            return
        await query.edit_message_text(
            f"🔥 **Расширенные реакции**\n\n"
            f"📱 Доступно аккаунтов: **{total}**\n\n"
            f"Отправь ссылку на пост:\n`https://t.me/durov/123`",
            parse_mode="Markdown"
        )
        user_states[user_id] = {'step': 'waiting_reaction_link'}
        return

    if data == "abuse":
        await query.edit_message_text(
            "🚀 Отправь ссылку TApp:\n`https://t.me/bot/app?startapp=ref`",
            parse_mode="Markdown"
        )
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
