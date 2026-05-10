# Dockerfile for deploying the Novaex Telegram Bot as a Koyeb web service.
# The container exposes a FastAPI HTTP service on $PORT (Koyeb default 8000)
# and runs the Telegram bot via long-polling in the background.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps (build tools for cryptography wheels, plus curl for health checks)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Install python deps
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy backend source
COPY backend /app/backend

# Persistent data dir (proxies + accounts.csv).
# NOTE: Koyeb free web-service filesystems are ephemeral across deploys.
# Use /data and the DATA_DIR env var so users can mount a volume on paid tiers.
RUN mkdir -p /data
ENV DATA_DIR=/data

WORKDIR /app/backend

EXPOSE 8000

# Koyeb sets PORT; default to 8000 if absent. Bind 0.0.0.0.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
