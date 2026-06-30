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
from datetime import datetime, timedelta
from telethon import TelegramClient
from telethon.tl.functions.messages import (
    SendReactionRequest, GetMessagesViewsRequest,
    RequestWebViewRequest, GetBotCallbackAnswerRequest
)
from telethon.tl.types import ReactionEmoji
from urllib.parse import urlparse, parse_qs

# ───────────────────────────────────────────
#  КОНФИГ
# ───────────────────────────────────────────
TOKEN    = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
API_ID   = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")

TELETHON_TIMEOUT = 30

os.makedirs("data", exist_ok=True)
os.makedirs("data/sessions", exist_ok=True)
os.makedirs("data/logs", exist_ok=True)

# ───────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ───────────────────────────────────────────
logger = logging.getLogger("farm_bot")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(console_handler)

file_handler = logging.handlers.RotatingFileHandler(
    "data/logs/bot.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"
)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(file_handler)

# ───────────────────────────────────────────
#  БАЗА ДАННЫХ
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

# ── Таблица аналитики ──
cursor.execute('''
    CREATE TABLE IF NOT EXISTS analytics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER,
        phone TEXT,
        action_type TEXT,
        success INTEGER DEFAULT 1,
        error_msg TEXT DEFAULT NULL,
        target TEXT DEFAULT NULL,
        created_at TEXT
    )
''')

# ── Таблица задач ──
cursor.execute('''
    CREATE TABLE IF NOT EXISTS task_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_type TEXT,
        total_accounts INTEGER,
        success_count INTEGER,
        error_count INTEGER,
        details TEXT DEFAULT NULL,
        created_at TEXT
    )
''')

conn.commit()

def db_execute(query, params=()):
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
#  АНАЛИТИКА — вспомогательные функции
# ───────────────────────────────────────────

def log_action(phone: str, action_type: str, success: bool, error_msg: str = None, target: str = None):
    cursor.execute('SELECT id FROM accounts WHERE phone = ?', (phone,))
    row = cursor.fetchone()
    account_id = row[0] if row else None
    db_execute(
        'INSERT INTO analytics (account_id, phone, action_type, success, error_msg, target, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (account_id, phone, action_type, 1 if success else 0, error_msg, target, datetime.now().isoformat())
    )

def log_task(task_type: str, total: int, success: int, errors: int, details: str = None):
    db_execute(
        'INSERT INTO task_history (task_type, total_accounts, success_count, error_count, details, created_at) VALUES (?, ?, ?, ?, ?, ?)',
        (task_type, total, success, errors, details, datetime.now().isoformat())
    )

def get_analytics_summary(days: int = 7):
    since = (datetime.now() - timedelta(days=days)).isoformat()
    
    cursor.execute('SELECT COUNT(*) FROM analytics WHERE created_at >= ?', (since,))
    total_actions = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM analytics WHERE created_at >= ? AND success = 1', (since,))
    total_success = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM analytics WHERE created_at >= ? AND success = 0', (since,))
    total_errors = cursor.fetchone()[0]

    cursor.execute('''
        SELECT action_type, COUNT(*) as cnt, SUM(success) as ok
        FROM analytics WHERE created_at >= ?
        GROUP BY action_type ORDER BY cnt DESC
    ''', (since,))
    by_type = cursor.fetchall()

    cursor.execute('''
        SELECT phone, COUNT(*) as cnt, SUM(success) as ok
        FROM analytics WHERE created_at >= ?
        GROUP BY phone ORDER BY ok DESC LIMIT 5
    ''', (since,))
    top_accounts = cursor.fetchall()

    cursor.execute('''
        SELECT phone, COUNT(*) as cnt, SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as errors
        FROM analytics WHERE created_at >= ?
        GROUP BY phone HAVING errors > 0 ORDER BY errors DESC LIMIT 5
    ''', (since,))
    problem_accounts = cursor.fetchall()

    cursor.execute('''
        SELECT DATE(created_at) as day, COUNT(*) as cnt, SUM(success) as ok
        FROM analytics WHERE created_at >= ?
        GROUP BY day ORDER BY day DESC LIMIT 7
    ''', (since,))
    daily = cursor.fetchall()

    return {
        'total_actions': total_actions,
        'total_success': total_success,
        'total_errors': total_errors,
        'by_type': by_type,
        'top_accounts': top_accounts,
        'problem_accounts': problem_accounts,
        'daily': daily,
    }

def get_task_history(limit: int = 10):
    cursor.execute('''
        SELECT task_type, total_accounts, success_count, error_count, created_at
        FROM task_history ORDER BY created_at DESC LIMIT ?
    ''', (limit,))
    return cursor.fetchall()

