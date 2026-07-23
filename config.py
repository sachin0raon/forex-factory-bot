"""
Configuration management – loads settings from environment variables.
"""

import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME: str = os.getenv("MONGO_DB_NAME", "forexfactory")

# Comma-separated chat IDs that are authorized to interact with the bot
AUTHORIZED_CHAT_IDS: list[int] = [
    int(cid.strip())
    for cid in os.getenv("AUTHORIZED_CHAT_IDS", "").split(",")
    if cid.strip()
]
if not AUTHORIZED_CHAT_IDS:
    logger.warning(
        "AUTHORIZED_CHAT_IDS is not set – all bot commands will be rejected. "
        "Set this env var to a comma-separated list of allowed Telegram chat IDs."
    )

# Default cron expression (minute hour day month day_of_week) – UTC
DEFAULT_CRON: str = os.getenv("DEFAULT_CRON", "0 5 * * 1-5")

# Which LLM backend powers news sentiment scoring, /summary, and the
# event-article search/match. Only the active provider's API key is required.
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "anthropic").lower()
if LLM_PROVIDER not in ("anthropic", "gemini"):
    raise ValueError(
        f"Invalid LLM_PROVIDER '{LLM_PROVIDER}': must be 'anthropic' or 'gemini'"
    )

if LLM_PROVIDER == "gemini":
    GEMINI_API_KEY: str = os.environ["GEMINI_API_KEY"]
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    _DEFAULT_SENTIMENT_MODEL = "gemini-2.5-flash-lite"
    _DEFAULT_SUMMARY_MODEL = "gemini-2.5-flash"
else:
    ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    _DEFAULT_SENTIMENT_MODEL = "claude-haiku-4-5-20251001"
    _DEFAULT_SUMMARY_MODEL = "claude-sonnet-5"

# How often (minutes) to poll news RSS feeds
NEWS_POLL_MINUTES: int = int(os.getenv("NEWS_POLL_MINUTES", "15"))

# Set to "false" to disable the recurring news poll job entirely (e.g. if
# you only want ForexFactory calendar events). /fetchnews still works
# on-demand regardless of this flag, since that's an explicit user action.
NEWS_POLLING_ENABLED: bool = os.getenv("NEWS_POLLING_ENABLED", "true").strip().lower() not in (
    "false", "0", "no",
)

# Cheap/fast model for per-item sentiment scoring (runs every poll).
# `or` (not getenv's default arg) so an accidentally-blank env var falls back
# too, not just a fully-unset one.
SENTIMENT_MODEL: str = os.getenv("SENTIMENT_MODEL") or _DEFAULT_SENTIMENT_MODEL

# Higher-quality model for the on-demand /summary outlook (low frequency)
SUMMARY_MODEL: str = os.getenv("SUMMARY_MODEL") or _DEFAULT_SUMMARY_MODEL

# Max output tokens per LLM call. Sentiment batches can be large (one JSON
# entry per news item), so SENTIMENT_MAX_TOKENS needs room for 50+ items.
SENTIMENT_MAX_TOKENS: int = int(os.getenv("SENTIMENT_MAX_TOKENS", "8192"))
SUMMARY_MAX_TOKENS: int = int(os.getenv("SUMMARY_MAX_TOKENS", "1024"))
MATCH_MAX_TOKENS: int = int(os.getenv("MATCH_MAX_TOKENS", "256"))
EVENT_SUMMARY_MAX_TOKENS: int = int(os.getenv("EVENT_SUMMARY_MAX_TOKENS", "384"))

# Speech/testimony calendar events (no forecast value) get an FXStreet article
# search instead of an actual-value check. Delay before the first attempt,
# max attempts, and the interval between retries are all configurable.
ARTICLE_SEARCH_DELAY_MINUTES: int = int(os.getenv("ARTICLE_SEARCH_DELAY_MINUTES", "10"))
ARTICLE_SEARCH_MAX_ATTEMPTS: int = int(os.getenv("ARTICLE_SEARCH_MAX_ATTEMPTS", "3"))
ARTICLE_SEARCH_RETRY_MINUTES: int = int(os.getenv("ARTICLE_SEARCH_RETRY_MINUTES", "30"))
