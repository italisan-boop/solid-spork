import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import db.connection as db_connection
import db.schema as schema
from db.books import (
    find_book_by_title_author,
    get_all_books_paginated,
    get_books_count,
    move_book_sync,
)


class BookCatalogManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "catalog.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        schema.initialize_database(self.database_path, seed_catalog=False)

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def _book(self, title, *, author="", sort_order=0, active=1, archived=0):
        database = schema.connect(self.database_path)
        try:
            cursor = database.execute(
                """
                INSERT INTO books (title, price, category, author, sort_order, is_active, is_archived)
                VALUES (?, 100, 'Test', ?, ?, ?, ?)
                """,
                (title, author, sort_order, active, archived),
            )
            database.commit()
            return cursor.lastrowid
        finally:
            database.close()

    def _order(self):
        database = schema.connect(self.database_path)
        try:
            return database.execute(
                """
                SELECT id, sort_order FROM books
                WHERE is_active = 1 AND COALESCE(is_archived, 0) = 0
                ORDER BY sort_order ASC, id ASC
                """
            ).fetchall()
        finally:
            database.close()

    async def test_title_search_and_duplicate_lookup_casefold_cyrillic_and_escape_wildcards(self):
        matching = self._book("100%_\\ Русская Книга", author="Иван ИВАНОВ")
        self._book("100aZ Русская книга", author="Другой автор")

        duplicate = await find_book_by_title_author(" 100%_\\ русская книга ", "иван иванов")
        self.assertEqual(matching, duplicate["id"])

        self.assertEqual(2, await get_books_count(search_query="РУССКАЯ КНИГА"))
        matching_books = await get_all_books_paginated(limit=10, search_query="русская книга")
        self.assertEqual([matching], [book["id"] for book in matching_books[:1]])
        self.assertEqual(2, len(matching_books))

        literal_query = "100%_\\"
        self.assertEqual(1, await get_books_count(search_query=literal_query))
        literal_books = await get_all_books_paginated(limit=1, search_query=literal_query)
        self.assertEqual([matching], [book["id"] for book in literal_books])

    async def test_order_moves_are_transactional_and_normalize_active_catalog_only(self):
        first = self._book("first", sort_order=0)
        second = self._book("second", sort_order=0)
        third = self._book("third", sort_order=20)
        fourth = self._book("fourth", sort_order=20)
        archived = self._book("archived", sort_order=900, active=0, archived=1)

        result = move_book_sync(third, "top", actor_user_id=1)
        self.assertTrue(result["moved"])
        self.assertEqual([third, first, second, fourth], result["book_ids"])
        self.assertEqual(
            [(third, 0), (first, 1), (second, 2), (fourth, 3)], self._order()
        )

        result = move_book_sync(second, "up", actor_user_id=1)
        self.assertTrue(result["moved"])
        self.assertEqual([third, second, first, fourth], result["book_ids"])

        result = move_book_sync(third, "bottom", actor_user_id=1)
        self.assertTrue(result["moved"])
        self.assertEqual([second, first, fourth, third], result["book_ids"])

        result = move_book_sync(third, "down", actor_user_id=1)
        self.assertFalse(result["moved"])
        self.assertEqual([second, first, fourth, third], result["book_ids"])

        unavailable = move_book_sync(archived, "top", actor_user_id=1)
        self.assertFalse(unavailable["found"])
        database = schema.connect(self.database_path)
        try:
            self.assertEqual(
                (900, 0, 1),
                database.execute(
                    "SELECT sort_order, is_active, is_archived FROM books WHERE id = ?",
                    (archived,),
                ).fetchone(),
            )
            events = database.execute(
                "SELECT action, entity_id, details_json FROM audit_events ORDER BY id"
            ).fetchall()
        finally:
            database.close()
        self.assertEqual(3, len(events))
        self.assertEqual(("catalog.position.updated", str(third)), events[0][:2])
        self.assertIn('"reason_code":"top"', events[0][2])

    async def test_boundary_normalizes_legacy_ties_without_audit_event(self):
        first = self._book("first", sort_order=0)
        second = self._book("second", sort_order=0)
        third = self._book("third", sort_order=0)

        result = move_book_sync(first, "top", actor_user_id=1)
        self.assertFalse(result["moved"])
        self.assertTrue(result["normalized"])
        self.assertEqual([(first, 0), (second, 1), (third, 2)], self._order())
        database = schema.connect(self.database_path)
        try:
            self.assertEqual(0, database.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
        finally:
            database.close()


if __name__ == "__main__":
    unittest.main()
