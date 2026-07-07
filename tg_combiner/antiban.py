"""
tg_combiner — Anti-ban system.
Manages sending limits, per-session counters, random delays, and FloodWait handling.
"""

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Optional

from config import (
    DEFAULT_ACCOUNT_LIMIT,
    DEFAULT_DAILY_LIMIT,
    DEFAULT_GLOBAL_LIMIT,
    DEFAULT_MAX_DELAY,
    DEFAULT_MIN_DELAY,
    QUARANTINE_BASE_SECONDS,
    SESSION_HEALTH_FILE,
)

logger = logging.getLogger("tg_combiner.antiban")


class AntiBanManager:
    """Tracks limits and handles humanization for multi-session mailing.

    Персистентный слой (session_health.json) хранит СУТОЧНЫЕ счётчики и карантин —
    они переживают рестарт и несколько прогонов рассылки, в отличие от
    per-run счётчиков, которые обнуляются reset() перед каждым запуском.
    """

    # Maximum seconds to wait on a FloodWait before skipping the session
    MAX_FLOOD_WAIT_SECONDS: int = 120

    def __init__(
        self,
        global_limit: int = DEFAULT_GLOBAL_LIMIT,
        account_limit: int = DEFAULT_ACCOUNT_LIMIT,
        min_delay: float = DEFAULT_MIN_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
    ):
        self.global_limit = global_limit
        self.account_limit = account_limit
        self.daily_limit = daily_limit
        self.min_delay = min_delay
        self.max_delay = max_delay

        # per-run counters (сбрасываются reset() каждый прогон)
        self._session_counters: dict[str, int] = {}
        self._global_counter: int = 0

        # persistent health: {"daily": {date: {session: count}},
        #                     "quarantine": {session: until_ts},
        #                     "incidents": {session: count}}
        self._health: dict = self._load_health()

    # ── Persistence ────────────────────────────────────────────────────

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load_health(self) -> dict:
        try:
            data = json.loads(SESSION_HEALTH_FILE.read_text(encoding="utf-8"))
            data.setdefault("daily", {})
            data.setdefault("quarantine", {})
            data.setdefault("incidents", {})
            return data
        except Exception:
            return {"daily": {}, "quarantine": {}, "incidents": {}}

    def _save_health(self) -> None:
        try:
            SESSION_HEALTH_FILE.parent.mkdir(exist_ok=True)
            SESSION_HEALTH_FILE.write_text(
                json.dumps(self._health, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            logger.warning("Не удалось сохранить session_health.json")

    def get_daily_count(self, session_name: str) -> int:
        return self._health.get("daily", {}).get(self._today(), {}).get(session_name, 0)

    def is_quarantined(self, session_name: str) -> bool:
        until = self._health.get("quarantine", {}).get(session_name, 0)
        return bool(until) and time.time() < until

    def quarantine(self, session_name: str) -> None:
        """Помещает аккаунт в карантин с растущим cooldown после спамблока."""
        incidents = self._health.setdefault("incidents", {})
        incidents[session_name] = incidents.get(session_name, 0) + 1
        cooldown = QUARANTINE_BASE_SECONDS * incidents[session_name]
        self._health.setdefault("quarantine", {})[session_name] = time.time() + cooldown
        self._save_health()
        logger.warning(
            "🚑 Аккаунт %s в карантине на %.1f ч (инцидент #%d)",
            session_name, cooldown / 3600, incidents[session_name],
        )

    # ── Limits ─────────────────────────────────────────────────────────

    def can_send(self, session_name: str) -> bool:
        """Check global, per-run, daily limits and quarantine."""
        if self._global_counter >= self.global_limit:
            return False
        if self._session_counters.get(session_name, 0) >= self.account_limit:
            return False
        if self.get_daily_count(session_name) >= self.daily_limit:
            return False
        if self.is_quarantined(session_name):
            return False
        return True

    def record_sent(self, session_name: str) -> None:
        """Increment per-run + persistent daily counters after a successful send."""
        self._session_counters[session_name] = (
            self._session_counters.get(session_name, 0) + 1
        )
        self._global_counter += 1
        day = self._health.setdefault("daily", {}).setdefault(self._today(), {})
        day[session_name] = day.get(session_name, 0) + 1
        self._save_health()

    def is_account_exhausted(self, session_name: str) -> bool:
        """True when the account has reached its per-session limit."""
        return self._session_counters.get(session_name, 0) >= self.account_limit

    def is_global_exhausted(self) -> bool:
        """True when the global mailing limit has been reached."""
        return self._global_counter >= self.global_limit

    def get_session_count(self, session_name: str) -> int:
        return self._session_counters.get(session_name, 0)

    def get_global_count(self) -> int:
        return self._global_counter

    def reset(self) -> None:
        """Reset all counters (for a new mailing run)."""
        self._session_counters.clear()
        self._global_counter = 0

    # ── Humanization ───────────────────────────────────────────────────

    async def wait_random_delay(self) -> float:
        """Sleep for a random duration in [min_delay, max_delay]. Returns seconds slept."""
        delay = random.uniform(self.min_delay, self.max_delay)
        logger.debug("Sleeping %.2f s", delay)
        await asyncio.sleep(delay)
        return delay

    # ── FloodWait ──────────────────────────────────────────────────────

    async def handle_flood_wait(
        self,
        session_name: str,
        seconds: int,
        bot=None,
        admin_id: Optional[int] = None,
    ) -> None:
        """
        Handle a FloodWait exception:
        1. Cap wait at MAX_FLOOD_WAIT_SECONDS to avoid multi-hour blocks
        2. Notify admin via bot (if provided)
        3. Sleep the capped duration
        """
        capped = min(seconds, self.MAX_FLOOD_WAIT_SECONDS)
        skipped = seconds > self.MAX_FLOOD_WAIT_SECONDS

        if skipped:
            msg = (
                f"🚨 **FloodWait** — аккаунт `{session_name}` "
                f"заблокирован на **{seconds}** сек. "
                f"Лимит {self.MAX_FLOOD_WAIT_SECONDS}s — пропускаем."
            )
        else:
            msg = (
                f"⏳ **FloodWait** — аккаунт `{session_name}` "
                f"заблокирован на **{seconds}** сек. Ждём…"
            )
        logger.warning(msg)

        if bot and admin_id:
            try:
                await bot.send_message(admin_id, msg)
            except Exception:  # noqa: BLE001
                logger.error("Failed to notify admin about FloodWait")

        await asyncio.sleep(capped)
