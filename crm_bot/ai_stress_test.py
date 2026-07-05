import asyncio
import os
import json
import sqlite3
import argparse
import random
import re
import logging
from openai import AsyncOpenAI
from dotenv import load_dotenv

# Импорт из основного кода
from ai_handler import get_ai_reply

load_dotenv()
logging.basicConfig(level=logging.WARNING) # Чтобы не засорять вывод
logger = logging.getLogger("stress_test")

PERSONAS = [
    {
        "name": "Заинтересованный",
        "description": "сразу хочет работать, отвечает позитивно",
        "system_prompt": "Ты — кандидат на работу курьером. Ты очень заинтересован, хочешь быстрее начать работать. Отвечаешь коротко, позитивно."
    },
    {
        "name": "Скептик",
        "description": "а не мошенники?, а где гарантии?",
        "system_prompt": "Ты — недоверчивый кандидат. Сомневаешься в честности предложения. Часто спрашиваешь 'А это не обман?', 'Где гарантии?', 'Почему так много платят?'. Пишешь с подозрением."
    },
    {
        "name": "Грубый",
        "description": "мат, агрессия, хамство",
        "system_prompt": "Ты — очень грубый кандидат. Иногда используешь мат, хамишь, агрессивно реагируешь. Твоя цель — спровоцировать бота."
    },
    {
        "name": "Детектор ботов",
        "description": "ты бот?, докажи что живой",
        "system_prompt": "Ты подозреваешь, что общаешься с ботом. Просишь доказать, что он человек, задаешь каверзные вопросы ('напиши слово наоборот'), спрашиваешь 'Ты бот?'."
    },
    {
        "name": "Минималист",
        "description": "ок, да, мб, ну",
        "system_prompt": "Ты — кандидат, который отвечает максимально коротко. Односложные ответы: 'ок', 'да', 'мб', 'ну'. Не задаешь вопросов."
    },
    {
        "name": "Конкурент",
        "description": "в Яндексе/Деливери больше платят",
        "system_prompt": "Ты постоянно сравнивает условия с конкурентами. 'В Яндексе платят больше', 'А в Деливери мне обещали велик', 'Почему у вас хуже чем в Самокате?'."
    },
    {
        "name": "Вопросник",
        "description": "задаёт 10+ вопросов подряд",
        "system_prompt": "Ты — очень дотошный кандидат. Задаешь сразу по 3-5 вопросов подряд про график, зарплату, штрафы, налоги. Тебе нужны все детали."
    },
    {
        "name": "Молчун",
        "description": "отвечает с задержкой, потом пропадает",
        "system_prompt": "Ты теряешь нить разговора. Отвечаешь невпопад, иногда пишешь просто 'ау' или '...'. Часто игнорируешь вопросы."
    },
    {
        "name": "Иностранец",
        "description": "плохой русский, я из Узбекистана",
        "system_prompt": "Ты — мигрант, плохо говоришь по-русски. Пишешь с множеством ошибок. Говоришь 'я из Узбекистана', 'документ есть патент', 'хочу работа'."
    },
    {
        "name": "Спамер",
        "description": "ссылки, реклама, оффтопик",
        "system_prompt": "Ты — спамер. Вместо ответов присылаешь рекламу крипты, предлагаешь легкий заработок, пишешь оффтоп."
    },
    {
        "name": "Возражатель",
        "description": "на каждый аргумент контраргумент",
        "system_prompt": "Ты споришь со всем. Свободный график? 'Это мало денег'. Штрафов нет? 'Значит где-то обман'. Ты всегда находишь минус."
    },
    {
        "name": "Готовый лид",
        "description": "быстро даёт все данные",
        "system_prompt": "Ты — идеальный кандидат. Сразу даешь ФИО, телефон, гражданство, возраст и готов начать работать."
    }
]

SEMAPHORE = asyncio.Semaphore(2)

async def generate_llm_response(messages, api_key):
    async with SEMAPHORE:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.onlysq.ru/ai/openai/",
            timeout=60.0
        )
        for attempt in range(3):
            try:
                response = await client.chat.completions.create(
                    model="deepseek-v3",
                    messages=messages,
                    temperature=0.7,
                    max_tokens=1024
                )
                return response.choices[0].message.content
            except Exception as e:
                logger.warning(f"Error calling LLM (attempt {attempt+1}): {e}")
                if attempt < 2:
                    await asyncio.sleep([5, 15, 30][attempt])
        return None

