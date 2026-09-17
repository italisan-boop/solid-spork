import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from controlplane.plan_policy import Plan
from controlplane.provisioning import provision_tenant
from controlplane.schema import initialize
from controlplane.tenants import create_tenant, set_secret_reference
from runtime.launcher import configure_tenant_environment


class TenantLauncherTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.control_database = self.root / "control.sqlite"
        initialize(self.control_database)
        self.tenant = create_tenant(
            self.control_database,
            display_name="Runtime store",
            slug="runtime-store",
            owner_telegram_id=202,
            plan=Plan.BUSINESS,
            tenant_data_root=self.root / "tenants",
            tenant_backup_root=self.root / "backups",
            tenant_base_domain="shops.example.test",
            actor_telegram_id=101,
        )
        set_secret_reference(
            self.control_database,
            tenant_id=self.tenant.id,
            secret_kind="telegram_bot_token",
            reference="env:TENANT_BOT_TOKEN_VALUE",
            version="v1",
            actor_telegram_id=101,
        )
        set_secret_reference(
            self.control_database,
            tenant_id=self.tenant.id,
            secret_kind="telegram_webhook_secret",
            reference="env:TENANT_WEBHOOK_SECRET_VALUE",
            version="v1",
            actor_telegram_id=101,
        )
        with patch.dict(os.environ, {
            "TENANT_BOT_TOKEN_VALUE": "234567:tenant-token",
            "TENANT_WEBHOOK_SECRET_VALUE": "tenant-webhook-secret",
        }, clear=False):
            provision_tenant(
                self.control_database,
                tenant_id=self.tenant.id,
                actor_telegram_id=101,
            )

    def tearDown(self):
        self._temporary_directory.cleanup()

    def test_launcher_derives_single_tenant_environment_from_secret_references(self):
        environment = {
            "PLATFORM_DATABASE_PATH": str(self.control_database),
            "PLATFORM_TENANT_DATA_ROOT": str(self.root / "tenants"),
            "PLATFORM_TENANT_BACKUP_ROOT": str(self.root / "backups"),
            "PLATFORM_TENANT_BASE_DOMAIN": "shops.example.test",
            "PLATFORM_BOT_TOKEN": "123456:platform-token",
            "PLATFORM_ADMIN_TELEGRAM_IDS": "101",
            "TENANT_RUNTIME_ID": self.tenant.id,
            "TENANT_BOT_TOKEN_VALUE": "234567:tenant-token",
            "TENANT_WEBHOOK_SECRET_VALUE": "tenant-webhook-secret",
        }
        with patch.dict(os.environ, environment, clear=False):
            tenant, context = configure_tenant_environment()
            self.assertEqual(self.tenant.id, tenant.id)
            self.assertEqual(self.tenant.id, context.tenant_id)
            self.assertEqual(str(self.tenant.database_path), os.environ["DATABASE_PATH"])
            self.assertEqual("234567:tenant-token", os.environ["BOT_TOKEN"])
            self.assertEqual("tenant-webhook-secret", os.environ["WEBHOOK_SECRET"])
            self.assertEqual("202", os.environ["OWNER_TELEGRAM_ID"])
            self.assertEqual("https://runtime-store.shops.example.test", os.environ["WEBAPP_URL"])

    def test_launcher_resolves_yookassa_added_after_provisioning(self):
        set_secret_reference(
            self.control_database,
            tenant_id=self.tenant.id,
            secret_kind="yookassa_credentials",
            reference="env:TENANT_YOOKASSA_JSON",
            version="v1",
            actor_telegram_id=101,
        )
        environment = {
            "PLATFORM_DATABASE_PATH": str(self.control_database),
            "PLATFORM_TENANT_DATA_ROOT": str(self.root / "tenants"),
            "PLATFORM_TENANT_BACKUP_ROOT": str(self.root / "backups"),
            "PLATFORM_TENANT_BASE_DOMAIN": "shops.example.test",
            "PLATFORM_BOT_TOKEN": "123456:platform-token",
            "PLATFORM_ADMIN_TELEGRAM_IDS": "101",
            "TENANT_RUNTIME_ID": self.tenant.id,
            "TENANT_BOT_TOKEN_VALUE": "234567:tenant-token",
            "TENANT_WEBHOOK_SECRET_VALUE": "tenant-webhook-secret",
            "TENANT_YOOKASSA_JSON": '{"shop_id":"test-shop","secret_key":"test-key"}',
        }
        with patch.dict(os.environ, environment, clear=False):
            configure_tenant_environment()
            self.assertEqual("test-shop", os.environ["YOOKASSA_SHOP_ID"])
            self.assertEqual("test-key", os.environ["YOOKASSA_SECRET_KEY"])


if __name__ == "__main__":
    unittest.main()
