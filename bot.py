from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import logging
import logging.handlers
import sqlite3
import os
import re
import asyncio
import zipfile
import shutil
import random
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.functions.messages import SendReactionRequest, GetMessagesViewsRequest, RequestWebViewRequest
from telethon.tl.types import ReactionEmoji

# ───────────────────────────────────────────
#  КОНФИГ
# ───────────────────────────────────────────
TOKEN    = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
API_ID   = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")

TELETHON_TIMEOUT = 30  # секунд до таймаута на любой Telethon-запрос

os.makedirs("data", exist_ok=True)
os.makedirs("data/sessions", exist_ok=True)
os.makedirs("data/logs", exist_ok=True)

# ───────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ───────────────────────────────────────────
logger = logging.getLogger("farm_bot")
logger.setLevel(logging.INFO)

# В консоль
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(console_handler)

# В файл с ротацией (макс 5 МБ, 3 файла)
file_handler = logging.handlers.RotatingFileHandler(
    "data/logs/bot.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"
)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(file_handler)

# ───────────────────────────────────────────
#  БАЗА ДАННЫХ (thread-safe через lock)
# ───────────────────────────────────────────
db_lock = asyncio.Lock()
conn   = sqlite3.connect("data/accounts.db", check_same_thread=False)
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

def db_execute(query, params=()):
    """Безопасное выполнение запроса к БД."""
    try:
        cursor.execute(query, params)
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"DB error: {e} | query: {query} | params: {params}")
        return False

def add_account(phone, session_path, name="Неизвестно", status_info="Активен"):
    return db_execute(
        'INSERT OR IGNORE INTO accounts (phone, session_path, name, status_info, created_at) VALUES (?, ?, ?, ?, ?)',
        (phone, session_path, name, status_info, datetime.now().isoformat())
    )

def get_accounts():
    cursor.execute('SELECT * FROM accounts WHERE status = "active"')
    return cursor.fetchall()

# ───────────────────────────────────────────
#  СОСТОЯНИЯ И ЗАЩИТА ОТ ДВОЙНОГО ЗАПУСКА
# ───────────────────────────────────────────
user_states    = {}
active_tasks   = set()   # user_id тех, у кого сейчас идёт задача

# ───────────────────────────────────────────
#  ГЛАВНОЕ МЕНЮ (ДОБАВЛЕНА КНОПКА РЕФКИ)
# ───────────────────────────────────────────
MAIN_KEYBOARD = [
    [InlineKeyboardButton("📊 Статистика",       callback_data="stats")],
    [InlineKeyboardButton("🔄 Обновить статусы", callback_data="refresh")],
    [InlineKeyboardButton("📂 Загрузить сессию", callback_data="upload_session")],
    [InlineKeyboardButton("🔥 Реакции",          callback_data="reaction")],
    [InlineKeyboardButton("🔗 Рефка (старт)",    callback_data="referral")],
    [InlineKeyboardButton("🚀 Абуз TApp",        callback_data="abuse")],
    [InlineKeyboardButton("📤 Экспорт аккаунтов",callback_data="export")],
]
BACK_BUTTON = [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]

# ───────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ───────────────────────────────────────────

def parse_reaction_input(text):
    """'🔥3 ❤️2 ⚡2' → [(emoji, count), ...]"""
    text = text.replace(',', ' ').replace(';', ' ')
    pattern = r'([\U00010000-\U0010ffff][\ufe0f\u20e3]?|[\u2600-\u27BF][\ufe0f]?)\s*(\d+)'
    matches = re.findall(pattern, text)
    if not matches:
        return None
    return [(emoji.strip(), int(count)) for emoji, count in matches]

def acc_emoji(status_info):
    return "🟢" if "Активен" in status_info else "🔴"

async def safe_disconnect(client):
    """Отключаемся от Telethon без исключений."""
    try:
        await client.disconnect()
    except Exception:
        pass

