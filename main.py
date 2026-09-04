import asyncio
import logging

import colorlog
from aiogram import Bot, Dispatcher

import db
from config import BOT_TOKEN, WEBAPP_URL, RUN_MODE, ADMIN_IDS
from handlers import user, admin_orders, catalog, categories, payments

# Цветные логи
color_formatter = colorlog.ColoredFormatter(
    "%(log_color)s%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
    log_colors={'DEBUG': 'cyan', 'INFO': 'green', 'WARNING': 'yellow', 'ERROR': 'red', 'CRITICAL': 'bold_red'}
)
console_handler = logging.StreamHandler()
console_handler.setFormatter(color_formatter)
logger = logging.getLogger(__name__)
logger.addHandler(console_handler)
logger.setLevel(logging.INFO)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
from aiogram import BaseMiddleware

class CallbackLoggerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        from aiogram.types import CallbackQuery
        if isinstance(event, CallbackQuery):
            print(f"📥 [CALLBACK] {event.data} от {event.from_user.id}")
        return await handler(event, data)

# Добавляем middleware
dp.callback_query.middleware(CallbackLoggerMiddleware())

# Подключаем роутеры
# ВАЖНО: payments должен быть ПЕРВЫМ, чтобы его callback-хендлеры срабатывали раньше
dp.include_router(payments.router)
dp.include_router(admin_orders.router)
dp.include_router(catalog.router)
dp.include_router(categories.router)
dp.include_router(user.router)  # user.router должен быть ПОСЛЕДНИМ, т.к. он перехватывает всё

async def on_startup():
    await db.init_db()
    logger.info("База данных инициализирована")

async def on_shutdown():
    await bot.session.close()

async def start_polling():
    await on_startup()
    logger.info(" Бот запущен в режиме polling!")
    logger.info(f"🌐 WebApp URL: {WEBAPP_URL}")
    logger.info(f" Админы: {ADMIN_IDS}")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await on_shutdown()

def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не найден!")
        return
    logger.info(f"Режим: {RUN_MODE}")
    asyncio.run(start_polling())

if __name__ == "__main__":
    main()