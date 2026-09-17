from contextlib import ExitStack
import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import db.connection as db_connection
import db.schema as schema
import server


TEST_TOKEN = "123456:yookassa-test-token"


def signed_headers(user_id=101, name="Покупатель"):
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "first_name": name}, separators=(",", ":"), ensure_ascii=False)),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


def payment_response(payment_id="provider-payment-1", status="pending"):
    return {
        "id": payment_id,
        "status": status,
        "confirmation": {"confirmation_url": f"https://payment.example/{payment_id}"},
    }


class YooKassaApiTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "yookassa.sqlite"
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
        self._patches.enter_context(patch.object(server.settings, "YOOKASSA_SHOP_ID", "test-shop"))
        self._patches.enter_context(patch.object(server.settings, "YOOKASSA_SECRET_KEY", "test-secret"))
        self._patches.enter_context(
            patch.object(server.settings, "YOOKASSA_RETURN_URL", "https://shop.example/payments/yookassa/return")
        )
        self._patches.enter_context(patch("server.send_telegram_message", return_value={"ok": True}))
        self._patches.enter_context(patch("server.send_telegram_with_keyboard", return_value={"ok": True}))
        self._patches.enter_context(patch("server.send_stars_invoice", return_value={"ok": True}))
        schema.initialize_database(self.database_path)
        self._execute(
            "UPDATE payment_settings SET setting_value = '1' WHERE setting_key = 'delivery_enabled'"
        )
        self._execute(
            "UPDATE payment_settings SET setting_value = '1' WHERE setting_key = 'yookassa_enabled'"
        )
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

    def _rows(self, query, params=()):
        connection = schema.connect(self.database_path)
        try:
            return connection.execute(query, params).fetchall()
        finally:
            connection.close()

    def _payload(self, checkout_key=None):
        return {
            "cart": [{"id": 1, "quantity": 1}],
            "cart_revision": 0,
            "checkout_key": checkout_key or str(uuid.uuid4()),
            "payment_method": "yookassa",
            "promo_code": "",
            "delivery": {
                "method": "sdek_pickup",
                "recipient_name": "Покупатель",
                "recipient_phone": "+79991234567",
                "city": "Москва",
                "pickup_point": "ПВЗ СДЭК 123",
            },
        }

    def _create_order(self, provider_response, checkout_key=None):
        with patch("server._yookassa_create_payment", return_value=provider_response):
            response = self.client.post("/order", json=self._payload(checkout_key), headers=signed_headers())
        return response

    def test_checkout_options_are_signed_and_only_advertise_configured_methods(self):
        self.assertEqual(401, self.client.get("/api/checkout/options").status_code)
        response = self.client.get("/api/checkout/options", headers=signed_headers())
        self.assertEqual(200, response.status_code)
        self.assertEqual("private, no-store", response.headers["Cache-Control"])
        self.assertEqual(
            {"manual", "yookassa"},
            {method["id"] for method in response.get_json()["methods"]},
        )

    def test_yookassa_checkout_persists_attempt_then_reuses_checkout_key(self):
        checkout_key = str(uuid.uuid4())
        with patch("server._yookassa_create_payment", return_value=payment_response()) as create_payment:
            created = self.client.post("/order", json=self._payload(checkout_key), headers=signed_headers())
            reused = self.client.post("/order", json=self._payload(checkout_key), headers=signed_headers())

        self.assertEqual(200, created.status_code)
        result = created.get_json()
        self.assertEqual("yookassa", result["payment_method"])
        self.assertEqual("awaiting_yookassa_payment", result["status"])
        self.assertEqual("https://payment.example/provider-payment-1", result["confirmation_url"])
        self.assertEqual("1700.00", create_payment.call_args.args[0])
        self.assertEqual(1, self._rows("SELECT COUNT(*) FROM orders")[0][0])
        attempts = self._rows(
            "SELECT idempotence_key, provider_payment_id, amount, currency, status FROM yookassa_payments"
        )
        self.assertEqual(1, len(attempts))
        self.assertEqual(("provider-payment-1", "1700.00", "RUB", "pending"), attempts[0][1:])
        self.assertEqual(200, reused.status_code)
        self.assertTrue(reused.get_json()["reused"])
        self.assertEqual(result["order_id"], reused.get_json()["order_id"])
        create_payment.assert_called_once()

    def test_failed_provider_creation_can_retry_with_same_idempotence_key(self):
        checkout_key = str(uuid.uuid4())
        with patch(
            "server._yookassa_create_payment",
            side_effect=[RuntimeError("temporary failure"), payment_response()],
        ) as create_payment:
            first = self.client.post("/order", json=self._payload(checkout_key), headers=signed_headers())
            second = self.client.post("/order", json=self._payload(checkout_key), headers=signed_headers())

        self.assertEqual(200, first.status_code)
        self.assertEqual("creation_pending", first.get_json()["provider_status"])
        self.assertIn("payment_error", first.get_json())
        self.assertEqual(200, second.status_code)
        self.assertTrue(second.get_json()["reused"])
        self.assertEqual("https://payment.example/provider-payment-1", second.get_json()["confirmation_url"])
        self.assertEqual(1, self._rows("SELECT COUNT(*) FROM orders")[0][0])
        self.assertEqual(1, self._rows("SELECT COUNT(*) FROM yookassa_payments")[0][0])
        self.assertEqual(
            create_payment.call_args_list[0].args[-1],
            create_payment.call_args_list[1].args[-1],
        )

    def test_canceled_attempt_creates_new_attempt_only_via_authenticated_retry(self):
        created = self._create_order(payment_response())
        order_id = created.get_json()["order_id"]
        self._execute("UPDATE yookassa_payments SET status = 'canceled' WHERE order_id = ?", (order_id,))

        with patch(
            "server._yookassa_create_payment",
            return_value=payment_response("provider-payment-2"),
        ) as create_payment:
            retry = self.client.post(
                f"/api/orders/{order_id}/yookassa/retry",
                headers=signed_headers(),
            )

        self.assertEqual(200, retry.status_code)
        self.assertEqual("https://payment.example/provider-payment-2", retry.get_json()["confirmation_url"])
        self.assertEqual(2, self._rows("SELECT COUNT(*) FROM yookassa_payments")[0][0])
        create_payment.assert_called_once()

    def test_webhook_only_marks_matching_canonical_success_as_paid(self):
        created = self._create_order(payment_response())
        order_id = created.get_json()["order_id"]
        attempt_id = self._rows("SELECT id FROM yookassa_payments WHERE order_id = ?", (order_id,))[0][0]
        canonical = {
            "id": "provider-payment-1",
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "1700.00", "currency": "RUB"},
            "metadata": {"order_id": str(order_id), "attempt_id": str(attempt_id)},
        }
        with (
            patch("server._is_yookassa_source", return_value=True),
            patch("server._yookassa_find_payment", return_value=canonical) as find_payment,
        ):
            response = self.client.post("/webhooks/yookassa", json={"object": {"id": "provider-payment-1"}})
            duplicate = self.client.post("/webhooks/yookassa", json={"object": {"id": "provider-payment-1"}})

        self.assertEqual(200, response.status_code)
        self.assertEqual(200, duplicate.status_code)
        self.assertEqual("paid", self._rows("SELECT status FROM orders WHERE id = ?", (order_id,))[0][0])
        self.assertEqual("succeeded", self._rows("SELECT status FROM yookassa_payments")[0][0])
        self.assertEqual(2, find_payment.call_count)

    def test_webhook_rejects_untrusted_source_and_never_fulfills_wrong_amount(self):
        created = self._create_order(payment_response())
        order_id = created.get_json()["order_id"]
        forbidden = self.client.post("/webhooks/yookassa", json={"object": {"id": "provider-payment-1"}})
        self.assertEqual(403, forbidden.status_code)

        attempt_id = self._rows("SELECT id FROM yookassa_payments WHERE order_id = ?", (order_id,))[0][0]
        canonical = {
            "id": "provider-payment-1",
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "1.00", "currency": "RUB"},
            "metadata": {"order_id": str(order_id), "attempt_id": str(attempt_id)},
        }
        with (
            patch("server._is_yookassa_source", return_value=True),
            patch("server._yookassa_find_payment", return_value=canonical),
        ):
            response = self.client.post("/webhooks/yookassa", json={"object": {"id": "provider-payment-1"}})

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "awaiting_yookassa_payment",
            self._rows("SELECT status FROM orders WHERE id = ?", (order_id,))[0][0],
        )

    def test_return_route_does_not_change_order_status(self):
        created = self._create_order(payment_response())
        order_id = created.get_json()["order_id"]
        response = self.client.get("/payments/yookassa/return", query_string={"payment_id": "provider-payment-1"})

        self.assertEqual(302, response.status_code)
        self.assertEqual(
            "awaiting_yookassa_payment",
            self._rows("SELECT status FROM orders WHERE id = ?", (order_id,))[0][0],
        )


if __name__ == "__main__":
    unittest.main()
