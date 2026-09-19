import asyncio
import json
import logging
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import CallbackQuery, Update

from config import settings
import db
from handlers import (
    admin_books,
    admin_broadcast,
    admin_commands,
    admin_orders,
    admin_promo,
    admin_support,
    admin_texts,
    catalog,
    categories,
    payments,
    user,
)
from db.backups import backup_database, claim_backup_lease
from db.operational_events import OperationalEvent, record_event
from handlers.admin_orders import (
    inventory_notification_loop,
    new_orders_notify_loop,
    operational_alert_loop,
)
from handlers.user import order_support_notify_loop, support_escalation_loop
from storage import SQLiteStorage
from utils import log_event, setup_logger


WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
BACKGROUND_TASKS_KEY = web.AppKey("background_tasks", list)
RUNTIME_STARTED_KEY = web.AppKey("runtime_started", bool)

logger = setup_logger(__name__)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def create_bot(token: str | None, proxy_url: str | None = None) -> Bot:
    if proxy_url:
        return Bot(token=token, session=AiohttpSession(proxy=proxy_url))
    return Bot(token=token)


bot = create_bot(settings.BOT_TOKEN, settings.BOT_PROXY_URL)
storage = SQLiteStorage()
dp = Dispatcher(storage=storage)


class CallbackLoggerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery):
            logger.debug("Callback %s from %s", event.data, event.from_user.id)
        return await handler(event, data)


dp.callback_query.middleware(CallbackLoggerMiddleware())
dp.include_router(payments.router)
dp.include_router(admin_orders.router)
dp.include_router(admin_books.router)
dp.include_router(admin_broadcast.router)
dp.include_router(admin_promo.router)
dp.include_router(catalog.router)
dp.include_router(categories.router)
dp.include_router(admin_commands.router)
dp.include_router(admin_texts.router)
dp.include_router(admin_support.router)
dp.include_router(user.router)


async def initialize_runtime():
    await db.init_db()
    active_support_users = await db.get_all_support_active_user_ids()
    settings.support_pending_users = set(active_support_users)
    logger.info("Database initialized; active support dialogs: %s", len(active_support_users))


async def delivery_pii_cleanup_loop():
    while True:
        try:
            redacted = await db.redact_expired_delivery_pii()
            if redacted:
                log_event(
                    logger,
                    logging.INFO,
                    component="delivery_pii_cleanup",
                    event="delivery_pii_redacted",
                    outcome="succeeded",
                )
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                component="delivery_pii_cleanup",
                event="delivery_pii_redacted",
                outcome="failed",
                reason="database_error",
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(24 * 60 * 60)


async def verified_backup_loop():
    while True:
        try:
            if settings.BACKUP_DIR and await asyncio.to_thread(claim_backup_lease):
                await asyncio.to_thread(backup_database)
                log_event(
                    logger,
                    logging.INFO,
                    component="backup",
                    event="scheduled_backup",
                    outcome="succeeded",
                )
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                component="backup",
                event="scheduled_backup",
                outcome="failed",
                reason="backup_or_verification_failed",
                error_type=type(exc).__name__,
            )
        await asyncio.sleep(max(60, settings.BACKUP_INTERVAL_SECONDS))


def start_background_tasks():
    return [
        asyncio.create_task(new_orders_notify_loop(bot)),
        asyncio.create_task(operational_alert_loop(bot)),
        asyncio.create_task(inventory_notification_loop(bot)),
        asyncio.create_task(support_escalation_loop(bot)),
        asyncio.create_task(order_support_notify_loop(bot)),
        asyncio.create_task(delivery_pii_cleanup_loop()),
        asyncio.create_task(verified_backup_loop()),
    ]


async def stop_background_tasks(tasks):
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def close_runtime():
    await storage.close()
    await bot.session.close()
    logger.info("Bot stopped")


async def start_polling():
    tasks = []
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await initialize_runtime()
        tasks = start_background_tasks()
        logger.info("Bot started in polling mode")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        await stop_background_tasks(tasks)
        await close_runtime()


