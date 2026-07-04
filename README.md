# ForexFactory Telegram Bot

A Telegram bot that monitors the [ForexFactory](https://www.forexfactory.com/) calendar for high and medium-impact economic events, saving them to MongoDB, and automatically notifying a list of authorized Telegram chats.

## Features

- **Automated Scraping**: Uses `cloudscraper` to bypass Cloudflare and fetch calendar events directly from the ForexFactory API.
- **Dynamic Notifications**: Subscribers are notified about new economic events automatically, formatted with emojis for impact levels (🔴 High, 🟠 Medium) and localized to IST.
- **Smart "Actual" Updates**: For scheduled future events, the bot dynamically schedules one-off check tasks (30s after the event occurs) to scrape the released "actual" metrics and notify subscribers.
- **APScheduler Integration**: Fully manageable cron-based asynchronous task scheduler built right into the Telegram bot logic.
- **Access Control**: Only pre-authorized `chat_ids` can use the bot and receive notifications.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (for ultra-fast dependency management)
- MongoDB instance (local or remote)
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)

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
