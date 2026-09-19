import tempfile
import unittest
import uuid
from pathlib import Path

from controlplane.unit_templates import ControllerPaths, TenantStoragePaths, render_tenant_dropin


class ManagedPlatformAssetTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.units = Path(__file__).parent.parent / "deploy" / "systemd"

    def tearDown(self):
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.root.rmdir()

    def _unit(self, name: str) -> str:
        return (self.units / name).read_text(encoding="utf-8")

    def test_service_identity_and_mutation_boundaries(self):
        console = self._unit("bookapp-console.service")
        controller = self._unit("bookapp-controller.service")
        host_operations = self._unit("bookapp-host-operations.service")
        sealer = self._unit("bookapp-sealer.service")
        platform_bot = self._unit("bookapp-platform-bot.service")

        self.assertIn("User=platform-console", console)
        self.assertIn("Group=platform-control", console)
        self.assertIn(
            "Environment=PLATFORM_SEALER_SOCKET=/run/bookapp-sealer/sealer.sock",
            console,
        )
        self.assertIn("ReadWritePaths=/var/lib/bookapp/control", console)
        self.assertNotIn("controller-kek", console)

        self.assertIn("User=platform-controller", controller)
        self.assertIn("Group=platform-control", controller)
        self.assertIn("LoadCredential=controller_kek:", controller)
        self.assertIn("LoadCredential=controller_signing_key:", controller)
        self.assertIn("ReadWritePaths=/var/lib/bookapp/control", controller)
        self.assertNotIn("/etc/caddy", controller)
        self.assertNotIn("/etc/systemd/system", controller)

        self.assertIn("User=root", host_operations)
        self.assertIn("Group=platform-control", host_operations)
        self.assertIn("EnvironmentFile=/etc/bookapp/host-operations.env", host_operations)
        self.assertIn("ReadWritePaths=/var/lib/bookapp /run/bookapp-host-operations /etc", host_operations)

        self.assertIn("User=root", sealer)
        self.assertIn("WorkingDirectory=/opt/bookapp/releases/current", sealer)
        self.assertIn("EnvironmentFile=/etc/bookapp/sealer.env", sealer)
        self.assertIn("ReadWritePaths=/run/bookapp-sealer", sealer)

        self.assertIn("User=platform-bot", platform_bot)
        self.assertIn("LoadCredential=platform_bot_token:", platform_bot)
        self.assertNotIn("PLATFORM_BOT_TOKEN=", platform_bot)

        for service in (console, controller, host_operations, sealer, platform_bot):
            self.assertIn("WorkingDirectory=/opt/bookapp/releases/current", service)
            self.assertIn("NoNewPrivileges", service)

    def test_generated_tenant_dropin_is_tenant_scoped(self):
        tenant_id = str(uuid.uuid4())
        paths = ControllerPaths(
            release_root=self.root / "release",
            credential_root=self.root / "credentials",
            runtime_root=self.root / "runtime",
            tenant_data_root=self.root / "tenants",
            tenant_backup_root=self.root / "backups",
            public_key_file=self.root / "manifest-public.key",
        )
        storage = TenantStoragePaths(
            database_path=self.root / "tenants" / tenant_id / "app.sqlite",
            media_root=self.root / "tenants" / tenant_id / "media",
            backup_root=self.root / "backups" / tenant_id / "backups",
        )
        dropin = render_tenant_dropin(paths, tenant_id, storage)
        tenant_user = f"tenant-{uuid.UUID(tenant_id).hex[:16]}"
        self.assertIn(f"User={tenant_user}", dropin)
        self.assertIn(f"Group={tenant_user}", dropin)
        self.assertIn(f"WorkingDirectory={paths.release_root}", dropin)
        self.assertIn("Environment=BOOKAPP_MANAGED_RUNTIME=1", dropin)
        self.assertIn("LoadCredential=telegram_bot_token:", dropin)
        self.assertIn("LoadCredential=telegram_webhook_secret:", dropin)
        self.assertIn("LoadCredential=bot_proxy_url:", dropin)
        self.assertIn(str(storage.database_path.parent), dropin)
        self.assertIn(str(storage.media_root), dropin)
        self.assertIn(str(storage.backup_root), dropin)
        self.assertIn(str(paths.runtime_root / tenant_id), dropin)
        self.assertNotIn(str(paths.credential_root), dropin.split("ReadWritePaths=", 1)[1])
        self.assertNotIn("/etc/systemd/system", dropin)
        self.assertNotIn("/etc/caddy", dropin)


if __name__ == "__main__":
    unittest.main()
