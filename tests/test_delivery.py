import hashlib
import hmac
import json
import tempfile
import time
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import db.connection as db_connection
import db.schema as schema
import server
from db.deliveries import (
    DeliveryTransitionError,
    METHOD_RUSSIAN_POST_PICKUP,
    METHOD_SELF_PICKUP,
    SHIPMENT_DELIVERED,
    SHIPMENT_PACKED,
    SHIPMENT_PREPARING,
    SHIPMENT_READY_FOR_PICKUP,
    SHIPMENT_SHIPPED,
    normalize_russian_post_tracking_number,
    redact_expired_delivery_pii_sync,
    sdek_tracking_url,
    transition_delivery_sync,
)
from db.orders import complete_delivery_and_order_sync, transition_order_status_sync
from utils.delivery_crypto import DeliveryCryptoError, decrypt_destination, encrypt_destination


TEST_TOKEN = "123456:delivery-test-token"
TEST_KEY = "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="
DEFAULT_DELIVERY = object()
OMIT_DELIVERY = object()


def signed_headers(user_id=101, name="Покупатель"):
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "first_name": name}, ensure_ascii=False, separators=(",", ":"))),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "delivery.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(server, "BOT_TOKEN", TEST_TOKEN))
        self._patches.enter_context(patch.object(server.settings, "DELIVERY_ENCRYPTION_ACTIVE_KEY_ID", "test"))
        self._patches.enter_context(
            patch.object(server.settings, "DELIVERY_ENCRYPTION_KEYS_JSON", json.dumps({"test": TEST_KEY}))
        )
        self._patches.enter_context(patch("server.send_telegram_message", return_value={"ok": True}))
        self._patches.enter_context(patch("server.send_telegram_with_keyboard", return_value={"ok": True}))
        self._patches.enter_context(patch("server.send_stars_invoice", return_value={"ok": True}))
        schema.initialize_database(self.database_path)
        connection = schema.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE payment_settings SET setting_value = '1' WHERE setting_key = 'delivery_enabled'"
            )
            connection.commit()
        finally:
            connection.close()
        self.client = server.app.test_client()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    @staticmethod
    def delivery_payload():
        return {
            "method": "sdek_pickup",
            "recipient_name": "Анна Покупатель",
            "recipient_phone": "+7 (999) 123-45-67",
            "city": "Москва",
            "pickup_point": "ПВЗ СДЭК, Тестовый проспект 1",
        }

    def checkout(self, *, user_id=101, checkout_key=None, delivery=DEFAULT_DELIVERY):
        connection = schema.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE payment_settings SET setting_value = '0' WHERE setting_key = 'payment_enabled'"
            )
            connection.commit()
        finally:
            connection.close()
        payload = {
            "cart": [{"id": 1, "quantity": 1}],
            "cart_revision": 0,
            "checkout_key": checkout_key or str(uuid.uuid4()),
            "payment_method": "none",
            "promo_code": "",
        }
        if delivery is not OMIT_DELIVERY:
            payload["delivery"] = self.delivery_payload() if delivery is DEFAULT_DELIVERY else delivery
        return self.client.post("/order", headers=signed_headers(user_id), json=payload)

    def query(self, statement, params=()):
        connection = schema.connect(self.database_path)
        try:
            return connection.execute(statement, params).fetchall()
        finally:
            connection.close()

    def set_delivery_settings(self, **values):
        connection = schema.connect(self.database_path)
        try:
            connection.executemany(
                "UPDATE payment_settings SET setting_value = ? WHERE setting_key = ?",
                [(value, key) for key, value in values.items()],
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def russian_post_payload():
        return {
            "method": "russian_post_pickup",
            "recipient_name": "Анна Покупатель",
            "recipient_phone": "+7 (999) 123-45-67",
            "city": "Москва",
            "post_office": "101000, отделение Почты России",
        }

    def test_destination_encryption_is_bound_to_order_and_method(self):
        destination = {
            "recipient_name": "Анна",
            "recipient_phone": "+79991234567",
            "city": "Москва",
            "pickup_point": "ПВЗ 123",
        }
        first = encrypt_destination(1, "sdek_pickup", destination)
        second = encrypt_destination(1, "sdek_pickup", destination)
        self.assertNotEqual(first, second)
        self.assertEqual(destination, decrypt_destination(1, "sdek_pickup", first))
        with self.assertRaises(DeliveryCryptoError):
            decrypt_destination(2, "sdek_pickup", first)
        with self.assertRaises(DeliveryCryptoError):
            decrypt_destination(1, "other_method", first)

    def test_checkout_adds_fixed_delivery_and_never_exposes_destination(self):
        response = self.checkout()
        self.assertEqual(200, response.status_code)
        result = response.get_json()
        self.assertEqual(1200, result["items_total"])
        self.assertEqual(500, result["delivery_price"])
        self.assertEqual(1700, result["final_total"])
        self.assertEqual("sdek_pickup", result["delivery"]["method"])
        self.assertNotIn("Анна", json.dumps(result, ensure_ascii=False))

        encrypted = self.query("SELECT destination_encrypted FROM order_deliveries")[0][0]
        self.assertNotIn("Анна", encrypted)
        self.assertNotIn("Тестовый", encrypted)
        self.assertEqual("Анна Покупатель", decrypt_destination(1, "sdek_pickup", encrypted)["recipient_name"])

        orders = self.client.get("/api/orders", headers=signed_headers()).get_json()
        serialized = json.dumps(orders, ensure_ascii=False)
        self.assertNotIn("Анна", serialized)
        self.assertNotIn("pickup_point", serialized)
        self.assertEqual("private, no-store", self.client.get("/api/orders", headers=signed_headers()).headers["Cache-Control"])

    def test_delivery_validation_and_encryption_failure_rollback_checkout(self):
        missing = self.checkout(delivery=OMIT_DELIVERY)
        self.assertEqual(400, missing.status_code)

        invalid = self.checkout(delivery={"method": "sdek_pickup"})
        self.assertEqual(400, invalid.status_code)

        with patch.object(server.settings, "DELIVERY_ENCRYPTION_KEYS_JSON", ""):
            failed = self.checkout(user_id=202)
        self.assertEqual(503, failed.status_code)
        self.assertEqual(0, self.query("SELECT COUNT(*) FROM orders")[0][0])
        self.assertEqual(0, self.query("SELECT COUNT(*) FROM order_deliveries")[0][0])

    def test_replayed_checkout_key_keeps_original_delivery(self):
        checkout_key = str(uuid.uuid4())
        created = self.checkout(checkout_key=checkout_key)
        self.assertEqual(200, created.status_code)
        replay = self.checkout(
            checkout_key=checkout_key,
            delivery={
                **self.delivery_payload(),
                "city": "Санкт-Петербург",
                "pickup_point": "Другой ПВЗ",
            },
        )
        self.assertEqual(200, replay.status_code)
        result = replay.get_json()
        self.assertTrue(result["reused"])
        self.assertEqual(1200, result["items_total"])
        self.assertEqual(500, result["delivery_price"])
        self.assertEqual(1, self.query("SELECT COUNT(*) FROM orders")[0][0])
        encrypted = self.query("SELECT destination_encrypted FROM order_deliveries")[0][0]
        destination = decrypt_destination(1, "sdek_pickup", encrypted)
        self.assertEqual("Москва", destination["city"])
        self.assertEqual("ПВЗ СДЭК, Тестовый проспект 1", destination["pickup_point"])

    def test_tracking_link_and_delivery_transitions_are_allowlisted(self):
        created = self.checkout()
        order_id = created.get_json()["order_id"]
        connection = schema.connect(self.database_path)
        connection.row_factory = __import__("sqlite3").Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.assertTrue(transition_delivery_sync(connection, order_id, SHIPMENT_PREPARING, admin_id=1))
            self.assertTrue(transition_delivery_sync(connection, order_id, SHIPMENT_PACKED, admin_id=1))
            self.assertTrue(
                transition_delivery_sync(
                    connection,
                    order_id,
                    SHIPMENT_SHIPPED,
                    admin_id=1,
                    tracking_number="ab-12345",
                )
            )
            self.assertTrue(transition_delivery_sync(connection, order_id, SHIPMENT_READY_FOR_PICKUP, admin_id=1))
            self.assertTrue(transition_delivery_sync(connection, order_id, SHIPMENT_DELIVERED, admin_id=1))
            connection.commit()
        finally:
            connection.close()
        row = self.query("SELECT shipment_status, tracking_carrier, tracking_number FROM order_deliveries")[0]
        self.assertEqual(("delivered", "sdek", "AB-12345"), row)
        self.assertEqual("https://www.cdek.ru/ru/tracking?order_id=AB-12345", sdek_tracking_url("AB-12345"))

    def test_delivered_transition_completes_confirmed_order_atomically(self):
        created = self.checkout()
        order_id = created.get_json()["order_id"]
        self.assertTrue(transition_order_status_sync(order_id, "confirmed"))
        with self.assertRaises(DeliveryTransitionError):
            transition_order_status_sync(order_id, "completed")

        connection = schema.connect(self.database_path)
        connection.row_factory = __import__("sqlite3").Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.assertTrue(transition_delivery_sync(connection, order_id, SHIPMENT_PACKED, admin_id=1))
            self.assertTrue(
                transition_delivery_sync(
                    connection,
                    order_id,
                    SHIPMENT_SHIPPED,
                    admin_id=1,
                    tracking_number="AB-12345",
                )
            )
            self.assertTrue(
                transition_delivery_sync(
                    connection, order_id, SHIPMENT_READY_FOR_PICKUP, admin_id=1
                )
            )
            connection.commit()
        finally:
            connection.close()

        self.assertTrue(complete_delivery_and_order_sync(order_id, 1))
        self.assertEqual(
            ("completed", "delivered"),
            self.query(
                """
                SELECT o.status, d.shipment_status
                FROM orders o JOIN order_deliveries d ON d.order_id = o.id
                WHERE o.id = ?
                """,
                (order_id,),
            )[0],
        )

    def test_delivered_destination_is_redacted_after_ninety_days(self):
        destination = encrypt_destination(77, "sdek_pickup", self.delivery_payload())
        connection = schema.connect(self.database_path)
        try:
            connection.execute("INSERT INTO orders (id, user_id, total) VALUES (77, 101, 1700)")
            connection.execute(
                """
                INSERT INTO order_deliveries (
                    order_id, method, destination_encrypted, delivery_price, shipment_status, delivered_at
                ) VALUES (77, 'sdek_pickup', ?, 500, 'delivered', datetime('now', '-91 days'))
                """,
                (destination,),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(1, redact_expired_delivery_pii_sync())
        row = self.query("SELECT destination_encrypted, pii_redacted_at FROM order_deliveries WHERE order_id = 77")[0]
        self.assertEqual("", row[0])
        self.assertIsNotNone(row[1])

    def test_russian_post_checkout_encrypts_destination_and_tracks_manually(self):
        self.set_delivery_settings(delivery_russian_post_pickup_enabled="1")
        response = self.checkout(delivery=self.russian_post_payload())

        self.assertEqual(200, response.status_code)
        result = response.get_json()
        self.assertEqual(METHOD_RUSSIAN_POST_PICKUP, result["delivery"]["method"])
        self.assertEqual(500, result["delivery_price"])
        self.assertNotIn("Почты России", json.dumps(result, ensure_ascii=False))
        encrypted = self.query("SELECT destination_encrypted FROM order_deliveries")[0][0]
        self.assertNotIn("101000", encrypted)
        self.assertEqual(
            "101000, отделение Почты России",
            decrypt_destination(1, METHOD_RUSSIAN_POST_PICKUP, encrypted)["post_office"],
        )

        connection = schema.connect(self.database_path)
        connection.row_factory = __import__("sqlite3").Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.assertTrue(transition_delivery_sync(connection, 1, SHIPMENT_PREPARING, admin_id=1))
            self.assertTrue(transition_delivery_sync(connection, 1, SHIPMENT_PACKED, admin_id=1))
            self.assertTrue(
                transition_delivery_sync(
                    connection,
                    1,
                    SHIPMENT_SHIPPED,
                    admin_id=1,
                    tracking_number="12345678901234",
                )
            )
            connection.commit()
        finally:
            connection.close()
        summary = self.client.get("/api/orders", headers=signed_headers()).get_json()["orders"][0]["delivery"]
        self.assertEqual("russian_post", summary["tracking"]["carrier"])
        self.assertEqual("12345678901234", summary["tracking"]["number"])
        self.assertEqual("https://www.pochta.ru/tracking", summary["tracking"]["url"])
        with self.assertRaises(ValueError):
            normalize_russian_post_tracking_number("RA123456789RU")

    def test_self_pickup_snapshots_public_location_and_skips_tracking(self):
        self.set_delivery_settings(
            delivery_self_pickup_enabled="1",
            delivery_self_pickup_location="Москва, Тестовая улица, 1",
            delivery_self_pickup_schedule="Пн–Пт 10:00–18:00",
            delivery_self_pickup_instructions="Назовите номер заказа на стойке.",
        )
        response = self.checkout(delivery={"method": METHOD_SELF_PICKUP})

        self.assertEqual(200, response.status_code)
        result = response.get_json()
        self.assertEqual(METHOD_SELF_PICKUP, result["delivery"]["method"])
        self.assertEqual(0, result["delivery_price"])
        self.assertIn("Тестовая улица", result["delivery"]["public_instructions"])
        row = self.query(
            "SELECT tracking_carrier, tracking_number, public_instructions_snapshot FROM order_deliveries"
        )[0]
        self.assertEqual(("none", None), row[:2])
        self.assertIn("Пн–Пт", row[2])

        connection = schema.connect(self.database_path)
        connection.row_factory = __import__("sqlite3").Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.assertTrue(transition_delivery_sync(connection, 1, SHIPMENT_PREPARING, admin_id=1))
            self.assertTrue(transition_delivery_sync(connection, 1, SHIPMENT_PACKED, admin_id=1))
            with self.assertRaises(DeliveryTransitionError):
                transition_delivery_sync(
                    connection,
                    1,
                    SHIPMENT_SHIPPED,
                    admin_id=1,
                    tracking_number="AB-12345",
                )
            self.assertTrue(transition_delivery_sync(connection, 1, SHIPMENT_READY_FOR_PICKUP, admin_id=1))
            connection.commit()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
