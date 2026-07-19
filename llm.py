"""
LLM integration – news sentiment/bias scoring and the on-demand gold outlook.

Backed by either Claude or Gemini, chosen by config.LLM_PROVIDER — see
_generate_text() below, the single dispatch point. Every public function's
prompt text and JSON-parsing/fallback logic is provider-agnostic and unchanged
regardless of which backend is active.

Two model tiers are used deliberately:
  - SENTIMENT_MODEL (cheap/fast): runs every poll cycle, one batched call covering
    all newly-seen relevant news items (also used for event-article matching).
  - SUMMARY_MODEL (higher quality): runs only when a user calls /summary, or when
    a matched event article is being written up.
"""

import asyncio
import json
import logging
import re

from anthropic import AsyncAnthropic
from google import genai
from google.genai import types as genai_types

from config import (
    ANTHROPIC_API_KEY,
    EVENT_SUMMARY_MAX_TOKENS,
    GEMINI_API_KEY,
    LLM_PROVIDER,
    MATCH_MAX_TOKENS,
    SENTIMENT_MAX_TOKENS,
    SENTIMENT_MODEL,
    SUMMARY_MAX_TOKENS,
    SUMMARY_MODEL,
)

logger = logging.getLogger(__name__)

# Only the active provider's client is constructed — google-genai's Client
# raises ValueError immediately (not just on first request) if api_key is
# empty, so unconditionally constructing both would break startup for anyone
# on the default provider who hasn't also set the *other* provider's key.
_anthropic_client: AsyncAnthropic | None = None
_gemini_client: genai.Client | None = None

if LLM_PROVIDER == "gemini":
    _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
else:
    _anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

_SUMMARY_MAX_CHARS = 500
_SENTIMENT_TIMEOUT_SECONDS = 30.0
_SUMMARY_TIMEOUT_SECONDS = 60.0
_MATCH_TIMEOUT_SECONDS = 20.0


async def _generate_anthropic(system: str, user_content: str, model: str, max_tokens: int, timeout: float) -> str:
    assert _anthropic_client is not None, "LLM_PROVIDER is 'anthropic' but the client wasn't constructed"
    response = await _anthropic_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
        timeout=timeout,
    )
    return "".join(block.text for block in response.content if block.type == "text")


async def _generate_gemini(
    system: str, user_content: str, model: str, max_tokens: int, timeout: float, json_mode: bool
) -> str:
    assert _gemini_client is not None, "LLM_PROVIDER is 'gemini' but the client wasn't constructed"
    config = genai_types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens,
        # SDK-level timeout is in milliseconds and has had reliability issues
        # in some google-genai versions (see googleapis/python-genai#911,
        # #1330) — asyncio.wait_for below is the reliable outer bound; this
        # is just a courtesy hint to the SDK/httpx layer.
        http_options=genai_types.HttpOptions(timeout=int(timeout * 1000)),
        response_mime_type="application/json" if json_mode else None,
    )
    response = await asyncio.wait_for(
        _gemini_client.aio.models.generate_content(
            model=model, contents=user_content, config=config
        ),
        timeout=timeout,
    )
    return response.text or ""


async def _generate_text(
    system: str,
    user_content: str,
    model: str,
    max_tokens: int,
    timeout: float,
    json_mode: bool = False,
) -> str:
    """Single dispatch point: routes to whichever provider config.LLM_PROVIDER selects."""
    if LLM_PROVIDER == "gemini":
        return await _generate_gemini(system, user_content, model, max_tokens, timeout, json_mode)
    return await _generate_anthropic(system, user_content, model, max_tokens, timeout)


_SENTIMENT_SYSTEM_PROMPT = (
    "You are a gold (XAU/USD) market analyst. For each news item, judge its "
    "likely directional impact on gold price. Respond with ONLY a JSON array, "
    "no prose, no markdown fences. Each element must be an object with exactly "
    'these keys: "guid" (copied verbatim from the input), "sentiment" (one of '
    '"bullish", "bearish", "neutral" for gold), "score" (a number from -1.0 '
    "bearish to 1.0 bullish), and \"rationale\" (a single short sentence)."
)


def _strip_code_fence(text: str) -> str:
    """Remove a ```json ... ``` or ``` ... ``` wrapper if Claude added one."""
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1) if match else text


