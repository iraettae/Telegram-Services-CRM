import asyncio
import logging
import os
import time
import sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, BusinessConnection
from dotenv import load_dotenv

# --- НОВЫЕ ИМПОРТЫ ДЛЯ ВЕБ-СЕРВЕРА ---
import uvicorn
import urllib.parse
import hmac
import hashlib
import html
import json
import uuid
from fastapi import FastAPI, Request, Depends, HTTPException, File, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from openai import AsyncOpenAI, APIError, APITimeoutError, AuthenticationError

# Загружаем переменные окружения из файла .env
load_dotenv()

# Слой доступа к данным вынесен в db.py. Ре-экспортируем имена, чтобы существующие
# `from main import ...` (в т.ч. в ai_handler.py) продолжали работать без правок.
from db import (
    DB_FILE, connect_db, offload, LEAD_STAGES,
    normalize_phone, compute_lead_score,
    upsert_lead, mark_lead_exported, lead_export_blocked,
    open_kb_gap, answer_kb_gap,
    get_topic, save_topic, get_lead_by_topic,
    save_account, save_chat_and_message, fetch_chat_history_rows,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MASTER_GROUP_ID = os.getenv("MASTER_GROUP_ID")

if not BOT_TOKEN or not MASTER_GROUP_ID:
    raise ValueError("Убедитесь, что BOT_TOKEN и MASTER_GROUP_ID заданы в файле .env")

# Парсим список разрешенных ID/Юзернеймов операторов (через запятую)
OPERATORS_ENV = os.getenv("OPERATORS", "")
OPERATORS = []
for x in OPERATORS_ENV.split(","):
    x = x.strip()
    if x.isdigit():
        OPERATORS.append(int(x))
    elif x.startswith("@"):
        OPERATORS.append(x.lower())

try:
    MASTER_GROUP_ID = int(MASTER_GROUP_ID)
except ValueError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Инициализируем бота и диспетчер
from aiogram.client.session.aiohttp import AiohttpSession
PROXY_URL = os.getenv("PROXY_URL", "socks5://127.0.0.1:40000")
session = AiohttpSession(proxy=PROXY_URL)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
app = FastAPI(title="CRM Telegram TMA API") # Наш веб-фреймворк

# --- DEBUG: логируем ВСЕ incoming updates ---
from aiogram.types import Update

@dp.update.outer_middleware
async def log_all_updates(handler, event: Update, data):
    update_types = [k for k in event.model_fields_set if k != "update_id"]
    logger.warning(f"[RAW_UPDATE] id={event.update_id} types={update_types}")
    return await handler(event, data)

# Секрет для валидации Telegram webhook (X-Telegram-Bot-Api-Secret-Token).
# Если не задан явно — детерминированно выводим из BOT_TOKEN, чтобы set_webhook и
# проверка совпадали без ручной настройки.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(
    (BOT_TOKEN + ":webhook").encode()
).hexdigest()[:48]

# TTL валидности Telegram initData (сек). Перехваченную строку нельзя
# переигрывать бессрочно. По умолчанию 24 часа.
INIT_DATA_TTL = int(os.getenv("INIT_DATA_TTL", "86400"))

# Лимит размера загружаемого файла (совпадает с client_max_body_size в nginx).
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))


# Создаем папку для загруженных аватарок
os.makedirs("frontend/avatars", exist_ok=True)
os.makedirs("frontend/media", exist_ok=True)

def init_db():
    """Создает базу данных и таблицы при первом запуске."""
    conn = connect_db()
    c = conn.cursor()
    # WAL персистится в заголовке файла БД — включаем один раз при инициализации.
    # Резко снижает взаимные блокировки читателей/писателей ('database is locked').
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    # Старая таблица (для форума)
    c.execute('''
        CREATE TABLE IF NOT EXISTS topics (
            business_connection_id TEXT,
            chat_id INTEGER,
            message_thread_id INTEGER,
            PRIMARY KEY (business_connection_id, chat_id)
        )
    ''')
    
    # --- НОВЫЕ ТАБЛИЦЫ ДЛЯ CRM (TMA) ---
    c.execute('''
        CREATE TABLE IF NOT EXISTS accounts (
            business_connection_id TEXT PRIMARY KEY,
            business_name TEXT,
            user_id INTEGER
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            business_connection_id TEXT,
            lead_name TEXT,
            last_message_time DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            text TEXT,
            is_outgoing BOOLEAN,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # -------------------------------------
    
    # СХЕМА ОБНОВЛЕНИЯ: добавляем поддержку медиа для старых баз
    try:
        c.execute('ALTER TABLE messages ADD COLUMN media_type TEXT')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE messages ADD COLUMN media_url TEXT')
    except sqlite3.OperationalError:
        pass
        
    # --- СХЕМА ОБНОВЛЕНИЯ ИИ ---
    c.execute('''
        CREATE TABLE IF NOT EXISTS ai_settings (
            id INTEGER PRIMARY KEY DEFAULT 1,
            api_key TEXT,
            system_prompt TEXT,
            knowledge_base TEXT
        )
    ''')
    try:
        c.execute('ALTER TABLE accounts ADD COLUMN ai_enabled BOOLEAN DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE chats ADD COLUMN ai_paused BOOLEAN DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    # КРИТИЧНО: эти колонки использовались в коде, но НЕ создавались нигде —
    # на чистой БД первое же сообщение падало с 'no such column: is_unread'.
    try:
        c.execute('ALTER TABLE chats ADD COLUMN is_unread BOOLEAN DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE chats ADD COLUMN is_high_priority BOOLEAN DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE ai_settings ADD COLUMN read_delay INTEGER DEFAULT 2')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE ai_settings ADD COLUMN typing_delay INTEGER DEFAULT 2')
    except sqlite3.OperationalError:
        pass

    # Инициализируем настройки ИИ дефолтными значениями, если их нет
    c.execute('SELECT COUNT(*) FROM ai_settings')
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO ai_settings (id, system_prompt, knowledge_base) VALUES (1, 'Ты — вежливый AI-рекрутер.', 'Ответы на частые вопросы...')")

    # Создаем таблицу операторов
    c.execute('''
        CREATE TABLE IF NOT EXISTS operators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity TEXT UNIQUE
        )
    ''')
    
    try:
        c.execute('ALTER TABLE operators ADD COLUMN is_super BOOLEAN DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # Инициализация операторов из .env (выполняется только 1 раз, при старте если пусто)
    c.execute('SELECT COUNT(*) FROM operators')
    if c.fetchone()[0] == 0:
        for op in OPERATORS:
            c.execute('INSERT OR IGNORE INTO operators (identity) VALUES (?)', (str(op),))

    # --- ТАБЛИЦА ЛИДОВ (Фаза 1: фундамент воронки) ---
    # chat_id PRIMARY KEY = естественная дедупликация (один лид = одна строка),
    # чтобы повторный [LEAD_READY] не создавал двойной счёт за одного курьера.
    c.execute('''
        CREATE TABLE IF NOT EXISTS leads (
            chat_id INTEGER PRIMARY KEY,
            business_connection_id TEXT,
            phone TEXT,
            full_name TEXT,
            dob TEXT,
            citizenship TEXT,
            transport TEXT,
            source_account TEXT,
            stage TEXT DEFAULT 'ready',
            assigned_operator TEXT,
            outcome TEXT,
            drop_reason TEXT,
            exported INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            stage_changed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Доп. колонки лида: скоринг, дедуп/анти-фрод (идемпотентные миграции)
    for _col in (
        'score INTEGER DEFAULT 0',
        'source_confidence INTEGER',        # проброс уверенности парсера (на будущее)
        'phone_normalized TEXT',            # нормализованный телефон для дедупа
        'is_duplicate INTEGER DEFAULT 0',
        'fraud_flags TEXT',                 # CSV-список сработавших флагов
    ):
        try:
            c.execute(f'ALTER TABLE leads ADD COLUMN {_col}')
        except sqlite3.OperationalError:
            pass

    # Follow-up молчащим лидам: счётчик и время последней реактивации
    for _col in ('followups_sent INTEGER DEFAULT 0', 'last_followup_at DATETIME'):
        try:
            c.execute(f'ALTER TABLE chats ADD COLUMN {_col}')
        except sqlite3.OperationalError:
            pass

    # Самообучающаяся база знаний: незакрытые вопросы из эскалаций
    c.execute('''
        CREATE TABLE IF NOT EXISTS kb_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            thread_id INTEGER,
            question TEXT,
            operator_answer TEXT,
            status TEXT DEFAULT 'open',   -- open -> answered -> approved / dismissed
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()
    logger.info("База данных инициализирована.")


# Слой данных (leads/kb/chats/topics/scoring/dedup) вынесен в db.py
# и импортирован выше. Функции ниже (bot/веб) остаются здесь.


async def update_avatar_if_needed(chat_id: int):
    avatar_path = f"frontend/avatars/{chat_id}.jpg"
    if not os.path.exists(avatar_path):
        try:
            photos = await bot.get_user_profile_photos(chat_id, limit=1)
            if photos.total_count > 0:
                file_id = photos.photos[0][0].file_id
                file = await bot.get_file(file_id)
                await bot.download_file(file.file_path, avatar_path)
        except Exception as e:
            logger.warning(f"Не удалось загрузить аватар для chat_id {chat_id}: {e}")

async def download_media_if_present(message: Message):
    media_type = None
    media_url = None
    file_id = None
    ext = ""
    
    if message.photo:
        media_type = "photo"
        file_id = message.photo[-1].file_id
        ext = ".jpg"
    elif message.video:
        media_type = "video"
        file_id = message.video.file_id
        ext = ".mp4"
    elif message.video_note:
        media_type = "video_note"
        file_id = message.video_note.file_id
        ext = ".mp4"
    elif message.voice:
        media_type = "voice"
        file_id = message.voice.file_id
        ext = ".ogg"
    elif message.audio:
        media_type = "audio"
        file_id = message.audio.file_id
        ext = ".mp3"
    elif message.document:
        media_type = "document"
        file_id = message.document.file_id
        if message.document.file_name and "." in message.document.file_name:
            ext = "." + message.document.file_name.split('.')[-1]
        else:
            ext = ".dat"
            
    if file_id:
        try:
            filename = f"{uuid.uuid4().hex}{ext}"
            filepath = os.path.join("frontend/media", filename)
            file = await bot.get_file(file_id)
            await bot.download_file(file.file_path, filepath)
            media_url = f"/static/media/{filename}"
        except Exception as e:
            logger.error(f"Error downloading media: {e}")
            media_type = None
            media_url = None
            
    return media_type, media_url


# ----------------- AI RECRUITER LOGIC (DeepSeek-V3 via OnlySq) -----------------

from ai_handler import process_ai_reply_new as process_ai_reply

@dp.message(CommandStart())
async def cmd_start(message: Message):
    """ОБРАБОТЧИК КОМАНДЫ /start"""
    user_id = message.from_user.id
    
    # Если список OPERATORS задан, и пользователя там нет — блокируем доступ
    if OPERATORS and user_id not in OPERATORS:
        logger.warning(f"Заблокирован доступ к /start для {user_id}")
        await message.answer("⛔️ У вас нет доступа к этой CRM-системе.")
        return
        
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть CRM", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )
    await message.answer(
        "👋 Привет! Я ваш CRM-помощник.\n\n"
        "С помощью меня вы можете управлять диалогами ваших лидов прямо из Telegram.\n"
        "Нажмите на кнопку ниже, чтобы открыть панель управления (Mini App).",
        reply_markup=markup
    )

@dp.business_connection()
async def handle_business_connection(event: BusinessConnection):
    """ОБРАБОТЧИК ПОДКЛЮЧЕНИЯ/ОТКЛЮЧЕНИЯ БИЗНЕС-АККАУНТА"""
    user = event.user
    business_name = f"{user.first_name} {user.last_name or ''}".strip()
    conn_id = event.id
    
    conn = connect_db()
    c = conn.cursor()
    
    if event.is_enabled:
        # Аккаунт подключён — сохраняем/обновляем с новым business_connection_id
        # Сначала ищем старый bc_id для этого user_id
        c.execute('SELECT business_connection_id FROM accounts WHERE user_id = ?', (user.id,))
        old_rows = c.fetchall()
        old_bc_ids = [r[0] for r in old_rows]
        
        # Сохраняем ai_enabled от старого аккаунта
        ai_enabled = 0
        for old_id in old_bc_ids:
            c.execute('SELECT ai_enabled FROM accounts WHERE business_connection_id = ?', (old_id,))
            row = c.fetchone()
            if row and row[0]:
                ai_enabled = row[0]
                break
        
        # Вставляем новый аккаунт
        c.execute('''
            INSERT OR REPLACE INTO accounts (business_connection_id, business_name, user_id, ai_enabled) 
            VALUES (?, ?, ?, ?)
        ''', (conn_id, business_name, user.id, ai_enabled))
        
        # Мигрируем чаты и топики на новый bc_id
        for old_id in old_bc_ids:
            if old_id != conn_id:
                c.execute('UPDATE chats SET business_connection_id = ? WHERE business_connection_id = ?', (conn_id, old_id))
                # Для topics: сначала удаляем дубли (если уже есть запись с новым bc_id для того же chat_id)
                c.execute('''
                    DELETE FROM topics WHERE business_connection_id = ? 
                    AND chat_id IN (SELECT chat_id FROM topics WHERE business_connection_id = ?)
                ''', (old_id, conn_id))
                c.execute('UPDATE OR IGNORE topics SET business_connection_id = ? WHERE business_connection_id = ?', (conn_id, old_id))
        
        logger.info(f"[BIZ_CONN] Аккаунт подключён: {business_name} (uid={user.id}) new_bc_id={conn_id}")
    else:
        # Аккаунт отключён
        logger.info(f"[BIZ_CONN] Аккаунт отключён: {business_name} (uid={user.id}) bc_id={conn_id}")
    
    conn.commit()
    conn.close()

@dp.business_message()
async def handle_business_message(message: Message):
    """ОБРАБОТЧИК ДЛЯ ВХОДЯЩИХ (И ИСХОДЯЩИХ ЧЕРЕЗ БИЗНЕС) СООБЩЕНИЙ"""
    conn_id = message.business_connection_id
    chat_id = message.chat.id
    
    # --- НОВЫЙ КОД: Сохраняем аккаунт, чат и сообщение ---
    conn = connect_db()
    c = conn.cursor()
    try:
        conn_info = await bot.get_business_connection(conn_id)
        user = conn_info.user
        business_name = f"{user.first_name} {user.last_name or ''}".strip()
        
        # Обновляем аккаунт или добавляем новый, не трогая ai_enabled
        c.execute('SELECT ai_enabled FROM accounts WHERE business_connection_id = ?', (conn_id,))
        acc_row = c.fetchone()
        ai_enabled = acc_row[0] if acc_row else 0
        
        c.execute('''
            INSERT OR REPLACE INTO accounts (business_connection_id, business_name, user_id, ai_enabled) 
            VALUES (?, ?, ?, ?)
        ''', (conn_id, business_name, user.id, ai_enabled))
        conn.commit()
        # Скачиваем аватар рабочего аккаунта
        asyncio.create_task(update_avatar_if_needed(user.id))
    except Exception as e:
        logger.warning(f"Не удалось получить бизнес-инфо для {conn_id}: {e}")
        business_name = f"ID: {conn_id[:6]}..."
        
    lead_name = message.chat.full_name or f"Пользователь {chat_id}"
    
    # Сохраняем сообщение в базу
    # Aiogram 3.x может ловить и исходящие сообщения из бизнес аккаунта, 
    # проверяем кто автор
    is_outgoing = message.from_user.id != chat_id
    
    # ОБРАБОТКА ИИ-ПЕРЕХВАТА И HUMAN OVERRIDE
    if is_outgoing:
        # Админ ответил вручную -> глушим ИИ
        c.execute('UPDATE chats SET ai_paused = 1 WHERE chat_id = ?', (chat_id,))
        conn.commit()
    
    # Проверяем статус ИИ
    c.execute('SELECT ai_enabled FROM accounts WHERE business_connection_id = ?', (conn_id,))
    acc_row = c.fetchone()
    account_ai_enabled = bool(acc_row[0]) if acc_row else False
    
    c.execute('SELECT ai_paused FROM chats WHERE chat_id = ?', (chat_id,))
    chat_row = c.fetchone()
    chat_ai_paused = bool(chat_row[0]) if chat_row else False
    
    conn.close()
    
    media_type, media_url = await download_media_if_present(message)
    
    save_chat_and_message(
        chat_id=chat_id,
        business_connection_id=conn_id,
        lead_name=lead_name,
        text=message.text or message.caption or "",
        is_outgoing=is_outgoing,
        media_type=media_type,
        media_url=media_url
    )
    asyncio.create_task(update_avatar_if_needed(chat_id))
    # -----------------------------------------------------

    # 1. Проверяем по базе данных, существует ли уже топик для этого лида
    thread_id = get_topic(conn_id, chat_id)
    
    # 2. Если топика нет — создаем новую тему в мастер-группе
    if not thread_id:
        topic_name = f"{lead_name} | {business_name}"
        try:
            topic = await bot.create_forum_topic(chat_id=MASTER_GROUP_ID, name=topic_name[:128])
            thread_id = topic.message_thread_id
            save_topic(conn_id, chat_id, thread_id)
            
            await bot.send_message(
                chat_id=MASTER_GROUP_ID,
                message_thread_id=thread_id,
                text=f"🆕 <b>Новый чат создан!</b>\n\n"
                     f"👤 <b>Лид:</b> {html.escape(lead_name or '')}\n"
                     f"💼 <b>Рабочий аккаунт:</b> {html.escape(business_name or '')}\n"
                     f"🔗 <b>ID чата лида:</b> <code>{chat_id}</code>\n\n"
                     f"<i>💡 Чтобы ответить лиду с рабочего аккаунта, просто напишите сообщение в эту тему.</i>",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Ошибка при создании темы форума: {e}")
            return
            
    # ИИ Автоответчик (только для входящих)
    logger.info(f"[ROUTER] is_outgoing: {is_outgoing}, account_ai_enabled: {account_ai_enabled}, chat_ai_paused: {chat_ai_paused}, conn_id: {conn_id}")
    if not is_outgoing and account_ai_enabled and not chat_ai_paused:
        user_msg = message.text or message.caption or ""
        logger.info(f"[ROUTER] Сообщение для ИИ: '{user_msg}'")
        if user_msg:
            logger.info(f"[ROUTER] Запускаем ИИ процесс для: {chat_id}")
            asyncio.create_task(process_ai_reply(chat_id, conn_id, user_msg, lead_name, thread_id, message.message_id))
        else:
            logger.info(f"[ROUTER] Пустое текстовое сообщение, ИИ пропущен")
    else:
        logger.info(f"[ROUTER] ИИ пропущен (условие не выполнено)")

    # Если это сообщение от нас (мы написали с рабочего аккаунта напрямую), 
    # то не обязательно дублировать его как входящее в топике.
    if is_outgoing:
        return
            
    # 3. Бот отправляет текст сообщения (вместе с медиа) в этот топик
    try:
        await message.send_copy(
            chat_id=MASTER_GROUP_ID,
            message_thread_id=thread_id
        )
    except Exception as e:
        logger.error(f"Ошибка при пересылке сообщения лида в топик: {e}")


@dp.message(F.chat.id == MASTER_GROUP_ID)
async def handle_operator_reply(message: Message):
    """ОБРАБОТЧИК ДЛЯ ОТВЕТОВ ИЗ МАСТЕР-ГРУППЫ"""
    thread_id = message.message_thread_id
    if not thread_id:
        return 
        
    lead_info = get_lead_by_topic(thread_id)
    if not lead_info:
        return 
        
    conn_id, lead_chat_id = lead_info
    
    if message.content_type in [
        ContentType.FORUM_TOPIC_CREATED, ContentType.FORUM_TOPIC_CLOSED, 
        ContentType.FORUM_TOPIC_REOPENED, ContentType.FORUM_TOPIC_EDITED
    ]:
        return
        
    # Бот отправляет это сообщение лиду через Business API
    try:
        await message.send_copy(
            chat_id=lead_chat_id,
            business_connection_id=conn_id
        )
        # --- НОВЫЙ КОД: Сохраняем это сообщение как исходящее в базу
        media_type, media_url = await download_media_if_present(message)
        op_text = message.text or message.caption or ""
        save_chat_and_message(
            chat_id=lead_chat_id,
            business_connection_id=conn_id,
            lead_name=f"User {lead_chat_id}", # fallback
            text=op_text,
            is_outgoing=True,
            media_type=media_type,
            media_url=media_url
        )
        # Оператор ответил вручную через топик → человек взял диалог на себя.
        # Ставим ai_paused=1 (как и для прямого ответа с аккаунта), иначе AI и
        # follow-up продолжат писать поверх оператора.
        conn_p = connect_db()
        conn_p.execute('UPDATE chats SET ai_paused = 1 WHERE chat_id = ?', (lead_chat_id,))
        conn_p.commit()
        conn_p.close()

        # Если у чата был открытый пробел в базе знаний (эскалация) — ответ
        # оператора становится кандидатом на добавление в KB (самообучение).
        if op_text.strip():
            answer_kb_gap(lead_chat_id, op_text)
        # ---------------------------------------------------------
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа оператора лиду: {e}")
        await message.reply(f"❌ <b>Ошибка отправки сообщения лиду:</b>\n<code>{e}</code>", parse_mode="HTML")


# ==============================================================================
# КОНЕЦ СТАРОГО КОДА (AIOGRAM BOT) / НАЧАЛО НОВОГО КОДА (FASTAPI ВЕБ-СЕРВЕР)
# ==============================================================================

# --- WEBHOOK ENDPOINT для Telegram ---
WEBHOOK_PATH = "/tg-webhook"
# Домен берём из env (PUBLIC_DOMAIN=crmsystem.yourdomain.com), чтобы не забыть
# поправить хардкод при переносе. example.com оставлен только как явный маркер.
PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN", "crmsystem.example.com")
WEBHOOK_URL = f"https://{PUBLIC_DOMAIN}{WEBHOOK_PATH}"
WEBAPP_URL = os.getenv("WEBAPP_URL", f"https://{PUBLIC_DOMAIN}/")


# Монтируем директорию со статикой для фронтенда


# =====================================================================
# TEST AI CHAT — тестирование ИИ без Telegram
# =====================================================================


async def delayed_send_to_telegram(chat_id: int, text: str, business_conn_id: str, thread_id: str = None):
    """Имитирует процесс печати перед отправкой сообщения"""
    try:
        # Расчет времени: 100 символов = 10 секунд (строгая пропорция)
        typing_time = len(text) * 0.10
        
        # Запускаем фоновую задачу для постоянной отправки action="typing"
        async def keep_typing_local():
            end_time = time.time() + typing_time
            while time.time() < end_time:
                try:
                    await bot.send_chat_action(chat_id=chat_id, action="typing", business_connection_id=business_conn_id)
                except Exception as e:
                    pass
                await asyncio.sleep(3.0)
                
        logger.info(f"Начинаем печать для чата {chat_id}, текст {len(text)} симв., время {typing_time}с")
        typing_task = asyncio.create_task(keep_typing_local())
        
        # Ждем реальное время, которое "человек" бы потратил на набор текста
        await asyncio.sleep(typing_time)
        typing_task.cancel()

        # Наконец, отправляем само сообщение
        logger.info(f"Отправляем сообщение в {chat_id}")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                business_connection_id=business_conn_id
            )
        except Exception as e:
            # Откат: удаляем ИМЕННО последнюю несохранённую строку по id
            # (портируемый подзапрос вместо DELETE ... ORDER BY ... LIMIT,
            # который не поддерживается частью сборок SQLite), с гарантией close.
            conn = connect_db()
            try:
                c = conn.cursor()
                c.execute(
                    "DELETE FROM messages WHERE id = ("
                    "SELECT id FROM messages WHERE chat_id = ? AND text = ? "
                    "ORDER BY id DESC LIMIT 1)",
                    (chat_id, text),
                )
                conn.commit()
            finally:
                conn.close()
            raise e
        
        # ОПЦИОНАЛЬНО: дублируем в Мастер-группу
        if thread_id and MASTER_GROUP_ID:
            try:
                await bot.send_message(
                    chat_id=MASTER_GROUP_ID,
                    message_thread_id=thread_id,
                    text=f"[CRM Web]: {text}"
                )
            except:
                pass
    except Exception as e:
        logger.error(f"Error in delayed_send_to_telegram: {e}")


# ----------------- AI SETTINGS ENDPOINTS -----------------


# ----------------- OPERATORS ENDPOINTS -----------------


# ----------------- LEADS ENDPOINTS (Фаза 1: воронка) -----------------


# ----------------- KNOWLEDGE BASE (самообучение) -----------------


# ── FastAPI веб-слой вынесен в webapi.py; регистрируем роуты на app ──
import webapi
from types import SimpleNamespace
webapi.register_routes(app, SimpleNamespace(
    bot=bot, dp=dp, BOT_TOKEN=BOT_TOKEN, MASTER_GROUP_ID=MASTER_GROUP_ID,
    WEBHOOK_SECRET=WEBHOOK_SECRET, WEBHOOK_PATH=WEBHOOK_PATH, WEBAPP_URL=WEBAPP_URL,
    INIT_DATA_TTL=INIT_DATA_TTL, MAX_UPLOAD_BYTES=MAX_UPLOAD_BYTES,
    delayed_send_to_telegram=delayed_send_to_telegram,
    update_avatar_if_needed=update_avatar_if_needed,
    download_media_if_present=download_media_if_present,
))


# ==============================================================================
# ОБЩИЙ ЗАПУСК
# ==============================================================================

async def followup_loop():
    """Фоновая реактивация молчащих лидов: если последнее сообщение — наше и
    прошло больше FOLLOWUP_HOURS, а лид не ответил, AI шлёт мягкий follow-up.
    Лимит попыток на чат — FOLLOWUP_MAX. Выключается FOLLOWUP_HOURS<=0."""
    hours = float(os.getenv("FOLLOWUP_HOURS", "24"))
    max_followups = int(os.getenv("FOLLOWUP_MAX", "2"))
    interval = int(os.getenv("FOLLOWUP_CHECK_MINUTES", "30")) * 60
    if hours <= 0:
        logger.info("Follow-up выключен (FOLLOWUP_HOURS<=0)")
        return
    from ai_handler import send_followup
    logger.info(f"Follow-up включён: каждые {interval//60} мин, порог {hours}ч, макс {max_followups}/чат")
    while True:
        try:
            await asyncio.sleep(interval)
            conn = connect_db()
            c = conn.cursor()
            c.execute('''
                SELECT c.chat_id, c.business_connection_id, c.lead_name
                FROM chats c
                JOIN accounts a ON c.business_connection_id = a.business_connection_id
                LEFT JOIN leads le ON le.chat_id = c.chat_id
                WHERE COALESCE(c.ai_paused, 0) = 0
                  AND COALESCE(a.ai_enabled, 0) = 1
                  AND COALESCE(c.followups_sent, 0) < ?
                  -- не тревожим уже закрытых/оформленных лидов
                  AND (le.stage IS NULL OR le.stage NOT IN ('paid', 'lost', 'on_line', 'onboarding'))
                  AND (SELECT m.is_outgoing FROM messages m WHERE m.chat_id = c.chat_id ORDER BY m.id DESC LIMIT 1) = 1
                  AND (SELECT m.timestamp FROM messages m WHERE m.chat_id = c.chat_id ORDER BY m.id DESC LIMIT 1) < datetime('now', ?)
            ''', (max_followups, f'-{hours} hours'))
            candidates = c.fetchall()
            conn.close()
            if candidates:
                logger.info(f"Follow-up: {len(candidates)} молчащих лидов к реактивации")
            for chat_id, conn_id, lead_name in candidates:
                try:
                    # Перепроверяем состояние в момент отправки: за время прохода
                    # (по 8с на кандидата) лид мог ответить или оператор — поставить паузу.
                    cchk = connect_db(); ck = cchk.cursor()
                    ck.execute('''SELECT COALESCE(ai_paused,0),
                                    (SELECT m.is_outgoing FROM messages m WHERE m.chat_id = ? ORDER BY m.id DESC LIMIT 1)
                                  FROM chats WHERE chat_id = ?''', (chat_id, chat_id))
                    st = ck.fetchone(); cchk.close()
                    if not st or st[0] or st[1] != 1:
                        continue  # чат встал на паузу или лид ответил — не тревожим
                    await send_followup(chat_id, conn_id, lead_name or f"Лид {chat_id}")
                    c2 = connect_db()
                    c2.execute(
                        "UPDATE chats SET followups_sent = COALESCE(followups_sent,0)+1, "
                        "last_followup_at = CURRENT_TIMESTAMP WHERE chat_id = ?", (chat_id,))
                    c2.commit(); c2.close()
                    await asyncio.sleep(8)  # разносим отправки во времени
                except Exception as e:
                    logger.error(f"Follow-up для {chat_id} упал: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"followup_loop ошибка: {e}", exc_info=True)


async def main():
    # Инициализация БД
    init_db()
    
    logger.info("Запуск Telegram CRM Бота (Aiogram + FastAPI + WEBHOOK)...")
    
    ALLOWED_UPDATES = ["message", "business_message", "business_connection", "edited_business_message"]
    
    # 1. Устанавливаем webhook (вместо polling)
    async def setup_webhook():
        for attempt in range(10):
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                await bot.set_webhook(
                    url=WEBHOOK_URL,
                    allowed_updates=ALLOWED_UPDATES,
                    drop_pending_updates=False,
                    secret_token=WEBHOOK_SECRET
                )
                info = await bot.get_webhook_info()
                logger.info(f"Webhook установлен: {info.url}, allowed_updates={info.allowed_updates}")
                return True
            except Exception as e:
                wait = min(5 * (attempt + 1), 30)
                logger.warning(f"Не удалось установить webhook (попытка {attempt+1}/10): {e}. Повтор через {wait}с...")
                await asyncio.sleep(wait)
        logger.error("Не удалось установить webhook после 10 попыток.")
        return False
    
    asyncio.create_task(setup_webhook())

    # Фоновая реактивация молчащих лидов
    asyncio.create_task(followup_loop())

    # 2. Настраиваем и запускаем FastAPI (Uvicorn) — стартует СРАЗУ
    # host=0.0.0.0 обязателен в Docker: 127.0.0.1 внутри контейнера недостижим
    # снаружи (изоляцию порта делаем публикацией на loopback ХОСТА в compose).
    config = uvicorn.Config(app, host=os.getenv("HOST", "0.0.0.0"), port=8000, log_level="info")
    server = uvicorn.Server(config)
    
    await server.serve()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
