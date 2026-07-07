import os
import asyncio
import google.generativeai as genai

# Load API key from env (assumes user has GEMINI_API_KEY in .env)
_api_key = os.getenv("GEMINI_API_KEY")

if _api_key:
    genai.configure(api_key=_api_key)
    # Using lightweight flash model for quick chatting
    model = genai.GenerativeModel('gemini-1.5-flash')
else:
    model = None

async def generate_reply(history_context: str) -> str:
    """
    Generates an automated contextual reply for Telegram chats.
    Prompts Gemini with recent conversation history.
    """
    if not model:
        return "⚠️ GEMINI_API_KEY не задан в .env. Добавьте его для работы ИИ."

    prompt = (
        "Ты — помощник, который должен помочь пользователю сформировать ответ в Telegram-переписке.\n"
        "Ниже предоставлена история сообщений (от старых к новым). Постарайся написать короткий, естественный "
        "и вежливый ответ на последнее сообщение. Стиль должен быть неформальным, но профессиональным, подходящим "
        "для общения с кандидатами, курьерами или клиентами. Пиши ТОЛЬКО текст ответа, без пояснений.\n\n"
        "### История Чата ###\n"
        + history_context + "\n"
        "### Твой ответ: ###"
    )

    try:
        # Синхронный SDK Gemini — уводим в поток, чтобы не блокировать event loop.
        response = await asyncio.to_thread(model.generate_content, prompt)
        return response.text.strip()
    except Exception as e:
        return f"❌ Ошибка ИИ: {e}"
