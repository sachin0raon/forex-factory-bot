"""
ForexFactory calendar data scraper.

Uses cloudscraper to bypass Cloudflare and fetch economic event data
from the ForexFactory calendar API.
"""

import logging
from datetime import datetime, timezone

import cloudscraper
import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

FOREXFACTORY_URL = (
    "https://www.forexfactory.com/calendar/apply-settings/1?navigation=0"
)

PAYLOAD = {
    "default_view": "this_week",
    "impacts": [3, 2],
    "event_types": [1, 2, 3, 4, 5, 7, 8, 9, 10, 11],
    "currencies": [9],
}


def _create_scraper() -> cloudscraper.CloudScraper:
    """Create a cloudscraper instance with stealth options."""
    return cloudscraper.create_scraper(
        browser="chrome",
        delay=5,
    )


def fetch_forex_data() -> dict:
    """
    Send POST request to ForexFactory and return the raw JSON response.
    This is a *synchronous* call (cloudscraper is not async) and should be
    run in an executor when called from async code.
    """
    scraper = _create_scraper()
    logger.info("Fetching ForexFactory calendar data …")
    response = scraper.post(FOREXFACTORY_URL, json=PAYLOAD)
    response.raise_for_status()
    data = response.json()
    logger.info("Received %d day(s) from ForexFactory", len(data.get("days", [])))
    return data


def parse_events(data: dict) -> list[dict]:
    """
    Flatten the nested days→events structure into a list of documents
    ready for MongoDB persistence.
    """
    events: list[dict] = []
    for day in data.get("days", []):
        day_dateline = day.get("dateline")
        for ev in day.get("events", []):
            event_dt = datetime.fromtimestamp(ev["dateline"], tz=timezone.utc)
            events.append(
                {
                    "eventId": ev["id"],
                    "name": ev["name"],
                    "dateline": day_dateline,
                    "eventDate": event_dt.isoformat(),
                    "country": ev.get("country", ""),
                    "currency": ev.get("currency", ""),
                    "impactName": ev.get("impactName", ""),
                    "impactTitle": ev.get("impactTitle", ""),
                    "actual": ev.get("actual", ""),
                    "forecast": ev.get("forecast", ""),
                }
            )
    return events


def format_event_message(event: dict) -> str:
    """
    Build a beautifully formatted Telegram message (MarkdownV2 compatible)
    for a single event.
    """
    impact = event.get("impactName", "").lower()
    impact_emoji = "🔴" if impact == "high" else "🟠"

    # Convert eventDate to IST for display
    try:
        event_dt = datetime.fromisoformat(event["eventDate"])
        ist_dt = event_dt.astimezone(IST)
        date_str = ist_dt.strftime("%a, %d %b %Y %I:%M %p IST")
    except Exception:
        date_str = event.get("eventDate", "N/A")

    lines = [
        f"{impact_emoji} *{_escape_md(event['name'])}*",
        f"📅 {_escape_md(date_str)}",
        f"🌍 {_escape_md(event.get('country', ''))} \\| 💰 {_escape_md(event.get('currency', ''))}",
        f"📊 Impact: {_escape_md(event.get('impactTitle', ''))}",
    ]

    forecast = event.get("forecast", "")
    actual = event.get("actual", "")

    if forecast:
        lines.append(f"🔮 Forecast: `{_escape_md(forecast)}`")
    if actual:
        lines.append(f"✅ Actual: `{_escape_md(actual)}`")

    return "\n".join(lines)


def format_events_summary(events: list[dict], title: str = "📰 *ForexFactory Events*") -> str:
    """
    Format multiple events into a single notification message.
    """
    if not events:
        return f"{title}\n\nNo events found\\."

    parts = [title, ""]
    for ev in events:
        parts.append(format_event_message(ev))
        parts.append("─" * 30)

    return "\n".join(parts)


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"_*[]()~`>#+-=|{}.!"
    escaped = ""
    for ch in str(text):
        if ch in special:
            escaped += f"\\{ch}"
        else:
            escaped += ch
    return escaped
