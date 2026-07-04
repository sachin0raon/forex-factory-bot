# Use Python 3.13 slim image
FROM python:3.13-slim

# Install uv from the official Astral image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

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
