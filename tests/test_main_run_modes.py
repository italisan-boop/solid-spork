import os
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config.settings import Settings
import main
import server


class RunModeDispatchTests(TestCase):
    def test_bot_proxy_url_is_optional_and_strictly_validated(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(Settings().BOT_PROXY_URL)

        proxy_url = "http://proxy-user:proxy-password@203.0.113.10:3128/"
        with patch.dict(os.environ, {"BOT_PROXY_URL": proxy_url}, clear=True):
            self.assertEqual(
                "http://proxy-user:proxy-password@203.0.113.10:3128",
                Settings().BOT_PROXY_URL,
            )

        for invalid_proxy_url in (
            "https://proxy-user:proxy-password@203.0.113.10:3128",
            "http://203.0.113.10:3128",
            "http://proxy-user@203.0.113.10:3128",
            "http://proxy-user:proxy-password@203.0.113.10",
            "http://proxy-user:proxy-password@203.0.113.10:99999",
            "http://proxy-user:proxy-password@203.0.113.10/proxy",
            "http://proxy-user:proxy-password@203.0.113.10:3128?mode=test",
            "http://proxy user:proxy-password@203.0.113.10:3128",
        ):
            with self.subTest(proxy_url=invalid_proxy_url), patch.dict(
                os.environ, {"BOT_PROXY_URL": invalid_proxy_url}, clear=True
            ):
                with self.assertRaisesRegex(ValueError, "BOT_PROXY_URL"):
                    Settings()

    def test_create_bot_uses_proxy_only_when_configured(self):
        proxy_url = "http://proxy-user:proxy-password@203.0.113.10:3128"
        proxied_session = object()
        proxied_bot = object()
        with (
            patch("main.AiohttpSession", return_value=proxied_session) as session_factory,
            patch("main.Bot", return_value=proxied_bot) as bot_factory,
        ):
            self.assertIs(proxied_bot, main.create_bot("123456:test-token", proxy_url))
        session_factory.assert_called_once_with(proxy=proxy_url)
        bot_factory.assert_called_once_with(
            token="123456:test-token", session=proxied_session
        )

        direct_bot = object()
        with (
            patch("main.AiohttpSession") as session_factory,
            patch("main.Bot", return_value=direct_bot) as bot_factory,
        ):
            self.assertIs(direct_bot, main.create_bot("123456:test-token"))
        session_factory.assert_not_called()
        bot_factory.assert_called_once_with(token="123456:test-token")

    def test_webhook_configuration_requires_explicit_secure_values(self):
        with patch.multiple(
            main.settings,
            WEBHOOK_URL="https://bot.example.com/webhook",
            WEBHOOK_SECRET="valid_secret-123",
        ):
            self.assertIsNone(main.webhook_configuration_error())
        with patch.multiple(main.settings, WEBHOOK_URL="http://bot.example.com/webhook"):
            self.assertIsNotNone(main.webhook_configuration_error())
        with patch.multiple(main.settings, WEBHOOK_URL="https://bot.example.com/other"):
            self.assertIsNotNone(main.webhook_configuration_error())

    def test_main_selects_only_requested_transport(self):
        polling_coroutine = object()
        with (
            patch.object(main.settings, "BOT_TOKEN", "123456:test-token"),
            patch.object(main.settings, "RUN_MODE", "polling"),
            patch("main.start_polling", new=MagicMock(return_value=polling_coroutine)),
            patch("main.asyncio.run") as run,
            patch("main.run_webhook") as run_webhook,
        ):
            self.assertEqual(0, main.main())
        run.assert_called_once_with(polling_coroutine)
        run_webhook.assert_not_called()

        with (
            patch.object(main.settings, "BOT_TOKEN", "123456:test-token"),
            patch.object(main.settings, "RUN_MODE", "webhook"),
            patch("main.webhook_configuration_error", return_value=None),
            patch("main.run_webhook") as run_webhook,
            patch("main.asyncio.run") as run,
        ):
            self.assertEqual(0, main.main())
        run_webhook.assert_called_once()
        run.assert_not_called()

    def test_main_rejects_unknown_or_incomplete_modes(self):
        with (
            patch.object(main.settings, "BOT_TOKEN", "123456:test-token"),
            patch.object(main.settings, "RUN_MODE", "invalid"),
            patch("main.asyncio.run") as run,
            patch("main.run_webhook") as run_webhook,
        ):
            self.assertEqual(2, main.main())
        run.assert_not_called()
        run_webhook.assert_not_called()

        with (
            patch.object(main.settings, "BOT_TOKEN", "123456:test-token"),
            patch.object(main.settings, "RUN_MODE", "webhook"),
            patch("main.webhook_configuration_error", return_value="missing URL"),
            patch("main.run_webhook") as run_webhook,
        ):
            self.assertEqual(2, main.main())
        run_webhook.assert_not_called()


class HttpLaunchTests(TestCase):
    def test_standalone_flask_uses_shared_host_port_and_debug_setting(self):
        with (
            patch.object(server.settings, "HOST", "127.0.0.1"),
            patch.object(server.settings, "PORT", 9123),
            patch.object(server.settings, "FLASK_DEBUG", False),
            patch.object(server.settings, "SUPPRESS_LOOPBACK_SUCCESS_ACCESS_LOGS", False),
            patch.object(server.app, "run") as run,
        ):
            server.run_server()
        run.assert_called_once_with(
            host="127.0.0.1",
            port=9123,
            debug=False,
            use_reloader=False,
        )

    def test_standalone_flask_uses_quiet_handler_when_enabled(self):
        with (
            patch.object(server.settings, "SUPPRESS_LOOPBACK_SUCCESS_ACCESS_LOGS", True),
            patch.object(server.app, "run") as run,
        ):
            server.run_server()

        self.assertIs(
            server._LoopbackSuccessQuietRequestHandler,
            run.call_args.kwargs["request_handler"],
        )

    def test_loopback_success_access_log_filter_preserves_errors_and_remote_requests(self):
        with patch.object(server.settings, "SUPPRESS_LOOPBACK_SUCCESS_ACCESS_LOGS", True):
            self.assertTrue(server._should_suppress_loopback_success_access_log("127.0.0.1", 200))
            self.assertTrue(server._should_suppress_loopback_success_access_log("::1", "304"))
            self.assertFalse(server._should_suppress_loopback_success_access_log("127.0.0.1", 404))
            self.assertFalse(server._should_suppress_loopback_success_access_log("203.0.113.1", 200))
        with patch.object(server.settings, "SUPPRESS_LOOPBACK_SUCCESS_ACCESS_LOGS", False):
            self.assertFalse(server._should_suppress_loopback_success_access_log("127.0.0.1", 200))


class UnifiedWebhookAppTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch("main.webhook_startup", new_callable=AsyncMock), patch(
            "main.webhook_shutdown", new_callable=AsyncMock
        ):
            self.app = main.create_webhook_app()
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_flask_health_and_catalog_are_mounted_under_webhook_server(self):
        health = await self.client.get("/health")
        self.assertEqual(200, health.status)
        self.assertEqual({"status": "ok"}, await health.json())

        root = await self.client.get("/")
        self.assertEqual(200, root.status)
        self.assertIn("Семена Знаний", await root.text())

        books = await self.client.get("/api/books")
        self.assertEqual(200, books.status)
        self.assertIn("books", await books.json())

    async def test_webhook_route_wins_over_flask_fallback(self):
        with (
            patch.object(main.settings, "WEBHOOK_SECRET", "valid_secret"),
            patch.object(main.dp, "feed_update", new_callable=AsyncMock) as feed_update,
        ):
            blocked = await self.client.post("/webhook", json={"update_id": 1})
            self.assertEqual(403, blocked.status)
            accepted = await self.client.post(
                "/webhook",
                json={"update_id": 2},
                headers={main.WEBHOOK_SECRET_HEADER: "valid_secret"},
            )
            self.assertEqual(200, accepted.status)
        feed_update.assert_awaited_once()



    async def test_polling_removes_webhook_before_full_dispatcher(self):
        calls = []

        async def delete_webhook(**kwargs):
            calls.append(("delete", kwargs))

        async def initialize():
            calls.append(("initialize", {}))

        async def start_polling(*_args, **kwargs):
            calls.append(("poll", kwargs))

        with (
            patch.object(main.bot, "delete_webhook", side_effect=delete_webhook),
            patch("main.initialize_runtime", side_effect=initialize),
            patch("main.start_background_tasks", return_value=[]),
            patch.object(main.dp, "start_polling", side_effect=start_polling),
            patch.object(main.dp, "resolve_used_update_types", return_value=["message"]),
            patch("main.close_runtime", new_callable=AsyncMock),
        ):
            await main.start_polling()

        self.assertEqual("delete", calls[0][0])
        self.assertEqual({"drop_pending_updates": False}, calls[0][1])
        self.assertEqual("poll", calls[-1][0])
        self.assertEqual(["message"], calls[-1][1]["allowed_updates"])
        self.assertFalse(calls[-1][1]["close_bot_session"])

    async def test_webhook_startup_registers_full_dispatcher_and_shutdown_keeps_webhook(self):
        app = web.Application()
        with (
            patch.object(main.settings, "WEBHOOK_URL", "https://bot.example.com/webhook"),
            patch.object(main.settings, "WEBHOOK_SECRET", "valid_secret"),
            patch("main.initialize_runtime", new_callable=AsyncMock) as initialize,
            patch("main.start_background_tasks", return_value=[]),
            patch.object(main.bot, "set_webhook", new_callable=AsyncMock) as set_webhook,
            patch.object(main.bot, "delete_webhook", new_callable=AsyncMock) as delete_webhook,
            patch.object(main.dp, "resolve_used_update_types", return_value=["message"]),
            patch("main.close_runtime", new_callable=AsyncMock) as close_runtime,
        ):
            await main.webhook_startup(app)
            await main.webhook_shutdown(app)

        initialize.assert_awaited_once()
        set_webhook.assert_awaited_once_with(
            url="https://bot.example.com/webhook",
            secret_token="valid_secret",
            allowed_updates=["message"],
            drop_pending_updates=False,
        )
        close_runtime.assert_awaited_once()
        delete_webhook.assert_not_awaited()


    async def test_webhook_rejects_wrong_secret_before_dispatch(self):
        with (
            patch.object(main.settings, "WEBHOOK_SECRET", "valid_secret"),
            patch.object(main.dp, "feed_update", new_callable=AsyncMock) as feed_update,
        ):
            response = await self.client.post("/webhook", json={"update_id": 1})
            self.assertEqual(403, response.status)
            response = await self.client.post(
                "/webhook",
                json={"update_id": 1},
                headers={main.WEBHOOK_SECRET_HEADER: "wrong"},
            )
            self.assertEqual(403, response.status)
        feed_update.assert_not_awaited()

    async def test_webhook_dispatches_valid_update_and_rejects_bad_json(self):
        with (
            patch.object(main.settings, "WEBHOOK_SECRET", "valid_secret"),
            patch.object(main.dp, "feed_update", new_callable=AsyncMock) as feed_update,
        ):
            response = await self.client.post(
                "/webhook",
                json={"update_id": 1},
                headers={main.WEBHOOK_SECRET_HEADER: "valid_secret"},
            )
            self.assertEqual(200, response.status)
            feed_update.assert_awaited_once()
            self.assertIs(main.bot, feed_update.await_args.args[0])

            response = await self.client.post(
                "/webhook",
                data="not-json",
                headers={main.WEBHOOK_SECRET_HEADER: "valid_secret"},
            )
            self.assertEqual(400, response.status)
            self.assertEqual(1, feed_update.await_count)


if __name__ == "__main__":
    import unittest
    unittest.main()
