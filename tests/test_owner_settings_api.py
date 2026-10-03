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

import db.connection as db_connection
import db.schema as schema
import server
from controlplane.plan_policy import Plan, effective_entitlements
from runtime.context import TenantContext


TEST_TOKEN = "123456:owner-settings-token"


def signed_headers(user_id: int) -> dict[str, str]:
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("query_id", "owner-settings"),
        ("user", json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":"))),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


class OwnerSettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database_path = root / "data" / "tenant.sqlite"
        self.backup_root = root / "backups"
        self.patches = ExitStack()
        self.patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self.patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self.patches.enter_context(patch.object(server, "BOT_TOKEN", TEST_TOKEN))
        self.patches.enter_context(patch.object(server, "ADMIN_IDS", [1]))
        schema.initialize_database(self.database_path, seed_catalog=False)
        self.client = server.app.test_client()
        self.context = TenantContext(
            tenant_id="tenant",
            canonical_host="tenant.example.test",
            database_path=self.database_path,
            media_root=root / "media",
            backup_root=self.backup_root,
            owner_telegram_id=1,
            entitlements=effective_entitlements(Plan.BUSINESS),
            runtime_generation=1,
        )
        with self.context.scope():
            database = schema.connect()
            try:
                database.execute(
                    """
                    INSERT INTO tenant_runtime_metadata (id, tenant_id, canonical_host, owner_telegram_id)
                    VALUES (1, ?, ?, ?)
                    """,
                    (self.context.tenant_id, self.context.canonical_host, self.context.owner_telegram_id),
                )
                database.commit()
            finally:
                database.close()

    def tearDown(self):
        self.patches.close()
        self.temporary.cleanup()

    def owner_headers(self):
        return signed_headers(1)

    def test_owner_only_promo_lifecycle_and_checkout_claim(self):
        self.assertEqual(401, self.client.get("/api/admin/promos").status_code)
        self.assertEqual(403, self.client.get("/api/admin/promos", headers=signed_headers(99)).status_code)
        created = self.client.post(
            "/api/admin/promos",
            headers=self.owner_headers(),
            json={
                "code": "fall-2026",
                "discount_percent": 10,
                "discount_fixed": 0,
                "min_order": 100,
                "max_uses": 1,
                "expires_at": "2030-01-01T00:00:00+00:00",
            },
        )
        self.assertEqual(201, created.status_code)
        promo = created.get_json()["promo"]
        self.assertEqual("FALL-2026", promo["code"])
        self.assertEqual(400, self.client.post(
            "/api/admin/promos",
            headers=self.owner_headers(),
            json={**promo, "code": "BAD", "discount_percent": 10, "discount_fixed": 10},
        ).status_code)
        connection = schema.connect()
        connection.row_factory = __import__("sqlite3").Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            claimed = server.validate_and_claim_promo(connection, "fall-2026", 1000)
            self.assertTrue(claimed["valid"])
            self.assertEqual(100, claimed["discount"])
            self.assertFalse(server.validate_and_claim_promo(connection, "fall-2026", 1000)["valid"])
            connection.rollback()
        finally:
            connection.close()
        disabled = self.client.post(
            f"/api/admin/promos/{promo['id']}/disable", headers=self.owner_headers(), json={}
        )
        self.assertEqual(200, disabled.status_code)
        self.assertFalse(disabled.get_json()["promo"]["is_active"])

    def test_payment_templates_and_storefront_are_private_and_validated(self):
        initial = self.client.get("/api/admin/payment-settings", headers=self.owner_headers())
        self.assertEqual(200, initial.status_code)
        manual = initial.get_json()["manual"]
        self.assertNotIn("card_number", manual)
        saved = self.client.put(
            "/api/admin/payment-settings",
            headers=self.owner_headers(),
            json={
                "manual": {
                    "enabled": True,
                    "card_number": "2200123412341234",
                    "sbp_phone": "",
                    "sbp_bank": "",
                    "recipient_name": "",
                    "instructions": "Укажите номер заказа",
                },
                "stars": {"enabled": False, "rubles_per_star": 2},
                "yookassa": {"enabled": False},
                "delivery": {
                    "enabled": False,
                    "sdek_pickup": {"enabled": False, "price": 500},
                    "russian_post_pickup": {"enabled": False, "price": 500},
                    "self_pickup": {"enabled": False, "price": 0, "location": "", "schedule": "", "instructions": ""},
                },
            },
        )
        self.assertEqual(200, saved.status_code)
        self.assertNotIn("2200123412341234", json.dumps(saved.get_json()))
        templates = self.client.get("/api/admin/templates", headers=self.owner_headers())
        self.assertEqual(200, templates.status_code)
        key = "support.quick.greeting"
        self.assertEqual(
            400,
            self.client.put(
                f"/api/admin/templates/{key}", headers=self.owner_headers(), json={"value": ""}
            ).status_code,
        )
        updated = self.client.put(
            f"/api/admin/templates/{key}", headers=self.owner_headers(), json={"value": "Здравствуйте"}
        )
        self.assertEqual(200, updated.status_code)
        storefront = self.client.put(
            "/api/admin/storefront",
            headers=self.owner_headers(),
            json={
                "store_name": "Магазин",
                "primary_color": "#112233",
                "accent_color": "#abcdef",
                "support_contact": "@help",
            },
        )
        self.assertEqual(200, storefront.status_code)
        self.assertEqual("#112233", storefront.get_json()["primary_color"])

    def test_mini_app_owner_settings_contract(self):
        source = Path(server.app.static_folder, "Index.html").read_text(encoding="utf-8")
        for marker in (
            "🎟️ Промокоды",
            "⚙️ Оплата и доставка",
            "✍️ Тексты",
            "🎨 Брендинг",
            "🗄️ Резервные копии",
            "/api/admin/promos",
            "/api/admin/payment-settings",
            "/api/admin/templates",
            "/api/admin/storefront",
            "/api/admin/database/backups",
            "восстановления нет",
            "downloadAdminFile",
        ):
            self.assertIn(marker, source)

    def test_owner_creates_downloads_and_stages_tenant_backup(self):
        with self.context.scope():
            created = self.client.post("/api/admin/database/backups", headers=self.owner_headers())
            self.assertEqual(201, created.status_code)
            backup = created.get_json()["backup"]
            self.assertEqual("generated", backup["source"])
            downloaded = self.client.get(
                f"/api/admin/database/backups/{backup['id']}/download", headers=self.owner_headers()
            )
            self.assertEqual(200, downloaded.status_code)
            self.assertEqual("private, no-store", downloaded.headers["Cache-Control"])
            self.assertEqual("nosniff", downloaded.headers["X-Content-Type-Options"])
            backup_bytes = downloaded.data
            downloaded.close()
            uploaded = self.client.post(
                "/api/admin/database/backups/upload",
                headers=self.owner_headers(),
                data={"file": (io.BytesIO(backup_bytes), "tenant.sqlite")},
                content_type="multipart/form-data",
            )
        self.assertEqual(201, uploaded.status_code)
        self.assertEqual("uploaded", uploaded.get_json()["backup"]["source"])
        self.assertFalse(uploaded.get_json()["restore_available"])


if __name__ == "__main__":
    unittest.main()
