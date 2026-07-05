"""
tg_combiner — Direct Media Sender.
Sends messages (text / photo / video / voice / audio / document) to a list of
usernames or user-IDs through an already-running Pyrogram session.
"""

import asyncio
import logging
import mimetypes
import os
import random
from pathlib import Path
from typing import Callable, Awaitable, Optional

from pyrogram.errors import (
    FloodWait,
    PeerIdInvalid,
    UserIsBlocked,
    InputUserDeactivated,
    PeerFlood,
    UserBannedInChannel,
    RPCError,
)

from webapp.main import running_clients

logger = logging.getLogger("tg_combiner.direct_sender")

# Anti-ban defaults
_MIN_DELAY = 3.0
_MAX_DELAY = 8.0
_FLOOD_WAIT_CAP = 120  # seconds


def _detect_send_method(file_path: Optional[str]) -> str:
    """Return the Pyrogram method name based on MIME type / extension."""
    if not file_path:
        return "text"

    ext = Path(file_path).suffix.lower()
    mime, _ = mimetypes.guess_type(file_path)
    mime = mime or ""

    if ext in (".ogg",):
        return "voice"
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        return "photo"
    if mime.startswith("image/"):
        return "photo"
    if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        return "video"
    if mime.startswith("video/"):
        return "video"
    if ext in (".mp3", ".wav", ".m4a", ".flac", ".aac"):
        return "audio"
    if mime.startswith("audio/"):
        return "audio"
    return "document"


async def _send_one(
    client,
    target: str,
    text: str,
    media_path: Optional[str],
    method: str,
) -> tuple[bool, str]:
    """Send a single message to *target*. Returns (ok, info)."""
    try:
        if method == "text":
            await client.send_message(target, text)
        elif method == "photo":
            await client.send_photo(target, media_path, caption=text or None)
        elif method == "video":
            await client.send_video(target, media_path, caption=text or None)
        elif method == "voice":
            await client.send_voice(target, media_path, caption=text or None)
        elif method == "audio":
            await client.send_audio(target, media_path, caption=text or None)
        else:  # document
            await client.send_document(target, media_path, caption=text or None)
        return True, "✅ Отправлено"

    except PeerFlood:
        return False, "🚨 SPAMBLOCK (PeerFlood)"
    except UserBannedInChannel:
        return False, "🚨 SPAMBLOCK (Banned)"
    except PeerIdInvalid:
        return False, "⚠️ Невалидный ID/Username"
    except UserIsBlocked:
        return False, "🚫 Пользователь заблокировал"
    except InputUserDeactivated:
        return False, "💀 Аккаунт деактивирован"
    except RPCError as e:
        return False, f"❌ RPC: {e}"
    except Exception as exc:
        return False, f"❌ {exc}"


# Type alias for the progress callback
ProgressFn = Callable[..., Awaitable[None]]


async def run_direct_send(
    task_id: str,
    session_name: str,
    recipients: list[str],
    text: str,
    media_path: Optional[str],
    broadcast_fn: ProgressFn,
) -> dict:
    """
    Core direct-send loop.

    Parameters
    ----------
    task_id : unique ID for this mailing job (used for WS progress events)
    session_name : name of the Pyrogram session to use
    recipients : list of @usernames or numeric IDs
    text : message body (can be empty if media is attached)
    media_path : absolute path to the uploaded file (or None)
    broadcast_fn : coroutine called after each send to push WS progress
    """
    client = running_clients.get(session_name)
    if not client or not client.is_connected:
        await broadcast_fn({
            "type": "dm_complete",
            "task_id": task_id,
            "sent": 0,
            "failed": 0,
            "total": len(recipients),
            "error": "Сессия не подключена",
        })
        return {"sent": 0, "failed": 0, "total": len(recipients)}

    method = _detect_send_method(media_path)
    total = len(recipients)
    sent = 0
    failed = 0

    for idx, target in enumerate(recipients):
        target = target.strip()
        if not target:
            continue

        # Anti-ban delay (skip before the very first message)
        if idx > 0:
            delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
            await broadcast_fn({
                "type": "dm_progress",
                "task_id": task_id,
                "target": target,
                "status": "waiting",
                "detail": f"⏳ Ожидание {delay:.1f}с…",
                "index": idx,
                "total": total,
                "sent": sent,
                "failed": failed,
            })
            await asyncio.sleep(delay)

        try:
            ok, info = await _send_one(client, target, text, media_path, method)
        except FloodWait as e:
            wait = min(e.value, _FLOOD_WAIT_CAP)
            await broadcast_fn({
                "type": "dm_progress",
                "task_id": task_id,
                "target": target,
                "status": "flood",
                "detail": f"⏳ FloodWait {e.value}s → ждём {wait}s",
                "index": idx,
                "total": total,
                "sent": sent,
                "failed": failed,
            })
            await asyncio.sleep(wait)
            # Retry once after flood wait
            try:
                ok, info = await _send_one(client, target, text, media_path, method)
            except FloodWait:
                ok, info = False, "🚨 Повторный FloodWait — пропуск"
            except Exception as exc:
                ok, info = False, f"❌ {exc}"

        if ok:
            sent += 1
        else:
            failed += 1

        await broadcast_fn({
            "type": "dm_progress",
            "task_id": task_id,
            "target": target,
            "status": "ok" if ok else "fail",
            "detail": info,
            "index": idx + 1,
            "total": total,
            "sent": sent,
            "failed": failed,
        })

    # Cleanup temp media file
    if media_path:
        try:
            os.unlink(media_path)
        except OSError:
            pass

    summary = {"sent": sent, "failed": failed, "total": total}
    await broadcast_fn({
        "type": "dm_complete",
        "task_id": task_id,
        **summary,
    })
    logger.info("Direct send complete: %s", summary)
    return summary