# ───────────────────────────────────────────
#  НОВАЯ ФУНКЦИЯ ДЛЯ РЕФЕРАЛКИ
# ───────────────────────────────────────────
async def process_referral_start(session_path, link):
    """Переходит по ссылке и жмёт старт (отправляет /start)"""
    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TIMEOUT)
        if not await client.is_user_authorized():
            return {'success': False, 'error': 'Сессия не активна'}
        
        # Парсим ссылку
        match_tapp = re.search(r't\.me/([^/]+)/app\?startapp=([^&]+)', link)
        match_start = re.search(r't\.me/([^?]+)\?start=([^&]+)', link)
        match_bot = re.search(r't\.me/([^/?]+)', link)
        
        if match_tapp:
            bot_username = match_tapp.group(1)
            start_param = match_tapp.group(2)
            bot = await asyncio.wait_for(client.get_entity(f"@{bot_username}"), timeout=TELETHON_TIMEOUT)
            await client.send_message(bot, f"/start {start_param}")
            await asyncio.sleep(2)
            try:
                webview = await asyncio.wait_for(
                    client.invoke(RequestWebViewRequest(
                        peer=bot, bot=bot, platform='android',
                        url=f'https://t.me/{bot_username}', from_background=False
                    )),
                    timeout=TELETHON_TIMEOUT
                )
                logger.info(f"referral_start OK (tapp): {session_path} → @{bot_username}")
                return {'success': True, 'type': 'tapp', 'bot': bot_username}
            except Exception:
                logger.info(f"referral_start OK (tapp no webview): {session_path} → @{bot_username}")
                return {'success': True, 'type': 'tapp_no_webview', 'bot': bot_username}
        
        elif match_start:
            bot_username = match_start.group(1)
            start_param = match_start.group(2)
            bot = await asyncio.wait_for(client.get_entity(f"@{bot_username}"), timeout=TELETHON_TIMEOUT)
            await client.send_message(bot, f"/start {start_param}")
            await asyncio.sleep(2)
            logger.info(f"referral_start OK (start): {session_path} → @{bot_username}")
            return {'success': True, 'type': 'start', 'bot': bot_username}
        
        elif match_bot:
            bot_username = match_bot.group(1)
            bot = await asyncio.wait_for(client.get_entity(f"@{bot_username}"), timeout=TELETHON_TIMEOUT)
            await client.send_message(bot, "/start")
            await asyncio.sleep(2)
            logger.info(f"referral_start OK (start_no_param): {session_path} → @{bot_username}")
            return {'success': True, 'type': 'start_no_param', 'bot': bot_username}
        
        else:
            return {'success': False, 'error': 'Не удалось распознать ссылку'}
            
    except asyncio.TimeoutError:
        return {'success': False, 'error': 'Таймаут'}
    except Exception as e:
        logger.warning(f"referral_start ERROR: {session_path} → {e}")
        return {'success': False, 'error': str(e)}
    finally:
        await safe_disconnect(client)

async def run_referral(update, user_id, selected_accounts, link):
    """Запускает рефку на выбранных аккаунтах"""
    active_tasks.add(user_id)
    await update.message.reply_text(f"🚀 Запускаю рефку на {len(selected_accounts)} аккаунтах...")
    success_count = 0
    error_count = 0
    report = []
    for acc in selected_accounts:
        phone = acc[1]
        session_path = acc[2]
        await update.message.reply_text(f"🔄 {phone}...")
        result = await process_referral_start(session_path, link)
        if result['success']:
            success_count += 1
            report.append(f"✅ {phone} — успешно! (@{result['bot']})")
        else:
            error_count += 1
            report.append(f"❌ {phone} — ошибка: {result['error'][:40]}")
        await asyncio.sleep(random.uniform(3, 8))
    total = success_count + error_count
    report_text = (
        f"📊 **Отчёт по рефке:**\n\n"
        f"✅ Успешно: {success_count}\n"
        f"❌ Ошибок: {error_count}\n"
        f"📌 Всего: {total}\n\n"
        f"Детали:\n" + "\n".join(report[:10])
    )
    await update.message.reply_text(report_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
    active_tasks.discard(user_id)

# ───────────────────────────────────────────
#  TELETHON-ФУНКЦИИ (с таймаутом и finally)
# ───────────────────────────────────────────

async def check_account_status(session_path):
    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TIMEOUT)
        if not await client.is_user_authorized():
            return {'status': '❌ Забанен', 'name': 'Неизвестно'}
        me = await asyncio.wait_for(client.get_me(), timeout=TELETHON_TIMEOUT)
        name = me.first_name or me.username or 'Без имени'
        logger.info(f"check_status OK: {session_path} → {name}")
        return {'status': '✅ Активен', 'name': name}
    except asyncio.TimeoutError:
        logger.warning(f"check_status TIMEOUT: {session_path}")
        return {'status': '❌ Ошибка', 'name': 'Таймаут'}
    except Exception as e:
        logger.warning(f"check_status ERROR: {session_path} → {e}")
        return {'status': '❌ Ошибка', 'name': 'Неизвестно'}
    finally:
        await safe_disconnect(client)

