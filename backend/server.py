"""
FastAPI web service that satisfies Koyeb's web-service contract while
running the Telegram bot in the background using long-polling.

Endpoints:
  GET /api/         -> service info (also pingable for Koyeb health)
  GET /api/health   -> health probe
  GET /api/stats    -> simple stats (account count, proxy count, bot status)
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

# Bot wiring (after dotenv so env vars are visible)
from bot import (  # noqa: E402
    build_application,
    CSV_PATH,
    PROXY_PATH,
    DATA_DIR,
)
from registrar import count_csv_rows  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start telegram bot polling in the background
    application = build_application()
    app.state.bot_app = application
    if application:
        try:
            await application.initialize()
            await application.start()
            await application.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=None,
            )
            logger.info("Telegram bot started (polling).")
        except Exception as e:
            logger.exception("Failed to start Telegram bot: %s", e)
            app.state.bot_app = None
    else:
        logger.warning("Telegram bot disabled (no TELEGRAM_BOT_TOKEN).")

    try:
        yield
    finally:
        if app.state.bot_app:
            try:
                await app.state.bot_app.updater.stop()
                await app.state.bot_app.stop()
                await app.state.bot_app.shutdown()
            except Exception:
                pass


app = FastAPI(lifespan=lifespan, title="Novaex AI Telegram Bot")

api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {
        "service": "novaex-telegram-bot",
        "status": "ok",
        "bot_enabled": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
    }


@api_router.get("/health")
async def health():
    return {"status": "ok"}


@api_router.get("/stats")
async def stats():
    proxies = 0
    if PROXY_PATH.exists():
        with open(PROXY_PATH) as f:
            proxies = sum(1 for line in f if line.strip() and not line.startswith("#"))
    return {
        "accounts": count_csv_rows(str(CSV_PATH)),
        "proxies": proxies,
        "data_dir": str(DATA_DIR),
        "bot_enabled": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
    }


app.include_router(api_router)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
