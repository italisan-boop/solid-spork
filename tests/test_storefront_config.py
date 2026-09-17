import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import db.connection as db_connection
import db.schema as schema
import server
from controlplane.plan_policy import Plan, effective_entitlements
from runtime.context import TenantContext


class StorefrontConfigTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.database_path = self.root / "storefront.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        schema.initialize_database(self.database_path, seed_catalog=False)
        self.client = server.app.test_client()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def test_storefront_config_is_tenant_scoped_and_contains_no_secrets(self):
        database = schema.connect(self.database_path)
        try:
            database.execute(
                """
                INSERT INTO storefront_settings (
                    id, store_name, primary_color, accent_color, support_contact
                ) VALUES (1, 'Книжный сад', '#123456', '#abcdef', '@support')
                """
            )
            database.commit()
        finally:
            database.close()
        context = TenantContext(
            tenant_id="storefront-tenant",
            canonical_host="storefront.example.test",
            database_path=self.database_path,
            media_root=self.root / "media",
            backup_root=self.root / "backups",
            owner_telegram_id=101,
            entitlements=effective_entitlements(Plan.BUSINESS),
            runtime_generation=1,
        )
        with context.scope():
            response = self.client.get("/api/storefront/config")
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual("storefront-tenant", payload["tenant_id"])
        self.assertEqual("Книжный сад", payload["store_name"])
        self.assertEqual("#123456", payload["primary_color"])
        self.assertEqual("#abcdef", payload["accent_color"])
        self.assertIn("inventory", payload["features"])
        self.assertNotIn("secret", str(payload).lower())
        self.assertEqual("no-store", response.headers["Cache-Control"])


if __name__ == "__main__":
    unittest.main()
