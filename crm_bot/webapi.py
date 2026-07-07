"""
crm_bot — FastAPI веб-слой (эндпоинты WebApp + вебхук Telegram).

Вынесено из main.py. Эндпоинты регистрируются на переданном app внутри
register_routes(app, ctx); зависимости (bot, dp, config, хелперы) приходят через
ctx и распаковываются в локальные имена, поэтому тела эндпоинтов не менялись.
Циклического импорта нет: main вызывает register_routes в рантайме, а webapi
импортирует только db/ai_handler (ai_handler тянет main лениво, внутри функций).
"""

import os
import sqlite3
import hmac
import hashlib
import json
import time
import uuid
import asyncio
import logging
import urllib.parse
from datetime import datetime

from fastapi import Request, Depends, HTTPException, File, UploadFile, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from aiogram.types import Update, FSInputFile, ContentType
from openai import AsyncOpenAI, AuthenticationError, APITimeoutError

from db import connect_db, LEAD_STAGES, save_chat_and_message, get_topic

logger = logging.getLogger(__name__)

import uuid as uuid_module

test_chats = {}  # {chat_id: {"name": str, "messages": [{role, content}], "created_at": str}}

class SendMessageRequest(BaseModel):
    business_connection_id: str
    text: str

class AISettingsRequest(BaseModel):
    api_key: str
    system_prompt: str
    knowledge_base: str
    read_delay: int = 2
    typing_delay: int = 2

class VerifyKeyRequest(BaseModel):
    api_key: str

class OperatorRequest(BaseModel):
    identity: str

class LeadStageRequest(BaseModel):
    stage: str


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


