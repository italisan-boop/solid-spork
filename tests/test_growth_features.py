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
from db.inventory import adjust_stock_sync, commit_order_inventory, reserve_order_inventory


TEST_TOKEN = "123456:growth-test-token"


def signed_headers(user_id=101, name="Покупатель"):
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "first_name": name}, ensure_ascii=False, separators=(",", ":"))),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


class GrowthFeatureTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "growth.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(server, "BOT_TOKEN", TEST_TOKEN))
        self._patches.enter_context(patch.object(server, "ADMIN_IDS", [101]))
        schema.initialize_database(self.database_path)
        self.client = server.app.test_client()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def connection(self):
        return schema.connect(self.database_path)

    def test_inventory_journal_and_low_stock_outbox_are_transactional(self):
        connection = self.connection()
        try:
            connection.execute("UPDATE books SET stock_quantity = 3 WHERE id = 1")
            connection.execute("INSERT INTO orders (id, user_id, total) VALUES (77, 101, 1200)")
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            reserve_order_inventory(connection, 77, [{"id": 1, "quantity": 1}])
            connection.commit()
            rows = connection.execute(
                "SELECT action, stock_delta, reserved_delta FROM inventory_movements WHERE book_id = 1"
            ).fetchall()
            outbox = connection.execute(
                "SELECT kind FROM notification_outbox WHERE book_id = 1"
            ).fetchall()
            connection.execute("BEGIN IMMEDIATE")
            commit_order_inventory(connection, 77)
            connection.commit()
            stock = connection.execute("SELECT stock_quantity FROM books WHERE id = 1").fetchone()[0]
        finally:
            connection.close()

        self.assertIn(("reservation_created", 0, 1), rows)
        self.assertEqual([("low_stock",)], outbox)
        self.assertEqual(2, stock)

    def test_catalog_exposes_finite_availability(self):
        connection = self.connection()
        try:
            connection.execute("UPDATE books SET stock_quantity = 3 WHERE id = 1")
            connection.execute("UPDATE books SET stock_quantity = 0 WHERE id = 2")
            connection.commit()
        finally:
            connection.close()

        books = {book["id"]: book for book in self.client.get("/api/books").get_json()["books"]}

        self.assertEqual(3, books[1]["available_quantity"])
        self.assertTrue(books[1]["is_available"])
        self.assertEqual(0, books[2]["available_quantity"])
        self.assertFalse(books[2]["is_available"])
        self.assertIsNone(books[3]["available_quantity"])
        self.assertTrue(books[3]["is_available"])

    def test_referral_analytics_is_anonymous_and_paid_only(self):
        connection = self.connection()
        try:
            connection.execute(
                """
                INSERT INTO referrals (referrer_id, referred_id, created_at)
                VALUES (777777, 101, '2026-09-01 10:00:00')
                """
            )
            connection.executemany(
                """
                INSERT INTO orders (
                    user_id, user_name, total, status, created_at, paid_at,
                    items_subtotal, promo_discount, bonus_discount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (101, "Private referred customer", 850, "paid", "2026-09-02 12:00:00", "2026-09-02 12:00:00", 1000, 100, 50),
                    (101, "Private referred customer", 850, "cancelled", "2026-09-03 12:00:00", None, 1000, 100, 50),
                    (202, "Direct customer", 1200, "paid", "2026-09-03 12:00:00", "2026-09-03 12:00:00", 1200, 0, 0),
                ],
            )
            connection.commit()
        finally:
            connection.close()

        response = self.client.get(
            "/api/admin/analytics/referrals?date_from=2026-09-01&date_to=2026-09-30",
            headers=signed_headers(101),
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("private, no-store", response.headers["Cache-Control"])
        self.assertEqual(
            {
                "basis": "paid_referral_sales_after_acceptance",
                "accepted_referrals": 1,
                "buyers": 1,
                "orders": 1,
                "gross_revenue": 1000,
                "promo_discount": 100,
                "bonus_discount": 50,
                "net_revenue": 850,
            },
            response.get_json(),
        )

        exported = self.client.get(
            "/api/admin/export/analytics?dimension=referral",
            headers=signed_headers(101),
        )
        payload = exported.get_data(as_text=True)
        self.assertEqual(200, exported.status_code)
        self.assertEqual("private, no-store", exported.headers["Cache-Control"])
        self.assertNotIn("Private referred customer", payload)
        self.assertNotIn("777777", payload)
        self.assertNotIn("referrer", payload.lower())

    def test_favorite_does_not_create_back_in_stock_subscription(self):
        favorite = self.client.put("/api/favorites/1", headers=signed_headers())
        self.assertEqual(200, favorite.status_code)
        favorites = self.client.get("/api/favorites", headers=signed_headers()).get_json()
        subscriptions = self.client.get(
            "/api/back-in-stock-subscriptions", headers=signed_headers()
        ).get_json()
        self.assertEqual([1], [item["book_id"] for item in favorites["favorites"]])
        self.assertEqual([], subscriptions["book_ids"])

    def test_back_in_stock_subscription_requires_a_sold_out_finite_book(self):
        available = self.client.put(
            "/api/books/1/back-in-stock-subscription", headers=signed_headers()
        )
        self.assertEqual(409, available.status_code)

        connection = self.connection()
        try:
            connection.execute("UPDATE books SET stock_quantity = 0 WHERE id = 1")
            connection.commit()
        finally:
            connection.close()

        subscribed = self.client.put(
            "/api/books/1/back-in-stock-subscription", headers=signed_headers()
        )
        self.assertEqual(200, subscribed.status_code)
        self.assertTrue(subscribed.get_json()["subscribed"])
        revoked = self.client.delete(
            "/api/books/1/back-in-stock-subscription", headers=signed_headers()
        )
        self.assertEqual(200, revoked.status_code)
        self.assertFalse(revoked.get_json()["subscribed"])

    def test_restock_after_repeated_subscription_enqueues_one_notification(self):
        connection = self.connection()
        try:
            connection.execute("UPDATE books SET stock_quantity = 0 WHERE id = 1")
            connection.commit()
        finally:
            connection.close()

        for _ in range(2):
            response = self.client.put(
                "/api/books/1/back-in-stock-subscription", headers=signed_headers(101)
            )
            self.assertEqual(200, response.status_code)
            self.assertTrue(response.get_json()["subscribed"])

        self.assertTrue(adjust_stock_sync(1, 3, 9001, "received"))
        self.assertTrue(adjust_stock_sync(1, 2, 9001, "received"))

        connection = self.connection()
        try:
            subscriptions = connection.execute(
                "SELECT COUNT(*) FROM back_in_stock_subscriptions WHERE user_id = 101 AND book_id = 1 AND active = 1"
            ).fetchone()[0]
            jobs = connection.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE kind = 'back_in_stock' AND book_id = 1 AND user_id = 101"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(1, subscriptions)
        self.assertEqual(1, jobs)

    def test_public_catalog_never_returns_token_bearing_telegram_media(self):
        unsafe = "https://api.telegram.org/file/botTOKEN_SHOULD_NOT_LEAK/photo.jpg"
        connection = self.connection()
        try:
            connection.execute("UPDATE books SET emoji = ?, images = ? WHERE id = 1", (unsafe, json.dumps([unsafe])))
            connection.commit()
        finally:
            connection.close()

        payload = self.client.get("/api/books").get_data(as_text=True)
        self.assertNotIn("TOKEN_SHOULD_NOT_LEAK", payload)
        self.assertNotIn("api.telegram.org/file/bot", payload)
    def test_public_catalog_never_renders_raw_legacy_media_values(self):
        connection = self.connection()
        try:
            connection.execute(
                "UPDATE books SET emoji = ?, cover_photo = ?, images = ? WHERE id = 1",
                ("321", "telegram-file:file-id", json.dumps(["7", "telegram-file:page"])),
            )
            connection.commit()
        finally:
            connection.close()

        book = next(item for item in self.client.get("/api/books").get_json()["books"] if item["id"] == 1)
        self.assertNotIn("emoji", book)
        self.assertEqual("[]", book["images"])
        self.assertNotIn("legacy_cover_url", book["media"])
