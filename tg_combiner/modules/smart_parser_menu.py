"""
tg_combiner — Smart AI-Parser menu handlers.
Registers inline keyboard + FSM states for configuring and running SmartParser.
"""

import asyncio
import functools
import logging

from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from modules.parser import SmartParserConfig, run_smart_parser_task
from modules.sender import get_session_files

logger = logging.getLogger("tg_combiner.smart_menu")

# ── Per-user state stores ──────────────────────────────────────────
_smart_state: dict[int, str] = {}
_smart_data: dict[int, dict] = {}


def _get_config(uid: int) -> dict:
    """Get or initialize smart parser config dict for user."""
    d = _smart_data.setdefault(uid, {})
    d.setdefault("days_depth", 30)
    d.setdefault("age_min", 16)
    d.setdefault("age_max", 25)
    d.setdefault("city", "")
    d.setdefault("strict_location", True)
    d.setdefault("require_experience", False)
    d.setdefault("gemini_key", "REDACTED_ONLYSQ_KEY")
    d.setdefault("chat", "")
    d.setdefault("selected_session", "")
    return d


async def _safe_edit(message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except MessageNotModified:
        pass


# ── Keyboard ───────────────────────────────────────────────────────

def smart_parser_kb(uid: int) -> InlineKeyboardMarkup:
    cfg = _get_config(uid)

    city_display = cfg["city"] or "Любой"
    loc_mode = "Строгий" if cfg["strict_location"] else "Мягкий"
    exp_display = "Требуется" if cfg["require_experience"] else "Не важен"
    key_display = ("****" + cfg["gemini_key"][-4:]) if len(cfg["gemini_key"]) > 4 else (cfg["gemini_key"] or "Не задан")
    chat_display = cfg["chat"] or "Не задан"

    sessions = get_session_files()
    selected = cfg.get("selected_session") or (sessions[0].stem if sessions else "Нет")

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📅 Глубина: {cfg['days_depth']} дней", callback_data="sp_days")],
        [InlineKeyboardButton(f"👤 Возраст: {cfg['age_min']}—{cfg['age_max']}", callback_data="sp_age")],
        [InlineKeyboardButton(f"📍 Город: {city_display} ({loc_mode})", callback_data="sp_city")],
        [InlineKeyboardButton(f"🔄 Режим: {loc_mode}", callback_data="sp_toggle_strict")],
        [InlineKeyboardButton(f"💼 Опыт: {exp_display}", callback_data="sp_toggle_exp")],
        [InlineKeyboardButton(f"🔑 AI Key: {key_display}", callback_data="sp_apikey")],
        [InlineKeyboardButton(f"📱 Аккаунт: {selected}", callback_data="sp_select_acc")],
        [InlineKeyboardButton(f"💬 Чат: {chat_display}", callback_data="sp_chat")],
        [InlineKeyboardButton("🚀 ЗАПУСТИТЬ АНАЛИЗ", callback_data="sp_start")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu_parser")],
    ])


def sp_acc_select_kb(uid: int) -> InlineKeyboardMarkup:
    cfg = _get_config(uid)
    sessions = get_session_files()
    selected = cfg.get("selected_session", "")
    buttons = []
    for s in sessions:
        mark = "✅" if s.stem == selected else "◻️"
        buttons.append([InlineKeyboardButton(f"{mark} {s.stem}", callback_data=f"sp_acc_{s.stem}")])
    buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="parse_src_smart")])
    return InlineKeyboardMarkup(buttons)


# ── Register handlers ─────────────────────────────────────────────