async def view_post(session_path, channel_username, post_id):
    """Засчитывает просмотр поста."""
    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TIMEOUT)
        if not await client.is_user_authorized():
            return {'success': False, 'error': 'Сессия не активна'}
        entity = await asyncio.wait_for(client.get_entity(f"@{channel_username}"), timeout=TELETHON_TIMEOUT)
        await asyncio.wait_for(
            client(GetMessagesViewsRequest(peer=entity, id=[post_id], increment=True)),
            timeout=TELETHON_TIMEOUT
        )
        logger.info(f"view_post OK: {session_path} → @{channel_username}/{post_id}")
        return {'success': True}
    except asyncio.TimeoutError:
        return {'success': False, 'error': 'Таймаут'}
    except Exception as e:
        logger.warning(f"view_post ERROR: {session_path} → {e}")
        return {'success': False, 'error': str(e)}
    finally:
        await safe_disconnect(client)

async def set_reaction(session_path, link, emoji="🔥"):
    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TIMEOUT)
        if not await client.is_user_authorized():
            return {'success': False, 'error': 'Сессия не активна'}
        match = re.search(r't\.me/([^/]+)/(\d+)', link)
        if not match:
            return {'success': False, 'error': 'Неверный формат ссылки'}
        channel_username = match.group(1)
        post_id = int(match.group(2))
        entity = await asyncio.wait_for(client.get_entity(f"@{channel_username}"), timeout=TELETHON_TIMEOUT)
        await asyncio.wait_for(
            client(SendReactionRequest(peer=entity, msg_id=post_id, reaction=[ReactionEmoji(emoticon=emoji)])),
            timeout=TELETHON_TIMEOUT
        )
        logger.info(f"set_reaction OK: {session_path} → {emoji} на {link}")
        return {'success': True, 'emoji': emoji}
    except asyncio.TimeoutError:
        return {'success': False, 'error': 'Таймаут'}
    except Exception as e:
        logger.warning(f"set_reaction ERROR: {session_path} → {e}")
        return {'success': False, 'error': str(e)}
    finally:
        await safe_disconnect(client)

async def open_tapp(session_path, link):
    from telethon.tl.functions.messages import RequestWebViewRequest
    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TIMEOUT)
        if not await client.is_user_authorized():
            return {'success': False, 'error': 'Сессия не активна'}
        match = re.search(r't\.me/([^/]+)/app\?startapp=([^&]+)', link)
        if not match:
            return {'success': False, 'error': 'Неверный формат ссылки'}
        bot_username = match.group(1)
        start_param  = match.group(2)
        bot = await asyncio.wait_for(client.get_entity(f"@{bot_username}"), timeout=TELETHON_TIMEOUT)
        await client.send_message(bot, f"/start {start_param}")
        await asyncio.sleep(2)
        webview = await asyncio.wait_for(
            client.invoke(RequestWebViewRequest(
                peer=bot, bot=bot, platform='android',
                url=f'https://t.me/{bot_username}', from_background=False
            )),
            timeout=TELETHON_TIMEOUT
        )
        logger.info(f"open_tapp OK: {session_path} → @{bot_username}")
        return {'success': True, 'url': webview.url, 'bot': bot_username}
    except asyncio.TimeoutError:
        return {'success': False, 'error': 'Таймаут'}
    except Exception as e:
        logger.warning(f"open_tapp ERROR: {session_path} → {e}")
        return {'success': False, 'error': str(e)}
    finally:
        await safe_disconnect(client)

# ───────────────────────────────────────────
#  ЛОГИКА РАСШИРЕННЫХ РЕАКЦИЙ
# ───────────────────────────────────────────

