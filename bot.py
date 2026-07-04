"""
Telegram bot command handlers.

Commands
--------
/start  – Welcome message & subscribe for notifications.
/cron   – View or update the cron schedule.
/fetch  – Manually trigger data fetch + notify.
/events – Show current week events from the database.
"""

import logging
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

import db
import scheduler
import scraper
from config import AUTHORIZED_CHAT_IDS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Authorisation decorator
# ---------------------------------------------------------------------------

def authorized(func):
    """Only allow commands from AUTHORIZED_CHAT_IDS."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if chat_id not in AUTHORIZED_CHAT_IDS:
            logger.warning("Unauthorized access attempt from chat %s", chat_id)
            await update.message.reply_text("⛔ You are not authorized to use this bot.")
            return
        return await func(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@authorized
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message and register the chat as a subscriber."""
    chat_id = update.effective_chat.id
    is_new = await db.add_subscriber(chat_id)

    welcome = (
        "👋 *Welcome to ForexFactory Bot\\!*\n\n"
        "I keep you updated with high\\-impact economic events "
        "from the ForexFactory calendar\\.\n\n"
        "🔔 You are now *subscribed* for notifications\\.\n\n"
        "*Available Commands:*\n"
        "• /cron \\– View or set the fetch schedule\n"
        "• /fetch \\– Manually fetch latest events\n"
        "• /events \\– Show this week's events"
    )
    if not is_new:
        welcome += "\n\n_\\(You were already subscribed\\)_"

    await update.message.reply_text(welcome, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# /cron
# ---------------------------------------------------------------------------

@authorized
async def cron_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Without arguments: display the current cron expression.
    With arguments:    update the cron expression.
    """
    args = context.args
    if not args:
        current = await scheduler.get_current_cron()
        await update.message.reply_text(
            f"⏰ *Current cron schedule \\(UTC\\):*\n`{scraper._escape_md(current)}`",
            parse_mode="MarkdownV2",
        )
        return

    new_cron = " ".join(args)
    try:
        await scheduler.update_cron(new_cron)
        await update.message.reply_text(
            f"✅ Cron schedule updated to:\n`{scraper._escape_md(new_cron)}`",
            parse_mode="MarkdownV2",
        )
    except ValueError as exc:
        await update.message.reply_text(f"❌ Invalid cron expression: {exc}")


# ---------------------------------------------------------------------------
# /fetch
# ---------------------------------------------------------------------------

@authorized
async def fetch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the data fetch process."""
    await update.message.reply_text("⏳ Fetching data from ForexFactory…")

    try:
        events = await scheduler.fetch_and_notify()
    except Exception as exc:
        logger.exception("Manual fetch failed")
        await update.message.reply_text(f"❌ Fetch failed: {exc}")
        return

    if not events:
        await update.message.reply_text("ℹ️ No events returned.")
        return

    summary = scraper.format_events_summary(
        events, title="📰 *Fetched Events*"
    )

    # Split if needed
    chunks = scheduler._split_message(summary, 4000)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# /events
# ---------------------------------------------------------------------------

@authorized
async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current week's events from the database."""
    events = await db.get_current_week_events()

    if not events:
        await update.message.reply_text("ℹ️ No events found for this week.")
        return

    summary = scraper.format_events_summary(
        events, title="📅 *This Week's Events*"
    )

    chunks = scheduler._split_message(summary, 4000)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="MarkdownV2")