def register_smart_parser_handlers(bot: Client):
    """Register callback query and text handlers for Smart Parser UI."""

    from bot_interface import _is_admin, admin_only

    @bot.on_callback_query(filters.regex(r"^parse_src_smart$"))
    @admin_only
    async def on_smart_menu(client: Client, cb: CallbackQuery):
        _get_config(cb.from_user.id)
        await _safe_edit(
            cb.message,
            "🧠 **Smart AI-Parser**\n\n"
            "Настрой параметры анализа и нажми «🚀 Запустить»:",
            reply_markup=smart_parser_kb(cb.from_user.id),
        )

    @bot.on_callback_query(filters.regex(r"^sp_days$"))
    @admin_only
    async def on_sp_days(client: Client, cb: CallbackQuery):
        cfg = _get_config(cb.from_user.id)
        cycle = [7, 14, 30, 60, 90]
        current = cfg["days_depth"]
        try:
            idx = cycle.index(current)
            cfg["days_depth"] = cycle[(idx + 1) % len(cycle)]
        except ValueError:
            cfg["days_depth"] = 30
        await _safe_edit(
            cb.message,
            "🧠 **Smart AI-Parser**\n\nНастрой параметры анализа и нажми «🚀 Запустить»:",
            reply_markup=smart_parser_kb(cb.from_user.id),
        )

    @bot.on_callback_query(filters.regex(r"^sp_age$"))
    @admin_only
    async def on_sp_age(client: Client, cb: CallbackQuery):
        _smart_state[cb.from_user.id] = "sp_wait_age"
        await _safe_edit(
            cb.message,
            "👤 **Возрастной диапазон**\n\n"
            "Введи минимальный и максимальный возраст через дефис.\n"
            "Пример: `16-25`",
        )

    @bot.on_callback_query(filters.regex(r"^sp_city$"))
    @admin_only
    async def on_sp_city(client: Client, cb: CallbackQuery):
        _smart_state[cb.from_user.id] = "sp_wait_city"
        await _safe_edit(
            cb.message,
            "📍 **Город**\n\n"
            "Введи название города для фильтрации.\n"
            "Отправь `0` чтобы сбросить (любой город).",
        )

    @bot.on_callback_query(filters.regex(r"^sp_toggle_strict$"))
    @admin_only
    async def on_sp_strict(client: Client, cb: CallbackQuery):
        cfg = _get_config(cb.from_user.id)
        cfg["strict_location"] = not cfg["strict_location"]
        await _safe_edit(
            cb.message,
            "🧠 **Smart AI-Parser**\n\nНастрой параметры анализа и нажми «🚀 Запустить»:",
            reply_markup=smart_parser_kb(cb.from_user.id),
        )

    @bot.on_callback_query(filters.regex(r"^sp_toggle_exp$"))
    @admin_only
    async def on_sp_exp(client: Client, cb: CallbackQuery):
        cfg = _get_config(cb.from_user.id)
        cfg["require_experience"] = not cfg["require_experience"]
        await _safe_edit(
            cb.message,
            "🧠 **Smart AI-Parser**\n\nНастрой параметры анализа и нажми «🚀 Запустить»:",
            reply_markup=smart_parser_kb(cb.from_user.id),
        )

    @bot.on_callback_query(filters.regex(r"^sp_apikey$"))
    @admin_only
    async def on_sp_apikey(client: Client, cb: CallbackQuery):
        _smart_state[cb.from_user.id] = "sp_wait_apikey"
        await _safe_edit(
            cb.message,
            "🔑 **AI API Key**\n\n"
            "Отправь свой API-ключ от OnlySQ.\n"
            "По умолчанию используется Claude Opus 4.5.",
        )

    @bot.on_callback_query(filters.regex(r"^sp_chat$"))
    @admin_only
    async def on_sp_chat(client: Client, cb: CallbackQuery):
        _smart_state[cb.from_user.id] = "sp_wait_chat"
        await _safe_edit(
            cb.message,
            "💬 **Целевой чат**\n\n"
            "Отправь юзернейм или ссылку на чат/группу для анализа:",
        )

    @bot.on_callback_query(filters.regex(r"^sp_select_acc$"))
    @admin_only
    async def on_sp_select_acc(client: Client, cb: CallbackQuery):
        await _safe_edit(
            cb.message,
            "📱 **Выбери аккаунт для Smart Parser:**",
            reply_markup=sp_acc_select_kb(cb.from_user.id),
        )

    @bot.on_callback_query(filters.regex(r"^sp_acc_"))
    @admin_only
    async def on_sp_acc_pick(client: Client, cb: CallbackQuery):
        acc = cb.data.replace("sp_acc_", "")
        cfg = _get_config(cb.from_user.id)
        cfg["selected_session"] = acc
        await _safe_edit(
            cb.message,
            "🧠 **Smart AI-Parser**\n\nНастрой параметры анализа и нажми «🚀 Запустить»:",
            reply_markup=smart_parser_kb(cb.from_user.id),
        )

    @bot.on_callback_query(filters.regex(r"^sp_start$"))
    @admin_only
    async def on_sp_start(client: Client, cb: CallbackQuery):
        import config as app_config
        cfg = _get_config(cb.from_user.id)

        # Validate
        if not cfg["gemini_key"]:
            await cb.answer("❌ Не задан Gemini API Key!", show_alert=True)
            return
        if not cfg["chat"]:
            await cb.answer("❌ Не задан чат для анализа!", show_alert=True)
            return

        sessions = get_session_files()
        if not sessions:
            await cb.answer("❌ Нет доступных сессий!", show_alert=True)
            return

        smart_cfg = SmartParserConfig(
            days_depth=cfg["days_depth"],
            age_min=cfg["age_min"],
            age_max=cfg["age_max"],
            city=cfg["city"],
            strict_location=cfg["strict_location"],
            require_experience=cfg["require_experience"],
            gemini_api_key=cfg["gemini_key"],
            chat_id=cfg["chat"],
        )

        task_data = {
            "chat": cfg["chat"],
            "selected_session": cfg.get("selected_session", sessions[0].stem),
            "smart_config": smart_cfg,
        }

        await _safe_edit(cb.message, "🚀 **Smart AI-Parser запущен в фоне...**\n\nОжидайте результатов.")
        asyncio.create_task(run_smart_parser_task(client, app_config.ADMIN_ID, task_data))


