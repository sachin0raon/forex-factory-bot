"""
APScheduler management for recurring and one-off event data fetching.

Uses AsyncIOScheduler from APScheduler 3.x.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db
import scraper
from config import DEFAULT_CRON

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

# Reference to the telegram bot application – set during startup
_bot_app = None

MAIN_JOB_ID = "forex_fetch_job"


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def get_scheduler() -> AsyncIOScheduler:
    """Return the singleton scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def set_bot_app(app) -> None:
    """Store a reference to the Telegram Application so jobs can send msgs."""
    global _bot_app
    _bot_app = app


async def start_scheduler() -> None:
    """
    Start the scheduler and register the main cron job.
    Uses the persisted cron expression from the DB if one exists,
    otherwise falls back to DEFAULT_CRON.
    """
    sched = get_scheduler()

    # Restore cron expression
    saved_cron = await db.get_setting("cron_expression")
    cron_expr = saved_cron or DEFAULT_CRON
    await db.set_setting("cron_expression", cron_expr)

    trigger = _parse_cron(cron_expr)
    sched.add_job(
        fetch_and_notify,
        trigger=trigger,
        id=MAIN_JOB_ID,
        replace_existing=True,
        name="ForexFactory weekly fetch",
    )
    sched.start()
    logger.info("Scheduler started with cron: %s", cron_expr)


# ---------------------------------------------------------------------------
# Cron management
# ---------------------------------------------------------------------------

async def get_current_cron() -> str:
    """Return the currently configured cron expression."""
    expr = await db.get_setting("cron_expression")
    return expr or DEFAULT_CRON


async def update_cron(cron_expr: str) -> None:
    """Update the main job's cron trigger and persist the expression."""
    sched = get_scheduler()
    trigger = _parse_cron(cron_expr)
    sched.reschedule_job(MAIN_JOB_ID, trigger=trigger)
    await db.set_setting("cron_expression", cron_expr)
    logger.info("Cron expression updated to: %s", cron_expr)


def _parse_cron(expr: str) -> CronTrigger:
    """Convert a 5-field cron string into an APScheduler CronTrigger."""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"Invalid cron expression '{expr}'. Expected 5 fields: "
            "minute hour day month day_of_week"
        )
    minute, hour, day, month, day_of_week = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone="UTC",
    )


# ---------------------------------------------------------------------------
# Core fetch-and-notify logic
# ---------------------------------------------------------------------------

async def fetch_and_store() -> list[dict]:
    """
    Fetch, parse, persist, and schedule actual-checks — without sending any notification.
    Returns the list of parsed events (empty list if none found).
    """
    loop = asyncio.get_running_loop()

    raw_data = await asyncio.wait_for(
        loop.run_in_executor(None, scraper.fetch_forex_data),
        timeout=60.0,
    )

    events = scraper.parse_events(raw_data)
    if not events:
        logger.warning("No events returned from ForexFactory")
        return []

    new_events = await db.upsert_events(events)
    logger.info("Persisted %d new events out of %d total", len(new_events), len(events))

    _schedule_actual_checks(events)

    return events


async def fetch_and_notify() -> list[dict]:
    """
    Scheduled job: fetch + store, then broadcast to all subscribers.
    Returns the list of parsed events.
    """
    events = await fetch_and_store()
    if events:
        await _notify_subscribers(events, title="📰 *ForexFactory Weekly Events*")
    return events


async def fetch_event_actual(event_id: int, dateline: int) -> None:
    """
    One-off job: re-fetch data from ForexFactory, find the target event,
    and if the 'actual' value is now populated, update the DB and notify.
    """
    loop = asyncio.get_running_loop()

    try:
        raw_data = await asyncio.wait_for(
            loop.run_in_executor(None, scraper.fetch_forex_data),
            timeout=60.0,
        )
    except Exception:
        logger.exception("Failed to re-fetch data for event %s", event_id)
        return

    events = scraper.parse_events(raw_data)
    target = next(
        (e for e in events if e["eventId"] == event_id and e["dateline"] == dateline),
        None,
    )

    if target is None:
        logger.warning("Event %s not found in re-fetch", event_id)
        return

    # Upsert (will update the actual field if it's now present)
    await db.upsert_event(target)

    actual = target.get("actual", "")
    if actual:
        logger.info("Actual value for event %s: %s", event_id, actual)
        await _notify_subscribers(
            [target],
            title="📢 *Event Actual Released\\!*",
        )
    else:
        logger.info("Actual still not available for event %s", event_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _schedule_actual_checks(events: list[dict]) -> None:
    """
    For each future event, schedule a one-off job 30 seconds after eventDate
    to attempt capturing the 'actual' value.
    """
    sched = get_scheduler()
    now = datetime.now(timezone.utc)

    for ev in events:
        # Skip events that already have an actual value
        if ev.get("actual"):
            continue

        try:
            event_dt = datetime.fromisoformat(ev["eventDate"])
        except (ValueError, KeyError):
            continue

        run_at = event_dt + timedelta(seconds=30)
        if run_at <= now:
            continue  # Already in the past

        job_id = f"actual_{ev['eventId']}_{ev['dateline']}"
        sched.add_job(
            fetch_event_actual,
            "date",
            run_date=run_at,
            args=[ev["eventId"], ev["dateline"]],
            id=job_id,
            replace_existing=True,
            name=f"Actual check: {ev['name']}",
        )
        logger.info(
            "Scheduled actual check for '%s' at %s", ev["name"], run_at.isoformat()
        )


async def _notify_subscribers(events: list[dict], title: str) -> None:
    """Send a formatted message to every subscriber concurrently."""
    if _bot_app is None:
        logger.warning("Bot app not set – cannot send notifications")
        return

    subscribers = await db.get_all_subscribers()
    if not subscribers:
        logger.info("No subscribers to notify")
        return

    message = scraper.format_events_summary(events, title=title)
    chunks = scraper.split_message(message, 4000)

    async def _send_to_chat(chat_id: int) -> None:
        for chunk in chunks:
            try:
                await _bot_app.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="MarkdownV2",
                )
            except Exception:
                logger.exception("Failed to notify chat %s", chat_id)

    await asyncio.gather(*[_send_to_chat(cid) for cid in subscribers])


