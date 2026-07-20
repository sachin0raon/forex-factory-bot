"""
Telegram bot command handlers.

Commands
--------
/start     – Welcome message & subscribe for notifications.
/cron      – View or update the cron schedule.
/fetch     – Manually trigger calendar data fetch + notify.
/events    – Show current week events from the database.
/news      – Show recent scored gold/USD news.
/summary   – Generate an on-demand overall gold outlook.
/fetchnews – Manually trigger a news poll + notify.
"""

import logging
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

import db
import llm
import news
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
        if not update.effective_chat or not update.message:
            return
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
async def start_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message and register the chat as a subscriber."""
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    is_new = await db.add_subscriber(chat_id)

    welcome = (
        "👋 *Welcome to ForexFactory Bot\\!*\n\n"
        "I keep you updated with high\\-impact economic events from the "
        "ForexFactory calendar, plus gold/USD news scored for sentiment\\.\n\n"
        "🔔 You are now *subscribed* for notifications\\.\n\n"
        "*Available Commands:*\n"
        "• /cron \\– View or set the fetch schedule\n"
        "• /fetch \\– Manually fetch latest events\n"
        "• /events \\– Show this week's events\n"
        "• /news \\– Show recent scored gold/USD news\n"
        "• /summary \\– Generate an overall gold outlook\n"
        "• /fetchnews \\– Manually poll news now\n"
        "• /unsub \\– Unsubscribe from notifications"
    )
    if not is_new:
        welcome += "\n\n_\\(You were already subscribed\\)_"

    await update.message.reply_text(welcome, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# /unsub
# ---------------------------------------------------------------------------

@authorized
async def unsub_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unsubscribe the current chat from notifications."""
    if not update.effective_chat or not update.message:
        return
    chat_id = update.effective_chat.id
    removed = await db.remove_subscriber(chat_id)
    if removed:
        await update.message.reply_text(
            "🔕 You have been *unsubscribed* from notifications\\.\n"
            "Use /start to subscribe again\\.",
            parse_mode="MarkdownV2",
        )
    else:
        await update.message.reply_text(
            "ℹ️ You were not subscribed\\.",
            parse_mode="MarkdownV2",
        )


# ---------------------------------------------------------------------------
# /cron
# ---------------------------------------------------------------------------

@authorized
async def cron_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Without arguments: display the current cron expression.
    With arguments:    update the cron expression.
    """
    if not update.message:
        return
    args = context.args
    if not args:
        current = await scheduler.get_current_cron()
        await update.message.reply_text(
            f"⏰ *Current cron schedule \\(UTC\\):*\n`{scraper.escape_md(current)}`",
            parse_mode="MarkdownV2",
        )
        return

    new_cron = " ".join(args)
    try:
        await scheduler.update_cron(new_cron)
        await update.message.reply_text(
            f"✅ Cron schedule updated to:\n`{scraper.escape_md(new_cron)}`",
            parse_mode="MarkdownV2",
        )
    except ValueError as exc:
        await update.message.reply_text(f"❌ Invalid cron expression: {exc}")


# ---------------------------------------------------------------------------
# /fetch
# ---------------------------------------------------------------------------

@authorized
async def fetch_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger the data fetch process."""
    if not update.message:
        return
    await update.message.reply_text("⏳ Fetching data from ForexFactory…")

    try:
        events = await scheduler.fetch_and_store()
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
    chunks = scraper.split_message(summary, 4000)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# /events
# ---------------------------------------------------------------------------

@authorized
async def events_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current week's events from the database."""
    if not update.message:
        return
    events = await db.get_current_week_events()

    if not events:
        await update.message.reply_text("ℹ️ No events found for this week.")
        return

    summary = scraper.format_events_summary(
        events, title="📅 *This Week's Events*"
    )

    chunks = scraper.split_message(summary, 4000)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# /news
# ---------------------------------------------------------------------------

@authorized
async def news_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recently scored gold/USD news from the database."""
    if not update.message:
        return
    items = await db.get_recent_news()

    if not items:
        await update.message.reply_text("ℹ️ No recent news found.")
        return

    summary = news.format_news_summary(items)
    chunks = scraper.split_message(summary, 4000)
    for chunk in chunks:
        await update.message.reply_text(chunk, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# /summary
# ---------------------------------------------------------------------------

@authorized
async def summary_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate an on-demand overall gold outlook from recent news + upcoming events."""
    if not update.message:
        return
    await update.message.reply_text("🤔 Analyzing recent news and upcoming events…")

    recent_news = await db.get_recent_news()
    upcoming_events = await db.get_upcoming_events()

    try:
        outlook = await llm.generate_gold_summary(recent_news, upcoming_events)
    except Exception as exc:
        logger.exception("Summary generation failed")
        await update.message.reply_text(f"❌ Summary failed: {exc}")
        return

    await update.message.reply_text(f"🔮 Gold Outlook\n\n{outlook}")


# ---------------------------------------------------------------------------
# /fetchnews
# ---------------------------------------------------------------------------

@authorized
async def fetchnews_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger a news poll (fetch, filter, score, notify)."""
    if not update.message:
        return
    await update.message.reply_text("⏳ Polling news feeds…")

    try:
        scored_items = await scheduler.fetch_and_notify_news()
    except Exception as exc:
        logger.exception("Manual news poll failed")
        await update.message.reply_text(f"❌ News poll failed: {exc}")
        return

    if not scored_items:
        await update.message.reply_text("ℹ️ No new relevant news found.")
        return

    # fetch_and_notify_news() already broadcasts the formatted items to all
    # subscribers; just confirm the outcome to the caller here.
    await update.message.reply_text(
        f"✅ Found and notified {len(scored_items)} new item(s)."
    )