def get_account_analytics(phone: str, days: int = 30):
    since = (datetime.now() - timedelta(days=days)).isoformat()
    cursor.execute('''
        SELECT action_type, COUNT(*) as cnt, SUM(success) as ok
        FROM analytics WHERE phone = ? AND created_at >= ?
        GROUP BY action_type ORDER BY cnt DESC
    ''', (phone, since))
    by_type = cursor.fetchall()

    cursor.execute('''
        SELECT COUNT(*), SUM(success)
        FROM analytics WHERE phone = ? AND created_at >= ?
    ''', (phone, since))
    row = cursor.fetchone()
    total = row[0] or 0
    ok    = row[1] or 0

    cursor.execute('''
        SELECT error_msg, COUNT(*) as cnt
        FROM analytics WHERE phone = ? AND success = 0 AND created_at >= ?
        GROUP BY error_msg ORDER BY cnt DESC LIMIT 3
    ''', (phone, since))
    top_errors = cursor.fetchall()

    return {'total': total, 'ok': ok, 'by_type': by_type, 'top_errors': top_errors}

# ───────────────────────────────────────────
#  СОСТОЯНИЯ И ЗАЩИТА ОТ ДВОЙНОГО ЗАПУСКА
# ───────────────────────────────────────────
user_states  = {}
active_tasks = set()

# ───────────────────────────────────────────
#  ГЛАВНОЕ МЕНЮ
# ───────────────────────────────────────────
MAIN_KEYBOARD = [
    [InlineKeyboardButton("📊 Статистика",        callback_data="stats")],
    [InlineKeyboardButton("🔄 Обновить статусы",  callback_data="refresh")],
    [InlineKeyboardButton("📂 Загрузить сессию",  callback_data="upload_session")],
    [InlineKeyboardButton("🔥 Реакции",           callback_data="reaction")],
    [InlineKeyboardButton("🚀 Абуз TApp",         callback_data="abuse")],
    [InlineKeyboardButton("🔗 Рефка (старт)",     callback_data="refka")],
    [InlineKeyboardButton("📤 Экспорт аккаунтов", callback_data="export")],
    [InlineKeyboardButton("📈 Аналитика",         callback_data="analytics")],
]
BACK_BUTTON = [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]

# ───────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ───────────────────────────────────────────

def parse_reaction_input(text):
    text = text.replace(',', ' ').replace(';', ' ')
    pattern = r'([\U00010000-\U0010ffff][\ufe0f\u20e3]?|[\u2600-\u27BF][\ufe0f]?)\s*(\d+)'
    matches = re.findall(pattern, text)
    if not matches:
        return None
    return [(emoji.strip(), int(count)) for emoji, count in matches]

def acc_emoji(status_info):
    return "🟢" if "Активен" in status_info else "🔴"

async def safe_disconnect(client):
    try:
        await client.disconnect()
    except Exception:
        pass

def parse_refka_link(url: str):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    path_parts = parsed.path.strip("/").split("/")
    username = path_parts[0] if path_parts else ""
    if not username:
        raise ValueError("Не удалось определить username бота.")
    if "startapp" in qs:
        return username, "startapp", qs["startapp"][0]
    elif "start" in qs:
        return username, "start", qs["start"][0]
    else:
        raise ValueError("Не найден параметр ?start= или ?startapp= в ссылке.")

def make_bar(value: int, total: int, length: int = 10) -> str:
    if total == 0:
        filled = 0
    else:
        filled = round((value / total) * length)
    return "█" * filled + "░" * (length - filled)

# ───────────────────────────────────────────
#  TELETHON-ФУНКЦИИ
# ───────────────────────────────────────────

async def check_account_status(session_path):
    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TIMEOUT)
        if not await client.is_user_authorized():
            return {'status': '❌ Забанен', 'name': 'Неизвестно'}
        me = await asyncio.wait_for(client.get_me(), timeout=TELETHON_TIMEOUT)
        name = me.first_name or me.username or 'Без имени'
        logger.info(f"check_status OK: {session_path} -> {name}")
        return {'status': '✅ Активен', 'name': name}
    except asyncio.TimeoutError:
        logger.warning(f"check_status TIMEOUT: {session_path}")
        return {'status': '❌ Ошибка', 'name': 'Таймаут'}
    except Exception as e:
        logger.warning(f"check_status ERROR: {session_path} -> {e}")
        return {'status': '❌ Ошибка', 'name': 'Неизвестно'}
    finally:
        await safe_disconnect(client)

async def view_post(session_path, channel_username, post_id):
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
        logger.info(f"view_post OK: {session_path} -> @{channel_username}/{post_id}")
        return {'success': True}
    except asyncio.TimeoutError:
        return {'success': False, 'error': 'Таймаут'}
    except Exception as e:
        logger.warning(f"view_post ERROR: {session_path} -> {e}")
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
        logger.info(f"set_reaction OK: {session_path} -> {emoji}")
        return {'success': True, 'emoji': emoji}
    except asyncio.TimeoutError:
        return {'success': False, 'error': 'Таймаут'}
    except Exception as e:
        logger.warning(f"set_reaction ERROR: {session_path} -> {e}")
        return {'success': False, 'error': str(e)}
    finally:
        await safe_disconnect(client)

