from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from handlers.admin_books import (
    _build_books_list_view,
    confirm_archive_selection,
    confirm_purge_code,
    issue_purge_code,
    render_archive_list,
    render_books_list_for_message,
    start_archive_selection,
    toggle_archive_selection,
)
from states import AdminBooksState
from utils.otp_confirm import ACTION_PURGE_ARCHIVED_BOOKS


class AdminBookDeletionHandlerTests(unittest.IsolatedAsyncioTestCase):
    def callback(self, data, user_id=101):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        )

    def message(self, text, user_id=101):
        return SimpleNamespace(
            text=text,
            from_user=SimpleNamespace(id=user_id),
            answer=AsyncMock(),
        )

    def state(self, data=None):
        return SimpleNamespace(
            get_data=AsyncMock(return_value=data or {}),
            update_data=AsyncMock(),
            set_state=AsyncMock(),
            clear=AsyncMock(),
        )

    async def test_selection_starts_empty_and_toggles_only_active_book(self):
        state = self.state({"current_page": 2})
        callback = self.callback("admin_books_archive_select")
        with patch("handlers.admin_books.show_books_list", new_callable=AsyncMock) as show_list:
            await start_archive_selection(callback, state)
        state.update_data.assert_awaited_once_with(book_selection_mode="archive", selected_book_ids=[])
        show_list.assert_awaited_once_with(callback, state, page=2)

        state = self.state({"book_selection_mode": "archive", "selected_book_ids": [], "current_page": 2})
        callback = self.callback("admin_books_archive_toggle_7")
        with (
            patch("handlers.admin_books.get_book", new_callable=AsyncMock, return_value={"id": 7}),
            patch("handlers.admin_books.show_books_list", new_callable=AsyncMock) as show_list,
        ):
            await toggle_archive_selection(callback, state)
        state.update_data.assert_awaited_once_with(selected_book_ids=[7])
        show_list.assert_awaited_once_with(callback, state, page=2)

    async def test_bulk_archive_uses_repository_result_and_clears_selection(self):
        state = self.state({"book_selection_mode": "archive", "selected_book_ids": [3, 7], "current_page": 1})
        callback = self.callback("admin_books_archive_selected_confirm")
        with (
            patch("handlers.admin_books.archive_books", new_callable=AsyncMock, return_value={"archived_ids": [3], "skipped_ids": [7]}) as archive,
            patch("handlers.admin_books.show_books_list", new_callable=AsyncMock) as show_list,
        ):
            await confirm_archive_selection(callback, state)
        archive.assert_awaited_once_with([3, 7])
        state.update_data.assert_awaited_once_with(book_selection_mode=None, selected_book_ids=[])
        show_list.assert_awaited_once_with(callback, state, page=1)

    async def test_books_actions_are_visible_before_book_rows_and_titles_are_safe(self):
        books = [
            {
                "id": index,
                "title": "<Очень длинное название & книги>" * 4,
                "price": 100,
                "category_emoji": "📖",
            }
            for index in range(1, 11)
        ]
        state = self.state({"sort_by": "default", "search_query": "", "current_page": 0})
        with (
            patch("handlers.admin_books.get_books_count", new_callable=AsyncMock, return_value=10),
            patch("handlers.admin_books.get_all_books_paginated", new_callable=AsyncMock, return_value=books),
        ):
            text, markup, page = await _build_books_list_view(state, 0)

        labels = [button.text for row in markup.inline_keyboard for button in row]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        book_label = next(label for label in labels if label.startswith("📝 "))
        self.assertEqual(0, page)
        self.assertLess(labels.index("➕ Добавить книгу"), labels.index(book_label))
        self.assertLess(labels.index("☑️ Выбрать несколько для архива"), labels.index(book_label))
        self.assertIn("&lt;Очень длинное название &amp; книги&gt;", text)
        self.assertIn("admin_add_book", callbacks)
        self.assertIn("admin_books_archive_select", callbacks)

    async def test_search_message_renderer_uses_shared_books_view(self):
        state = self.state({"sort_by": "default", "search_query": "needle", "current_page": 0})
        message = self.message("needle")
        with (
            patch("handlers.admin_books.get_books_count", new_callable=AsyncMock, return_value=0),
            patch("handlers.admin_books.get_all_books_paginated", new_callable=AsyncMock, return_value=[]),
        ):
            await render_books_list_for_message(message, state)

        self.assertIn("По этому запросу ничего не найдено", message.answer.await_args.args[0])
        self.assertIn(
            "admin_add_book",
            [button.callback_data for row in message.answer.await_args.kwargs["reply_markup"].inline_keyboard for button in row],
        )


        state = self.state({"archive_page": 0, "book_selection_mode": None})
        callback = self.callback("admin_books_archive")
        books = [
            {"id": 7, "title": "Eligible", "price": 100, "order_items_count": 0},
            {"id": 8, "title": "Referenced", "price": 100, "order_items_count": 2},
        ]
        with (
            patch("handlers.admin_books.get_archived_books_count", new_callable=AsyncMock, return_value=2),
            patch("handlers.admin_books.get_archived_books", new_callable=AsyncMock, return_value=books),
        ):
            await render_archive_list(callback, state, page=0)

        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertIn("admin_book_restore_7", callbacks)
        self.assertIn("admin_book_purge_7", callbacks)
        self.assertIn("admin_book_restore_8", callbacks)
        self.assertIn("admin_books_purge_protected_8", callbacks)
        self.assertNotIn("admin_book_purge_8", callbacks)


        state = self.state({"book_selection_mode": "purge", "selected_book_ids": [7]})
        callback = self.callback("admin_books_purge_issue_code", user_id=202)
        with patch("handlers.admin_books.issue_otp") as issue:
            await issue_purge_code(callback, state)
        state.clear.assert_awaited_once()
        issue.assert_not_called()
        callback.answer.assert_awaited_once_with("❌ Нет прав", show_alert=True)

    async def test_admin_purge_code_requires_eligible_snapshot(self):
        state = self.state({"book_selection_mode": "purge", "selected_book_ids": [7, 8]})
        callback = self.callback("admin_books_purge_issue_code")
        with (
            patch("handlers.admin_books.is_admin", return_value=True),
            patch("handlers.admin_books.classify_archived_book_ids", new_callable=AsyncMock, return_value={"eligible_ids": [7], "referenced_ids": [8], "not_archived_ids": [], "missing_ids": []}),
            patch("handlers.admin_books.issue_otp", return_value="123456") as issue,
        ):
            await issue_purge_code(callback, state)
        issue.assert_called_once_with(101, ACTION_PURGE_ARCHIVED_BOOKS)
        state.update_data.assert_awaited_once_with(purge_book_ids=[7], book_selection_mode="purge_pending")
        state.set_state.assert_awaited_once_with(AdminBooksState.waiting_for_archive_purge_code)

    async def test_purge_terminal_rejects_non_admin_and_bad_or_reused_codes(self):
        state = self.state({"book_selection_mode": "purge_pending", "purge_book_ids": [7]})
        message = self.message("123456", user_id=202)
        with patch("handlers.admin_books.purge_archived_books", new_callable=AsyncMock) as purge:
            await confirm_purge_code(message, state)
        state.clear.assert_awaited_once()
        purge.assert_not_awaited()

        state = self.state({"book_selection_mode": "purge_pending", "purge_book_ids": [7]})
        message = self.message("bad")
        with (
            patch("handlers.admin_books.is_admin", return_value=True),
            patch("handlers.admin_books.consume_otp", return_value=False) as consume,
            patch("handlers.admin_books.purge_archived_books", new_callable=AsyncMock) as purge,
        ):
            await confirm_purge_code(message, state)
        consume.assert_called_once_with(101, ACTION_PURGE_ARCHIVED_BOOKS, "bad")
        purge.assert_not_awaited()
        state.clear.assert_awaited_once()

    async def test_valid_purge_code_executes_once_and_clears_state(self):
        state = self.state({"book_selection_mode": "purge_pending", "purge_book_ids": [7]})
        message = self.message("123456")
        result = {"deleted_ids": [7], "referenced_ids": [], "not_archived_ids": [], "missing_ids": []}
        with (
            patch("handlers.admin_books.is_admin", return_value=True),
            patch("handlers.admin_books.consume_otp", return_value=True) as consume,
            patch("handlers.admin_books.purge_archived_books", new_callable=AsyncMock, return_value=result) as purge,
        ):
            await confirm_purge_code(message, state)
        consume.assert_called_once_with(101, ACTION_PURGE_ARCHIVED_BOOKS, "123456")
        purge.assert_awaited_once_with([7])
        state.clear.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
