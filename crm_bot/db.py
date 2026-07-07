"""
crm_bot — слой доступа к данным (SQLite).

Выделен из main.py, чтобы отделить работу с БД от бота/веба и удешевить правки.
Все функции синхронные и переиспользуемые; для горячих путей есть async-offload
(`offload` / `fetch_chat_history`), уводящий запрос в поток, чтобы не блокировать
event loop. WAL + busy_timeout (см. connect_db / init_db) убирают 'database is locked'.

main.py ре-экспортирует эти имена, поэтому существующие `from main import ...`
(в т.ч. в ai_handler.py) продолжают работать без изменений.
"""

import os
import re
import sqlite3
import asyncio
import logging

logger = logging.getLogger(__name__)

DB_FILE = os.getenv("DB_PATH", "crm_data.db")


def connect_db():
    """Единая точка подключения к SQLite: busy_timeout защищает от
    мгновенного 'database is locked' под конкуренцией. WAL включается
    один раз в init_db() и персистится в самом файле БД."""
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


async def offload(func, *args, **kwargs):
    """Выполнить синхронную функцию БД в отдельном потоке — не блокирует event loop.
    Используй на горячих путях: await offload(some_query, arg)."""
    return await asyncio.to_thread(func, *args, **kwargs)


# ── Допустимые этапы воронки (порядок = порядок канбан-колонок) ─────────
LEAD_STAGES = ["new", "dialog", "ready", "onboarding", "on_line", "paid", "lost"]

# Ключевые слова, по которым считаем, что лид «горячий» (интересуется условиями)
_HOT_KEYWORDS = ("зарплат", "оплат", "график", "смен", "выплат", "сколько", "когда", "как оформ")


