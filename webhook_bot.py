import os
import json
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, WebAppInfo, Update
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import db

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://example.com")
WEBHOOK_URL = f"{WEBAPP_URL}/webhook"  # URL для webhook

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в переменных окружения!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    # Сохраняем пользователя в БД
    async with aiosqlite.connect(DB_NAME) as database:
        await database.execute(
            "INSERT OR IGNORE INTO users (user_id, user_name) VALUES (?, ?)",
            (message.from_user.id, message.from_user.username or message.from_user.first_name)
        )
        await database.commit()
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🌱 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))
    builder.adjust(1)

    await message.answer(
        "🌿 Добро пожаловать!\nНажми кнопку ниже:",
        reply_markup=builder.as_markup()
    )


@dp.message(F.web_app_data)
async def handle_webapp(message: Message):
    print(f"✅ ПОЛУЧЕНЫ ДАННЫЕ: {message.web_app_data.data}")
    data = json.loads(message.web_app_data.data)
    await message.answer(f"Получил заказ: {data}")


async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    print(f"✅ Webhook установлен: {WEBHOOK_URL}")


async def on_shutdown(app):
    await bot.delete_webhook()
    print("✅ Webhook удалён")


async def handle_update(request):
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return web.Response()


app = web.Application()
app.router.add_post('/webhook', handle_update)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == '__main__':
    print(" Запуск webhook бота...")
    web.run_app(app, host='0.0.0.0', port=8081)