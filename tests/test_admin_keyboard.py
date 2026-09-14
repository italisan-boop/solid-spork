from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from handlers.admin_orders import admin_keyboard, admin_menu, cmd_admin


EXPECTED_BUTTONS = [
    ("📋 Все заказы", "admin_orders_all"),
    ("🆕 Новые", "admin_orders_new"),
    ("✅ Подтверждённые", "admin_orders_confirmed"),
    ("📊 Статистика", "admin_stats"),
    ("📢 Рассылка", "admin_broadcast"),
    ("📚 Управление книгами", "admin_books_menu"),
    ("📂 Управление категориями", "admin_categories"),
    ("💳 Настройки оплаты", "admin_payments"),
    ("🎟️ Промокоды", "admin_promo"),
    ("🗑 Сброс кэша", "admin_drop_cache"),
    ("🎧 Поддержка", "admin_support_menu"),
    ("✏️ Тексты", "admin_texts"),
]


def button_pairs(markup):
    return [
        (button.text, button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]


class AdminKeyboardTests(unittest.IsolatedAsyncioTestCase):
    def test_keyboard_has_current_buttons_in_order(self):
        markup = admin_keyboard()
        self.assertEqual(EXPECTED_BUTTONS, button_pairs(markup))
        self.assertEqual([2, 2, 2, 2, 2, 2], [len(row) for row in markup.inline_keyboard])

    async def test_admin_command_uses_keyboard_factory(self):
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=42, full_name="Админ"),
            answer=AsyncMock(),
        )
        with patch("handlers.admin_orders.is_admin", return_value=True):
            await cmd_admin(message)

        markup = message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(EXPECTED_BUTTONS, button_pairs(markup))

    async def test_admin_menu_callback_uses_keyboard_factory(self):
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=42, full_name="Админ"),
            answer=AsyncMock(),
        )
        with (
            patch("handlers.admin_orders.is_admin", return_value=True),
            patch("handlers.admin_orders.safe_edit_text", new_callable=AsyncMock) as safe_edit,
        ):
            await admin_menu(callback)

        markup = safe_edit.await_args.kwargs["reply_markup"]
        self.assertEqual(EXPECTED_BUTTONS, button_pairs(markup))
        callback.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
