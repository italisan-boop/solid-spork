import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path


_LINUX_SOCKET_AVAILABLE = (
    sys.platform == "linux"
    and hasattr(socket, "AF_UNIX")
    and hasattr(socket, "SO_PEERCRED")
)


class ManagedPlatformVerifierArgumentTests(unittest.TestCase):
    def test_verifier_has_bounded_private_health_probe_without_body_capture(self):
        script = (
            Path(__file__).parent.parent / "deploy" / "verify-managed-platform.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("--connect-timeout 2", script)
        self.assertIn("--max-time 5", script)
        self.assertNotIn('payload="$(curl', script)
        self.assertNotIn("printf '%s' \"$payload\"", script)

    def test_rejects_invalid_tenant_before_commands_or_socket_access(self):
        script = Path(__file__).parent.parent / "deploy" / "verify-managed-platform.sh"
        script_argument = script.as_posix()
        bash_binary = shutil.which("bash") or "bash"
        result = subprocess.run(
            [
                bash_binary,
                script_argument,
                "--socket",
                "/tmp/tenant.sock",
                "--tenant",
                "../../root",
                "--generation",
                "7",
                "--unit",
                "bookapp-tenant@../../root.service",
            ],
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("tenant id must be a UUID", result.stderr)


@unittest.skipUnless(_LINUX_SOCKET_AVAILABLE, "Linux Unix sockets are required")
class ManagedPlatformVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.script = (
            Path(__file__).parent.parent / "deploy" / "verify-managed-platform.sh"
        )
        self.tenant_id = str(uuid.uuid4())
        self.unit_name = f"bookapp-tenant@{self.tenant_id}.service"
        self.socket_path = self.root / "tenant.sock"
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        systemctl = self.bin_directory / "systemctl"
        systemctl.write_text("#!/usr/bin/env bash\nexit \"${SYSTEMCTL_EXIT:-0}\"\n")
        systemctl.chmod(0o755)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _serve_once(self, payload: str):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen(1)

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.recv(4096)
                    body = payload.encode("utf-8")
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                        + body
                    )
            finally:
                listener.close()

        thread = threading.Thread(target=serve)
        thread.start()
        return thread

    def _run(self, *arguments, systemctl_exit=0):
        environment = {
            **os.environ,
            "PATH": str(self.bin_directory) + os.pathsep + os.environ["PATH"],
            "PYTHON_BIN": sys.executable,
            "SYSTEMCTL_EXIT": str(systemctl_exit),
        }
        return subprocess.run(
            ["bash", str(self.script), *arguments],
            text=True,
            capture_output=True,
            env=environment,
            timeout=10,
        )

    def _arguments(self, *, tenant_id=None, generation="7", socket_path=None):
        selected_tenant_id = tenant_id or self.tenant_id
        return (
            "--socket",
            str(socket_path or self.socket_path),
            "--tenant",
            selected_tenant_id,
            "--generation",
            generation,
            "--unit",
            f"bookapp-tenant@{selected_tenant_id}.service",
        )

    def test_accepts_exact_private_socket_health_contract(self):
        thread = self._serve_once(
            json.dumps({"tenant_id": self.tenant_id, "generation": 7})
        )
        result = self._run(*self._arguments())
        thread.join(timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("managed tenant verification passed\n", result.stdout)

    def test_rejects_unavailable_socket(self):
        result = self._run(*self._arguments())
        self.assertNotEqual(0, result.returncode)
        self.assertIn("socket is unavailable", result.stderr)

    def test_rejects_inactive_unit_without_socket_probe(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        try:
            result = self._run(*self._arguments(), systemctl_exit=1)
        finally:
            listener.close()
        self.assertNotEqual(0, result.returncode)
        self.assertIn("tenant unit is not active", result.stderr)

    def test_rejects_malformed_or_mismatched_health_without_echoing_body(self):
        for payload in (
            "not-json",
            json.dumps({"tenant_id": self.tenant_id, "generation": 8}),
            json.dumps({"tenant_id": str(uuid.uuid4()), "generation": 7}),
        ):
            with self.subTest(payload=payload):
                thread = self._serve_once(payload)
                result = self._run(*self._arguments())
                thread.join(timeout=5)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("tenant health response is invalid", result.stderr)
                self.assertNotIn(payload, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
