import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import db
import db.connection as db_connection
import db.schema as schema


class SupportHistoryRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "support.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        schema.initialize_database(self.database_path)

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    async def test_history_persists_and_dialogs_sort_by_latest_message_id(self):
        await db.append_support_message(200, "user", "Older user", "Первое сообщение")
        await db.append_support_message(100, "user", "Newer user", "Новый диалог")
        await db.append_support_message(200, "admin", "Admin", "Последний ответ")

        history = await db.get_support_messages(200, limit=20)
        self.assertEqual(
            [("user", "Первое сообщение"), ("admin", "Последний ответ")],
            [(entry["role"], entry["text"]) for entry in history],
        )
        dialogs = await db.get_support_dialogs(limit=20)
        self.assertEqual([200, 100], [dialog["user_id"] for dialog in dialogs])
        self.assertEqual("Older user", dialogs[0]["user_name"])
        self.assertEqual("Последний ответ", dialogs[0]["last_text"])
        self.assertEqual(2, await db.get_support_dialog_count())
        self.assertEqual(2, await db.get_support_message_count(200))

    async def test_history_paginates_from_newest_and_returns_each_page_chronologically(self):
        for index in range(10):
            await db.append_support_message(100, "user", "User", f"message {index}")

        newest_page = await db.get_support_messages(100, limit=4, offset=0)
        older_page = await db.get_support_messages(100, limit=4, offset=4)
        self.assertEqual(["message 6", "message 7", "message 8", "message 9"], [entry["text"] for entry in newest_page])
        self.assertEqual(["message 2", "message 3", "message 4", "message 5"], [entry["text"] for entry in older_page])

    async def test_clear_history_and_active_support_do_not_delete_users(self):
        await db.append_support_message(100, "user", "User", "message")
        await db.set_support_active(100, True)

        await db.clear_support_history()
        await db.clear_support_active_users()

        self.assertEqual(0, await db.get_support_dialog_count())
        self.assertFalse(await db.is_support_active(100))
        self.assertEqual(100, (await db.get_user(100))["user_id"])

    async def test_invalid_role_is_rejected(self):
        with self.assertRaises(ValueError):
            await db.append_support_message(100, "system", "Bot", "invalid")

    async def test_schema_cleanup_removes_only_messages_older_than_ninety_days(self):
        connection = schema.connect(self.database_path)
        try:
            connection.execute(
                "INSERT INTO support_messages (user_id, role, sender_name, text, created_at) VALUES (1, 'user', 'Old', 'expired', datetime('now', '-91 days'))"
            )
            connection.execute(
                "INSERT INTO support_messages (user_id, role, sender_name, text, created_at) VALUES (2, 'user', 'Fresh', 'keep', datetime('now', '-90 days'))"
            )
            connection.execute("INSERT INTO users (user_id, user_name) VALUES (1, 'Old')")
            connection.commit()
        finally:
            connection.close()

        schema.initialize_database(self.database_path)

        connection = sqlite3.connect(self.database_path)
        try:
            messages = connection.execute(
                "SELECT user_id, text FROM support_messages ORDER BY user_id"
            ).fetchall()
            user = connection.execute("SELECT user_name FROM users WHERE user_id = 1").fetchone()
        finally:
            connection.close()
        self.assertEqual([(2, "keep")], messages)
        self.assertEqual(("Old",), user)


if __name__ == "__main__":
    unittest.main()
