from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from handlers.payments import (
    payment_receipt_photo,
    payment_skip_photo,
    process_pre_checkout,
    user_confirm_payment,
)
from config import settings


class PaymentBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def _state(self, data=None):
        return SimpleNamespace(
            clear=AsyncMock(),
            get_data=AsyncMock(return_value=data or {}),
            set_state=AsyncMock(),
            update_data=AsyncMock(),
        )

    async def test_manual_payment_confirmation_rejects_non_owner(self):
        callback = SimpleNamespace(
            data="user_paid_10",
            from_user=SimpleNamespace(id=101),
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        state = self._state()
        bot = SimpleNamespace(send_message=AsyncMock())
        order = {"id": 10, "user_id": 202, "status": "awaiting_payment", "total": 100, "items": []}

        with (
            patch("handlers.payments.db.get_order_full", new_callable=AsyncMock, return_value=order),
            patch("handlers.payments.db.update_order_status", new_callable=AsyncMock) as update_status,
        ):
            await user_confirm_payment(callback, bot, state)

        update_status.assert_not_awaited()
        callback.message.edit_text.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_receipt_skip_requires_state_order_and_owner(self):
        callback = SimpleNamespace(
            data="payment_skip_photo_11",
            from_user=SimpleNamespace(id=101),
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        state = self._state({"order_id": 10})

        with patch("handlers.payments.db.get_order_full", new_callable=AsyncMock) as get_order:
            await payment_skip_photo(callback, state)

        get_order.assert_not_awaited()
        state.clear.assert_awaited_once()
        callback.answer.assert_awaited_once_with("❌ Заказ не найден", show_alert=True)

    async def test_manual_payment_marks_notification_sent_after_admin_delivery(self):
        callback = SimpleNamespace(
            data="user_paid_10",
            from_user=SimpleNamespace(id=101),
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        state = self._state()
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=55), None])
        )
        order = {
            "id": 10,
            "user_id": 101,
            "user_name": "Покупатель",
            "status": "awaiting_payment",
            "total": 100,
            "items": [],
        }
        original_admin_ids = settings.ADMIN_IDS
        settings.ADMIN_IDS = [303]
        try:
            with (
                patch("handlers.payments.recipient_ids_for_event_sync", return_value=[303]),
                patch("handlers.payments.db.get_order_full", new_callable=AsyncMock, return_value=order),
                patch("handlers.payments.db.update_order_status", new_callable=AsyncMock, return_value=True),
                patch("handlers.payments.save_admin_notification_ids", new_callable=AsyncMock) as save_ids,
                patch("handlers.payments.db.mark_new_order_notified", new_callable=AsyncMock) as mark_notified,
            ):
                await user_confirm_payment(callback, bot, state)
        finally:
            settings.ADMIN_IDS = original_admin_ids

        save_ids.assert_awaited_once_with(10, [303], [55])
        mark_notified.assert_awaited_once_with(10)

    async def test_manual_payment_keeps_poller_fallback_when_all_admin_sends_fail(self):
        callback = SimpleNamespace(
            data="user_paid_10",
            from_user=SimpleNamespace(id=101),
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        state = self._state()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[RuntimeError("failed"), None]))
        order = {
            "id": 10,
            "user_id": 101,
            "user_name": "Покупатель",
            "status": "awaiting_payment",
            "total": 100,
            "items": [],
        }
        original_admin_ids = settings.ADMIN_IDS
        settings.ADMIN_IDS = [303]
        try:
            with (
                patch("handlers.payments.recipient_ids_for_event_sync", return_value=[303]),
                patch("handlers.payments.db.get_order_full", new_callable=AsyncMock, return_value=order),
                patch("handlers.payments.db.update_order_status", new_callable=AsyncMock, return_value=True),
                patch("handlers.payments.save_admin_notification_ids", new_callable=AsyncMock) as save_ids,
                patch("handlers.payments.db.mark_new_order_notified", new_callable=AsyncMock) as mark_notified,
            ):
                await user_confirm_payment(callback, bot, state)
        finally:
            settings.ADMIN_IDS = original_admin_ids

        save_ids.assert_not_awaited()
        mark_notified.assert_not_awaited()

    async def test_failed_receipt_photo_keeps_existing_manual_notification(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=101),
            photo=[SimpleNamespace(file_id="photo-id")],
            answer=AsyncMock(),
        )
        state = self._state({"order_id": 10, "requested_at": 0})
        bot = SimpleNamespace(
            send_photo=AsyncMock(side_effect=RuntimeError("failed")),
            delete_message=AsyncMock(),
        )
        order = {
            "id": 10,
            "user_id": 101,
            "user_name": "Покупатель",
            "status": "payment_pending",
            "total": 100,
            "items": [],
            "admin_notification_ids": [{"admin_id": 303, "message_id": 55}],
        }
        original_admin_ids = settings.ADMIN_IDS
        settings.ADMIN_IDS = [303]
        try:
            with (
                patch("handlers.payments.recipient_ids_for_event_sync", return_value=[303]),
                patch("handlers.payments.db.get_order_full", new_callable=AsyncMock, return_value=order),
                patch("handlers.payments.replace_admin_notification_ids", new_callable=AsyncMock) as replace_ids,
            ):
                await payment_receipt_photo(message, bot, state)
        finally:
            settings.ADMIN_IDS = original_admin_ids

        bot.delete_message.assert_not_awaited()
        replace_ids.assert_not_awaited()

    async def test_receipt_photo_replaces_existing_manual_notification_after_delivery(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=101),
            photo=[SimpleNamespace(file_id="photo-id")],
            answer=AsyncMock(),
        )
        state = self._state({"order_id": 10, "requested_at": 0})
        bot = SimpleNamespace(
            send_photo=AsyncMock(return_value=SimpleNamespace(message_id=66)),
            delete_message=AsyncMock(),
        )
        order = {
            "id": 10,
            "user_id": 101,
            "user_name": "Покупатель",
            "status": "payment_pending",
            "total": 100,
            "items": [],
            "admin_notification_ids": [{"admin_id": 303, "message_id": 55}],
        }
        original_admin_ids = settings.ADMIN_IDS
        settings.ADMIN_IDS = [303]
        try:
            with (
                patch("handlers.payments.recipient_ids_for_event_sync", return_value=[303]),
                patch("handlers.payments.db.get_order_full", new_callable=AsyncMock, return_value=order),
                patch("handlers.payments.replace_admin_notification_ids", new_callable=AsyncMock) as replace_ids,
            ):
                await payment_receipt_photo(message, bot, state)
        finally:
            settings.ADMIN_IDS = original_admin_ids

        bot.delete_message.assert_awaited_once_with(chat_id=303, message_id=55)
        replace_ids.assert_awaited_once_with(10, [(303, 66)])

        query = SimpleNamespace(
            id="query",
            from_user=SimpleNamespace(id=101),
            invoice_payload="10",
            currency="XTR",
            total_amount=1,
        )
        bot = SimpleNamespace(answer_pre_checkout_query=AsyncMock())
        order = {"id": 10, "user_id": 101, "status": "awaiting_stars_payment", "total": 1200, "items": []}

        with (
            patch("handlers.payments.db.get_order_full", new_callable=AsyncMock, return_value=order),
            patch("handlers.payments.db.get_stars_setting", new_callable=AsyncMock, return_value="2"),
        ):
            await process_pre_checkout(query, bot)

        bot.answer_pre_checkout_query.assert_awaited_once_with(
            "query", ok=False, error_message="Неверная сумма"
        )


if __name__ == "__main__":
    unittest.main()
