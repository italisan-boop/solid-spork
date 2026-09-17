import asyncio
from contextlib import ExitStack
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import db.connection as db_connection
import db.schema as schema
import server
from aiogram.fsm.storage.base import StorageKey
from storage.sqlite_storage import SQLiteStorage


class SharedDatabaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "shared.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        schema.initialize_database(self.database_path)

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def test_database_path_must_be_absolute(self):
        with self.assertRaises(RuntimeError):
            schema._resolve_database_path("")
        with self.assertRaises(RuntimeError):
            schema._resolve_database_path("relative.sqlite")

    def test_explicit_database_path_does_not_need_legacy_default(self):
        explicit_path = Path(self._temporary_directory.name) / "explicit.sqlite"
        with patch.object(schema, "DB_PATH", None):
            schema.initialize_database(explicit_path, seed_catalog=False)
            database = schema.connect(explicit_path)
            try:
                self.assertEqual(
                    1,
                    database.execute(
                        "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                        (schema.SCHEMA_VERSION,),
                    ).fetchone()[0],
                )
            finally:
                database.close()
            with self.assertRaisesRegex(RuntimeError, "DATABASE_PATH must point"):
                schema.connect()

    async def test_sync_flask_async_repository_and_fsm_share_one_database(self):
        self.assertEqual(6, len(server.get_books_sync()))

        await db.add_user(101, "buyer")
        connection = schema.connect(self.database_path)
        try:
            user_name = connection.execute(
                "SELECT user_name FROM users WHERE user_id = ?", (101,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual("buyer", user_name)

        key = StorageKey(bot_id=1, chat_id=101, user_id=101)
        storage = SQLiteStorage(str(self.database_path))
        await storage.set_state(key, "checkout")
        await storage.set_data(key, {"book_id": 1})
        await storage.close()

        reopened_storage = SQLiteStorage(str(self.database_path))
        self.assertEqual("checkout", await reopened_storage.get_state(key))
        self.assertEqual({"book_id": 1}, await reopened_storage.get_data(key))
        await reopened_storage.close()

    async def test_clear_referrals_preserves_user_bonuses(self):
        connection = schema.connect(self.database_path)
        try:
            connection.execute("INSERT INTO referrals (referrer_id, referred_id) VALUES (1, 2)")
            connection.execute("INSERT INTO user_bonuses (user_id, bonus_type, amount) VALUES (1, 'percent', 15)")
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(1, await db.clear_referrals())

        connection = schema.connect(self.database_path)
        try:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM referrals").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM user_bonuses").fetchone()[0])
        finally:
            connection.close()

    async def test_async_adapter_and_sync_runner_are_idempotent(self):
        schema.initialize_database(self.database_path)
        await db.init_db()
        connection = schema.connect(self.database_path)
        try:
            versions = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                (schema.SCHEMA_VERSION,),
            ).fetchone()[0]
            books = connection.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(1, versions)
        self.assertEqual(6, books)

    async def test_concurrent_initialization_has_one_schema_version(self):
        await asyncio.gather(
            asyncio.to_thread(schema.initialize_database, self.database_path),
            db.init_db(),
        )
        connection = schema.connect(self.database_path)
        try:
            versions = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                (schema.SCHEMA_VERSION,),
            ).fetchone()[0]
            categories = connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(1, versions)
        self.assertEqual(6, categories)


if __name__ == "__main__":
    unittest.main()
