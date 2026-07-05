import logging
import asyncio
from typing import Optional

class TelegramConsoleHandler(logging.Handler):
    """
    Custom logging handler that sends/edits structured logs directly to the Admin in Telegram.
    This provides a 'Live Console' feel.
    """
    def __init__(self, botclient, admin_id: int):
        super().__init__()
        self.bot = botclient
        self.admin_id = admin_id
        self._edit_queue = asyncio.Queue()
        self._updater_task = asyncio.create_task(self._updater_loop())

    def emit(self, record):
        try:
            msg = self.format(record)
            # Fire and forget into the queue to keep emit sync
            try:
                self._edit_queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass
        except Exception:
            self.handleError(record)

    async def _updater_loop(self):
        """Send each log as a separate message, with throttling to avoid FloodWait."""
        while True:
            msg = await self._edit_queue.get()
            text = f"🖥 {msg}"
            
            try:
                await self.bot.send_message(chat_id=self.admin_id, text=text)
            except Exception as e:
                err_str = str(e)
                if "FloodWait" in err_str:
                    await asyncio.sleep(2)
            
            # Rate limit Telegram UI updates
            await asyncio.sleep(0.5)

    def close(self):
        """Cancel the background updater task to prevent leaks."""
        if self._updater_task and not self._updater_task.done():
            self._updater_task.cancel()
        super().close()

def setup_live_logger(botclient, admin_id: int) -> logging.Logger:
    logger = logging.getLogger("tg_combiner.live")
    # Close and remove existing TelegramConsoleHandler(s) to prevent duplicates/leaks
    for h in logger.handlers[:]:
        if isinstance(h, TelegramConsoleHandler):
            h.close()
            logger.removeHandler(h)

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S МСК")
    
    tg_handler = TelegramConsoleHandler(botclient, admin_id)
    tg_handler.setFormatter(formatter)
    logger.addHandler(tg_handler)
    
    return logger