def register_routes(app, ctx):
    """Регистрирует все FastAPI-роуты на app. ctx несёт зависимости из main."""
    bot = ctx.bot
    dp = ctx.dp
    BOT_TOKEN = ctx.BOT_TOKEN
    MASTER_GROUP_ID = ctx.MASTER_GROUP_ID
    WEBHOOK_SECRET = ctx.WEBHOOK_SECRET
    WEBHOOK_PATH = ctx.WEBHOOK_PATH
    WEBAPP_URL = ctx.WEBAPP_URL
    INIT_DATA_TTL = ctx.INIT_DATA_TTL
    MAX_UPLOAD_BYTES = ctx.MAX_UPLOAD_BYTES
    delayed_send_to_telegram = ctx.delayed_send_to_telegram
    update_avatar_if_needed = ctx.update_avatar_if_needed
    download_media_if_present = ctx.download_media_if_present

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

    app.mount("/static", StaticFiles(directory="frontend"), name="static")

    @app.get("/")
    def get_index():
        with open("frontend/index.html", "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content, status_code=200)

    @app.get("/health")
    def health_check():
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
    def api_get_accounts():
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

    @app.get("/api/test_ai/chats", dependencies=[Depends(verify_init_data)])
    def api_test_ai_list():
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
    def api_test_ai_delete(chat_id: str):
        """Удалить тест-чат"""
        if chat_id in test_chats:
            del test_chats[chat_id]
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "error", "message": "Чат не найден"}, status_code=404)

    @app.get("/api/test_ai/chats/{chat_id}/messages", dependencies=[Depends(verify_init_data)])
    def api_test_ai_get_messages(chat_id: str):
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

    @app.get("/api/chats", dependencies=[Depends(verify_init_data)])
    def api_get_chats():
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
    def api_get_messages(chat_id: int):
        """Получить историю сообщений для конкретного чата"""
        conn = connect_db()
        c = conn.cursor()
        c.execute('SELECT id, text, is_outgoing, timestamp, media_type, media_url FROM messages WHERE chat_id = ? ORDER BY timestamp ASC', (chat_id,))
        rows = c.fetchall()
        conn.close()
    
        msgs = [{"id": r[0], "text": r[1], "is_outgoing": bool(r[2]), "timestamp": r[3], "media_type": r[4], "media_url": r[5]} for r in rows]
        return JSONResponse(msgs)

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
    def api_read_chat(chat_id: int):
        """Отметить чат как прочитанный"""
        conn = connect_db()
        c = conn.cursor()
        c.execute('UPDATE chats SET is_unread = 0 WHERE chat_id = ?', (chat_id,))
        conn.commit()
        conn.close()
        return JSONResponse({"status": "ok"})

    @app.post("/api/chats/{chat_id}/unread", dependencies=[Depends(verify_init_data)])
    def api_unread_chat(chat_id: int):
        """Сбросить статус прочтения (Невидимка) - пометить чат как непрочитанный"""
        conn = connect_db()
        c = conn.cursor()
        c.execute('UPDATE chats SET is_unread = 1 WHERE chat_id = ?', (chat_id,))
        conn.commit()
        conn.close()
        return JSONResponse({"status": "ok"})

    @app.post("/api/chats/{chat_id}/priority", dependencies=[Depends(verify_init_data)])
    def api_priority_chat(chat_id: int):
        """Переключить метку High Priority для чата"""
        conn = connect_db()
        c = conn.cursor()
        c.execute('UPDATE chats SET is_high_priority = NOT is_high_priority WHERE chat_id = ?', (chat_id,))
        c.execute('SELECT is_high_priority FROM chats WHERE chat_id = ?', (chat_id,))
        val = c.fetchone()[0]
        conn.commit()
        conn.close()
        return JSONResponse({"status": "ok", "is_high_priority": bool(val)})

    @app.post("/api/accounts/{account_id}/toggle_ai", dependencies=[Depends(verify_init_data)])
    def api_toggle_account_ai(account_id: str):
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
    def api_resume_chat_ai(chat_id: int):
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
    def api_toggle_chat_ai(chat_id: int):
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
    def api_get_ai_settings():
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

    @app.post("/api/ai_settings", dependencies=[Depends(verify_init_data)])
    def api_update_ai_settings(req: AISettingsRequest):
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

    @app.get("/api/operators", dependencies=[Depends(verify_init_data)])
    def api_get_operators():
        """Получить список всех операторов"""
        conn = connect_db()
        c = conn.cursor()
        c.execute('SELECT id, identity, is_super FROM operators')
        ops = [{"id": row[0], "identity": row[1], "is_super": bool(row[2])} for row in c.fetchall()]
        conn.close()
        return JSONResponse(ops)

    @app.post("/api/operators", dependencies=[Depends(verify_init_data)])
    def api_add_operator(req: OperatorRequest):
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
    def api_delete_operator(op_id: int):
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

    @app.get("/api/leads", dependencies=[Depends(verify_init_data)])
    def api_get_leads():
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
    def api_leads_stats():
        """Счётчики по этапам воронки (для сводки над доской)."""
        conn = connect_db()
        c = conn.cursor()
        c.execute('SELECT stage, COUNT(*) FROM leads GROUP BY stage')
        counts = {row[0]: row[1] for row in c.fetchall()}
        conn.close()
        return JSONResponse({"counts": {s: counts.get(s, 0) for s in LEAD_STAGES},
                             "total": sum(counts.values())})

    @app.post("/api/leads/{chat_id}/stage", dependencies=[Depends(verify_init_data)])
    def api_set_lead_stage(chat_id: int, req: LeadStageRequest):
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

    @app.get("/api/kb_gaps", dependencies=[Depends(verify_init_data)])
    def api_get_kb_gaps():
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
    def api_approve_kb_gap(gap_id: int):
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
    def api_dismiss_kb_gap(gap_id: int):
        """Отклонить кандидата (не добавлять в базу знаний)."""
        conn = connect_db()
        c = conn.cursor()
        c.execute("UPDATE kb_gaps SET status = 'dismissed' WHERE id = ?", (gap_id,))
        conn.commit()
        conn.close()
        return JSONResponse({"status": "ok"})