async def open_tapp(session_path, link):
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
        logger.info(f"open_tapp OK: {session_path} -> @{bot_username}")
        return {'success': True, 'url': webview.url, 'bot': bot_username}
    except asyncio.TimeoutError:
        return {'success': False, 'error': 'Таймаут'}
    except Exception as e:
        logger.warning(f"open_tapp ERROR: {session_path} -> {e}")
        return {'success': False, 'error': str(e)}
    finally:
        await safe_disconnect(client)

async def do_refka(session_path, bot_username: str, link_type: str, ref_param: str):
    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TIMEOUT)
        if not await client.is_user_authorized():
            return {'success': False, 'error': 'Сессия не активна'}

        entity = await asyncio.wait_for(
            client.get_entity(f"@{bot_username}"), timeout=TELETHON_TIMEOUT
        )

        if link_type == "start":
            await asyncio.wait_for(
                client.send_message(entity, f"/start {ref_param}"),
                timeout=TELETHON_TIMEOUT
            )
        elif link_type == "startapp":
            await asyncio.wait_for(
                client.invoke(RequestWebViewRequest(
                    peer=entity,
                    bot=entity,
                    platform="android",
                    url=f"https://t.me/{bot_username}/app?startapp={ref_param}",
                    from_background=False,
                )),
                timeout=TELETHON_TIMEOUT
            )

        logger.info(f"[Refka] do_refka OK ({link_type}): {session_path} -> @{bot_username} ref={ref_param}")
        return {'success': True}

    except asyncio.TimeoutError:
        logger.warning(f"[Refka] do_refka TIMEOUT: {session_path}")
        return {'success': False, 'error': 'Таймаут'}
    except Exception as e:
        logger.warning(f"[Refka] do_refka ERROR: {session_path} -> {e}")
        return {'success': False, 'error': str(e)}
    finally:
        await safe_disconnect(client)

async def get_bot_buttons(session_path, bot_username: str):
    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TIMEOUT)
        if not await client.is_user_authorized():
            return []
        entity = await asyncio.wait_for(
            client.get_entity(f"@{bot_username}"), timeout=TELETHON_TIMEOUT
        )
        messages = await asyncio.wait_for(
            client.get_messages(entity, limit=5), timeout=TELETHON_TIMEOUT
        )
        buttons = []
        seen = set()
        for msg in messages:
            if not msg.reply_markup:
                continue
            for row in msg.reply_markup.rows:
                for btn in row.buttons:
                    text = getattr(btn, 'text', '') or ''
                    data = getattr(btn, 'data', None)
                    if text and text not in seen:
                        seen.add(text)
                        buttons.append({'text': text, 'has_callback': data is not None})
            if buttons:
                break
        return buttons
    except Exception as e:
        logger.warning(f"[Refka] get_bot_buttons ERROR: {session_path} -> {e}")
        return []
    finally:
        await safe_disconnect(client)

async def click_button_by_text(session_path, bot_username: str, button_text_fragment: str):
    client = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=TELETHON_TIMEOUT)
        if not await client.is_user_authorized():
            return {'success': False, 'error': 'Сессия не активна'}

        entity = await asyncio.wait_for(
            client.get_entity(f"@{bot_username}"), timeout=TELETHON_TIMEOUT
        )
        messages = await asyncio.wait_for(
            client.get_messages(entity, limit=10), timeout=TELETHON_TIMEOUT
        )

        target_msg = None
        target_btn = None

        for msg in messages:
            if not msg.reply_markup:
                continue
            for row in msg.reply_markup.rows:
                for btn in row.buttons:
                    btn_text = getattr(btn, 'text', '') or ''
                    if button_text_fragment.lower() in btn_text.lower():
                        target_msg = msg
                        target_btn = btn
                        break
                if target_btn:
                    break
            if target_btn:
                break

        if not target_btn:
            return {'success': False, 'error': f'Кнопка с текстом "{button_text_fragment}" не найдена'}

        btn_data = getattr(target_btn, 'data', None)
        if not btn_data:
            return {'success': False, 'error': 'Кнопка не callback (WebApp — нажать нельзя)'}

        await asyncio.wait_for(
            client(GetBotCallbackAnswerRequest(
                peer=entity,
                msg_id=target_msg.id,
                data=btn_data,
            )),
            timeout=TELETHON_TIMEOUT
        )
        logger.info(f"[Refka] click_button OK: {session_path} -> '{target_btn.text}' @{bot_username}")
        return {'success': True, 'button': target_btn.text}

    except asyncio.TimeoutError:
        return {'success': False, 'error': 'Таймаут'}
    except Exception as e:
        logger.warning(f"[Refka] click_button ERROR: {session_path} -> {e}")
        return {'success': False, 'error': str(e)}
    finally:
        await safe_disconnect(client)

# ───────────────────────────────────────────
#  ЛОГИКА РАСШИРЕННЫХ РЕАКЦИЙ (ИЗМЕНЕНА)
# ───────────────────────────────────────────

