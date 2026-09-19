import unittest
import uuid
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from controlplane.host_operations import (
    HostOperationsError,
    HostOperationsServer,
    HostOperationsSettings,
)


class FakeLinuxOperations:
    def __init__(self):
        self.calls = []

    def ensure_tenant_identity(self, system_user):
        self.calls.append(("identity", system_user))
        return 20_001

    def prepare_tenant_storage(self, **kwargs):
        self.calls.append(("storage", kwargs))

    def initialize_tenant_database(self, **kwargs):
        self.calls.append(("schema", kwargs))

    def owner_claim_verdict(self, **kwargs):
        self.calls.append(("claim", kwargs))
        return "claimed"

    def write_runtime_material(self, **kwargs):
        self.calls.append(("material", kwargs))

    def install_tenant_unit(self, **kwargs):
        self.calls.append(("unit", kwargs))

    def start_tenant_unit(self, unit_name):
        self.calls.append(("start", unit_name))

    def check_tenant_health(self, tenant_id, generation):
        self.calls.append(("health", tenant_id, generation))

    def publish_tenant_route(self, **kwargs):
        self.calls.append(("route", kwargs))


class HostOperationsServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.settings = HostOperationsSettings(
            socket_path=root / "run" / "host-operations.sock",
            allowed_uid=20_002,
            allowed_gid=20_003,
            release_root=root / "release",
            credential_root=root / "credentials",
            runtime_root=root / "runtime",
            tenant_data_root=root / "tenants",
            tenant_backup_root=root / "backups",
            public_key_file=root / "keys" / "manifest-public.key",
            unit_root=root / "units",
            caddy_route_root=root / "routes",
            caddy_config=root / "Caddyfile",
            caddy_user="caddy",
        )
        self.operations = FakeLinuxOperations()
        self.server = object.__new__(HostOperationsServer)
        self.server.settings = self.settings
        self.server.operations = self.operations
        self.tenant_id = str(uuid.uuid4())

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_derives_all_host_paths_and_unit_names_from_tenant_uuid(self):
        result = self.server.dispatch(
            {"operation": "ensure_identity", "tenant_id": self.tenant_id}
        )
        self.assertEqual({"uid": 20_001}, result)
        self.server.dispatch(
            {"operation": "prepare_storage", "tenant_id": self.tenant_id, "uid": 20_001}
        )
        self.server.dispatch(
            {
                "operation": "install_unit",
                "tenant_id": self.tenant_id,
            }
        )
        self.server.dispatch(
            {
                "operation": "publish_route",
                "tenant_id": self.tenant_id,
                "hosts": ["tenant.example.test"],
            }
        )
        storage = self.operations.calls[1][1]
        self.assertEqual(
            {"verdict": "claimed"},
            self.server.dispatch(
                {
                    "operation": "owner_claim_verdict",
                    "tenant_id": self.tenant_id,
                    "owner_telegram_id": 101,
                }
            ),
        )

        self.assertEqual(
            self.settings.tenant_data_root / self.tenant_id / "app.sqlite",
            storage["database_path"],
        )
        unit = self.operations.calls[2][1]
        self.assertEqual(f"bookapp-tenant@{self.tenant_id}.service", unit["unit_name"])
        self.assertNotIn("tenant.example.test", unit["dropin"])
        route = self.operations.calls[3][1]["route"]
        self.assertTrue(route.startswith("tenant.example.test {"))
        self.assertIn(
            (self.settings.runtime_root / self.tenant_id / "tenant.sock").as_posix(),
            route,
        )

    def test_server_applies_mode_to_bound_socket_path(self):
        listener = MagicMock()
        listener.__enter__.return_value = listener
        listener.accept.side_effect = KeyboardInterrupt
        with (
            patch("controlplane.host_operations.socket.AF_UNIX", new=1, create=True),
            patch("controlplane.host_operations.socket.socket", return_value=listener),
            patch("controlplane.host_operations.os.chown", create=True) as chown,
            patch("controlplane.host_operations.os.chmod") as chmod,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.server.serve()
        listener.bind.assert_called_once_with(str(self.settings.socket_path))
        chown.assert_called_once_with(
            self.settings.socket_path, 0, self.settings.allowed_gid
        )
        chmod.assert_called_once_with(self.settings.socket_path, 0o660)

    def test_rejects_extra_fields_unsupported_credentials_and_untrusted_paths(self):
        with self.assertRaisesRegex(HostOperationsError, "invalid host operation"):
            self.server.dispatch(
                {
                    "operation": "prepare_storage",
                    "tenant_id": self.tenant_id,
                    "uid": 20_001,
                    "database_path": "/etc/shadow",
                }
            )
        with self.assertRaisesRegex(HostOperationsError, "unsupported tenant credential"):
            self.server.dispatch(
                {
                    "operation": "write_material",
                    "tenant_id": self.tenant_id,
                    "runtime_generation": 1,
                    "credentials": {"controller_kek": "dGVzdA"},
                    "manifest": "e30",
                    "signature": "c2lnbmF0dXJl",
                    "public_key": "cHVibGljLWtleQ",
                }
            )
        self.assertEqual([], self.operations.calls)


if __name__ == "__main__":
    unittest.main()