def normalize_phone(phone: str) -> str:
    """Оставляет только цифры; 8XXXXXXXXXX → 7XXXXXXXXXX для единого ключа дедупа."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def compute_lead_score(chat_id: int, lead_data: dict) -> int:
    """Rule-based скоринг «вероятности выйти на линию» из сигналов диалога.
    Диапазон 0..100. Заменяется обученной моделью позже, когда накопятся исходы."""
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT text, is_outgoing FROM messages WHERE chat_id = ?", (chat_id,))
    rows = c.fetchall()
    conn.close()

    incoming = [(t or "") for t, out in rows if not out]
    score = 15  # дошёл до LEAD_READY — базовый балл
    if normalize_phone(lead_data.get("phone", "")):
        score += 30                                    # оставил валидный телефон
    if any(k in (t.lower()) for t in incoming for k in _HOT_KEYWORDS):
        score += 20                                    # спрашивал про оплату/график
    score += min(len(incoming) * 3, 20)                # вовлечённость
    if incoming and (sum(len(t) for t in incoming) / len(incoming)) > 25:
        score += 10                                    # развёрнутые ответы
    if (lead_data.get("transport") or "").strip():
        score += 5
    return max(0, min(100, score))


def _detect_fraud(chat_id: int, phone_norm: str, cursor) -> tuple:
    """Возвращает (is_duplicate, fraud_flags_csv). Дешёвая защита выручки
    до выставления счёта службе: один человек на двух аккаунтах = двойной счёт."""
    flags = []
    is_dup = 0
    if phone_norm:
        cursor.execute(
            "SELECT chat_id FROM leads WHERE phone_normalized = ? AND chat_id != ?",
            (phone_norm, chat_id),
        )
        if cursor.fetchone():
            is_dup = 1
            flags.append("dup_phone")       # тот же телефон уже есть у другого лида
        if len(phone_norm) < 10:
            flags.append("bad_phone")       # телефон-мусор
    else:
        flags.append("no_phone")
    return is_dup, ",".join(flags)


def upsert_lead(chat_id: int, business_connection_id: str, data: dict, source_account: str = "") -> bool:
    """Создаёт/обновляет лида по chat_id. Возвращает True, если лид НОВЫЙ
    (для идемпотентности уведомлений/выгрузки). Пустые поля не затирают
    ранее собранные значения. Заодно считает скоринг, нормализует телефон,
    помечает дубли и фрод-флаги."""
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM leads WHERE chat_id = ?", (chat_id,))
    is_new = c.fetchone() is None

    phone = (data.get("phone") or "").strip()
    phone_norm = normalize_phone(phone)
    score = compute_lead_score(chat_id, data)
    is_dup, fraud_flags = _detect_fraud(chat_id, phone_norm, c)

    vals = (
        phone,
        (data.get("name") or data.get("full_name") or "").strip(),
        (data.get("dob") or "").strip(),
        (data.get("citizenship") or "").strip(),
        (data.get("transport") or "").strip(),
    )
    if is_new:
        c.execute(
            '''INSERT INTO leads (chat_id, business_connection_id, phone, full_name,
               dob, citizenship, transport, source_account, stage,
               phone_normalized, score, is_duplicate, fraud_flags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)''',
            (chat_id, business_connection_id, *vals, source_account,
             phone_norm, score, is_dup, fraud_flags),
        )
    else:
        c.execute(
            '''UPDATE leads SET
                 phone            = COALESCE(NULLIF(?,''), phone),
                 full_name        = COALESCE(NULLIF(?,''), full_name),
                 dob              = COALESCE(NULLIF(?,''), dob),
                 citizenship      = COALESCE(NULLIF(?,''), citizenship),
                 transport        = COALESCE(NULLIF(?,''), transport),
                 phone_normalized = COALESCE(NULLIF(?,''), phone_normalized),
                 score            = ?,
                 is_duplicate     = ?,
                 fraud_flags      = ?
               WHERE chat_id = ?''',
            (*vals, phone_norm, score, is_dup, fraud_flags, chat_id),
        )
    conn.commit()
    conn.close()
    return is_new


def mark_lead_exported(chat_id: int) -> None:
    conn = connect_db()
    conn.execute("UPDATE leads SET exported = 1 WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def lead_export_blocked(chat_id: int) -> bool:
    """Дубль не выгружаем во внешнюю систему до ручной проверки — защита выручки."""
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT is_duplicate FROM leads WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row[0])


# ── KB GAPS (самообучающаяся база знаний) ──────────────────────────────
def open_kb_gap(chat_id: int, thread_id, question: str) -> None:
    """Фиксирует вопрос-эскалацию как «пробел» в базе знаний (если ещё не открыт)."""
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT id FROM kb_gaps WHERE chat_id = ? AND status = 'open'", (chat_id,))
    if c.fetchone() is None:
        c.execute(
            "INSERT INTO kb_gaps (chat_id, thread_id, question, status) VALUES (?, ?, ?, 'open')",
            (chat_id, thread_id, (question or "")[:500]),
        )
        conn.commit()
    conn.close()


def answer_kb_gap(chat_id: int, operator_answer: str) -> None:
    """Оператор ответил лиду с открытым пробелом → сохраняем его ответ как кандидат в KB."""
    if not (operator_answer or "").strip():
        return
    conn = connect_db()
    c = conn.cursor()
    c.execute(
        """UPDATE kb_gaps SET operator_answer = ?, status = 'answered'
           WHERE id = (SELECT id FROM kb_gaps WHERE chat_id = ? AND status = 'open'
                       ORDER BY id DESC LIMIT 1)""",
        (operator_answer.strip()[:1000], chat_id),
    )
    conn.commit()
    conn.close()


# ── Topics (форум мастер-группы) ───────────────────────────────────────
def get_topic(business_connection_id: str, chat_id: int):
    conn = connect_db()
    c = conn.cursor()
    c.execute('SELECT message_thread_id FROM topics WHERE business_connection_id = ? AND chat_id = ?',
              (business_connection_id, chat_id))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None


def save_topic(business_connection_id: str, chat_id: int, message_thread_id: int):
    conn = connect_db()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO topics (business_connection_id, chat_id, message_thread_id)
        VALUES (?, ?, ?)
    ''', (business_connection_id, chat_id, message_thread_id))
    conn.commit()
    conn.close()


def get_lead_by_topic(message_thread_id: int):
    conn = connect_db()
    c = conn.cursor()
    c.execute('SELECT business_connection_id, chat_id FROM topics WHERE message_thread_id = ?',
              (message_thread_id,))
    result = c.fetchone()
    conn.close()
    return result


# ── Accounts / chats / messages ────────────────────────────────────────
def save_account(business_connection_id: str, business_name: str, user_id: int):
    conn = connect_db()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO accounts (business_connection_id, business_name, user_id)
        VALUES (?, ?, ?)
    ''', (business_connection_id, business_name, user_id))
    conn.commit()
    conn.close()


def save_chat_and_message(chat_id: int, business_connection_id: str, lead_name: str, text: str,
                          is_outgoing: bool, media_type: str = None, media_url: str = None):
    conn = connect_db()
    c = conn.cursor()
    # Обновляем инфо о чате
    c.execute('''
        INSERT INTO chats (chat_id, business_connection_id, lead_name, last_message_time, is_unread)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            last_message_time=CURRENT_TIMESTAMP,
            is_unread=excluded.is_unread,
            business_connection_id=excluded.business_connection_id,
            lead_name=CASE
                WHEN excluded.lead_name LIKE 'User %' OR excluded.lead_name LIKE 'Пользователь %'
                THEN chats.lead_name
                ELSE excluded.lead_name
            END
    ''', (chat_id, business_connection_id, lead_name, not is_outgoing))

    # Сохраняем сообщение
    c.execute('''
        INSERT INTO messages (chat_id, text, is_outgoing, media_type, media_url)
        VALUES (?, ?, ?, ?, ?)
    ''', (chat_id, text if text else "", is_outgoing, media_type, media_url))

    conn.commit()
    conn.close()


def fetch_chat_history_rows(chat_id: int, limit: int = 100):
    """Последние `limit` сообщений чата в хронологическом порядке — контекст для AI.
    Порядок устойчив по (timestamp, id), чтобы реплики в одну секунду не путались."""
    conn = connect_db()
    c = conn.cursor()
    c.execute('''
        SELECT text, is_outgoing FROM (
            SELECT text, is_outgoing, timestamp, id
            FROM messages
            WHERE chat_id = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        ) sub ORDER BY timestamp ASC, id ASC
    ''', (chat_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows
