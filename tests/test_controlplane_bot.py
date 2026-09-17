from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from controlplane.bot import handle_start, run_polling
from controlplane.settings import PlatformBotSettings


TOKEN = "123456:platform-bot-token-for-tests"
URL = "https://platform.example.test"
SETTINGS = PlatformBotSettings(
    bot_token=TOKEN,
    admin_telegram_ids=frozenset({101}),
    console_url=URL,
    proxy_url=None,
)


def message(*, user_id: int | None, chat_type: str = "private"):
    return SimpleNamespace(
        chat=SimpleNamespace(type=chat_type),
        from_user=None if user_id is None else SimpleNamespace(id=user_id),
        answer=AsyncMock(),
    )


class PlatformBotSettingsTests(unittest.TestCase):
    def test_loads_valid_console_url(self):
        with patch.dict(os.environ, {
            "PLATFORM_BOT_TOKEN": TOKEN,
            "PLATFORM_ADMIN_TELEGRAM_IDS": "101, 202",
            "PLATFORM_CONSOLE_URL": "https://platform.example.test/",
        }, clear=True):
            settings = PlatformBotSettings.from_environment()

        self.assertEqual("https://platform.example.test", settings.console_url)
        self.assertEqual(frozenset({101, 202}), settings.admin_telegram_ids)
        self.assertIsNone(settings.proxy_url)

    def test_loads_authenticated_http_proxy(self):
        proxy_url = "http://proxy-user:proxy-password@203.0.113.10:3128"
        with patch.dict(os.environ, {
            "PLATFORM_BOT_TOKEN": TOKEN,
            "PLATFORM_ADMIN_TELEGRAM_IDS": "101",
            "PLATFORM_CONSOLE_URL": URL,
            "PLATFORM_BOT_PROXY_URL": proxy_url,
        }, clear=True):
            settings = PlatformBotSettings.from_environment()

        self.assertEqual(proxy_url, settings.proxy_url)

    def test_rejects_invalid_proxy_urls(self):
        for proxy_url in (
            "https://user:password@203.0.113.10:3128",
            "http://203.0.113.10:3128",
            "http://user@203.0.113.10:3128",
            "http://user:password@203.0.113.10",
            "http://user:password@203.0.113.10:invalid",
            "http://user:password@203.0.113.10:3128/path",
            "http://user:password@203.0.113.10:3128?x=1",
        ):
            with self.subTest(proxy_url=proxy_url), patch.dict(os.environ, {
                "PLATFORM_BOT_TOKEN": TOKEN,
                "PLATFORM_ADMIN_TELEGRAM_IDS": "101",
                "PLATFORM_CONSOLE_URL": URL,
                "PLATFORM_BOT_PROXY_URL": proxy_url,
            }, clear=True):
                with self.assertRaises(ValueError):
                    PlatformBotSettings.from_environment()

    def test_rejects_invalid_console_urls(self):
        for url in (
            "",
            "http://platform.example.test",
            "https://",
            "https://user@platform.example.test",
            "https://platform.example.test/console",
            "https://platform.example.test/?next=x",
            "https://platform.example.test/#fragment",
            "https://platform.example.test bad",
            "https://platform.example.test:invalid",
        ):
            with self.subTest(url=url), patch.dict(os.environ, {
                "PLATFORM_BOT_TOKEN": TOKEN,
                "PLATFORM_ADMIN_TELEGRAM_IDS": "101",
                "PLATFORM_CONSOLE_URL": url,
            }, clear=True):
                with self.assertRaises(ValueError):
                    PlatformBotSettings.from_environment()


class PlatformBotHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_start_returns_console_web_app_button(self):
        incoming = message(user_id=101)

        await handle_start(incoming, SETTINGS)

        markup = incoming.answer.await_args.kwargs["reply_markup"]
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertEqual(1, len(buttons))
        self.assertEqual("Открыть Platform Console", buttons[0].text)
        self.assertEqual(URL, buttons[0].web_app.url)

    async def test_non_admin_does_not_receive_console_url(self):
        incoming = message(user_id=999)

        await handle_start(incoming, SETTINGS)

        self.assertEqual("Доступ запрещён.", incoming.answer.await_args.args[0])
        self.assertNotIn("reply_markup", incoming.answer.await_args.kwargs)
        self.assertNotIn(URL, str(incoming.answer.await_args))

    async def test_group_start_requires_private_chat(self):
        incoming = message(user_id=101, chat_type="group")

        await handle_start(incoming, SETTINGS)

        self.assertEqual(
            "Откройте Platform Console в личном чате с ботом.",
            incoming.answer.await_args.args[0],
        )
        self.assertNotIn("reply_markup", incoming.answer.await_args.kwargs)

    async def test_polling_removes_stale_webhook_and_closes_session(self):
        dispatcher = MagicMock()
        dispatcher.resolve_used_update_types.return_value = ["message"]
        dispatcher.start_polling = AsyncMock()
        bot = MagicMock()
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        with (
            patch("controlplane.bot.create_platform_dispatcher", return_value=dispatcher),
            patch("controlplane.bot.Bot", return_value=bot),
        ):
            await run_polling(SETTINGS)

        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        dispatcher.start_polling.assert_awaited_once_with(
            bot,
            allowed_updates=["message"],
            close_bot_session=False,
        )
        bot.session.close.assert_awaited_once()

    async def test_polling_uses_proxy_only_when_configured(self):
        proxy_url = "http://proxy-user:proxy-password@203.0.113.10:3128"
        settings = PlatformBotSettings(
            bot_token=TOKEN,
            admin_telegram_ids=frozenset({101}),
            console_url=URL,
            proxy_url=proxy_url,
        )
        dispatcher = MagicMock()
        dispatcher.resolve_used_update_types.return_value = ["message"]
        dispatcher.start_polling = AsyncMock()
        session = MagicMock()
        bot = MagicMock()
        bot.delete_webhook = AsyncMock()
        bot.session.close = AsyncMock()
        with (
            patch("controlplane.bot.create_platform_dispatcher", return_value=dispatcher),
            patch("controlplane.bot.AiohttpSession", return_value=session) as session_factory,
            patch("controlplane.bot.Bot", return_value=bot) as bot_factory,
        ):
            await run_polling(settings)

        session_factory.assert_called_once_with(proxy=proxy_url)
        bot_factory.assert_called_once_with(token=TOKEN, session=session)


class PlatformConsoleHtmlTests(unittest.TestCase):
    def test_telegram_sdk_loads_before_console_initialization(self):
        page = (Path(__file__).parent.parent / "controlplane" / "platform_index.html").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            page.index("telegram-web-app.js"),
            page.index("const tg = window.Telegram?.WebApp"),
        )


if __name__ == "__main__":
    unittest.main()
