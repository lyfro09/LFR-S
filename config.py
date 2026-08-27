"""Configuration values for LFR Spam."""

from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = DATA_DIR / "lfr.db"
MESSAGES_FILE = BASE_DIR / "messages.txt"
MESSAGES_DIR = BASE_DIR / "messages"

APP_VERSION = "1.1.0"

FREE_MAX_MESSAGES = 5
FREE_MIN_DELAY = 0.5

VIP_MAX_MESSAGES = 20
VIP_MIN_DELAY = 0.2

MAX_DELAY = 5.0
DAILY_REWARD = 100
DAILY_COOLDOWN = 24 * 60 * 60
SPAM_REWARD = 5
VIP_TRIAL_DURATION = 24 * 60 * 60
# Sentinel timestamp used for permanent VIP (9999-12-31 23:59:59 UTC).
PERMANENT_VIP_UNTIL = 253_402_300_799

SHOP_ITEMS = {
    "vip_1": {"label": "VIP на 1 день", "days": 1, "price": 1_000},
    "vip_7": {"label": "VIP на 7 дней", "days": 7, "price": 5_000},
    "vip_30": {"label": "VIP на 30 дней", "days": 30, "price": 15_000},
}


def _ids_from_env(name: str) -> set[int]:
    result: set[int] = set()
    for value in os.getenv(name, "").replace(";", ",").split(","):
        value = value.strip()
        if value.isdigit():
            result.add(int(value))
    return result


# IDs can be supplied as comma-separated environment variables. Literal IDs may
# also be added to these sets later without changing the rest of the project.
ADMIN_USER_IDS: set[int] = _ids_from_env("ADMIN_USER_IDS") | set()
# Example for literal IDs: ADMIN_USER_IDS.add(123456789012345678)

MOD_USER_IDS: set[int] = _ids_from_env("MOD_USER_IDS") | set()
# Example for literal IDs: MOD_USER_IDS.add(123456789012345678)
