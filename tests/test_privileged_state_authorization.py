from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from handlers.admin_books import AdminBooksMiddleware
from handlers.admin_broadcast import BroadcastState, process_message
from handlers.admin_support import deferred_admin_reply
from handlers.payments import process_card, process_stars_rate
from handlers.user import skip_description
from states import SupportReplyState


class PrivilegedStateAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def _message(self, user_id=999, text="secret"):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            text=text,
            answer=AsyncMock(),
        )

    def _state(self, data=None):
        return SimpleNamespace(
            clear=AsyncMock(),
            get_data=AsyncMock(return_value=data or {}),
            update_data=AsyncMock(),
            set_state=AsyncMock(),
        )

    async def test_non_admin_cannot_write_card_setting_from_forged_state(self):
        message = self._message(text="4111111111111111")
        state = self._state()
        with patch("handlers.payments.db.set_payment_setting", new_callable=AsyncMock) as set_setting:
            await process_card(message, state)

        set_setting.assert_not_awaited()
        state.clear.assert_awaited_once()
        self.assertIn("Нет прав", message.answer.await_args.args[0])

    async def test_non_admin_cannot_write_stars_rate_from_forged_state(self):
        message = self._message(text="2")
        state = self._state()
        with patch("handlers.payments.db.set_stars_setting", new_callable=AsyncMock) as set_setting:
            await process_stars_rate(message, state)

        set_setting.assert_not_awaited()
        state.clear.assert_awaited_once()

    async def test_non_admin_cannot_create_broadcast_draft_from_forged_state(self):
        message = self._message(text="<b>broadcast</b>")
        state = self._state()
        with patch("handlers.admin_broadcast.get_all_users", new_callable=AsyncMock) as users:
            await process_message(message, state)

        users.assert_not_awaited()
        state.update_data.assert_not_awaited()
        state.clear.assert_awaited_once()

    async def test_non_admin_cannot_send_deferred_support_reply(self):
        message = self._message(text="reply")
        state = self._state({"reply_user_id": 101})
        with patch("handlers.admin_support._send_admin_reply", new_callable=AsyncMock) as send_reply:
            await deferred_admin_reply(message, state)

        send_reply.assert_not_awaited()
        state.clear.assert_awaited_once()

    async def test_non_admin_cannot_use_legacy_skip_description_callback(self):
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=999),
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )
        state = self._state()
        with patch("handlers.user.db.add_book", new_callable=AsyncMock) as add_book:
            await skip_description(callback, state)

        add_book.assert_not_awaited()
        state.clear.assert_awaited_once()
        callback.answer.assert_awaited_once_with("❌ Нет прав", show_alert=True)

    async def test_admin_books_middleware_stops_non_admin_before_handler(self):
        event = SimpleNamespace(from_user=SimpleNamespace(id=999))
        state = self._state()
        handler = AsyncMock()

        result = await AdminBooksMiddleware()(handler, event, {"state": state})

        self.assertIsNone(result)
        handler.assert_not_awaited()
        state.clear.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
