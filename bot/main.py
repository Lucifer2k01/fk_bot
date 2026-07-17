"""
Bot entry point — Aiogram dispatcher + APScheduler.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bot.config import settings
from bot.handlers import router
from bot.models import init_db
from bot.worker import track_all_products, keep_all_sessions_alive
from bot.notification import set_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
dp.include_router(router)

set_bot(bot)

scheduler = AsyncIOScheduler()

async def check_connectivity():
    """Check if we can reach Telegram API and Flipkart before starting."""
    import httpx
    import socket

    # Check DNS resolution
    try:
        socket.getaddrinfo("api.telegram.org", None)
        logger.info("[CONNECTIVITY] DNS resolution for api.telegram.org: OK")
    except socket.gaierror as e:
        logger.error(f"[CONNECTIVITY] DNS resolution failed: {e}")
        logger.error("[CONNECTIVITY] Check your network/DNS settings. If behind a corporate firewall, you may need a proxy.")
        return False

    # Check Telegram API reachability
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://api.telegram.org")
            logger.info(f"[CONNECTIVITY] Telegram API reachable: HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"[CONNECTIVITY] Cannot reach Telegram API: {e}")
        logger.error("[CONNECTIVITY] Possible causes: firewall, VPN, proxy required, or network down.")
        return False

    # Check Flipkart reachability
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://www.flipkart.com", follow_redirects=True)
            logger.info(f"[CONNECTIVITY] Flipkart reachable: HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"[CONNECTIVITY] Cannot reach Flipkart: {e}")
        logger.warning("[CONNECTIVITY] Flipkart may be blocked in your region. Auto-buy will not work.")

    return True

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[WEBHOOK] Initializing database...")
    init_db()
    logger.info("[WEBHOOK] Starting scheduler...")
    scheduler.add_job(
        track_all_products,
        IntervalTrigger(seconds=settings.check_interval_seconds),
        id="price_tracker",
        replace_existing=True,
        max_instances=1
    )
    scheduler.add_job(
        keep_all_sessions_alive,
        IntervalTrigger(minutes=3),
        id="session_keepalive",
        replace_existing=True,
        max_instances=1
    )
    scheduler.start()
    await bot.set_webhook(
        url=settings.webhook_url,
        secret_token=settings.webhook_secret
    )
    logger.info(f"[WEBHOOK] Webhook set to: {settings.webhook_url}")
    yield
    logger.info("[WEBHOOK] Shutting down...")
    await bot.delete_webhook()
    scheduler.shutdown()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def webhook(update: dict):
    from aiogram.types import Update
    try:
        telegram_update = Update.model_validate(update)
        await dp.feed_update(bot, telegram_update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"ok": False, "error": str(e)}

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0", "mode": "webhook"}

async def main_polling():
    logger.info("[POLLING] Starting FlashCart Bot...")
    logger.info(f"[POLLING] Bot token: {settings.bot_token[:20]}...")
    init_db()

    # Check connectivity before starting
    connected = await check_connectivity()
    if not connected:
        logger.error("[POLLING] Network connectivity check failed. Bot may not work correctly.")
        logger.error("[POLLING] Retrying in 10 seconds...")
        await asyncio.sleep(10)
        connected = await check_connectivity()
        if not connected:
            logger.error("[POLLING] Still no connectivity. Starting anyway, but expect failures.")

    scheduler.add_job(
        track_all_products,
        IntervalTrigger(seconds=settings.check_interval_seconds),
        id="price_tracker",
        replace_existing=True,
        max_instances=1
    )
    scheduler.add_job(
        keep_all_sessions_alive,
        IntervalTrigger(minutes=3),
        id="session_keepalive",
        replace_existing=True,
        max_instances=1
    )
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("[POLLING] Bot is running! Send /start in Telegram.")

    try:
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"[POLLING] Error: {e}")
        raise
    finally:
        logger.info("[POLLING] Shutting down...")
        scheduler.shutdown()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main_polling())