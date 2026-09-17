import asyncio
from contextlib import ExitStack
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import db.schema as schema


class DatabaseInitializationSmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "smoke.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))

    async def asyncTearDown(self):
        await asyncio.sleep(0.1)

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def _query_one(self, query):
        connection = sqlite3.connect(self.database_path)
        try:
            return connection.execute(query).fetchone()[0]
        finally:
            connection.close()

    def _table_names(self):
        connection = sqlite3.connect(self.database_path)
        try:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()

    async def test_fresh_database_initializes_twice_without_locks_or_duplicate_seeds(self):
        await db.init_db()
        await db.init_db()

        self.assertTrue(
            {
                "schema_migrations",
                "users",
                "orders",
                "order_items",
                "books",
                "categories",
                "promo_codes",
                "referrals",
                "user_bonuses",
                "payment_settings",
                "stars_settings",
                "support_messages",
                "yookassa_payments",
                "message_templates",
                "fsm_records",
                "mini_app_carts",
                "inventory_reservations",
                "operational_events",
                "order_deliveries",
                "order_support_requests",
            }.issubset(self._table_names())
        )
        self.assertEqual(schema.SCHEMA_VERSION, self._query_one("PRAGMA user_version"))
        self.assertEqual(6, self._query_one("SELECT COUNT(*) FROM categories"))
        self.assertEqual(6, self._query_one("SELECT COUNT(*) FROM books"))
        self.assertEqual(17, self._query_one("SELECT COUNT(*) FROM payment_settings"))
        self.assertEqual(
            "0",
            self._query_one(
                "SELECT setting_value FROM payment_settings WHERE setting_key = 'delivery_enabled'"
            ),
        )
        self.assertEqual(2, self._query_one("SELECT COUNT(*) FROM stars_settings"))

    async def test_legacy_order_support_requests_accept_failed_state(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.executescript(
                """
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    total INTEGER NOT NULL
                );
                CREATE TABLE order_support_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    receipt_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending', 'processing', 'sent')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    claimed_at TIMESTAMP,
                    sent_at TIMESTAMP,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (order_id) REFERENCES orders (id)
                );
                INSERT INTO orders (id, user_id, total) VALUES (1, 101, 1000);
                INSERT INTO order_support_requests (order_id, user_id, receipt_json)
                VALUES (1, 101, '{}');
                """
            )
            connection.commit()
        finally:
            connection.close()

        await db.init_db()
        await db.init_db()

        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("UPDATE order_support_requests SET state = 'failed' WHERE id = 1")
            connection.commit()
            state = connection.execute("SELECT state FROM order_support_requests WHERE id = 1").fetchone()[0]
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(order_support_requests)")}
        finally:
            connection.close()

        self.assertEqual("failed", state)
        self.assertIn("idx_order_support_requests_lease", indexes)

    async def test_legacy_books_migration_initializes_twice_without_locks(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    user_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    emoji TEXT DEFAULT ''
                )
                """
            )
            connection.execute(
                "INSERT INTO books (title, price, category, emoji) VALUES (?, ?, ?, ?)",
                ("Старая книга", 500, "Ботаника", "🌿"),
            )
            connection.commit()
        finally:
            connection.close()

        await db.init_db()
        await db.init_db()

        connection = sqlite3.connect(self.database_path)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(books)")}
            legacy_book = connection.execute(
                "SELECT category_id, created_at FROM books WHERE title = ?",
                ("Старая книга",),
            ).fetchone()
        finally:
            connection.close()

        self.assertTrue({"category_id", "created_at", "description", "images"}.issubset(columns))
        self.assertIsNotNone(legacy_book[0])
        self.assertIsNotNone(legacy_book[1])
        self.assertEqual(6, self._query_one("SELECT COUNT(*) FROM categories"))
        self.assertEqual(17, self._query_one("SELECT COUNT(*) FROM payment_settings"))
        self.assertEqual(2, self._query_one("SELECT COUNT(*) FROM stars_settings"))

    async def test_legacy_orders_gain_checkout_and_yookassa_schema_idempotently(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    total INTEGER NOT NULL
                )
                """
            )
            connection.execute("INSERT INTO orders (user_id, total) VALUES (101, 1200)")
            connection.commit()
        finally:
            connection.close()

        await db.init_db()
        await db.init_db()

        connection = sqlite3.connect(self.database_path)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)")}
            migrated_order = connection.execute(
                "SELECT payment_method, checkout_key FROM orders WHERE id = 1"
            ).fetchone()
            indexes = {
                row[1]
                for row in connection.execute("PRAGMA index_list(orders)")
            }
            payment_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'yookassa_payments'"
            ).fetchone()
        finally:
            connection.close()

        self.assertTrue({"payment_method", "checkout_key", "payment_details_json", "paid_at"}.issubset(columns))
        self.assertEqual(("manual", None), migrated_order)
        self.assertIn("idx_orders_user_checkout_key", indexes)
        self.assertEqual(("yookassa_payments",), payment_table)

    async def test_sdek_delivery_table_rebuild_preserves_existing_ciphertext(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.executescript(
                """
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    total INTEGER NOT NULL
                );
                CREATE TABLE order_deliveries (
                    order_id INTEGER PRIMARY KEY,
                    method TEXT NOT NULL CHECK (method = 'sdek_pickup'),
                    destination_encrypted TEXT NOT NULL,
                    delivery_price INTEGER NOT NULL CHECK (delivery_price >= 0),
                    shipment_status TEXT NOT NULL DEFAULT 'awaiting_payment',
                    tracking_carrier TEXT NOT NULL DEFAULT 'none'
                        CHECK (tracking_carrier IN ('none', 'sdek')),
                    tracking_number TEXT,
                    tracking_set_at TIMESTAMP,
                    delivered_at TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_by_admin_id INTEGER,
                    pii_redacted_at TIMESTAMP,
                    FOREIGN KEY (order_id) REFERENCES orders (id)
                );
                """
            )
            connection.execute("INSERT INTO orders (id, user_id, total) VALUES (1, 101, 1700)")
            connection.execute(
                """
                INSERT INTO order_deliveries (
                    order_id, method, destination_encrypted, delivery_price, shipment_status
                ) VALUES (1, 'sdek_pickup', 'gcm1.test.ciphertext', 500, 'packed')
                """
            )
            connection.commit()
        finally:
            connection.close()

        await db.init_db()
        await db.init_db()

        connection = sqlite3.connect(self.database_path)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(order_deliveries)")}
            table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'order_deliveries'"
            ).fetchone()[0]
            migrated = connection.execute(
                "SELECT method, destination_encrypted, public_instructions_snapshot FROM order_deliveries"
            ).fetchone()
        finally:
            connection.close()

        self.assertIn("public_instructions_snapshot", columns)
        self.assertIn("russian_post_pickup", table_sql)
        self.assertEqual(("sdek_pickup", "gcm1.test.ciphertext", ""), migrated)