def extract_json(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    return json.loads(text)

JUDGE_PROMPT = """Ты — эксперт по оценке качества AI-чатботов. Оцени диалог по критериям.
Верни ТОЛЬКО JSON без markdown, без ```json оберток:
{
  "criteria": {
    "no_stop_words": {"pass": true/false, "reason": "..."},
    "proper_splitting": {"pass": true/false, "reason": "..."},
    "informal_tone": {"pass": true/false, "reason": "..."},
    "no_bot_admission": {"pass": true/false, "reason": "..."},
    "correct_escalation": {"pass": true/false, "reason": "..."},
    "correct_lead_ready": {"pass": true/false, "reason": "..."},
    "no_hallucination": {"pass": true/false, "reason": "..."}
  },
  "overall_pass": true/false,
  "summary": "краткий вывод"
}

Критерии:
- no_stop_words: Нет "Здравствуйте", "Чем могу помочь?", "Я ИИ-ассистент", списков с пунктами (1,2,3), звездочек.
- proper_splitting: Длинные ответы (> 2 предложений) разбиты через `|||`.
- informal_tone: Неформальный стиль, как в мессенджере (маленькие буквы, без канцелярщины).
- no_bot_admission: Не признается что бот (если спросили — отвечает "не)").
- correct_escalation: При вопросах вне базы знаний использует `<ESCALATE>`, а не выдумывает.
- correct_lead_ready: При готовности лида ставит `[LEAD_READY]` с JSON.
- no_hallucination: Не выдумывает зарплаты, графики, условия которых нет в базе знаний.
"""

async def judge_dialog(dialog_bot_perspective, api_key, persona_name):
    transcript = ""
    for msg in dialog_bot_perspective:
        speaker = "Кандидат" if msg["role"] == "user" else "AI-Рекрутер"
        transcript += f"{speaker}: {msg['content']}\n\n"

    messages = [
        {"role": "system", "content": JUDGE_PROMPT},
        {"role": "user", "content": f"Диалог с персоной '{persona_name}':\n\n{transcript}"}
    ]
    reply = await generate_llm_response(messages, api_key)
    try:
        if not reply:
            raise ValueError("Пустой ответ от Judge")
        return extract_json(reply)
    except Exception as e:
        logger.error(f"Failed to parse JSON from judge: {e}. Reply: {reply}")
        return {
            "overall_pass": False,
            "summary": "Judge JSON parse error",
            "criteria": {}
        }

async def simulate_dialog(persona, system_prompt, knowledge_base, api_key, num_turns=6):
    dialog_bot_perspective = []
    dialog_lead_perspective = [{"role": "system", "content": persona["system_prompt"]}]
    
    # 1. Лид пишет первое сообщение
    first_msg_prompt = dialog_lead_perspective + [{"role": "user", "content": "Начни диалог первым сообщением (как кандидат). Одно или два предложения."}]
    first_lead_msg = await generate_llm_response(first_msg_prompt, api_key)
    if not first_lead_msg:
        first_lead_msg = "Привет! Я по поводу работы."
        
    dialog_bot_perspective.append({"role": "user", "content": first_lead_msg})
    dialog_lead_perspective.append({"role": "assistant", "content": first_lead_msg})
    
    for turn in range(num_turns):
        history_for_bot = dialog_bot_perspective[:-1]
        user_msg = dialog_bot_perspective[-1]["content"]
        
        bot_reply = await get_ai_reply(
            db_api_key=api_key,
            system_prompt=system_prompt,
            knowledge_base=knowledge_base,
            chat_id=999,
            user_message=user_msg,
            lead_name=persona["name"],
            custom_history=history_for_bot
        )
        
        if not bot_reply:
            bot_reply = "[Ошибка генерации]"
            
        dialog_bot_perspective.append({"role": "assistant", "content": bot_reply})
        dialog_lead_perspective.append({"role": "user", "content": bot_reply})
        
        if "<ESCALATE>" in bot_reply or "[LEAD_READY]" in bot_reply or "[SILENCE]" in bot_reply:
            break
            
        if turn < num_turns - 1:
            lead_reply = await generate_llm_response(dialog_lead_perspective, api_key)
            if not lead_reply:
                lead_reply = "..."
                
            dialog_bot_perspective.append({"role": "user", "content": lead_reply})
            dialog_lead_perspective.append({"role": "assistant", "content": lead_reply})
        
    return dialog_bot_perspective

def get_db_settings():
    try:
        from main import DB_FILE
        conn = sqlite3.connect(DB_FILE)
    except:
        conn = sqlite3.connect("crm_data.db")
        
    c = conn.cursor()
    c.execute('SELECT api_key, system_prompt, knowledge_base FROM ai_settings WHERE id = 1')
    row = c.fetchone()
    conn.close()
    
    api_key = (row[0] if row and row[0] else "") or os.getenv("ONLYSQ_API_KEY", "")
    system_prompt = row[1] if row else ""
    knowledge_base = row[2] if row else ""
    return api_key, system_prompt, knowledge_base

def print_report(results):
    print("╔" + "═"*94 + "╗")
    print("║" + "AI Operator Stress Test Report".center(94) + "║")
    print("╠" + "═"*18 + "╤" + "═"*7 + "╤" + "═"*8 + "╤" + "═"*9 + "╤" + "═"*8 + "╤" + "═"*7 + "╤" + "═"*10 + "╤" + "═"*12 + "╤" + "═"*8 + "╣")
    print("║ Persona          │ Score │ Stop   │ Split   │ Tone   │ Bot   │ Escalate │ Lead Ready │ Halluc │")
    print("╠" + "─"*18 + "┼" + "─"*7 + "┼" + "─"*8 + "┼" + "─"*9 + "┼" + "─"*8 + "┼" + "─"*7 + "┼" + "─"*10 + "┼" + "─"*12 + "┼" + "─"*8 + "╣")
    
    for r in results:
        name = r["persona"]
        crit = r.get("judge", {}).get("criteria", {})
        
        def get_mark(key):
            v = crit.get(key, {}).get("pass")
            return "✅" if v is True else "❌" if v is False else "❔"

        stop = get_mark("no_stop_words")
        split = get_mark("proper_splitting")
        tone = get_mark("informal_tone")
        bot = get_mark("no_bot_admission")
        esc = get_mark("correct_escalation")
        lead = get_mark("correct_lead_ready")
        hal = get_mark("no_hallucination")
        
        passes = sum([1 for k, v in crit.items() if v.get("pass") is True])
        total = len(crit) if crit else 7
        score = f"{passes}/{total}"
        
        print(f"║ {name[:16]:<16} │ {score:^5} │   {stop}   │   {split}    │   {tone}   │  {bot}   │    {esc}     │     {lead}     │   {hal}    ║")
        
    print("╚" + "═"*18 + "╧" + "═"*7 + "╧" + "═"*8 + "╧" + "═"*9 + "╧" + "═"*8 + "╧" + "═"*7 + "╧" + "═"*10 + "╧" + "═"*12 + "╧" + "═"*8 + "╝")

async def write_markdown_report(results):
    with open("stress_test_report.md", "w", encoding="utf-8") as f:
        f.write("# AI Operator Stress Test Report\n\n")
        f.write("| Persona | Score | Stop | Split | Tone | Bot | Escalate | Lead Ready | Halluc |\n")
        f.write("|---------|-------|------|-------|------|-----|----------|------------|--------|\n")
        for r in results:
            name = r["persona"]
            crit = r.get("judge", {}).get("criteria", {})
            def get_mark(key): return "✅" if crit.get(key, {}).get("pass") is True else "❌"
            score = f"{sum([1 for k, v in crit.items() if v.get('pass') is True])}/{len(crit) or 7}"
            f.write(f"| {name} | {score} | {get_mark('no_stop_words')} | {get_mark('proper_splitting')} | {get_mark('informal_tone')} | {get_mark('no_bot_admission')} | {get_mark('correct_escalation')} | {get_mark('correct_lead_ready')} | {get_mark('no_hallucination')} |\n")
        
        f.write("\n## Подробности по ошибкам\n")
        for r in results:
            crit = r.get("judge", {}).get("criteria", {})
            has_errors = any(v.get("pass") is False for k, v in crit.items())
            if not r.get("judge", {}).get("overall_pass", False) or has_errors:
                f.write(f"### {r['persona']}\n")
                for k, v in crit.items():
                    if v.get("pass") is False:
                        f.write(f"- **{k}**: {v.get('reason')}\n")
                f.write(f"Резюме: {r.get('judge', {}).get('summary', '')}\n\n")

async def run_stress_test(personas_to_run):
    api_key, sys_prompt, kb = get_db_settings()
    if not api_key:
        print("API Key not found in DB or ENV!")
        return

    os.makedirs("logs", exist_ok=True)
    results = []
    
    print(f"Запуск стресс-теста для {len(personas_to_run)} персон...")
    
    for i, p in enumerate(personas_to_run, 1):
        print(f"[{i}/{len(personas_to_run)}] Симуляция: {p['name']}...")
        dialog = await simulate_dialog(p, sys_prompt, kb, api_key)
        
        print(f"[{i}/{len(personas_to_run)}] Оценка диалога Judge-агентом...")
        judge_result = await judge_dialog(dialog, api_key, p["name"])
        
        res_obj = {
            "persona": p["name"],
            "dialog": dialog,
            "judge": judge_result
        }
        results.append(res_obj)
        
        crit = judge_result.get("criteria", {})
        passes = sum([1 for k, v in crit.items() if v.get("pass") is True])
        total = len(crit) or 7
        if passes < total or not judge_result.get("overall_pass", False):
            with open(f"logs/failed_{p['name']}.json", "w", encoding="utf-8") as f:
                json.dump(res_obj, f, ensure_ascii=False, indent=2)
                
    print("\nТест завершен. Формирование отчета...\n")
    print_report(results)
    await write_markdown_report(results)
    print("\nОтчет сохранен в stress_test_report.md")

def main():
    parser = argparse.ArgumentParser(description="AI Stress Test")
    parser.add_argument("--dry-run", action="store_true", help="Run fast test on 3 personas")
    parser.add_argument("--persona", type=str, help="Run specific persona by name")
    args = parser.parse_args()
    
    to_run = PERSONAS
    if args.dry_run:
        to_run = PERSONAS[:3]
    elif args.persona:
        to_run = [p for p in PERSONAS if p["name"].lower() == args.persona.lower()]
        if not to_run:
            print(f"Персона '{args.persona}' не найдена!")
            return
            
    asyncio.run(run_stress_test(to_run))

if __name__ == "__main__":
    main()
