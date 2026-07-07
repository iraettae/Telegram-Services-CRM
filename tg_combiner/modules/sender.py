"""
tg_combiner — Advanced Mailing Engine (12 Modes).
"""
import asyncio
import logging
import random
from pathlib import Path
from typing import Optional, List, Dict, Any

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    FloodWait, PeerIdInvalid, UserIsBlocked, InputUserDeactivated,
    PeerFlood, UserBannedInChannel, RPCError
)

from antiban import AntiBanManager
from config import API_ID, API_HASH, SESSIONS_DIR
from device_spoof import get_device_for_session
from proxy_manager import proxy_to_pyrogram
from spintax import spin
from core.logger import setup_live_logger
from webapp.main import running_clients

# Fallback logger if live logger isn't passed
logger = logging.getLogger("tg_combiner.sender")

def get_session_files() -> list[Path]:
    return sorted([
        p for p in SESSIONS_DIR.glob("*.session")
        if p.stem != "tg_combiner_bot" and not p.stem.endswith("_pending")
    ])

def _make_client(session_path: Path, proxy: Optional[dict] = None) -> Client:
    device = get_device_for_session(session_path.stem)
    session_name = str(session_path.with_suffix(""))
    kwargs: dict = {
        "name": session_name,
        "api_id": API_ID,
        "api_hash": API_HASH,
        "device_model": device["device_model"],
        "system_version": device["system_version"],
        "app_version": device["app_version"],
    }
    if proxy:
        kwargs["proxy"] = proxy_to_pyrogram(proxy)
    else:
        from config import PYROGRAM_PROXY
        kwargs["proxy"] = PYROGRAM_PROXY
    return Client(**kwargs)

async def _send_with_mode(
    client: Client,
    target: str|int,
    mode: str,
    text: str,
    media_path: Optional[str] = None,
    repost_link: Optional[str] = None,
    effect_id: Optional[int] = None,
    silent: bool = False
) -> tuple[bool, str, Optional[int]]:
    """
    Executes the actual Pyrogram sending logic across 12 modes.
    Returns (success, info_string, message_id).
    """
    msg_id = None
    try:
        if mode == "text":
            sent = await client.send_message(target, text, disable_notification=silent)
            msg_id = sent.id
        elif mode == "media" and media_path:
            sent = await client.send_photo(target, media_path, caption=text, disable_notification=silent)
            msg_id = sent.id
        elif mode == "repost" and repost_link:
            # Assuming repost_link is like https://t.me/channel/123
            parts = repost_link.rstrip("/").split("/")
            chat, msg_id_rep = parts[-2], int(parts[-1])
            sent = await client.forward_messages(target, chat, msg_id_rep, disable_notification=silent)
            msg_id = sent.id
        elif mode == "hidden_repost" and repost_link:
            parts = repost_link.rstrip("/").split("/")
            chat, msg_id_rep = parts[-2], int(parts[-1])
            # copy_message hides the original sender
            sent = await client.copy_message(target, chat, msg_id_rep, caption=text, disable_notification=silent)
            msg_id = sent.id
        elif mode == "voice" and media_path:
            sent = await client.send_voice(target, media_path, caption=text, disable_notification=silent)
            msg_id = sent.id
        elif mode == "video_note" and media_path: # Кружочки
            sent = await client.send_video_note(target, media_path, disable_notification=silent)
            msg_id = sent.id
        elif mode == "secret_chat":
            # Secret chats not fully supported in simple Pyrogram via ID, requires creating secret chat first.
            # Simplified fallback to normal text for now
            sent = await client.send_message(target, f"[Secret Request] {text}", disable_notification=silent)
            msg_id = sent.id
        elif mode == "story_reply":
            # Pyrogram 2.0+ supports stories. A bit complex to target randomly.
            pass
        elif mode == "postbot":
            # Usually means inline buttons generated via a bot like @PostBot
            sent = await client.send_message(target, text, disable_notification=silent)
            msg_id = sent.id
        elif mode == "dialogs":
            # We don't send here, the outer loop will provide `target` as a dialog ID
            sent = await client.send_message(target, text, disable_notification=silent)
            msg_id = sent.id
        elif mode == "contacts":
            sent = await client.send_message(target, text, disable_notification=silent)
            msg_id = sent.id
        else:
            # Fallback
            sent = await client.send_message(target, text, disable_notification=silent)
            msg_id = sent.id

        return True, f"✅ Отправлено ({mode})", msg_id

    except PeerFlood:
        return False, "🚨 SPAMBLOCK (PeerFlood)", None
    except UserBannedInChannel:
        return False, "🚨 SPAMBLOCK (Banned)", None
    except FloodWait as e:
        raise
    except PeerIdInvalid:
        return False, "⚠️ Невалидный ID/Username", None
    except UserIsBlocked:
        return False, "🚫 Заблокировал", None
    except InputUserDeactivated:
        return False, "💀 Деактивирован", None
    except RPCError as e:
        return False, f"❌ Ошибка RPC: {e}", None
    except Exception as exc:
        return False, f"❌ Ошибка: {exc}", None

