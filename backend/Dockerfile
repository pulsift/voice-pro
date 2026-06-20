# voice-pro backend (FastAPI + Pipecat + OpenAI Realtime) — Railway deploy
# Uses the official uv image (uv + Python 3.12) for fast, reproducible installs.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PORT=8000

# System build deps for native wheels (psycopg, hiredis, audio libs, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached layer) — project code not yet copied
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy app code and install the project itself
COPY . .
RUN uv sync --frozen --no-dev

# Run DB migrations, then serve.
# Single worker on purpose: the app runs an in-process campaign worker + holds
# realtime WebSocket call state, so multiple workers would duplicate/break it.
CMD ["sh", "-c", "uv run alembic upgrade head && exec uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
