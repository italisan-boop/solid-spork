from contextlib import ExitStack
import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import db.connection as db_connection
import db.schema as schema
import server


TEST_TOKEN = "123456:cart-test-token"


def signed_headers(user_id=101, name="Покупатель"):
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "first_name": name}, separators=(",", ":"), ensure_ascii=False)),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


class PersistentCartApiTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "cart.sqlite"
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
        self._patches.enter_context(patch("server.send_telegram_message", return_value={"ok": True}))
        self._patches.enter_context(patch("server.send_telegram_with_keyboard", return_value={"ok": True}))
        self._patches.enter_context(patch("server.send_stars_invoice", return_value={"ok": True}))
        schema.initialize_database(self.database_path)
        self.client = server.app.test_client()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def _cart(self, user_id=101):
        return self.client.get("/api/cart", headers=signed_headers(user_id))

    def _save(self, cart, revision, user_id=101):
        return self.client.put(
            "/api/cart",
            json={"cart": cart, "revision": revision},
            headers=signed_headers(user_id),
        )

    def _execute(self, query, params=()):
        connection = schema.connect(self.database_path)
        try:
            connection.execute(query, params)
            connection.commit()
        finally:
            connection.close()

    def test_cart_routes_require_signed_telegram_identity(self):
        self.assertEqual(401, self.client.get("/api/cart").status_code)
        self.assertEqual(401, self.client.put("/api/cart", json={"cart": [], "revision": 0}).status_code)

    def test_cart_round_trip_is_isolated_per_signed_user(self):
        initial = self._cart()
        self.assertEqual({"cart": [], "revision": 0}, initial.get_json())
        self.assertEqual("private, no-store", initial.headers["Cache-Control"])

        saved = self._save([{"id": 1, "quantity": 2}, {"id": 2, "quantity": 1}], 0)
        self.assertEqual(200, saved.status_code)
        self.assertEqual(1, saved.get_json()["revision"])

        own_cart = self._cart().get_json()
        self.assertEqual([{"id": 1, "quantity": 2}, {"id": 2, "quantity": 1}], own_cart["cart"])
        self.assertEqual({"cart": [], "revision": 0}, self._cart(202).get_json())

        other_cart = self._save([{"id": 3, "quantity": 4}], 0, 202)
        self.assertEqual(200, other_cart.status_code)
        self.assertEqual(own_cart, self._cart().get_json())

    def test_cart_ignores_forged_identity_and_rejects_invalid_snapshots(self):
        response = self.client.put(
            "/api/cart?user_id=202",
            json={"cart": [{"id": 1, "quantity": 1}], "revision": 0},
            headers=signed_headers(101),
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual([{"id": 1, "quantity": 1}], self._cart(101).get_json()["cart"])
        self.assertEqual([], self._cart(202).get_json()["cart"])

        for invalid in (
            {"cart": [{"id": 1, "quantity": 0}], "revision": 1},
            {"cart": [{"id": 1, "quantity": 1}, {"id": 1, "quantity": 2}], "revision": 1},
            {"cart": [{"id": 1, "quantity": 1, "title": "forged"}], "revision": 1},
            {"cart": [{"id": 999, "quantity": 1}], "revision": 1},
            {"cart": [], "revision": -1},
        ):
            response = self.client.put("/api/cart", json=invalid, headers=signed_headers())
            self.assertEqual(400, response.status_code)

        self.assertEqual([{"id": 1, "quantity": 1}], self._cart().get_json()["cart"])

    def test_cart_rejects_stale_revision_without_overwrite(self):
        first = self._save([{"id": 1, "quantity": 1}], 0)
        self.assertEqual(200, first.status_code)

        stale = self._save([{"id": 2, "quantity": 1}], 0)
        self.assertEqual(409, stale.status_code)
        self.assertEqual([{"id": 1, "quantity": 1}], stale.get_json()["cart"])
        self.assertEqual(1, stale.get_json()["revision"])
        self.assertEqual([{"id": 1, "quantity": 1}], self._cart().get_json()["cart"])

    def test_cart_rejects_quantity_above_finite_availability_with_canonical_snapshot(self):
        self._execute("UPDATE books SET stock_quantity = 3 WHERE id = 1")
        saved = self._save([{"id": 1, "quantity": 3}], 0)
        self.assertEqual(200, saved.status_code)

        rejected = self._save([{"id": 1, "quantity": 4}], 1)

        self.assertEqual(409, rejected.status_code)
        self.assertEqual("inventory_changed", rejected.get_json()["code"])
        self.assertEqual([{"id": 1, "quantity": 3}], rejected.get_json()["cart"])
        self.assertEqual(1, rejected.get_json()["revision"])
        self.assertEqual([{"id": 1, "quantity": 3}], self._cart().get_json()["cart"])

    def test_cart_rejects_inactive_book_without_replacing_saved_cart(self):
        self.assertEqual(200, self._save([{"id": 1, "quantity": 1}], 0).status_code)
        self._execute("UPDATE books SET is_active = 0 WHERE id = 2")

        response = self._save([{"id": 2, "quantity": 1}], 1)
        self.assertEqual(400, response.status_code)
        self.assertEqual([{"id": 1, "quantity": 1}], self._cart().get_json()["cart"])

    def test_successful_checkout_clears_only_signed_users_cart(self):
        self._execute(
            "UPDATE payment_settings SET setting_value = '0' WHERE setting_key = 'payment_enabled'"
        )
        self._save([{"id": 1, "quantity": 1}], 0, 101)
        self._save([{"id": 2, "quantity": 1}], 0, 202)

        checkout = self.client.post(
            "/order",
            json={
                "cart": [{"id": 1, "quantity": 1}],
                "cart_revision": 1,
                "checkout_key": "00000000-0000-4000-8000-000000000001",
                "payment_method": "none",
                "promo_code": "",
                "delivery": {
                    "method": "sdek_pickup",
                    "recipient_name": "Покупатель",
                    "recipient_phone": "+79991234567",
                    "city": "Москва",
                    "pickup_point": "ПВЗ СДЭК 123",
                },
            },
            headers=signed_headers(101),
        )
        self.assertEqual(200, checkout.status_code)
        self.assertEqual(2, checkout.get_json()["cart_revision"])
        self.assertEqual([], self._cart(101).get_json()["cart"])
        self.assertEqual([{"id": 2, "quantity": 1}], self._cart(202).get_json()["cart"])

    def test_stale_checkout_does_not_clear_newer_cart(self):
        self._execute(
            "UPDATE payment_settings SET setting_value = '0' WHERE setting_key = 'payment_enabled'"
        )
        self._save([{"id": 1, "quantity": 1}], 0)

        response = self.client.post(
            "/order",
            json={
                "cart": [{"id": 1, "quantity": 1}],
                "cart_revision": 0,
                "checkout_key": "00000000-0000-4000-8000-000000000002",
                "payment_method": "none",
                "promo_code": "",
                "delivery": {
                    "method": "sdek_pickup",
                    "recipient_name": "Покупатель",
                    "recipient_phone": "+79991234567",
                    "city": "Москва",
                    "pickup_point": "ПВЗ СДЭК 123",
                },
            },
            headers=signed_headers(),
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual([{"id": 1, "quantity": 1}], self._cart().get_json()["cart"])

        self._save([{"id": 1, "quantity": 1}], 0)

        response = self.client.post(
            "/order",
            json={
                "cart": [],
                "cart_revision": 1,
                "checkout_key": "00000000-0000-4000-8000-000000000003",
                "payment_method": "none",
                "promo_code": "",
                "delivery": {
                    "method": "sdek_pickup",
                    "recipient_name": "Покупатель",
                    "recipient_phone": "+79991234567",
                    "city": "Москва",
                    "pickup_point": "ПВЗ СДЭК 123",
                },
            },
            headers=signed_headers(),
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual([{"id": 1, "quantity": 1}], self._cart().get_json()["cart"])


if __name__ == "__main__":
    unittest.main()
