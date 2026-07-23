"""
APScheduler management for recurring and one-off event data fetching.

Uses AsyncIOScheduler from APScheduler 3.x.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import db
import llm
import news
import scraper
from config import (
    ARTICLE_SEARCH_DELAY_MINUTES,
    ARTICLE_SEARCH_MAX_ATTEMPTS,
    ARTICLE_SEARCH_RETRY_MINUTES,
    DEFAULT_CRON,
    NEWS_POLL_MINUTES,
    NEWS_POLLING_ENABLED,
)

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

# Reference to the telegram bot application – set during startup
_bot_app = None

MAIN_JOB_ID = "forex_fetch_job"
NEWS_JOB_ID = "news_fetch_job"


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
    if NEWS_POLLING_ENABLED:
        sched.add_job(
            fetch_and_notify_news,
            trigger=IntervalTrigger(minutes=NEWS_POLL_MINUTES),
            id=NEWS_JOB_ID,
            replace_existing=True,
            name="Gold/USD news poll",
        )

    sched.start()
    logger.info(
        "Scheduler started with cron: %s (news poll %s)",
        cron_expr,
        f"every {NEWS_POLL_MINUTES} min" if NEWS_POLLING_ENABLED else "disabled",
    )


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
    _schedule_article_searches(events)

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


async def fetch_and_notify_news() -> list[dict]:
    """
    Scheduled job: poll news feeds, filter to gold/USD-relevant items, dedupe
    against the DB (read-only check), score genuinely new items with Claude,
    persist the scored items, and broadcast to subscribers.

    Nothing is written to the DB until *after* scoring succeeds: if
    llm.score_news_batch raises (rate limit, timeout, network blip), this
    function propagates the exception without persisting anything, so the
    same items are picked up again on the next poll instead of being
    silently marked "seen" and lost. The scheduled job itself is protected by
    APScheduler's default exception logging; the manual /fetchnews command
    surfaces the error directly to the caller.
    """
    try:
        raw_items = await asyncio.wait_for(news.fetch_all_news(), timeout=30.0)
    except asyncio.TimeoutError:
        logger.warning("News poll timed out while fetching feeds")
        return []

    relevant = [item for item in raw_items if news.is_relevant(item)]
    new_items = await db.filter_unseen_items(relevant)
    if not new_items:
        logger.info("News poll: no new relevant items")
        return []

    logger.info("News poll: %d new relevant item(s), scoring with Claude", len(new_items))
    scored_items = await llm.score_news_batch(new_items)

    await db.upsert_news_items(scored_items)

    actionable = [i for i in scored_items if i.get("sentiment", "neutral") != "neutral"]
    if actionable:
        await _notify_subscribers_news(actionable)
    else:
        logger.info("News poll: all %d item(s) scored neutral, skipping notification", len(scored_items))

    return scored_items


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


ARTICLE_SEARCH_JOB_PREFIX = "article_search"


async def search_event_article(
    event_id: int, dateline: int, event_name: str, event_date_iso: str, attempt: int
) -> None:
    """
    One-off, self-rescheduling job: search FXStreet for the article covering a
    no-forecast (speech/testimony) calendar event, retrying up to
    ARTICLE_SEARCH_MAX_ATTEMPTS times, ARTICLE_SEARCH_RETRY_MINUTES apart.
    Persists the terminal outcome (found/not_found) and notifies subscribers
    either way.
    """
    if await db.get_event_article(event_id, dateline):
        # Already resolved by a previous chain (e.g. duplicate scheduling from
        # a re-poll) — nothing left to do.
        return

    # This is a bounded, self-scheduling chain of one-off jobs, not a
    # recurring poll — unlike fetch_and_notify_news, there's no "next cycle"
    # safety net. Any failure here (feed fetch, Claude match call — rate
    # limit, network blip, timeout) must fall through to the same
    # retry-or-give-up logic as a genuine "no match", or the whole chain would
    # die silently: no retry scheduled, no notification ever sent, nothing
    # persisted.
    match = None
    try:
        try:
            candidates_raw = await asyncio.wait_for(news.fetch_fxstreet_recent(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("Article search for '%s' timed out fetching FXStreet", event_name)
            candidates_raw = []

        event_dt = datetime.fromisoformat(event_date_iso)
        candidates = [
            c for c in candidates_raw
            if datetime.fromisoformat(c["publishedAt"]) >= event_dt
        ][:15]

        if candidates:
            match = await llm.match_event_article(event_name, event_date_iso, candidates)
    except Exception:
        logger.exception(
            "Article search attempt %d for '%s' failed; treating as no match", attempt, event_name
        )
        match = None

    if match:
        try:
            enrichment = await llm.summarize_event_article(event_name, match)
        except Exception:
            # We DID find the right article — don't discard that just because
            # the write-up call failed. Fall back to a minimal summary rather
            # than re-running (and possibly losing) the match on a retry.
            logger.exception(
                "Found a match for '%s' but summarization failed; using a minimal fallback",
                event_name,
            )
            enrichment = {
                "summary": match["title"],
                "market_reaction": "Not specified (summary generation failed).",
            }
        doc = {
            "eventId": event_id,
            "dateline": dateline,
            "eventName": event_name,
            "eventDate": event_date_iso,
            "status": "found",
            "articleLink": match["link"],
            "articleTitle": match["title"],
            "summary": enrichment["summary"],
            "marketReaction": enrichment["market_reaction"],
            "attempts": attempt,
            "resolvedAt": datetime.now(timezone.utc).isoformat(),
        }
        await db.upsert_event_article(doc)
        await _notify_event_article_found(doc)
        logger.info("Article search: found match for '%s' on attempt %d", event_name, attempt)
        return

    if attempt < ARTICLE_SEARCH_MAX_ATTEMPTS:
        sched = get_scheduler()
        next_run = datetime.now(timezone.utc) + timedelta(minutes=ARTICLE_SEARCH_RETRY_MINUTES)
        job_id = f"{ARTICLE_SEARCH_JOB_PREFIX}_{event_id}_{dateline}_{attempt + 1}"
        sched.add_job(
            search_event_article,
            "date",
            run_date=next_run,
            args=[event_id, dateline, event_name, event_date_iso, attempt + 1],
            id=job_id,
            replace_existing=True,
            name=f"Article search: {event_name} (attempt {attempt + 1})",
        )
        logger.info(
            "Article search: no match for '%s' on attempt %d, retrying at %s",
            event_name, attempt, next_run.isoformat(),
        )
        return

    doc = {
        "eventId": event_id,
        "dateline": dateline,
        "eventName": event_name,
        "eventDate": event_date_iso,
        "status": "not_found",
        "articleLink": None,
        "articleTitle": None,
        "summary": None,
        "marketReaction": None,
        "attempts": attempt,
        "resolvedAt": datetime.now(timezone.utc).isoformat(),
    }
    await db.upsert_event_article(doc)
    await _notify_event_article_not_found(doc)
    logger.info("Article search: exhausted %d attempt(s) for '%s', no match found", attempt, event_name)


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

        # Speech/testimony-type events (no forecast) never publish an actual
        # value either — they get an FXStreet article search instead, via
        # _schedule_article_searches.
        if not ev.get("forecast"):
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


def _schedule_article_searches(events: list[dict]) -> None:
    """
    For each future no-forecast (speech/testimony) event, schedule the first
    FXStreet article-search attempt at eventDate + ARTICLE_SEARCH_DELAY_MINUTES.
    """
    sched = get_scheduler()
    now = datetime.now(timezone.utc)

    for ev in events:
        if ev.get("forecast"):
            continue  # has a forecast → data-release event, not a speech

        try:
            event_dt = datetime.fromisoformat(ev["eventDate"])
        except (ValueError, KeyError):
            continue

        run_at = event_dt + timedelta(minutes=ARTICLE_SEARCH_DELAY_MINUTES)
        if run_at <= now:
            continue  # Already in the past

        job_id = f"{ARTICLE_SEARCH_JOB_PREFIX}_{ev['eventId']}_{ev['dateline']}_1"
        sched.add_job(
            search_event_article,
            "date",
            run_date=run_at,
            args=[ev["eventId"], ev["dateline"], ev["name"], ev["eventDate"], 1],
            id=job_id,
            replace_existing=True,
            name=f"Article search: {ev['name']} (attempt 1)",
        )
        logger.info(
            "Scheduled article search for '%s' at %s", ev["name"], run_at.isoformat()
        )


async def _notify_subscribers(events: list[dict], title: str) -> None:
    """Send a formatted calendar-events message to every subscriber."""
    message = scraper.format_events_summary(events, title=title)
    await _broadcast(scraper.split_message(message, 4000))


async def _notify_subscribers_news(items: list[dict], title: str = "📰 *Gold/USD News*") -> None:
    """Send a formatted news message to every subscriber."""
    message = news.format_news_summary(items, title=title)
    await _broadcast(scraper.split_message(message, 4000))


async def _notify_event_article_found(doc: dict) -> None:
    """Notify subscribers that FXStreet coverage was found for a speech/testimony event."""
    lines = [
        f"📰 *Article Found: {scraper.escape_md(doc['eventName'])}*",
        f"📄 {scraper.escape_md(doc['articleTitle'])}",
        f"💡 {scraper.escape_md(doc['summary'])}",
        f"📊 Market Reaction: {scraper.escape_md(doc['marketReaction'])}",
        f"🔗 {scraper.escape_md(doc['articleLink'])}",
    ]
    await _broadcast(scraper.split_message("\n".join(lines), 4000))


async def _notify_event_article_not_found(doc: dict) -> None:
    """Notify subscribers that no FXStreet coverage was found after all retry attempts."""
    lines = [
        f"🔍 *No Article Found: {scraper.escape_md(doc['eventName'])}*",
        f"Searched FXStreet {doc['attempts']} time\\(s\\) after the event — "
        "no matching article found\\.",
    ]
    await _broadcast(scraper.split_message("\n".join(lines), 4000))


async def _broadcast(chunks: list[str]) -> None:
    """Send pre-formatted message chunks to every subscriber concurrently."""
    if _bot_app is None:
        logger.warning("Bot app not set – cannot send notifications")
        return

    subscribers = await db.get_all_subscribers()
    if not subscribers:
        logger.info("No subscribers to notify")
        return

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