async def run_advanced_reactions(update, user_id, link, reaction_plan, view_sessions):
    active_tasks.add(user_id)
    match = re.search(r't\.me/([^/]+)/(\d+)', link)
    channel_username = match.group(1)
    post_id = int(match.group(2))

    total_view_accs = len(view_sessions)          # Все аккаунты
    total_reaction_accs = sum(len(accs) for _, accs in reaction_plan)  # Сколько поставят реакции

    logger.info(f"run_advanced_reactions START: user={user_id}, link={link}, "
                f"views={total_view_accs}, reactions={total_reaction_accs}")

    await update.message.reply_text(
        f"🚀 *Запуск задачи:*\n"
        f"👁 **Все {total_view_accs} аккаунтов** заходят на просмотр\n"
        f"🔥 **{total_reaction_accs} аккаунтов** поставят реакции\n"
        f"⏱ Сначала просмотры, потом реакции...",
        parse_mode="Markdown"
    )

    # ── Шаг 1: просмотры (ВСЕ аккаунты) ──
    view_success = 0
    view_errors = 0
    shuffled_viewers = list(view_sessions)
    random.shuffle(shuffled_viewers)

    for session_path in shuffled_viewers:
        cursor.execute('SELECT phone FROM accounts WHERE session_path = ?', (session_path,))
        row = cursor.fetchone()
        phone = row[0] if row else session_path

        result = await view_post(session_path, channel_username, post_id)
        if result['success']:
            view_success += 1
            log_action(phone, 'view', True, target=link)
        else:
            view_errors += 1
            log_action(phone, 'view', False, error_msg=result.get('error'), target=link)
        await asyncio.sleep(random.uniform(2, 6))

    await update.message.reply_text(
        f"👁 *Просмотры готовы:*\n"
        f"✅ Успешно: {view_success}  ❌ Ошибок: {view_errors}\n\n"
        f"⏳ Пауза перед реакциями...",
        parse_mode="Markdown"
    )
    await asyncio.sleep(random.uniform(5, 15))

    # ── Шаг 2: реакции (ТОЛЬКО выбранные аккаунты) ──
    reaction_success = 0
    reaction_errors = 0
    reaction_report = []
    shuffled_plan = list(reaction_plan)
    random.shuffle(shuffled_plan)

    for emoji, sessions in shuffled_plan:
        random.shuffle(sessions)
        for session_path in sessions:
            cursor.execute('SELECT phone FROM accounts WHERE session_path = ?', (session_path,))
            row = cursor.fetchone()
            phone = row[0] if row else session_path

            result = await set_reaction(session_path, link, emoji)
            if result['success']:
                reaction_success += 1
                reaction_report.append(f"✅ {emoji}")
                log_action(phone, 'reaction', True, target=link)
            else:
                reaction_errors += 1
                reaction_report.append(f"❌ {emoji} — {result['error'][:40]}")
                log_action(phone, 'reaction', False, error_msg=result.get('error'), target=link)
            await asyncio.sleep(random.uniform(3, 10))

    # Записываем задачу в историю
    log_task('reactions', total_view_accs,
             view_success + reaction_success, view_errors + reaction_errors,
             details=link)

    report_lines = "\n".join(reaction_report[:15])
    if len(reaction_report) > 15:
        report_lines += f"\n...и ещё {len(reaction_report) - 15}"

    await update.message.reply_text(
        f"📊 *Итоговый отчёт:*\n\n"
        f"👁 Просмотры: ✅{view_success} / ❌{view_errors}\n"
        f"💬 Реакции:   ✅{reaction_success} / ❌{reaction_errors}\n"
        f"📌 Всего аккаунтов задействовано: {total_view_accs}\n\n"
        f"*Детали реакций:*\n{report_lines}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    logger.info(f"run_advanced_reactions DONE: user={user_id}, "
                f"views={view_success}/{view_errors}, reactions={reaction_success}/{reaction_errors}")
    active_tasks.discard(user_id)

# ───────────────────────────────────────────
#  ЛОГИКА РЕФКИ
# ───────────────────────────────────────────

