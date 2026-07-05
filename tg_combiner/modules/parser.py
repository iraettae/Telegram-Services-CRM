import asyncio
import logging
from typing import Optional, List, Dict, Any
from pyrogram import Client
from pyrogram.enums import ChatType
from pyrogram.types import User
from pyrogram.errors import FloodWait
import pandas as pd
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("tg_combiner.parser")

def _utc_now_naive():
    """Return current UTC time as naive datetime (matches Pyrogram's naive UTC datetimes)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

async def _ensure_joined(client: Client, chat_id: int|str):
    """Try to access the chat; if it fails, attempt to join it."""
    try:
        await client.get_chat(chat_id)
        logger.info(f"_ensure_joined: already have access to {chat_id}")
    except Exception as e:
        err_str = str(e).upper()
        logger.warning(f"_ensure_joined: cannot access {chat_id}: {e}")
        if any(kw in err_str for kw in ["PRIVATE", "PARTICIPANT", "FORBIDDEN", "400", "403", "INVITE"]):
            logger.info(f"_ensure_joined: attempting join_chat({chat_id})")
            try:
                await client.join_chat(chat_id)
                logger.info(f"_ensure_joined: successfully joined {chat_id}")
            except Exception as je:
                logger.error(f"_ensure_joined: join_chat failed: {je}")
                raise je
        else:
            raise e


async def _safe_get_history(client: Client, chat_id: int|str, message_thread_id: int|None, limit: int = 0):
    """Iterate chat messages. For topics uses get_discussion_replies, otherwise get_chat_history.
    Auto-joins the chat if not a member. Handles FloodWait by letting it bubble up."""
    await _ensure_joined(client, chat_id)

    if message_thread_id:
        logger.info(f"_safe_get_history: reading topic {message_thread_id} in {chat_id} (limit={limit})")
        async for msg in client.get_discussion_replies(chat_id, message_thread_id, limit=limit):
            yield msg
    else:
        logger.info(f"_safe_get_history: reading chat history of {chat_id} (limit={limit})")
        async for msg in client.get_chat_history(chat_id, limit=limit):
            yield msg


class ParserManager:
    """
    Advanced Pyrogram Parser.
    Supports 5 modes: members, comments, history, reactions, polls.
    Applies filters (online status, avatar, premium, sex).
    Exports to Excel.
    """
    def __init__(self, clients: List[Client]):
        self.clients = clients

    def _passes_filters(self, user: User, filters: dict) -> bool:
        if not user or user.is_bot or user.is_deleted:
            return False
            
        # Avatar filter
        if filters.get("require_photo") and not user.photo:
            return False
            
        # Premium filter
        if filters.get("require_premium") and not user.is_premium:
            return False
            
        # Sex filter (heuristic russian names)
        sex = filters.get("sex", "all")
        if sex in ("male", "female"):
            name = (user.first_name or "").lower()
            if sex == "male":
                if any(name.endswith(end) for end in ["а", "я", "ova", "eva"]): # very naive heuristic
                    return False
            elif sex == "female":
                if not any(name.endswith(end) for end in ["а", "я", "ova", "eva"]):
                    return False

        from pyrogram.enums import UserStatus
        
        # Online Date filter
        online_filter = filters.get("online_filter", "all")
        if online_filter == "1d":
            if user.status not in [UserStatus.ONLINE, UserStatus.RECENTLY]:
                return False
        elif online_filter == "7d":
            if user.status not in [UserStatus.ONLINE, UserStatus.RECENTLY, UserStatus.LAST_WEEK]:
                return False
        elif online_filter == "30d":
            if user.status in [UserStatus.LONG_AGO, UserStatus.EMPTY]:
                return False

        return True

    def _format_user(self, user: User, source: str) -> dict:
        return {
            "ID": user.id,
            "Имя": user.first_name or "",
            "Фамилия": user.last_name or "",
            "Username": user.username or "",
            "Телефон": user.phone_number or "Скрыт",
            "Премиум": "Да" if user.is_premium else "Нет",
            "Аватарка": "Да" if user.photo else "Нет",
            "Источник": source,
            "Сборка": datetime.now().strftime("%Y-%m-%d %H:%M")
        }

    async def parse_members(self, chat_id: str|int, filters: dict, max_users: int = 0) -> List[dict]:
        """Mode 1: Group Members — uses alphabet search trick for full extraction"""
        import string
        client = self.clients[0]
        results = []
        seen = set()
        bots_skipped = 0
        filtered_out = 0
        
        # Telegram only returns ~200 members per search query.
        # To get everyone, we iterate through every letter of latin + cyrillic alphabets + digits.
        search_queries = list(string.ascii_lowercase) + [
            'а','б','в','г','д','е','ё','ж','з','и','й','к','л','м',
            'н','о','п','р','с','т','у','ф','х','ц','ч','ш','щ',
            'ъ','ы','ь','э','ю','я'
        ] + list(string.digits) + ['']  # empty string = no filter
        
        try:
            for query in search_queries:
                try:
                    async for member in client.get_chat_members(chat_id, query=query):
                        if member.user.id in seen:
                            continue
                        seen.add(member.user.id)
                        if member.user.is_bot or member.user.is_deleted:
                            bots_skipped += 1
                            continue
                        if self._passes_filters(member.user, filters):
                            results.append(self._format_user(member.user, f"Группа {chat_id}"))
                        else:
                            filtered_out += 1
                        if max_users and len(results) >= max_users:
                            break
                except FloodWait as e:
                    logger.warning(f"parse_members: FloodWait {e.value}s on query '{query}' for {chat_id}")
                    await asyncio.sleep(e.value + 1)
                    # retry same query after sleep
                    try:
                        async for member in client.get_chat_members(chat_id, query=query):
                            if member.user.id in seen:
                                continue
                            seen.add(member.user.id)
                            if member.user.is_bot or member.user.is_deleted:
                                bots_skipped += 1
                                continue
                            if self._passes_filters(member.user, filters):
                                results.append(self._format_user(member.user, f"Группа {chat_id}"))
                            else:
                                filtered_out += 1
                            if max_users and len(results) >= max_users:
                                break
                    except Exception as retry_ex:
                        logger.warning(f"parse_members: retry query '{query}' also failed: {retry_ex}")
                except Exception as ex:
                    logger.warning(f"parse_members: query '{query}' failed for {chat_id}: {ex}")
                    continue
                    
                if max_users and len(results) >= max_users:
                    break
                    
            logger.info(
                f"FUNNEL [parse_members] [{chat_id}]: "
                f"seen_unique={len(seen)}, bots_deleted={bots_skipped}, "
                f"filtered_out={filtered_out}, passed_filters={len(results)}, "
                f"queries_searched={len(search_queries)}"
            )
        except Exception as e:
            logger.error(f"Error parsing members from {chat_id}: {e}", exc_info=True)
        return results

    async def parse_comments(self, chat_id: str|int, post_id: int, filters: dict, max_users: int = 0) -> List[dict]:
        """Mode 2: Channel Comments"""
        client = self.clients[0]
        results = []
        seen = set()
        filtered_out = 0
        try:
            async for reply in client.get_discussion_replies(chat_id, post_id):
                user = reply.from_user
                if user and user.id not in seen:
                    seen.add(user.id)
                    if self._passes_filters(user, filters):
                        results.append(self._format_user(user, f"Комменты {chat_id}_{post_id}"))
                    else:
                        filtered_out += 1
                    if max_users and len(results) >= max_users:
                        break
            logger.info(
                f"FUNNEL [parse_comments] [{chat_id}_{post_id}]: "
                f"seen_unique={len(seen)}, filtered_out={filtered_out}, passed_filters={len(results)}"
            )
        except FloodWait as e:
            logger.warning(f"parse_comments: FloodWait {e.value}s on {chat_id}_{post_id}")
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            logger.error(f"Error parsing comments from {chat_id}_{post_id}: {e}", exc_info=True)
        return results

    async def parse_history(self, chat_id: str|int, filters: dict, limit: int = 0, days_back: int = 0, progress_callback=None, message_thread_id: int = None) -> List[dict]:
        """Mode 3: Chat History (Active writers). limit=0 means scan ALL messages."""
        client = self.clients[0]
        results = []
        seen = set()
        scanned = 0
        bots_skipped = 0
        filtered_out = 0
        cutoff = _utc_now_naive() - timedelta(days=days_back) if days_back > 0 else None
        try:
            async for msg in _safe_get_history(client, chat_id, message_thread_id, limit=limit or 0):
                scanned += 1
                if cutoff and msg.date and msg.date.replace(tzinfo=None) < cutoff:
                    if message_thread_id:
                        continue  # topics may not be strictly chronological
                    else:
                        break
                user = msg.from_user
                if user and user.id not in seen:
                    seen.add(user.id)
                    if user.is_bot or user.is_deleted:
                        bots_skipped += 1
                    elif self._passes_filters(user, filters):
                        results.append(self._format_user(user, f"История {chat_id}"))
                    else:
                        filtered_out += 1
                # Progress report every 5000 messages
                if progress_callback and scanned % 5000 == 0:
                    await progress_callback(scanned, len(results))
            logger.info(
                f"FUNNEL [parse_history] [{chat_id}]: "
                f"scanned_msgs={scanned}, seen_unique={len(seen)}, bots_deleted={bots_skipped}, "
                f"filtered_out={filtered_out}, passed_filters={len(results)}"
            )
        except FloodWait as e:
            logger.warning(f"parse_history: FloodWait {e.value}s on {chat_id}, resuming...")
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            logger.error(f"Error parsing history from {chat_id}: scanned {scanned} msgs before error: {e}", exc_info=True)
        return results

    async def export_to_excel(self, users: List[dict], filename: str) -> str:
        if not users:
            return ""
        df = pd.DataFrame(users)
        df.to_excel(filename, index=False)
        return filename

async def run_parser_task(bot: Client, admin_id: int|str, task_data: dict):
    """
    Executes a parsing job asynchronously and sends the result to the admin.
    task_data = {
      "src": str,
      "chat": str,
      "limit": int,
    }
    """
    await bot.send_message(admin_id, f"🔍 Запуск парсера: {task_data.get('parser_src')} -> {task_data.get('chat')}")
    
    from modules.sender import get_session_files, _make_client
    from webapp.main import running_clients
    
    sessions = get_session_files()
    if not sessions:
        await bot.send_message(admin_id, "❌ Ошибка: нет доступных сессий для работы парсера.")
        return
        
    session_name = task_data.get("selected_session", sessions[0].stem)
    session_path = next((s for s in sessions if s.stem == session_name), sessions[0])
    
    is_borrowed = False
    if session_name in running_clients:
        client = running_clients[session_name]
        is_borrowed = True
    else:
        client = _make_client(session_path)
        
    try:
        if not is_borrowed:
            await client.connect()
            
        manager = ParserManager([client])
        
        filters = task_data.get("filters", {
            "require_photo": False, 
            "require_premium": False,
            "sex": "all",
            "online_filter": "all",
            "days_back": 0,
        })
        
        chat = task_data.get("chat", "")
        # Strip t.me URLs to just usernames
        for prefix in ["https://t.me/", "http://t.me/", "t.me/", "@"]:
            if chat.startswith(prefix):
                chat = chat[len(prefix):]
                break
        chat = chat.strip().rstrip("/")
        
        message_thread_id = None
        if "/" in chat:
            parts = chat.split("/")
            if parts[-1].isdigit():
                message_thread_id = int(parts[-1])
                chat = parts[0]
        
        limit = int(task_data.get("limit", 0))
        src = task_data.get("parser_src")
        days_back = int(filters.get("days_back", 0))
        
        await bot.send_message(admin_id, f"🔍 Парсим `{chat}` | Источник: {src} | Лимит: {limit or 'Все'} | Дней: {days_back or 'Все'}")
        
        users = []
        if src == "group":
            users = await manager.parse_members(chat, filters, max_users=limit)
        elif src == "comments":
            users = await manager.parse_comments(chat, limit, filters)
        elif src == "history":
            async def on_progress(scanned, found):
                await bot.send_message(admin_id, f"⏳ Прогресс: просканировано {scanned} сообщений, найдено {found} юзеров...")
            users = await manager.parse_history(chat, filters, limit=limit, days_back=days_back, progress_callback=on_progress, message_thread_id=message_thread_id)
        else:
            await bot.send_message(admin_id, f"❌ Источник {src} пока не реализован.")
            
        if users:
            filename = f"parser_{src}_{limit}.xlsx"
            await manager.export_to_excel(users, filename)
            await bot.send_document(admin_id, filename, caption=f"✅ Собрано пользователей: {len(users)}\n\nФильтры:\nПоказать премиум: {filters['require_premium']}\nС аватаркой: {filters['require_photo']}\nПол: {filters['sex']}\nБыл в сети: {filters['online_filter']}")
            import os
            os.remove(filename)
        else:
            await bot.send_message(admin_id, "⚠️ Парсер не нашел ни одного пользователя по этим критериям.")
    except Exception as e:
        logger.error(f"Parser crash: {e}", exc_info=True)
        await bot.send_message(admin_id, f"❌ Ошибка парсера: {e}")
    finally:
        if not is_borrowed:
            await client.disconnect()



# ═══════════════════════════════════════════════════════════════════════
# Smart AI-Parser — Gemini-powered lead analysis
# ═══════════════════════════════════════════════════════════════════════

import json
import re
import aiohttp
from dataclasses import dataclass, field



@dataclass
class SmartParserConfig:
    """Configuration for smart AI parsing."""
    days_depth: int = 30
    age_min: int = 16
    age_max: int = 25
    city: str = ""
    strict_location: bool = True
    require_experience: bool = False
    gemini_api_key: str = ""
    chat_id: str = ""
    strictness: int = 50
    slang_threshold: int = 3
    custom_prompt: str = ""


# Weighted courier slang: {term: weight}
# High weight (2) = specific courier terms, Low weight (1) = general terms
COURIER_SLANG_WEIGHTED = {
    # Специфичные курьерские термины (вес 2)
    "курьер": 2, "батч": 2, "слот": 2, "пвз": 2, "пункт выдачи": 2,
    "пешкарус": 2, "велокурьер": 2, "самокатчик": 2, "дарк стор": 2,
    "darkstore": 2, "достависта": 2, "dostavista": 2,
    "брал заказ": 2, "закрыл смену": 2, "вышел на смену": 2,
    "сделал доставок": 2, "на линии": 2, "забрал заказ": 2,
    "принял заказ": 2, "в зоне": 2,
    # Бренды доставки (вес 2)
    "яндекс еда": 2, "сбермаркет": 2, "деливери": 2, "delivery": 2,
    "сдэк": 2, "cdek": 2, "вайлдберриз": 2, "wb": 2,
    "озон": 2, "ozon": 2, "магнит доставка": 2, "лавка": 2,
    # Общие термины (вес 1) — могут встречаться не только у курьеров
    "доставка": 1, "самокат": 1, "заказ": 1,
    "маршрут": 1, "смена": 1, "достав": 1, "пеший": 1,
    "велосип": 1, "зона": 1,
}

# Backward-compatible list for any external usage
COURIER_SLANG = list(COURIER_SLANG_WEIGHTED.keys())


class SmartParser:
    """
    Adaptive AI-powered parser that uses AI (via OnlySQ API) to analyze
    chat participants and find target courier candidates.
    """

    API_URL = "https://api.onlysq.ru/ai/openai/chat/completions"
    MODEL = "claude-opus-4-5"
    MAX_RETRIES = 3
    BATCH_SIZE = 150
    RPM_LIMIT = 3

    def __init__(self, client: Client, config: SmartParserConfig):
        self.client = client
        self.config = config
        self._semaphore = asyncio.Semaphore(5)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Collect messages ───────────────────────────────────────────

    async def _collect_user_messages(
        self, chat_id, user_id: int, message_thread_id: int | None = None, limit: int = 50
    ) -> list[str]:
        """Collect up to `limit` messages from a specific user in a chat."""
        messages = []
        cutoff = _utc_now_naive() - timedelta(days=self.config.days_depth)
        try:
            async for msg in self.client.search_messages(
                chat_id, from_user=user_id, limit=limit * (5 if message_thread_id else 1)
            ):
                if message_thread_id and getattr(msg, "message_thread_id", getattr(msg, "reply_to_message_id", None)) != message_thread_id:
                    continue
                if msg.date and msg.date.replace(tzinfo=None) < cutoff:
                    break
                if msg.text:
                    messages.append(msg.text)
                if len(messages) >= limit:
                    break
        except Exception as e:
            logger.warning(f"SmartParser: could not collect msgs for user {user_id}: {e}")
        return messages

    async def _collect_context_messages(
        self, chat_id, user_id: int, message_thread_id: int | None = None, limit: int = 30
    ) -> list[dict]:
        """
        Collect conversation context: messages that reply to this user
        or that this user replied to.
        """
        context = []
        cutoff = _utc_now_naive() - timedelta(days=self.config.days_depth)
        user_msg_ids = set()

        try:
            # Step 1: collect message IDs of our target user
            async for msg in self.client.search_messages(
                chat_id, from_user=user_id, limit=100
            ):
                if message_thread_id and getattr(msg, "message_thread_id", getattr(msg, "reply_to_message_id", None)) != message_thread_id:
                    continue
                if msg.date and msg.date.replace(tzinfo=None) < cutoff:
                    break
                user_msg_ids.add(msg.id)
                # Also grab what THIS user was replying to
                if msg.reply_to_message and msg.reply_to_message.text:
                    reply_from = "unknown"
                    if msg.reply_to_message.from_user:
                        reply_from = msg.reply_to_message.from_user.first_name or str(msg.reply_to_message.from_user.id)
                    context.append({
                        "direction": "user_replied_to",
                        "author": reply_from,
                        "text": msg.reply_to_message.text[:300],
                    })

            # Step 2: scan recent history to find replies TO our user
            async for msg in _safe_get_history(self.client, chat_id, message_thread_id, limit=500):
                if msg.date and msg.date.replace(tzinfo=None) < cutoff:
                    break
                if (
                    msg.reply_to_message_id
                    and msg.reply_to_message_id in user_msg_ids
                    and msg.from_user
                    and msg.from_user.id != user_id
                    and msg.text
                ):
                    author = msg.from_user.first_name or str(msg.from_user.id)
                    context.append({
                        "direction": "replied_to_user",
                        "author": author,
                        "text": msg.text[:300],
                    })
                if len(context) >= limit:
                    break

        except Exception as e:
            logger.warning(f"SmartParser: context collection error for user {user_id}: {e}")

        return context

    async def _collect_chat_data(self, chat_id, message_thread_id=None) -> tuple[dict[int, dict], dict[int, list[str]]]:
        """
        Collect all unique users and their messages from chat history in a single pass.
        Returns:
            users: {user_id: {"name": str, "username": str, "photo_id": str|None}}
            user_messages: {user_id: [msg_text_1, msg_text_2, ...]}
        """
        users = {}
        user_messages = {}
        cutoff = _utc_now_naive() - timedelta(days=self.config.days_depth)
        scanned = 0
        bots_skipped = 0
        flood_retries = 0
        max_flood_retries = 3

        while flood_retries <= max_flood_retries:
            try:
                async for msg in _safe_get_history(self.client, chat_id, message_thread_id, limit=0):
                    if msg.date and msg.date.replace(tzinfo=None) < cutoff:
                        if message_thread_id:
                            continue
                        else:
                            break
                    scanned += 1
                    user = msg.from_user
                    if not user:
                        continue
                        
                    if user.id not in users:
                        if user.is_bot or user.is_deleted:
                            bots_skipped += 1
                            continue
                        name = f"{user.first_name or ''} {user.last_name or ''}".strip()
                        username = user.username or ""
                        photo_id = user.photo.small_file_id if user.photo else None
                        users[user.id] = {
                            "name": name,
                            "username": username,
                            "photo_id": photo_id,
                            "last_message": "",
                            "last_message_date": None,
                        }
                        user_messages[user.id] = []
                    
                    # Collect message text (cap at 30 messages per user)
                    if user.id in user_messages and msg.text and len(user_messages[user.id]) < 30:
                        user_messages[user.id].append(msg.text[:300])
                        # Track the latest message (messages come newest-first from API)
                        if not users[user.id]["last_message"]:
                            users[user.id]["last_message"] = msg.text[:200]
                            users[user.id]["last_message_date"] = msg.date.isoformat() if msg.date else None
                break
            except FloodWait as e:
                flood_retries += 1
                logger.warning(
                    f"SmartParser: FloodWait {e.value}s while scanning {chat_id} "
                    f"(retry {flood_retries}/{max_flood_retries}, scanned {scanned} msgs so far)"
                )
                if flood_retries > max_flood_retries:
                    logger.error(f"SmartParser: max FloodWait retries exceeded for {chat_id}")
                    break
                await asyncio.sleep(e.value + 1)
            except Exception as e:
                logger.error(f"SmartParser: error scanning chat history: {e}", exc_info=True)
                break

        logger.info(
            f"FUNNEL [SmartParser] [{chat_id}]: scanned_msgs={scanned}, "
            f"unique_users={len(users)}, bots_deleted={bots_skipped}"
        )
        return users, user_messages

    async def _enrich_users_bio(self, user_ids: list[int]) -> dict[int, dict]:
        """Fetch bio/about/status for users via Pyrogram get_users (batch)."""
        bio_data = {}
        # Pyrogram get_users accepts up to 200 ids per call
        for i in range(0, len(user_ids), 200):
            batch = user_ids[i:i+200]
            try:
                users = await self.client.get_users(batch)
                if not isinstance(users, list):
                    users = [users]
                for u in users:
                    if u:
                        from pyrogram.enums import UserStatus
                        status_map = {
                            UserStatus.ONLINE: "online",
                            UserStatus.RECENTLY: "recently",
                            UserStatus.LAST_WEEK: "last_week",
                            UserStatus.LAST_MONTH: "last_month",
                            UserStatus.LONG_AGO: "long_ago",
                        }
                        bio_data[u.id] = {
                            "bio": getattr(u, "bio", None) or "",
                            "status": status_map.get(u.status, "unknown"),
                            "is_premium": getattr(u, "is_premium", False),
                        }
            except Exception as e:
                logger.warning(f"SmartParser: failed to fetch bio for batch: {e}")
        return bio_data

    def _build_batch_prompt(
        self,
        batch_users: list[dict],
    ) -> str:
        """Build a mega-batch prompt for analyzing multiple users at once."""
        cfg = self.config

        location_instruction = ""
        if cfg.city and cfg.strict_location:
            location_instruction = (
                f"ЛОКАЦИЯ (СТРОГИЙ РЕЖИМ): Юзер ДОЛЖЕН находиться в городе '{cfg.city}'. "
                f"Если нет подтверждений, но нет и опровержений — включай в результат, но снижай confidence."
            )
        elif cfg.city and not cfg.strict_location:
            location_instruction = (
                f"ЛОКАЦИЯ (МЯГКИЙ РЕЖИМ): Предпочтительно город '{cfg.city}' или его окрестности. "
                f"Принимай кандидата, если он в РФ и нет явных доказательств обратного."
            )
        else:
            location_instruction = "ЛОКАЦИЯ: Город не важен."

        experience_instruction = ""
        if cfg.require_experience:
            experience_instruction = (
                "ОПЫТ: Требуется любой курьерский опыт (пеший, вело, авто). "
                "Ищи любые намёки на работу в доставке (Яндекс Еда, Озон, WB, СДЭК, Самокат, Достависта и т.п.)."
            )
        else:
            experience_instruction = (
                "ОПЫТ: Наличие опыта НЕ обязательно. Любой человек, обсуждающий работу или подработку, подходит."
            )

        # Build users block
        users_block_parts = []
        for u in batch_users:
            header = f"[U{u['idx']:03d}] id={u['user_id']}"
            if u.get('username'):
                header += f" | @{u['username']}"
            if u.get('bio'):
                header += f" | bio: \"{u['bio'][:100]}\""
            if u.get('status'):
                header += f" | статус: {u['status']}"
            if u.get('is_premium'):
                header += " | premium: да"
                
            msgs = "\n".join(f'  💬 "{m}"' for m in u.get('messages', []))
            users_block_parts.append(f"{header}\n{msgs}")

        users_block = "\n\n".join(users_block_parts)

        prompt = (
            "Ты — HR-аналитик. Анализируешь сообщения из Telegram-чата и определяешь, "
            "кто из пользователей подходит на позицию курьера.\n\n"
            "КРИТЕРИИ:\n"
            f"- ВОЗРАСТ: Оцени возраст по стилю общения. Целевой диапазон: {cfg.age_min}-{cfg.age_max} лет. "
            f"Любые сомнения трактуй в пользу кандидата.\n"
            f"- {location_instruction}\n"
            f"- {experience_instruction}\n\n"
            f"ОБЩЕЕ ПРАВИЛО: Индекс строгости отбора (0-100) равен {cfg.strictness}. "
            "Если строгость ниже 50, будь ОЧЕНЬ ЛОЯЛЕН — отклоняй только явных ботов, детей (<14) и иностранцев. "
            "Если строгость выше 50, будь более придирчив (включай только тех, у кого есть признаки курьера).\n\n"
        )
        
        if cfg.custom_prompt:
            prompt += f"ДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ ОТ ПОЛЬЗОВАТЕЛЯ:\n{cfg.custom_prompt}\n\n"

        prompt += (
            f"ДАННЫЕ ({len(batch_users)} пользователей):\n\n"
            f"{users_block}\n\n"
            "ЗАДАНИЕ: Из всех пользователей выше выбери ТОЛЬКО тех, кто является потенциальным курьером "
            "или кандидатом на работу курьером.\n\n"
            "Ответь СТРОГО в формате JSON (без markdown, без ```):\n"
            '[\n'
            '  {"idx": 1, "user_id": 12345, "is_target": true, "confidence": 85, '
            '"reason": "краткое объяснение", "inferred_age": 22, "inferred_city": "Москва"},\n'
            '  ...\n'
            ']\n\n'
            "Если ни один пользователь не подходит, верни пустой массив: []\n"
            "ВАЖНО: включай ТОЛЬКО подходящих кандидатов (is_target=true). НЕ включай остальных."
        )
        return prompt

    async def _call_api_with_rate_limit(self, prompt: str, request_num: int = 0) -> list[dict] | None:
        """Call AI API with rate limiting for low-RPM models. Returns list of targets or None."""
        session = await self._get_session()
        headers = {
            "Authorization": f"Bearer {self.config.gemini_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.MODEL,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 16000,
        }

        # Rate limit: wait between requests (RPM=3 → 20s between calls)
        if request_num > 0:
            wait_time = 60.0 / self.RPM_LIMIT
            logger.info(f"SmartParser: rate limit — waiting {wait_time:.0f}s before request #{request_num + 1}")
            await asyncio.sleep(wait_time)

        raw_text = ""
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                async with session.post(
                    self.API_URL, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120)  # longer timeout for big batches
                ) as resp:
                    if resp.status == 429:
                        wait = 2 ** (attempt + 2)
                        logger.warning(f"SmartParser: rate limit 429, waiting {wait}s (attempt {attempt+1})")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 403:
                        text = await resp.text()
                        logger.error(f"SmartParser: API 403 — invalid/missing API key. Response: {text[:200]}")
                        return None  # Early exit — no point retrying with bad key
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"SmartParser: API error {resp.status}: {text[:200]}")
                        if attempt < self.MAX_RETRIES:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return None

                    data = await resp.json()
                    raw_text = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    # Clean up: remove markdown fences if present
                    cleaned = raw_text.strip()
                    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
                    cleaned = re.sub(r"\s*```$", "", cleaned)
                    cleaned = cleaned.strip()

                    parsed = json.loads(cleaned)
                    
                    if not isinstance(parsed, list):
                        parsed = [parsed]
                    
                    logger.info(f"SmartParser: batch API call returned {len(parsed)} targets")
                    return parsed

            except json.JSONDecodeError:
                logger.warning(f"SmartParser: API returned non-JSON: {raw_text[:300]}")
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(2)
                    continue
                return None
            except asyncio.TimeoutError:
                logger.warning(f"SmartParser: API timeout (attempt {attempt+1}/{self.MAX_RETRIES+1})")
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(5)
                    continue
                return None
            except Exception as e:
                logger.error(f"SmartParser: API call error: {e}")
                return None
        return None

    # ── Gemini API ─────────────────────────────────────────────────

    def _build_prompt(
        self,
        user_messages: list[str],
        context: list[dict] | None = None,
        phase: int = 1,
    ) -> str:
        cfg = self.config

        location_instruction = ""
        if cfg.city and cfg.strict_location:
            location_instruction = (
                f"ЛОКАЦИЯ (СТРОГИЙ РЕЖИМ): Юзер ДОЛЖЕН находиться в городе '{cfg.city}'. "
                f"Если нет подтверждений, но нет и опровержений — ставь is_target=true, но снижай confidence."
            )
        elif cfg.city and not cfg.strict_location:
            location_instruction = (
                f"ЛОКАЦИЯ (МЯГКИЙ РЕЖИМ): Предпочтительно город '{cfg.city}' или его окрестности. "
                f"Принимай кандидата, если он в РФ и нет явных доказательств обратного."
            )
        else:
            location_instruction = (
                "ЛОКАЦИЯ: Город не важен."
            )

        experience_instruction = ""
        if cfg.require_experience:
            experience_instruction = (
                "ОПЫТ: Требуется любой курьерский опыт (пеший, вело, авто). "
                "Ищи любые намёки на работу в доставке (Яндекс, Озон, WB, СДЭК, Самокат, Достависта и т.п.)."
            )
        else:
            experience_instruction = (
                "ОПЫТ: Наличие опыта НЕ обязательно. Любой человек, обсуждающий работу или подработку, подходит."
            )

        phase_note = ""
        if phase == 2:
            phase_note = (
                "\n\nВНИМАНИЕ: Это повторный анализ с бОльшим количеством сообщений. "
                "Постарайся сделать более уверенный вывод."
            )
        elif phase == 3:
            phase_note = (
                "\n\nВНИМАНИЕ: Это финальный анализ с контекстом диалога. "
                "Ниже также приведены сообщения других людей, которые общались с этим юзером. "
                "Используй контекст для более точного определения возраста и локации."
            )

        msgs_block = "\n".join(f'- "{m[:200]}"' for m in user_messages[:100])

        prompt = (
            "Ты — HR-аналитик. Твоя задача — определить, подходит ли человек на позицию курьера.\n\n"
            "КРИТЕРИИ:\n"
            f"- ВОЗРАСТ: Оцени возраст по стилю общения. Целевой диапазон: {cfg.age_min}-{cfg.age_max} лет. Любые сомнения трактуй в пользу кандидата.\n"
            f"- {location_instruction}\n"
            f"- {experience_instruction}\n\n"
            f"ОБЩЕЕ ПРАВИЛО: Индекс строгости отбора (0-100) равен {cfg.strictness}. Отбирай людей соответственно. "
            "Если строгость ниже 50, будь ОЧЕНЬ ЛОЯЛЕН. Отклоняй только явных ботов (is_target=false), детей (<14) и иностранцев. "
            "Если строгость выше 50, будь более придирчив (is_target=true только если есть признаки курьера).\n"
            f"СООБЩЕНИЯ ЮЗЕРА:\n{msgs_block}\n"
        )

        if context:
            prompt += "\nКОНТЕКСТ ДИАЛОГА (сообщения окружающих):\n"
            for c in context[:20]:
                direction = "Юзер ответил на" if c["direction"] == "user_replied_to" else "Ответ юзеру от"
                prompt += f'- [{direction} {c["author"]}]: "{c["text"]}"\n'
                
        if cfg.custom_prompt:
            prompt += f"\nДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ ОТ ПОЛЬЗОВАТЕЛЯ:\n{cfg.custom_prompt}\n"

        prompt += (
            f"{phase_note}\n\n"
            "Ответь СТРОГО в формате JSON (без markdown, без ```):\n"
            '{"is_target": true/false, "confidence": 0-100, "reason": "краткое объяснение", '
            '"inferred_age": число_или_null, "inferred_city": "город_или_null"}\n'
        )
        return prompt

    async def _call_gemini(self, prompt: str) -> dict | None:
        """Call AI model via OnlySQ OpenAI-compatible API with semaphore rate limiting."""
        async with self._semaphore:
            session = await self._get_session()
            headers = {
                "Authorization": f"Bearer {self.config.gemini_api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.MODEL,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3,
                "max_tokens": 300,
            }

            raw_text = ""
            for attempt in range(self.MAX_RETRIES + 1):
                try:
                    async with session.post(
                        self.API_URL, json=payload, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status == 429:
                            wait = 2 ** (attempt + 1)
                            logger.warning(f"AI rate limit, waiting {wait}s...")
                            await asyncio.sleep(wait)
                            continue
                        if resp.status != 200:
                            text = await resp.text()
                            logger.error(f"AI API error {resp.status}: {text[:200]}")
                            return None

                        data = await resp.json()
                        raw_text = (
                            data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                        )

                        # Clean up: remove markdown fences if present
                        cleaned = raw_text.strip()
                        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
                        cleaned = re.sub(r"\s*```$", "", cleaned)
                        cleaned = cleaned.strip()

                        parsed = json.loads(cleaned)
                        logger.info(
                            f"AI response ({self.MODEL}): is_target={parsed.get('is_target')}, "
                            f"confidence={parsed.get('confidence')}, "
                            f"reason={str(parsed.get('reason', ''))[:80]}"
                        )
                        return parsed

                except json.JSONDecodeError:
                    logger.warning(f"AI returned non-JSON: {raw_text[:200]}")
                    return None
                except asyncio.TimeoutError:
                    logger.warning(f"AI timeout (attempt {attempt+1})")
                    if attempt < self.MAX_RETRIES:
                        await asyncio.sleep(2)
                        continue
                    return None
                except Exception as e:
                    logger.error(f"AI call error: {e}")
                    return None
        return None

    # ── Slang check (fallback) ─────────────────────────────────────

    @staticmethod
    def _has_courier_slang(messages: list[str], threshold: int) -> tuple[bool, int]:
        """Check if messages contain courier-related slang using weighted scoring.
        Returns (is_match, total_score)."""
        if threshold <= 0:
            return False, 0
            
        combined = " ".join(messages).lower()
        score = sum(weight for term, weight in COURIER_SLANG_WEIGHTED.items() if term in combined)
        return score >= threshold, score

    # ── Analyze single user ────────────────────────────────────────

    async def _analyze_user(
        self, chat_id, user_id: int, display_name: str, message_thread_id: int | None = None
    ) -> dict | None:
        """
        Adaptive multi-phase analysis of a single user.
        Returns result dict or None if user should be skipped.
        """

        # ── Phase 1: first 50 messages ──
        messages = await self._collect_user_messages(chat_id, user_id, message_thread_id, limit=50)
        if len(messages) < 3:
            logger.debug(f"SmartParser: user {user_id} has too few messages ({len(messages)}), skip")
            return None

        prompt = self._build_prompt(messages, phase=1)
        result = await self._call_gemini(prompt)

        if result is None:
            logger.warning(f"SmartParser: user {user_id} phase 1 — Gemini returned None (API error?)")
        elif result.get("is_target") and result.get("confidence", 0) >= 40:
            result["user_id"] = user_id
            result["display_name"] = display_name
            result["phase"] = 1
            return result
        else:
            logger.info(f"SmartParser: user {user_id} phase 1 — not target (is_target={result.get('is_target')}, conf={result.get('confidence')})")

        # ── Phase 2: +50 messages (total 100) ──
        logger.debug(f"SmartParser: user {user_id} phase 2 starting")
        more_messages = await self._collect_user_messages(chat_id, user_id, message_thread_id, limit=100)
        if len(more_messages) > len(messages):
            messages = more_messages

        prompt = self._build_prompt(messages, phase=2)
        result = await self._call_gemini(prompt)

        if result is None:
            logger.warning(f"SmartParser: user {user_id} phase 2 — Gemini returned None (API error?)")
        elif result.get("is_target") and result.get("confidence", 0) >= 40:
            result["user_id"] = user_id
            result["display_name"] = display_name
            result["phase"] = 2
            return result
        else:
            logger.info(f"SmartParser: user {user_id} phase 2 — not target (is_target={result.get('is_target')}, conf={result.get('confidence')})")

        # ── Phase 3: conversation context ──
        logger.debug(f"SmartParser: user {user_id} phase 3 (context) starting")
        context = await self._collect_context_messages(chat_id, user_id, message_thread_id)

        if context:
            prompt = self._build_prompt(messages, context=context, phase=3)
            result = await self._call_gemini(prompt)

            if result is None:
                logger.warning(f"SmartParser: user {user_id} phase 3 — Gemini returned None (API error?)")
            elif result.get("is_target") and result.get("confidence", 0) >= 40:
                result["user_id"] = user_id
                result["display_name"] = display_name
                result["phase"] = 3
                return result
            else:
                logger.info(f"SmartParser: user {user_id} phase 3 — not target (is_target={result.get('is_target')}, conf={result.get('confidence')})")

        # ── Phase 4-5: slang fallback ──
        slang_match, slang_score = self._has_courier_slang(messages, threshold=self.config.slang_threshold)
        if slang_match:
            logger.info(f"SmartParser: user {user_id} — courier slang detected (score={slang_score}), accepting")
            return {
                "user_id": user_id,
                "display_name": display_name,
                "is_target": True,
                "confidence": min(60 + slang_score * 2, 90),
                "reason": f"Обнаружен курьерский сленг (score={slang_score}, порог={self.config.slang_threshold})",
                "inferred_age": None,
                "inferred_city": None,
                "phase": 4,
            }

        # ── Phase 5: skip ──
        logger.debug(f"SmartParser: user {user_id} — no slang, skipping")
        return None

    # ── Main entry point ───────────────────────────────────────────

    async def _process_collected_data(
        self, chat_id, users, user_messages, total_users, progress_callback
    ) -> list[dict]:
        """Process collected user data: filter, enrich bio, batch AI analysis, slang fallback, avatars."""
        # Filter out users with < 2 messages
        active_users = {uid: info for uid, info in users.items() if len(user_messages.get(uid, [])) >= 2}
        skipped = total_users - len(active_users)
        if skipped:
            logger.info(f"SmartParser: skipped {skipped} users with < 2 messages")

        if not active_users:
            logger.info(f"SmartParser: no active users (with 2+ messages) in {chat_id}")
            return []

        # Step 1.5: Enrich with bio/status
        logger.info(f"SmartParser: enriching {len(active_users)} users with bio/status...")
        bio_data = await self._enrich_users_bio(list(active_users.keys()))

        # Step 2: Form batches and send to AI
        batch_users_list = []
        idx = 0
        for user_id, info in active_users.items():
            idx += 1
            bio_info = bio_data.get(user_id, {})
            batch_users_list.append({
                "idx": idx,
                "user_id": user_id,
                "name": info["name"],
                "username": info["username"],
                "photo_id": info.get("photo_id"),
                "bio": bio_info.get("bio", ""),
                "status": bio_info.get("status", ""),
                "is_premium": bio_info.get("is_premium", False),
                "messages": user_messages.get(user_id, []),
            })

        # Split into batches of BATCH_SIZE
        batches = []
        for i in range(0, len(batch_users_list), self.BATCH_SIZE):
            batches.append(batch_users_list[i:i + self.BATCH_SIZE])

        logger.info(f"SmartParser: {len(active_users)} users → {len(batches)} batch(es) of max {self.BATCH_SIZE}")

        all_ai_targets = []
        api_failed = False

        for batch_idx, batch in enumerate(batches):
            prompt = self._build_batch_prompt(batch)
            logger.info(f"SmartParser: sending batch {batch_idx+1}/{len(batches)} ({len(batch)} users, ~{len(prompt)} chars)")
            
            targets = await self._call_api_with_rate_limit(prompt, request_num=batch_idx)
            
            if targets is None:
                logger.error(f"SmartParser: batch {batch_idx+1} failed (API error)")
                api_failed = True
                break
            
            all_ai_targets.extend(targets)
            
            if progress_callback:
                processed_so_far = min((batch_idx + 1) * self.BATCH_SIZE, len(active_users))
                await progress_callback(processed_so_far, total_users, len(all_ai_targets))

        # Step 3: Map AI results back to user data
        results = []
        ai_target_user_ids = set()
        idx_to_user = {u["idx"]: u for u in batch_users_list}

        for target in all_ai_targets:
            t_user_id = target.get("user_id")
            t_idx = target.get("idx")
            
            user_info = None
            if t_user_id and t_user_id in active_users:
                user_info = active_users[t_user_id]
                uid = t_user_id
            elif t_idx and t_idx in idx_to_user:
                uid = idx_to_user[t_idx]["user_id"]
                user_info = active_users.get(uid)
            
            if not user_info:
                continue

            confidence = target.get("confidence", 50)
            if confidence < 40:
                continue

            ai_target_user_ids.add(uid)
            results.append({
                "user_id": uid,
                "display_name": user_info["name"],
                "username": user_info["username"],
                "is_target": True,
                "confidence": confidence,
                "reason": target.get("reason", ""),
                "inferred_age": target.get("inferred_age"),
                "inferred_city": target.get("inferred_city"),
                "last_message": user_info.get("last_message", ""),
                "last_message_date": user_info.get("last_message_date"),
                "phase": "batch",
            })

        # Step 4: Slang fallback
        slang_found = 0
        for user_id, info in active_users.items():
            if user_id in ai_target_user_ids:
                continue
            msgs = user_messages.get(user_id, [])
            slang_match, slang_score = self._has_courier_slang(msgs, threshold=self.config.slang_threshold)
            if slang_match:
                slang_found += 1
                results.append({
                    "user_id": user_id,
                    "display_name": info["name"],
                    "username": info["username"],
                    "is_target": True,
                    "confidence": min(60 + slang_score * 2, 90),
                    "reason": f"Обнаружен курьерский сленг (score={slang_score}, порог={self.config.slang_threshold})",
                    "inferred_age": None,
                    "inferred_city": None,
                    "last_message": info.get("last_message", ""),
                    "last_message_date": info.get("last_message_date"),
                    "phase": "slang_fallback",
                })
        
        if slang_found:
            logger.info(f"SmartParser: slang fallback added {slang_found} more targets")

        # Step 5: Download avatars in parallel
        import os
        import asyncio
        from datetime import datetime
        import json
        avatar_sem = asyncio.Semaphore(10)
        
        async def _download_avatar(result_item):
            user_id = result_item["user_id"]
            info = active_users.get(user_id, {})
            avatar_url = None
            if info.get("photo_id"):
                async with avatar_sem:
                    try:
                        save_path = f"webapp/static/avatars/{user_id}.jpg"
                        if not os.path.exists(save_path):
                            await self.client.download_media(
                                info["photo_id"],
                                file_name=save_path
                            )
                        if os.path.exists(save_path):
                            avatar_url = f"/static/avatars/{user_id}.jpg"
                    except Exception as e:
                        logger.error(f"Error downloading avatar for {user_id}: {e}")
            result_item["avatar_url"] = avatar_url
            result_item["parsed_at"] = datetime.now().isoformat()
        
        logger.info(f"SmartParser: downloading avatars for {len(results)} targets (parallel, sem=10)...")
        await asyncio.gather(*[_download_avatar(r) for r in results])

        await self.close()

        # Save to contacts.json
        if results:
            try:
                db_path = "contacts.json"
                existing = []
                if os.path.exists(db_path):
                    with open(db_path, "r", encoding="utf-8") as f:
                        try:
                            existing = json.load(f)
                        except json.JSONDecodeError:
                            existing = []
                
                existing_map = {str(r_item.get("user_id")): r_item for r_item in existing if r_item.get("user_id")}
                for r_item in results:
                    existing_map[str(r_item["user_id"])] = r_item
                
                with open(db_path, "w", encoding="utf-8") as f:
                    json.dump(list(existing_map.values()), f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f"Failed to save contacts.json: {e}")

        if progress_callback:
            await progress_callback(total_users, total_users, len(results))

        logger.info(f"SmartParser: analysis complete. {len(results)}/{total_users} targets found.")
        return results

    async def analyze_chat(self, chat_id, message_thread_id=None, progress_callback=None) -> list[dict]:
        """
        Analyze all users in a chat using mega-batch approach.
        Collects all messages in a single pass, then sends 1-2 large batches to AI.
        For forum chats (with topics), auto-detects and scans all topics.
        """
        # Step 0: Auto-detect forum chats and scan all topics
        if not message_thread_id:
            try:
                chat_obj = await self.client.get_chat(chat_id)
                is_forum = getattr(chat_obj, 'is_forum', False)
                if is_forum:
                    logger.info(f"SmartParser: '{chat_id}' is a forum chat. Auto-scanning all topics...")
                    # Get topics via raw API
                    from pyrogram.raw.functions.channels import GetForumTopics
                    from pyrogram.raw.types import ForumTopic
                    
                    peer = await self.client.resolve_peer(chat_id)
                    
                    all_users = {}
                    all_user_messages = {}
                    topics_scanned = 0
                    
                    try:
                        result = await self.client.invoke(
                            GetForumTopics(
                                channel=peer,
                                offset_date=0,
                                offset_id=0,
                                offset_topic=0,
                                limit=100,
                                q=""
                            )
                        )
                        
                        topic_ids = []
                        for topic in getattr(result, 'topics', []):
                            if isinstance(topic, ForumTopic):
                                topic_ids.append(topic.id)
                        
                        logger.info(f"SmartParser: found {len(topic_ids)} topics in forum '{chat_id}'")
                        
                        for tid in topic_ids:
                            try:
                                t_users, t_msgs = await self._collect_chat_data(chat_id, message_thread_id=tid)
                                # Merge results
                                for uid, info in t_users.items():
                                    if uid not in all_users:
                                        all_users[uid] = info
                                        all_user_messages[uid] = []
                                    all_user_messages[uid].extend(t_msgs.get(uid, []))
                                    # Cap at 30 messages per user across all topics
                                    all_user_messages[uid] = all_user_messages[uid][:30]
                                topics_scanned += 1
                                logger.info(
                                    f"SmartParser: topic {tid} → {len(t_users)} users, "
                                    f"{sum(len(v) for v in t_msgs.values())} msgs"
                                )
                            except Exception as topic_err:
                                logger.warning(f"SmartParser: error scanning topic {tid}: {topic_err}")
                                continue
                    except Exception as e:
                        logger.error(f"SmartParser: failed to get forum topics: {e}", exc_info=True)
                        # Fallback: try General topic (id=1)
                        logger.info("SmartParser: falling back to General topic (id=1)")
                        all_users, all_user_messages = await self._collect_chat_data(chat_id, message_thread_id=1)
                    
                    logger.info(
                        f"SmartParser: forum scan complete. "
                        f"Topics scanned: {topics_scanned}, "
                        f"Total users: {len(all_users)}, "
                        f"Total messages: {sum(len(v) for v in all_user_messages.values())}"
                    )
                    
                    # Use the merged data instead of calling _collect_chat_data again
                    users = all_users
                    user_messages = all_user_messages
                    
                    # Skip the regular _collect_chat_data call below
                    # Jump to the rest of analyze_chat with users and user_messages already set
                    total_users = len(users)
                    if total_users == 0:
                        logger.info(f"SmartParser: no users found in forum '{chat_id}'")
                        return []
                    
                    if progress_callback:
                        await progress_callback(0, total_users, 0)
                    
                    # Continue with the rest of the method from "Filter out users..."
                    # We need to skip the regular collection below
                    return await self._process_collected_data(
                        chat_id, users, user_messages, total_users, progress_callback
                    )
            except Exception as forum_err:
                logger.warning(f"SmartParser: forum detection failed for '{chat_id}': {forum_err}")
                # Continue with normal flow

        # Step 1: Collect all users and their messages in a single pass
        if progress_callback:
            await progress_callback(0, 0, 0)

        users, user_messages = await self._collect_chat_data(chat_id, message_thread_id)
        total_users = len(users)

        if total_users == 0:
            logger.info(f"SmartParser: no users found in {chat_id}")
            return []

        if progress_callback:
            await progress_callback(0, total_users, 0)

        return await self._process_collected_data(
            chat_id, users, user_messages, total_users, progress_callback
        )

    async def export_results(self, results: list[dict], filename: str) -> str:
        """Export results to Excel."""
        if not results:
            return ""
        rows = []
        for r in results:
            rows.append({
                "ID": r.get("user_id", ""),
                "Имя": r.get("display_name", ""),
                "Username": r.get("username", ""),
                "Источник": r.get("source", ""),
                "Таргет": "✅ Да" if r.get("is_target") else "❌ Нет",
                "Уверенность": r.get("confidence", 0),
                "Последнее сообщение": (r.get("last_message", "") or "")[:100],
                "Дата сообщения": r.get("last_message_date", ""),
                "Возраст (оценка)": r.get("inferred_age", "N/A"),
                "Город (оценка)": r.get("inferred_city", "N/A"),
                "Причина": r.get("reason", ""),
                "Фаза анализа": r.get("phase", "?"),
            })
        df = pd.DataFrame(rows)
        df.to_excel(filename, index=False)
        return filename


async def run_smart_parser_task(bot: Client, admin_id, task_data: dict):
    """
    Execute a Smart AI Parsing job and send results to admin.
    task_data = {
        "chat": str,
        "selected_session": str (optional),
        "smart_config": SmartParserConfig,
    }
    """
    cfg: SmartParserConfig = task_data.get("smart_config")
    chat = task_data.get("chat", "")
    target_chats = task_data.get("target_chats", [])
    if chat and not target_chats:
        target_chats = [chat]

    cleaned_chats = []
    for c in target_chats:
        c = c.strip()
        for prefix in ["https://t.me/", "http://t.me/", "t.me/", "@"]:
            if c.startswith(prefix):
                c = c[len(prefix):]
                break
        c = c.rstrip("/")
        if c:
            cleaned_chats.append(c)

    if not cleaned_chats:
        await bot.send_message(admin_id, "❌ Нет корректных чатов для анализа.")
        return

    chat_list_str = ", ".join(cleaned_chats[:5]) + ("..." if len(cleaned_chats) > 5 else "")

    await bot.send_message(
        admin_id,
        f"🧠 **Smart AI-Parser запущен**\n\n"
        f"📥 Источники ({len(cleaned_chats)} шт): `{chat_list_str}`\n"
        f"📅 Глубина: {cfg.days_depth} дней\n"
        f"👤 Возраст: {cfg.age_min}—{cfg.age_max}\n"
        f"📍 Город: {cfg.city or 'Любой'} ({'Строгий' if cfg.strict_location else 'Мягкий'})\n"
        f"💼 Опыт: {'Требуется' if cfg.require_experience else 'Не важен'}\n\n"
        f"⏳ Подготовка сессии..."
    )

    from modules.sender import get_session_files, _make_client
    from webapp.main import running_clients

    sessions = get_session_files()
    if not sessions:
        await bot.send_message(admin_id, "❌ Нет доступных сессий.")
        return

    session_name = task_data.get("selected_session", sessions[0].stem)
    session_path = next((s for s in sessions if s.stem == session_name), sessions[0])

    is_borrowed = False
    if session_name in running_clients:
        client = running_clients[session_name]
        is_borrowed = True
    else:
        client = _make_client(session_path)

    progress_msg_id = None

    async def on_progress(chat_name, analyzed, total, targets):
        nonlocal progress_msg_id
        text = (
            f"⏳ **Smart Parser — {chat_name}**\n\n"
            f"👥 Проанализировано: {analyzed}/{total}\n"
            f"🎯 Найдено целевых: {targets}"
        )
        try:
            if progress_msg_id:
                await bot.edit_message_text(admin_id, progress_msg_id, text)
            else:
                sent = await bot.send_message(admin_id, text)
                progress_msg_id = sent.id
        except Exception:
            pass

    try:
        if not is_borrowed:
            await client.connect()

        parser = SmartParser(client, cfg)
        all_results = []

        for chat_target in cleaned_chats:
            progress_msg_id = None  # Reset for each chat
            
            message_thread_id = None
            if "/" in chat_target:
                parts = chat_target.split("/")
                if parts[-1].isdigit():
                    message_thread_id = int(parts[-1])
                    chat_target = parts[0]
            
            logger.info(f"SmartParser: starting scan of '{chat_target}' (thread={message_thread_id})")
            await bot.send_message(admin_id, f"🔍 Начинаем сканирование: `{chat_target}`")
            
            from functools import partial
            chat_progress = partial(on_progress, chat_target)
            
            try:
                results = await parser.analyze_chat(chat_target, message_thread_id=message_thread_id, progress_callback=chat_progress)
                if results:
                    for r in results:
                        r["source"] = chat_target
                    all_results.extend(results)
            except Exception as chat_err:
                error_msg = str(chat_err)
                logger.error(f"SmartParser: failed to analyze chat '{chat_target}': {chat_err}", exc_info=True)
                await bot.send_message(
                    admin_id,
                    f"⚠️ Ошибка при сканировании `{chat_target}`: {error_msg[:200]}\n"
                    f"Продолжаю с остальными чатами..."
                )

        if all_results:
            import os as _os
            filename = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", f"smart_parser_{len(cleaned_chats)}chats_{len(all_results)}.xlsx")
            await parser.export_results(all_results, filename)
            await bot.send_document(
                admin_id,
                filename,
                caption=(
                    f"✅ **Smart AI-Parser завершён**\n\n"
                    f"🎯 Найдено целевых кандидатов: **{len(all_results)}**\n"
                    f"📅 Глубина: {cfg.days_depth} дней\n"
                    f"👤 Возраст: {cfg.age_min}—{cfg.age_max}\n"
                    f"📍 Город: {cfg.city or 'Любой'}"
                ),
            )
            import os
            os.remove(filename)
        else:
            await bot.send_message(
                admin_id,
                "⚠️ **Smart Parser** не нашёл ни одного целевого кандидата по заданным критериям."
            )

    except Exception as e:
        logger.error(f"SmartParser crash: {e}", exc_info=True)
        await bot.send_message(admin_id, f"❌ Ошибка Smart Parser: {e}")
    finally:
        if not is_borrowed:
            await client.disconnect()
