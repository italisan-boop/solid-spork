import unittest
from pathlib import Path


class ManagedBootstrapAssetsTests(unittest.TestCase):
    def test_bootstrap_is_idempotent_and_does_not_enable_services(self):
        script_path = (
            Path(__file__).parent.parent / "deploy" / "bootstrap-managed-platform.sh"
        )
        self.assertNotIn(b"\r\n", script_path.read_bytes())
        script = script_path.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", script)
        self.assertIn("ensure_user platform-console platform-console", script)
        self.assertIn("ensure_user platform-bot platform-bot", script)
        self.assertIn("refusing to overwrite", script)
        self.assertIn("create_controller_keys", script)
        self.assertIn("ensure_user platform-controller platform-control", script)
        self.assertIn("write_sealer_environment", script)
        self.assertIn("write_host_operations_environment", script)
        self.assertIn("write_controller_environment", script)
        self.assertIn("00-placeholder.caddy", script)
        self.assertIn("systemctl daemon-reload", script)
        self.assertNotIn("systemctl enable", script)
        self.assertNotIn("systemctl start", script)
        self.assertNotIn("PLATFORM_BOT_TOKEN=", script)
        self.assertIn("bookapp-logging.caddy", script)
        self.assertIn("import /etc/caddy/bookapp-logging.caddy", script)
        self.assertIn("import /etc/caddy/bookapp-tenants.import", script)
        self.assertIn("ensure_state_directory", script)
        self.assertIn('ensure_state_directory "$STATE_ROOT/tenant-runtime"', script)
        self.assertIn("setfacl -m g::---,o::---", script)
        self.assertIn('require_command "$command"', script)
        self.assertIn("caddy chown chmod cmp curl cut getent", script)
        self.assertIn('getent passwd caddy >/dev/null 2>&1', script)
        self.assertIn("PLATFORM_CONTROLLER_LOCK_FILE=", script)
        self.assertIn("ensure_release_virtualenv", script)
        self.assertIn("BOOKAPP_PYTHON_BIN:-python3.12", script)
        self.assertIn("-m pip install --require-hashes -r", script)
        self.assertIn(".bookapp-requirements.sha256", script)
        self.assertIn("sha256sum", script)


if __name__ == "__main__":
    unittest.main()
