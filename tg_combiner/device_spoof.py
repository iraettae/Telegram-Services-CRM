"""
tg_combiner — Device fingerprint spoofing.
Generates realistic (device_model, system_version, app_version) tuples
so Pyrogram clients don't reveal default library signatures.
"""

import random

# ── Pool of realistic device fingerprints ──────────────────────────────
_DEVICE_POOL: list[dict[str, str]] = [
    # Samsung
    {"device_model": "Samsung Galaxy S24 Ultra",  "system_version": "Android 14",  "app_version": "Telegram Android 10.14.5"},
    {"device_model": "Samsung Galaxy S23",         "system_version": "Android 14",  "app_version": "Telegram Android 10.12.0"},
    {"device_model": "Samsung Galaxy S23 Ultra",   "system_version": "Android 13",  "app_version": "Telegram Android 10.8.3"},
    {"device_model": "Samsung Galaxy S22",         "system_version": "Android 13",  "app_version": "Telegram Android 10.6.1"},
    {"device_model": "Samsung Galaxy S21 FE",      "system_version": "Android 13",  "app_version": "Telegram Android 10.3.2"},
    {"device_model": "Samsung Galaxy A54",         "system_version": "Android 14",  "app_version": "Telegram Android 10.14.1"},
    {"device_model": "Samsung Galaxy A34",         "system_version": "Android 13",  "app_version": "Telegram Android 10.9.0"},
    {"device_model": "Samsung Galaxy Z Fold5",     "system_version": "Android 14",  "app_version": "Telegram Android 10.13.2"},
    {"device_model": "Samsung Galaxy Z Flip5",     "system_version": "Android 14",  "app_version": "Telegram Android 10.11.0"},

    # Xiaomi
    {"device_model": "Xiaomi 14 Pro",              "system_version": "Android 14",  "app_version": "Telegram Android 10.14.3"},
    {"device_model": "Xiaomi 13",                  "system_version": "Android 14",  "app_version": "Telegram Android 10.10.0"},
    {"device_model": "Xiaomi Redmi Note 13 Pro",   "system_version": "Android 14",  "app_version": "Telegram Android 10.13.0"},
    {"device_model": "Xiaomi Redmi Note 12",       "system_version": "Android 13",  "app_version": "Telegram Android 10.7.2"},
    {"device_model": "POCO F5",                    "system_version": "Android 13",  "app_version": "Telegram Android 10.8.1"},
    {"device_model": "POCO X5 Pro",                "system_version": "Android 13",  "app_version": "Telegram Android 10.5.4"},

    # Google Pixel
    {"device_model": "Google Pixel 8 Pro",         "system_version": "Android 14",  "app_version": "Telegram Android 10.14.5"},
    {"device_model": "Google Pixel 8",             "system_version": "Android 14",  "app_version": "Telegram Android 10.14.2"},
    {"device_model": "Google Pixel 7a",            "system_version": "Android 14",  "app_version": "Telegram Android 10.12.1"},
    {"device_model": "Google Pixel 7",             "system_version": "Android 13",  "app_version": "Telegram Android 10.9.3"},

    # OnePlus
    {"device_model": "OnePlus 12",                 "system_version": "Android 14",  "app_version": "Telegram Android 10.14.0"},
    {"device_model": "OnePlus 11",                 "system_version": "Android 14",  "app_version": "Telegram Android 10.11.4"},
    {"device_model": "OnePlus Nord 3",             "system_version": "Android 13",  "app_version": "Telegram Android 10.7.0"},
    {"device_model": "OnePlus 10 Pro",             "system_version": "Android 13",  "app_version": "Telegram Android 10.5.2"},

    # Realme / Oppo / Vivo
    {"device_model": "Realme GT 5 Pro",            "system_version": "Android 14",  "app_version": "Telegram Android 10.13.1"},
    {"device_model": "OPPO Find X7",               "system_version": "Android 14",  "app_version": "Telegram Android 10.14.4"},
    {"device_model": "vivo X100 Pro",              "system_version": "Android 14",  "app_version": "Telegram Android 10.12.3"},

    # Honor / Huawei
    {"device_model": "Honor Magic6 Pro",           "system_version": "Android 14",  "app_version": "Telegram Android 10.14.2"},
    {"device_model": "Honor 90",                   "system_version": "Android 13",  "app_version": "Telegram Android 10.10.1"},
    {"device_model": "Huawei P60 Pro",             "system_version": "HarmonyOS 4", "app_version": "Telegram Android 10.8.0"},

    # Motorola / Nothing
    {"device_model": "Motorola Edge 40 Pro",       "system_version": "Android 13",  "app_version": "Telegram Android 10.9.2"},
    {"device_model": "Nothing Phone (2)",          "system_version": "Android 14",  "app_version": "Telegram Android 10.11.3"},
]


def get_random_device() -> dict[str, str]:
    """Return a random device fingerprint dict with keys:
    device_model, system_version, app_version.
    """
    return random.choice(_DEVICE_POOL).copy()
