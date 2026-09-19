import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from controlplane.controller import (
    RootControllerSettings,
    controller_singleton_lock,
    wait_for_host_operations_socket,
)


class ManagedControllerSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.credentials = self.root / "credentials"
        self.credentials.mkdir()
        (self.credentials / "controller_kek").write_text("credential", encoding="utf-8")
        (self.credentials / "controller_signing_key").write_text(
            "credential", encoding="utf-8"
        )
        self.environment = {
            "BOOKAPP_MANAGED_CONTROLLER": "1",
            "CREDENTIALS_DIRECTORY": str(self.credentials),
            "PLATFORM_CONTROLLER_DATABASE_PATH": str(self.root / "control" / "control.sqlite"),
            "PLATFORM_CONTROLLER_RELEASE_ROOT": str(self.root / "release"),
            "PLATFORM_CONTROLLER_CREDENTIAL_ROOT": str(self.root / "credentials-out"),
            "PLATFORM_CONTROLLER_RUNTIME_ROOT": str(self.root / "runtime"),
            "PLATFORM_CONTROLLER_TENANT_DATA_ROOT": str(self.root / "tenants"),
            "PLATFORM_CONTROLLER_TENANT_BACKUP_ROOT": str(self.root / "backups"),
            "PLATFORM_CONTROLLER_MANIFEST_PUBLIC_KEY_FILE": str(self.root / "keys" / "public"),
            "PLATFORM_CONTROLLER_HOST_OPERATIONS_SOCKET": str(self.root / "run" / "helper.sock"),
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_managed_controller_uses_only_systemd_key_credentials(self):
        with patch.dict(os.environ, self.environment, clear=True):
            settings = RootControllerSettings.from_environment()
        self.assertEqual(
            self.credentials / "controller_kek", settings.controller_kek_file
        )
        self.assertEqual(
            self.credentials / "controller_signing_key",
            settings.controller_signing_key_file,
        )
        self.assertEqual(self.root / "run" / "helper.sock", settings.host_operations_socket)
        self.assertEqual(self.root / "control" / "controller.lock", settings.controller_lock_file)

    def test_controller_assets_separate_root_host_mutation_from_db_controller(self):
        units = Path(__file__).parent.parent / "deploy" / "systemd"
        controller = (units / "bookapp-controller.service").read_text(encoding="utf-8")
        helper = (units / "bookapp-host-operations.service").read_text(encoding="utf-8")
        console = (units / "bookapp-console.service").read_text(encoding="utf-8")
        sealer = (units / "bookapp-sealer.service").read_text(encoding="utf-8")
        self.assertIn("User=platform-controller", controller)
        self.assertIn("Group=platform-control", controller)
        self.assertIn("LoadCredential=controller_kek:", controller)
        self.assertNotIn("User=root", controller)
        self.assertNotIn("/etc/caddy", controller)
        self.assertNotIn("/etc/systemd/system", controller)
        self.assertIn("User=root", helper)
        self.assertIn("EnvironmentFile=/etc/bookapp/host-operations.env", helper)
        self.assertIn("-m controlplane.host_operations", helper)
        self.assertIn("Group=platform-control", console)
        self.assertIn("Environment=PLATFORM_SEALER_SOCKET=/run/bookapp-sealer/sealer.sock", console)
        self.assertIn("UMask=0007", console)
        self.assertIn("Group=platform-control", sealer)
        self.assertIn("WorkingDirectory=/opt/bookapp/releases/current", sealer)

    @unittest.skipUnless(os.name == "posix", "POSIX file locking is required")
    def test_controller_singleton_lock_rejects_second_owner(self):
        lock_path = self.root / "control" / "controller.lock"
        with controller_singleton_lock(lock_path):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with controller_singleton_lock(lock_path):
                    pass

    def test_waits_for_host_operations_socket_before_claiming_jobs(self):
        socket_path = self.root / "run" / "host.sock"
        with (
            patch.object(
                Path,
                "stat",
                side_effect=[
                    FileNotFoundError,
                    SimpleNamespace(st_mode=stat.S_IFSOCK),
                ],
            ),
            patch("controlplane.controller.time.sleep") as sleep,
        ):
            wait_for_host_operations_socket(socket_path, 3)
        sleep.assert_called_once_with(3)

    def test_relative_controller_paths_are_rejected_before_resolution(self):
        environment = dict(self.environment)
        environment["PLATFORM_CONTROLLER_DATABASE_PATH"] = "relative/control.sqlite"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "absolute"):
                RootControllerSettings.from_environment()


if __name__ == "__main__":
    unittest.main()
