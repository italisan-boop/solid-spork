import base64
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime.credentials import RuntimeCredentialError, read_runtime_credential
from runtime.manifest import RuntimeManifestError, load_managed_runtime_manifest


class ManagedRuntimeManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.credentials = self.root / "credentials"
        self.credentials.mkdir()
        self.private_key = Ed25519PrivateKey.generate()
        public_key = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self.public_key_path = self.root / "manifest-public.key"
        self.public_key_path.write_bytes(base64.urlsafe_b64encode(public_key).rstrip(b"="))
        self.payload = {
            "tenant_id": str(uuid.uuid4()),
            "canonical_host": "store.example.test",
            "allowed_hosts": ["store.example.test"],
            "database_path": str(self.root / "tenant" / "app.sqlite"),
            "media_root": str(self.root / "tenant" / "media"),
            "backup_root": str(self.root / "backups" / "tenant"),
            "socket_path": str(self.root / "runtime" / "tenant.sock"),
            "owner_telegram_id": 101,
            "lifecycle_state": "awaiting_owner_claim",
            "runtime_generation": 3,
            "plan": "business",
            "feature_overrides": {},
            "limit_overrides": {},
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _write_manifest(self, payload=None):
        raw = json.dumps(payload or self.payload, separators=(",", ":")).encode()
        (self.credentials / "runtime.json").write_bytes(raw)
        signature = self.private_key.sign(raw)
        (self.credentials / "runtime.sig").write_bytes(
            base64.urlsafe_b64encode(signature).rstrip(b"=")
        )

    def _environment(self):
        return {
            "BOOKAPP_MANAGED_RUNTIME": "1",
            "CREDENTIALS_DIRECTORY": str(self.credentials),
            "BOOKAPP_MANIFEST_PUBLIC_KEY_FILE": str(self.public_key_path),
        }

    def test_loads_a_signed_manifest(self):
        self._write_manifest()
        with patch.dict(os.environ, self._environment(), clear=True):
            manifest = load_managed_runtime_manifest()
        self.assertEqual(self.payload["tenant_id"], manifest.tenant_id)
        self.assertEqual("store.example.test", manifest.canonical_host)
        self.assertEqual(3, manifest.runtime_generation)
        self.assertEqual("business", manifest.entitlements.plan.value)

    def test_rejects_modified_manifest(self):
        self._write_manifest()
        (self.credentials / "runtime.json").write_text("{}", encoding="utf-8")
        with patch.dict(os.environ, self._environment(), clear=True):
            with self.assertRaises(RuntimeManifestError):
                load_managed_runtime_manifest()

    def test_managed_settings_ignore_ambient_tenant_secrets(self):
        self._write_manifest()
        (self.credentials / "telegram_bot_token").write_text(
            "123456:credential-token\n", encoding="utf-8"
        )
        (self.credentials / "telegram_webhook_secret").write_text(
            "credential-webhook-secret\n", encoding="utf-8"
        )
        (self.credentials / "bot_proxy_url").write_text(
            "http://proxy-user:proxy-password@203.0.113.10:3128\n", encoding="utf-8"
        )
        from config.settings import Settings

        environment = {
            **self._environment(),
            "BOT_TOKEN": "ambient-token-must-not-be-used",
            "BOT_PROXY_URL": "http://ambient:proxy@203.0.113.11:3128",
            "WEBHOOK_SECRET": "ambient-webhook-must-not-be-used",
            "DATABASE_PATH": "relative.sqlite",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings()
        self.assertTrue(settings.MANAGED_RUNTIME)
        self.assertEqual("123456:credential-token", settings.BOT_TOKEN)
        self.assertEqual("credential-webhook-secret", settings.WEBHOOK_SECRET)
        self.assertEqual(
            "http://proxy-user:proxy-password@203.0.113.10:3128",
            settings.BOT_PROXY_URL,
        )
        self.assertEqual(
            "https://store.example.test/setup", settings.WEBAPP_URL
        )
        self.assertEqual(str(self.root / "tenant" / "app.sqlite"), settings.DATABASE_PATH)

    def test_managed_settings_load_delivery_credential_and_ignore_ambient_values(self):
        self._write_manifest()
        (self.credentials / "telegram_bot_token").write_text(
            "123456:credential-token\n", encoding="utf-8"
        )
        (self.credentials / "telegram_webhook_secret").write_text(
            "credential-webhook-secret\n", encoding="utf-8"
        )
        key_id = "delivery-v1-test"
        keyring = base64.urlsafe_b64encode(b"d" * 32).decode("ascii").rstrip("=")
        credential = {
            "version": 1,
            "active_key_id": key_id,
            "keys": {key_id: keyring},
        }
        (self.credentials / "delivery_encryption_keys").write_text(
            json.dumps(credential), encoding="utf-8"
        )
        from config.settings import Settings
        from utils.delivery_crypto import (
            decrypt_destination,
            delivery_encryption_is_available,
            encrypt_destination,
        )

        environment = {
            **self._environment(),
            "DELIVERY_ENCRYPTION_ACTIVE_KEY_ID": "ambient-key-must-not-be-used",
            "DELIVERY_ENCRYPTION_KEYS_JSON": '{"ambient-key-must-not-be-used":"bad"}',
        }
        with patch.dict(os.environ, environment, clear=True):
            managed_settings = Settings()
        self.assertEqual(key_id, managed_settings.DELIVERY_ENCRYPTION_ACTIVE_KEY_ID)
        self.assertEqual({key_id: keyring}, json.loads(managed_settings.DELIVERY_ENCRYPTION_KEYS_JSON))
        with patch("utils.delivery_crypto.settings", managed_settings):
            self.assertTrue(delivery_encryption_is_available())
            envelope = encrypt_destination(1, "courier", {"city": "Test"})
            self.assertEqual(
                {"city": "Test"}, decrypt_destination(1, "courier", envelope)
            )

    def test_managed_settings_fail_closed_for_absent_or_malformed_delivery_credential(self):
        self._write_manifest()
        (self.credentials / "telegram_bot_token").write_text(
            "123456:credential-token\n", encoding="utf-8"
        )
        (self.credentials / "telegram_webhook_secret").write_text(
            "credential-webhook-secret\n", encoding="utf-8"
        )
        from config.settings import Settings

        environment = {
            **self._environment(),
            "DELIVERY_ENCRYPTION_ACTIVE_KEY_ID": "ambient-key-must-not-be-used",
            "DELIVERY_ENCRYPTION_KEYS_JSON": '{"ambient-key-must-not-be-used":"bad"}',
        }
        for credential in (
            None,
            "not-json",
            '{"version":true,"active_key_id":"key","keys":{}}',
            '{"version":1,"active_key_id":"key","keys":[]}',
        ):
            if credential is None:
                (self.credentials / "delivery_encryption_keys").unlink(missing_ok=True)
            else:
                (self.credentials / "delivery_encryption_keys").write_text(
                    credential, encoding="utf-8"
                )
            with patch.dict(os.environ, environment, clear=True):
                managed_settings = Settings()
            self.assertEqual("", managed_settings.DELIVERY_ENCRYPTION_ACTIVE_KEY_ID)
            self.assertEqual("", managed_settings.DELIVERY_ENCRYPTION_KEYS_JSON)

    def test_reads_only_allowlisted_credentials(self):
        (self.credentials / "telegram_bot_token").write_text(
            "123456:tenant-token\n", encoding="utf-8"
        )
        with patch.dict(os.environ, self._environment(), clear=True):
            self.assertEqual(
                "123456:tenant-token",
                read_runtime_credential("telegram_bot_token", required=True),
            )
            self.assertIsNone(read_runtime_credential("yookassa_credentials"))
            with self.assertRaises(RuntimeCredentialError):
                read_runtime_credential("../../control.sqlite")
    def test_active_managed_settings_point_tenant_bot_to_storefront_root(self):
        payload = {**self.payload, "lifecycle_state": "active"}
        self._write_manifest(payload)
        (self.credentials / "telegram_bot_token").write_text(
            "123456:credential-token\n", encoding="utf-8"
        )
        (self.credentials / "telegram_webhook_secret").write_text(
            "credential-webhook-secret\n", encoding="utf-8"
        )
        from config.settings import Settings

        with patch.dict(os.environ, self._environment(), clear=True):
            settings = Settings()
        self.assertEqual("https://store.example.test", settings.WEBAPP_URL)


if __name__ == "__main__":
    unittest.main()
