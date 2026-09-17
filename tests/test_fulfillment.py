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
from config import settings


TEST_TOKEN = "123456:fulfillment-test-token"
OWNER_ID = 101
WAREHOUSE_ID = 303
OTHER_WAREHOUSE_ID = 404


def signed_headers(user_id: int) -> dict[str, str]:
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "first_name": "Staff"}, separators=(",", ":"))),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


class FulfillmentApiTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "fulfillment.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(server, "BOT_TOKEN", TEST_TOKEN))
        self._patches.enter_context(patch.object(server, "ADMIN_IDS", []))
        self._patches.enter_context(patch.object(settings, "OWNER_TELEGRAM_ID", OWNER_ID))
        self._patches.enter_context(patch.object(server.settings, "DELIVERY_ENCRYPTION_ACTIVE_KEY_ID", "test"))
        self._patches.enter_context(patch.object(
            server.settings,
            "DELIVERY_ENCRYPTION_KEYS_JSON",
            '{"test":"eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="}',
        ))
        schema.initialize_database(self.database_path)
        self.client = server.app.test_client()
        connection = schema.connect(self.database_path)
        try:
            connection.execute(
                "INSERT INTO staff_members (telegram_user_id, role, changed_by_user_id) VALUES (?, 'warehouse', ?)",
                (WAREHOUSE_ID, OWNER_ID),
            )
            connection.execute(
                "INSERT INTO staff_members (telegram_user_id, role, changed_by_user_id) VALUES (?, 'warehouse', ?)",
                (OTHER_WAREHOUSE_ID, OWNER_ID),
            )
            connection.execute(
                "INSERT INTO orders (id, user_id, total, status) VALUES (1, 11, 2000, 'confirmed')"
            )
            connection.executemany(
                "INSERT INTO order_items (order_id, book_id, title, price) VALUES (1, 1, 'Историческая книга', 1000)",
                [(), ()],
            )
            connection.execute(
                """
                INSERT INTO order_deliveries (
                    order_id, method, destination_encrypted, delivery_price, shipment_status
                ) VALUES (1, 'self_pickup', 'not-used', 0, 'preparing')
                """
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def request(self, method: str, path: str, user_id=WAREHOUSE_ID, json_body=None):
        return self.client.open(path, method=method, headers=signed_headers(user_id), json=json_body)

    def test_warehouse_claims_checks_and_packs_historical_lines(self):
        claim = self.request("POST", "/api/admin/fulfillment/1/claim")
        self.assertEqual(200, claim.status_code)
        record = claim.get_json()["fulfillment"]
        line = record["lines"][0]
        self.assertEqual(2, line["ordered_quantity"])
        self.assertEqual(0, line["picked_quantity"])

        incomplete = self.request("POST", "/api/admin/fulfillment/1/pack")
        self.assertEqual(409, incomplete.status_code)
        self.assertEqual("incomplete_checklist", incomplete.get_json()["code"])
        self.assertEqual(2, incomplete.get_json()["remaining_quantity"])
        completed = self.request("PUT", "/api/admin/fulfillment/1/lines", json_body={
            "book_id": line["book_id"], "title": line["title"], "price": line["price"], "picked_quantity": 2,
        })
        self.assertEqual(200, completed.status_code)
        packed = self.request("POST", "/api/admin/fulfillment/1/pack")
        self.assertEqual(200, packed.status_code)
        self.assertFalse(packed.get_json()["already_packed"])
        repeated = self.request("POST", "/api/admin/fulfillment/1/pack")
        self.assertEqual(200, repeated.status_code)
        self.assertTrue(repeated.get_json()["already_packed"])

        connection = schema.connect(self.database_path)
        try:
            self.assertEqual("packed", connection.execute(
                "SELECT shipment_status FROM order_deliveries WHERE order_id = 1"
            ).fetchone()[0])
            actions = connection.execute("SELECT action FROM audit_events ORDER BY id").fetchall()
        finally:
            connection.close()
        self.assertEqual(1, actions.count(("fulfillment.packed",)))

    def test_only_claimant_can_pack(self):
        self.assertEqual(200, self.request("POST", "/api/admin/fulfillment/1/claim").status_code)
        denied = self.request("POST", "/api/admin/fulfillment/1/pack", user_id=OTHER_WAREHOUSE_ID)
        self.assertEqual(409, denied.status_code)
        self.assertEqual("claim_required", denied.get_json()["code"])

    def test_warehouse_cannot_access_owner_operations(self):
        self.assertEqual(403, self.request("GET", "/api/admin/audit").status_code)
        self.assertEqual(403, self.request("GET", "/api/admin/dashboard").status_code)
        self.assertEqual(200, self.request("GET", "/api/admin/fulfillment").status_code)


if __name__ == "__main__":
    unittest.main()
