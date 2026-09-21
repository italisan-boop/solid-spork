import asyncio
from contextlib import suppress
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from handlers.admin_support import (
    _build_support_menu_markup,
    _support_reply_markup,
    cb_support_close,
    cb_support_dialog_history,
    cb_support_dialogs,
)
from handlers.user import (
    _closed_ticket_action_markup,
    close_support_ticket,
    _rewrite_ticket_notices,
    _ticket_action_markup,
    order_support_notify_loop,
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

    def test_support_menu_exposes_all_dialogs_without_active_tickets(self):
        self.assertIn("support_dialogs:0", callbacks(_build_support_menu_markup([])))

    def test_live_and_closed_ticket_markup_have_expected_actions(self):
        self.assertIn("🔒 Взять в работу", labels(_ticket_action_markup(42)))
        support_claims[42] = 7
        self.assertIn("🔓 Отпустить", labels(_ticket_action_markup(42)))
        support_claims.pop(42)
        self.assertIn("support_close:42", callbacks(_ticket_action_markup(42)))
        self.assertIn("admin_support_menu", callbacks(_ticket_action_markup(42)))
        closed = callbacks(_closed_ticket_action_markup(42))
        self.assertIn("support_history:42", closed)
        self.assertIn("admin_support_menu", closed)
        self.assertFalse(any(value.startswith("support_reply:") for value in closed))
        self.assertFalse(any(value.startswith("support_close:") for value in closed))


class SupportCallbackTests(unittest.IsolatedAsyncioTestCase):
    def callback(self, data, user_id=7):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=user_id, full_name="Admin"),
            bot=SimpleNamespace(send_message=AsyncMock()),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )

    async def asyncTearDown(self):
        support_claims.clear()
        support_forward_msgs.clear()

    async def test_close_ticket_notifies_customer_and_returns_to_menu(self):
        callback = self.callback("support_close:42")
        with (
            patch("handlers.admin_support.is_admin", return_value=True),
            patch("handlers.admin_support.close_support_ticket", new_callable=AsyncMock, return_value=True) as close,
            patch("handlers.admin_support.cb_support_menu", new_callable=AsyncMock) as menu,
        ):
            await cb_support_close(callback)
        close.assert_awaited_once_with(42, "Admin", callback.bot)
        menu.assert_awaited_once_with(callback)

    async def test_close_ticket_rejects_non_admin_before_state_mutation(self):
        callback = self.callback("support_close:42", user_id=999)
        with (
            patch("handlers.admin_support.is_admin", return_value=False),
            patch("handlers.admin_support.close_support_ticket", new_callable=AsyncMock) as close,
            patch("handlers.admin_support.cb_support_menu", new_callable=AsyncMock) as menu,
        ):
            await cb_support_close(callback)
        close.assert_not_awaited()
        menu.assert_not_awaited()
        callback.answer.assert_awaited_once_with("❌ Нет прав", show_alert=True)

    async def test_all_dialogs_are_history_only(self):
        callback = self.callback("support_dialogs:0")
        dialogs = [{
            "user_id": 42,
            "user_name": "Покупатель",
            "last_role": "user",
            "last_text": "Нужна помощь",
            "last_created_at": "2026-09-15 10:00:00",
        }]
        with (
            patch("handlers.admin_support.is_admin", return_value=True),
            patch("handlers.admin_support.db.get_support_dialog_count", new_callable=AsyncMock, return_value=1),
            patch("handlers.admin_support.db.get_support_dialogs", new_callable=AsyncMock, return_value=dialogs),
        ):
            await cb_support_dialogs(callback)

        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        values = callbacks(markup)
        self.assertIn("support_dialog_history:42:0:0", values)
        self.assertFalse(any(value.startswith("support_reply:") for value in values))
        self.assertFalse(any(value.startswith("support_claim:") for value in values))

    async def test_dialog_history_rejects_outsider_before_database_read(self):
        callback = self.callback("support_dialog_history:42:0:0", user_id=999)
        with (
            patch("handlers.admin_support.is_admin", return_value=False),
            patch("handlers.admin_support._build_history_text", new_callable=AsyncMock) as history,
        ):
            await cb_support_dialog_history(callback)

        history.assert_not_awaited()
        callback.answer.assert_awaited_once_with("❌ Нет прав", show_alert=True)

    async def test_rewrite_ticket_notices_keeps_live_controls(self):
        support_claims[42] = 7
        support_forward_msgs[42] = [(100, 200)]
        bot = SimpleNamespace(edit_message_text=AsyncMock())

        await _rewrite_ticket_notices(42, bot, "🔒 <b>Тикет уже в работе</b>")

        kwargs = bot.edit_message_text.await_args.kwargs
        self.assertIn("support_reply:42", callbacks(kwargs["reply_markup"]))
        self.assertIn("support_release:42", callbacks(kwargs["reply_markup"]))
        self.assertEqual([(100, 200)], support_forward_msgs[42])

    async def test_order_support_worker_delivers_authoritative_ticket_without_exit_control(self):
        sent = asyncio.Event()
        request = {
            "id": 71,
            "user_id": 42,
            "receipt_json": (
                '{"id":44,"status":"confirmed","payment_method":"manual",'
                '"items":[{"title":"Историческая книга","quantity":2,"line_total":2000}],'
                '"items_subtotal":2000,"delivery_price":0,"promo_code_snapshot":"SAVE25",'
                '"promo_discount":400,"bonus_discount":100,"total_discount":500,"total":1500}'
            ),
        }

        async def mark_sent(request_id):
            self.assertEqual(71, request_id)
            sent.set()

        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)))
        with (
            patch("db.order_support_requests.claim_order_support_requests", new_callable=AsyncMock, return_value=[request]),
            patch("db.order_support_requests.mark_order_support_request_sent", side_effect=mark_sent),
            patch("db.order_support_requests.release_order_support_request", new_callable=AsyncMock) as release,
            patch("handlers.user.set_support_mode", new_callable=AsyncMock, return_value=True) as set_mode,
            patch("handlers.user.recipient_ids_for_event_sync", return_value=[101]),
            patch("handlers.user.settings.ADMIN_IDS", [101]),
        ):
            task = asyncio.create_task(order_support_notify_loop(bot))
            try:
                await asyncio.wait_for(sent.wait(), timeout=1)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        admin_call = bot.send_message.await_args_list[0]
        self.assertEqual(101, admin_call.args[0])
        self.assertIn("Историческая книга ×2", admin_call.args[1])
        self.assertIn("support_reply:42", callbacks(admin_call.kwargs["reply_markup"]))
        customer_call = bot.send_message.await_args_list[1]
        self.assertEqual(42, customer_call.args[0])
        self.assertNotIn("reply_markup", customer_call.kwargs)
        set_mode.assert_awaited_once_with(42, True)
        release.assert_not_awaited()

    async def test_admin_close_persists_notifies_customer_and_rewrites_notices(self):
        support_forward_msgs[42] = [(100, 200)]
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        )
        with (
            patch("handlers.user._is_in_support", new_callable=AsyncMock, return_value=True),
            patch("handlers.user.db.set_support_active", new_callable=AsyncMock) as set_active,
        ):
            self.assertTrue(await close_support_ticket(42, "Admin", bot))

        set_active.assert_awaited_once_with(42, False)
        self.assertEqual(42, bot.send_message.await_args.args[0])
        markup = bot.edit_message_text.await_args.kwargs["reply_markup"]
        values = callbacks(markup)
        self.assertIn("support_history:42", values)
        self.assertFalse(any(value.startswith("support_reply:") for value in values))
        self.assertFalse(any(value.startswith("support_close:") for value in values))


        message = SimpleNamespace(delete=AsyncMock(), edit_reply_markup=AsyncMock())
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42),
            message=message,
            answer=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock())

        await support_exit_callback(callback, state)

        state.clear.assert_awaited_once()
        message.delete.assert_awaited_once()
        callback.answer.assert_awaited_once_with(
            "Обращение остаётся открытым до закрытия поддержкой."
        )


if __name__ == "__main__":
    unittest.main()
