"""
tg_combiner — Anti-ban system.
Manages sending limits, per-session counters, random delays, and FloodWait handling.
"""

import asyncio
import logging
import random
from typing import Optional

from config import (
    DEFAULT_ACCOUNT_LIMIT,
    DEFAULT_GLOBAL_LIMIT,
    DEFAULT_MAX_DELAY,
    DEFAULT_MIN_DELAY,
)

logger = logging.getLogger("tg_combiner.antiban")


class AntiBanManager:
    """Tracks limits and handles humanization for multi-session mailing."""

    # Maximum seconds to wait on a FloodWait before skipping the session
    MAX_FLOOD_WAIT_SECONDS: int = 120

    def __init__(
        self,
        global_limit: int = DEFAULT_GLOBAL_LIMIT,
        account_limit: int = DEFAULT_ACCOUNT_LIMIT,
        min_delay: float = DEFAULT_MIN_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
    ):
        self.global_limit = global_limit
        self.account_limit = account_limit
        self.min_delay = min_delay
        self.max_delay = max_delay

        # counters
        self._session_counters: dict[str, int] = {}
        self._global_counter: int = 0

    # ── Limits ─────────────────────────────────────────────────────────

    def can_send(self, session_name: str) -> bool:
        """Check both global and per-account limits."""
        if self._global_counter >= self.global_limit:
            return False
        session_count = self._session_counters.get(session_name, 0)
        if session_count >= self.account_limit:
            return False
        return True

    def record_sent(self, session_name: str) -> None:
        """Increment counters after a successful send."""
        self._session_counters[session_name] = (
            self._session_counters.get(session_name, 0) + 1
        )
        self._global_counter += 1

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
