# Use Python 3.14 slim image (matches pyproject.toml requires-python = ">=3.14")
FROM python:3.14-slim

# Install uv from the official Astral image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install git — required by uv to fetch the cloudscraper git source dependency
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency definitions
COPY pyproject.toml uv.lock ./

# Install dependencies using uv
# --frozen ensures uv.lock is not updated during install
# --no-dev skips development dependencies
RUN uv sync --frozen --no-dev

# Copy the rest of the application code
COPY . .

# Set environment variables (optional, to avoid python buffering)
ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

# Run the bot
CMD ["uv", "run", "python", "main.py"]