async def _process_refka_accounts(update, user_id, bot_username, link_type, ref_param,
                                   accounts, button_text, skip_first=False):
    active_tasks.add(user_id)
    results = []

    for i, acc in enumerate(accounts):
        phone        = acc[1]
        session_path = acc[2]

        if i == 0 and skip_first:
            result = {'success': True, 'phone': phone, 'btn_clicked': False, 'btn_error': None}
        else:
            result = await do_refka(session_path, bot_username, link_type, ref_param)
            result['phone']       = phone
            result['btn_clicked'] = False
            result['btn_error']   = None

        log_action(phone, 'refka', result['success'],
                   error_msg=result.get('error'), target=f"@{bot_username}")

        if result['success'] and button_text:
            wait = random.uniform(2, 5)
            await asyncio.sleep(wait)
            click = await click_button_by_text(session_path, bot_username, button_text)
            if click['success']:
                result['btn_clicked'] = True
                log_action(phone, 'button_click', True, target=f"@{bot_username}:{button_text}")
                logger.info(f"[Refka] [{i+1}/{len(accounts)}] {phone} -> кнопка '{click['button']}' OK")
            else:
                result['btn_error'] = click['error']
                log_action(phone, 'button_click', False,
                           error_msg=click['error'], target=f"@{bot_username}:{button_text}")
                logger.warning(f"[Refka] [{i+1}/{len(accounts)}] {phone} -> кнопка не нажата: {click['error']}")

        results.append(result)

        if i < len(accounts) - 1:
            await asyncio.sleep(random.uniform(3, 8))

    success_count  = sum(1 for r in results if r['success'])
    error_count    = len(results) - success_count
    btn_click_ok   = sum(1 for r in results if r.get('btn_clicked'))
    btn_click_fail = sum(1 for r in results if button_text and not r.get('btn_clicked') and r['success'])

    log_task('refka', len(accounts), success_count, error_count, details=f"@{bot_username}")

    lines = [f"📊 *Отчёт по рефке* `@{bot_username}`\n",
             f"✅ Переходов успешно: *{success_count}*",
             f"❌ Ошибок перехода: *{error_count}*"]
    if button_text:
        lines.append(f"🖱 Кнопка нажата: *{btn_click_ok}* | не нажата: *{btn_click_fail}*")
    lines.append("━━━━━━━━━━━━━━━━━━━━\n")

    for r in results:
        phone = r['phone']
        if r['success']:
            if button_text:
                if r['btn_clicked']:
                    lines.append(f"✅ `{phone}` — переход + кнопка OK")
                else:
                    err = (r.get('btn_error') or '?')[:35]
                    lines.append(f"⚠️ `{phone}` — переход OK | кнопка: {err}")
            else:
                lines.append(f"✅ `{phone}` — успех")
        else:
            err = (r.get('error') or 'неизвестная ошибка')[:50]
            lines.append(f"❌ `{phone}` — {err}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
    )
    logger.info(f"[Refka] DONE: user={user_id}, success={success_count}, errors={error_count}, btn_ok={btn_click_ok}")
    active_tasks.discard(user_id)


