import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path


_LINUX_SOCKET_AVAILABLE = (
    sys.platform == "linux"
    and hasattr(socket, "AF_UNIX")
    and hasattr(socket, "SOCK_STREAM")
    and hasattr(socket, "SO_PEERCRED")
)


@unittest.skipUnless(_LINUX_SOCKET_AVAILABLE, "Linux Unix peer sockets are required")
class LinuxSocketAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = Path(__file__).parent.parent
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.temporary_directory.cleanup()

    def _environment(self):
        return {
            **os.environ,
            "PYTHONPATH": str(self.repository),
        }

    def _wait_for_socket(self, process, path: Path):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(f"socket service exited: {stdout!r} {stderr!r}")
            time.sleep(0.02)
        self.fail(f"socket was not created: {path}")

    @staticmethod
    def _send_chunks(connection: socket.socket, payload: bytes, chunk_size: int = 3):
        for offset in range(0, len(payload), chunk_size):
            connection.sendall(payload[offset : offset + chunk_size])

    @staticmethod
    def _read_frame(connection: socket.socket) -> bytes:
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def _start_sealer(self, socket_path: Path, key_path: Path, allowed_uid: int):
        code = textwrap.dedent(
            """
            import os
            os.chown = lambda *_args: None
            from controlplane.sealer import serve
            serve()
            """
        )
        environment = self._environment()
        environment.update(
            {
                "PLATFORM_SEALER_SOCKET": str(socket_path),
                "PLATFORM_SEALER_KEK_FILE": str(key_path),
                "PLATFORM_SEALER_KEY_VERSION": "test",
                "PLATFORM_SEALER_ALLOWED_UID": str(allowed_uid),
                "PLATFORM_SEALER_ALLOWED_GID": str(os.getgid()),
            }
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=self.repository,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.processes.append(process)
        self._wait_for_socket(process, socket_path)
        return process

    def _start_host_operations(self, socket_path: Path, allowed_uid: int):
        code = textwrap.dedent(
            """
            import os
            os.chown = lambda *_args: None
            from pathlib import Path
            from controlplane.host_operations import HostOperationsServer, HostOperationsSettings

            class FakeOperations:
                def ensure_tenant_identity(self, _user):
                    return 20001

            root = Path(os.environ["TEST_ROOT"])
            settings = HostOperationsSettings(
                socket_path=Path(os.environ["TEST_SOCKET"]),
                allowed_uid=int(os.environ["TEST_UID"]),
                allowed_gid=os.getgid(),
                release_root=root / "release",
                credential_root=root / "credentials",
                runtime_root=root / "runtime",
                tenant_data_root=root / "tenants",
                tenant_backup_root=root / "backups",
                public_key_file=root / "manifest-public.key",
                unit_root=root / "units",
                caddy_route_root=root / "routes",
                caddy_config=root / "Caddyfile",
                caddy_user="caddy",
            )
            server = HostOperationsServer(settings)
            server.operations = FakeOperations()
            server.serve()
            """
        )
        environment = self._environment()
        environment.update(
            {
                "TEST_ROOT": str(self.root),
                "TEST_SOCKET": str(socket_path),
                "TEST_UID": str(allowed_uid),
            }
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=self.repository,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.processes.append(process)
        self._wait_for_socket(process, socket_path)
        return process

    def test_sealer_handles_fragmented_request_and_rejects_bad_frames(self):
        socket_path = self.root / "sealer.sock"
        key_path = self.root / "kek"
        key_path.write_text(base64.urlsafe_b64encode(b"k" * 32).decode("ascii"), encoding="ascii")
        self._start_sealer(socket_path, key_path, os.getuid())
        tenant_id = str(uuid.uuid4())
        request = json.dumps(
            {
                "tenant_id": tenant_id,
                "secret_kind": "telegram_webhook_secret",
                "generation": 1,
                "value": "fixture-value",
            },
            separators=(",", ":"),
        ).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(socket_path))
            self._send_chunks(connection, request)
            response = self._read_frame(connection)
        self.assertNotIn(b"fixture-value", response)
        self.assertEqual(True, json.loads(response)["ok"])

        for bad in (
            b"not-json\n",
            b"{}\ntrailing",
            b'{"tenant_id":"00000000-0000-0000-0000-000000000000","secret_kind":"telegram_bot_token","generation":1,"value":"\\ud800"}\n',
            b"x" * 65_537 + b"\n",
        ):
            with self.subTest(bad_length=len(bad)):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.connect(str(socket_path))
                    connection.sendall(bad)
                    self.assertEqual({"ok": False}, json.loads(self._read_frame(connection)))

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(socket_path))
            connection.sendall(request)
            self.assertEqual(True, json.loads(self._read_frame(connection))["ok"])

    def test_sealer_rejects_untrusted_peer_uid(self):
        socket_path = self.root / "sealer-unauthorized.sock"
        key_path = self.root / "unauthorized-kek"
        key_path.write_text(
            base64.urlsafe_b64encode(b"k" * 32).decode("ascii"), encoding="ascii"
        )
        self._start_sealer(socket_path, key_path, os.getuid() + 1)
        request = json.dumps(
            {
                "tenant_id": str(uuid.uuid4()),
                "secret_kind": "telegram_webhook_secret",
                "generation": 1,
                "value": "fixture-value",
            },
            separators=(",", ":"),
        ).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(socket_path))
            connection.sendall(request)
            self.assertEqual({"ok": False}, json.loads(self._read_frame(connection)))

    def test_host_operations_rejects_untrusted_peer_uid(self):
        socket_path = self.root / "host-unauthorized.sock"
        self._start_host_operations(socket_path, os.getuid() + 1)
        request = json.dumps(
            {"operation": "ensure_identity", "tenant_id": str(uuid.uuid4())},
            separators=(",", ":"),
        ).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(socket_path))
            connection.sendall(request)
            self.assertEqual({"ok": False}, json.loads(self._read_frame(connection)))

    def test_host_operations_handles_fragmented_request_and_bad_frame_recovery(self):
        socket_path = self.root / "host.sock"
        self._start_host_operations(socket_path, os.getuid())
        tenant_id = str(uuid.uuid4())
        request = json.dumps(
            {"operation": "ensure_identity", "tenant_id": tenant_id},
            separators=(",", ":"),
        ).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(socket_path))
            self._send_chunks(connection, request, chunk_size=2)
            self.assertEqual({"ok": True, "uid": 20001}, json.loads(self._read_frame(connection)))

        for bad in (b"not-json\n", b"{}\ntrailing", b"x" * 262_145 + b"\n"):
            with self.subTest(bad_length=len(bad)):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.connect(str(socket_path))
                    connection.sendall(bad)
                    self.assertEqual({"ok": False}, json.loads(self._read_frame(connection)))

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(socket_path))
            connection.sendall(request)
            self.assertEqual({"ok": True, "uid": 20001}, json.loads(self._read_frame(connection)))


if __name__ == "__main__":
    unittest.main()
