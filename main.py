import asyncio
import logging

from aiogram import Bot, Dispatcher

import db
from config import settings
from handlers import user, admin_orders, catalog, categories, payments, admin_books, admin_broadcast
from utils import setup_logger

# Настраиваем логгер
logger = setup_logger(__name__)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)

# Инициализация бота
bot = Bot(token=settings.BOT_TOKEN)
dp = Dispatcher()
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
dp.include_router(catalog.router)
dp.include_router(categories.router)
dp.include_router(user.router)  # user.router должен быть ПОСЛЕДНИМ, т.к. он перехватывает всё

async def on_startup():
    """Инициализация при запуске бота."""
    await db.init_db()
    logger.info("База данных инициализирована")


async def on_shutdown():
    """Очистка при остановке бота."""
    await bot.session.close()
    logger.info("Бот остановлен")


async def start_polling():
    """Запуск бота в режиме polling."""
    await on_startup()
    logger.info("🚀 Бот запущен в режиме polling!")
    logger.info(f"🌐 WebApp URL: {settings.WEBAPP_URL}")
    logger.info(f"👥 Админы: {settings.ADMIN_IDS}")
    
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
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