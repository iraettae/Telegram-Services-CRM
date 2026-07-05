import asyncio
import sqlite3
import os
from dotenv import load_dotenv
from aiogram import Bot

# Загружаем переменные окружения
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_FILE = "crm_data.db"

bot = Bot(token=BOT_TOKEN)

async def fix_names():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT chat_id, lead_name FROM chats WHERE lead_name LIKE 'User %'")
    rows = c.fetchall()
    
    print(f"Chats to fix: {len(rows)}")
    for row in rows:
        chat_id, old_name = row
        try:
            # We try to get chat info using bot
            chat = await bot.get_chat(chat_id)
            new_name = chat.full_name
            if new_name:
                c.execute("UPDATE chats SET lead_name = ? WHERE chat_id = ?", (new_name, chat_id))
                print(f"Fixed {old_name} -> {new_name}")
        except Exception as e:
            print(f"Could not fix {old_name}: {e}")
            
    conn.commit()
    conn.close()
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(fix_names())
