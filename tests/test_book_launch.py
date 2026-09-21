import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import settings
from handlers.user import (
    _parse_start_payload,
    _webapp_url_for_book,
    cmd_start_with_ref,
    ref_accept_callback,
    ref_decline_callback,
)


class BookLaunchTests(unittest.IsolatedAsyncioTestCase):
    def message(self, payload: str):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=77, username="reader", first_name="Reader"),
            text=f"/start {payload}",
            answer=AsyncMock(),
        )

    def callback(self, user_id=77):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )

    def state(self, data=None):
        return SimpleNamespace(
            get_data=AsyncMock(return_value=data or {}),
            update_data=AsyncMock(),
            set_state=AsyncMock(),
            clear=AsyncMock(),
        )

    def test_start_payload_parser_preserves_existing_links(self):
        self.assertEqual(("ref_123", 7), _parse_start_payload("ref_123_book_7"))
        self.assertEqual(("ref_123", None), _parse_start_payload("ref_123"))
        self.assertEqual(("c_september", None), _parse_start_payload("c_september"))
        invalid = (
            "ref_0_book_7",
            "ref_123_book_0",
            "ref_123_book_7_extra",
            "ref_123_book_-7",
            "ref_abc_book_7",
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual((payload, None), _parse_start_payload(payload))
        self.assertEqual(("", None), _parse_start_payload("ref_123_book_9223372036854775808"))
        self.assertEqual(("", None), _parse_start_payload("x" * 65))

    def test_target_book_url_preserves_existing_query_and_fragment(self):
        with patch.object(settings, "WEBAPP_URL", "https://shop.example.test/store?source=menu&book=old#catalog"):
            self.assertEqual(
                "https://shop.example.test/store?source=menu&book=7#catalog",
                _webapp_url_for_book(7),
            )
            self.assertEqual(
                "https://shop.example.test/store?source=menu&book=old#catalog",
                _webapp_url_for_book(),
            )

    async def test_composite_start_keeps_referral_and_book_target(self):
        message = self.message("ref_55_book_7")
        state = self.state()
        with (
            patch("handlers.user.db.add_user", new_callable=AsyncMock),
            patch("handlers.user.db.get_book", new_callable=AsyncMock, return_value={"id": 7}),
            patch("handlers.user.db.capture_campaign_first_touch", new_callable=AsyncMock) as capture,
            patch("handlers.user.db.parse_referral_code", new_callable=AsyncMock, return_value=55) as parse_referral,
            patch("handlers.user.db.check_referral_exists", new_callable=AsyncMock, return_value=False),
        ):
            await cmd_start_with_ref(message, state)

        capture.assert_awaited_once_with(77, "ref_55")
        parse_referral.assert_awaited_once_with("ref_55")
        state.update_data.assert_awaited_once_with(referrer_id=55, launch_book_id=7)
        self.assertIn("ref_accept", [
            button.callback_data
            for row in message.answer.await_args.kwargs["reply_markup"].inline_keyboard
            for button in row
        ])

    async def test_existing_referral_target_opens_specific_book(self):
        message = self.message("ref_55_book_7")
        state = self.state()
        with (
            patch("handlers.user.db.add_user", new_callable=AsyncMock),
            patch("handlers.user.db.get_book", new_callable=AsyncMock, return_value={"id": 7}),
            patch("handlers.user.db.capture_campaign_first_touch", new_callable=AsyncMock),
            patch("handlers.user.db.parse_referral_code", new_callable=AsyncMock, return_value=55),
            patch("handlers.user.db.check_referral_exists", new_callable=AsyncMock, return_value=True),
            patch("handlers.user._send_start_menu", new_callable=AsyncMock) as menu,
        ):
            await cmd_start_with_ref(message, state)

        menu.assert_awaited_once_with(message, launch_book_id=7)

    async def test_referral_choices_keep_target_book_button(self):
        state = self.state({"referrer_id": 55, "launch_book_id": 7})
        callback = self.callback()
        bot = SimpleNamespace(send_message=AsyncMock())
        with (
            patch("handlers.user.db.get_book", new_callable=AsyncMock, return_value={"id": 7}),
            patch("handlers.user.db.create_referral", new_callable=AsyncMock),
            patch("handlers.user.db.add_user_bonus", new_callable=AsyncMock),
            patch.object(settings, "WEBAPP_URL", "https://shop.example.test/"),
        ):
            await ref_accept_callback(callback, state, bot)
        accepted = callback.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual("https://shop.example.test/?book=7", accepted.inline_keyboard[0][0].web_app.url)

        state = self.state({"launch_book_id": 7})
        callback = self.callback()
        with (
            patch("handlers.user.db.get_book", new_callable=AsyncMock, return_value={"id": 7}),
            patch.object(settings, "WEBAPP_URL", "https://shop.example.test/"),
        ):
            await ref_decline_callback(callback, state)
        declined = callback.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual("https://shop.example.test/?book=7", declined.inline_keyboard[0][0].web_app.url)


if __name__ == "__main__":
    unittest.main()
