import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from controlplane.materializer import ManifestSigner, materialize_runtime
from controlplane.plan_policy import Plan, effective_entitlements
from controlplane.schema import initialize
from controlplane.secret_envelopes import EnvelopeCipher, SecretEnvelope
from controlplane.tenants import (
    create_tenant,
    get_tenant,
    request_custom_domain,
    set_custom_domain_verification,
    set_secret_envelopes,
    ensure_delivery_encryption_envelope,
    queue_missing_delivery_encryption_reconciliations,
)


class RuntimeMaterializerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.control_database = self.root / "control.sqlite"
        initialize(self.control_database)
        self.tenant = create_tenant(
            self.control_database,
            display_name="Materialized tenant",
            slug="materialized-tenant",
            owner_telegram_id=101,
            plan=Plan.BUSINESS,
            tenant_data_root=self.root / "tenants",
            tenant_backup_root=self.root / "backups",
            tenant_base_domain="shops.example.test",
            actor_telegram_id=1,
        )
        self.cipher = EnvelopeCipher(b"m" * 32, "v1")
        set_secret_envelopes(
            self.control_database,
            tenant_id=self.tenant.id,
            values={
                "telegram_bot_token": "123456:materialized-token",
                "telegram_webhook_secret": "materialized-webhook-secret",
            },
            actor_telegram_id=1,
            sealer=self.cipher,
        )
        database = sqlite3.connect(self.control_database)
        try:
            database.execute(
                "UPDATE platform_tenants SET lifecycle_state = 'awaiting_owner_claim' WHERE id = ?",
                (self.tenant.id,),
            )
            database.commit()
        finally:
            database.close()
        self.tenant = get_tenant(self.control_database, self.tenant.id)
        self.signer = ManifestSigner(Ed25519PrivateKey.generate())

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_materializes_only_required_credentials_and_signed_manifest(self):
        material = materialize_runtime(
            self.control_database,
            tenant=self.tenant,
            entitlements=effective_entitlements(Plan.BUSINESS),
            cipher=self.cipher,
            signer=self.signer,
            socket_path=self.root / "runtime" / "tenant.sock",
        )
        self.assertEqual(
            {"telegram_bot_token", "telegram_webhook_secret"},
            set(material.credentials),
        )
        self.assertEqual(b"123456:materialized-token", material.credentials["telegram_bot_token"])
        payload = json.loads(material.manifest)
        self.assertEqual(self.tenant.id, payload["tenant_id"])
        self.assertEqual([self.tenant.canonical_host], payload["allowed_hosts"])
        self.assertNotIn("materialized-token", material.manifest.decode())
        self.assertTrue(self.signer.public_key())
        self.assertTrue(material.signature)

    def test_queues_existing_managed_tenants_missing_delivery_credentials(self):
        self.assertEqual(
            1, queue_missing_delivery_encryption_reconciliations(self.control_database)
        )
        self.assertEqual(
            0, queue_missing_delivery_encryption_reconciliations(self.control_database)
        )
        database = sqlite3.connect(self.control_database)
        try:
            pending = database.execute(
                """
                SELECT operation, state FROM tenant_deployment_jobs
                WHERE tenant_id = ?
                """,
                (self.tenant.id,),
            ).fetchall()
            credential_count = database.execute(
                """
                SELECT COUNT(*) FROM tenant_secret_envelopes
                WHERE tenant_id = ? AND secret_kind = 'delivery_encryption_keys'
                """,
                (self.tenant.id,),
            ).fetchone()[0]
        finally:
            database.close()
        self.assertEqual([("redeploy", "pending")], pending)
        self.assertEqual(0, credential_count)

    def test_initializes_unique_delivery_keyrings_as_non_manifest_credentials(self):
        self.assertTrue(
            ensure_delivery_encryption_envelope(
                self.control_database,
                tenant_id=self.tenant.id,
                cipher=self.cipher,
            )
        )
        self.assertFalse(
            ensure_delivery_encryption_envelope(
                self.control_database,
                tenant_id=self.tenant.id,
                cipher=self.cipher,
            )
        )
        second_tenant = create_tenant(
            self.control_database,
            display_name="Second materialized tenant",
            slug="second-materialized-tenant",
            owner_telegram_id=102,
            plan=Plan.BUSINESS,
            tenant_data_root=self.root / "tenants",
            tenant_backup_root=self.root / "backups",
            tenant_base_domain="shops.example.test",
            actor_telegram_id=1,
        )
        self.assertTrue(
            ensure_delivery_encryption_envelope(
                self.control_database,
                tenant_id=second_tenant.id,
                cipher=self.cipher,
            )
        )
        material = materialize_runtime(
            self.control_database,
            tenant=self.tenant,
            entitlements=effective_entitlements(Plan.BUSINESS),
            cipher=self.cipher,
            signer=self.signer,
            socket_path=self.root / "runtime" / "tenant.sock",
        )
        delivery_credential = material.credentials["delivery_encryption_keys"]
        self.assertEqual(1, json.loads(delivery_credential)["version"])
        self.assertNotIn(delivery_credential.decode("utf-8"), material.manifest.decode("utf-8"))
        database = sqlite3.connect(self.control_database)
        try:
            rows = database.execute(
                """
                SELECT tenant_id, generation, algorithm, ciphertext, data_nonce,
                       wrapped_key, wrap_nonce, key_version
                FROM tenant_secret_envelopes
                WHERE secret_kind = 'delivery_encryption_keys'
                """
            ).fetchall()
            audit_details = database.execute(
                """
                SELECT details_json FROM platform_audit_events
                WHERE tenant_id = ? AND action = 'tenant.delivery_encryption.initialized'
                """,
                (self.tenant.id,),
            ).fetchone()[0]
        finally:
            database.close()
        self.assertEqual(
            {"generation": self.tenant.runtime_generation, "keyring_version": 1},
            json.loads(audit_details),
        )
        keyrings = {
            row[0]: json.loads(
                self.cipher.open(
                    row[0],
                    "delivery_encryption_keys",
                    row[1],
                    SecretEnvelope(
                        algorithm=row[2],
                        ciphertext=row[3],
                        data_nonce=row[4],
                        wrapped_key=row[5],
                        wrap_nonce=row[6],
                        key_version=row[7],
                    ),
                )
            )
            for row in rows
        }
        self.assertEqual(2, len(keyrings))
        self.assertNotEqual(
            keyrings[self.tenant.id]["keys"], keyrings[second_tenant.id]["keys"]
        )

    def test_materializes_configured_tenant_proxy_without_manifest_leakage(self):
        proxy_url = "http://proxy-user:proxy-password@203.0.113.10:3128"
        tenant = set_secret_envelopes(
            self.control_database,
            tenant_id=self.tenant.id,
            values={"bot_proxy_url": proxy_url},
            actor_telegram_id=1,
            sealer=self.cipher,
        )
        material = materialize_runtime(
            self.control_database,
            tenant=tenant,
            entitlements=effective_entitlements(Plan.BUSINESS),
            cipher=self.cipher,
            signer=self.signer,
            socket_path=self.root / "runtime" / "tenant.sock",
        )
        self.assertEqual(proxy_url.encode(), material.credentials["bot_proxy_url"])
        self.assertNotIn(proxy_url, material.manifest.decode())
    def test_disabled_custom_host_is_excluded_from_active_runtime_material(self):
        database = sqlite3.connect(self.control_database)
        try:
            database.execute(
                "UPDATE platform_tenants SET lifecycle_state = 'active' WHERE id = ?",
                (self.tenant.id,),
            )
            database.commit()
        finally:
            database.close()
        request_custom_domain(
            self.control_database,
            tenant_id=self.tenant.id,
            host="books.materialized.example",
            actor_telegram_id=1,
        )
        set_custom_domain_verification(
            self.control_database,
            tenant_id=self.tenant.id,
            host="books.materialized.example",
            verification_state="verified",
            actor_telegram_id=1,
        )
        active = get_tenant(self.control_database, self.tenant.id)
        material = materialize_runtime(
            self.control_database,
            tenant=active,
            entitlements=effective_entitlements(Plan.BUSINESS),
            cipher=self.cipher,
            signer=self.signer,
            socket_path=self.root / "runtime" / "tenant.sock",
        )
        self.assertEqual(
            sorted([active.canonical_host, "books.materialized.example"]), material.allowed_hosts
        )
        set_custom_domain_verification(
            self.control_database,
            tenant_id=self.tenant.id,
            host="books.materialized.example",
            verification_state="disabled",
            actor_telegram_id=1,
        )
        material = materialize_runtime(
            self.control_database,
            tenant=get_tenant(self.control_database, self.tenant.id),
            entitlements=effective_entitlements(Plan.BUSINESS),
            cipher=self.cipher,
            signer=self.signer,
            socket_path=self.root / "runtime" / "tenant.sock",
        )
        self.assertEqual([active.canonical_host], material.allowed_hosts)
