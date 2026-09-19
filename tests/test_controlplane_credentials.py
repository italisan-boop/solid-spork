import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from controlplane.credentials import read_console_credential
from controlplane.settings import ControlPlaneSettings, PlatformBotSettings


class ManagedConsoleCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.credentials = self.root / "credentials"
        self.credentials.mkdir()
        (self.credentials / "platform_bot_token").write_text(
            "123456:managed-console-token\n", encoding="utf-8"
        )
        self.environment = {
            "BOOKAPP_MANAGED_CONSOLE": "1",
            "CREDENTIALS_DIRECTORY": str(self.credentials),
            "PLATFORM_DATABASE_PATH": str(self.root / "control" / "control.sqlite"),
            "PLATFORM_TENANT_DATA_ROOT": str(self.root / "tenants"),
            "PLATFORM_TENANT_BACKUP_ROOT": str(self.root / "backups"),
            "PLATFORM_TENANT_BASE_DOMAIN": "shops.example.test",
            "PLATFORM_ADMIN_TELEGRAM_IDS": "101",
            "PLATFORM_BOT_TOKEN": "123456:ambient-token-must-not-be-used",
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_managed_console_reads_only_systemd_credential(self):
        with patch.dict(os.environ, self.environment, clear=True):
            settings = ControlPlaneSettings.from_environment()
        self.assertEqual("123456:managed-console-token", settings.bot_token)
        self.assertTrue(settings.managed_mode)

    def test_managed_console_rejects_missing_required_credential(self):
        (self.credentials / "platform_bot_token").unlink()
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(ValueError, "credential"):
                ControlPlaneSettings.from_environment()

    def test_console_credential_name_is_allowlisted(self):
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(
                "123456:managed-console-token",
                read_console_credential("platform_bot_token", required=True),
            )
            with self.assertRaisesRegex(ValueError, "unsupported"):
                read_console_credential("controller_kek", required=True)

    def test_managed_platform_bot_reads_systemd_credential(self):
        environment = {
            "BOOKAPP_MANAGED_PLATFORM_BOT": "1",
            "CREDENTIALS_DIRECTORY": str(self.credentials),
            "PLATFORM_BOT_TOKEN": "123456:ambient-token-must-not-be-used",
            "PLATFORM_ADMIN_TELEGRAM_IDS": "101",
            "PLATFORM_CONSOLE_URL": "https://platform.example.test",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = PlatformBotSettings.from_environment()
        self.assertEqual("123456:managed-console-token", settings.bot_token)

    def test_console_and_platform_bot_units_use_dedicated_credentials(self):
        systemd = Path(__file__).parent.parent / "deploy" / "systemd"
        console = (systemd / "bookapp-console.service").read_text(encoding="utf-8")
        bot = (systemd / "bookapp-platform-bot.service").read_text(encoding="utf-8")
        self.assertIn("User=platform-console", console)
        self.assertIn("BOOKAPP_MANAGED_CONSOLE=1", console)
        self.assertIn("LoadCredential=platform_bot_token:", console)
        self.assertNotIn("Environment=PLATFORM_BOT_TOKEN=", console)
        self.assertNotIn("User=root", console)
        self.assertIn("User=platform-bot", bot)
        self.assertIn("BOOKAPP_MANAGED_PLATFORM_BOT=1", bot)
        self.assertIn("LoadCredential=platform_bot_token:", bot)
        self.assertNotIn("Environment=PLATFORM_BOT_TOKEN=", bot)


if __name__ == "__main__":
    unittest.main()
