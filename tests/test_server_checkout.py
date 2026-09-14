import uuid
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


TEST_TOKEN = "123456:checkout-test-token"


def signed_headers(user_id=101, name="Покупатель"):
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "first_name": name}, separators=(",", ":"), ensure_ascii=False)),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


class CheckoutApiTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "checkout.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(server, "BOT_TOKEN", TEST_TOKEN))
        self._send_message = self._patches.enter_context(
            patch("server.send_telegram_message", return_value={"ok": True})
        )
        self._send_keyboard = self._patches.enter_context(
            patch("server.send_telegram_with_keyboard", return_value={"ok": True})
        )
        self._send_invoice = self._patches.enter_context(
            patch("server.send_stars_invoice", return_value={"ok": True})
        )
        schema.initialize_database(self.database_path)
        self.client = server.app.test_client()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def _execute(self, query, params=()):
        connection = schema.connect(self.database_path)
        try:
            connection.execute(query, params)
            connection.commit()
        finally:
            connection.close()

    def _scalar(self, query, params=()):
        connection = schema.connect(self.database_path)
        try:
            return connection.execute(query, params).fetchone()[0]
        finally:
            connection.close()

    def _settings(self, *, card=True, stars=False, rate=2):
        self._execute(
            "UPDATE payment_settings SET setting_value = ? WHERE setting_key = 'payment_enabled'",
            ("1" if card else "0",),
        )
        self._execute(
            "UPDATE stars_settings SET setting_value = ? WHERE setting_key = 'stars_enabled'",
            ("1" if stars else "0",),
        )
        self._execute(
            "UPDATE stars_settings SET setting_value = ? WHERE setting_key = 'rubles_per_star'",
            (str(rate),),
        )

    def _order(self, **overrides):
        payload = {
            "cart": [{"id": 1, "title": "Подмена", "price": 1, "quantity": 2}],
            "cart_revision": 0,
            "checkout_key": str(uuid.uuid4()),
            "payment_method": "manual",
            "promo_code": "",
        }
        payload.update(overrides)
        return self.client.post("/order", json=payload, headers=signed_headers())

    def test_rejects_invalid_checkout_payload_without_creating_order(self):
        response = self.client.post("/order", json={"cart": [], "cart_revision": 0}, headers=signed_headers())
        self.assertEqual(400, response.status_code)
        self.assertEqual(0, self._scalar("SELECT COUNT(*) FROM orders"))

        response = self._order(cart=[{"id": 1, "quantity": 0}])
        self.assertEqual(400, response.status_code)
        response = self._order(cart=[{"id": 99999, "quantity": 1}])
        self.assertEqual(400, response.status_code)
        self.assertEqual(0, self._scalar("SELECT COUNT(*) FROM orders"))

    def test_checkout_uses_catalog_price_and_persists_quantity(self):
        self._settings(card=False)
        response = self._order(payment_method="none")
        self.assertEqual(200, response.status_code)
        result = response.get_json()
        self.assertEqual("none", result["payment_method"])
        self.assertFalse(result["payment_required"])
        self.assertEqual(2400, result["final_total"])

        connection = schema.connect(self.database_path)
        try:
            order = connection.execute("SELECT total, status FROM orders").fetchone()
            items = connection.execute("SELECT title, price FROM order_items").fetchall()
        finally:
            connection.close()
        self.assertEqual((2400, "new"), order)
        self.assertEqual(2, len(items))
        self.assertTrue(all(item[1] == 1200 for item in items))
        self.assertTrue(all(item[0] != "Подмена" for item in items))
        self._send_message.assert_called_once()

    def test_promo_is_consumed_with_order_transaction(self):
        self._settings(card=False)
        self._execute(
            """
            INSERT INTO promo_codes (code, discount_percent, max_uses, current_uses)
            VALUES ('SAVE10', 10, 1, 0)
            """
        )
        response = self._order(promo_code="save10", payment_method="none")
        self.assertEqual(200, response.status_code)
        result = response.get_json()
        self.assertEqual(240, result["discount"])
        self.assertEqual(2160, result["final_total"])
        self.assertEqual("SAVE10", result["applied_promo"])
        self.assertEqual(1, self._scalar(
            "SELECT current_uses FROM promo_codes WHERE code = 'SAVE10'"
        ))

    def test_rechecks_book_availability_inside_checkout_transaction(self):
        self._settings(card=False)
        with patch("server._checkout_cart_is_available", return_value=False):
            response = self._order(payment_method="none")

        self.assertEqual(400, response.status_code)
        self.assertEqual("book is unavailable", response.get_json()["error"])
        self.assertEqual(0, self._scalar("SELECT COUNT(*) FROM orders"))

    def test_card_and_stars_contracts(self):
        self._settings(card=True, stars=False)
        card_result = self._order().get_json()
        self.assertEqual("manual", card_result["payment_method"])
        self.assertTrue(card_result["payment_required"])
        self.assertIn("payment_info", card_result)
        self._send_keyboard.assert_called_once()

        self._execute("UPDATE books SET price = 1201 WHERE id = 1")
        self._settings(card=True, stars=True, rate=2)
        stars_result = self._order(
            cart=[{"id": 1, "quantity": 1}],
            cart_revision=card_result["cart_revision"],
            payment_method="stars",
        ).get_json()
        self.assertEqual("stars", stars_result["payment_method"])
        self.assertFalse(stars_result["payment_required"])
        self.assertEqual(601, stars_result["stars_amount"])
        self._send_invoice.assert_called_once()


if __name__ == "__main__":
    unittest.main()
