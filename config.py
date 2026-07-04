"""
Configuration management – loads settings from environment variables.
"""

import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME: str = os.getenv("MONGO_DB_NAME", "forexfactory")

# Comma-separated chat IDs that are authorized to interact with the bot
AUTHORIZED_CHAT_IDS: list[int] = [
    int(cid.strip())
    for cid in os.getenv("AUTHORIZED_CHAT_IDS", "").split(",")
    if cid.strip()
]

# Default cron expression (minute hour day month day_of_week) – UTC
DEFAULT_CRON: str = os.getenv("DEFAULT_CRON", "0 5 * * 1-5")
