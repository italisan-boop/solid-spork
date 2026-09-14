from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from config import settings
from handlers.admin_books import AdminBooksMiddleware
from handlers.admin_broadcast import process_broadcast_code, process_message
from handlers.admin_commands import cmd_drop_cache, submit_drop_cache_code
from handlers.admin_orders import cancel_order, confirm_order
from handlers.admin_promo import admin_promo_create
from handlers.admin_support import cb_support_reply, deferred_admin_reply
from handlers.admin_texts import admin_texts, save_template_value
from handlers.categories import category_add_start
from handlers.payments import (
    admin_yookassa_settings,
    pay_toggle_enabled,
    process_card,
    process_instructions,
    process_recipient,
    process_sbp_bank,
    process_sbp_phone,
    process_stars_rate,
    stars_toggle,
    yookassa_toggle,
)
from handlers.user import _send_admin_reply, support_claims, universal_text_handler
from states import (
    CategoryState,
    CriticalActionState,
    EditBookState,
    PaymentReceiptState,
    PaymentSettingsState,
    PromoCodeState,
    TextSettingsState,
)
from utils.otp_confirm import ACTION_DROP_CACHE


ADMIN_ID = 101
OUTSIDER_ID = 202


class AuthorizationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._admin_ids = settings.ADMIN_IDS
        settings.ADMIN_IDS = [ADMIN_ID]

    def tearDown(self):
        settings.ADMIN_IDS = self._admin_ids
        support_claims.clear()

    def message(self, user_id=OUTSIDER_ID, text="value", *, bot=None):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, full_name="Test User"),
            text=text,
            photo=None,
            answer=AsyncMock(),
            bot=bot or SimpleNamespace(send_message=AsyncMock()),
        )

    def callback(self, data, user_id=OUTSIDER_ID):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=user_id, full_name="Test User"),
            answer=AsyncMock(),
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
            message=SimpleNamespace(
                message_id=1,
                chat=SimpleNamespace(id=user_id),
                answer=AsyncMock(),
                edit_text=AsyncMock(),
                edit_reply_markup=AsyncMock(),
            ),
        )

    def state(self, *, current=None, data=None):
        return SimpleNamespace(
            clear=AsyncMock(),
            get_state=AsyncMock(return_value=current),
            get_data=AsyncMock(return_value=data or {}),
            set_state=AsyncMock(),
            update_data=AsyncMock(),
        )

    async def test_cache_command_and_forged_otp_state_are_denied(self):
        message = self.message(text="123456")
        state = self.state(data={"action": ACTION_DROP_CACHE})
        with (
            patch("handlers.admin_commands.issue_otp") as issue_otp,
            patch("handlers.admin_commands.consume_otp", return_value=True) as consume_otp,
        ):
            await cmd_drop_cache(message, state)
            await submit_drop_cache_code(message, state)

        issue_otp.assert_not_called()
        consume_otp.assert_not_called()
        self.assertEqual(2, state.clear.await_count)

    async def test_admin_cache_smoke_starts_otp_flow(self):
        message = self.message(ADMIN_ID)
        state = self.state()
        with patch("handlers.admin_commands.issue_otp", return_value="123456") as issue_otp:
            await cmd_drop_cache(message, state)

        issue_otp.assert_called_once_with(ADMIN_ID, ACTION_DROP_CACHE)
        state.set_state.assert_awaited_once_with(CriticalActionState.waiting_for_code)

    async def test_broadcast_forged_draft_and_code_states_are_denied(self):
        message = self.message(text="broadcast")
        state = self.state()
        with (
            patch("handlers.admin_broadcast.get_all_users", new_callable=AsyncMock) as get_users,
            patch("handlers.admin_broadcast.consume_otp", return_value=True) as consume_otp,
        ):
            await process_message(message, state)
            await process_broadcast_code(message, state)

        get_users.assert_not_awaited()
        consume_otp.assert_not_called()
        self.assertEqual(2, state.clear.await_count)
        state.update_data.assert_not_awaited()

    async def test_admin_broadcast_smoke_creates_preview(self):
        message = self.message(ADMIN_ID, "hello")
        state = self.state()
        with patch("handlers.admin_broadcast.get_all_users", new_callable=AsyncMock, return_value=[{"user_id": 1}]):
            await process_message(message, state)

        state.update_data.assert_awaited_once_with(message_text="hello")
        state.set_state.assert_awaited_once()

    async def test_template_callback_and_forged_writer_are_denied(self):
        callback = self.callback("admin_texts")
        message = self.message(text="<b>new</b>")
        state = self.state(data={"template_key": "support.quick.greeting"})
        with patch("handlers.admin_texts.db.set_message_template", new_callable=AsyncMock) as set_template:
            await admin_texts(callback, state)
            await save_template_value(message, state)

        callback.answer.assert_awaited_once_with("❌ Нет прав", show_alert=True)
        set_template.assert_not_awaited()
        state.clear.assert_awaited_once()

    async def test_admin_template_smoke_writes_value(self):
        message = self.message(ADMIN_ID, "<b>new</b>")
        state = self.state(data={"template_key": "support.quick.greeting"})
        with patch("handlers.admin_texts.db.set_message_template", new_callable=AsyncMock) as set_template:
            await save_template_value(message, state)

        set_template.assert_awaited_once_with("support.quick.greeting", "<b>new</b>")

    async def test_orders_do_not_expose_legacy_public_callbacks(self):
        import handlers.user as user_handlers

        self.assertFalse(hasattr(user_handlers, "confirm_order"))
        self.assertFalse(hasattr(user_handlers, "cancel_order"))

    async def test_non_admin_order_callback_cannot_read_or_update_order(self):
        callback = self.callback("confirm_44")
        with (
            patch("handlers.admin_orders.db.get_order_full", new_callable=AsyncMock) as get_order,
            patch("handlers.admin_orders.db.update_order_status", new_callable=AsyncMock) as update_order,
        ):
            await confirm_order(callback, callback.bot)

        get_order.assert_not_awaited()
        update_order.assert_not_awaited()
        callback.answer.assert_awaited_once_with("❌ Нет прав", show_alert=True)

    async def test_admin_order_callback_allows_pending_order_only(self):
        callback = self.callback("confirm_44", ADMIN_ID)
        order = {"id": 44, "user_id": 1, "status": "new"}
        with (
            patch("handlers.admin_orders.db.get_order_full", new_callable=AsyncMock, return_value=order),
            patch("handlers.admin_orders.db.update_order_status", new_callable=AsyncMock) as update_order,
            patch("handlers.admin_orders.order_detail", new_callable=AsyncMock),
        ):
            await confirm_order(callback, callback.bot)

        update_order.assert_awaited_once_with(44, "confirmed")

    async def test_admin_order_callback_rejects_stale_transition(self):
        callback = self.callback("confirm_44", ADMIN_ID)
        order = {"id": 44, "user_id": 1, "status": "completed"}
        with (
            patch("handlers.admin_orders.db.get_order_full", new_callable=AsyncMock, return_value=order),
            patch("handlers.admin_orders.db.update_order_status", new_callable=AsyncMock) as update_order,
        ):
            await confirm_order(callback, callback.bot)

        update_order.assert_not_awaited()

    async def test_admin_can_accept_verified_provider_payment_only_after_paid(self):
        callback = self.callback("confirm_44", ADMIN_ID)
        order = {"id": 44, "user_id": 1, "status": "paid", "payment_method": "yookassa"}
        with (
            patch("handlers.admin_orders.db.get_order_full", new_callable=AsyncMock, return_value=order),
            patch("handlers.admin_orders.db.update_order_status", new_callable=AsyncMock) as update_order,
            patch("handlers.admin_orders.order_detail", new_callable=AsyncMock),
        ):
            await confirm_order(callback, callback.bot)

        update_order.assert_awaited_once_with(44, "confirmed")

    async def test_admin_cannot_cancel_verified_provider_payment(self):
        callback = self.callback("cancel_order_44", ADMIN_ID)
        order = {"id": 44, "user_id": 1, "status": "paid", "payment_method": "yookassa"}
        with (
            patch("handlers.admin_orders.db.get_order_full", new_callable=AsyncMock, return_value=order),
            patch("handlers.admin_orders.db.update_order_status", new_callable=AsyncMock) as update_order,
        ):
            await cancel_order(callback, callback.bot)

        update_order.assert_not_awaited()


        from handlers.user import cancel_action

        for current_state in (
            PaymentSettingsState.waiting_for_card.state,
            PaymentReceiptState.waiting_for_photo.state,
            EditBookState.waiting_for_new_title.state,
        ):
            message = self.message(text="/cancel")
            state = self.state(current=current_state)
            with patch("handlers.user.revoke_otp") as revoke_otp:
                await cancel_action(message, state)
            state.clear.assert_awaited_once()
            revoke_otp.assert_called_once_with(OUTSIDER_ID)

    async def test_admin_books_middleware_blocks_outsider_and_allows_admin(self):
        middleware = AdminBooksMiddleware()
        outsider_state = self.state()
        outsider_handler = AsyncMock()
        await middleware(outsider_handler, SimpleNamespace(from_user=SimpleNamespace(id=OUTSIDER_ID)), {"state": outsider_state})
        outsider_handler.assert_not_awaited()
        outsider_state.clear.assert_awaited_once()

        admin_handler = AsyncMock(return_value="ok")
        result = await middleware(admin_handler, SimpleNamespace(from_user=SimpleNamespace(id=ADMIN_ID)), {})
        self.assertEqual("ok", result)
        admin_handler.assert_awaited_once()

    async def test_category_callback_and_forged_terminal_state_are_denied(self):
        callback = self.callback("category_add")
        callback_state = self.state()
        message = self.message(text="📚")
        writer_state = self.state(
            current=CategoryState.waiting_for_emoji.state,
            data={"category_name": "Books"},
        )
        with (
            patch("handlers.user.db.is_support_active", new_callable=AsyncMock, return_value=False),
            patch("handlers.user.db.add_category", new_callable=AsyncMock) as add_category,
        ):
            await category_add_start(callback, callback_state)
            await universal_text_handler(message, writer_state, message.bot)

        callback_state.set_state.assert_not_awaited()
        add_category.assert_not_awaited()
        writer_state.clear.assert_awaited_once()

    async def test_admin_category_terminal_state_can_write(self):
        message = self.message(ADMIN_ID, "📚")
        state = self.state(
            current=CategoryState.waiting_for_emoji.state,
            data={"category_name": "Books"},
        )
        with (
            patch("handlers.user.db.is_support_active", new_callable=AsyncMock, return_value=False),
            patch("handlers.user.db.add_category", new_callable=AsyncMock, return_value=7) as add_category,
        ):
            await universal_text_handler(message, state, message.bot)

        add_category.assert_awaited_once_with("Books", "📚")

    async def test_promo_callback_and_forged_terminal_state_are_denied(self):
        callback = self.callback("admin_promo_create")
        callback_state = self.state()
        message = self.message(text="forever")
        writer_state = self.state(
            current=PromoCodeState.waiting_for_expires.state,
            data={"promo_code": "SAVE", "discount_percent": 10},
        )
        with (
            patch("handlers.user.db.is_support_active", new_callable=AsyncMock, return_value=False),
            patch("handlers.user.db.add_promo_code", new_callable=AsyncMock) as add_promo,
        ):
            await admin_promo_create(callback, callback_state)
            await universal_text_handler(message, writer_state, message.bot)

        callback_state.set_state.assert_not_awaited()
        add_promo.assert_not_awaited()
        writer_state.clear.assert_awaited_once()

    async def test_admin_promo_callback_starts_wizard(self):
        callback = self.callback("admin_promo_create", ADMIN_ID)
        state = self.state()
        await admin_promo_create(callback, state)
        state.set_state.assert_awaited_once_with(PromoCodeState.waiting_for_code)

    async def test_payment_and_stars_callbacks_and_all_forged_writers_are_denied(self):
        callback = self.callback("pay_toggle_enabled")
        stars_callback = self.callback("stars_toggle")
        with (
            patch("handlers.payments.db.set_payment_setting", new_callable=AsyncMock) as set_payment,
            patch("handlers.payments.db.set_stars_setting", new_callable=AsyncMock) as set_stars,
        ):
            await pay_toggle_enabled(callback)
            await stars_toggle(stars_callback)
            for writer in (
                process_card,
                process_sbp_phone,
                process_sbp_bank,
                process_recipient,
                process_instructions,
                process_stars_rate,
            ):
                state = self.state()
                await writer(self.message(text="12"), state)
                state.clear.assert_awaited_once()

        set_payment.assert_not_awaited()
        set_stars.assert_not_awaited()

    async def test_yookassa_callbacks_are_denied_to_non_admin(self):
        callback = self.callback("payment_settings:yookassa")
        toggle_callback = self.callback("yookassa_toggle")
        with patch("handlers.payments.db.set_payment_setting", new_callable=AsyncMock) as set_payment:
            await admin_yookassa_settings(callback)
            await yookassa_toggle(toggle_callback)

        callback.message.edit_text.assert_not_awaited()
        set_payment.assert_not_awaited()

    async def test_admin_yookassa_toggle_requires_environment_configuration(self):
        callback = self.callback("yookassa_toggle", ADMIN_ID)
        with (
            patch("handlers.payments.db.get_payment_setting", new_callable=AsyncMock, return_value="0"),
            patch("handlers.payments.db.set_payment_setting", new_callable=AsyncMock) as set_payment,
            patch("handlers.payments.settings.YOOKASSA_SHOP_ID", ""),
            patch("handlers.payments.settings.YOOKASSA_SECRET_KEY", ""),
            patch("handlers.payments.settings.YOOKASSA_RETURN_URL", ""),
        ):
            await yookassa_toggle(callback)

        set_payment.assert_not_awaited()
        callback.answer.assert_awaited_once_with(
            "Сначала задайте shopId, secret key и return URL в .env", show_alert=True
        )

    async def test_admin_payment_and_stars_writers_can_update_settings(self):
        payment_state = self.state()
        stars_state = self.state()
        with (
            patch("handlers.payments.db.set_payment_setting", new_callable=AsyncMock) as set_payment,
            patch("handlers.payments.db.set_stars_setting", new_callable=AsyncMock) as set_stars,
        ):
            await process_card(self.message(ADMIN_ID, "4111"), payment_state)
            await process_stars_rate(self.message(ADMIN_ID, "2"), stars_state)

        set_payment.assert_awaited_once_with("card_number", "4111")
        set_stars.assert_awaited_once_with("rubles_per_star", "2")

    async def test_support_callback_and_forged_writer_are_denied(self):
        callback = self.callback("support_reply:44")
        callback_state = self.state()
        message = self.message(text="reply")
        writer_state = self.state(data={"reply_user_id": 44})
        with patch("handlers.admin_support._send_admin_reply", new_callable=AsyncMock) as send_reply:
            await cb_support_reply(callback, callback_state)
            await deferred_admin_reply(message, writer_state)

        send_reply.assert_not_awaited()
        callback_state.set_state.assert_not_awaited()
        writer_state.clear.assert_awaited_once()

    async def test_claim_owner_is_the_only_admin_who_can_reply(self):
        settings.ADMIN_IDS.append(303)
        support_claims[44] = ADMIN_ID
        message = self.message(303, "reply")

        ok, status = await _send_admin_reply(message, 44, "reply", admin_id=303)

        self.assertFalse(ok)
        self.assertIn("ведёт другой админ", status)
        message.bot.send_message.assert_not_awaited()

    async def test_claim_owner_can_reply(self):
        support_claims[44] = ADMIN_ID
        message = self.message(ADMIN_ID, "reply")
        message.chat = SimpleNamespace(id=ADMIN_ID)
        message.message_id = 1
        message.reply_to_message = None
        with (
            patch("handlers.user.db.get_message_template", new_callable=AsyncMock, side_effect=["Header", "Follow up"]),
            patch("handlers.user.set_support_mode", new_callable=AsyncMock),
        ):
            ok, _ = await _send_admin_reply(message, 44, "reply")

        self.assertTrue(ok)
        message.bot.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
