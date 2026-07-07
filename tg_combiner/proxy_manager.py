"""
tg_combiner — Proxy management.
CRUD operations on proxies.json, IP-info checks via ip-api.com, validation.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from config import PROXIES_FILE, SESSION_PROXY_FILE

logger = logging.getLogger("tg_combiner.proxy")

# ── Helpers ────────────────────────────────────────────────────────────

def _load_proxies() -> list[dict]:
    """Load proxy list from JSON file."""
    if not PROXIES_FILE.exists():
        return []
    try:
        return json.loads(PROXIES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_proxies(proxies: list[dict]) -> None:
    """Persist proxy list to JSON file."""
    PROXIES_FILE.write_text(
        json.dumps(proxies, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── Public API ─────────────────────────────────────────────────────────

def parse_proxy_string(raw: str) -> Optional[dict]:
    """Parse 'ip:port:user:pass' into a dict. Returns None on bad format."""
    parts = raw.strip().split(":")
    if len(parts) != 4:
        return None
    ip, port, user, password = parts
    if not port.isdigit():
        return None
    return {"ip": ip, "port": int(port), "user": user, "pass": password}


def list_proxies() -> list[dict]:
    """Return all saved proxies."""
    return _load_proxies()


def add_proxy(proxy: dict) -> int:
    """Add a proxy to the list. Returns its index."""
    proxies = _load_proxies()
    proxies.append(proxy)
    _save_proxies(proxies)
    return len(proxies) - 1


def remove_proxy(index: int) -> bool:
    """Remove proxy by index. Returns True on success."""
    proxies = _load_proxies()
    if 0 <= index < len(proxies):
        proxies.pop(index)
        _save_proxies(proxies)
        return True
    return False


def proxy_to_url(proxy: dict) -> str:
    """Convert proxy dict to socks5/http URL for httpx."""
    return f"socks5://{proxy['user']}:{proxy['pass']}@{proxy['ip']}:{proxy['port']}"


def proxy_to_pyrogram(proxy: dict) -> dict:
    """Convert proxy dict to Pyrogram proxy kwargs."""
    return {
        "scheme": "socks5",
        "hostname": proxy["ip"],
        "port": proxy["port"],
        "username": proxy["user"],
        "password": proxy["pass"],
    }


# ── Привязка прокси к аккаунту (session → proxy) ───────────────────────
# Свой sticky-IP на каждую учётку: Telegram не должен видеть несколько
# прогретых аккаунтов с одного адреса. Хранится в session_proxy.json.

def _load_session_proxies() -> dict:
    if not SESSION_PROXY_FILE.exists():
        return {}
    try:
        data = json.loads(SESSION_PROXY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_session_proxies(mapping: dict) -> None:
    # Атомарная запись: temp + os.replace. Иначе конкурентный читатель
    # (get_proxy_for_session при старте сессии) может поймать усечённый JSON →
    # None → тихий откат аккаунта на общий/WARP IP (нежелательная смена IP).
    try:
        SESSION_PROXY_FILE.parent.mkdir(exist_ok=True)
        tmp = SESSION_PROXY_FILE.with_name(SESSION_PROXY_FILE.name + ".tmp")
        tmp.write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, SESSION_PROXY_FILE)
    except OSError:
        logger.error("Не удалось сохранить session_proxy.json")


def get_proxy_for_session(session_name: str) -> Optional[dict]:
    """Прокси, привязанный к аккаунту, или None (тогда fallback на WARP)."""
    proxy = _load_session_proxies().get(session_name)
    if isinstance(proxy, dict) and {"ip", "port", "user", "pass"} <= set(proxy):
        return proxy
    return None


def set_proxy_for_session(session_name: str, proxy: dict) -> None:
    mapping = _load_session_proxies()
    mapping[session_name] = proxy
    _save_session_proxies(mapping)


def unset_proxy_for_session(session_name: str) -> bool:
    mapping = _load_session_proxies()
    if session_name in mapping:
        del mapping[session_name]
        _save_session_proxies(mapping)
        return True
    return False


def list_session_proxies() -> dict:
    """Вся карта {session_name: proxy} — для отображения в боте."""
    return _load_session_proxies()


# ── Async network checks ──────────────────────────────────────────────

async def check_current_ip(proxy: Optional[dict] = None) -> dict:
    """
    GET ip-api.com and return a dict with:
    ip, country, isp, hosting (bool), warning (str or None)
    """
    url = "http://ip-api.com/json/?fields=query,country,isp,hosting,proxy"
    transport_kwargs = {}
    if proxy:
        transport_kwargs["proxy"] = proxy_to_url(proxy)

    async with httpx.AsyncClient(**transport_kwargs, timeout=15) as client:
        resp = await client.get(url)
        data = resp.json()

    result = {
        "ip": data.get("query", "N/A"),
        "country": data.get("country", "N/A"),
        "isp": data.get("isp", "N/A"),
        "hosting": bool(data.get("hosting", False)),
        "proxy_flag": bool(data.get("proxy", False)),
    }
    if result["hosting"]:
        result["warning"] = "⚠️ IP принадлежит дата-центру (hosting=true)"
    else:
        result["warning"] = None
    return result


async def validate_proxy(proxy: dict) -> tuple[bool, str]:
    """
    Try a test request through the proxy.
    Returns (ok: bool, message: str).
    """
    try:
        url = "http://ip-api.com/json/?fields=query"
        async with httpx.AsyncClient(
            proxy=proxy_to_url(proxy), timeout=15
        ) as client:
            resp = await client.get(url)
            data = resp.json()
            ip = data.get("query", "?")
            return True, f"✅ Прокси работает. IP: {ip}"
    except httpx.ProxyError as exc:
        return False, f"❌ Ошибка прокси: {exc}"
    except httpx.ConnectTimeout:
        return False, "❌ Таймаут подключения к прокси"
    except Exception as exc:  # noqa: BLE001
        return False, f"❌ Ошибка: {exc}"
