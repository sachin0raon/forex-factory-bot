"""
Gold/USD macro news scraper.

Polls a fixed set of free RSS feeds (FXStreet, InvestingLive) and filters
entries down to ones relevant to gold (XAU/USD) or its USD macro drivers
(Fed, CPI, NFP, DXY) via a keyword pre-filter, before they're handed off
for Claude sentiment scoring.
"""

import asyncio
import calendar
import logging
from datetime import datetime, timezone

import feedparser
import requests

from scraper import escape_md

logger = logging.getLogger(__name__)

NEWS_FEEDS = {
    "fxstreet": "https://www.fxstreet.com/rss/news",
    "investinglive": "https://investinglive.com/feed",
    "investinglive_centralbank": "https://investinglive.com/feed/centralbank",
}

GOLD_KEYWORDS = [
    "gold",
    "xau",
    "fomc",
    "warsh",
    "nfp",
    "non-farm",
    "nonfarm",
    "dxy",
    "rate cut",
    "rate hike",
    "usd index",
    "us cpi",
    "fed rate",
    "fed decision",
    "fed chair",
    "fed meeting",
    "fed hike",
    "fed cut",
    "fed policy",
]
# NOTE: bare "dollar"/"inflation"/"fed" were tried and rejected — they match
# ~75% of FXStreet's general forex feed (every "X vs US Dollar" pair story,
# every country's own inflation news), since FXStreet/InvestingLive write
# about USD as counter-currency in nearly every article. This list trades
# recall for precision to keep LLM batch sizes (and cost) down.

# Articles matching any of these terms are excluded even if they also match a
# GOLD_KEYWORD — crypto articles commonly mention "rate cut" or "Fed" in their
# summaries and would otherwise slip through.
EXCLUDE_KEYWORDS = [
    "bitcoin",
    "btc",
    "ethereum",
    "crypto",
    "cryptocurrency",
    "altcoin",
    "ripple",
    "xrp",
    "solana",
    "dogecoin",
    "nft",
]


FEED_TIMEOUT_SECONDS = 15
FEED_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; forex-bot/1.0)"}


def fetch_feed_sync(url: str) -> feedparser.FeedParserDict:
    """
    Blocking fetch+parse of a single RSS/Atom feed.
    Uses `requests` (with an explicit timeout) to fetch the raw bytes, then
    hands them to feedparser — feedparser's own `parse(url)` shells out to
    urllib with no timeout, which can hang the executor thread indefinitely
    on a stalled connection.
    """
    response = requests.get(url, headers=FEED_HEADERS, timeout=FEED_TIMEOUT_SECONDS)
    response.raise_for_status()
    return feedparser.parse(response.content)


def _entry_published_at(entry) -> str:
    """Convert a feedparser entry's published time to an ISO-8601 UTC string."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        ts = calendar.timegm(parsed)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _normalize_entry(entry, source: str) -> dict | None:
    """Turn a feedparser entry into our normalized news-item dict, or None if unusable."""
    link = entry.get("link", "")
    guid = entry.get("id") or link
    if not guid:
        return None

    return {
        "guid": guid,
        "title": entry.get("title", "").strip(),
        "link": link,
        "source": source,
        "publishedAt": _entry_published_at(entry),
        "summary": entry.get("summary", "").strip(),
    }


def _fetch_and_normalize(source: str, url: str) -> list[dict]:
    """Fetch+parse+normalize a single feed. Raises on failure (caller decides how to handle)."""
    parsed = fetch_feed_sync(url)
    if parsed.get("bozo") and not parsed.get("entries"):
        raise RuntimeError(parsed.get("bozo_exception"))

    items = []
    for entry in parsed.get("entries", []):
        item = _normalize_entry(entry, source)
        if item:
            items.append(item)
    logger.info("Fetched %d item(s) from %s", len(items), source)
    return items


async def fetch_all_news() -> list[dict]:
    """
    Fetch and normalize entries from every configured feed concurrently.
    Each feed has its own request timeout (FEED_TIMEOUT_SECONDS); a single
    failing/slow feed is logged and skipped rather than aborting the poll.
    """
    loop = asyncio.get_running_loop()
    results = await asyncio.gather(
        *(
            loop.run_in_executor(None, _fetch_and_normalize, source, url)
            for source, url in NEWS_FEEDS.items()
        ),
        return_exceptions=True,
    )

    items: list[dict] = []
    for (source, url), result in zip(NEWS_FEEDS.items(), results):
        if isinstance(result, BaseException):
            logger.error(
                "Failed to fetch news feed '%s' (%s): %s", source, url, result,
                exc_info=result,
            )
            continue
        items.extend(result)
    return items


async def fetch_fxstreet_recent() -> list[dict]:
    """
    Fetch the FXStreet feed fresh and *unfiltered* (no GOLD_KEYWORDS gate).
    Used for matching calendar events (e.g. "President Trump Speaks") to their
    news coverage — a speech about, say, election security wouldn't pass the
    gold/USD-driver keyword filter, but we still need to find it.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _fetch_and_normalize, "fxstreet", NEWS_FEEDS["fxstreet"]
    )


def is_relevant(item: dict) -> bool:
    """Keyword pre-filter against title+summary, case-insensitive."""
    haystack = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    if any(term in haystack for term in EXCLUDE_KEYWORDS):
        return False
    return any(keyword in haystack for keyword in GOLD_KEYWORDS)


_SENTIMENT_EMOJI = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}
_IMPACT_EMOJI = {"high": "🔺", "medium": "🔸", "low": "▫️"}


def format_news_message(item: dict) -> str:
    """Build a Telegram MarkdownV2 message for a single scored news item."""
    sentiment = (item.get("sentiment") or "neutral").lower()
    emoji = _SENTIMENT_EMOJI.get(sentiment, "⚪")
    impact = (item.get("impact") or "medium").lower()
    impact_emoji = _IMPACT_EMOJI.get(impact, "🔸")

    lines = [f"{impact_emoji} {emoji} *{escape_md(item['title'])}*"]

    rationale = item.get("rationale")
    if rationale:
        lines.append(f"💡 {escape_md(rationale)}")

    if item.get("link"):
        lines.append(f"🔗 {escape_md(item['link'])}")

    return "\n".join(lines)


def format_news_summary(items: list[dict], title: str = "📰 *Gold/USD News*") -> str:
    """Format multiple scored news items into a single notification message."""
    if not items:
        return f"{title}\n\nNo news found\\."

    parts = [title, ""]
    for item in items:
        parts.append(format_news_message(item))
        parts.append("─" * 15)

    return "\n".join(parts)
