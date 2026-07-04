"""
MongoDB helpers using motor (async driver).

Collections
-----------
subscribers : { "chatId": int }
events      : { "eventId": int, "dateline": int, ... }
settings    : { "key": str, "value": str }   – stores the current cron expression
"""

import logging
from datetime import datetime, timedelta, timezone

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import UpdateOne

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
    except Exception:
        # Duplicate key – already subscribed
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
