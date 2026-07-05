import asyncio
import json
import logging
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, File, UploadFile, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from pydantic import BaseModel

from .auth import validate_init_data, validate_auth_token
from .llm import generate_reply
from config import SESSIONS_DIR
import config
from proxy_manager import proxy_to_pyrogram

ws_logger = logging.getLogger("tg_combiner.webapp")

# Timeout for heavy Pyrogram API calls (seconds)
_PYROGRAM_TIMEOUT = 30

# We need a shared dict of running pyrogram clients from main.py
running_clients: Dict[str, "Client"] = {} 

# Global reference to the bot client (set by main.py after bot starts)
bot_client: Optional["Client"] = None

app = FastAPI(title="TG Combiner Console")

# Mount Static Files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/app", response_class=HTMLResponse)
async def serve_app():
    """Serve index.html with no-cache headers to always deliver latest version."""
    index_path = static_dir / "index.html"
    content = index_path.read_text(encoding="utf-8")
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    })

# Avatar cache directory
_AVATAR_CACHE_DIR = static_dir / "avatars"
_AVATAR_CACHE_DIR.mkdir(exist_ok=True)

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def send_json(self, websocket: WebSocket, data: dict):
        await websocket.send_json(data)

manager = ConnectionManager()

async def broadcast_new_message(session_name: str, msg):
    """Called from main.py Pyrogram handler to push new messages to UI"""
    if not manager.active_connections:
        return
    
    payload = {
        "type": "new_message",
        "session": session_name,
        "chat_id": msg.chat.id,
        "data": {
            "id": msg.id,
            "text": msg.text or "[Media]",
            "out": msg.outgoing,
            "date": msg.date.strftime("%H:%M") if msg.date else "Now"
        }
    }
    dead: list[WebSocket] = []
    for ws in manager.active_connections:
        try:
            await manager.send_json(ws, payload)
        except Exception:
            dead.append(ws)
    # Purge dead connections so we don't iterate over them every time
    for ws in dead:
        try:
            manager.disconnect(ws)
        except ValueError:
            pass

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # For a real production app we would enforce auth parsing first
    # Example: init_data passed in first payload
    await manager.connect(websocket)
    websocket.auth_passed = False # Initialize auth status for this websocket

    try:
        while True:
            raw_data = await websocket.receive_text()

            # Guard against malformed JSON from the client
            try:
                payload = json.loads(raw_data)
            except (json.JSONDecodeError, ValueError):
                await manager.send_json(websocket, {"type": "error", "message": "Invalid JSON"})
                continue

            action = payload.get("action")
            
            try:
                ws_logger.info(f"WS Payload received: action={action}")
                
                if action != "auth" and not websocket.auth_passed:
                    continue

                if action == "auth":
                    init_data = payload.get("initData", "")
                    fallback_uid_raw = payload.get("fallback_uid")
                    ws_logger.info(f"AUTH attempt: initData_len={len(init_data)}, initData_preview='{init_data[:80]}', fallback_uid={fallback_uid_raw}")
                    auth_ok = False
                    
                    if init_data:
                        auth_ok = validate_init_data(init_data)
                    
                    # Fallback: if initData is empty (Menu Button launch),
                    # try HMAC auth_token from URL query parameter
                    if not auth_ok:
                        auth_token = payload.get("auth_token", "")
                        if auth_token:
                            auth_ok = validate_auth_token(auth_token)
                            ws_logger.info(f"Token auth result: {auth_ok}")
                    
                    if auth_ok:
                        websocket.auth_passed = True
                        from modules.sender import get_session_files
                        sessions = get_session_files()
                        
                        data_s = []
                        for s in sessions:
                            name = s.stem
                            client = running_clients.get(name)
                            if client and client.is_connected:
                                status = "🟢 В сети"
                            else:
                                status = "🔴 Спит"
                            data_s.append({"name": name, "status": status})
                            
                        await manager.send_json(websocket, {"type": "sessions_list", "data": data_s})
                    else:
                        websocket.auth_passed = False
                        await manager.send_json(websocket, {"type": "auth_failed"})

                # Load chats (with timeout to prevent WS hang)
                elif action == "load_chats":
                    session_name = payload.get("session_name")
                    client = running_clients.get(session_name)
                    chats = []
                    
                    if client:
                        try:
                            async def _fetch_dialogs():
                                result = []
                                async for dialog in client.get_dialogs(limit=25):
                                    chat = dialog.chat
                                    # Build display name
                                    if chat.title:
                                        chat_name = chat.title
                                    elif chat.first_name:
                                        parts = [chat.first_name]
                                        if chat.last_name:
                                            parts.append(chat.last_name)
                                        chat_name = " ".join(parts)
                                    else:
                                        chat_name = "Unknown"

                                    # Username with @ prefix
                                    uname = f"@{chat.username}" if chat.username else None

                                    # Initials (1-2 uppercase letters)
                                    name_parts = chat_name.split()
                                    if len(name_parts) >= 2:
                                        initials = (name_parts[0][0] + name_parts[1][0]).upper()
                                    else:
                                        initials = chat_name[:2].upper()

                                    # Chat type as string
                                    chat_type = str(chat.type).split(".")[-1].lower() if chat.type else "private"

                                    result.append({
                                        "id": chat.id,
                                        "name": chat_name,
                                        "username": uname,
                                        "initials": initials,
                                        "chat_type": chat_type,
                                        "has_photo": bool(chat.photo),
                                        "unread_count": dialog.unread_messages_count,
                                        "last_msg": dialog.top_message.text[:50] if dialog.top_message and dialog.top_message.text else "Media/Other"
                                    })
                                return result
                            chats = await asyncio.wait_for(_fetch_dialogs(), timeout=_PYROGRAM_TIMEOUT)
                        except asyncio.TimeoutError:
                            ws_logger.warning(f"get_dialogs timeout for {session_name}")
                            await manager.send_json(websocket, {"type": "error", "message": "Timeout loading chats"})
                            continue
                    
                    await manager.send_json(websocket, {"type": "chats_list", "session": session_name, "data": chats})

                # Load message history (Hidden read, with timeout)
                elif action == "load_history":
                    session_name = payload.get("session_name")
                    chat_id_raw = payload.get("chat_id")
                    ws_logger.info(f"load_history requested for {session_name} in chat {chat_id_raw}")
                    # Validate chat ID because JS might send it as a string
                    try:
                        chat_id = int(chat_id_raw)
                    except (TypeError, ValueError):
                        await manager.send_json(websocket, {"type": "error", "message": "Invalid chat_id"})
                        continue
                    client = running_clients.get(session_name)
                    
                    if client:
                        try:
                            async def _fetch_history():
                                result = []
                                # We do NOT call read_history(), so it remains hidden
                                async for msg in client.get_chat_history(chat_id, limit=30):
                                    if hasattr(msg, "date") and msg.date:
                                        if hasattr(msg.date, "strftime"):
                                            d_str = msg.date.strftime("%H:%M")
                                        else:
                                            d_str = datetime.fromtimestamp(msg.date).strftime("%H:%M")
                                    else:
                                        d_str = "Now"
                                    out_flag = getattr(msg, "outgoing", False)
                                    text_content = getattr(msg, "text", "") or getattr(msg, "caption", "") or "[Media]"
                                    result.append({
                                        "id": msg.id,
                                        "text": text_content,
                                        "out": out_flag,
                                        "date": d_str
                                    })
                                return result
                            msgs = await asyncio.wait_for(_fetch_history(), timeout=_PYROGRAM_TIMEOUT)
                        except asyncio.TimeoutError:
                            ws_logger.warning(f"get_chat_history timeout for {session_name}/{chat_id}")
                            await manager.send_json(websocket, {"type": "error", "message": "Timeout loading history"})
                            continue

                        ws_logger.info(f"Got {len(msgs)} messages. Sending to WS.")
                        msgs.reverse()  # Chronological

                        # Try to get user status for the dialog header
                        user_status = None
                        try:
                            peer = await asyncio.wait_for(client.get_chat(chat_id), timeout=5)
                            raw_status = getattr(peer, 'status', None)
                            if raw_status:
                                status_str = str(raw_status).split('.')[-1].upper()
                                status_map = {
                                    'ONLINE': 'В сети',
                                    'OFFLINE': 'Не в сети',
                                    'RECENTLY': 'Недавно',
                                    'LAST_WEEK': 'На этой неделе',
                                    'LAST_MONTH': 'В этом месяце',
                                    'LONG_TIME_AGO': 'Давно',
                                }
                                user_status = status_map.get(status_str, None)
                        except Exception:
                            pass

                        await manager.send_json(websocket, {
                            "type": "history_loaded",
                            "chat_id": chat_id,
                            "data": msgs,
                            "user_status": user_status
                        })
                        ws_logger.info("Sent history payload to WS.")
                    else:
                        ws_logger.error(f"Client not found for session_name: {session_name}")

                # Generate AI Reply
                elif action == "generate_reply":
                    history = payload.get("history_context", "")
                    reply = await generate_reply(history)
                    await manager.send_json(websocket, {"type": "ai_reply", "text": reply})

                # Send a real message
                elif action == "send_message":
                    session_name = payload.get("session_name")
                    chat_id = payload.get("chat_id")
                    text = payload.get("text")
                    client = running_clients.get(session_name)
                    
                    if client:
                        try:
                            await asyncio.wait_for(
                                client.send_message(chat_id, text),
                                timeout=_PYROGRAM_TIMEOUT
                            )
                            await manager.send_json(websocket, {"type": "message_sent", "chat_id": chat_id})
                        except asyncio.TimeoutError:
                            await manager.send_json(websocket, {"type": "error", "message": "Timeout sending message"})

            except Exception as e:
                ws_logger.error(f"Error handling action '{action}': {e}", exc_info=True)
                try:
                    await manager.send_json(websocket, {"type": "error", "message": str(e)})
                except Exception:
                    pass  # WS might already be dead

    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ── Direct Media Sender REST API ───────────────────────────────────────

