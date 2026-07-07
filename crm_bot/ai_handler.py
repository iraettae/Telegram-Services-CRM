import asyncio
import os
import random
import logging
import sqlite3
import openai
import re
import html
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

ONLYSQ_API_KEY = os.getenv("ONLYSQ_API_KEY", "")

# Семафор: макс 3 параллельных запроса к API (защита от 10 RPM лимита)
API_SEMAPHORE = asyncio.Semaphore(3)

# Regex: ловит <ESCALATE>...</ESCALATE> ИЛИ незакрытый <ESCALATE>...до конца строки
ESCALATE_PATTERN = re.compile(r'<ESCALATE>(.*?)(?:</ESCALATE>|$)', re.DOTALL | re.IGNORECASE)

def extract_escalation(reply_text: str) -> tuple:
    """Извлекает тег эскалации из ответа ИИ. Returns: (clean_text, reason или None)"""
    match = ESCALATE_PATTERN.search(reply_text)
    if match:
        reason = match.group(1).strip()
        clean_text = ESCALATE_PATTERN.sub('', reply_text).strip()
        if not reason or len(reason) > 500:
            reason = "Вопрос вне базы знаний (тег повреждён)"
        return clean_text, reason
    return reply_text, None


def extract_lead_ready(reply_text: str):
    """Устойчиво извлекает данные лида после тега [LEAD_READY].

    Возвращает (clean_text_до_тега, lead_data|None). Снимает ```json-обрамление
    и находит первый СБАЛАНСИРОВАННЫЙ JSON-объект с учётом строк и экранирования —
    прежний парсер терял данные при малейшем отклонении формата модели.
    """
    import json as _json
    if "[LEAD_READY]" not in reply_text:
        return reply_text, None
    idx = reply_text.index("[LEAD_READY]")
    clean_text = reply_text[:idx].strip()
    tail = reply_text[idx + len("[LEAD_READY]"):]
    tail = re.sub(r'```(?:json)?', '', tail).strip()
    start = tail.find('{')
    if start == -1:
        return clean_text, {}
    depth, in_str, esc, end = 0, False, False, -1
    for i in range(start, len(tail)):
        ch = tail[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return clean_text, {}
    try:
        return clean_text, _json.loads(tail[start:end])
    except Exception:
        logger.warning(f"[LEAD_READY] JSON не распарсился, сырой хвост: {tail[start:end][:200]}")
        return clean_text, {}

# Словарь для хранения активных тасок ожидания генерации ответа (Debounce)
chat_tasks = {}

async def read_chat_history(chat_id: int, conn_id: str, message_id: int):
    """
    Симуляция полного прочтения истории чата.
    Эффект живого человека: перед ответом бот читает сообщение.
    """
    from main import bot
    await asyncio.sleep(random.uniform(2.0, 4.0))
    if message_id and conn_id:
        try:
            await bot.read_business_message(business_connection_id=conn_id, chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.warning(f"Не удалось отметить сообщение прочитанным: {e}")

async def get_ai_reply(db_api_key: str, system_prompt: str, knowledge_base: str, chat_id: int, user_message: str, lead_name: str = "", business_name: str = "", custom_history: list = None):
    async with API_SEMAPHORE:
        return await _get_ai_reply_impl(db_api_key, system_prompt, knowledge_base, chat_id, user_message, lead_name, business_name, custom_history)

async def _get_ai_reply_impl(db_api_key: str, system_prompt: str, knowledge_base: str, chat_id: int, user_message: str, lead_name: str = "", business_name: str = "", custom_history: list = None):
    from main import DB_FILE, connect_db
    
    # =====================================================================
    # FIX #3: Загружаем историю КОНКРЕТНОГО чата из БД, с разумным лимитом
    # Берём последние 100 сообщений — достаточно для контекста, но защищает
    # от переполнения контекстного окна DeepSeek-V3 (~64K токенов).
    # =====================================================================
    if custom_history is not None:
        # Тестовый режим: используем переданную историю
        history = list(custom_history)
        if user_message and not (history and history[-1]["role"] == "user" and history[-1]["content"] == user_message):
            history.append({"role": "user", "content": user_message})
    else:
        conn = connect_db()
        c = conn.cursor()
        c.execute('''
            SELECT text, is_outgoing FROM (
                SELECT text, is_outgoing, timestamp, id
                FROM messages
                WHERE chat_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT 100
            ) sub ORDER BY timestamp ASC, id ASC
        ''', (chat_id,))
        rows = c.fetchall()
        conn.close()
        
        # Формируем историю как локальную переменную (изолировано для этого чата)
        history = []
        for text, is_outgoing in rows:
            if not text:
                continue
            role = "assistant" if is_outgoing else "user"
            history.append({"role": role, "content": text})

        # Убеждаемся, что текущее сообщение тоже учтено, если оно ещё не в БД
        if user_message and not (history and history[-1]["role"] == "user" and history[-1]["content"] == user_message):
            history.append({"role": "user", "content": user_message})

    # API ключ: из БД, потом из .env, потом fallback "free"
    api_key = (db_api_key or "").strip() or os.getenv("ONLYSQ_API_KEY", "").strip() or "free"

    # =====================================================================
    # FIX #1: Формируем system prompt — ВСЕГДА первый, роль "system"
    # =====================================================================
    sys_content = (system_prompt or "").strip()
    
    # Диагностика: логируем, что пришло из БД
    logger.info(f"[AI] DB system_prompt empty: {not bool(sys_content)}, length: {len(sys_content)}")
    
    if knowledge_base and knowledge_base.strip():
        sys_content += f"\n\n--- БАЗА ЗНАНИЙ (используй эту информацию при ответах) ---\n{knowledge_base.strip()}\n--- КОНЕЦ БАЗЫ ЗНАНИЙ ---"
    
    # Fallback: если промпт полностью пуст — задаём минимальный дефолтный
    if not sys_content:
        sys_content = "Ты — вежливый помощник. Отвечай на сообщения клиента коротко и по делу."
        logger.warning("[AI] System prompt is EMPTY! Using fallback. Check ai_settings table in DB.")

    # =====================================================================
    # FIX #2: Контекстная изоляция — вставляем имя лида в system prompt
    # Это гарантирует, что ИИ знает, с кем общается, и не путает лидов.
    # =====================================================================
    if lead_name:
        sys_content += f"\n\nТы сейчас ведёшь диалог с клиентом по имени: {lead_name}. Обращайся к нему соответственно."
        
    sys_content += """
ПРАВИЛО ЭСКАЛАЦИИ: Если клиент задаёт вопрос, на который в базе знаний нет точного ответа (условия работы, зарплаты, графики которые не описаны) — НЕ выдумывай ответ. Вместо этого напиши ТОЛЬКО служебный тег и больше ничего:
<ESCALATE>краткое описание вопроса клиента</ESCALATE>
Клиент НЕ получит никакого сообщения, автоответчик выключится и оператор получит уведомление.
НЕ используй <ESCALATE> если ответ ЕСТЬ в базе знаний. Только когда точного ответа нет.

ФОРМАТ [LEAD_READY]: Когда ставишь тег [LEAD_READY], добавь сразу после него JSON с данными которые ты собрал у клиента:
[LEAD_READY]{"phone": "номер", "name": "ФИО", "dob": "дата рождения", "citizenship": "гражданство", "transport": "транспорт"}
JSON будет автоматически вырезан вместе с тегом — клиент его НЕ увидит. Если какое-то поле неизвестно — ставь прочерк.
"""

    messages = [{"role": "system", "content": sys_content}]
    logger.info(f"[AI] System prompt total length: {len(sys_content)} chars. First 300: {sys_content[:300]}")
    logger.info(f"[AI] Chat {chat_id} (lead: {lead_name}): loaded {len(history)} messages from DB")
    logger.info(f"[AI] API key source: {'DB' if db_api_key.strip() else 'ENV' if os.getenv('ONLYSQ_API_KEY', '').strip() else 'FREE'}")
        
    messages.extend(history)

    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://api.onlysq.ru/ai/openai/",
        timeout=60.0,
    )
    
    for attempt in range(3):
        try:
            response = await client.chat.completions.create(
                model="deepseek-v3",
                messages=messages,
                temperature=0.7,
                max_tokens=1024,
            )
            reply_text = response.choices[0].message.content
            return reply_text
            
        except openai.RateLimitError as e:
            logger.warning(f"RateLimitError. Ждем 20 секунд перед повтором (Попытка {attempt+1}/3)... {e}")
            await asyncio.sleep(20)
        except Exception as e:
            logger.error(f"DeepSeek AI error: {e}")
            return None
            
    logger.error("Rate limit exceeded after 3 attempts.")
    return None

async def _internal_process_ai_reply(chat_id: int, conn_id: str, user_message: str, lead_name: str, thread_id: str = None, message_id: int = None):
    from main import bot, DB_FILE, connect_db, save_chat_and_message, update_avatar_if_needed, delayed_send_to_telegram, MASTER_GROUP_ID
    from datetime import datetime

    # Human-override guard: за время debounce (до 7-9с) оператор мог ответить
    # вручную — тогда ai_paused уже = 1. Раньше эта проверка была только ДО
    # постановки задачи, и AI перебивал человека. Перечитываем состояние здесь.
    if user_message:  # для Force AI (user_message="") паузу снимают явно в эндпоинте
        _g = connect_db(); _gc = _g.cursor()
        _gc.execute('SELECT ai_paused FROM chats WHERE chat_id = ?', (chat_id,))
        _row = _gc.fetchone()
        _acc = None
        if conn_id:
            _gc.execute('SELECT ai_enabled FROM accounts WHERE business_connection_id = ?', (conn_id,))
            _acc = _gc.fetchone()
        _g.close()
        if (_row and _row[0]) or (_acc is not None and not _acc[0]):
            logger.info(f"[AI] Пропуск чата {chat_id}: пауза/AI выключен (проверка после debounce)")
            return

    # Читаем настройки из БД
    conn = connect_db()
    c = conn.cursor()
    c.execute('SELECT api_key, system_prompt, knowledge_base, read_delay, typing_delay FROM ai_settings WHERE id = 1')
    ai_config = c.fetchone()
    
    db_api_key = ai_config[0] if ai_config and ai_config[0] else ""
    system_prompt = ai_config[1] if ai_config else ""
    knowledge_base = ai_config[2] if ai_config else ""
    read_delay = ai_config[3] if ai_config and ai_config[3] is not None else 2
    typing_delay = ai_config[4] if ai_config and ai_config[4] is not None else 2

    # Проверяем, нужно ли делать задержку на чтение (менее 8 сек между сообщениями)
    is_consecutive = False
    if user_message:
        c.execute('SELECT timestamp FROM messages WHERE chat_id = ? AND is_outgoing = 0 ORDER BY timestamp DESC LIMIT 2', (chat_id,))
        rows = c.fetchall()
        if len(rows) == 2:
            try:
                t1 = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
                t2 = datetime.strptime(rows[1][0], "%Y-%m-%d %H:%M:%S")
                if (t1 - t2).total_seconds() < 8.0:
                    is_consecutive = True
            except:
                pass
    
    # Узнаём имя рабочего аккаунта, от которого отвечает ИИ
    business_name = ""
    if conn_id:
        c.execute("SELECT business_name FROM accounts WHERE business_connection_id = ?", (conn_id,))
        biz_row = c.fetchone()
        business_name = biz_row[0] if biz_row else ""
    conn.close()

    # 1. Чтение сообщения с задержкой (эффект живого человека)
    if not is_consecutive and user_message and read_delay > 0:
        await asyncio.sleep(max(0, read_delay + random.uniform(-0.5, 0.5)))
        
    if message_id and conn_id:
        try:
            await bot.read_business_message(business_connection_id=conn_id, chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.warning(f"Не удалось отметить сообщение прочитанным: {e}")

    # Задержка перед началом печатания
    if user_message and typing_delay > 0:
        await asyncio.sleep(max(0, typing_delay + random.uniform(-0.5, 0.5)))
    
    # 2. Перед ответом — статус typing (с safety timeout 120с)
    async def keep_typing():
        try:
            elapsed = 0
            while elapsed < 120:  # Safety: макс 120 секунд typing
                try:
                    await bot.send_chat_action(chat_id=chat_id, action="typing", business_connection_id=conn_id)
                except:
                    pass
                await asyncio.sleep(4.0)
                elapsed += 4
            logger.warning(f"keep_typing safety timeout (120s) для чата {chat_id}")
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(keep_typing())
    
    # Генерация ответа через ИИ (передаём lead_name для контекстной изоляции)
    # ВАЖНО: try/finally гарантирует cancel typing даже при краше get_ai_reply()
    reply_text = None
    try:
        logger.info(f"Начинаем генерацию ответа для {chat_id} (lead: {lead_name})")
        reply_text = await get_ai_reply(db_api_key, system_prompt, knowledge_base, chat_id, user_message, lead_name, business_name)
        logger.info(f"Получен ответ: {reply_text}")
    except Exception as e:
        logger.error(f"Критическая ошибка при генерации AI ответа для {chat_id}: {e}", exc_info=True)
    finally:
        typing_task.cancel()

    escalated = False       # был ли ответ эскалацией (тогда пауза выставлена намеренно)
    pause_after_send = False  # LEAD_READY: после хендоффа оператору глушим автоответчик

    # =====================================================================
    # ESCALATION: Human-in-the-Loop — перехват тега <ESCALATE>
    # Если ИИ не нашёл ответа в базе знаний, он добавляет тег.
    # Мы вырезаем тег, отключаем автоответчик и шлём алерт оператору.
    # =====================================================================
    if reply_text:
        try:
            clean_text, escalation_reason = extract_escalation(reply_text)
            if escalation_reason:
                logger.info(f"ESCALATION: chat {chat_id}, lead: {lead_name}, reason: {escalation_reason}")
                reply_text = clean_text
                escalated = True
                
                # Атомарное обновление — защита от дублей алертов
                esc_conn = connect_db()
                esc_cursor = esc_conn.cursor()
                esc_cursor.execute("UPDATE chats SET ai_paused = 1 WHERE chat_id = ? AND ai_paused = 0", (chat_id,))
                is_new_pause = esc_cursor.rowcount > 0
                esc_conn.commit()
                esc_conn.close()
                
                if is_new_pause:
                    # Получаем имя бизнес-аккаунта для алерта
                    try:
                        info_conn = connect_db()
                        info_cursor = info_conn.cursor()
                        info_cursor.execute("SELECT business_name FROM accounts WHERE business_connection_id = ?", (conn_id,))
                        acc_row = info_cursor.fetchone()
                        biz_name = acc_row[0] if acc_row else "Неизвестный"
                        info_cursor.execute("SELECT identity FROM operators")
                        ops = [row[0] for row in info_cursor.fetchall()]
                        info_conn.close()
                        
                        # HTML + html.escape: имя/вопрос со спецсимволами (Иван_2007, *Аня*)
                        # больше не ломают отправку — раньше такие алерты молча терялись.
                        alert_text = (
                            f"⚠️ Эскалация\n"
                            f"👤 Лид: <a href=\"tg://user?id={chat_id}\">{html.escape(lead_name or '—')}</a>\n"
                            f"❓ Вопрос: {html.escape(escalation_reason or '—')}\n"
                            f"💼 Аккаунт: {html.escape(biz_name or '—')}\n\n"
                            f"Автоответчик отключён. Ответьте вручную."
                        )

                        sent_count = 0
                        for op in ops:
                            if op.isdigit():
                                try:
                                    await bot.send_message(int(op), alert_text, parse_mode="HTML")
                                    sent_count += 1
                                except Exception as e:
                                    logger.error(f"Ошибка отправки эскалации оператору {op}: {e}")

                        # Fallback на MASTER_GROUP_ID
                        if sent_count == 0:
                            try:
                                await bot.send_message(MASTER_GROUP_ID, alert_text, parse_mode="HTML")
                            except Exception as e:
                                logger.error(f"Ошибка отправки эскалации в мастер-группу: {e}")
                                
                    except Exception as e:
                        logger.error(f"Ошибка при отправке алерта эскалации: {e}")
                        # НЕ делаем rollback ai_paused! Бот должен молчать.
        except Exception as e:
            logger.error(f"Ошибка при обработке эскалации для chat {chat_id}: {e}")
            # Graceful degradation: отправляем reply_text как есть

    if reply_text:
        # Перехватываем [SILENCE] — AI решил не отвечать (лид написал "ок", "спс", реакция и т.д.)
        if "[SILENCE]" in reply_text:
            logger.info(f"SILENCE: chat {chat_id}, AI решил промолчать. Raw: {reply_text[:100]}")
            return  # Не отправляем ничего лиду
        
        # Перехватываем [LEAD_READY] с JSON данными лида
        if "[LEAD_READY]" in reply_text:
            # Устойчивый разбор через общий хелпер (см. extract_lead_ready).
            clean_before, lead_data = extract_lead_ready(reply_text)
            lead_data = lead_data or {}
            reply_text = clean_before
            pause_after_send = True  # лид передан оператору → AI замолкает
            if not reply_text:
                reply_text = "Отлично! Сейчас передам твои данные менеджеру, он свяжется с тобой."
                
            # Формируем алерт с данными лида
            conn = connect_db()
            cursor = conn.cursor()
            
            # Получаем имя бизнес-аккаунта
            cursor.execute("SELECT business_name FROM accounts WHERE business_connection_id = ?", (conn_id,))
            acc_row = cursor.fetchone()
            biz_name = acc_row[0] if acc_row else "Неизвестный"
            
            cursor.execute("SELECT identity FROM operators")
            ops = [row[0] for row in cursor.fetchall()]
            conn.close()
            
            # Форматируем данные
            phone = lead_data.get('phone', '—')
            full_name = lead_data.get('name', lead_name)
            dob = lead_data.get('dob', '—')
            citizenship = lead_data.get('citizenship', '—')
            transport = lead_data.get('transport', '—')
            
            # HTML + escape: данные лида из ответа модели могут содержать _ * [ ] ` —
            # на Markdown такие алерты падали, и готовый лид терялся навсегда.
            msg_text = (
                f"🚀 <b>Новый лид!</b>\n"
                f"\n"
                f"👤 Имя: <a href=\"tg://user?id={chat_id}\">{html.escape(str(full_name) or '—')}</a>\n"
                f"📱 Телефон: {html.escape(str(phone))}\n"
                f"🎂 Дата рождения: {html.escape(str(dob))}\n"
                f"🌍 Гражданство: {html.escape(str(citizenship))}\n"
                f"🚗 Транспорт: {html.escape(str(transport))}\n"
                f"\n"
                f"💼 Аккаунт: {html.escape(str(biz_name))}"
            )

            sent_count = 0
            for op_identity in ops:
                if op_identity.isdigit():
                    try:
                        await bot.send_message(int(op_identity), msg_text, parse_mode="HTML")
                        sent_count += 1
                    except Exception as e:
                        logger.error(f"ОШИБКА отправки уведомления оператору {op_identity}: {e}")

            # Fallback
            if sent_count == 0:
                try:
                    await bot.send_message(MASTER_GROUP_ID, msg_text, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"ОШИБКА отправки резервного уведомления: {e}")
            
            logger.info(f"LEAD_READY: chat {chat_id}, data: {lead_data}")

        # Повторная проверка перед отправкой: оператор мог ответить вручную,
        # пока AI генерировал ответ. Эскалацию не трогаем (там пауза намеренная).
        if not escalated and user_message:
            _p = connect_db(); _pc = _p.cursor()
            _pc.execute('SELECT ai_paused FROM chats WHERE chat_id = ?', (chat_id,))
            _pr = _pc.fetchone()
            _p.close()
            if _pr and _pr[0]:
                logger.info(f"[AI] Отмена отправки чату {chat_id}: пауза выставлена во время генерации")
                return

        # 3. Разбиваем получившийся ответ по ||| или двойному переносу строки
        raw_chunks = re.split(r'\|\|\||\n{2,}', reply_text)
        chunks = [chunk.strip() for chunk in raw_chunks if chunk.strip()]
        logger.info(f"Ответ разбит на {len(chunks)} частей")
        
        for i, chunk in enumerate(chunks):
            # Сохраняем сообщение в локальную БД CRM для отображения в WebApp
            save_chat_and_message(
                chat_id=chat_id,
                business_connection_id=conn_id,
                lead_name=lead_name,
                text=chunk,
                is_outgoing=True
            )
            asyncio.create_task(update_avatar_if_needed(chat_id))
            
            # Отправка с искусственной задержкой печатания внутри (вызывается из main.py)
            await delayed_send_to_telegram(chat_id, chunk, conn_id, thread_id)

        # После LEAD_READY-хендоффа глушим автоответчик, чтобы AI не дублировал
        # уведомления и не конфликтовал с живым оператором.
        if pause_after_send:
            _pa = connect_db(); _pac = _pa.cursor()
            _pac.execute('UPDATE chats SET ai_paused = 1 WHERE chat_id = ?', (chat_id,))
            _pa.commit(); _pa.close()
            logger.info(f"[AI] Чат {chat_id} передан оператору (LEAD_READY) → автоответчик на паузе")

async def process_ai_reply_new(chat_id: int, conn_id: str, user_message: str, lead_name: str, thread_id: str = None, message_id: int = None):
    """Обертка(Debouncer) с адаптивным таймером для группировки сообщений от лида.
    
    Короткие сообщения (типа "ну", "да", "мне 20") ждут дольше — 
    скорее всего человек ещё допишет мысль в следующем сообщении.
    Полные ответы (длинные, с вопросом) ждут меньше.
    """
    # Если для этого чата уже есть ожидающая задача, отменяем её
    if chat_id in chat_tasks and not chat_tasks[chat_id].done():
        chat_tasks[chat_id].cancel()
        logger.info(f"Отменена предыдущая задача генерации ИИ для чата {chat_id} (Debounce)")
    
    async def delayed_process():
        try:
            if user_message:  # Не ждем если это ручной вызов Force AI где message=""
                # Адаптивный таймер: длина и содержание определяют время ожидания
                msg_len = len(user_message.strip())
                has_question = '?' in user_message
                
                if msg_len < 30 and not has_question:
                    # Короткое сообщение без вопроса ("ну", "да", "мне 20", "из мск")
                    # Скорее всего продолжит писать → ждём дольше
                    delay = random.uniform(7.0, 9.0)
                elif msg_len < 80:
                    # Среднее сообщение — может быть полным, может нет
                    delay = random.uniform(5.0, 6.5)
                else:
                    # Длинный развёрнутый ответ — скорее всего полный
                    delay = random.uniform(3.5, 5.0)
                
                logger.info(f"Debounce chat {chat_id}: msg={msg_len} chars, delay={delay:.1f}s")
                await asyncio.sleep(delay)
            
            # Вызываем основную логику генерации
            logger.info(f"Группировка завершена, запускаем ИИ для чата {chat_id}")
            await _internal_process_ai_reply(chat_id, conn_id, user_message, lead_name, thread_id, message_id)
            
        except asyncio.CancelledError:
            logger.info(f"Таймер генерации для чата {chat_id} был прерван новым сообщением. Ждем дальше...")
        except Exception as e:
            logger.error(f"Непредвиденная критическая ошибка в ИИ задаче: {e}", exc_info=True)
            
    # Создаем новую задачу таймера для чата.
    # done-callback удаляет запись, иначе chat_tasks растёт вечно (утечка памяти).
    task = asyncio.create_task(delayed_process())
    chat_tasks[chat_id] = task
    task.add_done_callback(
        lambda t, cid=chat_id: chat_tasks.pop(cid, None) if chat_tasks.get(cid) is t else None
    )

