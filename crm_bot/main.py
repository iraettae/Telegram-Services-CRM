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

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    """Принимает обновления от Telegram через webhook"""
    # Проверяем секретный токен — без него любой мог слать поддельные апдейты.
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        logger.warning("[WEBHOOK] Отклонён запрос с неверным secret-token")
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
        # Логируем все входящие updates
        update_types = [k for k in update.model_fields_set if k != "update_id"]
        logger.warning(f"[WEBHOOK_UPDATE] id={update.update_id} types={update_types}")
        await dp.feed_update(bot=bot, update=update)
    except Exception as e:
        logger.error(f"[WEBHOOK_ERROR] {e}", exc_info=True)
    # ВСЕГДА возвращаем 200 OK, иначе Telegram будет ретраить и заблокирует очередь
    return JSONResponse({"ok": True})

# Монтируем директорию со статикой для фронтенда
app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/")
async def get_index():
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content, status_code=200)

@app.get("/health")
async def health_check():
    """Liveness/readiness: проверяем доступность БД. Используется healthcheck'ом Docker."""
    try:
        conn = connect_db()
        conn.execute("SELECT 1")
        conn.close()
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)

@app.on_event("shutdown")
async def _drain_on_shutdown():
    """При остановке (SIGTERM от docker) даём in-flight debounce-задачам AI
    до 5 секунд договорить, чтобы не терять ответы лидам при каждом деплое."""
    try:
        from ai_handler import chat_tasks
        pending = [t for t in list(chat_tasks.values()) if not t.done()]
        if pending:
            logger.info(f"Shutdown: ждём {len(pending)} debounce-задач AI (до 5с)...")
            await asyncio.wait(pending, timeout=5)
    except Exception as e:
        logger.warning(f"Ошибка при дренаже задач на shutdown: {e}")

def verify_init_data(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("tma "):
        logger.warning(f"Ошибка валидации initData: Отсутствует заголовок или префикс. Заголовок: '{auth_header}'")
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    init_data = auth_header[4:]
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        hash_val = parsed_data.pop("hash", None)
        if not hash_val:
            raise ValueError("No hash")
            
        # Сортируем ключи и создаем data_check_string
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated_hash, hash_val):
            raise ValueError("Invalid hash")

        # Проверяем свежесть initData: перехваченную строку нельзя переигрывать вечно.
        try:
            auth_date = int(parsed_data.get("auth_date", "0"))
        except ValueError:
            auth_date = 0
        if not auth_date or (time.time() - auth_date) > INIT_DATA_TTL:
            raise HTTPException(status_code=401, detail="Unauthorized: initData expired")

        # Проверяем доступ (по ID или Username)
        user_str = parsed_data.get("user", "{}")
        user_obj = json.loads(user_str)
        user_id = user_obj.get("id")
        username = ("@" + user_obj.get("username", "")).lower()
        
        conn = connect_db()
        c = conn.cursor()
        c.execute('SELECT identity FROM operators')
        db_operators = [row[0] for row in c.fetchall()]
        conn.close()
        
        # Fail-closed: пустой список операторов = запретить всё
        # (раньше пустая таблица открывала доступ любому пользователю Telegram).
        if not db_operators:
            raise HTTPException(status_code=403, detail="Forbidden: no operators configured")
        if str(user_id) not in db_operators and username not in db_operators:
            raise HTTPException(status_code=403, detail="Forbidden: User not in DB operators")

        return user_obj
        
    except ValueError as ve:
        logger.warning(f"Ошибка валидации initData (ValueError): {ve}")
        raise HTTPException(status_code=401, detail=f"Unauthorized: {ve}")
    except Exception as e:
        # Не логируем полную initData — это фактически ключ доступа.
        logger.warning(f"Ошибка валидации initData: {e}")
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/api/accounts", dependencies=[Depends(verify_init_data)])
async def api_get_accounts():
    """Получить список подключенных рабочих аккаунтов"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('''
        SELECT business_connection_id, business_name, user_id, ai_enabled 
        FROM accounts 
        WHERE rowid IN (SELECT MAX(rowid) FROM accounts GROUP BY user_id)
    ''')
    rows = c.fetchall()
    conn.close()
    return JSONResponse([{"id": r[0], "name": r[1], "user_id": r[2], "ai_enabled": bool(r[3])} for r in rows])

@app.post("/api/refresh_avatars", dependencies=[Depends(verify_init_data)])
async def api_refresh_avatars():
    """Скачать недостающие аватарки для всех чатов и аккаунтов"""
    conn = connect_db()
    c = conn.cursor()
    
    # Собираем все user_id которым нужны аватары
    ids_to_check = set()
    
    c.execute('SELECT chat_id FROM chats')
    for row in c.fetchall():
        ids_to_check.add(row[0])
    
    c.execute('SELECT DISTINCT user_id FROM accounts')
    for row in c.fetchall():
        ids_to_check.add(row[0])
    
    conn.close()
    
    # Фильтруем — только те, у кого нет файла
    missing = [uid for uid in ids_to_check if not os.path.exists(f"frontend/avatars/{uid}.jpg")]
    
    # Скачиваем последовательно в ОДНОМ фоновом таске (не спамим сеть)
    async def _download_sequentially():
        for uid in missing:
            await update_avatar_if_needed(uid)
            await asyncio.sleep(1.0)  # пауза между запросами
    
    if missing:
        asyncio.create_task(_download_sequentially())
    
    return JSONResponse({"status": "ok", "queued": len(missing), "total": len(ids_to_check)})

# =====================================================================
# TEST AI CHAT — тестирование ИИ без Telegram
# =====================================================================
import uuid as uuid_module

test_chats = {}  # {chat_id: {"name": str, "messages": [{role, content}], "created_at": str}}

@app.get("/api/test_ai/chats", dependencies=[Depends(verify_init_data)])
async def api_test_ai_list():
    """Список тест-чатов"""
    result = []
    for cid, chat in test_chats.items():
        last_msg = chat["messages"][-1]["content"][:50] if chat["messages"] else ""
        result.append({
            "id": cid,
            "name": chat["name"],
            "message_count": len(chat["messages"]),
            "last_message": last_msg,
            "created_at": chat["created_at"]
        })
    return JSONResponse(result)

@app.post("/api/test_ai/chats", dependencies=[Depends(verify_init_data)])
async def api_test_ai_create(request: Request):
    """Создать новый тест-чат. ИИ автоматически пишет первое сообщение (как рабочий акк)."""
    body = await request.json()
    chat_id = str(uuid_module.uuid4())[:8]
    name = body.get("name", f"Тест #{len(test_chats) + 1}")
    from datetime import datetime
    test_chats[chat_id] = {
        "name": name,
        "messages": [],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    
    # Первое сообщение: либо ручное от пользователя, либо ИИ генерирует
    custom_first_msg = body.get("first_message", "").strip()
    
    if custom_first_msg:
        # Пользователь сам написал первое сообщение
        test_chats[chat_id]["messages"].append({"role": "assistant", "content": custom_first_msg})
    else:
        # ИИ пишет первым — как рабочий аккаунт пишет лиду
        conn = connect_db()
        c = conn.cursor()
        c.execute('SELECT api_key, system_prompt, knowledge_base FROM ai_settings WHERE id = 1')
        ai_config = c.fetchone()
        conn.close()
        
        db_api_key = ai_config[0] if ai_config and ai_config[0] else ""
        system_prompt = ai_config[1] if ai_config else ""
        knowledge_base = ai_config[2] if ai_config else ""
        
        from ai_handler import get_ai_reply
        
        first_msg = await get_ai_reply(
            db_api_key, system_prompt, knowledge_base,
            chat_id=0,
            user_message="Напиши первое сообщение лиду. Ты начинаешь диалог — лид ещё ничего не писал.",
            lead_name="Лид",
            custom_history=[]
        )
        
        if first_msg:
            clean_msg = first_msg.replace("[SILENCE]", "").replace("[LEAD_READY]", "").strip()
            if clean_msg:
                test_chats[chat_id]["messages"].append({"role": "assistant", "content": clean_msg})
    
    return JSONResponse({
        "id": chat_id,
        "name": name,
        "first_message": test_chats[chat_id]["messages"][0]["content"] if test_chats[chat_id]["messages"] else ""
    })

@app.delete("/api/test_ai/chats/{chat_id}", dependencies=[Depends(verify_init_data)])
async def api_test_ai_delete(chat_id: str):
    """Удалить тест-чат"""
    if chat_id in test_chats:
        del test_chats[chat_id]
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "error", "message": "Чат не найден"}, status_code=404)

@app.get("/api/test_ai/chats/{chat_id}/messages", dependencies=[Depends(verify_init_data)])
async def api_test_ai_get_messages(chat_id: str):
    """Получить сообщения тест-чата"""
    if chat_id not in test_chats:
        return JSONResponse({"status": "error", "message": "Чат не найден"}, status_code=404)
    return JSONResponse({"messages": test_chats[chat_id]["messages"]})

@app.post("/api/test_ai/chats/{chat_id}/message", dependencies=[Depends(verify_init_data)])
async def api_test_ai_message(chat_id: str, request: Request):
    """Отправить сообщение. Если no_ai=true — только сохранить, без вызова ИИ."""
    if chat_id not in test_chats:
        return JSONResponse({"status": "error", "message": "Чат не найден"}, status_code=404)
    
    body = await request.json()
    user_message = body.get("message", "").strip()
    no_ai = body.get("no_ai", False)
    
    if not user_message:
        return JSONResponse({"status": "error", "message": "Пустое сообщение"}, status_code=400)
    
    chat = test_chats[chat_id]
    
    # Добавляем сообщение пользователя в историю
    chat["messages"].append({"role": "user", "content": user_message})
    
    # Если no_ai — просто сохраняем, AI не вызываем
    if no_ai:
        return JSONResponse({"status": "ok", "no_ai": True})
    
    # Вызываем AI (полная логика)
    return await _trigger_test_ai(chat_id, chat)

@app.post("/api/test_ai/chats/{chat_id}/trigger_ai", dependencies=[Depends(verify_init_data)])
async def api_test_ai_trigger(chat_id: str):
    """Триггер AI ответа по накопленной истории (вызывается после дебаунса)."""
    if chat_id not in test_chats:
        return JSONResponse({"status": "error", "message": "Чат не найден"}, status_code=404)
    
    chat = test_chats[chat_id]
    return await _trigger_test_ai(chat_id, chat)

async def _trigger_test_ai(chat_id: str, chat: dict):
    """Общая логика: вызвать AI и обработать теги."""
    # Загружаем AI настройки из БД
    conn = connect_db()
    c = conn.cursor()
    c.execute('SELECT api_key, system_prompt, knowledge_base FROM ai_settings WHERE id = 1')
    ai_config = c.fetchone()
    conn.close()
    
    db_api_key = ai_config[0] if ai_config and ai_config[0] else ""
    system_prompt = ai_config[1] if ai_config else ""
    knowledge_base = ai_config[2] if ai_config else ""
    
    # Вызываем get_ai_reply с in-memory историей
    from ai_handler import get_ai_reply, extract_escalation
    import re as re_module
    
    # Берём последнее user-сообщение из истории 
    last_user_msg = ""
    for m in reversed(chat["messages"]):
        if m.get("role") == "user":
            last_user_msg = m["content"]
            break
    
    raw_reply = await get_ai_reply(
        db_api_key, system_prompt, knowledge_base,
        chat_id=0, user_message=last_user_msg,
        lead_name="Тестер",
        custom_history=chat["messages"][:-1]  # без последнего (user_message добавится внутри)
    )
    
    if not raw_reply:
        return JSONResponse({"status": "error", "message": "ИИ не вернул ответ"}, status_code=500)
    
    # Анализируем теги НО не вырезаем — показываем в UI
    tags = []
    
    # Проверяем ESCALATE
    clean_text, escalation_reason = extract_escalation(raw_reply)
    if escalation_reason:
        tags.append({"type": "escalate", "reason": escalation_reason})
    
    # Проверяем LEAD_READY
    lead_data = {}
    if "[LEAD_READY]" in (clean_text or raw_reply):
        import json as json_module
        try:
            src = clean_text if clean_text else raw_reply
            idx = src.index("[LEAD_READY]")
            json_part = src[idx + len("[LEAD_READY]"):].strip()
            if json_part.startswith("{"):
                brace_count = 0
                end_idx = 0
                for i, ch in enumerate(json_part):
                    if ch == '{': brace_count += 1
                    elif ch == '}': brace_count -= 1
                    if brace_count == 0:
                        end_idx = i + 1
                        break
                if end_idx > 0:
                    lead_data = json_module.loads(json_part[:end_idx])
            clean_text = src[:idx].strip()
        except:
            pass
        if lead_data:
            tags.append({"type": "lead_ready", "data": lead_data})
        else:
            tags.append({"type": "lead_ready", "data": {}})
    
    # Проверяем SILENCE
    is_silence = False
    display_text = clean_text or ""
    if "[SILENCE]" in display_text:
        display_text = display_text.replace("[SILENCE]", "").strip()
        is_silence = True
        tags.append({"type": "silence"})
    
    # Сохраняем ответ ИИ в историю (чистый текст для контекста)
    # Разбиваем по ||| чтобы каждый чанк стал отдельным сообщением (как в реальном Telegram)
    if display_text:
        import re as re_split
        chunks = [c.strip() for c in re_split.split(r'\|\|\||\n{2,}', display_text) if c.strip()]
        for chunk in chunks:
            chat["messages"].append({"role": "assistant", "content": chunk})
    elif is_silence:
        chat["messages"].append({"role": "assistant", "content": "[SILENCE]"})
    
    return JSONResponse({
        "status": "ok",
        "raw": raw_reply,
        "display": display_text,
        "tags": tags,
        "is_silence": is_silence
    })


@app.get("/api/chats", dependencies=[Depends(verify_init_data)])
async def api_get_chats():
    """Получить список всех чатов (лидов)"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('''
        SELECT c.chat_id, c.business_connection_id, c.lead_name, c.last_message_time, c.is_unread, c.is_high_priority,
               (SELECT text FROM messages m WHERE m.chat_id = c.chat_id ORDER BY m.timestamp DESC LIMIT 1) as last_msg,
               (SELECT media_type FROM messages m WHERE m.chat_id = c.chat_id ORDER BY m.timestamp DESC LIMIT 1) as last_media,
               (SELECT COUNT(*) 
                FROM messages m 
                WHERE m.chat_id = c.chat_id 
                AND m.is_outgoing = 0 
                AND m.timestamp > COALESCE((SELECT MAX(timestamp) FROM messages m2 WHERE m2.chat_id = c.chat_id AND m2.is_outgoing = 1), '1970-01-01')
               ) as unread_count,
               a.business_name,
               c.ai_paused,
               l.score
        FROM chats c
        LEFT JOIN accounts a ON c.business_connection_id = a.business_connection_id
        LEFT JOIN leads l ON c.chat_id = l.chat_id
        ORDER BY c.is_high_priority DESC,
                 (CASE WHEN COALESCE(l.score,0) >= 60 THEN 1 ELSE 0 END) DESC,
                 c.last_message_time DESC
    ''')
    rows = c.fetchall()
    conn.close()
    
    chats = []
    for r in rows:
        # Если чат принудительно прочитан (is_unread=0), счетчик 0.
        # Если не прочитан (is_unread=1), показываем реальное кол-во (или минимум 1).
        is_unread_flag = bool(r[4])
        raw_unread_count = r[8]
        final_unread_count = raw_unread_count if is_unread_flag else 0
        if is_unread_flag and final_unread_count == 0:
            final_unread_count = 1

        chats.append({
            "chat_id": r[0],
            "business_connection_id": r[1],
            "lead_name": r[2],
            "last_message_time": r[3],
            "is_unread": is_unread_flag,
            "unread_count": final_unread_count,
            "is_high_priority": bool(r[5]),
            "last_message_text": r[6] if r[6] else (f"[{r[7]}]" if r[7] else ""),
            "business_name": r[9] if r[9] else "Неизвестный аккаунт",
            "ai_paused": bool(r[10]),
            "score": r[11] or 0
        })
    return JSONResponse(chats)

@app.get("/api/messages/{chat_id}", dependencies=[Depends(verify_init_data)])
async def api_get_messages(chat_id: int):
    """Получить историю сообщений для конкретного чата"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('SELECT id, text, is_outgoing, timestamp, media_type, media_url FROM messages WHERE chat_id = ? ORDER BY timestamp ASC', (chat_id,))
    rows = c.fetchall()
    conn.close()
    
    msgs = [{"id": r[0], "text": r[1], "is_outgoing": bool(r[2]), "timestamp": r[3], "media_type": r[4], "media_url": r[5]} for r in rows]
    return JSONResponse(msgs)

class SendMessageRequest(BaseModel):
    business_connection_id: str
    text: str

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

@app.post("/api/send/{chat_id}", dependencies=[Depends(verify_init_data)])
async def api_send_message(chat_id: int, req: SendMessageRequest):
    """Отправить сообщение лиду прямо из веб-интерфейса"""
    try:
        # Пытаемся получить свежий business_connection_id для этого аккаунта
        conn = connect_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM accounts WHERE business_connection_id = ?", (req.business_connection_id,))
        acc_row = c.fetchone()
        
        active_conn_id = req.business_connection_id
        if acc_row:
            user_id = acc_row[0]
            c.execute("SELECT business_connection_id FROM accounts WHERE user_id = ? ORDER BY rowid DESC LIMIT 1", (user_id,))
            latest_row = c.fetchone()
            if latest_row:
                active_conn_id = latest_row[0]
                
        # СНАЧАЛА Сохраняем в БД как исходящее, чтобы оно появилось в интерфейсе мгновенно
        c.execute("SELECT lead_name FROM chats WHERE chat_id = ?", (chat_id,))
        row = c.fetchone()
        current_name = row[0] if row else f"Пользователь {chat_id}"
        conn.close()
        
        save_chat_and_message(
            chat_id=chat_id,
            business_connection_id=active_conn_id,
            lead_name=current_name,
            text=req.text,
            is_outgoing=True
        )
        
        asyncio.create_task(update_avatar_if_needed(chat_id))
        
        # ЗАПУСК ФОНОВОЙ ОТПРАВКИ В ТЕЛЕГРАМ С ИМИТАЦИЕЙ НАБОРА ТЕКСТА
        thread_id = get_topic(active_conn_id, chat_id)
        asyncio.create_task(delayed_send_to_telegram(chat_id, req.text, active_conn_id, thread_id))

        return JSONResponse({"status": "ok"})
    except Exception as e:
        logger.error(f"Ошибка API при отправке: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

@app.post("/api/upload/{chat_id}", dependencies=[Depends(verify_init_data)])
async def api_upload_media(chat_id: int, business_connection_id: str = Form(...), file: UploadFile = File(...)):
    """Отправить медиафайл лиду"""
    try:
        conn = connect_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM accounts WHERE business_connection_id = ?", (business_connection_id,))
        acc_row = c.fetchone()
        
        active_conn_id = business_connection_id
        if acc_row:
            user_id = acc_row[0]
            c.execute("SELECT business_connection_id FROM accounts WHERE user_id = ? ORDER BY rowid DESC LIMIT 1", (user_id,))
            latest_row = c.fetchone()
            if latest_row:
                active_conn_id = latest_row[0]

        ext = ""
        filename_orig = file.filename or ""
        if "." in filename_orig:
            ext = "." + filename_orig.split('.')[-1]
        else:
            ext = ".dat"
            
        filename = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join("frontend/media", filename)
        
        content = await file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            conn.close()
            return JSONResponse({"status": "error", "message": "Файл слишком большой (макс 50 МБ)"}, status_code=413)
        with open(filepath, "wb") as f_out:
            f_out.write(content)
            
        media_url = f"/static/media/{filename}"
        mime = file.content_type or ""
        fs_file = FSInputFile(filepath)
        
        media_type = "document"
        if mime.startswith("image/"):
            media_type = "photo"
            await bot.send_photo(chat_id=chat_id, photo=fs_file, business_connection_id=active_conn_id)
        elif mime.startswith("video/"):
            media_type = "video"
            await bot.send_video(chat_id=chat_id, video=fs_file, business_connection_id=active_conn_id)
        elif mime.startswith("audio/") or "ogg" in mime or ext.lower() == ".ogg":
            if "ogg" in mime or ext.lower() == ".ogg":
                media_type = "voice"
                await bot.send_voice(chat_id=chat_id, voice=fs_file, business_connection_id=active_conn_id)
            else:
                media_type = "audio"
                await bot.send_audio(chat_id=chat_id, audio=fs_file, business_connection_id=active_conn_id)
        else:
            media_type = "document"
            await bot.send_document(chat_id=chat_id, document=fs_file, business_connection_id=active_conn_id)

        c.execute("SELECT lead_name FROM chats WHERE chat_id = ?", (chat_id,))
        row = c.fetchone()
        current_name = row[0] if row else f"Пользователь {chat_id}"
        conn.close()
        
        save_chat_and_message(
            chat_id=chat_id,
            business_connection_id=active_conn_id,
            lead_name=current_name,
            text="",
            is_outgoing=True,
            media_type=media_type,
            media_url=media_url
        )
        asyncio.create_task(update_avatar_if_needed(chat_id))
        
        return JSONResponse({"status": "ok", "media_url": media_url, "media_type": media_type})
    except Exception as e:
        logger.error(f"Ошибка API при загрузке файла: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@app.post("/api/chats/{chat_id}/read", dependencies=[Depends(verify_init_data)])
async def api_read_chat(chat_id: int):
    """Отметить чат как прочитанный"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('UPDATE chats SET is_unread = 0 WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok"})

@app.post("/api/chats/{chat_id}/unread", dependencies=[Depends(verify_init_data)])
async def api_unread_chat(chat_id: int):
    """Сбросить статус прочтения (Невидимка) - пометить чат как непрочитанный"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('UPDATE chats SET is_unread = 1 WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok"})

@app.post("/api/chats/{chat_id}/priority", dependencies=[Depends(verify_init_data)])
async def api_priority_chat(chat_id: int):
    """Переключить метку High Priority для чата"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('UPDATE chats SET is_high_priority = NOT is_high_priority WHERE chat_id = ?', (chat_id,))
    c.execute('SELECT is_high_priority FROM chats WHERE chat_id = ?', (chat_id,))
    val = c.fetchone()[0]
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok", "is_high_priority": bool(val)})


# ----------------- AI SETTINGS ENDPOINTS -----------------
@app.post("/api/accounts/{account_id}/toggle_ai", dependencies=[Depends(verify_init_data)])
async def api_toggle_account_ai(account_id: str):
    """Включить/выключить ИИ для конкретного аккаунта"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('UPDATE accounts SET ai_enabled = NOT ai_enabled WHERE business_connection_id = ?', (account_id,))
    c.execute('SELECT ai_enabled FROM accounts WHERE business_connection_id = ?', (account_id,))
    row = c.fetchone()
    if row is None:
        conn.close()
        return JSONResponse({"status": "error", "message": "Аккаунт не найден"}, status_code=404)
    val = row[0]
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok", "ai_enabled": bool(val)})

@app.post("/api/chats/{chat_id}/resume_ai", dependencies=[Depends(verify_init_data)])
async def api_resume_chat_ai(chat_id: int):
    """Снять чат с паузы ИИ (разрешить ИИ снова отвечать)"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('UPDATE chats SET ai_paused = 0 WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok"})

@app.post("/api/chats/{chat_id}/force_ai", dependencies=[Depends(verify_init_data)])
async def api_force_chat_ai(chat_id: int):
    """Принудительно заставить ИИ ответить в диалоге"""
    conn = connect_db()
    c = conn.cursor()
    # Снимем чат с паузы, если он был
    c.execute('UPDATE chats SET ai_paused = 0 WHERE chat_id = ?', (chat_id,))
    
    # Чтобы бот ответил, нужно знать business_connection_id и lead_name из чата
    c.execute('SELECT business_connection_id, lead_name FROM chats WHERE chat_id = ?', (chat_id,))
    chat_row = c.fetchone()
    
    if not chat_row:
        conn.close()
        return JSONResponse({"status": "error", "message": "Чат не найден"}, status_code=404)
        
    conn_id, lead_name = chat_row
    conn.close()
    
    # Вызываем логику ИИ асинхронно
    from ai_handler import process_ai_reply_new
    # user_message передаем как "" чтобы бот просто прочел историю и сгенерил ответ
    asyncio.create_task(process_ai_reply_new(chat_id, conn_id, "", lead_name))
    
    return JSONResponse({"status": "ok"})

@app.post("/api/chats/{chat_id}/generate_ai", dependencies=[Depends(verify_init_data)])
async def api_generate_ai_text(chat_id: int):
    """Сгенерировать текст ИИ и вернуть его (без отправки в Telegram)"""
    conn = connect_db()
    c = conn.cursor()
    
    c.execute('SELECT business_connection_id, lead_name FROM chats WHERE chat_id = ?', (chat_id,))
    chat_row = c.fetchone()
    if not chat_row:
        conn.close()
        return JSONResponse({"status": "error", "message": "Чат не найден"}, status_code=404)
    
    conn_id, lead_name = chat_row
    
    # Загружаем настройки ИИ
    c.execute('SELECT api_key, system_prompt, knowledge_base FROM ai_settings WHERE id = 1')
    ai_config = c.fetchone()
    conn.close()
    
    db_api_key = ai_config[0] if ai_config and ai_config[0] else ""
    system_prompt = ai_config[1] if ai_config else ""
    knowledge_base = ai_config[2] if ai_config else ""
    
    # Получаем имя бизнес-аккаунта
    conn2 = connect_db()
    c2 = conn2.cursor()
    c2.execute("SELECT business_name FROM accounts WHERE business_connection_id = ?", (conn_id,))
    biz_row = c2.fetchone()
    biz_name = biz_row[0] if biz_row else ""
    conn2.close()
    
    from ai_handler import get_ai_reply
    reply_text = await get_ai_reply(db_api_key, system_prompt, knowledge_base, chat_id, "", lead_name, biz_name)
    
    if reply_text:
        # Убираем [LEAD_READY] тег и ||| разделители для чистого текста
        import re
        reply_text = reply_text.replace("[LEAD_READY]", "").strip()
        reply_text = re.sub(r'\|\|\|', '\n', reply_text).strip()
        return JSONResponse({"status": "ok", "text": reply_text})
    else:
        return JSONResponse({"status": "error", "message": "ИИ не смог сгенерировать ответ"}, status_code=500)

@app.post("/api/chats/{chat_id}/toggle_ai", dependencies=[Depends(verify_init_data)])
async def api_toggle_chat_ai(chat_id: int):
    """(Пользовательское) Включить/выключить ИИ для конкретного чата (переключает ai_paused)"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('UPDATE chats SET ai_paused = CASE WHEN ai_paused = 1 THEN 0 ELSE 1 END WHERE chat_id = ?', (chat_id,))
    c.execute('SELECT ai_paused FROM chats WHERE chat_id = ?', (chat_id,))
    val = c.fetchone()[0]
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok", "ai_paused": bool(val)})

@app.get("/api/ai_settings", dependencies=[Depends(verify_init_data)])
async def api_get_ai_settings():
    """Получить глобальные настройки ИИ"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('SELECT api_key, system_prompt, knowledge_base, read_delay, typing_delay FROM ai_settings WHERE id = 1')
    row = c.fetchone()
    conn.close()
    
    if not row:
        return JSONResponse({"api_key": "", "system_prompt": "", "knowledge_base": "", "read_delay": 2, "typing_delay": 2})
    return JSONResponse({
        "api_key": row[0] or "",
        "system_prompt": row[1] or "",
        "knowledge_base": row[2] or "",
        "read_delay": row[3] if row[3] is not None else 2,
        "typing_delay": row[4] if row[4] is not None else 2
    })

class AISettingsRequest(BaseModel):
    api_key: str
    system_prompt: str
    knowledge_base: str
    read_delay: int = 2
    typing_delay: int = 2

@app.post("/api/ai_settings", dependencies=[Depends(verify_init_data)])
async def api_update_ai_settings(req: AISettingsRequest):
    """Обновить глобальные настройки ИИ"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('''
        UPDATE ai_settings 
        SET api_key = ?, system_prompt = ?, knowledge_base = ?, read_delay = ?, typing_delay = ?
        WHERE id = 1
    ''', (req.api_key, req.system_prompt, req.knowledge_base, req.read_delay, req.typing_delay))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok"})

class VerifyKeyRequest(BaseModel):
    api_key: str

@app.post("/api/ai_settings/verify", dependencies=[Depends(verify_init_data)])
async def api_verify_ai_key(req: VerifyKeyRequest):
    """Проверка валидности API ключа OnlySq (DeepSeek-V3)"""
    if not req.api_key:
        return JSONResponse({"status": "error", "message": "API ключ пуст."}, status_code=400)
    
    try:
        client = AsyncOpenAI(
            api_key=req.api_key,
            base_url="https://api.onlysq.ru/ai/openai/",
            timeout=15.0,
        )
        response = await client.chat.completions.create(
            model="deepseek-v3",
            messages=[{"role": "user", "content": "Ответь одним словом: OK"}],
            max_tokens=5,
        )
        return JSONResponse({"status": "ok", "message": "✅ API ключ OnlySq подключен! Модель: DeepSeek-V3"})
    except AuthenticationError:
        return JSONResponse({"status": "error", "message": "Недействительный API ключ."}, status_code=400)
    except APITimeoutError:
        return JSONResponse({"status": "error", "message": "Таймаут. Сервер OnlySq не отвечает."}, status_code=400)
    except Exception as e:
        logger.error(f"Verify key error: {e}")
        return JSONResponse({"status": "error", "message": f"Ошибка: {str(e)[:100]}"}, status_code=400)

# ----------------- OPERATORS ENDPOINTS -----------------
@app.get("/api/operators", dependencies=[Depends(verify_init_data)])
async def api_get_operators():
    """Получить список всех операторов"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('SELECT id, identity, is_super FROM operators')
    ops = [{"id": row[0], "identity": row[1], "is_super": bool(row[2])} for row in c.fetchall()]
    conn.close()
    return JSONResponse(ops)

class OperatorRequest(BaseModel):
    identity: str

@app.post("/api/operators", dependencies=[Depends(verify_init_data)])
async def api_add_operator(req: OperatorRequest):
    """Добавить оператора"""
    identity = req.identity.strip()
    if not identity:
        return JSONResponse({"status": "error", "message": "Пустой оператор"}, status_code=400)
    
    # Если не ID и не начинается с @ - добавляем @
    if not identity.isdigit() and not identity.startswith("@"):
        identity = "@" + identity
        
    identity = identity.lower() if identity.startswith("@") else identity
    
    conn = connect_db()
    c = conn.cursor()
    try:
        c.execute('INSERT INTO operators (identity) VALUES (?)', (identity,))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return JSONResponse({"status": "ok"})

@app.delete("/api/operators/{op_id}", dependencies=[Depends(verify_init_data)])
async def api_delete_operator(op_id: int):
    """Удалить оператора по ID в БД"""
    conn = connect_db()
    c = conn.cursor()
    c.execute('SELECT is_super FROM operators WHERE id = ?', (op_id,))
    row = c.fetchone()
    if row and row[0]:
        conn.close()
        return JSONResponse({"status": "error", "message": "Нельзя удалить супер-админа!"}, status_code=403)
    c.execute('DELETE FROM operators WHERE id = ?', (op_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok"})

# ----------------- LEADS ENDPOINTS (Фаза 1: воронка) -----------------
@app.get("/api/leads", dependencies=[Depends(verify_init_data)])
async def api_get_leads():
    """Все лиды с этапами — для канбан-доски."""
    conn = connect_db()
    c = conn.cursor()
    c.execute('''
        SELECT l.chat_id, l.phone, l.full_name, l.dob, l.citizenship, l.transport,
               l.stage, l.assigned_operator, l.outcome, l.drop_reason, l.exported,
               l.created_at, l.stage_changed_at, l.source_account,
               a.business_name, c.lead_name, l.score, l.is_duplicate, l.fraud_flags
        FROM leads l
        LEFT JOIN accounts a ON l.business_connection_id = a.business_connection_id
        LEFT JOIN chats c ON l.chat_id = c.chat_id
        ORDER BY l.score DESC, l.stage_changed_at DESC
    ''')
    rows = c.fetchall()
    conn.close()
    leads = [{
        "chat_id": r[0], "phone": r[1], "full_name": r[2] or r[15] or f"Лид {r[0]}",
        "dob": r[3], "citizenship": r[4], "transport": r[5], "stage": r[6] or "ready",
        "assigned_operator": r[7], "outcome": r[8], "drop_reason": r[9],
        "exported": bool(r[10]), "created_at": r[11], "stage_changed_at": r[12],
        "source_account": r[13], "business_name": r[14] or "—",
        "score": r[16] or 0, "is_duplicate": bool(r[17]),
        "fraud_flags": (r[18] or "").split(",") if r[18] else [],
    } for r in rows]
    return JSONResponse({"stages": LEAD_STAGES, "leads": leads})

@app.get("/api/leads/stats", dependencies=[Depends(verify_init_data)])
async def api_leads_stats():
    """Счётчики по этапам воронки (для сводки над доской)."""
    conn = connect_db()
    c = conn.cursor()
    c.execute('SELECT stage, COUNT(*) FROM leads GROUP BY stage')
    counts = {row[0]: row[1] for row in c.fetchall()}
    conn.close()
    return JSONResponse({"counts": {s: counts.get(s, 0) for s in LEAD_STAGES},
                         "total": sum(counts.values())})

class LeadStageRequest(BaseModel):
    stage: str

@app.post("/api/leads/{chat_id}/stage", dependencies=[Depends(verify_init_data)])
async def api_set_lead_stage(chat_id: int, req: LeadStageRequest):
    """Перевести лида на другой этап (перетаскивание карточки в канбане)."""
    if req.stage not in LEAD_STAGES:
        return JSONResponse({"status": "error", "message": "Неизвестный этап"}, status_code=400)
    conn = connect_db()
    c = conn.cursor()
    c.execute("UPDATE leads SET stage = ?, stage_changed_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
              (req.stage, chat_id))
    changed = c.rowcount
    conn.commit()
    conn.close()
    if not changed:
        return JSONResponse({"status": "error", "message": "Лид не найден"}, status_code=404)
    return JSONResponse({"status": "ok", "stage": req.stage})

# ----------------- KNOWLEDGE BASE (самообучение) -----------------
@app.get("/api/kb_gaps", dependencies=[Depends(verify_init_data)])
async def api_get_kb_gaps():
    """Очередь «подтвердить в базу знаний»: вопросы-эскалации, на которые
    оператор уже ответил, ждут одного клика для добавления в KB."""
    conn = connect_db()
    c = conn.cursor()
    c.execute('''SELECT id, chat_id, question, operator_answer, created_at
                 FROM kb_gaps WHERE status = 'answered' ORDER BY id DESC''')
    gaps = [{"id": r[0], "chat_id": r[1], "question": r[2],
             "answer": r[3], "created_at": r[4]} for r in c.fetchall()]
    conn.close()
    return JSONResponse(gaps)

@app.post("/api/kb_gaps/{gap_id}/approve", dependencies=[Depends(verify_init_data)])
async def api_approve_kb_gap(gap_id: int):
    """Добавляет пару Вопрос/Ответ в базу знаний AI и закрывает пробел."""
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT question, operator_answer FROM kb_gaps WHERE id = ? AND status = 'answered'", (gap_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return JSONResponse({"status": "error", "message": "Пробел не найден или уже обработан"}, status_code=404)
    question, answer = row
    addition = f"\n\nВ: {(question or '').strip()}\nО: {(answer or '').strip()}"
    # Дописываем на стороне SQL (|| ?) — без read-modify-write в Python, чтобы
    # одновременный approve или сохранение настроек не затёрли друг друга (lost update).
    c.execute("UPDATE ai_settings SET knowledge_base = COALESCE(knowledge_base, '') || ? WHERE id = 1", (addition,))
    c.execute("UPDATE kb_gaps SET status = 'approved' WHERE id = ?", (gap_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok"})

@app.post("/api/kb_gaps/{gap_id}/dismiss", dependencies=[Depends(verify_init_data)])
async def api_dismiss_kb_gap(gap_id: int):
    """Отклонить кандидата (не добавлять в базу знаний)."""
    conn = connect_db()
    c = conn.cursor()
    c.execute("UPDATE kb_gaps SET status = 'dismissed' WHERE id = ?", (gap_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "ok"})

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
