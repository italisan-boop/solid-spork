import csv
import hashlib
import hmac
import io
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from openpyxl import Workbook

import db.connection as db_connection
import db.schema as schema
import server
from config import settings


TEST_TOKEN = "123456:book-import-test-token"
OWNER_ID = 101


def signed_headers(user_id: int = OWNER_ID) -> dict[str, str]:
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":"))),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


HEADERS = [
    "book_id", "title", "author", "description", "price_rub", "category", "stock_mode", "stock_quantity"
]


class BookImportApiTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "imports.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(server, "BOT_TOKEN", TEST_TOKEN))
        self._patches.enter_context(patch.object(server, "ADMIN_IDS", []))
        self._patches.enter_context(patch.object(settings, "OWNER_TELEGRAM_ID", OWNER_ID))
        schema.initialize_database(self.database_path)
        self.client = server.app.test_client()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def preview(self, filename: str, content: bytes, user_id: int = OWNER_ID):
        return self.client.post(
            "/api/admin/books/import/preview",
            headers=signed_headers(user_id),
            data={"file": (io.BytesIO(content), filename)},
            content_type="multipart/form-data",
        )

    def csv_content(self, rows, *, delimiter=","):
        output = io.StringIO()
        writer = csv.writer(output, delimiter=delimiter)
        writer.writerow(HEADERS)
        writer.writerows(rows)
        return output.getvalue().encode()

    def test_semicolon_csv_preview_is_supported(self):
        preview = self.preview("books.csv", self.csv_content([[
            "", "Книга; с подзаголовком", "Автор", "Описание", "750", "Ботаника", "finite", "7"
        ]], delimiter=";"))
        self.assertEqual(200, preview.status_code)
        self.assertEqual(1, preview.get_json()["valid_rows"])

    def test_csv_preview_is_server_owned_and_commits_stock_once(self):
        preview = self.preview("books.csv", self.csv_content([[
            "", "Импортированная книга", "Автор", "Описание", "750", "Ботаника", "finite", "7"
        ]]))
        self.assertEqual(200, preview.status_code)
        result = preview.get_json()
        self.assertEqual(1, result["valid_rows"])
        batch_id = result["batch_id"]

        committed = self.client.post(
            f"/api/admin/books/import/{batch_id}/commit", headers=signed_headers()
        )
        self.assertEqual(200, committed.status_code)
        self.assertEqual({"batch_id": batch_id, "created": 1, "updated": 0}, committed.get_json())
        replayed = self.client.post(
            f"/api/admin/books/import/{batch_id}/commit", headers=signed_headers()
        )
        self.assertEqual(committed.get_json(), replayed.get_json())

        connection = schema.connect(self.database_path)
        try:
            book = connection.execute(
                "SELECT id, stock_quantity FROM books WHERE title = 'Импортированная книга'"
            ).fetchone()
            movements = connection.execute(
                "SELECT action, stock_delta FROM inventory_movements WHERE book_id = ?", (book[0],)
            ).fetchall()
            audit_actions = connection.execute("SELECT action FROM audit_events").fetchall()
        finally:
            connection.close()
        self.assertEqual(7, book[1])
        self.assertEqual([("opening_balance", 7)], movements)
        self.assertIn(("catalog.import.committed",), audit_actions)

    def test_invalid_preview_never_creates_books_and_xlsx_is_supported(self):
        invalid = self.preview("books.csv", self.csv_content([[
            "", "=formula", "Автор", "", "100", "Ботаника", "finite", "1"
        ]]))
        self.assertEqual(200, invalid.status_code)
        self.assertEqual(1, invalid.get_json()["invalid_rows"])

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(HEADERS)
        sheet.append(["", "XLSX книга", "Автор", "", 900, "Ботаника", "unlimited", ""])
        payload = io.BytesIO()
        workbook.save(payload)
        xlsx = self.preview("books.xlsx", payload.getvalue())
        self.assertEqual(200, xlsx.status_code)
        self.assertEqual(1, xlsx.get_json()["valid_rows"])

        connection = schema.connect(self.database_path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM books WHERE title = '=formula'").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, count)
        self.assertEqual(403, self.preview("books.csv", self.csv_content([]), user_id=202).status_code)


if __name__ == "__main__":
    unittest.main()
