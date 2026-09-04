import os
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: Message):
    print(f"✅ ПОЛУЧЕН /start от {message.from_user.id}")
    await message.answer("✅ Бот работает! Я получил команду /start")

@dp.message()
async def any_message(message: Message):
    print(f" Получено сообщение: {message.text}")
    await message.answer(f"Получил: {message.text}")

async def main():
    print("🤖 Запуск тестового бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())