async def score_news_batch(items: list[dict]) -> list[dict]:
    """
    Score a batch of news items for gold sentiment/bias in a single API call.
    Returns copies of *items* with sentiment/score/rationale added; items the
    model doesn't return a match for keep neutral/zero defaults.
    """
    if not items:
        return []

    payload = [
        {
            "guid": item["guid"],
            "title": item["title"],
            "summary": item.get("summary", "")[:_SUMMARY_MAX_CHARS],
        }
        for item in items
    ]

    text = await _generate_text(
        system=_SENTIMENT_SYSTEM_PROMPT,
        user_content=json.dumps(payload),
        model=SENTIMENT_MODEL,
        max_tokens=SENTIMENT_MAX_TOKENS,
        timeout=_SENTIMENT_TIMEOUT_SECONDS,
        json_mode=True,
    )

    try:
        scored_by_guid = {
            entry["guid"]: entry for entry in json.loads(_strip_code_fence(text))
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.exception("Failed to parse %s sentiment response: %s", LLM_PROVIDER, text)
        scored_by_guid = {}

    results = []
    for item in items:
        scored = scored_by_guid.get(item["guid"], {})
        results.append(
            {
                **item,
                "sentiment": scored.get("sentiment", "neutral"),
                "score": scored.get("score", 0.0),
                "rationale": scored.get("rationale", ""),
            }
        )
    return results


_SUMMARY_SYSTEM_PROMPT = (
    "You are a gold (XAU/USD) market analyst producing a short, personal-use "
    "briefing. Given recent scored news items and upcoming USD-relevant economic "
    "calendar events, write a concise overall gold outlook (bullish/bearish/neutral "
    "bias with your reasoning, plus what to watch for from the upcoming events). "
    "Keep it under 250 words. Plain text, no markdown headers."
)


async def generate_gold_summary(news_items: list[dict], upcoming_events: list[dict]) -> str:
    """One-shot call combining recent news + upcoming calendar events into an outlook."""
    news_lines = [
        f"- [{item.get('sentiment', 'neutral')}] {item['title']} — {item.get('rationale', '')}"
        for item in news_items
    ] or ["- No recent relevant news."]

    event_lines = [
        f"- {ev['name']} ({ev.get('impactName', '')} impact) at {ev['eventDate']}, "
        f"forecast: {ev.get('forecast', 'n/a')}"
        for ev in upcoming_events
    ] or ["- No high/medium-impact USD events upcoming."]

    user_content = (
        "Recent news:\n" + "\n".join(news_lines) +
        "\n\nUpcoming events:\n" + "\n".join(event_lines)
    )

    text = await _generate_text(
        system=_SUMMARY_SYSTEM_PROMPT,
        user_content=user_content,
        model=SUMMARY_MODEL,
        max_tokens=SUMMARY_MAX_TOKENS,
        timeout=_SUMMARY_TIMEOUT_SECONDS,
    )
    return text.strip()


_MATCH_SYSTEM_PROMPT = (
    "You are matching a scheduled economic-calendar event (typically a speech "
    "or testimony, which has no forecast/actual data value) to news coverage "
    "of it. Given the event's name and time, and a list of recent news items "
    "(each with a link, title, summary, and publish time), determine whether "
    "any item is actually reporting on THIS SPECIFIC event — not just a "
    "topically related story. Respond with ONLY JSON, no prose, no markdown "
    'fences: {"matched_link": "<link of the matching item>"} if you find a '
    'confident match, or {"matched_link": null} if none clearly match.'
)


async def match_event_article(
    event_name: str, event_date_iso: str, candidates: list[dict]
) -> dict | None:
    """
    Cheap classification call: does any candidate news item (from
    news.fetch_fxstreet_recent, already filtered to items published at/after
    the event) actually report on this calendar event? Returns the matching
    candidate dict, or None if no confident match / on any failure.
    """
    if not candidates:
        return None

    payload = {
        "event": {"name": event_name, "time": event_date_iso},
        "candidates": [
            {
                "link": c["link"],
                "title": c["title"],
                "summary": c.get("summary", "")[:_SUMMARY_MAX_CHARS],
                "publishedAt": c["publishedAt"],
            }
            for c in candidates
        ],
    }

    text = await _generate_text(
        system=_MATCH_SYSTEM_PROMPT,
        user_content=json.dumps(payload),
        model=SENTIMENT_MODEL,
        max_tokens=MATCH_MAX_TOKENS,
        timeout=_MATCH_TIMEOUT_SECONDS,
        json_mode=True,
    )

    try:
        matched_link = json.loads(_strip_code_fence(text)).get("matched_link")
    except (json.JSONDecodeError, AttributeError):
        logger.exception("Failed to parse %s event-match response: %s", LLM_PROVIDER, text)
        return None

    if not matched_link:
        return None
    return next((c for c in candidates if c["link"] == matched_link), None)


_EVENT_SUMMARY_SYSTEM_PROMPT = (
    "You are a gold (XAU/USD) and forex market analyst. Given a scheduled "
    "calendar event and a news article's title/preview text about it, "
    "respond with ONLY JSON, no prose, no markdown fences: "
    '{"summary": "<2-3 sentence summary of what was said/happened>", '
    '"market_reaction": "<what the article says about USD/gold/market '
    "reaction, or state that the preview text doesn't mention one>\"}."
)


async def summarize_event_article(event_name: str, article: dict) -> dict:
    """
    One-shot call turning a matched article's preview text into a short
    summary + market-reaction note for the Telegram notification. Falls back
    to a minimal summary (rather than raising) if Claude's response can't be
    parsed, since a found-but-unsummarized article is still useful to the user.
    """
    user_content = json.dumps(
        {
            "event": event_name,
            "article_title": article["title"],
            "article_preview": article.get("summary", "")[:_SUMMARY_MAX_CHARS],
        }
    )

    text = await _generate_text(
        system=_EVENT_SUMMARY_SYSTEM_PROMPT,
        user_content=user_content,
        model=SUMMARY_MODEL,
        max_tokens=EVENT_SUMMARY_MAX_TOKENS,
        timeout=_SUMMARY_TIMEOUT_SECONDS,
        json_mode=True,
    )

    try:
        parsed = json.loads(_strip_code_fence(text))
        return {
            "summary": parsed.get("summary", article["title"]),
            "market_reaction": parsed.get("market_reaction", "Not specified."),
        }
    except json.JSONDecodeError:
        logger.exception("Failed to parse %s event-summary response: %s", LLM_PROVIDER, text)
        return {"summary": article["title"], "market_reaction": "Not specified."}