def webhook_configuration_error() -> str | None:
    parsed = urlparse(settings.WEBHOOK_URL)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path != WEBHOOK_PATH:
        return f"WEBHOOK_URL must be an HTTPS URL ending with {WEBHOOK_PATH}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", settings.WEBHOOK_SECRET):
        return "WEBHOOK_SECRET must contain 1-256 letters, digits, underscores, or hyphens"
    return None


async def webhook_update(request: web.Request):
    received_secret = request.headers.get(WEBHOOK_SECRET_HEADER, "")
    if not secrets.compare_digest(received_secret, settings.WEBHOOK_SECRET):
        log_event(
            logger, logging.WARNING, component="telegram_webhook", event="update",
            outcome="rejected", reason="invalid_secret"
        )
        return web.Response(status=403)
    try:
        payload = await request.json(loads=json.loads)
        update = Update.model_validate(payload, context={"bot": bot})
    except (TypeError, ValueError, json.JSONDecodeError):
        log_event(
            logger, logging.WARNING, component="telegram_webhook", event="update",
            outcome="rejected", reason="invalid_payload"
        )
        return web.Response(status=400)
    try:
        await dp.feed_update(bot, update)
    except Exception as exc:
        log_event(
            logger, logging.CRITICAL, component="telegram_webhook", event="dispatch",
            outcome="failed", reason="dispatch_error", error_type=type(exc).__name__
        )
        try:
            await record_event(
                OperationalEvent(
                    severity="critical", component="telegram_webhook", event="dispatch",
                    outcome="failed", reason="dispatch_error", error_type=type(exc).__name__,
                )
            )
        except Exception:
            logger.error("Could not persist Telegram webhook failure")
        return web.Response(status=503)
    log_event(
        logger, logging.INFO, component="telegram_webhook", event="dispatch",
        outcome="succeeded"
    )
    return web.Response()


async def webhook_health(_request: web.Request):
    return web.Response(text="ok")


async def webhook_startup(app: web.Application):
    try:
        await initialize_runtime()
        await bot.set_webhook(
            url=settings.WEBHOOK_URL,
            secret_token=settings.WEBHOOK_SECRET,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        app[BACKGROUND_TASKS_KEY] = start_background_tasks()
        app[RUNTIME_STARTED_KEY] = True
        logger.info("Bot started in webhook mode on %s", WEBHOOK_PATH)
    except Exception:
        await close_runtime()
        raise


async def webhook_shutdown(app: web.Application):
    if not app.get(RUNTIME_STARTED_KEY):
        return
    await stop_background_tasks(app.get(BACKGROUND_TASKS_KEY, []))
    await close_runtime()


def create_webhook_app(wsgi_app=None) -> web.Application:
    try:
        from aiohttp_wsgi import WSGIHandler
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "RUN_MODE=webhook requires aiohttp-wsgi; install dependencies with "
            "python -m pip install -r requirements.txt"
        ) from exc

    if wsgi_app is None:
        from server import app as wsgi_app

    wsgi_handler = WSGIHandler(wsgi_app)

    async def flask_fallback(request: web.Request):
        return await wsgi_handler(request)

    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, webhook_update)
    app.router.add_route("*", "/{path_info:.*}", flask_fallback)
    app.on_startup.append(webhook_startup)
    app.on_shutdown.append(webhook_shutdown)
    return app


def run_webhook(wsgi_app=None):
    app = create_webhook_app(wsgi_app)
    if settings.UNIX_SOCKET_PATH:
        socket_path = Path(settings.UNIX_SOCKET_PATH)
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        web.run_app(app, path=str(socket_path))
        return
    web.run_app(app, host=settings.HOST, port=settings.PORT)


def main(wsgi_app=None) -> int:
    if not settings.BOT_TOKEN:
        logger.error("BOT_TOKEN not found")
        return 1
    if settings.RUN_MODE == "polling":
        asyncio.run(start_polling())
        return 0
    if settings.RUN_MODE == "webhook":
        error = webhook_configuration_error()
        if error:
            logger.error("Invalid webhook configuration: %s", error)
            return 2
        if wsgi_app is None:
            run_webhook()
        else:
            run_webhook(wsgi_app)
        return 0
    logger.error("RUN_MODE must be polling or webhook")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