async def run_advanced_reactions(update, user_id, link, reaction_plan, view_sessions):
    active_tasks.add(user_id)
    match = re.search(r't\.me/([^/]+)/(\d+)', link)
    channel_username = match.group(1)
    post_id = int(match.group(2))

    total_reaction_accs = sum(len(accs) for _, accs in reaction_plan)
    total_view_accs = len(view_sessions)

    logger.info(f"run_advanced_reactions START: user={user_id}, link={link}, "
                f"reactions={total_reaction_accs}, views={total_view_accs}")

    await update.message.reply_text(
        f"🚀 **Запуск задачи:**\n"
        f"👁 Просмотры: {total_view_accs} аккаунтов\n"
        f"🔥 Реакции: {total_reaction_accs} аккаунтов\n"
        f"⏱ Сначала просмотры, потом реакции...",
        parse_mode="Markdown"
    )

    # ── Шаг 1: просмотры ──
    view_success = 0
    view_errors  = 0
    shuffled_viewers = list(view_sessions)
    random.shuffle(shuffled_viewers)

    for session_path in shuffled_viewers:
        result = await view_post(session_path, channel_username, post_id)
        if result['success']:
            view_success += 1
        else:
            view_errors += 1
        await asyncio.sleep(random.uniform(2, 6))

    await update.message.reply_text(
        f"👁 **Просмотры готовы:**\n"
        f"✅ Успешно: {view_success}  ❌ Ошибок: {view_errors}\n\n"
        f"⏳ Пауза перед реакциями...",
        parse_mode="Markdown"
    )
    await asyncio.sleep(random.uniform(5, 15))

    # ── Шаг 2: реакции ──
    reaction_success = 0
    reaction_errors  = 0
    reaction_report  = []

    shuffled_plan = list(reaction_plan)
    random.shuffle(shuffled_plan)

    for emoji, sessions in shuffled_plan:
        random.shuffle(sessions)
        for session_path in sessions:
            result = await set_reaction(session_path, link, emoji)
            if result['success']:
                reaction_success += 1
                reaction_report.append(f"✅ {emoji}")
            else:
                reaction_errors += 1
                reaction_report.append(f"❌ {emoji} — {result['error'][:40]}")
            await asyncio.sleep(random.uniform(3, 10))

    # ── Итог ──
    report_lines = "\n".join(reaction_report[:15])
    if len(reaction_report) > 15:
        report_lines += f"\n...и ещё {len(reaction_report) - 15}"

    await update.message.reply_text(
        f"📊 **Итоговый отчёт:**\n\n"
        f"👁 Просмотры: ✅{view_success} / ❌{view_errors}\n"
        f"💬 Реакции:   ✅{reaction_success} / ❌{reaction_errors}\n"
        f"📌 Всего аккаунтов: {total_view_accs + total_reaction_accs}\n\n"
        f"**Детали:**\n{report_lines}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    logger.info(f"run_advanced_reactions DONE: user={user_id}, "
                f"views={view_success}/{view_errors}, reactions={reaction_success}/{reaction_errors}")
    active_tasks.discard(user_id)

