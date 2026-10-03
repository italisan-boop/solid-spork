import hashlib
import hmac
import io
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from PIL import Image

import db.connection as db_connection
import db.schema as schema
import server
from controlplane.plan_policy import Plan, effective_entitlements
from runtime.context import TenantContext


TEST_TOKEN = "123456:catalog-admin-test-token"


def signed_headers(user_id: int) -> dict[str, str]:
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("query_id", "catalog-admin-test"),
        ("user", json.dumps({"id": user_id, "first_name": "Catalog"}, separators=(",", ":"))),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


def image_file(name: str = "cover.png"):
    payload = io.BytesIO()
    Image.new("RGB", (16, 16), "#226633").save(payload, format="PNG")
    payload.seek(0)
    return payload, name


class CatalogAdminApiTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "catalog.sqlite"
        self.media_path = Path(self._temporary_directory.name) / "media"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(server, "BOT_TOKEN", TEST_TOKEN))
        self._patches.enter_context(patch.object(server, "ADMIN_IDS", [1]))
        self._patches.enter_context(patch.object(server.settings, "BOOK_MEDIA_ROOT", str(self.media_path)))
        schema.initialize_database(self.database_path, seed_catalog=False)
        self.client = server.app.test_client()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def owner_headers(self):
        return signed_headers(1)

    def test_business_owner_can_assign_editor_and_session_exposes_eligibility(self):
        context = TenantContext(
            tenant_id="tenant",
            canonical_host="tenant.example.test",
            database_path=self.database_path,
            media_root=self.media_path,
            backup_root=Path(self._temporary_directory.name) / "backups",
            owner_telegram_id=1,
            entitlements=effective_entitlements(Plan.BUSINESS),
            runtime_generation=1,
        )
        with context.scope():
            owner_session = self.client.get("/api/admin/session", headers=self.owner_headers())
            self.assertTrue(owner_session.get_json()["editor_assignable"])
            assigned = self.client.put(
                "/api/admin/staff",
                headers=self.owner_headers(),
                json={"telegram_user_id": 202, "roles": ["editor"], "is_active": True},
            )
            self.assertEqual(200, assigned.status_code)
            editor_session = self.client.get("/api/admin/session", headers=signed_headers(202))
        self.assertEqual("editor", editor_session.get_json()["role"])
        self.assertEqual(["admin.access", "catalog.manage"], editor_session.get_json()["capabilities"])
        self.assertTrue(editor_session.get_json()["editor_assignable"])

    def test_editor_can_manage_catalog_but_cannot_change_stock_or_staff(self):
        editor_id = 202
        connection = schema.connect(self.database_path)
        try:
            connection.execute(
                "INSERT INTO staff_members (telegram_user_id, changed_by_user_id) VALUES (?, 1)",
                (editor_id,),
            )
            connection.execute(
                "INSERT INTO staff_member_roles (telegram_user_id, role, assigned_by_user_id) VALUES (?, 'editor', 1)",
                (editor_id,),
            )
            connection.execute(
                "INSERT INTO books (title, price, category) VALUES ('Редакторская', 100, 'Test')"
            )
            connection.commit()
            book_id = connection.execute("SELECT id FROM books").fetchone()[0]
        finally:
            connection.close()

        session = self.client.get("/api/admin/session", headers=signed_headers(editor_id))
        self.assertEqual(200, session.status_code)
        self.assertEqual("editor", session.get_json()["role"])
        self.assertEqual(["admin.access", "catalog.manage"], session.get_json()["capabilities"])
        self.assertEqual(200, self.client.get("/api/admin/catalog/books", headers=signed_headers(editor_id)).status_code)
        self.assertEqual(
            403,
            self.client.put(
                f"/api/admin/catalog/books/{book_id}/stock",
                headers=signed_headers(editor_id),
                json={"mode": "finite", "quantity": 1},
            ).status_code,
        )
        self.assertEqual(403, self.client.get("/api/admin/staff", headers=signed_headers(editor_id)).status_code)
        from handlers.admin_books import is_admin as bot_books_admin
        from handlers.admin_promo import is_admin as bot_promo_admin
        from handlers.categories import is_admin as bot_categories_admin
        from handlers.catalog import is_admin as bot_catalog_admin

        self.assertFalse(bot_books_admin(editor_id))
        self.assertFalse(bot_catalog_admin(editor_id))
        self.assertFalse(bot_categories_admin(editor_id))
        self.assertFalse(bot_promo_admin(editor_id))
        connection = schema.connect(self.database_path)
        try:
            self.assertIsNone(connection.execute("SELECT stock_quantity FROM books WHERE id = ?", (book_id,)).fetchone()[0])
        finally:
            connection.close()

    def test_catalog_routes_are_owner_only_and_validate_payloads(self):
        self.assertEqual(401, self.client.get("/api/admin/catalog/books").status_code)
        self.assertEqual(403, self.client.get("/api/admin/catalog/books", headers=signed_headers(999)).status_code)
        invalid = self.client.post(
            "/api/admin/catalog/books",
            headers=self.owner_headers(),
            json={"title": "Book", "author": "", "description": "", "price": "100", "category_id": None},
        )
        self.assertEqual(400, invalid.status_code)
        self.assertEqual("private, no-store", invalid.headers["Cache-Control"])

    def test_owner_manages_book_stock_media_bulk_archive_and_restore(self):
        category = self.client.post(
            "/api/admin/catalog/categories",
            headers=self.owner_headers(),
            json={"name": "Новые", "emoji": "📚"},
        )
        self.assertEqual(201, category.status_code)
        category_id = category.get_json()["category"]["id"]
        created = self.client.post(
            "/api/admin/catalog/books",
            headers=self.owner_headers(),
            json={
                "title": "Русская Книга",
                "author": "Автор",
                "description": "Первая строка\nВторая строка",
                "price": 1200,
                "category_id": category_id,
            },
        )
        self.assertEqual(201, created.status_code)
        book_id = created.get_json()["book"]["id"]
        stock = self.client.put(
            f"/api/admin/catalog/books/{book_id}/stock",
            headers=self.owner_headers(),
            json={"mode": "finite", "quantity": 7},
        )
        self.assertEqual(200, stock.status_code)
        self.assertEqual(7, stock.get_json()["book"]["stock"]["stock_quantity"])
        connection = schema.connect(self.database_path)
        try:
            stock_audit = connection.execute(
                "SELECT source FROM audit_events WHERE action = 'inventory.stock.mode_changed'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(("mini_app",), stock_audit)

        cover, cover_name = image_file()
        uploaded = self.client.put(
            f"/api/admin/catalog/books/{book_id}/cover",
            headers=self.owner_headers(),
            data={"file": (cover, cover_name)},
            content_type="multipart/form-data",
        )
        self.assertEqual(200, uploaded.status_code)
        self.assertTrue(uploaded.get_json()["book"]["media"]["cover"]["display_url"].startswith("/media/books/"))
        page, page_name = image_file("page.png")
        uploaded_page = self.client.post(
            f"/api/admin/catalog/books/{book_id}/pages",
            headers=self.owner_headers(),
            data={"file": (page, page_name)},
            content_type="multipart/form-data",
        )
        self.assertEqual(200, uploaded_page.status_code)
        self.assertEqual(1, sum(item["role"] == "page" for item in uploaded_page.get_json()["media"]))

        listed = self.client.get(
            "/api/admin/catalog/books?q=РУССКАЯ&sort=title&limit=20&offset=0",
            headers=self.owner_headers(),
        )
        self.assertEqual(200, listed.status_code)
        self.assertEqual([book_id], [book["id"] for book in listed.get_json()["books"]])
        archived = self.client.post(
            "/api/admin/catalog/books/bulk/archive",
            headers=self.owner_headers(),
            json={"book_ids": [book_id]},
        )
        self.assertEqual({"archived_ids": [book_id], "skipped_ids": []}, archived.get_json())
        restore = self.client.post(
            f"/api/admin/catalog/books/{book_id}/restore",
            headers=self.owner_headers(),
            json={},
        )
        self.assertEqual(200, restore.status_code)
        self.assertEqual(book_id, restore.get_json()["book"]["id"])

    def test_category_rename_cascades_and_deactivation_reports_active_books(self):
        category = self.client.post(
            "/api/admin/catalog/categories",
            headers=self.owner_headers(),
            json={"name": "Старое", "emoji": "📘"},
        ).get_json()["category"]
        book = self.client.post(
            "/api/admin/catalog/books",
            headers=self.owner_headers(),
            json={"title": "Книга", "author": "", "description": "", "price": 100, "category_id": category["id"]},
        ).get_json()["book"]
        renamed = self.client.put(
            f"/api/admin/catalog/categories/{category['id']}",
            headers=self.owner_headers(),
            json={"name": "Новое", "emoji": "📗"},
        )
        self.assertEqual(200, renamed.status_code)
        details = self.client.get(
            f"/api/admin/catalog/books/{book['id']}", headers=self.owner_headers()
        ).get_json()["book"]
        self.assertEqual("Новое", details["category"]["name"])
        blocked = self.client.delete(
            f"/api/admin/catalog/categories/{category['id']}", headers=self.owner_headers()
        )
        self.assertEqual(409, blocked.status_code)
        self.assertEqual(1, blocked.get_json()["books_count"])

    def test_mini_app_catalog_workspace_contract(self):
        source = Path(server.app.static_folder, "Index.html").read_text(encoding="utf-8")
        self.assertIn("hasAdminCapability('catalog.manage')", source)
        self.assertIn("['catalog', '📚 Каталог']", source)
        self.assertIn("/api/admin/catalog/books", source)
        self.assertIn("/api/admin/catalog/categories", source)
        self.assertIn("uploadCatalogCover", source)
        self.assertIn("uploadCatalogPages", source)
        self.assertIn("escapeHtml(book.title)", source)
        self.assertIn("assignable_roles", source)
        self.assertIn("Редактор каталога", source)
        self.assertIn("hasAdminCapability('inventory.adjust')", source)
        self.assertIn("catalog.manage", source)

    def test_restore_requires_a_replacement_after_category_deactivation(self):
        category = self.client.post(
            "/api/admin/catalog/categories",
            headers=self.owner_headers(),
            json={"name": "Временная", "emoji": "📙"},
        ).get_json()["category"]
        book = self.client.post(
            "/api/admin/catalog/books",
            headers=self.owner_headers(),
            json={"title": "Архив", "author": "", "description": "", "price": 100, "category_id": category["id"]},
        ).get_json()["book"]
        self.client.post(
            "/api/admin/catalog/books/bulk/archive",
            headers=self.owner_headers(),
            json={"book_ids": [book["id"]]},
        )
        deactivated = self.client.delete(
            f"/api/admin/catalog/categories/{category['id']}", headers=self.owner_headers()
        )
        self.assertEqual(200, deactivated.status_code)
        unavailable = self.client.post(
            f"/api/admin/catalog/books/{book['id']}/restore",
            headers=self.owner_headers(),
            json={},
        )
        self.assertEqual(409, unavailable.status_code)
        self.assertEqual("category_unavailable", unavailable.get_json()["error"])
        restored = self.client.post(
            f"/api/admin/catalog/books/{book['id']}/restore",
            headers=self.owner_headers(),
            json={"category_id": None},
        )
        self.assertEqual(200, restored.status_code)
        self.assertIsNone(restored.get_json()["book"]["category"])


if __name__ == "__main__":
    unittest.main()