async def run_refka_with_button_detect(update, user_id, bot_username, link_type, ref_param, selected_accounts):
    active_tasks.add(user_id)
    first_acc = selected_accounts[0]

    await update.message.reply_text(
        f"🚀 *Рефка запущена*\n\n"
        f"🤖 Бот: `@{bot_username}`\n"
        f"🔗 Тип: `{link_type}={ref_param}`\n"
        f"👥 Аккаунтов: *{len(selected_accounts)}*\n\n"
        f"⏳ Делаю переход первым аккаунтом и смотрю кнопки...",
        parse_mode="Markdown"
    )

    result = await do_refka(first_acc[2], bot_username, link_type, ref_param)
    if not result['success']:
        log_action(first_acc[1], 'refka', False,
                   error_msg=result.get('error'), target=f"@{bot_username}")
        await update.message.reply_text(
            f"❌ Первый аккаунт не прошёл: {result.get('error', '?')}\n"
            f"Попробуй ещё раз.",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        active_tasks.discard(user_id)
        return

    log_action(first_acc[1], 'refka', True, target=f"@{bot_username}")
    await asyncio.sleep(3)

    buttons = await get_bot_buttons(first_acc[2], bot_username)
    active_tasks.discard(user_id)

    if not buttons:
        user_states[user_id] = {
            'step': 'waiting_refka_button',
            'bot_username': bot_username,
            'link_type': link_type,
            'ref_param': ref_param,
            'selected': selected_accounts,
            'first_done': True,
        }
        await update.message.reply_text(
            "🖱 Кнопок не обнаружено в ответе бота.\n\n"
            "Введи фрагмент текста кнопки вручную\n"
            "или `-` если нажимать не нужно:",
            parse_mode="Markdown"
        )
        return

    btn_lines = []
    for b in buttons:
        icon = "✅" if b['has_callback'] else "🌐 WebApp"
        btn_lines.append(f"{icon} `{b['text']}`")

    user_states[user_id] = {
        'step': 'waiting_refka_button',
        'bot_username': bot_username,
        'link_type': link_type,
        'ref_param': ref_param,
        'selected': selected_accounts,
        'first_done': True,
    }

    await update.message.reply_text(
        f"🖱 *Кнопки в боте @{bot_username}:*\n\n"
        + "\n".join(btn_lines) +
        "\n\n✅ — можно нажать  |  🌐 WebApp — нельзя\n\n"
        "Введи фрагмент текста кнопки для нажатия\n"
        "или `-` чтобы не нажимать:",
        parse_mode="Markdown"
    )

# ───────────────────────────────────────────
#  АНАЛИТИКА — форматирование сообщений
# ───────────────────────────────────────────

def format_analytics(days: int = 7) -> str:
    s = get_analytics_summary(days)

    total   = s['total_actions']
    success = s['total_success']
    errors  = s['total_errors']
    rate    = round((success / total * 100) if total > 0 else 0)

    lines = [
        f"📈 *Аналитика за {days} дней*\n",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📌 Всего действий: *{total}*",
        f"✅ Успешных:  *{success}*  {make_bar(success, total)}",
        f"❌ Ошибок:    *{errors}*  {make_bar(errors, total)}",
        f"📊 Успех:     *{rate}%*\n",
    ]

    if s['by_type']:
        lines.append("🔧 *По типам действий:*")
        type_icons = {
            'reaction': '🔥',
            'view': '👁',
            'refka': '🔗',
            'button_click': '🖱',
            'tapp': '🚀',
        }
        for action_type, cnt, ok in s['by_type']:
            icon = type_icons.get(action_type, '▪️')
            ok   = ok or 0
            err  = cnt - ok
            pct  = round((ok / cnt * 100) if cnt > 0 else 0)
            lines.append(f"  {icon} {action_type}: {cnt} (✅{ok} ❌{err} {pct}%)")
        lines.append("")

    if s['daily']:
        lines.append("📅 *Активность по дням:*")
        for day, cnt, ok in s['daily']:
            ok  = ok or 0
            bar = make_bar(ok, cnt, 8)
            lines.append(f"  `{day}` {bar} {cnt} ({ok}✅)")
        lines.append("")

    if s['top_accounts']:
        lines.append("🏆 *Топ аккаунтов:*")
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, (phone, cnt, ok) in enumerate(s['top_accounts']):
            ok  = ok or 0
            pct = round((ok / cnt * 100) if cnt > 0 else 0)
            medal = medals[i] if i < len(medals) else "▪️"
            lines.append(f"  {medal} `{phone}` — {cnt} действий ({pct}%)")
        lines.append("")

    if s['problem_accounts']:
        lines.append("⚠️ *Проблемные аккаунты:*")
        for phone, cnt, errors in s['problem_accounts']:
            errors = errors or 0
            pct    = round((errors / cnt * 100) if cnt > 0 else 0)
            lines.append(f"  🔴 `{phone}` — {errors} ошибок из {cnt} ({pct}%)")

    return "\n".join(lines)

def format_task_history() -> str:
    tasks = get_task_history(10)
    if not tasks:
        return "📋 История задач пуста."

    task_icons = {
        'reactions': '🔥',
        'refka':     '🔗',
        'tapp':      '🚀',
    }

    lines = ["📋 *История задач (последние 10):*\n", "━━━━━━━━━━━━━━━━━━━━"]
    for task_type, total, success, errors, created_at in tasks:
        icon = task_icons.get(task_type, '▪️')
        date = created_at[:16].replace('T', ' ') if created_at else '—'
        pct  = round((success / total * 100) if total > 0 else 0)
        lines.append(
            f"{icon} *{task_type}* | {date}\n"
            f"   👥 {total} акк → ✅{success} ❌{errors} ({pct}%)\n"
        )
    return "\n".join(lines)

def format_account_analytics(phone: str) -> str:
    data = get_account_analytics(phone, days=30)
    total = data['total']
    ok    = data['ok']
    errors = total - ok
    rate  = round((ok / total * 100) if total > 0 else 0)

    lines = [
        f"👤 *Аналитика аккаунта* `{phone}`\n",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📌 Всего действий за 30 дней: *{total}*",
        f"✅ Успешных: *{ok}* ({rate}%)",
        f"❌ Ошибок:   *{errors}*\n",
    ]

    if data['by_type']:
        lines.append("🔧 *По типам:*")
        for action_type, cnt, ok_cnt in data['by_type']:
            ok_cnt = ok_cnt or 0
            lines.append(f"  ▪️ {action_type}: {cnt} (✅{ok_cnt})")
        lines.append("")

    if data['top_errors']:
        lines.append("🔴 *Частые ошибки:*")
        for err_msg, cnt in data['top_errors']:
            err_short = (err_msg or 'неизвестно')[:50]
            lines.append(f"  ❌ {err_short} — {cnt}x")

    return "\n".join(lines)

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
        zip_path = None
        extract_path = None
        try:
            file         = await document.get_file()
            zip_path     = f"/tmp/{document.file_name}"
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
            for p in [zip_path, extract_path]:
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

    # ══════════════════════════════════════════
    #  АНАЛИТИКА — выбор аккаунта для детальной инфо
    # ══════════════════════════════════════════
    if state.get('step') == 'waiting_analytics_phone':
        user_states.pop(user_id, None)
        msg = format_account_analytics(text)
        await update.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
        )
        return

    # ══════════════════════════════════════════
    #  РЕФКА: шаг 1 — получаем ссылку
    # ══════════════════════════════════════════
    if state.get('step') == 'waiting_refka_link':
        try:
            bot_username, link_type, ref_param = parse_refka_link(text)
        except ValueError as e:
            await update.message.reply_text(
                f"❌ Неверная ссылка: {e}\n\n"
                f"Примеры:\n"
                f"`https://t.me/username?start=ref`\n"
                f"`https://t.me/username/app?startapp=ref`",
                parse_mode="Markdown"
            )
            return

        accounts = get_accounts()
        if not accounts:
            await update.message.reply_text("❌ Нет активных аккаунтов в базе.")
            user_states.pop(user_id, None)
            return

        phones_list = "\n".join(
            f"  {i+1}. `{acc[1]}`" for i, acc in enumerate(accounts)
        )
        user_states[user_id] = {
            'step': 'waiting_refka_count',
            'bot_username': bot_username,
            'link_type': link_type,
            'ref_param': ref_param,
            'accounts': accounts,
        }
        await update.message.reply_text(
            f"✅ Ссылка принята!\n\n"
            f"🤖 Бот: `@{bot_username}`\n"
            f"🔗 Тип: `{link_type}={ref_param}`\n\n"
            f"📋 *Активные аккаунты ({len(accounts)} шт.):*\n"
            f"{phones_list}\n\n"
            f"Введи количество аккаунтов для перехода (1–{len(accounts)}):",
            parse_mode="Markdown"
        )
        return

    # ══════════════════════════════════════════
    #  РЕФКА: шаг 2 — получаем количество
    # ══════════════════════════════════════════
    if state.get('step') == 'waiting_refka_count':
        accounts = state['accounts']
        if not text.isdigit():
            await update.message.reply_text("❌ Введи целое число!")
            return
        count = int(text)
        if not (1 <= count <= len(accounts)):
            await update.message.reply_text(f"❌ Число от 1 до {len(accounts)}!")
            return

        selected = random.sample(accounts, count)
        user_states.pop(user_id, None)

        if user_id in active_tasks:
            await update.message.reply_text("⚠️ Задача уже запущена, подожди завершения!")
            return

        asyncio.create_task(run_refka_with_button_detect(
            update, user_id,
            state['bot_username'],
            state['link_type'],
            state['ref_param'],
            selected,
        ))
        return

    # ══════════════════════════════════════════
    #  РЕФКА: шаг 3 — текст кнопки
    # ══════════════════════════════════════════
    if state.get('step') == 'waiting_refka_button':
        if user_id in active_tasks:
            await update.message.reply_text("⚠️ Задача уже запущена, подожди завершения!")
            return

        button_text = None if text.strip() == '-' else text.strip()
        selected    = state['selected']
        first_done  = state.get('first_done', False)
        user_states.pop(user_id, None)

        asyncio.create_task(_process_refka_accounts(
            update,
            user_id,
            state['bot_username'],
            state['link_type'],
            state['ref_param'],
            selected,
            button_text,
            skip_first=first_done,
        ))
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
            f"📱 Активных аккаунтов: *{total}*\n\n"
            f"Сколько аккаунтов поставят реакции? (0–{total})",
            parse_mode="Markdown"
        )
        return

    # ── Реакции: шаг 2 — количество ──
    if state.get('step') == 'waiting_reaction_count':
        if not text.isdigit():
            await update.message.reply_text("❌ Введи число!")
            return
        total = state['total']
        reaction_count = int(text)
        if reaction_count < 0 or reaction_count > total:
            await update.message.reply_text(f"❌ Число от 0 до {total}!")
            return

        # Все аккаунты идут на просмотр
        user_states[user_id] = {
            'step': 'waiting_reaction_distribution',
            'link': state['link'],
            'reaction_count': reaction_count,
            'view_count': total,  # ← ВСЕ аккаунты идут на просмотр
            'total': total
        }
        await update.message.reply_text(
            f"👁 **Все {total} аккаунтов** зайдут на просмотр поста.\n"
            f"🔥 **{reaction_count} аккаунтов** поставят реакции.\n\n"
            f"Задай распределение реакций (сумма = **{reaction_count}**):\n"
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

        # Все аккаунты идут на просмотр
        view_sessions = [acc[2] for acc in accounts]

        # Аккаунты для реакций (первые reaction_count из перемешанных)
        reaction_accs = accounts[:reaction_count]

        reaction_plan = []
        idx = 0
        for emoji, count in parsed:
            group = [reaction_accs[i][2] for i in range(idx, idx + count)]
            reaction_plan.append((emoji, group))
            idx += count

        plan_text  = "📋 *План действий:*\n\n"
        plan_text += f"👁 **Все {len(view_sessions)} аккаунтов** → просмотр\n"
        for emoji, sessions in reaction_plan:
            plan_text += f"{emoji} Реакция: {len(sessions)} аккаунтов\n"
        plan_text += "\n⏱ Задержки: 2–6 сек (просмотры), 3–10 сек (реакции)\n"
        plan_text += "Подтверди запуск — напиши *да* или *нет*"

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
                log_action(phone, 'tapp', True, target=text)
            else:
                error_count += 1
                report.append(f"❌ {phone} — {result['error'][:40]}")
                log_action(phone, 'tapp', False, error_msg=result.get('error'), target=text)
            await asyncio.sleep(random.uniform(3, 8))

        log_task('tapp', len(accounts), success_count, error_count, details=text)

        await update.message.reply_text(
            f"📊 *Отчёт по абузу:*\n\n"
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
            if "Активен" in status_info:
                new_status = "Отключен"
                icon = "⛔"
            else:
                new_status = "Активен"
                icon = "✅"
            db_execute("UPDATE accounts SET status_info = ? WHERE id = ?", (new_status, acc_id))
            logger.info(f"Account toggled: id={acc_id} -> {new_status}")
            keyboard = [[InlineKeyboardButton("📋 Назад к списку", callback_data="full_list")]] + BACK_BUTTON
            await query.edit_message_text(
                f"{icon} Аккаунт `{phone}` — *{new_status}*",
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

    # ════════════════════════════════════════
    #  АНАЛИТИКА
    # ════════════════════════════════════════
    if data == "analytics":
        keyboard = [
            [InlineKeyboardButton("📅 За 7 дней",  callback_data="analytics_7")],
            [InlineKeyboardButton("📅 За 30 дней", callback_data="analytics_30")],
            [InlineKeyboardButton("📋 История задач", callback_data="analytics_tasks")],
            [InlineKeyboardButton("👤 По аккаунту", callback_data="analytics_account")],
        ] + BACK_BUTTON
        await query.edit_message_text(
            "📈 *Аналитика*\n\nВыбери раздел:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "analytics_7":
        msg = format_analytics(7)
        await query.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Назад", callback_data="analytics")]] + BACK_BUTTON
            )
        )
        return

    if data == "analytics_30":
        msg = format_analytics(30)
        await query.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Назад", callback_data="analytics")]] + BACK_BUTTON
            )
        )
        return

    if data == "analytics_tasks":
        msg = format_task_history()
        await query.edit_message_text(
            msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Назад", callback_data="analytics")]] + BACK_BUTTON
            )
        )
        return

    if data == "analytics_account":
        accounts = get_accounts()
        if not accounts:
            await query.edit_message_text(
                "❌ Нет аккаунтов!",
                reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
            )
            return
        phones_list = "\n".join(f"  `{acc[1]}`" for acc in accounts[:20])
        user_states[user_id] = {'step': 'waiting_analytics_phone'}
        await query.edit_message_text(
            f"👤 *Аналитика по аккаунту*\n\n"
            f"Аккаунты:\n{phones_list}\n\n"
            f"Введи номер телефона (с +):",
            parse_mode="Markdown"
        )
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
        active = sum(1 for a in accounts if "Активен"  in a[5])
        banned = sum(1 for a in accounts if "Забанен"  in a[5])
        error  = sum(1 for a in accounts if "Ошибка"   in a[5])
        off    = sum(1 for a in accounts if "Отключен" in a[5])

        cursor.execute('SELECT COUNT(*), SUM(success) FROM analytics')
        row = cursor.fetchone()
        all_actions = row[0] or 0
        all_ok      = row[1] or 0

        text = (
            f"📊 *Статистика фермы:*\n\n"
            f"📱 Всего: {total}\n"
            f"🟢 Активны: {active}\n"
            f"⛔ Отключены: {off}\n"
            f"🔴 Забанены: {banned}\n"
            f"⚠️ Ошибок: {error}\n\n"
            f"📈 *Всего действий:* {all_actions} (✅{all_ok} ❌{all_actions - all_ok})\n\n"
            f"📋 *Последние 5:*\n"
        )
        for acc in accounts[:5]:
            _, phone, _, _, name, status_info, _ = acc
            text += f"{acc_emoji(status_info)} `{phone}` — {status_info}\n"
        keyboard = [
            [InlineKeyboardButton("📋 Все аккаунты", callback_data="full_list")],
            [InlineKeyboardButton("📈 Подробная аналитика", callback_data="analytics")],
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
            "📂 *Загрузка сессии*\n\n"
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
            f"🔥 *Расширенные реакции*\n\n"
            f"📱 Доступно аккаунтов: *{total}*\n\n"
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

    # ── Рефка ──
    if data == "refka":
        if user_id in active_tasks:
            await query.edit_message_text(
                "⚠️ Задача уже запущена, подожди завершения!",
                reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
            )
            return
        accounts = get_accounts()
        if not accounts:
            await query.edit_message_text(
                "❌ Нет активных аккаунтов в базе!",
                reply_markup=InlineKeyboardMarkup(BACK_BUTTON)
            )
            return
        await query.edit_message_text(
            "🔗 *Рефка (старт)*\n\n"
            "Отправь ссылку на Telegram-бота:\n\n"
            "`https://t.me/username?start=ref`\n"
            "или\n"
            "`https://t.me/username/app?startapp=ref`",
            parse_mode="Markdown"
        )
        user_states[user_id] = {'step': 'waiting_refka_link'}
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

    text = f"📋 *Аккаунты (стр. {page + 1}/{total_pages}):*\n\n"
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
