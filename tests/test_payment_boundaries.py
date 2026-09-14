from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from handlers.payments import (
    payment_skip_photo,
    process_pre_checkout,
    user_confirm_payment,
)


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

    async def test_stars_pre_checkout_rejects_wrong_amount(self):
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
