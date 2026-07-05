import sqlite3
import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot

# Загружаем переменные окружения из файла .env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    print("Не задан BOT_TOKEN в .env")
    exit(1)

bot = Bot(token=BOT_TOKEN)
DB_FILE = 'crm_data.db'

async def sync_data():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Получаем все уникальные business_connection_id из старой таблицы topics
    c.execute('SELECT DISTINCT business_connection_id FROM topics')
    connections = c.fetchall()
    
    print(f"Найдено уникальных бизнес-подключений: {len(connections)}")
    
    for (conn_id,) in connections:
        try:
            conn_info = await bot.get_business_connection(conn_id)
            user = conn_info.user
            business_name = f"{user.first_name} {user.last_name or ''}".strip()
            
            # Сохраняем аккаунт
            c.execute('''
                INSERT OR REPLACE INTO accounts (business_connection_id, business_name, user_id) 
                VALUES (?, ?, ?)
            ''', (conn_id, business_name, user.id))
            print(f"✅ Аккаунт '{business_name}' ({conn_id}) добавлен.")
            
        except Exception as e:
            print(f"❌ Ошибка получения инфо для {conn_id}: {e}")
            
    # Получаем все чаты из topics и добавляем их в chats, если их там нет
    c.execute('SELECT business_connection_id, chat_id FROM topics')
    chats = c.fetchall()
    
    for conn_id, chat_id in chats:
        c.execute('SELECT 1 FROM chats WHERE chat_id = ?', (chat_id,))
        if not c.fetchone():
            c.execute('''
                INSERT OR IGNORE INTO chats (chat_id, business_connection_id, lead_name) 
                VALUES (?, ?, ?)
            ''', (chat_id, conn_id, f"User {chat_id}"))
            print(f"✅ Чат {chat_id} синхронизирован.")
            
    conn.commit()
    conn.close()
    await bot.session.close()

if __name__ == '__main__':
    asyncio.run(sync_data())
