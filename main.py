"""
ForexFactory Telegram Bot – main entry point.

Starts the Telegram bot, connects to MongoDB, and launches the APScheduler.
"""

import logging

from telegram.ext import ApplicationBuilder, CommandHandler

import bot
import db
import scheduler
from config import TELEGRAM_BOT_TOKEN

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------

async def post_init(application) -> None:
    """Called after the Application is fully initialised."""
    # Connect to MongoDB
    await db.connect()
    logger.info("MongoDB connected")

    # Give the scheduler a reference to the bot so it can send messages
    scheduler.set_bot_app(application)

    # Start the APScheduler cron job
    await scheduler.start_scheduler()
    logger.info("Scheduler started")


async def post_shutdown(application) -> None:
    """Called when the Application is shutting down."""
    sched = scheduler.get_scheduler()
    if sched.running:
        sched.shutdown(wait=False)
        logger.info("Scheduler shut down")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Build and run the Telegram bot."""
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start", bot.start_command))
    app.add_handler(CommandHandler("cron", bot.cron_command))
    app.add_handler(CommandHandler("fetch", bot.fetch_command))
    app.add_handler(CommandHandler("events", bot.events_command))

    logger.info("🚀 ForexFactory Bot starting …")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
