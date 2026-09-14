import asyncio
from contextlib import ExitStack
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import db.schema as schema


class DatabaseInitializationSmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "smoke.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))

    async def asyncTearDown(self):
        await asyncio.sleep(0.1)

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def _query_one(self, query):
        connection = sqlite3.connect(self.database_path)
        try:
            return connection.execute(query).fetchone()[0]
        finally:
            connection.close()

    def _table_names(self):
        connection = sqlite3.connect(self.database_path)
        try:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()

    async def test_fresh_database_initializes_twice_without_locks_or_duplicate_seeds(self):
        await db.init_db()
        await db.init_db()

        self.assertTrue(
            {
                "schema_migrations",
                "users",
                "orders",
                "order_items",
                "books",
                "categories",
                "promo_codes",
                "referrals",
                "user_bonuses",
                "payment_settings",
                "stars_settings",
                "message_templates",
                "fsm_records",
                "mini_app_carts",
            }.issubset(self._table_names())
        )
        self.assertEqual(schema.SCHEMA_VERSION, self._query_one("PRAGMA user_version"))
        self.assertEqual(6, self._query_one("SELECT COUNT(*) FROM categories"))
        self.assertEqual(6, self._query_one("SELECT COUNT(*) FROM books"))
        self.assertEqual(6, self._query_one("SELECT COUNT(*) FROM payment_settings"))
        self.assertEqual(2, self._query_one("SELECT COUNT(*) FROM stars_settings"))

    async def test_legacy_books_migration_initializes_twice_without_locks(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    user_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    emoji TEXT DEFAULT ''
                )
                """
            )
            connection.execute(
                "INSERT INTO books (title, price, category, emoji) VALUES (?, ?, ?, ?)",
                ("Старая книга", 500, "Ботаника", "🌿"),
            )
            connection.commit()
        finally:
            connection.close()

        await db.init_db()
        await db.init_db()

        connection = sqlite3.connect(self.database_path)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(books)")}
            legacy_book = connection.execute(
                "SELECT category_id, created_at FROM books WHERE title = ?",
                ("Старая книга",),
            ).fetchone()
        finally:
            connection.close()

        self.assertTrue({"category_id", "created_at", "description", "images"}.issubset(columns))
        self.assertIsNotNone(legacy_book[0])
        self.assertIsNotNone(legacy_book[1])
        self.assertEqual(6, self._query_one("SELECT COUNT(*) FROM categories"))
        self.assertEqual(6, self._query_one("SELECT COUNT(*) FROM payment_settings"))
        self.assertEqual(2, self._query_one("SELECT COUNT(*) FROM stars_settings"))


if __name__ == "__main__":
    unittest.main()
