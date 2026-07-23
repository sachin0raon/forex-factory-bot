# ForexFactory Telegram Bot

A Telegram bot that monitors the [ForexFactory](https://www.forexfactory.com/) calendar for high and medium-impact economic events, plus gold (XAU/USD) and USD macro news (FXStreet, InvestingLive), scores news sentiment with an LLM (Claude or Gemini), and notifies a list of authorized Telegram chats.

## Features

- **Automated Scraping**: Uses `cloudscraper` to bypass Cloudflare and fetch calendar events directly from the ForexFactory API.
- **Dynamic Notifications**: Subscribers are notified about new economic events automatically, formatted with emojis for impact levels (🔴 High, 🟠 Medium) and localized to IST.
- **Smart "Actual" Updates**: For scheduled future events, the bot dynamically schedules one-off check tasks (30s after the event occurs) to scrape the released "actual" metrics and notify subscribers.
- **Gold/USD News + Sentiment**: Polls free RSS feeds (FXStreet, InvestingLive) every `NEWS_POLL_MINUTES`, filters to gold/USD-relevant items, scores each with the LLM (bullish/bearish/neutral + rationale), and notifies subscribers.
- **On-Demand Outlook**: `/summary` combines recent scored news with upcoming calendar events into a single LLM-generated gold outlook.
- **Speech/Testimony Coverage**: Calendar events with no forecast value (e.g. "President Trump Speaks", "Fed Chairman Warsh Testifies") get an automatic FXStreet article search starting `ARTICLE_SEARCH_DELAY_MINUTES` after the event, retrying up to `ARTICLE_SEARCH_MAX_ATTEMPTS` times `ARTICLE_SEARCH_RETRY_MINUTES` apart. The LLM matches the event to the right article and summarizes it + the market reaction; the outcome (found or not) is sent to subscribers and persisted in Mongo.
- **Pluggable LLM Backend**: `LLM_PROVIDER` switches between Claude (Anthropic) and Gemini (Google) for all of the above — only the active provider's API key is required. Gemini's Flash/Flash-Lite models are free-tier as of 2026, a good option if you don't have an Anthropic subscription.
- **APScheduler Integration**: Fully manageable cron-based asynchronous task scheduler built right into the Telegram bot logic.
- **Access Control**: Only pre-authorized `chat_ids` can use the bot and receive notifications.

## Requirements

- Python 3.14+
- [uv](https://github.com/astral-sh/uv) (for ultra-fast dependency management)
- MongoDB instance (local or remote)
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- An LLM API key for news sentiment scoring, `/summary`, and event-article matching — either an Anthropic key from [console.anthropic.com](https://console.anthropic.com/), or a Gemini key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (free-tier eligible on Flash/Flash-Lite models as of 2026). Set `LLM_PROVIDER` accordingly.

## Environment Variables

Copy the `.env.example` file to `.env` and configure your settings:

```env
# Telegram Bot Token from @BotFather
TELEGRAM_BOT_TOKEN=your_bot_token_here

# MongoDB connection URI
MONGO_URI=mongodb://localhost:27017

# MongoDB database name
MONGO_DB_NAME=forexfactory

# Comma-separated list of authorized Telegram chat IDs
AUTHORIZED_CHAT_IDS=123456789,987654321

# Default cron expression for the scheduler (UTC)
DEFAULT_CRON=0 5 * * 1-5

# Which LLM backend powers news sentiment scoring, /summary, and event-article
# matching: "anthropic" or "gemini". Only the active provider's key is required.
LLM_PROVIDER=anthropic

# Claude API key (console.anthropic.com) – required if LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# Gemini API key (aistudio.google.com/apikey) – required if LLM_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here

# How often (minutes) to poll gold/USD news RSS feeds
NEWS_POLL_MINUTES=15

# Set to false to disable the recurring news poll entirely (e.g. if you only
# want ForexFactory calendar events). /fetchnews still works on-demand
# regardless of this flag.
NEWS_POLLING_ENABLED=true

# Cheap/fast model for per-item sentiment scoring (runs every poll).
# Leave unset to default per LLM_PROVIDER (claude-haiku-4-5-20251001 / gemini-2.5-flash-lite).
# SENTIMENT_MODEL=gemini-2.5-flash-lite

# Higher-quality model for the on-demand /summary outlook (low frequency).
# Leave unset to default per LLM_PROVIDER (claude-sonnet-5 / gemini-2.5-flash).
# SUMMARY_MODEL=gemini-2.5-flash

# Speech/testimony calendar events (no forecast value) get an FXStreet article
# search instead of an actual-value check: delay before the first attempt,
# max attempts, and the interval between retries.
ARTICLE_SEARCH_DELAY_MINUTES=10
ARTICLE_SEARCH_MAX_ATTEMPTS=3
ARTICLE_SEARCH_RETRY_MINUTES=30
```

## Running Locally

1. **Install dependencies**:
   Ensure `uv` is installed, then run:
   ```bash
   uv sync
   ```
2. **Start the Bot**:
   ```bash
   uv run python main.py
   ```

## Running with Docker

You can easily containerize the bot.

1. **Build the image**:
   ```bash
   docker build -t forexfactory-bot .
   ```
2. **Run the container**:
   ```bash
   docker run -d --name forexfactory-bot \
     --env-file .env \
     forexfactory-bot
   ```
   *(If you're running MongoDB locally on your host machine, you may need to use `mongodb://host.docker.internal:27017` in your `.env` for the container to access it.)*

## Commands

Once running, send the following commands to the bot:
- `/start` - Displays a welcome message and subscribes your chat to future notifications.
- `/cron` - View the current fetch schedule.
- `/cron <expression>` - Update the APScheduler fetch schedule dynamically (e.g., `/cron 0 4 * * *`).
- `/fetch` - Immediately fetch the latest events and send a notification.
- `/events` - Show all events for the current week stored in the database.
- `/news` - Show recent scored gold/USD news items.
- `/summary` - Generate an on-demand overall gold outlook from recent news + upcoming events.
- `/fetchnews` - Immediately poll news feeds, score new items, and notify.
