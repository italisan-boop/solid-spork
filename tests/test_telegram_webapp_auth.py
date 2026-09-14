import hashlib
import hmac
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import db.connection as db_connection
import db.schema as schema
import server
from telegram_auth import TelegramInitDataError, validate_telegram_init_data


TEST_TOKEN = "123456:unit-test-token"


def make_init_data(*, user, auth_date=None, token=TEST_TOKEN, extra_pairs=()):
    pairs = [
        ("auth_date", str(int(time.time()) if auth_date is None else auth_date)),
        ("query_id", "test-query"),
        ("user", json.dumps(user, separators=(",", ":"), ensure_ascii=False)),
        *extra_pairs,
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode([*pairs, ("hash", signature)])


class TelegramInitDataValidatorTests(unittest.TestCase):
    def test_valid_unicode_init_data_returns_signed_user(self):
        raw = make_init_data(
            user={"id": 101, "first_name": "Анна", "last_name": "Иванова"},
            auth_date=1_700_000_000,
        )
        user = validate_telegram_init_data(raw, TEST_TOKEN, now=1_700_000_100)
        self.assertEqual(101, user.id)
        self.assertEqual("Анна Иванова", user.name)

    def test_tampered_expired_and_duplicate_values_are_rejected(self):
        raw = make_init_data(user={"id": 101, "first_name": "Анна"}, auth_date=1_700_000_000)
        with self.assertRaises(TelegramInitDataError):
            validate_telegram_init_data(raw + "&tampered=1", TEST_TOKEN, now=1_700_000_100)
        with self.assertRaises(TelegramInitDataError) as expired:
            validate_telegram_init_data(raw, TEST_TOKEN, now=1_700_000_000 + 86_401)
        self.assertEqual("expired", expired.exception.kind)
        with self.assertRaises(TelegramInitDataError):
            validate_telegram_init_data(raw + "&user=%7B%7D", TEST_TOKEN, now=1_700_000_100)

    def test_invalid_signed_user_id_is_rejected(self):
        raw = make_init_data(user={"id": "101", "first_name": "Анна"})
        with self.assertRaises(TelegramInitDataError):
            validate_telegram_init_data(raw, TEST_TOKEN)


class TelegramProtectedRoutesTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "auth.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(server, "BOT_TOKEN", TEST_TOKEN))
        self._patches.enter_context(patch.object(server, "ADMIN_IDS", [1]))
        self._patches.enter_context(patch("server.send_telegram_message", return_value={"ok": True}))
        self._patches.enter_context(patch("server.send_telegram_with_keyboard", return_value={"ok": True}))
        self._patches.enter_context(patch("server.send_stars_invoice", return_value={"ok": True}))
        schema.initialize_database(self.database_path)
        self.client = server.app.test_client()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def _headers(self, user_id, name="User", **kwargs):
        raw = make_init_data(user={"id": user_id, "first_name": name}, **kwargs)
        return {"X-Telegram-Init-Data": raw}

    def test_protected_routes_require_init_data(self):
        for path in ("/order", "/api/admin/dashboard", "/api/admin/export/orders", "/api/admin/export/books"):
            method = self.client.post if path == "/order" else self.client.get
            response = method(path, json={"cart": []} if path == "/order" else None)
            self.assertEqual(401, response.status_code, path)

    def test_admin_query_cannot_elevate_signed_non_admin(self):
        response = self.client.get(
            "/api/admin/dashboard?user_id=1", headers=self._headers(999)
        )
        self.assertEqual(403, response.status_code)

    def test_signed_admin_accesses_dashboard_without_query_identity(self):
        response = self.client.get("/api/admin/dashboard", headers=self._headers(1, "Admin"))
        self.assertEqual(200, response.status_code)
        self.assertEqual("private, no-store", response.headers["Cache-Control"])

    def test_order_uses_signed_identity_not_forged_body_values(self):
        response = self.client.post(
            "/order",
            headers={**self._headers(101, "Signed User"), "Content-Type": "application/json"},
            json={
                "user_id": 999,
                "user_name": "Forged User",
                "cart": [{"id": 1, "quantity": 1}],
                "promo_code": "",
            },
        )
        self.assertEqual(200, response.status_code)
        connection = schema.connect(self.database_path)
        try:
            order = connection.execute("SELECT user_id, user_name FROM orders").fetchone()
        finally:
            connection.close()
        self.assertEqual((101, "Signed User"), order)

    def test_public_catalog_stays_available_without_init_data(self):
        response = self.client.get("/api/books")
        self.assertEqual(200, response.status_code)


if __name__ == "__main__":
    unittest.main()
