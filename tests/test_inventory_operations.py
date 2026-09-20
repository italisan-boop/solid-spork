import hashlib
import hmac
import json
import sqlite3
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
from db.inventory import set_stock_quantity_sync


TEST_TOKEN = "123456:inventory-operation-test-token"
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


class InventoryOperationApiTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "inventory.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(server, "BOT_TOKEN", TEST_TOKEN))
        self._patches.enter_context(patch.object(server, "ADMIN_IDS", []))
        self._patches.enter_context(patch.object(settings, "OWNER_TELEGRAM_ID", OWNER_ID))
        schema.initialize_database(self.database_path)
        connection = schema.connect(self.database_path)
        try:
            connection.execute("UPDATE books SET stock_quantity = 10 WHERE id = 1")
            connection.commit()
        finally:
            connection.close()
        self.client = server.app.test_client()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def adjust(self, quantity: int, reason: str):
        return self.client.post(
            "/api/admin/inventory/1",
            headers=signed_headers(),
            json={"quantity": quantity, "reason": reason},
        )

    def stock_quantity(self) -> int:
        connection = schema.connect(self.database_path)
        try:
            return connection.execute("SELECT stock_quantity FROM books WHERE id = 1").fetchone()[0]
        finally:
            connection.close()

    def inventory(self) -> list[dict]:
        response = self.client.get("/api/admin/inventory?limit=50", headers=signed_headers())
        self.assertEqual(200, response.status_code)
        return response.get_json()["inventory"]

    def test_explicit_inventory_operations_have_exact_arithmetic(self):
        self.assertEqual(200, self.adjust(5, "received").status_code)
        self.assertEqual(15, self.stock_quantity())
        self.assertEqual(200, self.adjust(12, "recount").status_code)
        self.assertEqual(12, self.stock_quantity())
        self.assertEqual(200, self.adjust(2, "damaged").status_code)
        self.assertEqual(10, self.stock_quantity())
        self.assertEqual(200, self.adjust(3, "return").status_code)
        self.assertEqual(13, self.stock_quantity())

        connection = schema.connect(self.database_path)
        try:
            movements = connection.execute(
                "SELECT action, stock_delta, reason FROM inventory_movements WHERE book_id = 1 ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            [
                ("manual_adjustment", 5, "received"),
                ("manual_adjustment", -3, "recount"),
                ("manual_adjustment", -2, "damaged"),
                ("manual_adjustment", 3, "return"),
            ],
            movements,
        )

    def test_absolute_stock_editor_supports_finite_and_unlimited_values(self):
        self.assertTrue(set_stock_quantity_sync(1, 7, OWNER_ID))
        self.assertEqual(7, self.stock_quantity())
        self.assertTrue(set_stock_quantity_sync(1, None, OWNER_ID))
        self.assertIsNone(self.stock_quantity())
        self.assertTrue(set_stock_quantity_sync(1, 4, OWNER_ID))
        self.assertEqual(4, self.stock_quantity())

        connection = schema.connect(self.database_path)
        try:
            movements = connection.execute(
                "SELECT action, stock_after, reason FROM inventory_movements WHERE book_id = 1 ORDER BY id"
            ).fetchall()
            audit_actions = connection.execute(
                "SELECT action FROM audit_events WHERE entity_type = 'book' AND entity_id = 1 ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            [
                ("manual_adjustment", 7, "recount"),
                ("stock_mode_changed", None, "unlimited"),
                ("stock_mode_changed", 4, "finite"),
            ],
            movements,
        )
        self.assertEqual(
            [
                ("inventory.stock.adjusted",),
                ("inventory.stock.mode_changed",),
                ("inventory.stock.mode_changed",),
            ],
            audit_actions,
        )

    def test_inventory_movement_migration_preserves_history_and_immutability(self):
        connection = schema.connect(self.database_path)
        try:
            connection.execute("DROP TRIGGER inventory_movements_no_update")
            connection.execute("DROP TRIGGER inventory_movements_no_delete")
            connection.execute("DROP TABLE inventory_movements")
            connection.execute(
                """
                CREATE TABLE inventory_movements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    book_id INTEGER NOT NULL,
                    order_id INTEGER,
                    action TEXT NOT NULL CHECK (action IN (
                        'opening_balance', 'manual_adjustment', 'reservation_created',
                        'reservation_released', 'sale_committed', 'sale_reversed'
                    )),
                    stock_delta INTEGER NOT NULL DEFAULT 0,
                    reserved_delta INTEGER NOT NULL DEFAULT 0,
                    stock_after INTEGER,
                    reserved_after INTEGER NOT NULL DEFAULT 0,
                    actor_admin_id INTEGER,
                    reason TEXT NOT NULL DEFAULT '',
                    source_key TEXT UNIQUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (stock_delta != 0 OR reserved_delta != 0),
                    FOREIGN KEY (book_id) REFERENCES books (id) ON DELETE RESTRICT,
                    FOREIGN KEY (order_id) REFERENCES orders (id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO inventory_movements (
                    book_id, action, stock_delta, stock_after, reason
                ) VALUES (1, 'opening_balance', 10, 10, 'legacy')
                """
            )
            connection.commit()
        finally:
            connection.close()

        schema.initialize_database(self.database_path)

        connection = schema.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT action, stock_delta, stock_after, reason FROM inventory_movements"
            ).fetchone()
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'inventory_movements'"
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE inventory_movements SET reason = 'changed' WHERE id = 1")
        finally:
            connection.close()
        self.assertEqual(("opening_balance", 10, 10, "legacy"), row)
        self.assertIn("stock_mode_changed", table_sql)

    def test_inventory_order_follows_catalog_order_after_stock_changes(self):
        connection = schema.connect(self.database_path)
        try:
            connection.execute("UPDATE books SET sort_order = id * 100")
            connection.execute("UPDATE books SET sort_order = 30, stock_quantity = 1 WHERE id = 1")
            connection.execute("UPDATE books SET sort_order = 10, stock_quantity = 100 WHERE id = 2")
            connection.execute("UPDATE books SET sort_order = 20, stock_quantity = 0 WHERE id = 3")
            connection.commit()
        finally:
            connection.close()

        before = [item["id"] for item in self.inventory()]
        self.assertEqual([2, 3, 1], before[:3])
        self.assertEqual(200, self.adjust(50, "received").status_code)
        self.assertEqual(200, self.client.post(
            "/api/admin/inventory/3",
            headers=signed_headers(),
            json={"quantity": 1, "reason": "received"},
        ).status_code)
        after = [item["id"] for item in self.inventory()]
        self.assertEqual(before, after)

    def test_correction_and_invalid_operation_amounts_are_rejected(self):
        self.assertEqual(400, self.adjust(1, "correction").status_code)
        self.assertEqual(400, self.adjust(0, "received").status_code)
        self.assertEqual(400, self.adjust(-1, "return").status_code)
        self.assertEqual(10, self.stock_quantity())


if __name__ == "__main__":
    unittest.main()
