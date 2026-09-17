from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from controlplane.settings import PlatformBotSettings


async def handle_start(message: Message, settings: PlatformBotSettings) -> None:
    chat = getattr(message, "chat", None)
    user = getattr(message, "from_user", None)
    if chat is None or chat.type != "private":
        await message.answer("Откройте Platform Console в личном чате с ботом.")
        return
    if user is None or user.id not in settings.admin_telegram_ids:
        await message.answer("Доступ запрещён.")
        return
    keyboard = InlineKeyboardBuilder()
    keyboard.button(
        text="Открыть Platform Console",
        web_app=WebAppInfo(url=settings.console_url),
    )
    await message.answer(
        "Откройте консоль управления магазинами.",
        reply_markup=keyboard.as_markup(),
    )


def create_platform_dispatcher(settings: PlatformBotSettings) -> Dispatcher:
    router = Router(name="platform")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await handle_start(message, settings)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


async def run_polling(settings: PlatformBotSettings) -> None:
    dispatcher = create_platform_dispatcher(settings)
    session = AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else None
    bot = (
        Bot(token=settings.bot_token, session=session)
        if session
        else Bot(token=settings.bot_token)
    )
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        await bot.session.close()


def main() -> int:
    try:
        settings = PlatformBotSettings.from_environment()
    except ValueError as exc:
        print(f"Platform bot configuration error: {exc}")
        return 1
    asyncio.run(run_polling(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