def handle_smart_parser_text(uid: int, text: str) -> tuple[bool, str | None, InlineKeyboardMarkup | None]:
    """
    Process text input for smart parser FSM states.
    Returns (handled: bool, reply_text, reply_markup).
    Called from bot_interface.on_text() handler.
    """
    state = _smart_state.get(uid)
    if not state:
        return False, None, None

    cfg = _get_config(uid)

    if state == "sp_wait_age":
        _smart_state.pop(uid, None)
        parts = text.replace(" ", "").split("-")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            cfg["age_min"] = int(parts[0])
            cfg["age_max"] = int(parts[1])
            return (
                True,
                f"✅ Возраст: {cfg['age_min']}—{cfg['age_max']}\n\n🧠 **Smart AI-Parser**",
                smart_parser_kb(uid),
            )
        else:
            return (
                True,
                "❌ Неверный формат. Ожидается: `16-25`\n\n🧠 **Smart AI-Parser**",
                smart_parser_kb(uid),
            )

    elif state == "sp_wait_city":
        _smart_state.pop(uid, None)
        if text == "0":
            cfg["city"] = ""
        else:
            cfg["city"] = text.strip()
        city_display = cfg["city"] or "Любой"
        return (
            True,
            f"✅ Город: {city_display}\n\n🧠 **Smart AI-Parser**",
            smart_parser_kb(uid),
        )

    elif state == "sp_wait_apikey":
        _smart_state.pop(uid, None)
        cfg["gemini_key"] = text.strip()
        return (
            True,
            "✅ API Key сохранён.\n\n🧠 **Smart AI-Parser**",
            smart_parser_kb(uid),
        )

    elif state == "sp_wait_chat":
        _smart_state.pop(uid, None)
        # Clean up chat link
        clean = text.strip()
        for prefix in ["https://t.me/", "http://t.me/", "t.me/", "@"]:
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
                break
        clean = clean.rstrip("/")
        cfg["chat"] = clean
        return (
            True,
            f"✅ Чат: `{clean}`\n\n🧠 **Smart AI-Parser**",
            smart_parser_kb(uid),
        )

    return False, None, None
