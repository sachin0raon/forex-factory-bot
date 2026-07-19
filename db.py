"""
MongoDB helpers using motor (async driver).

Collections
-----------
subscribers : { "chatId": int }
events      : { "eventId": int, "dateline": int, ... }
news        : { "guid": str, "title": str, ..., "sentiment": str, "score": float }
event_articles : { "eventId": int, "dateline": int, "status": "found"|"not_found", ... }
settings    : { "key": str, "value": str }   – stores the current cron expression
"""

import logging
from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from config import MONGO_URI, MONGO_DB_NAME

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

async def connect() -> AsyncIOMotorDatabase:
    """Initialise (or return cached) motor client & database handle."""
    global _client, _db
    if _db is None:
        _client = AsyncIOMotorClient(MONGO_URI)
        _db = _client[MONGO_DB_NAME]
        # Ensure indexes
        await _db.events.create_index(
            [("eventId", 1), ("dateline", 1)], unique=True
        )
        await _db.subscribers.create_index("chatId", unique=True)
        await _db.news.create_index("guid", unique=True)
        await _db.event_articles.create_index(
            [("eventId", 1), ("dateline", 1)], unique=True
        )
        logger.info("Connected to MongoDB (%s)", MONGO_DB_NAME)
    return _db


async def get_db() -> AsyncIOMotorDatabase:
    """Return the database handle, connecting first if needed."""
    return await connect()


# ---------------------------------------------------------------------------
# Subscribers
# ---------------------------------------------------------------------------

async def add_subscriber(chat_id: int) -> bool:
    """
    Add *chat_id* to the subscribers collection.
    Returns True if newly added, False if already present.
    """
    db = await get_db()
    try:
        await db.subscribers.insert_one({"chatId": chat_id})
        logger.info("New subscriber added: %s", chat_id)
        return True
    except DuplicateKeyError:
        return False


async def get_all_subscribers() -> list[int]:
    """Return a list of all subscribed chat IDs."""
    db = await get_db()
    cursor = db.subscribers.find({}, {"_id": 0, "chatId": 1})
    return [doc["chatId"] async for doc in cursor]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

async def upsert_event(event: dict) -> bool:
    """
    Insert a new event or update an existing one (matched by eventId + dateline).
    Returns True if the document was newly inserted, False if it already existed
    (and was potentially updated).
    """
    db = await get_db()
    filter_q = {"eventId": event["eventId"], "dateline": event["dateline"]}
    result = await db.events.update_one(
        filter_q,
        {"$set": event},
        upsert=True,
    )
    return result.upserted_id is not None


async def upsert_events(events: list[dict]) -> list[dict]:
    """
    Upsert a batch of events in a single bulk_write call.
    Returns the list of *newly inserted* events.
    """
    if not events:
        return []
    db = await get_db()
    operations = [
        UpdateOne(
            {"eventId": ev["eventId"], "dateline": ev["dateline"]},
            {"$set": ev},
            upsert=True,
        )
        for ev in events
    ]
    result = await db.events.bulk_write(operations, ordered=False)
    # result.upserted_ids maps operation-index → ObjectId for newly inserted docs
    new_events = [events[i] for i in (result.upserted_ids or {})]
    return new_events


async def get_event_by_id(event_id: int, dateline: int) -> dict | None:
    """Fetch a single event by its eventId and dateline."""
    db = await get_db()
    return await db.events.find_one(
        {"eventId": event_id, "dateline": dateline},
        {"_id": 0},
    )


async def get_current_week_events() -> list[dict]:
    """Return all events stored for the current week (Mon-Sun)."""
    db = await get_db()
    now = datetime.now(timezone.utc)
    # Monday 00:00 UTC of current week
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday -= timedelta(days=now.weekday())
    sunday = monday + timedelta(days=7)

    cursor = db.events.find(
        {
            "eventDate": {
                "$gte": monday.isoformat(),
                "$lt": sunday.isoformat(),
            }
        },
        {"_id": 0},
    ).sort("eventDate", 1)

    return [doc async for doc in cursor]


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

async def filter_unseen_items(items: list[dict]) -> list[dict]:
    """
    Read-only dedupe check: given candidate items (each with a 'guid'), return
    the subset whose guid isn't already stored. Does NOT write anything —
    callers should only persist (via upsert_news_items) *after* any downstream
    processing (e.g. Claude scoring) succeeds, so a mid-pipeline failure leaves
    these items eligible to be picked up again on the next poll instead of
    being silently dropped.
    """
    if not items:
        return []
    db = await get_db()
    guids = [item["guid"] for item in items]
    cursor = db.news.find({"guid": {"$in": guids}}, {"_id": 0, "guid": 1})
    existing = {doc["guid"] async for doc in cursor}
    return [item for item in items if item["guid"] not in existing]


async def upsert_news_items(items: list[dict]) -> list[dict]:
    """
    Upsert a batch of (already-processed) news items in a single bulk_write
    call, matched by guid. Returns the list of *newly inserted* items.
    """
    if not items:
        return []
    db = await get_db()
    operations = [
        UpdateOne({"guid": item["guid"]}, {"$set": item}, upsert=True)
        for item in items
    ]
    result = await db.news.bulk_write(operations, ordered=False)
    new_items = [items[i] for i in (result.upserted_ids or {})]
    return new_items


async def get_recent_news(hours: int = 24) -> list[dict]:
    """Return news items published within the last *hours*, newest first."""
    db = await get_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    cursor = db.news.find(
        {"publishedAt": {"$gte": since}}, {"_id": 0}
    ).sort("publishedAt", -1)
    return [doc async for doc in cursor]


async def get_upcoming_events(hours: int = 48) -> list[dict]:
    """Return calendar events due within the next *hours*, soonest first."""
    db = await get_db()
    now = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours)
    cursor = db.events.find(
        {"eventDate": {"$gte": now.isoformat(), "$lt": until.isoformat()}},
        {"_id": 0},
    ).sort("eventDate", 1)
    return [doc async for doc in cursor]


# ---------------------------------------------------------------------------
# Event articles (FXStreet coverage for no-forecast "speech" calendar events)
# ---------------------------------------------------------------------------

async def get_event_article(event_id: int, dateline: int) -> dict | None:
    """
    Fetch the resolved (found/not_found) outcome for an event's article search,
    if one exists. Doubles as the idempotency guard: if a doc already exists,
    the search chain for this event has already reached a terminal state and
    shouldn't be re-scheduled or re-run.
    """
    db = await get_db()
    return await db.event_articles.find_one(
        {"eventId": event_id, "dateline": dateline}, {"_id": 0}
    )


async def upsert_event_article(doc: dict) -> None:
    """Persist the terminal (found or not_found) outcome of an article search."""
    db = await get_db()
    await db.event_articles.update_one(
        {"eventId": doc["eventId"], "dateline": doc["dateline"]},
        {"$set": doc},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Settings (cron expression persistence)
# ---------------------------------------------------------------------------

async def get_setting(key: str) -> str | None:
    """Read a setting value by key."""
    db = await get_db()
    doc = await db.settings.find_one({"key": key})
    return doc["value"] if doc else None


async def set_setting(key: str, value: str) -> None:
    """Persist a setting value."""
    db = await get_db()
    await db.settings.update_one(
        {"key": key}, {"$set": {"key": key, "value": value}}, upsert=True
    )
