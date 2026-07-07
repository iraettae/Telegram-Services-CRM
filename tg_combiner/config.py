"""
tg_combiner — Configuration module.
Loads .env and provides all paths / defaults.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ── Paths ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
PROXIES_FILE = BASE_DIR / "proxies.json"
ADMINS_FILE = BASE_DIR / "admins.json"
ENV_FILE = BASE_DIR / ".env"

SESSIONS_DIR.mkdir(exist_ok=True)

# ── .env ───────────────────────────────────────────────────────────────
load_dotenv(ENV_FILE)

API_ID: int = int(os.getenv("API_ID", "0"))
API_HASH: str = os.getenv("API_HASH", "")
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))

# ── SOCKS5 Proxy (WARP) ───────────────────────────────────────────────
# Docker: SOCKS5_HOST=warp, SOCKS5_PORT=1080
# Systemd: SOCKS5_HOST=127.0.0.1, SOCKS5_PORT=40000
SOCKS5_HOST: str = os.getenv("SOCKS5_HOST", "127.0.0.1")
SOCKS5_PORT: int = int(os.getenv("SOCKS5_PORT", "40000"))
PYROGRAM_PROXY: dict = {"scheme": "socks5", "hostname": SOCKS5_HOST, "port": SOCKS5_PORT}

# ── Default limits ─────────────────────────────────────────────────────
DEFAULT_GLOBAL_LIMIT: int = 500
DEFAULT_ACCOUNT_LIMIT: int = 20   # лимит на аккаунт за один прогон рассылки
DEFAULT_MIN_DELAY: float = 3.0   # seconds
DEFAULT_MAX_DELAY: float = 10.0  # seconds

# Суточный лимит на аккаунт (переживает рестарт и несколько прогонов за день).
# Раньше дневного лимита не было вовсе — можно было делать много прогонов по 20.
DEFAULT_DAILY_LIMIT: int = int(os.getenv("DAILY_ACCOUNT_LIMIT", "40"))
# Базовый карантин аккаунта после спамблока (сек); растёт при повторных инцидентах.
QUARANTINE_BASE_SECONDS: int = int(os.getenv("QUARANTINE_BASE_SECONDS", "86400"))
# Файл здоровья сессий (дневные счётчики, карантин, инциденты)
SESSION_HEALTH_FILE = SESSIONS_DIR / "session_health.json"


def save_admin_id(uid: int) -> None:
    """Persist ADMIN_ID to .env file."""
    global ADMIN_ID
    ADMIN_ID = uid

    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        new_lines = []
        found = False
        for line in lines:
            if line.startswith("ADMIN_ID="):
                new_lines.append(f"ADMIN_ID={uid}")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"ADMIN_ID={uid}")
        ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    else:
        ENV_FILE.write_text(f"ADMIN_ID={uid}\n", encoding="utf-8")

# ── Multi-Admin ────────────────────────────────────────────────────────

import json

def get_allowed_admins() -> list[int]:
    """Return a list of user IDs allowed to use the bot/webapp."""
    if not ADMINS_FILE.exists():
        return [ADMIN_ID] if ADMIN_ID else []
    try:
        data = json.loads(ADMINS_FILE.read_text(encoding="utf-8"))
        admins = data.get("admins", [])
        if ADMIN_ID and ADMIN_ID not in admins:
            admins.append(ADMIN_ID)
        return admins
    except Exception:
        return [ADMIN_ID] if ADMIN_ID else []

def add_admin(uid: int) -> bool:
    admins = get_allowed_admins()
    if uid in admins:
        return False
    admins.append(uid)
    # Never override primary ADMIN_ID in the JSON payload, just add it so the list is unified
    # but we store all of them.
    ADMINS_FILE.write_text(json.dumps({"admins": admins}), encoding="utf-8")
    return True

def remove_admin(uid: int) -> bool:
    if uid == ADMIN_ID:
        return False  # Cannot remove the primary owner
    admins = get_allowed_admins()
    if uid not in admins:
        return False
    admins.remove(uid)
    ADMINS_FILE.write_text(json.dumps({"admins": admins}), encoding="utf-8")
    return True
