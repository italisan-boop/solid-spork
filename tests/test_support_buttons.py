from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from handlers.admin_support import _build_support_menu_markup, _support_reply_markup
from handlers.user import (
    _rewrite_ticket_notices,
    _ticket_action_markup,
    support_claims,
    support_exit_callback,
    support_forward_msgs,
)


def callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


class SupportKeyboardTests(unittest.TestCase):
    def tearDown(self):
        support_claims.clear()
        support_forward_msgs.clear()

    def test_reply_menu_contains_all_templates_and_custom_reply(self):
        values = callbacks(_support_reply_markup(42))
        self.assertEqual(
            {
                "support_template:42:greeting",
                "support_template:42:wait",
                "support_template:42:ask_details",
                "support_template:42:payment",
                "support_template:42:resolved",
            },
            {value for value in values if value.startswith("support_template:")},
        )
        self.assertIn("support_custom_reply:42", values)

    def test_support_menu_has_admin_back_button_even_without_tickets(self):
        self.assertIn("admin_menu", callbacks(_build_support_menu_markup([])))

    def test_claim_button_changes_to_release_and_back(self):
        self.assertIn("🔒 Взять в работу", labels(_ticket_action_markup(42)))
        support_claims[42] = 7
        self.assertIn("🔓 Отпустить", labels(_ticket_action_markup(42)))
        support_claims.pop(42)
        self.assertIn("🔒 Взять в работу", labels(_ticket_action_markup(42)))


class SupportCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        support_claims.clear()
        support_forward_msgs.clear()

    async def test_claimed_notice_keeps_reply_actions(self):
        support_claims[42] = 7
        support_forward_msgs[42] = [(100, 200)]
        bot = SimpleNamespace(edit_message_text=AsyncMock())

        await _rewrite_ticket_notices(42, bot, "🔒 <b>Тикет уже в работе</b>")

        kwargs = bot.edit_message_text.await_args.kwargs
        self.assertIn("support_reply:42", callbacks(kwargs["reply_markup"]))
        self.assertIn("support_release:42", callbacks(kwargs["reply_markup"]))

    async def test_support_exit_deletes_prompt_and_sends_start_menu(self):
        message = SimpleNamespace(
            delete=AsyncMock(),
            edit_reply_markup=AsyncMock(),
            answer=AsyncMock(),
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            message=message,
            answer=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock())

        with (
            patch("handlers.user._is_in_support", new_callable=AsyncMock, return_value=True),
            patch("handlers.user.set_support_mode", new_callable=AsyncMock) as set_mode,
        ):
            await support_exit_callback(callback, state)

        set_mode.assert_awaited_once_with(42, False)
        message.delete.assert_awaited_once()
        self.assertIn("Добро пожаловать", message.answer.await_args.args[0])

    async def test_repeated_support_exit_does_not_send_another_start_menu(self):
        message = SimpleNamespace(
            delete=AsyncMock(),
            edit_reply_markup=AsyncMock(),
            answer=AsyncMock(),
        )
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            message=message,
            answer=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock())

        with (
            patch("handlers.user._is_in_support", new_callable=AsyncMock, return_value=False),
            patch("handlers.user.set_support_mode", new_callable=AsyncMock) as set_mode,
        ):
            await support_exit_callback(callback, state)

        set_mode.assert_not_awaited()
        message.delete.assert_awaited_once()
        message.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