async def _broadcast_dm_progress(payload: dict):
    """Push DM progress events to all connected WebSocket clients."""
    dead: list[WebSocket] = []
    for ws in manager.active_connections:
        try:
            await manager.send_json(ws, payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            manager.disconnect(ws)
        except ValueError:
            pass


# ── Avatar REST endpoint ───────────────────────────────────────────────

@app.get("/api/avatar/{session_name}/{chat_id}")
async def api_get_avatar(session_name: str, chat_id: int):
    """Download and cache avatar photo for a given chat."""
    cache_path = _AVATAR_CACHE_DIR / f"{chat_id}.jpg"

    if cache_path.exists():
        return FileResponse(cache_path, media_type="image/jpeg")

    client = running_clients.get(session_name)
    if not client or not client.is_connected:
        return JSONResponse({"error": "no session"}, status_code=404)

    try:
        chat = await asyncio.wait_for(client.get_chat(chat_id), timeout=10)
        if chat.photo:
            await asyncio.wait_for(
                client.download_media(
                    chat.photo.small_file_id,
                    file_name=str(cache_path)
                ),
                timeout=10
            )
            if cache_path.exists():
                return FileResponse(cache_path, media_type="image/jpeg")
    except Exception as e:
        ws_logger.warning(f"Avatar download failed for {chat_id}: {e}")

    return JSONResponse({"error": "no avatar"}, status_code=404)


@app.get("/api/contacts")
async def api_get_contacts():
    import os, json
    db_path = "contacts.json"
    if os.path.exists(db_path):
        try:
            with open(db_path, "r", encoding="utf-8") as f:
                contacts = json.load(f)
            return {"contacts": contacts}
        except Exception:
            return {"contacts": []}
    return {"contacts": []}

@app.post("/api/direct-send")
async def api_direct_send(
    session_name: str = Form(...),
    recipients: str = Form(...),
    text: str = Form(""),
    file: Optional[UploadFile] = File(None),
):
    """
    Accept a multipart form with session_name, newline-separated recipients,
    message text, and an optional media file.  Spawns the send loop as a
    background task and returns a task_id for progress tracking via WS.
    """
    recipient_list = [r.strip() for r in recipients.splitlines() if r.strip()]
    if not recipient_list:
        return JSONResponse({"error": "Список получателей пуст"}, status_code=400)

    if session_name not in running_clients:
        return JSONResponse({"error": "Сессия не найдена или оффлайн"}, status_code=400)

    # Save uploaded file to a temp location (if any)
    media_path: Optional[str] = None
    if file and file.filename:
        suffix = Path(file.filename).suffix
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir="/tmp")
        content = await file.read()
        tmp.write(content)
        tmp.close()
        media_path = tmp.name

    if not text.strip() and not media_path:
        return JSONResponse({"error": "Укажите текст или прикрепите файл"}, status_code=400)

    task_id = str(uuid.uuid4())[:8]

    # Import here to avoid circular import at module level
    from modules.direct_sender import run_direct_send

    asyncio.create_task(
        run_direct_send(
            task_id=task_id,
            session_name=session_name,
            recipients=recipient_list,
            text=text,
            media_path=media_path,
            broadcast_fn=_broadcast_dm_progress,
        )
    )

    return {"task_id": task_id, "total": len(recipient_list)}

