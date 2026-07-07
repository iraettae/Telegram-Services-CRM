"""
tg_combiner — Telegram bot Inline interface.
Menus: Network (proxy), Settings (limits/delays), Mailing (spintax),
Session management (phone → code → 2FA).
"""

import asyncio
import functools
import logging
import os
import traceback
from typing import Optional

from pyrogram import Client, filters
from pyrogram.errors import (
    MessageNotModified,
    SessionPasswordNeeded,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    PasswordHashInvalid,
)
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Message,
    WebAppInfo
)

import config
from antiban import AntiBanManager
from config import API_HASH, API_ID, BOT_TOKEN, SESSIONS_DIR
from proxy_manager import (
    add_proxy,
    check_current_ip,
    list_proxies,
    parse_proxy_string,
    remove_proxy,
    validate_proxy,
    get_proxy_for_session,
    set_proxy_for_session,
    unset_proxy_for_session,
    list_session_proxies,
)
from device_spoof import get_device_for_session
from modules.sender import get_session_files, run_advanced_mailing
from modules.parser import run_parser_task
from modules.smart_parser_menu import register_smart_parser_handlers, handle_smart_parser_text

from webapp.auth import generate_auth_token

_APP_URL_BASE = "https://combiner.example.com/app"

def get_app_url(uid: int) -> str:
    """Return the Web App URL with a signed auth token for the given user."""
    token = generate_auth_token(uid)
    return f"{_APP_URL_BASE}?auth_token={token}"

logger = logging.getLogger("tg_combiner.bot")

# ── State ──────────────────────────────────────────────────────────────
# Active proxy (None = direct connection)
active_proxy: Optional[dict] = None

# Anti-ban instance (configurable via bot)
antiban = AntiBanManager()

# FSM-like state for waiting user input
_user_state: dict[int, str] = {}
# Temporary storage for mailing flow
_mailing_data: dict[int, dict] = {}
# Temporary storage for session auth flow
_auth_data: dict[int, dict] = {}


def _is_admin(uid: int) -> bool:
    """Check if user is admin. If ADMIN_ID=0, auto-assign first user."""
    if config.ADMIN_ID == 0:
        config.save_admin_id(uid)
        logger.info("Auto-detected admin: %s", uid)
        return True
    return uid in config.get_allowed_admins()


def admin_only(func):
    """Decorator: allow only ADMIN_ID (auto-detect if 0)."""
    @functools.wraps(func)
    async def wrapper(client: Client, update):
        uid = update.from_user.id if hasattr(update, "from_user") else None
        if uid is None or not _is_admin(uid):
            return
        return await func(client, update)
    return wrapper


