import asyncio
from contextlib import suppress
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from handlers.admin_orders import (
    _parse_order_list_context,
    admin_orders_list,
    admin_orders_new,
    inventory_notification_loop,
    notify_confirmed_order,
    pending_page_switch,
)
from handlers.user import user_order_detail
from config import settings


class AdminOrderUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._admin_ids = settings.ADMIN_IDS
        settings.ADMIN_IDS = [101, 303]

    def tearDown(self):
        settings.ADMIN_IDS = self._admin_ids

    @staticmethod
    def callback(user_id=101):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock()),
        )

    async def test_confirmed_order_card_replaces_pending_cards_with_delivery_actions(self):
        order = {
            "id": 44,
            "user_id": 77,
            "user_name": "Покупатель <тег>",
            "total": 1700,
            "items_subtotal": 2200,
            "promo_code_snapshot": "SAVE20",
            "promo_discount": 300,
            "bonus_discount": 200,
            "total_discount": 500,
            "items": [
                {"book_id": 4, "title": "Книга для сборки", "price": 1100, "quantity": 2, "line_total": 2200},
            ],
            "status": "confirmed",
            "payment_method": "manual",
            "delivery": {
                "method": "russian_post_pickup",
                "shipment_status": "preparing",
            },
            "delivery_summary": {
                "method_label": "Почта России — отделение",
                "shipment_label": "Готовится к отправке",
                "tracking": None,
            },
        }
        bot = SimpleNamespace(
            send_message=AsyncMock(
                side_effect=[
                    SimpleNamespace(message_id=10),
                    SimpleNamespace(message_id=11),
                ]
            )
        )
        with (
            patch(
                "handlers.admin_orders.db.get_order_full",
                new_callable=AsyncMock,
                return_value=order,
            ),
            patch(
                "handlers.admin_orders.clear_admin_notifications",
                new_callable=AsyncMock,
            ) as clear_cards,
            patch(
                "handlers.admin_orders.save_admin_notification_ids",
                new_callable=AsyncMock,
            ) as save_cards,
        ):
            sent = await notify_confirmed_order(bot, 44)

        self.assertTrue(sent)
        clear_cards.assert_awaited_once_with(44, bot)
        save_cards.assert_awaited_once_with(44, [101, 303], [10, 11])
        text = bot.send_message.await_args.args[1]
        markup = bot.send_message.await_args.kwargs["reply_markup"]
        buttons = [
            (button.text, button.callback_data)
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertIn("Покупатель &lt;тег&gt;", text)
        self.assertIn("Книга для сборки ×2", text)
        self.assertIn("Промокод: <code>SAVE20</code>", text)
        self.assertIn("Скидка промокода: −300 ₽", text)
        self.assertIn("Скидка бонуса: −200 ₽", text)
        self.assertIn(("📍 Данные доставки", "delivery_details:44"), buttons)
        self.assertIn(("🧾 Ожидает сборки", "order_detail_44"), buttons)
        self.assertNotIn(("📦 Собран", "delivery_status:44:packed"), buttons)
        self.assertIn(("❌ Отменить", "cancel_order_44"), buttons)

    async def test_order_list_preserves_status_and_date_sort(self):
        callback = self.callback()
        orders = [
            {
                "id": 9,
                "status": "completed",
                "total": 1200,
                "created_at": "2026-09-16 12:00:00",
            }
        ]
        with (
            patch(
                "handlers.admin_orders.db.get_orders_count",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch(
                "handlers.admin_orders.db.get_all_orders",
                new_callable=AsyncMock,
                return_value=orders,
            ) as get_orders,
            patch(
                "handlers.admin_orders.safe_edit_text",
                new_callable=AsyncMock,
            ) as edit,
        ):
            await admin_orders_list(callback, status_key="completed", sort_by="old")

        get_orders.assert_awaited_once_with(
            limit=20, offset=0, status="completed", sort_by="old"
        )
        markup = edit.await_args.kwargs["reply_markup"]
        buttons = [
            (button.text, button.callback_data)
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertIn(("✅ 📦 Выполненные", "orders_filter_completed_old"), buttons)
        self.assertIn(("🆕 Новые заказы", "orders_filter_new_old"), buttons)
        self.assertIn(("✅ 📅 Старые сначала", "orders_sort_completed_old"), buttons)
        self.assertIn(("🔄 Обновить", "refresh_completed_old_0"), buttons)

    async def test_new_entry_uses_normal_new_status_filter(self):
        callback = self.callback()
        with patch(
            "handlers.admin_orders.admin_orders_list", new_callable=AsyncMock
        ) as render:
            await admin_orders_new(callback)

        render.assert_awaited_once_with(callback, status_key="new")

    async def test_legacy_pending_page_moves_to_new_status_filter(self):
        callback = self.callback()
        callback.data = "pending_page_2"
        with patch(
            "handlers.admin_orders.admin_orders_list", new_callable=AsyncMock
        ) as render:
            await pending_page_switch(callback)

        render.assert_awaited_once_with(callback, status_key="new", page=2)

    async def test_order_buttons_are_arranged_in_three_columns(self):
        callback = self.callback()
        orders = [
            {
                "id": order_id,
                "status": "new",
                "total": 1000 + order_id,
                "created_at": "2026-09-16 12:00:00",
            }
            for order_id in range(1, 7)
        ]
        with (
            patch(
                "handlers.admin_orders.db.get_orders_count",
                new_callable=AsyncMock,
                return_value=6,
            ),
            patch(
                "handlers.admin_orders.db.get_all_orders",
                new_callable=AsyncMock,
                return_value=orders,
            ),
            patch(
                "handlers.admin_orders.safe_edit_text",
                new_callable=AsyncMock,
            ) as edit,
        ):
            await admin_orders_list(callback, status_key="new")

        markup = edit.await_args.kwargs["reply_markup"]
        order_rows = [
            row
            for row in markup.inline_keyboard
            if row and all(button.callback_data.startswith("order_detail_") for button in row)
        ]
        self.assertEqual([3, 3], [len(row) for row in order_rows])

        self.assertEqual(
            ("confirmed", "new", 2),
            _parse_order_list_context("orders_page_confirmed_2", "orders_page_"),
        )
        self.assertEqual(
            ("new", "new", 2),
            _parse_order_list_context("orders_page_new_2", "orders_page_"),
        )
        self.assertEqual(
            ("new", "old", 2),
            _parse_order_list_context("orders_page_new_old_2", "orders_page_"),
        )
        self.assertIsNone(
            _parse_order_list_context("orders_page_drop_table_new_0", "orders_page_")
        )

    async def test_inventory_worker_delivers_back_in_stock_once(self):
        delivered = asyncio.Event()
        notification = {
            "id": 91,
            "kind": "back_in_stock",
            "book_id": 7,
            "user_id": 77,
            "payload_json": "{}",
        }

        async def send_message(*_args, **_kwargs):
            delivered.set()

        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send_message))
        with (
            patch("db.inventory.claim_notification_outbox", new_callable=AsyncMock, return_value=[notification]),
            patch("db.inventory.mark_notification_sent", new_callable=AsyncMock) as mark_sent,
            patch("db.inventory.revoke_back_in_stock_subscription", new_callable=AsyncMock) as revoke_subscription,
            patch("handlers.admin_orders.db.get_book", new_callable=AsyncMock, return_value={"title": "Тестовая книга"}),
            patch("handlers.admin_orders.NEW_ORDER_POLL_INTERVAL", 60),
        ):
            task = asyncio.create_task(inventory_notification_loop(bot))
            try:
                await asyncio.wait_for(delivered.wait(), timeout=1)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        bot.send_message.assert_awaited_once()
        revoke_subscription.assert_awaited_once_with(77, 7)
        mark_sent.assert_awaited_once_with(91)


        callback = self.callback(user_id=77)
        callback.data = "user_order_detail:44"
        order = {
            "id": 44,
            "user_id": 77,
            "total": 1700,
            "status": "confirmed",
            "delivery_summary": {
                "method_label": "Почта России — отделение",
                "shipment_label": "Отправлен",
                "public_instructions": None,
                "tracking": {
                    "carrier": "russian_post",
                    "number": "12345678901234",
                    "url": "https://www.pochta.ru/tracking",
                },
            },
        }
        with patch(
            "handlers.user.db.get_order_full", new_callable=AsyncMock, return_value=order
        ):
            await user_order_detail(callback)

        markup = callback.message.answer.await_args.kwargs["reply_markup"]
        buttons = [
            (button.text, button.url)
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertIn(
            ("📍 Отследить Почту России", "https://www.pochta.ru/tracking"),
            buttons,
        )


if __name__ == "__main__":
    unittest.main()
