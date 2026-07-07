import hashlib
import hmac
import os
import time
import urllib.parse
import json
import config
import logging
logger = logging.getLogger("tg_combiner.webapp.auth")

# TTL валидности initData (сек) — перехваченную строку нельзя переигрывать вечно.
INIT_DATA_TTL = int(os.getenv("INIT_DATA_TTL", "86400"))

def validate_init_data(init_data: str) -> bool:
    """
    Validates the data received from the Telegram Mini App.
    Ensures that only the authenticated Admin can access the WebApp.
    """
    try:
        parsed_data = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        data_dict = dict(parsed_data)
        
        # Check if hash is present
        if 'hash' not in data_dict:
            logger.warning(f"Auth rejected: no 'hash' in initData. raw_len={len(init_data)}, keys={list(data_dict.keys())}")
            return False
            
        received_hash = data_dict.pop('hash')
        
        # We need to sort keys alphabetically
        sorted_keys = sorted(data_dict.keys())
        data_check_string = "\n".join([f"{k}={data_dict[k]}" for k in sorted_keys])
        
        # Create secret key from BOT_TOKEN
        secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode('utf-8'), hashlib.sha256).digest()
        
        # Calculate confirmation hash
        calculated_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        
        # Timing-safe сравнение
        if not hmac.compare_digest(calculated_hash, received_hash):
            logger.info("Auth rejected: hash mismatch")
            return False

        # Проверка свежести initData (защита от бессрочного replay)
        try:
            auth_date = int(data_dict.get('auth_date', '0'))
        except ValueError:
            auth_date = 0
        if not auth_date or (time.time() - auth_date) > INIT_DATA_TTL:
            logger.warning("Auth rejected: initData expired")
            return False

        # Parse User ID
        user_data = json.loads(data_dict.get('user', '{}'))
        user_id = user_data.get('id')
        
        # Fallback if 'user' is missing but 'chat' is present
        if not user_id and 'chat' in data_dict:
            chat_data = json.loads(data_dict.get('chat', '{}'))
            user_id = chat_data.get('id')
            
        logger.info(f"Auth check: user_id={user_id}")

        return user_id in config.get_allowed_admins()
        
    except Exception as e:
        logger.error(f"Auth error: {e}")
        return False


# ── Token-based auth for Menu Button launches ──────────────────────────
# Menu Button does NOT populate tg.initData, so we generate a signed
# token containing the admin user_id and append it to the webapp URL.

def generate_auth_token(user_id: int) -> str:
    """Create an HMAC-SHA256 token: uid:signature."""
    sig = hmac.new(
        config.BOT_TOKEN.encode('utf-8'),
        str(user_id).encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return f"{user_id}:{sig}"


def validate_auth_token(token: str) -> bool:
    """Verify a token produced by generate_auth_token and check admin list."""
    try:
        uid_str, sig = token.split(":", 1)
        uid = int(uid_str)
        expected = hmac.new(
            config.BOT_TOKEN.encode('utf-8'),
            uid_str.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            logger.warning(f"Token auth: signature mismatch for uid={uid}")
            return False
        if uid not in config.get_allowed_admins():
            logger.warning(f"Token auth: uid={uid} not in admins")
            return False
        logger.info(f"Token auth OK: uid={uid}")
        return True
    except Exception as e:
        logger.error(f"Token auth error: {e}")
        return False