async def run_advanced_mailing(
    bot: Client,
    admin_id: int,
    targets: list[str|int],
    config: dict, # mode, text, media, effect_id, silent, auto_delete, pin
    antiban: AntiBanManager,
    proxy: Optional[dict] = None,
    selected_sessions: Optional[list[str]] = None,
) -> dict:
    
    live_log = setup_live_logger(bot, admin_id)
    all_sessions = get_session_files()
    if not all_sessions:
        live_log.error("❌ Нет .session файлов")
        return {"sent": 0, "failed": 0}
        
    sessions = [s for s in all_sessions if selected_sessions is None or s.stem in selected_sessions]
    if not sessions:
        live_log.error("❌ Ни один из выбранных аккаунтов не найден")
        return {"sent": 0, "failed": 0}

    antiban.reset()
    total_sent = 0
    total_failed = 0
    target_index = 0
    spam_blocks_streak = 0
    MAX_SPAMBLOCKS_STREAK = 5 # Emergency Stop Threshold

    live_log.info(f"🚀 **Рассылка v2 запущена** | Режим: {config.get('mode', 'text')}")
    live_log.info(f"📋 Сессий: {len(sessions)} | 👥 Получателей: {len(targets)}")

    for session_path in sessions:
        if spam_blocks_streak >= MAX_SPAMBLOCKS_STREAK:
            live_log.error("🛑 **АВАРИЙНАЯ ОСТАНОВКА**! Слишком много спамблоков подряд. Текст/ссылка в спам-базе.")
            break

        session_name = session_path.stem
        if antiban.is_global_exhausted() or target_index >= len(targets):
            break

        # Не берём в работу аккаунт в карантине (недавно словил спамблок).
        if antiban.is_quarantined(session_name):
            live_log.warning(f"🚑 `@{session_name}` в карантине — пропускаем")
            continue

        try:
            is_borrowed = False
            if session_name in running_clients:
                client = running_clients[session_name]
                is_borrowed = True
            else:
                client = _make_client(session_path, proxy)
                await client.start()
        except Exception as exc:
            live_log.error(f"❌ '{session_name}' ошибка запуска: {exc}")
            continue

        try:
            while target_index < len(targets):
                if spam_blocks_streak >= MAX_SPAMBLOCKS_STREAK:
                    break
                if antiban.is_global_exhausted() or antiban.is_account_exhausted(session_name) or not antiban.can_send(session_name):
                    break

                target = targets[target_index]
                text = spin(config.get("text", ""))

                # Calculate randomized wait time to show in Live Logger
                delay = random.uniform(antiban.min_delay, antiban.max_delay)
                live_log.info(f"👤 `@{session_name}` | Ждёт {delay:.1f}с | Цель: {target}")
                await asyncio.sleep(delay)

                try:
                    ok, info, msg_id = await _send_with_mode(
                        client=client,
                        target=target,
                        mode=config.get("mode", "text"),
                        text=text,
                        media_path=config.get("media_path"),
                        repost_link=config.get("repost_link"),
                        effect_id=config.get("effect_id"),
                        silent=config.get("silent", False)
                    )
                except FloodWait as e:
                    # Cap wait at 120s; if Telegram asks for more — skip session
                    wait_time = min(e.value, 120)
                    live_log.warning(
                        f"⏳ `@{session_name}` | FloodWait {e.value}s → "
                        f"ждём {wait_time}s, переключаем сессию"
                    )
                    await antiban.handle_flood_wait(
                        session_name, wait_time, bot, admin_id
                    )
                    break  # Rotate to next session, target stays for retry

                if ok:
                    antiban.record_sent(session_name)
                    total_sent += 1
                    target_index += 1
                    spam_blocks_streak = 0  # reset streak on success
                    live_log.info(f"🟢 `@{session_name}` | #{total_sent} отправлено!")

                    # Post-send actions
                    if config.get("pin") and msg_id:
                        try:
                            await client.pin_chat_message(target, msg_id, both_sides=True)
                        except Exception:
                            pass
                    if config.get("auto_delete"):
                        try:
                            await client.delete_history(target)
                        except Exception:
                            pass
                else:
                    total_failed += 1
                    live_log.warning(f"🔴 `@{session_name}` | Ошибка: {info}")
                    if "SPAMBLOCK" in info:
                        spam_blocks_streak += 1
                        # Карантиним аккаунт, чтобы не гнать его снова под перманентный бан.
                        antiban.quarantine(session_name)
                        target_index += 1  # Skip this target for this session
                        break  # Rotate to next session on spam block
                    else:
                        target_index += 1  # Always advance to next target

        except Exception as exc:
            live_log.error(f"❌ `@{session_name}` | Непредвиденная ошибка: {exc}")
        finally:
            if not is_borrowed:
                try:
                    await client.stop()
                except Exception:
                    pass

        count = antiban.get_session_count(session_name)
        live_log.info(f"📱 Сессия `{session_name}` завершила работу ({count} отправлено).")

    summary = {"sent": total_sent, "failed": total_failed, "sessions": len(sessions)}
    live_log.info(f"🏁 **Рассылка завершена** | ✅ {total_sent} | ❌ {total_failed}")
    return summary
