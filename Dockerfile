FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Install minimal system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY backend/requirements.txt .

RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY backend ./backend

# Persistent storage path
RUN mkdir -p /data
ENV DATA_DIR=/data

WORKDIR /app/backend

EXPOSE 8000

# Run FastAPI
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT}
