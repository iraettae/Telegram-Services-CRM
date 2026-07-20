#!/usr/bin/env python3
"""Import missing dialogs from Pyrogram session into CRM database."""
import asyncio
import json
import sqlite3
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.enums import ChatType

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)

# ── Конфигурация ───────────────────────────────────────────────────────
# Всё берём из .env: раньше здесь лежали живые api_id/api_hash, имя сессии
# (это номер телефона аккаунта) и business_connection_id рабочих учёток —
# и вместе с файлом уехали в публичный репозиторий.
API_ID = (os.getenv("USERBOT_API_ID") or "").strip()
API_HASH = (os.getenv("USERBOT_API_HASH") or "").strip()
SESSION_NAME = (os.getenv("USERBOT_SESSION") or "").strip()
WORKDIR = (os.getenv("USERBOT_SESSIONS_DIR") or "").strip()
DB_PATH = os.getenv("DB_PATH", "crm_data.db")

# {telegram_user_id: business_connection_id} в виде JSON — какому аккаунту
# приписывать импортированные чаты.
BC_IDS_RAW = (os.getenv("USERBOT_BC_IDS") or "").strip()


def _load_config():
    """Проверяем окружение ДО коннекта: скрипт разовый и пишет прямо в боевую
    БД — пустой api_id или чужой bc_id тут дороже, чем отказ запуститься."""
    missing = [
        name for name, value in (
            ("USERBOT_API_ID", API_ID),
            ("USERBOT_API_HASH", API_HASH),
            ("USERBOT_SESSION", SESSION_NAME),
            ("USERBOT_SESSIONS_DIR", WORKDIR),
            ("USERBOT_BC_IDS", BC_IDS_RAW),
        ) if not value
    ]
    if missing:
        raise SystemExit(
            f"import_dialogs: не заданы {', '.join(missing)} в {ENV_FILE} "
            "(см. .env.example)"
        )
    try:
        api_id = int(API_ID)
    except ValueError:
        raise SystemExit(f"import_dialogs: USERBOT_API_ID должен быть числом, а не {API_ID!r}")
    try:
        bc_ids = {int(k): str(v) for k, v in json.loads(BC_IDS_RAW).items()}
    except (ValueError, AttributeError) as e:
        raise SystemExit(
            f"import_dialogs: USERBOT_BC_IDS должен быть JSON вида "
            f'{{"123456789": "business_connection_id"}} ({e})'
        )
    if not bc_ids:
        raise SystemExit("import_dialogs: USERBOT_BC_IDS пуст — не к чему привязать чаты")
    return api_id, bc_ids


async def main():
    API_ID, BC_IDS = _load_config()
    print("Connecting to Pyrogram session...")
    app = Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH, workdir=WORKDIR)
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    try:
        await app.start()
        me = await app.get_me()
        print(f"Connected as {me.first_name} (ID: {me.id})")
        
        default_bc_id = BC_IDS.get(me.id)
        if not default_bc_id:
            print(f"Warning: unknown user ID {me.id}, cannot determine default bc_id.")
            default_bc_id = "unknown_bc_id"
        else:
            print(f"Mapped to business_connection_id: {default_bc_id}")
            
        target_date = datetime(2026, 3, 21, tzinfo=timezone.utc)
        print(f"Target date for import: {target_date}")
        
        dialogs_found = 0
        dialogs_imported = 0
        messages_inserted = 0
        
        async for dialog in app.get_dialogs():
            chat = dialog.chat
            if chat.type not in (ChatType.PRIVATE, ChatType.BOT):
                continue
                
            last_msg_date = dialog.last_message.date if dialog.last_message else None
            if not last_msg_date:
                continue
                
            # Stop if we reached dialogs older than target date
            if last_msg_date < target_date:
                break
                
            dialogs_found += 1
            chat_id = chat.id
            
            c.execute("SELECT business_connection_id, last_message_time FROM chats WHERE chat_id = ?", (chat_id,))
            chat_row = c.fetchone()
            
            # Fetch messages from history until we hit DB's last_message_time or target_date
            # Pyrogram history is newest first
            history = []
            async for msg in app.get_chat_history(chat_id):
                if not msg.date or msg.date < target_date:
                    break
                    
                if chat_row and chat_row['last_message_time']:
                    # Compare with DB last message time. If DB has UTC, parse it
                    db_last_time_str = chat_row['last_message_time']
                    try:
                        # Assuming format 'YYYY-MM-DD HH:MM:SS'
                        db_last_time = datetime.strptime(db_last_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        if msg.date <= db_last_time:
                            break
                    except Exception as e:
                        pass
                
                # Only insert text messages (or with text caption) for now
                text = msg.text or msg.caption or ""
                history.append(msg)
                
            if not history:
                continue # No new messages
                
            # Reverse history to insert oldest first
            history.reverse()
            
            if not chat_row:
                # Need to insert chat
                lead_name = f"{chat.first_name or ''} {chat.last_name or ''}".strip()
                if not lead_name:
                    lead_name = "Unknown"
                    
                c.execute("""
                    INSERT INTO chats (chat_id, business_connection_id, lead_name, last_message_time, is_unread, ai_paused)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (chat_id, default_bc_id, lead_name, history[-1].date.strftime("%Y-%m-%d %H:%M:%S"), 1, 0))
            else:
                # Update last_message_time
                c.execute("""
                    UPDATE chats 
                    SET last_message_time = ?, is_unread = 1 
                    WHERE chat_id = ?
                """, (history[-1].date.strftime("%Y-%m-%d %H:%M:%S"), chat_id))
                
            dialogs_imported += 1
            
            for msg in history:
                text = msg.text or msg.caption or ""
                is_outgoing = 1 if msg.out else 0
                msg_time = msg.date.strftime("%Y-%m-%d %H:%M:%S")
                
                # Check if message already exists just in case
                c.execute("SELECT id FROM messages WHERE chat_id = ? AND timestamp = ? AND text = ?", 
                          (chat_id, msg_time, text))
                if not c.fetchone():
                    c.execute("""
                        INSERT INTO messages (chat_id, text, is_outgoing, timestamp)
                        VALUES (?, ?, ?, ?)
                    """, (chat_id, text, is_outgoing, msg_time))
                    messages_inserted += 1
                    
        conn.commit()
        print(f"Import finished!")
        print(f"Dialogs found (since {target_date.date()}): {dialogs_found}")
        print(f"Dialogs with new messages imported: {dialogs_imported}")
        print(f"Total messages inserted: {messages_inserted}")
        
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await app.stop()
        conn.close()

if __name__ == '__main__':
    asyncio.run(main())
