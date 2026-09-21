import sqlite3
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from controlplane.linux_operations import LinuxOperationsError, LinuxOperationsPaths, LinuxPrivilegedOperations


class LinuxPrivilegedOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.paths = LinuxOperationsPaths(
            credential_root=self.root / "credentials",
            public_key_file=self.root / "keys" / "manifest-public.key",
            unit_root=self.root / "units",
            caddy_route_root=self.root / "routes",
            caddy_config=self.root / "Caddyfile",
            runtime_root=self.root / "runtime",
            caddy_user="caddy",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _operations(self):
        operations = object.__new__(LinuxPrivilegedOperations)
        operations.paths = self.paths
        return operations

    def test_caddy_user_is_strictly_validated(self):
        with self.assertRaisesRegex(LinuxOperationsError, "invalid Caddy user"):
            LinuxOperationsPaths(
                credential_root=self.paths.credential_root,
                public_key_file=self.paths.public_key_file,
                unit_root=self.paths.unit_root,
                caddy_route_root=self.paths.caddy_route_root,
                caddy_config=self.paths.caddy_config,
                runtime_root=self.paths.runtime_root,
                caddy_user="caddy;id",
            )

    def test_storage_and_runtime_acls_are_tenant_and_caddy_scoped(self):
        runtime_directory = self.paths.runtime_root / "tenant-id"
        with (
            patch("controlplane.linux_operations._run") as run,
            patch("controlplane.linux_operations.os.chown", create=True),
            patch.object(Path, "mkdir"),
            patch.object(Path, "chmod"),
        ):
            self._operations().prepare_tenant_storage(
                uid=20_001,
                database_path=self.root / "tenants" / "tenant-id" / "app.sqlite",
                media_root=self.root / "tenants" / "tenant-id" / "media",
                backup_root=self.root / "backups" / "tenant-id" / "backups",
                runtime_directory=runtime_directory,
            )
        commands = [call.args[0] for call in run.call_args_list]
        self.assertCountEqual(
            [
                ["setfacl", "-m", "u:20001:--x,m::--x", str(self.root / "tenants")],
                ["setfacl", "-m", "u:20001:--x,m::--x", str(self.root / "backups")],
            ],
            commands[:2],
        )
        self.assertEqual(
            ["setfacl", "-m", "u:20001:--x,m::--x", str(self.paths.runtime_root)],
            commands[2],
        )
        self.assertEqual(
            ["setfacl", "-m", "u:caddy:--x,m::--x", str(self.paths.runtime_root)],
            commands[3],
        )
        self.assertEqual(
            ["setfacl", "-m", "u:caddy:--x,m::--x", str(runtime_directory)],
            commands[4],
        )
        self.assertEqual(
            ["setfacl", "-m", "d:u:caddy:rw-,d:m::rw-", str(runtime_directory)],
            commands[5],
        )

    def test_materialization_writes_empty_optional_proxy_credential(self):
        tenant_id = str(uuid.uuid4())
        self._operations().write_runtime_material(
            tenant_id=tenant_id,
            runtime_generation=1,
            credentials={
                "telegram_bot_token": b"123456:tenant-token",
                "telegram_webhook_secret": b"tenant-webhook-secret",
            },
            manifest=b"{}",
            signature=b"signature",
            public_key=b"public-key",
        )
        directory = self.paths.credential_root / tenant_id
        self.assertEqual(b"", (directory / "bot_proxy_url").read_bytes())
        self.assertEqual(b"", (directory / "delivery_encryption_keys").read_bytes())
        self.assertEqual(
            b"123456:tenant-token", (directory / "telegram_bot_token").read_bytes()
        )

    def test_materialization_writes_delivery_credential_with_root_only_mode(self):
        tenant_id = str(uuid.uuid4())
        credential = b'{"version":1,"active_key_id":"delivery-v1-test","keys":{"delivery-v1-test":"test"}}'
        self._operations().write_runtime_material(
            tenant_id=tenant_id,
            runtime_generation=1,
            credentials={
                "telegram_bot_token": b"123456:tenant-token",
                "telegram_webhook_secret": b"tenant-webhook-secret",
                "delivery_encryption_keys": credential,
            },
            manifest=b"{}",
            signature=b"signature",
            public_key=b"public-key",
        )
        path = self.paths.credential_root / tenant_id / "delivery_encryption_keys"
        self.assertEqual(credential, path.read_bytes())
        if os.name == "posix":
            self.assertEqual(0o400, path.stat().st_mode & 0o777)

    def test_withdraw_tenant_route_validates_socket_owner_and_reloads_caddy(self):
        tenant_id = str(uuid.uuid4())
        runtime_directory = self.paths.runtime_root / tenant_id
        runtime_directory.mkdir(parents=True)
        route = self.paths.caddy_route_root / f"{tenant_id}.caddy"
        route.parent.mkdir(parents=True)
        route.write_text(
            f"tenant.example.test {{\n reverse_proxy unix//{runtime_directory / 'tenant.sock'}\n}}\n",
            encoding="utf-8",
        )
        with patch("controlplane.linux_operations._run") as run:
            self._operations().withdraw_tenant_route(tenant_id)
        self.assertFalse(route.exists())
        self.assertEqual(
            [
                ["caddy", "validate", "--config", str(self.paths.caddy_config)],
                ["systemctl", "reload", "caddy"],
            ],
            [call.args[0] for call in run.call_args_list],
        )

    def test_withdraw_tenant_route_rejects_unowned_route(self):
        tenant_id = str(uuid.uuid4())
        route = self.paths.caddy_route_root / f"{tenant_id}.caddy"
        route.parent.mkdir(parents=True)
        route.write_text("other tenant socket", encoding="utf-8")
        with self.assertRaisesRegex(LinuxOperationsError, "not owned"):
            self._operations().withdraw_tenant_route(tenant_id)
        self.assertTrue(route.exists())

    def test_remove_tenant_runtime_material_is_idempotent_and_scoped(self):
        tenant_id = str(uuid.uuid4())
        unit = self.paths.unit_root / f"bookapp-tenant@{tenant_id}.service.d"
        unit.mkdir(parents=True)
        (unit / "runtime.conf").write_text("[Service]", encoding="utf-8")
        for root, names in (
            (
                self.paths.credential_root,
                ("telegram_bot_token", "telegram_webhook_secret", "delivery_encryption_keys", "runtime.json", "runtime.sig"),
            ),
            (self.paths.runtime_root, ("tenant.sock",)),
        ):
            directory = root / tenant_id
            directory.mkdir(parents=True)
            for name in names:
                (directory / name).write_bytes(b"fixture")
        self._operations().remove_tenant_runtime_material(tenant_id)
        self._operations().remove_tenant_runtime_material(tenant_id)
        self.assertFalse(unit.exists())
        self.assertFalse((self.paths.credential_root / tenant_id).exists())
        self.assertFalse((self.paths.runtime_root / tenant_id).exists())

    def test_withdraw_tenant_route_rolls_back_when_caddy_reload_fails(self):
        tenant_id = str(uuid.uuid4())
        runtime_directory = self.paths.runtime_root / tenant_id
        runtime_directory.mkdir(parents=True)
        route = self.paths.caddy_route_root / f"{tenant_id}.caddy"
        route.parent.mkdir(parents=True)
        previous = f"tenant.example.test {{ reverse_proxy unix//{runtime_directory / 'tenant.sock'} }}"
        route.write_text(previous, encoding="utf-8")
        with patch(
            "controlplane.linux_operations._run",
            side_effect=[None, LinuxOperationsError("reload failed")],
        ):
            with self.assertRaisesRegex(LinuxOperationsError, "reload failed"):
                self._operations().withdraw_tenant_route(tenant_id)
        self.assertEqual(previous, route.read_text(encoding="utf-8"))

    def test_tenant_unit_start_restarts_only_valid_tenant_unit(self):
        tenant_id = str(uuid.uuid4())
        unit_name = f"bookapp-tenant@{tenant_id}.service"
        with patch("controlplane.linux_operations._run") as run:
            self._operations().start_tenant_unit(unit_name)
        run.assert_called_once_with(["systemctl", "restart", unit_name])
        with self.assertRaisesRegex(LinuxOperationsError, "invalid tenant unit"):
            self._operations().start_tenant_unit("bookapp-tenant@../../root.service")

    def test_identity_lookup_creates_only_explicitly_absent_user(self):

        user = "tenant-0123456789abcdef"
        with (
            patch(
                "controlplane.linux_operations.subprocess.run",
                side_effect=[
                    SimpleNamespace(returncode=1, stdout=""),
                    SimpleNamespace(returncode=0, stdout="20001\n"),
                ],
            ),
            patch("controlplane.linux_operations._run") as run,
        ):
            self.assertEqual(20_001, self._operations().ensure_tenant_identity(user))
        self.assertEqual("useradd", run.call_args.args[0][0])

    def test_identity_lookup_failure_does_not_create_user(self):
        user = "tenant-0123456789abcdef"
        with (
            patch(
                "controlplane.linux_operations.subprocess.run",
                return_value=SimpleNamespace(returncode=2, stdout=""),
            ),
            patch("controlplane.linux_operations._run") as run,
        ):
            with self.assertRaisesRegex(LinuxOperationsError, "identity lookup"):
                self._operations().ensure_tenant_identity(user)
        run.assert_not_called()

    def test_unchanged_caddy_route_skips_global_reload(self):
        tenant_id = str(uuid.uuid4())
        target = self.paths.caddy_route_root / f"{tenant_id}.caddy"
        target.parent.mkdir()
        target.write_text("same route", encoding="utf-8")
        with patch("controlplane.linux_operations._run") as run:
            self._operations().publish_tenant_route(
                tenant_id=tenant_id,
                route="same route",
            )
        run.assert_not_called()

    def test_failed_caddy_candidate_restores_previous_route(self):
        tenant_id = str(uuid.uuid4())
        target = self.paths.caddy_route_root / f"{tenant_id}.caddy"
        target.parent.mkdir()
        target.write_text("previous route", encoding="utf-8")
        with patch(
            "controlplane.linux_operations._run",
            side_effect=LinuxOperationsError("validation failed"),
        ):
            with self.assertRaisesRegex(LinuxOperationsError, "validation failed"):
                self._operations().publish_tenant_route(
                    tenant_id=tenant_id,
                    route="candidate route",
                )
        self.assertEqual("previous route", target.read_text(encoding="utf-8"))

    def test_failed_new_caddy_candidate_is_removed(self):
        tenant_id = str(uuid.uuid4())
        target = self.paths.caddy_route_root / f"{tenant_id}.caddy"
        with patch(
            "controlplane.linux_operations._run",
            side_effect=LinuxOperationsError("reload failed"),
        ):
            with self.assertRaisesRegex(LinuxOperationsError, "reload failed"):
                self._operations().publish_tenant_route(
                    tenant_id=tenant_id,
                    route="candidate route",
                )
        self.assertFalse(target.exists())

    def test_tenant_unit_install_enables_unit(self):
        tenant_id = str(uuid.uuid4())
        unit_name = f"bookapp-tenant@{tenant_id}.service"
        with patch("controlplane.linux_operations._run") as run:
            self._operations().install_tenant_unit(
                unit_name=unit_name,
                dropin="[Service]\nExecStart=/usr/bin/true\n",
            )
        self.assertEqual(
            [
                ["systemctl", "daemon-reload"],
                ["systemctl", "enable", unit_name],
            ],
            [call.args[0] for call in run.call_args_list],
        )

    def test_private_tenant_health_contract(self):
        tenant_id = "2b1e9b57-7698-4388-a3f6-50c9d61e5bfb"
        with patch(
            "controlplane.linux_operations._run",
            side_effect=[SimpleNamespace(stdout=""), SimpleNamespace(stdout='{"tenant_id":"2b1e9b57-7698-4388-a3f6-50c9d61e5bfb","generation":7}')],
        ) as run:
            self._operations().check_tenant_health(tenant_id, 7)
        self.assertEqual(
            ["systemctl", "is-active", "--quiet", f"bookapp-tenant@{tenant_id}.service"],
            run.call_args_list[0].args[0],
        )
        self.assertEqual("curl", run.call_args_list[1].args[0][0])
        self.assertIn("--unix-socket", run.call_args_list[1].args[0])
        self.assertIn("http://localhost/health", run.call_args_list[1].args[0])

        with patch(
            "controlplane.linux_operations._run",
            side_effect=[SimpleNamespace(stdout=""), SimpleNamespace(stdout='{"tenant_id":"2b1e9b57-7698-4388-a3f6-50c9d61e5bfb","generation":8}')],
        ):
            with self.assertRaisesRegex(LinuxOperationsError, "health response"):
                self._operations().check_tenant_health(tenant_id, 7)

    def test_private_tenant_health_retries_while_unit_starts(self):
        tenant_id = "2b1e9b57-7698-4388-a3f6-50c9d61e5bfb"
        with (
            patch(
                "controlplane.linux_operations._run",
                side_effect=[
                    LinuxOperationsError("unit is starting"),
                    LinuxOperationsError("unit is starting"),
                    SimpleNamespace(stdout=""),
                    SimpleNamespace(
                        stdout=(
                            '{"tenant_id":"2b1e9b57-7698-4388-a3f6-50c9d61e5bfb",'
                            '"generation":7}'
                        )
                    ),
                ],
            ),
            patch("controlplane.linux_operations.time.sleep") as sleep,
        ):
            self._operations().check_tenant_health(tenant_id, 7)
        self.assertEqual(2, sleep.call_count)
        sleep.assert_called_with(1)

    def test_database_initialization_records_tenant_metadata(self):
        database_path = self.root / "tenants" / "tenant-id" / "app.sqlite"
        database_path.parent.mkdir(parents=True)
        tenant_id = "2b1e9b57-7698-4388-a3f6-50c9d61e5bfb"
        with (
            patch("controlplane.linux_operations.os.chown", create=True),
            patch.object(Path, "chmod"),
        ):
            self._operations().initialize_tenant_database(
                database_path=database_path,
                uid=20_001,
                tenant_id=tenant_id,
                canonical_host="client.shops.example.test",
                owner_telegram_id=101,
                display_name="Client shop",
            )
        database = sqlite3.connect(database_path)
        try:
            metadata = database.execute(
                "SELECT tenant_id, canonical_host, owner_telegram_id FROM tenant_runtime_metadata"
            ).fetchone()
            storefront = database.execute(
                "SELECT store_name FROM storefront_settings WHERE id = 1"
            ).fetchone()
        finally:
            database.close()
        self.assertEqual((tenant_id, "client.shops.example.test", 101), metadata)
        self.assertEqual(("Client shop",), storefront)


if __name__ == "__main__":
    unittest.main()