@app.post("/api/start-parser")
async def api_start_parser(payload: dict):
    """
    Launch Smart AI-Parser from WebApp.
    """
    import config
    from modules.parser import SmartParserConfig, run_smart_parser_task
    from modules.smart_parser_menu import _get_config

    target_chats = payload.get("target_chats", [])
    if not target_chats:
        return JSONResponse({"error": "Список источников пуст"}, status_code=400)

    admin_id = config.ADMIN_ID
    if not admin_id:
        return JSONResponse({"error": "Admin ID не настроен"}, status_code=400)

    # Get the last saved gemini key from FSM states
    bot_cfg = _get_config(admin_id)
    gemini_key = bot_cfg.get("gemini_key", "")

    # We use the first running client to send progress reports if no bot is provided
    # since we don't have the global bot instance here.
    if not running_clients:
        return JSONResponse({"error": "Нет запущенных сессий. Добавьте аккаунт."}, status_code=400)
        
    session_name = list(running_clients.keys())[0]
    # Use the bot client for sending notifications (not the worker account)
    reporting_client = bot_client if bot_client and bot_client.is_connected else running_clients[session_name]

    smart_cfg = SmartParserConfig(
        days_depth=int(payload.get("depth_days", 30)),
        age_min=int(payload.get("age_min", 16)),
        age_max=int(payload.get("age_max", 25)),
        city=payload.get("city", ""),
        strict_location=payload.get("strict_location", True),
        require_experience=payload.get("require_experience", False),
        gemini_api_key=gemini_key,
        strictness=int(payload.get("strictness", 50)),
        slang_threshold=int(payload.get("slang_threshold", 2)),
        custom_prompt=payload.get("custom_prompt", "").strip(),
    )

    task_data = {
        "target_chats": target_chats,
        "selected_session": session_name,
        "smart_config": smart_cfg,
    }

    ws_logger.info(f"Starting Smart Parser on {len(target_chats)} chats from WebApp")

    asyncio.create_task(
        run_smart_parser_task(
            bot=reporting_client, 
            admin_id=admin_id, 
            task_data=task_data
        )
    )

    return {"status": "ok"}