# ───────────────────────────────────────────
#  HANDLERS
# ───────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Доступ запрещен!")
        return
    await update.message.reply_text("👋 Выбери действие:", reply_markup=InlineKeyboardMarkup(MAIN_KEYBOARD))

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    state = user_states.get(user_id, {})
    if state.get('step') != 'waiting_session_file':
        await update.message.reply_text("❌ Сначала нажми «📂 Загрузить сессию» в меню!")
        return
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ Отправь файл!")
        return

    # ── ZIP-архив ──
    if document.file_name.endswith('.zip'):
        await update.message.reply_text("⏳ Загружаю архив...")
        try:
            file        = await document.get_file()
            zip_path    = f"/tmp/{document.file_name}"
            extract_path = "/tmp/sessions_extract"
            await file.download_to_drive(zip_path)
            os.makedirs(extract_path, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(extract_path)

            sessions_path = None
            for root, dirs, files in os.walk(extract_path):
                if "sessions" in dirs:
                    sessions_path = os.path.join(root, "sessions")
                    break
                if any(f.endswith('.session') for f in files):
                    sessions_path = root
                    break

            if not sessions_path:
                await update.message.reply_text("❌ Нет папки sessions/ или .session файлов!")
                return

            session_files = [f for f in os.listdir(sessions_path) if f.endswith('.session')]
            if not session_files:
                await update.message.reply_text("❌ Нет файлов .session в архиве!")
                return

            added = 0
            for sf in session_files:
                src = os.path.join(sessions_path, sf)
                dst = f"data/sessions/{sf}"
                shutil.copy2(src, dst)
                phone = sf.replace('.session', '')
                client = TelegramClient(dst, API_ID, API_HASH)
                try:
                    await asyncio.wait_for(client.connect(), timeout=TELETHON_TIMEOUT)
                    if await client.is_user_authorized():
                        me = await client.get_me()
                        phone = me.phone or phone
                except Exception:
                    pass
                finally:
                    await safe_disconnect(client)
                if not phone.startswith('+'):
                    phone = '+' + phone
                if add_account(phone, dst):
                    added += 1

            await update.message.reply_text(
                f"✅ Добавлено {added} аккаунтов из {len(session_files)} сессий!",
                reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
            )
            user_states.pop(user_id, None)
        except Exception as e:
            logger.error(f"ZIP upload error: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}")
        finally:
            for p in [zip_path if 'zip_path' in dir() else None,
                      extract_path if 'extract_path' in dir() else None]:
                if p and os.path.exists(p):
                    try:
                        if os.path.isdir(p): shutil.rmtree(p)
                        else: os.remove(p)
                    except Exception: pass
        return

    # ── Одиночный .session ──
    if not document.file_name.endswith('.session'):
        await update.message.reply_text("❌ Отправь .session или ZIP!")
        return

    await update.message.reply_text("⏳ Загружаю сессию...")
    client = None
    try:
        file         = await document.get_file()
        session_path = f"data/sessions/{document.file_name}"
        await file.download_to_drive(session_path)
        client = TelegramClient(session_path, API_ID, API_HASH)
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TIMEOUT)
        if await client.is_user_authorized():
            me    = await client.get_me()
            phone = me.phone or document.file_name.replace('.session', '')
        else:
            phone = document.file_name.replace('.session', '')
        if not phone.startswith('+'):
            phone = '+' + phone
        add_account(phone, session_path)
        await update.message.reply_text(
            f"✅ Аккаунт {phone} добавлен!",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        user_states.pop(user_id, None)
        logger.info(f"Session uploaded: {phone}")
    except Exception as e:
        logger.error(f"Session upload error: {e}")
        await update.message.reply_text(f"❌ Ошибка: {e}")
    finally:
        if client:
            await safe_disconnect(client)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    text  = update.message.text.strip()
    state = user_states.get(user_id, {})

    # ── РЕФЕРАЛКА: ШАГ 1 — ссылка ──
    if state.get('step') == 'waiting_referral_link':
        if 't.me' not in text:
            await update.message.reply_text("❌ Неверная ссылка! Должен быть t.me")
            return
        accounts = get_accounts()
        if not accounts:
            await update.message.reply_text("❌ Нет активных аккаунтов!")
            user_states.pop(user_id, None)
            return
        # Сохраняем ссылку и просим ввести количество
        user_states[user_id] = {
            'step': 'waiting_referral_count',
            'link': text,
            'total': len(accounts)
        }
        await update.message.reply_text(
            f"🔗 **Ссылка принята!**\n\n"
            f"📱 Доступно аккаунтов: **{len(accounts)}**\n"
            f"Введи количество аккаунтов для перехода (1–{len(accounts)})",
            parse_mode="Markdown"
        )
        return

    # ── РЕФЕРАЛКА: ШАГ 2 — количество ──
    if state.get('step') == 'waiting_referral_count':
        if not text.isdigit():
            await update.message.reply_text("❌ Введи число!")
            return
        count = int(text)
        total = state['total']
        if count < 1 or count > total:
            await update.message.reply_text(f"❌ Число от 1 до {total}!")
            return
        # Берём случайные аккаунты
        accounts = get_accounts()
        random.shuffle(accounts)
        selected = accounts[:count]
        link = state['link']
        if user_id in active_tasks:
            await update.message.reply_text("⚠️ Задача уже запущена, подожди!")
            user_states.pop(user_id, None)
            return
        # Запускаем рефку
        user_states.pop(user_id, None)
        await run_referral(update, user_id, selected, link)
        return

    # ── Реакции: шаг 1 — ссылка ──
    if state.get('step') == 'waiting_reaction_link':
        if 't.me' not in text or not re.search(r't\.me/[\w]+/\d+', text):
            await update.message.reply_text("❌ Неверная ссылка!\nПример: https://t.me/durov/123")
            return
        accounts = get_accounts()
        total    = len(accounts)
        if not total:
            await update.message.reply_text("❌ Нет активных аккаунтов!")
            user_states.pop(user_id, None)
            return
        user_states[user_id] = {'step': 'waiting_reaction_count', 'link': text, 'total': total}
        await update.message.reply_text(
            f"✅ Ссылка принята!\n\n"
            f"📱 Активных аккаунтов: **{total}**\n\n"
            f"Сколько аккаунтов поставят реакции? (1–{total})",
            parse_mode="Markdown"
        )
        return

    # ── Реакции: шаг 2 — количество ──
    if state.get('step') == 'waiting_reaction_count':
        if not text.isdigit():
            await update.message.reply_text("❌ Введи число!")
            return
        count = int(text)
        total = state['total']
        if count < 1 or count > total:
            await update.message.reply_text(f"❌ Число от 1 до {total}!")
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
            f"👍 Реакции: {count} аккаунтов\n"
            f"👁 Просмотры: {view_count} аккаунтов\n\n"
            f"Задай распределение (сумма = **{count}**):\n"
            f"Формат: `🔥3 ❤️2 ⚡2`\n\n"
            f"Доступные: 🔥 ❤️ ⚡ 👍 👎 🎉 🤩 😢 💯 🤮",
            parse_mode="Markdown"
        )
        return

    # ── Реакции: шаг 3 — распределение ──
    if state.get('step') == 'waiting_reaction_distribution':
        reaction_count = state['reaction_count']
        parsed = parse_reaction_input(text)
        if not parsed:
            await update.message.reply_text(
                "❌ Не могу разобрать!\nПример: `🔥3 ❤️2 ⚡2`",
                parse_mode="Markdown"
            )
            return
        total_assigned = sum(c for _, c in parsed)
        if total_assigned != reaction_count:
            await update.message.reply_text(
                f"❌ Сумма {total_assigned} ≠ {reaction_count}!\nИсправь.",
                parse_mode="Markdown"
            )
            return

        accounts = get_accounts()
        random.shuffle(accounts)
        reaction_accs = accounts[:reaction_count]
        view_accs     = accounts[reaction_count:]

        reaction_plan = []
        idx = 0
        for emoji, count in parsed:
            group = [reaction_accs[i][2] for i in range(idx, idx + count)]
            reaction_plan.append((emoji, group))
            idx += count

        view_sessions = [acc[2] for acc in view_accs]

        plan_text  = "📋 **План действий:**\n\n"
        plan_text += f"👁 Просмотры: {len(view_sessions)} аккаунтов\n"
        for emoji, sessions in reaction_plan:
            plan_text += f"{emoji} Реакция: {len(sessions)} аккаунтов\n"
        plan_text += "\n⏱ Задержки: 2–6 сек (просмотры), 3–10 сек (реакции)\n"
        plan_text += "Подтверди запуск — напиши **да** или **нет**"

        user_states[user_id] = {
            'step': 'waiting_reaction_confirm',
            'link': state['link'],
            'reaction_plan': reaction_plan,
            'view_sessions': view_sessions
        }
        await update.message.reply_text(plan_text, parse_mode="Markdown")
        return

    # ── Реакции: шаг 4 — подтверждение ──
    if state.get('step') == 'waiting_reaction_confirm':
        if text.lower() in ('да', 'yes', 'go', 'старт', '+'):
            if user_id in active_tasks:
                await update.message.reply_text("⚠️ Задача уже запущена, подожди!")
                return
            link          = state['link']
            reaction_plan = state['reaction_plan']
            view_sessions = state['view_sessions']
            user_states.pop(user_id, None)
            await run_advanced_reactions(update, user_id, link, reaction_plan, view_sessions)
        else:
            user_states.pop(user_id, None)
            await update.message.reply_text(
                "❌ Отменено.",
                reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
            )
        return

    # ── TApp абуз ──
    if state.get('step') == 'waiting_link':
        if 't.me' not in text or 'startapp' not in text:
            await update.message.reply_text("❌ Неверная ссылка!\nПример: https://t.me/bot/app?startapp=ref")
            return
        accounts = get_accounts()
        if not accounts:
            await update.message.reply_text("❌ Нет активных аккаунтов!")
            user_states.pop(user_id, None)
            return

        if user_id in active_tasks:
            await update.message.reply_text("⚠️ Задача уже запущена, подожди!")
            return

        active_tasks.add(user_id)
        success_count = 0
        error_count   = 0
        report        = []
        for acc in accounts:
            phone        = acc[1]
            session_path = acc[2]
            result = await open_tapp(session_path, text)
            if result['success']:
                success_count += 1
                report.append(f"✅ {phone} — @{result['bot']}")
            else:
                error_count += 1
                report.append(f"❌ {phone} — {result['error'][:40]}")
            await asyncio.sleep(random.uniform(3, 8))

        await update.message.reply_text(
            f"📊 **Отчёт по абузу:**\n\n"
            f"✅ Успешно: {success_count}\n"
            f"❌ Ошибок: {error_count}\n"
            f"📌 Всего: {success_count + error_count}\n\n"
            f"Детали:\n" + "\n".join(report[:10]),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        active_tasks.discard(user_id)
        user_states.pop(user_id, None)
        return

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID:
        await query.answer("Доступ запрещен!", show_alert=True)
        return
    await query.answer()
    data  = query.data
    state = user_states.get(user_id, {})

    # ── Удалить аккаунт ──
    if data.startswith('delete_'):
        acc_id = int(data.split('_')[1])
        db_execute("DELETE FROM accounts WHERE id = ?", (acc_id,))
        logger.info(f"Account deleted: id={acc_id}")
        keyboard = [[InlineKeyboardButton("📋 Назад к списку", callback_data="full_list")]] + BACK_BUTTON
        await query.edit_message_text(f"✅ Аккаунт #{acc_id} удалён!", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # ── Включить/Отключить аккаунт ──
    if data.startswith('toggle_'):
        acc_id = int(data.split('_')[1])
        cursor.execute("SELECT status_info, phone FROM accounts WHERE id = ?", (acc_id,))
        row = cursor.fetchone()
        if row:
            status_info, phone = row
            # Учитываем что статус может быть "✅ Активен" или просто "Активен"
            if "Активен" in status_info:
                new_status = "Отключен"
                icon = "⛔"
            else:
                new_status = "Активен"
                icon = "✅"
            db_execute("UPDATE accounts SET status_info = ? WHERE id = ?", (new_status, acc_id))
            logger.info(f"Account toggled: id={acc_id} → {new_status}")
            keyboard = [[InlineKeyboardButton("📋 Назад к списку", callback_data="full_list")]] + BACK_BUTTON
            await query.edit_message_text(
                f"{icon} Аккаунт `{phone}` — **{new_status}**",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await query.edit_message_text("❌ Аккаунт не найден!")
        return

    # ── Пагинация ──
    if data.startswith('page_'):
        page = int(data.split('_')[1])
        user_states[user_id] = {**state, 'page': page}
        await _show_accounts_page(query, page)
        return

    # ── Статистика ──
    if data == "stats":
        accounts = get_accounts()
        if not accounts:
            await query.edit_message_text(
                "📊 Аккаунтов пока нет",
                reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
            )
            return
        total  = len(accounts)
        active = sum(1 for a in accounts if "Активен" in a[5])
        banned = sum(1 for a in accounts if "Забанен" in a[5])
        error  = sum(1 for a in accounts if "Ошибка"  in a[5])
        off    = sum(1 for a in accounts if "Отключен" in a[5])
        text   = (
            f"📊 **Статистика фермы:**\n\n"
            f"📱 Всего: {total}\n"
            f"🟢 Активны: {active}\n"
            f"⛔ Отключены: {off}\n"
            f"🔴 Забанены: {banned}\n"
            f"⚠️ Ошибок: {error}\n\n"
            f"📋 **Последние 5:**\n"
        )
        for acc in accounts[:5]:
            _, phone, _, _, name, status_info, _ = acc
            text += f"{acc_emoji(status_info)} `{phone}` — {status_info}\n"
        keyboard = [
            [InlineKeyboardButton("📋 Все аккаунты", callback_data="full_list")],
        ] + BACK_BUTTON
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # ── Полный список ──
    if data == "full_list":
        page = state.get('page', 0)
        await _show_accounts_page(query, page)
        return

    # ── Обновить статусы ──
    if data == "refresh":
        accounts = get_accounts()
        if not accounts:
            await query.edit_message_text("❌ Нет аккаунтов!", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
            return
        await query.edit_message_text(f"🔄 Обновляю {len(accounts)} аккаунтов...")
        for acc in accounts:
            acc_id, phone, session_path, *_ = acc
            check = await check_account_status(session_path)
            db_execute('UPDATE accounts SET name = ?, status_info = ? WHERE id = ?',
                       (check['name'], check['status'], acc_id))
        await query.edit_message_text(
            "✅ Статусы обновлены!",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return

    # ── Главное меню ──
    if data == "main_menu":
        user_states.pop(user_id, None)
        await query.edit_message_text("👋 Выбери действие:", reply_markup=InlineKeyboardMarkup(MAIN_KEYBOARD))
        return

    # ── Загрузить сессию ──
    if data == "upload_session":
        await query.edit_message_text(
            "📂 **Загрузка сессии**\n\n"
            "Отправь `.session` файл или ZIP-архив с папкой `sessions/`.",
            parse_mode="Markdown"
        )
        user_states[user_id] = {'step': 'waiting_session_file'}
        return

    # ── Реакции ──
    if data == "reaction":
        accounts = get_accounts()
        total    = len(accounts)
        if not total:
            await query.edit_message_text("❌ Нет активных аккаунтов!", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
            return
        if user_id in active_tasks:
            await query.edit_message_text("⚠️ Сейчас уже идёт задача, подожди!", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
            return
        await query.edit_message_text(
            f"🔥 **Расширенные реакции**\n\n"
            f"📱 Доступно аккаунтов: **{total}**\n\n"
            f"Отправь ссылку на пост:\n`https://t.me/durov/123`",
            parse_mode="Markdown"
        )
        user_states[user_id] = {'step': 'waiting_reaction_link'}
        return

    # ── Абуз TApp ──
    if data == "abuse":
        if user_id in active_tasks:
            await query.edit_message_text("⚠️ Сейчас уже идёт задача, подожди!", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
            return
        await query.edit_message_text(
            "🚀 Отправь ссылку TApp:\n`https://t.me/bot/app?startapp=ref`",
            parse_mode="Markdown"
        )
        user_states[user_id] = {'step': 'waiting_link'}
        return

    # ── Экспорт аккаунтов ──
    if data == "export":
        accounts = get_accounts()
        if not accounts:
            await query.edit_message_text("❌ Нет аккаунтов!", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
            return
        lines = ["Телефон | Статус | Имя | Добавлен"]
        lines.append("─" * 40)
        for acc in accounts:
            _, phone, _, _, name, status_info, created_at = acc
            date = created_at[:10] if created_at else "—"
            lines.append(f"{phone} | {status_info} | {name} | {date}")
        export_path = "data/accounts_export.txt"
        with open(export_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        await query.edit_message_text("📤 Отправляю файл...")
        with open(export_path, "rb") as f:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=f,
                filename="accounts.txt",
                caption=f"📤 Экспорт {len(accounts)} аккаунтов"
            )
        await query.edit_message_text("✅ Готово!", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
        return

# ───────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ПАГИНАЦИИ
# ───────────────────────────────────────────

async def _show_accounts_page(query, page: int):
    accounts  = get_accounts()
    if not accounts:
        await query.edit_message_text("📋 Список пуст", reply_markup=InlineKeyboardMarkup(BACK_BUTTON))
        return
    per_page    = 5
    total_pages = max(1, (len(accounts) + per_page - 1) // per_page)
    page        = max(0, min(page, total_pages - 1))
    start_idx   = page * per_page
    page_accs   = accounts[start_idx:start_idx + per_page]

    text = f"📋 **Аккаунты (стр. {page + 1}/{total_pages}):**\n\n"
    for acc in page_accs:
        acc_id, phone, _, _, name, status_info, created_at = acc
        e    = acc_emoji(status_info)
        date = created_at[:10] if created_at else "—"
        text += f"{e} `{phone}`\n   📛 {name}  |  {status_info}  |  🗓 {date}\n\n"

    keyboard = []
    for acc in page_accs:
        acc_id = acc[0]
        status_info = acc[5]
        toggle_label = "✅ Вкл" if "Отключен" in status_info else "⛔ Откл"
        keyboard.append([
            InlineKeyboardButton(f"🗑️ Удалить #{acc_id}", callback_data=f"delete_{acc_id}"),
            InlineKeyboardButton(f"{toggle_label} #{acc_id}", callback_data=f"toggle_{acc_id}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard += BACK_BUTTON
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# ───────────────────────────────────────────
#  ЗАПУСК
# ───────────────────────────────────────────

if __name__ == "__main__":
    logger.info("🚀 БОТ ЗАПУЩЕН!")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()
