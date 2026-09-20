import tempfile
import unittest
import uuid
from pathlib import Path

from controlplane.unit_templates import (
    ControllerPaths,
    TenantStoragePaths,
    UnitTemplateError,
    render_tenant_dropin,
    tenant_credential_directory,
    tenant_unit_name,
)


class UnitTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.paths = ControllerPaths(
            release_root=self.root / "release",
            credential_root=self.root / "credentials",
            runtime_root=self.root / "runtime",
            tenant_data_root=self.root / "tenants",
            tenant_backup_root=self.root / "backups",
            public_key_file=self.root / "keys" / "manifest-public.key",
        )
        self.tenant_id = str(uuid.uuid4())

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_renders_fixed_unit_from_uuid_identity(self):
        storage = TenantStoragePaths(
            database_path=self.root / "tenant" / "app.sqlite",
            media_root=self.root / "tenant" / "media",
            backup_root=self.root / "backups" / "tenant",
        )
        unit = render_tenant_dropin(self.paths, self.tenant_id, storage)
        self.assertIn(f"User=tenant-{uuid.UUID(self.tenant_id).hex[:16]}", unit)
        self.assertIn("BOOKAPP_MANAGED_RUNTIME=1", unit)
        self.assertIn("UMask=0007", unit)
        self.assertIn("LoadCredential=telegram_bot_token:", unit)
        self.assertIn("LoadCredential=bot_proxy_url:", unit)
        self.assertNotIn("PLATFORM_DATABASE_PATH", unit)
        self.assertEqual(
            f"bookapp-tenant@{self.tenant_id}.service",
            tenant_unit_name(self.tenant_id),
        )

    def test_rejects_non_uuid_tenant_inputs(self):
        with self.assertRaises(UnitTemplateError):
            tenant_credential_directory(self.paths, "../../root")
        with self.assertRaises(UnitTemplateError):
            render_tenant_dropin(
                self.paths,
                "tenant-name",
                TenantStoragePaths(
                    self.root / "tenant" / "app.sqlite",
                    self.root / "tenant" / "media",
                    self.root / "backups" / "tenant",
                ),
            )


if __name__ == "__main__":
    unittest.main()