async def _safe_edit(message, text: str, reply_markup=None):
    """Edit message text, silently ignoring MessageNotModified."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except MessageNotModified:
        pass


# ── Keyboards ──────────────────────────────────────────────────────────

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Открыть пульт управления", web_app=WebAppInfo(url=get_app_url(config.ADMIN_ID)))],
        [InlineKeyboardButton("🌐 Сеть", callback_data="menu_net")],
        [InlineKeyboardButton("🤖 Сессии", callback_data="menu_sessions")],
        [InlineKeyboardButton("👥 Доп. Админы", callback_data="menu_admins")],
        [InlineKeyboardButton("📨 Рассылка", callback_data="menu_mail"), InlineKeyboardButton("🔍 Парсер", callback_data="menu_parser")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings")],
    ])


def net_proxy_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да", callback_data="proxy_yes"),
            InlineKeyboardButton("❌ Нет", callback_data="proxy_no"),
        ],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu_main")],
    ])


def proxy_submenu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Выбрать из списка", callback_data="proxy_list")],
        [InlineKeyboardButton("➕ Добавить новый", callback_data="proxy_add")],
        [InlineKeyboardButton("🔗 Прокси на аккаунт", callback_data="proxy_assign")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu_net")],
    ])


def settings_kb(uid: int) -> InlineKeyboardMarkup:
    md = _mailing_data.get(uid, {})
    back_data = "mail_confirm_refresh" if md.get("targets") else "menu_main"
    back_text = "🔙 Вернуться к рассылке" if md.get("targets") else "🔙 Назад"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🔢 GLOBAL_LIMIT: {antiban.global_limit}",
            callback_data="set_global_limit",
        )],
        [InlineKeyboardButton(
            f"📱 ACCOUNT_LIMIT: {antiban.account_limit}",
            callback_data="set_account_limit",
        )],
        [InlineKeyboardButton(
            f"⏱ MIN_DELAY: {antiban.min_delay}s",
            callback_data="set_min_delay",
        )],
        [InlineKeyboardButton(
            f"⏱ MAX_DELAY: {antiban.max_delay}s",
            callback_data="set_max_delay",
        )],
        [InlineKeyboardButton(back_text, callback_data=back_data)],
    ])


def sessions_kb() -> InlineKeyboardMarkup:
    sessions = get_session_files()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📱 Активных сессий: {len(sessions)}",
            callback_data="sess_list",
        )],
        [InlineKeyboardButton("➕ Добавить аккаунт", callback_data="sess_add")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu_main")],
    ])

def admins_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Список админов", callback_data="admin_list")],
        [InlineKeyboardButton("➕ Добавить админа", callback_data="admin_add")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu_main")],
    ])


def mailing_kb() -> InlineKeyboardMarkup:
    sessions = get_session_files()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📱 Сессий: {len(sessions)}",
            callback_data="mail_sessions_info",
        )],
        [InlineKeyboardButton("✏️ Начать рассылку", callback_data="mail_start")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu_main")],
    ])

def mailing_modes_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Текст", callback_data="mail_mode_text"),
         InlineKeyboardButton("🖼 Медиа", callback_data="mail_mode_media")],
        [InlineKeyboardButton("🔄 Репост", callback_data="mail_mode_repost"),
         InlineKeyboardButton("📹 Кружочек", callback_data="mail_mode_video_note")],
        [InlineKeyboardButton("🎙 Голосовое", callback_data="mail_mode_voice"),
         InlineKeyboardButton("🔁 Скрытый репост", callback_data="mail_mode_hidden_repost")],
        [InlineKeyboardButton("🤖 PostBot", callback_data="mail_mode_postbot"),
         InlineKeyboardButton("🔒 Секретный чат", callback_data="mail_mode_secret_chat")],
        [InlineKeyboardButton("🔙 Назад к рассылке", callback_data="menu_mail")]
    ])

def parser_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Участники групп", callback_data="parse_src_group")],
        [InlineKeyboardButton("💬 Комментаторы (Канал)", callback_data="parse_src_comments")],
        [InlineKeyboardButton("⏱ Писавшие в чат", callback_data="parse_src_history")],
        [InlineKeyboardButton("🧠 Smart AI-Parser", callback_data="parse_src_smart")],
        [InlineKeyboardButton("🔙 Назад", callback_data="menu_main")]
    ])

def parser_filters_kb(uid: int) -> InlineKeyboardMarkup:
    md = _mailing_data.get(uid, {})
    f = md.get("filters", {"require_photo": False, "require_premium": False, "sex": "all", "online_filter": "all", "days_back": 0})
    
    sex_map = {"all": "Неважно", "male": "Только Мужчины", "female": "Только Женщины"}
    online_map = {"all": "Неважно", "1d": "Был сегодня", "7d": "Был на неделе", "30d": "Был в этом месяце"}
    photo_str = "📸 Обязательно" if f["require_photo"] else "Неважно"
    premium_str = "🌟 Обязательно" if f["require_premium"] else "Неважно"
    days = f.get("days_back", 0)
    days_str = f"За {days} дней" if days > 0 else "Неважно"
    
    sessions = get_session_files()
    selected = md.get("selected_session", sessions[0].stem if sessions else "Нет")

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Пол: {sex_map[f['sex']]}", callback_data="parse_filter_sex")],
        [InlineKeyboardButton(f"Активность: {online_map[f['online_filter']]}", callback_data="parse_filter_online")],
        [InlineKeyboardButton(f"📅 Последние дни: {days_str}", callback_data="parse_filter_days")],
        [InlineKeyboardButton(f"Аватар: {photo_str}", callback_data="parse_filter_photo"),
         InlineKeyboardButton(f"Премиум: {premium_str}", callback_data="parse_filter_premium")],
        [InlineKeyboardButton(f"📱 Выбрать аккаунт ({selected})", callback_data="parse_select_acc")],
        [InlineKeyboardButton("🚀 НАЧАТЬ ПАРСИНГ", callback_data="parse_start_engine")],
        [InlineKeyboardButton("🔙 В меню", callback_data="menu_main")]
    ])

def select_accounts_kb(mode: str, selected_list: list) -> InlineKeyboardMarkup:
    # mode is either 'mail' or 'parse'
    sessions = get_session_files()
    buttons = []
    
    for s in sessions:
        name = s.stem
        is_sel = name in selected_list
        icon = "✅" if is_sel else "⬜️"
        buttons.append([InlineKeyboardButton(f"{icon} {name}", callback_data=f"toggle_acc_{mode}_{name}")])
        
    buttons.append([InlineKeyboardButton("🔙 Назад", callback_data=f"menu_{mode}_back")])
    return InlineKeyboardMarkup(buttons)


def back_main_kb(uid: int) -> InlineKeyboardMarkup:
    md = _mailing_data.get(uid, {})
    back_data = "mail_confirm_refresh" if md.get("targets") else "menu_main"
    back_text = "🔙 Вернуться к рассылке" if md.get("targets") else "🔙 Главное меню"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(back_text, callback_data=back_data)],
    ])


def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [KeyboardButton("📱 Главное меню")],
        [KeyboardButton("🚀 Открыть пульт", web_app=WebAppInfo(url=get_app_url(config.ADMIN_ID)))]
    ], resize_keyboard=True)

async def show_mail_confirm(uid: int, message: Message, edit: bool = False):
    md = _mailing_data.get(uid, {})
    targets = md.get("targets", [])
    text = md.get("text", "")
    mode = md.get("mode", "text")
    
    all_sessions = get_session_files()
    selected_sessions = md.get("selected_sessions", [s.stem for s in all_sessions])
    md["selected_sessions"] = selected_sessions # save back if it was empty
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Изменить текст", callback_data="mail_edit_text"),
         InlineKeyboardButton("👥 Изменить базу", callback_data="mail_edit_targets")],
        [InlineKeyboardButton(f"🕹 Режим: {mode}", callback_data="mail_edit_mode"),
         InlineKeyboardButton(f"📱 Аки: {len(selected_sessions)}/{len(all_sessions)}", callback_data="mail_select_accs")],
        [InlineKeyboardButton("⚙️ Настройки/Задержки", callback_data="menu_settings")],
        [InlineKeyboardButton("🔄 Обновить инфу", callback_data="mail_confirm_refresh")],
        [InlineKeyboardButton("🚀 Подтвердить и ЗАПУСТИТЬ", callback_data="mail_start_engine")],
        [InlineKeyboardButton("❌ Отмена", callback_data="mail_cancel")]
    ])
    
    # We access global active_proxy and antiban implicitly
    proxy_status = '✅ ' + active_proxy['ip'] if active_proxy else '❌ Нет'
    text_preview = text[:100] + ('...' if len(text) > 100 else '') if text else "[Отсутствует]"
    
    msg_text = (
        f"📋 **Подтверждение рассылки**\n\n"
        f"🕹 **Режим:** `{mode.upper()}`\n"
        f"📝 **Контент:** `{text_preview}`\n"
        f"👥 **Получателей:** {len(targets)}\n"
        f"📱 **Выбрано аккаунтов:** {len(selected_sessions)} из {len(all_sessions)}\n"
        f"🌐 **Прокси:** {proxy_status}\n\n"
        f"⚙️ **Текущие настройки:**\n"
        f"  ├ Задержка: `{antiban.min_delay}с - {antiban.max_delay}с`\n"
        f"  └ Лимит на акк: `{antiban.account_limit}` | Общий: `{antiban.global_limit}`\n\n"
        f"Всё верно? Запускаем?"
    )
    
    if edit:
        await _safe_edit(message, msg_text, reply_markup=kb)
    else:
        await message.reply(msg_text, reply_markup=kb)

# ── Bot setup ──────────────────────────────────────────────────────────

def register_handlers(bot: Client):
    """Configure the management bot handlers."""
    register_smart_parser_handlers(bot)

    # ── /start ─────────────────────────────────────────────────────

    @bot.on_message(filters.command("start") & filters.private)
    @admin_only
    async def cmd_start(client: Client, message: Message):
        uid = message.from_user.id
        _user_state.pop(uid, None)
        _mailing_data.pop(uid, None)
        await message.reply(
            "🤖 **Telegram Combiner** — управляющий модуль\n\n"
            f"🆔 Твой ID: `{uid}`\n\n"
            "Выбери раздел ниже:",
            reply_markup=main_reply_kb(),
        )
        await message.reply("Меню:", reply_markup=main_menu_kb())

    # ── Callback router ────────────────────────────────────────────

    @bot.on_callback_query()
    @admin_only
    async def on_callback(client: Client, cb: CallbackQuery):
        data = cb.data

        # ── Main menu ──────────────────────────────────────────────
        if data == "menu_main":
            _user_state.pop(cb.from_user.id, None)
            _mailing_data.pop(cb.from_user.id, None)
            await _safe_edit(
                cb.message,
                "🤖 **Telegram Combiner**\n\nВыбери раздел:",
                reply_markup=main_menu_kb(),
            )

        # ── Net menu ──────────────────────────────────────────────
        elif data == "menu_net":
            await _safe_edit(
                cb.message,
                "🌐 **Сеть**\n\nИспользовать прокси?",
                reply_markup=net_proxy_kb(),
            )

        elif data == "proxy_no":
            await cb.answer("⏳ Проверяю IP...")
            try:
                info = await check_current_ip()
                text = (
                    f"🌐 **Текущий IP**\n\n"
                    f"🔹 IP: `{info['ip']}`\n"
                    f"🔹 Страна: {info['country']}\n"
                    f"🔹 Провайдер: {info['isp']}\n"
                    f"🔹 Hosting: {'⚠️ Да' if info['hosting'] else '✅ Нет'}\n"
                    f"🔹 Proxy-флаг: {'⚠️ Да' if info['proxy_flag'] else '✅ Нет'}"
                )
                if info["warning"]:
                    text += f"\n\n{info['warning']}"
            except Exception as exc:
                text = f"❌ Ошибка проверки IP: {exc}"

            global active_proxy
            active_proxy = None
            await _safe_edit(cb.message, text, reply_markup=back_main_kb(cb.from_user.id))

        elif data == "proxy_yes":
            await _safe_edit(
                cb.message,
                "🌐 **Прокси**\n\nВыбери действие:",
                reply_markup=proxy_submenu_kb(),
            )

        elif data == "proxy_list":
            proxies = list_proxies()
            if not proxies:
                await _safe_edit(
                    cb.message,
                    "📋 Список прокси пуст.\nДобавь прокси через кнопку ➕",
                    reply_markup=proxy_submenu_kb(),
                )
                return

            buttons = []
            for i, p in enumerate(proxies):
                label = f"{p['ip']}:{p['port']} ({p['user']})"
                buttons.append([
                    InlineKeyboardButton(label, callback_data=f"proxy_select_{i}"),
                    InlineKeyboardButton("🗑", callback_data=f"proxy_del_{i}"),
                ])
            buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="proxy_yes")])

            await _safe_edit(
                cb.message,
                "📋 **Список прокси** — нажми для выбора:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif data.startswith("proxy_select_"):
            idx = int(data.split("_")[-1])
            proxies = list_proxies()
            if idx >= len(proxies):
                await cb.answer("❌ Прокси не найден")
                return
            proxy = proxies[idx]
            await cb.answer("⏳ Проверяю прокси...")
            ok, msg = await validate_proxy(proxy)
            if ok:
                active_proxy = proxy
                try:
                    info = await check_current_ip(proxy)
                    text = (
                        f"{msg}\n\n"
                        f"🔹 IP: `{info['ip']}`\n"
                        f"🔹 Страна: {info['country']}\n"
                        f"🔹 Провайдер: {info['isp']}"
                    )
                    if info["warning"]:
                        text += f"\n\n{info['warning']}"
                except Exception:
                    text = msg
            else:
                text = msg

            await _safe_edit(cb.message, text, reply_markup=back_main_kb(cb.from_user.id))

        elif data.startswith("proxy_del_"):
            idx = int(data.split("_")[-1])
            if remove_proxy(idx):
                await cb.answer("🗑 Прокси удалён")
            else:
                await cb.answer("❌ Ошибка удаления")
            # Refresh list
            cb.data = "proxy_list"
            await on_callback(client, cb)
            return

        # ── Привязка прокси к аккаунту (свой sticky-IP на учётку) ──
        elif data == "proxy_assign":
            sessions = get_session_files()
            if not sessions:
                await _safe_edit(
                    cb.message,
                    "❌ Нет аккаунтов. Сначала добавь сессию.",
                    reply_markup=proxy_submenu_kb(),
                )
                return
            bindings = list_session_proxies()
            buttons = []
            for i, s in enumerate(sessions):
                p = bindings.get(s.stem)
                mark = f"🔗 {p['ip']}" if p else "🌐 WARP"
                buttons.append([InlineKeyboardButton(f"{s.stem} — {mark}", callback_data=f"proxyacc_s_{i}")])
            buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="proxy_yes")])
            await _safe_edit(
                cb.message,
                "🔗 **Прокси на аккаунт**\n\nУ каждой учётки — свой IP, чтобы TG не связал аккаунты. Выбери аккаунт:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif data.startswith("proxyacc_s_"):
            idx = int(data.split("_")[-1])
            sessions = get_session_files()
            if idx >= len(sessions):
                await cb.answer("❌ Аккаунт не найден")
                return
            name = sessions[idx].stem
            proxies = list_proxies()
            cur = get_proxy_for_session(name)
            cur_txt = f"🔗 {cur['ip']}:{cur['port']}" if cur else "🌐 WARP (общий)"
            buttons = []
            for j, p in enumerate(proxies):
                buttons.append([InlineKeyboardButton(f"{p['ip']}:{p['port']} ({p['user']})", callback_data=f"proxyacc_set_{idx}_{j}")])
            if cur:
                buttons.append([InlineKeyboardButton("🧹 Убрать (вернуть на WARP)", callback_data=f"proxyacc_clear_{idx}")])
            if not proxies:
                buttons.append([InlineKeyboardButton("➕ Сначала добавь прокси", callback_data="proxy_add")])
            buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="proxy_assign")])
            await _safe_edit(
                cb.message,
                f"Аккаунт `{name}`\nСейчас: {cur_txt}\n\nВыбери прокси для этого аккаунта:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif data.startswith("proxyacc_set_"):
            parts = data.split("_")  # proxyacc_set_{sidx}_{pidx}
            sidx, pidx = int(parts[2]), int(parts[3])
            sessions = get_session_files()
            proxies = list_proxies()
            if sidx >= len(sessions) or pidx >= len(proxies):
                await cb.answer("❌ Не найдено")
                return
            name = sessions[sidx].stem
            proxy = proxies[pidx]
            await cb.answer("⏳ Проверяю прокси...")
            ok, msg = await validate_proxy(proxy)
            if ok:
                set_proxy_for_session(name, proxy)
                text = (
                    f"✅ Прокси `{proxy['ip']}:{proxy['port']}` привязан к аккаунту `{name}`.\n\n"
                    f"⚠️ Применится после ПЕРЕЗАПУСКА бота — аккаунт заново поднимется уже на этом прокси. "
                    f"Пока сессия активна, она продолжает работать на старом IP (резкая смена IP на лету — плохой сигнал для TG)."
                )
            else:
                text = f"{msg}\n\nПрокси НЕ привязан."
            await _safe_edit(cb.message, text, reply_markup=back_main_kb(cb.from_user.id))

        elif data.startswith("proxyacc_clear_"):
            idx = int(data.split("_")[-1])
            sessions = get_session_files()
            if idx < len(sessions):
                unset_proxy_for_session(sessions[idx].stem)
                await cb.answer("🧹 Прокси убран — аккаунт вернётся на WARP")
            cb.data = "proxy_assign"
            await on_callback(client, cb)
            return

        elif data == "proxy_add":
            _user_state[cb.from_user.id] = "waiting_proxy"
            await _safe_edit(
                cb.message,
                "➕ **Добавить прокси**\n\n"
                "Отправь прокси в формате:\n"
                "`ip:port:user:pass`",
                reply_markup=back_main_kb(cb.from_user.id),
            )

        # ── Sessions menu ─────────────────────────────────────────
        elif data == "menu_sessions":
            await _safe_edit(
                cb.message,
                "📱 **Управление сессиями**\n\n"
                "Здесь можно добавить аккаунт (по номеру телефона)\n"
                "или посмотреть список активных сессий.",
                reply_markup=sessions_kb(),
            )

        elif data == "sess_list":
            sessions = get_session_files()
            if sessions:
                text = f"📱 **Сессии** ({len(sessions)}):\nНажми 🗑 чтобы удалить:"
                buttons = []
                for s in sessions:
                    buttons.append([
                        InlineKeyboardButton(f"📱 {s.stem}", callback_data=f"sess_info_{s.stem}"),
                        InlineKeyboardButton("🗑", callback_data=f"sess_del_{s.stem}"),
                    ])
                buttons.append([InlineKeyboardButton("➕ Добавить аккаунт", callback_data="sess_add")])
                buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="menu_main")])
                await _safe_edit(cb.message, text, reply_markup=InlineKeyboardMarkup(buttons))
            else:
                text = "📱 Нет авторизованных сессий.\nНажми «➕ Добавить аккаунт»."
                await _safe_edit(cb.message, text, reply_markup=sessions_kb())

        elif data.startswith("sess_del_"):
            stem = data[len("sess_del_"):]
            session_file = SESSIONS_DIR / f"{stem}.session"
            journal = SESSIONS_DIR / f"{stem}.session-journal"
            if session_file.exists():
                os.remove(session_file)
                if journal.exists():
                    os.remove(journal)
                await cb.answer(f"🗑 Сессия {stem} удалена")
                logger.info("Session deleted: %s", stem)
            else:
                await cb.answer("❌ Файл сессии не найден")
            # Refresh list
            cb.data = "sess_list"
            await on_callback(client, cb)
            return

        elif data.startswith("sess_info_"):
            stem = data[len("sess_info_"):]
            await cb.answer(f"Сессия: {stem}")

        elif data == "sess_add":
            _user_state[cb.from_user.id] = "auth_phone"
            await _safe_edit(
                cb.message,
                "📞 **Авторизация аккаунта**\n\n"
                "Отправь номер телефона в международном формате:\n"
                "Например: `+79991234567`",
                reply_markup=back_main_kb(cb.from_user.id),
            )

        elif data == "sess_resend":
            auth = _auth_data.get(cb.from_user.id)
            if not auth:
                await cb.answer("❌ Сессия авторизации не найдена или истекла.", show_alert=True)
                return

            client_to_use: Client = auth["client"]
            phone = auth["phone"]
            phone_code_hash = auth["phone_code_hash"]

            await cb.answer("⏳ Запрашиваю SMS...")
            try:
                sent_code = await client_to_use.resend_code(phone, phone_code_hash)
                # Update hash just in case
                auth["phone_code_hash"] = sent_code.phone_code_hash

                text = cb.message.text
                if text:
                    text = text.replace("в приложение Telegram", "по SMS")
                else:
                    text = f"📲 Код запрошен повторно (по SMS) на номер `{phone}`.\n\nОтправь полученный код (цифры):"

                await _safe_edit(cb.message, text, reply_markup=back_main_kb(cb.from_user.id))
            except Exception as exc:
                logger.error("resend_code failed: %s", getattr(exc, "MESSAGE", str(exc)))
                await cb.answer(f"❌ Ошибка: {getattr(exc, 'MESSAGE', str(exc))}", show_alert=True)

        # ── Admins menu ──────────────────────────────────────────
        elif data == "menu_admins":
            await _safe_edit(
                cb.message,
                "👥 **Управление администраторами**\n\n"
                "Вы можете выдать доступ к боту и WebApp другим пользователям.",
                reply_markup=admins_kb(),
            )

        elif data == "admin_list":
            admins = config.get_allowed_admins()
            text = f"👥 **Админы** ({len(admins)}):\nНажмите 🗑 для удаления:"
            buttons = []
            for ad in admins:
                # the primary admin cannot be deleted
                if ad == config.ADMIN_ID:
                    buttons.append([InlineKeyboardButton(f"👑 {ad} (Владелец)", callback_data="noop")])
                else:
                    buttons.append([
                        InlineKeyboardButton(f"👤 {ad}", callback_data="noop"),
                        InlineKeyboardButton("🗑", callback_data=f"admin_del_{ad}"),
                    ])
            buttons.append([InlineKeyboardButton("➕ Добавить админа", callback_data="admin_add")])
            buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="menu_main")])
            await _safe_edit(cb.message, text, reply_markup=InlineKeyboardMarkup(buttons))

        elif data.startswith("admin_del_"):
            target_uid = int(data.split("_")[-1])
            if config.remove_admin(target_uid):
                await cb.answer(f"🗑 Админ {target_uid} удален")
            else:
                await cb.answer("❌ Ошибка удаления")
            cb.data = "admin_list"
            await on_callback(client, cb)
            return

        elif data == "admin_add":
            _user_state[cb.from_user.id] = "wait_admin_id"
            await _safe_edit(
                cb.message,
                "➕ **Добавить администратора**\n\n"
                "Отправьте Telegram ID пользователя (число).",
                reply_markup=back_main_kb(cb.from_user.id),
            )

        # ── Settings menu ──────────────────────────────────────────
        elif data == "menu_settings":
            await _safe_edit(
                cb.message,
                "⚙️ **Настройки**\n\n"
                "Нажми на параметр для изменения:",
                reply_markup=settings_kb(cb.from_user.id),
            )

        elif data == "set_global_limit":
            _user_state[cb.from_user.id] = "set_global_limit"
            await _safe_edit(
                cb.message,
                f"🔢 **GLOBAL_LIMIT** (текущий: {antiban.global_limit})\n\n"
                "Отправь новое значение (число):",
                reply_markup=back_main_kb(cb.from_user.id),
            )

        elif data == "set_account_limit":
            _user_state[cb.from_user.id] = "set_account_limit"
            await _safe_edit(
                cb.message,
                f"📱 **ACCOUNT_LIMIT** (текущий: {antiban.account_limit})\n\n"
                "Отправь новое значение (число):",
                reply_markup=back_main_kb(cb.from_user.id),
            )

        elif data == "set_min_delay":
            _user_state[cb.from_user.id] = "set_min_delay"
            await _safe_edit(
                cb.message,
                f"⏱ **MIN_DELAY** (текущий: {antiban.min_delay}s)\n\n"
                "Отправь новое значение (число, секунды):",
                reply_markup=back_main_kb(cb.from_user.id),
            )

        elif data == "set_max_delay":
            _user_state[cb.from_user.id] = "set_max_delay"
            await _safe_edit(
                cb.message,
                f"⏱ **MAX_DELAY** (текущий: {antiban.max_delay}s)\n\n"
                "Отправь новое значение (число, секунды):",
                reply_markup=back_main_kb(cb.from_user.id),
            )

        # ── Mailing menu ──────────────────────────────────────────
        elif data == "menu_mail":
            await _safe_edit(
                cb.message,
                "📨 **Рассылка**\n\n"
                f"📱 Сессий: {len(get_session_files())}\n"
                f"🌐 Прокси: {'✅ ' + active_proxy['ip'] if active_proxy else '❌ Нет'}\n"
                f"🔢 Лимит: {antiban.global_limit} (глоб.) / {antiban.account_limit} (акк.)\n"
                f"⏱ Задержка: {antiban.min_delay}–{antiban.max_delay}s",
                reply_markup=mailing_kb(),
            )

        elif data == "mail_sessions_info":
            sessions = get_session_files()
            if sessions:
                names = "\n".join(f"  • `{s.stem}`" for s in sessions)
                text = f"📱 **Доступные сессии** ({len(sessions)}):\n{names}"
            else:
                text = (
                    "📱 Нет .session файлов.\n\n"
                    "Добавь аккаунты через раздел «📱 Сессии»."
                )
            await _safe_edit(cb.message, text, reply_markup=mailing_kb())

        elif data == "mail_start":
            sessions = get_session_files()
            if not sessions:
                await cb.answer("❌ Нет сессий! Сначала добавь аккаунт.", show_alert=True)
                return
            _user_state[cb.from_user.id] = "mail_text"
            _mailing_data[cb.from_user.id] = {}
            await _safe_edit(
                cb.message,
                "✏️ **Текст рассылки**\n\n"
                "Отправь текст сообщения.\n"
                "Поддерживается спинтакс:\n"
                "`{Привет|Здравствуйте}, {как дела|какие условия}?`",
                reply_markup=back_main_kb(cb.from_user.id),
            )

        elif data == "mail_edit_text":
            _user_state[cb.from_user.id] = "mail_text"
            _mailing_data.setdefault(cb.from_user.id, {})
            await _safe_edit(
                cb.message,
                "✏️ **Изменение текста рассылки**\n\nОтправь новый текст или `.txt` файл.",
                reply_markup=back_main_kb(cb.from_user.id),
            )

        elif data == "mail_edit_targets":
            _user_state[cb.from_user.id] = "mail_targets"
            _mailing_data.setdefault(cb.from_user.id, {})
            await _safe_edit(
                cb.message,
                "👥 **Изменение базы**\n\nОтправь новый список получателей или `.txt` файл.",
                reply_markup=back_main_kb(cb.from_user.id),
            )
            
        elif data == "mail_edit_mode":
            await _safe_edit(
                cb.message,
                "🕹 **Выбор режима рассылки**\n\nЧто именно мы будем рассылать?",
                reply_markup=mailing_modes_kb(),
            )
            
        elif data.startswith("mail_mode_"):
            mode = data.replace("mail_mode_", "")
            _mailing_data.setdefault(cb.from_user.id, {})["mode"] = mode
            await cb.answer(f"✅ Режим {mode.upper()} выбран!")
            await show_mail_confirm(cb.from_user.id, cb.message, edit=True)
            
        elif data == "mail_confirm_refresh":
            await show_mail_confirm(cb.from_user.id, cb.message, edit=True)

        elif data == "mail_start_engine":
            md = _mailing_data.get(cb.from_user.id, {})
            targets = md.get("targets", [])
            template = md.get("text", "")
            mode = md.get("mode", "text")

            if not targets or not template:
                await cb.answer("❌ Нет данных для рассылки", show_alert=True)
                return

            _user_state.pop(cb.from_user.id, None)
            await _safe_edit(cb.message, "🚀 Запускаю рассылку...")

            asyncio.create_task(
                run_advanced_mailing(
                    bot=client,
                    admin_id=config.ADMIN_ID,
                    targets=targets,
                    config={
                        "mode": mode,
                        "text": template,
                    },
                    antiban=antiban,
                    proxy=active_proxy,
                    selected_sessions=md.get("selected_sessions") # Pass explicitly
                )
            )

        elif data == "mail_cancel":
            _user_state.pop(cb.from_user.id, None)
            _mailing_data.pop(cb.from_user.id, None)
            await _safe_edit(
                cb.message,
                "❌ Рассылка отменена.",
                reply_markup=main_menu_kb(),
            )
            
        elif data == "mail_select_accs":
            md = _mailing_data.get(cb.from_user.id, {})
            selected = md.get("selected_sessions", [s.stem for s in get_session_files()])
            await _safe_edit(
                cb.message, 
                "📱 **Выбор аккаунтов для рассылки**\n\nОтметьте те, с которых пойдет спам:",
                reply_markup=select_accounts_kb("mail", selected)
            )
            
        elif data == "menu_mail_back":
            await show_mail_confirm(cb.from_user.id, cb.message, edit=True)
            
        # ── Parser menu ──────────────────────────────────────────
        elif data == "menu_parser":
            sessions = get_session_files()
            if not sessions:
                await cb.answer("❌ Нет сессий для парсинга!", show_alert=True)
                return
            await _safe_edit(
                cb.message,
                "🔍 **Парсинг аудитории**\n\nВыбери источник, откуда собирать пользователей:",
                reply_markup=parser_main_kb(),
            )
            
        elif data.startswith("parse_src_"):
            source = data.replace("parse_src_", "")
            _user_state[cb.from_user.id] = f"wait_parser_chat"
            _mailing_data[cb.from_user.id] = {"parser_src": source}
            await _safe_edit(
                cb.message,
                f"📥 **Режим:** {source.upper()}\n\n"
                "Отправь юзернейм (или ссылку) чата/группы, откуда будем парсить:",
                reply_markup=back_main_kb(cb.from_user.id),
            )

        elif data.startswith("parse_filter_"):
            action = data.replace("parse_filter_", "")
            md = _mailing_data.setdefault(cb.from_user.id, {})
            f = md.setdefault("filters", {"require_photo": False, "require_premium": False, "sex": "all", "online_filter": "all", "days_back": 0})

            if action == "sex":
                nxt = {"all": "male", "male": "female", "female": "all"}
                f["sex"] = nxt[f["sex"]]
            elif action == "online":
                nxt = {"all": "1d", "1d": "7d", "7d": "30d", "30d": "all"}
                f["online_filter"] = nxt[f["online_filter"]]
            elif action == "photo":
                f["require_photo"] = not f["require_photo"]
            elif action == "premium":
                f["require_premium"] = not f["require_premium"]
            elif action == "days":
                nxt = {0: 1, 1: 3, 3: 7, 7: 14, 14: 30, 30: 0}
                f["days_back"] = nxt.get(f.get("days_back", 0), 0)
                
            await _safe_edit(cb.message, "⚙️ **Фильтры парсинга**\n\nНастрой критерии отбора:", reply_markup=parser_filters_kb(cb.from_user.id))
            
        elif data == "parse_select_acc":
            md = _mailing_data.get(cb.from_user.id, {})
            selected = [md.get("selected_session", get_session_files()[0].stem if get_session_files() else "")]
            await _safe_edit(
                cb.message, 
                "📱 **Выбор аккаунта для парсера**\n\nПарсер собирает в 1 поток. Выбери акк:",
                reply_markup=select_accounts_kb("parse", selected)
            )
            
        elif data == "menu_parse_back":
            await _safe_edit(cb.message, "⚙️ **Фильтры парсинга**\n\nНастрой критерии отбора:", reply_markup=parser_filters_kb(cb.from_user.id))

        elif data == "parse_start_engine":
            md = _mailing_data.get(cb.from_user.id, {})
            if not md.get("parser_src"):
                await cb.answer("❌ Ошибка: нет данных", show_alert=True)
                return
            await _safe_edit(cb.message, "🚀 Запускаю парсинг аудитории в фоне...")
            asyncio.create_task(run_parser_task(client, config.ADMIN_ID, md))
            
        # ── Toggle Callbacks ──────────────────────────────────────
        elif data.startswith("toggle_acc_"):
            parts = data.split("_")
            mode = parts[2] # 'mail' or 'parse'
            acc_name = "_".join(parts[3:])
            
            md = _mailing_data.setdefault(cb.from_user.id, {})
            if mode == "mail":
                all_sessions = [s.stem for s in get_session_files()]
                selected = md.get("selected_sessions", all_sessions)
                if acc_name in selected:
                    selected.remove(acc_name)
                else:
                    selected.append(acc_name)
                md["selected_sessions"] = selected
                await _safe_edit(cb.message, "📱 **Выбор аккаунтов для рассылки**\n\nОтметьте те, с которых пойдет спам:", reply_markup=select_accounts_kb("mail", selected))
            elif mode == "parse":
                md["selected_session"] = acc_name
                # go back automatically
                await _safe_edit(cb.message, "⚙️ **Фильтры парсинга**\n\nНастрой критерии отбора:", reply_markup=parser_filters_kb(cb.from_user.id))

        try:
            await cb.answer()
        except Exception:
            pass

    # ── Text messages (FSM input) ──────────────────────────────────

    @bot.on_message((filters.text | filters.document) & filters.private & ~filters.command(["start"]))
    @admin_only
    async def on_text(client: Client, message: Message):
        uid = message.from_user.id
        state = _user_state.get(uid)

        logger.info("on_text: uid=%s state=%s type=%s", uid, state, "document" if message.document else "text")

        text_content = getattr(message, "text", "") or getattr(message, "caption", "")
        if text_content.strip() == "📱 Главное меню":
            _user_state.pop(uid, None)
            _mailing_data.pop(uid, None)
            await message.reply(
                "🤖 **Telegram Combiner**\n\nВыбери раздел ниже:",
                reply_markup=main_reply_kb(),
            )
            await message.reply("Меню:", reply_markup=main_menu_kb())
            return

        # ── Smart Parser text input ──
        handled, reply_text, reply_kb = handle_smart_parser_text(uid, text_content.strip() if text_content else "")
        if handled:
            if reply_text:
                await message.reply(reply_text, reply_markup=reply_kb)
            return

        if not state:
            return

        # ── Parse document or raw text
        if message.document and message.document.file_name.endswith('.txt'):
            await message.reply("⏳ Читаю текстовый файл...")
            doc_path = await message.download()
            with open(doc_path, 'r', encoding='utf-8') as f:
                raw_text = f.read()
            import os
            os.remove(doc_path)
            text = raw_text.strip()
        else:
            text = text_content.strip()

        # Save back to text for downstream compatibility
        message.text = text



        # ── Proxy input ────────────────────────────────────────────
        if state == "waiting_proxy":
            proxy = parse_proxy_string(text)
            if not proxy:
                await message.reply(
                    "❌ Неверный формат. Ожидается: `ip:port:user:pass`"
                )
                return

            await message.reply("⏳ Проверяю прокси...")
            ok, msg = await validate_proxy(proxy)
            if ok:
                idx = add_proxy(proxy)
                await message.reply(
                    f"{msg}\n✅ Прокси добавлен (#{idx + 1})",
                    reply_markup=proxy_submenu_kb(),
                )
            else:
                await message.reply(
                    f"{msg}\nПрокси **не добавлен**. Попробуй другой.",
                    reply_markup=proxy_submenu_kb(),
                )
            _user_state.pop(uid, None)

        # ── Settings input ─────────────────────────────────────────
        elif state.startswith("set_"):
            try:
                value = float(text)
            except ValueError:
                await message.reply("❌ Ожидается число. Попробуй снова.")
                return

            param_name = state.replace("set_", "")
            if param_name == "global_limit":
                antiban.global_limit = int(value)
            elif param_name == "account_limit":
                antiban.account_limit = int(value)
            elif param_name == "min_delay":
                antiban.min_delay = value
            elif param_name == "max_delay":
                antiban.max_delay = value

            _user_state.pop(uid, None)
            await message.reply(
                f"✅ `{param_name}` установлен: **{value}**",
                reply_markup=settings_kb(uid),
            )

        # ── Parser input ───────────────────────────────────────────
        elif state == "wait_parser_chat":
            _mailing_data.setdefault(uid, {})["chat"] = text
            _user_state[uid] = "wait_parser_limit"
            await message.reply(
                "🔢 Отправь лимит пользователей для сбора (0 = парсить всех):",
                reply_markup=back_main_kb(uid)
            )

        elif state == "wait_parser_limit":
            try:
                limit = int(text)
            except ValueError:
                await message.reply("❌ Ожидается число. Попробуй снова.", reply_markup=back_main_kb(uid))
                return
                
            md = _mailing_data.get(uid, {})
            md["limit"] = limit
            _user_state.pop(uid, None)
            md.setdefault("filters", {"require_photo": False, "require_premium": False, "sex": "all", "online_filter": "all", "days_back": 0})
            
            await message.reply("⚙️ **Фильтры парсинга**\n\nНастрой критерии отбора:", reply_markup=parser_filters_kb(uid))

        # ── Add Admin ──────────────────────────────────────────────
        elif state == "wait_admin_id":
            if text.isdigit():
                if config.add_admin(int(text)):
                    await message.reply(f"✅ Пользователь `{text}` добавлен в список администраторов.", reply_markup=admins_kb())
                else:
                    await message.reply(f"ℹ️ Данный пользователь уже является администратором.", reply_markup=admins_kb())
            else:
                await message.reply("❌ ID должен состоять только из цифр. Попробуйте еще раз.", reply_markup=back_main_kb(uid))
            _user_state.pop(uid, None)
            
        # ── Session auth: phone ────────────────────────────────────
        elif state == "auth_phone":
            phone = text.replace(" ", "").replace("-", "")
            if not phone.startswith("+"):
                phone = "+" + phone

            logger.info("Auth: sending code to %s", phone)
            await message.reply(f"⏳ Отправляю код на `{phone}`...")

            session_name = phone.replace("+", "").replace(" ", "")
            session_path = str(SESSIONS_DIR / f"{session_name}_pending")

            user_client = None
            try:
                # Фиксируем девайс под финальный stem сессии (без _pending),
                # чтобы после сохранения учётка не сменила устройство при первом запуске.
                device = get_device_for_session(session_name)
                user_client = Client(
                    name=session_path,
                    api_id=API_ID,
                    api_hash=API_HASH,
                    device_model=device["device_model"],
                    system_version=device["system_version"],
                    app_version=device["app_version"],
                    lang_code="ru",
                    proxy=config.PYROGRAM_PROXY
                )
                await user_client.connect()
                sent_code = await user_client.send_code(phone)

                # Determine delivery method using Pyrogram enum
                from pyrogram.enums import SentCodeType as SCT
                code_type = sent_code.type
                logger.info(
                    "Auth: code sent to %s, hash=%s, type=%r",
                    phone, sent_code.phone_code_hash[:8], code_type,
                )

                delivery_hints = {
                    SCT.APP: "📱 Код отправлен **в приложение Telegram** — ищи сообщение от «Telegram» (служебный аккаунт с синей галочкой).",
                    SCT.SMS: "💬 Код отправлен по **SMS** на номер.",
                    SCT.CALL: "📞 Telegram сейчас **позвонит** — код продиктуют голосом.",
                    SCT.FLASH_CALL: "📞 Придёт **быстрый звонок** — код = последние цифры номера.",
                    SCT.MISSED_CALL: "📞 Придёт **пропущенный звонок** — код = последние цифры номера.",
                    SCT.FRAGMENT_SMS: "💬 Код отправлен через **Fragment** (анонимный номер).",
                    SCT.EMAIL_CODE: "📧 Код отправлен на **привязанный email**.",
                }
                hint = delivery_hints.get(code_type, f"📨 Способ: `{code_type}`")

                _auth_data[uid] = {
                    "client": user_client,
                    "phone": phone,
                    "phone_code_hash": sent_code.phone_code_hash,
                    "session_name": session_name,
                }
                _user_state[uid] = "auth_code"

                # Build keyboard — resend via SMS if current method is APP
                kb_buttons = [[InlineKeyboardButton("🔙 Главное меню", callback_data="menu_main")]]
                if code_type == SCT.APP:
                    kb_buttons.insert(0, [InlineKeyboardButton("📩 Переотправить по SMS", callback_data="sess_resend")])

                await message.reply(
                    f"📲 Код отправлен на `{phone}`\n\n"
                    f"{hint}\n\n"
                    "Отправь полученный код (цифры):",
                    reply_markup=InlineKeyboardMarkup(kb_buttons),
                )

            except Exception as exc:
                logger.error("send_code failed for %s: %s\n%s", phone, exc, traceback.format_exc())
                await message.reply(
                    f"❌ Ошибка отправки кода: `{exc}`",
                    reply_markup=sessions_kb(),
                )
                _user_state.pop(uid, None)
                if user_client:
                    try:
                        await user_client.disconnect()
                    except Exception:
                        pass
                    import os
                    old_session = str(SESSIONS_DIR / f"{session_name}_pending.session")
                    if os.path.exists(old_session):
                        os.remove(old_session)

        # ── Session auth: code ─────────────────────────────────────
        elif state == "auth_code":
            auth = _auth_data.get(uid)
            if not auth:
                _user_state.pop(uid, None)
                await message.reply("❌ Сессия авторизации истекла.", reply_markup=sessions_kb())
                return

            code = text.replace(" ", "").replace("-", "")
            user_client: Client = auth["client"]
            logger.info("Auth: signing in %s with code", auth["phone"])

            try:
                await user_client.sign_in(
                    phone_number=auth["phone"],
                    phone_code_hash=auth["phone_code_hash"],
                    phone_code=code,
                )

                # Verify access by getting account info
                me = await user_client.get_me()
                full_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
                username = f"@{me.username}" if me.username else "—"
                phone_display = f"+{me.phone_number}" if me.phone_number else auth["phone"]

                await user_client.disconnect()
                import os
                old_session = str(SESSIONS_DIR / f"{auth['session_name']}_pending.session")
                new_session = str(SESSIONS_DIR / f"{auth['session_name']}.session")
                if os.path.exists(old_session):
                    os.rename(old_session, new_session)

                _auth_data.pop(uid, None)
                _user_state.pop(uid, None)

                logger.info("Auth: SUCCESS for %s (%s)", auth["phone"], full_name)
                await message.reply(
                    f"✅ **Аккаунт авторизован!**\n\n"
                    f"👤 Имя: **{full_name}**\n"
                    f"🆔 Username: {username}\n"
                    f"📞 Телефон: `{phone_display}`\n"
                    f"📁 Сессия: `{auth['session_name']}.session`",
                    reply_markup=sessions_kb(),
                )

            except SessionPasswordNeeded:
                # 2FA is enabled
                logger.info("Auth: 2FA required for %s", auth["phone"])
                _user_state[uid] = "auth_2fa"
                await message.reply(
                    "🔐 Аккаунт защищён двухфакторной аутентификацией.\n\n"
                    "Отправь пароль 2FA:",
                    reply_markup=back_main_kb(uid),
                )

            except PhoneCodeInvalid:
                await message.reply(
                    "❌ Неверный код. Попробуй ещё раз:\n"
                    "(отправь код повторно)",
                )

            except PhoneCodeExpired:
                _user_state.pop(uid, None)
                _auth_data.pop(uid, None)
                try:
                    await user_client.disconnect()
                except Exception:
                    pass
                import os
                old_session = str(SESSIONS_DIR / f"{auth['session_name']}_pending.session")
                if os.path.exists(old_session):
                    os.remove(old_session)
                await message.reply(
                    "❌ Код истёк. Начни авторизацию заново.",
                    reply_markup=sessions_kb(),
                )

            except Exception as exc:
                logger.error("sign_in failed: %s\n%s", exc, traceback.format_exc())
                _user_state.pop(uid, None)
                _auth_data.pop(uid, None)
                try:
                    await user_client.disconnect()
                except Exception:
                    pass
                import os
                old_session = str(SESSIONS_DIR / f"{auth['session_name']}_pending.session")
                if os.path.exists(old_session):
                    os.remove(old_session)
                await message.reply(
                    f"❌ Ошибка авторизации: `{exc}`",
                    reply_markup=sessions_kb(),
                )

        # ── Session auth: 2FA password ─────────────────────────────
        elif state == "auth_2fa":
            auth = _auth_data.get(uid)
            if not auth:
                _user_state.pop(uid, None)
                await message.reply("❌ Сессия авторизации истекла.", reply_markup=sessions_kb())
                return

            user_client: Client = auth["client"]
            password = text
            logger.info("Auth: checking 2FA for %s", auth["phone"])

            try:
                await user_client.check_password(password)

                # Verify access by getting account info
                me = await user_client.get_me()
                full_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
                username = f"@{me.username}" if me.username else "—"
                phone_display = f"+{me.phone_number}" if me.phone_number else auth["phone"]

                await user_client.disconnect()
                import os
                old_session = str(SESSIONS_DIR / f"{auth['session_name']}_pending.session")
                new_session = str(SESSIONS_DIR / f"{auth['session_name']}.session")
                if os.path.exists(old_session):
                    os.rename(old_session, new_session)

                _auth_data.pop(uid, None)
                _user_state.pop(uid, None)

                logger.info("Auth: 2FA SUCCESS for %s (%s)", auth["phone"], full_name)
                await message.reply(
                    f"✅ **Аккаунт авторизован (2FA)!**\n\n"
                    f"👤 Имя: **{full_name}**\n"
                    f"🆔 Username: {username}\n"
                    f"📞 Телефон: `{phone_display}`\n"
                    f"📁 Сессия: `{auth['session_name']}.session`",
                    reply_markup=sessions_kb(),
                )

            except PasswordHashInvalid:
                await message.reply(
                    "❌ Неверный пароль 2FA. Попробуй ещё раз:",
                )

            except Exception as exc:
                logger.error("2FA check failed: %s\n%s", exc, traceback.format_exc())
                _user_state.pop(uid, None)
                _auth_data.pop(uid, None)
                try:
                    await user_client.disconnect()
                except Exception:
                    pass
                import os
                old_session = str(SESSIONS_DIR / f"{auth['session_name']}_pending.session")
                if os.path.exists(old_session):
                    os.remove(old_session)
                await message.reply(
                    f"❌ Ошибка 2FA: `{exc}`",
                    reply_markup=sessions_kb(),
                )

        # ── Mailing: text input ────────────────────────────────────
        elif state == "mail_text":
            _mailing_data.setdefault(uid, {})["text"] = message.text
            _user_state[uid] = "mail_targets"
            await message.reply(
                "👥 **Получатели**\n\n"
                "Отправь список получателей — по одному на строку.\n"
                "ИЛИ отправь файл `.txt` с базой получателей."
            )

        # ── Mailing: targets input ─────────────────────────────────
        elif state == "mail_targets":
            raw_text = message.text
            targets = [
                line.strip()
                for line in raw_text.split("\n")
                if line.strip()
            ]
            if not targets:
                await message.reply("❌ Пустой список. Отправь получателей повторно.")
                return

            _mailing_data.setdefault(uid, {})["targets"] = targets
            _user_state.pop(uid, None)

            await show_mail_confirm(uid, message, edit=False)

    return bot
