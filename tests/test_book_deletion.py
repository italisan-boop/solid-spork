import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import db.connection as db_connection
import db.schema as schema
from db.books import archive_books, get_archived_books, purge_archived_books, restore_book


class BookDeletionRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "books.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        schema.initialize_database(self.database_path)

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def _execute(self, query, params=()):
        connection = schema.connect(self.database_path)
        try:
            cursor = connection.execute(query, params)
            connection.commit()
            return cursor.lastrowid
        finally:
            connection.close()

    def _book(self, title):
        return self._execute(
            "INSERT INTO books (title, price, category) VALUES (?, 100, 'Test')",
            (title,),
        )

    def _book_state(self, book_id):
        connection = schema.connect(self.database_path)
        try:
            return connection.execute(
                "SELECT is_active, is_archived FROM books WHERE id = ?", (book_id,)
            ).fetchone()
        finally:
            connection.close()

    async def test_bulk_archive_only_changes_active_books(self):
        first = self._book("first")
        second = self._book("second")
        self._execute("UPDATE books SET is_archived = 1, is_active = 0 WHERE id = ?", (second,))

        result = await archive_books([first, second, 999999])

        self.assertEqual([first], result["archived_ids"])
        self.assertEqual([second, 999999], result["skipped_ids"])
        self.assertEqual((0, 1), self._book_state(first))
        self.assertEqual((0, 1), self._book_state(second))
        archived = await get_archived_books()
        self.assertIn(first, {book["id"] for book in archived})

    async def test_purge_removes_only_archived_books_without_orders(self):
        eligible = self._book("eligible")
        referenced = self._book("referenced")
        active = self._book("active")
        await archive_books([eligible, referenced])
        order_id = self._execute(
            "INSERT INTO orders (user_id, user_name, total, status) VALUES (1, 'buyer', 100, 'new')"
        )
        self._execute(
            "INSERT INTO order_items (order_id, book_id, title, price) VALUES (?, ?, 'referenced', 100)",
            (order_id, referenced),
        )

        result = await purge_archived_books([eligible, referenced, active, 999999])

        self.assertEqual([eligible], result["deleted_ids"])
        self.assertEqual([referenced], result["referenced_ids"])
        self.assertEqual([active], result["not_archived_ids"])
        self.assertEqual([999999], result["missing_ids"])
        self.assertIsNone(self._book_state(eligible))
        self.assertEqual((0, 1), self._book_state(referenced))
        self.assertEqual((1, 0), self._book_state(active))

    async def test_restore_and_purge_input_validation(self):
        book_id = self._book("restore")
        await archive_books([book_id])
        self.assertTrue(await restore_book(book_id))
        self.assertFalse(await restore_book(book_id))
        with self.assertRaises(ValueError):
            await purge_archived_books([book_id, 0])
        self.assertEqual((1, 0), self._book_state(book_id))

    def test_schema_creates_book_order_items_index(self):
        connection = sqlite3.connect(self.database_path)
        try:
            indexes = {
                row[1]
                for row in connection.execute("PRAGMA index_list(order_items)")
            }
        finally:
            connection.close()
        self.assertIn("idx_order_items_book_id", indexes)


if __name__ == "__main__":
    unittest.main()
