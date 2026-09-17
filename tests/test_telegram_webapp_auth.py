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
        self._patches.enter_context(patch.object(server.settings, "DELIVERY_ENCRYPTION_ACTIVE_KEY_ID", "test"))
        self._patches.enter_context(
            patch.object(
                server.settings,
                "DELIVERY_ENCRYPTION_KEYS_JSON",
                '{"test":"eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="}',
            )
        )
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
        for path in ("/order", "/api/cart", "/api/orders", "/api/validate-promo", "/api/admin/dashboard", "/api/admin/export/orders", "/api/admin/export/books", "/api/admin/export/sales"):
            method = self.client.post if path in {"/order", "/api/validate-promo"} else self.client.get
            response = method(path, json={"cart": []} if path == "/order" else {"code": "SAVE", "total": 100} if path == "/api/validate-promo" else None)
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

    def test_analytics_and_campaign_routes_enforce_signed_admin_access(self):
        for path in (
            "/api/admin/analytics",
            "/api/admin/analytics/referrals",
            "/api/admin/export/analytics?dimension=referral",
            "/api/admin/campaigns",
        ):
            self.assertEqual(401, self.client.get(path).status_code, path)
            self.assertEqual(403, self.client.get(path, headers=self._headers(999)).status_code, path)

        campaign_payload = {"code": "vk_september", "channel": "vk", "source": "community", "campaign": "september_2026"}
        self.assertEqual(401, self.client.put("/api/admin/campaigns", json=campaign_payload).status_code)
        self.assertEqual(403, self.client.put(
            "/api/admin/campaigns", json=campaign_payload, headers=self._headers(999)
        ).status_code)
        created = self.client.put(
            "/api/admin/campaigns",
            headers=self._headers(1, "Admin"),
            json=campaign_payload,
        )
        self.assertEqual(200, created.status_code)
        self.assertEqual("c_vk_september", created.get_json()["deep_link_payload"])
        self.assertEqual("private, no-store", created.headers["Cache-Control"])
        listed = self.client.get("/api/admin/campaigns", headers=self._headers(1, "Admin"))
        self.assertEqual(200, listed.status_code)
        self.assertEqual("vk_september", listed.get_json()["campaigns"][0]["code"])

    def test_order_uses_signed_identity_not_forged_body_values(self):
        response = self.client.post(
            "/order",
            headers={**self._headers(101, "Signed User"), "Content-Type": "application/json"},
            json={
                "user_id": 999,
                "user_name": "Forged User",
                "cart": [{"id": 1, "quantity": 1}],
                "cart_revision": 0,
                "checkout_key": "00000000-0000-4000-8000-000000000004",
                "payment_method": "manual",
                "promo_code": "",
                "delivery": {
                    "method": "sdek_pickup",
                    "recipient_name": "Signed User",
                    "recipient_phone": "+79991234567",
                    "city": "Москва",
                    "pickup_point": "ПВЗ СДЭК 123",
                },
            },
        )
        self.assertEqual(200, response.status_code)
        connection = schema.connect(self.database_path)
        try:
            order = connection.execute("SELECT user_id, user_name FROM orders").fetchone()
        finally:
            connection.close()
        self.assertEqual((101, "Signed User"), order)

    def test_bot_identity_returns_only_validated_username_and_rekeys_cache(self):
        server._BOT_USERNAME_CACHE = None
        first = type("Response", (), {"json": lambda self: {"ok": True, "result": {"username": "booksseed_bot", "id": 123}}})()
        second = type("Response", (), {"json": lambda self: {"ok": True, "result": {"username": "aehgxjb_bot", "id": 456}}})()
        with patch("server.requests.get", side_effect=[first, second]) as get_me:
            response = self.client.get("/api/app/bot-identity", headers=self._headers(101))
            self.assertEqual(200, response.status_code)
            self.assertEqual({"username": "booksseed_bot"}, response.get_json())
            self.assertEqual("private, no-store", response.headers["Cache-Control"])
            cached = self.client.get("/api/app/bot-identity", headers=self._headers(101))
            self.assertEqual({"username": "booksseed_bot"}, cached.get_json())
            with patch.object(server, "BOT_TOKEN", "123456:replacement-token"):
                changed = self.client.get("/api/app/bot-identity", headers={"X-Telegram-Init-Data": make_init_data(user={"id": 101, "first_name": "User"}, token="123456:replacement-token")})
        self.assertEqual({"username": "aehgxjb_bot"}, changed.get_json())
        self.assertEqual(2, get_me.call_count)

    def test_bot_identity_hides_token_when_telegram_is_unavailable(self):
        server._BOT_USERNAME_CACHE = None
        with patch("server.requests.get", side_effect=server.requests.RequestException("secret failure")):
            response = self.client.get("/api/app/bot-identity", headers=self._headers(101))
        self.assertEqual(503, response.status_code)
        self.assertNotIn(TEST_TOKEN, response.get_data(as_text=True))
        self.assertNotIn("secret failure", response.get_data(as_text=True))

    def test_mini_app_uses_dynamic_bot_identity(self):
        source = Path(server.app.static_folder, "Index.html").read_text(encoding="utf-8")
        self.assertNotIn("const BOT_USERNAME", source)
        self.assertNotIn("bookseed_bot", source)
        self.assertNotIn("booksseed_bot", source)
        self.assertIn("/api/app/bot-identity", source)

    def test_public_catalog_stays_available_without_init_data(self):
        response = self.client.get("/api/books")
        self.assertEqual(200, response.status_code)

    def test_favicon_is_not_reported_as_missing(self):
        response = self.client.get("/favicon.ico")
        self.assertEqual(204, response.status_code)
        self.assertEqual("public, max-age=86400", response.headers["Cache-Control"])


if __name__ == "__main__":
    unittest.main()
