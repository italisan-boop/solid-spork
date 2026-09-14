import asyncio
import logging

from aiogram import Bot, Dispatcher

import db
from config import settings
from handlers import user, admin_orders, catalog, categories, payments, admin_books, admin_broadcast, admin_promo, admin_commands
from handlers.admin_orders import new_orders_notify_loop
from handlers.user import support_escalation_loop
from storage import SQLiteStorage
from utils import setup_logger

# Настраиваем логгер
logger = setup_logger(__name__)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)

# Инициализация бота
bot = Bot(token=settings.BOT_TOKEN)
# FSM-состояния храним в SQLite, чтобы они переживали рестарт бота
storage = SQLiteStorage()
dp = Dispatcher(storage=storage)
from aiogram import BaseMiddleware


class CallbackLoggerMiddleware(BaseMiddleware):
    """Мидлварь для логирования callback-запросов."""
    
    async def __call__(self, handler, event, data):
        from aiogram.types import CallbackQuery
        
        if isinstance(event, CallbackQuery):
            logger.debug(f"📥 [CALLBACK] {event.data} от {event.from_user.id}")
        
        return await handler(event, data)


# Добавляем middleware
dp.callback_query.middleware(CallbackLoggerMiddleware())

# Подключаем роутеры
# ВАЖНО: payments должен быть ПЕРВЫМ, чтобы его callback-хендлеры срабатывали раньше
dp.include_router(payments.router)
dp.include_router(admin_orders.router)
dp.include_router(admin_books.router)
dp.include_router(admin_broadcast.router)
dp.include_router(admin_promo.router)
dp.include_router(catalog.router)
dp.include_router(categories.router)
dp.include_router(admin_commands.router)  # /drop_cache, /mass_broadcast — до user.router
dp.include_router(user.router)  # user.router должен быть ПОСЛЕДНИМ, т.к. он перехватывает всё

async def on_startup():
    """Инициализация при запуске бота."""
    await db.init_db()
    # Восстанавливаем активные диалоги с поддержкой из БД,
    # чтобы они переживали рестарт бота.
    active_support_users = await db.get_all_support_active_user_ids()
    settings.support_pending_users = set(active_support_users)
    logger.info(
        f"База данных инициализирована. "
        f"Активных диалогов поддержки: {len(active_support_users)}"
    )


async def on_shutdown():
    """Очистка при остановке бота."""
    await storage.close()
    await bot.session.close()
    logger.info("Бот остановлен")


async def start_polling():
    """Запуск бота в режиме polling."""
    await on_startup()
    logger.info("🚀 Бот запущен в режиме polling!")
    logger.info(f"🌐 WebApp URL: {settings.WEBAPP_URL}")
    logger.info(f"👥 Админы: {settings.ADMIN_IDS}")

    # Фоновая задача авто-уведомлений о новых заказах
    notifier_task = asyncio.create_task(new_orders_notify_loop(bot))
    # Фоновая задача эскалации неотвеченных сообщений поддержки
    escalation_task = asyncio.create_task(support_escalation_loop(bot))

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        escalation_task.cancel()
        notifier_task.cancel()
        await on_shutdown()


def main():
    """Точка входа приложения."""
    if not settings.BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не найден!")
        return
    
    logger.info(f"🔧 Режим: {settings.RUN_MODE}")
    asyncio.run(start_polling())

if __name__ == "__main__":
    main()