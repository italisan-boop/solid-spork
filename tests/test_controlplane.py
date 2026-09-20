from dataclasses import replace
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from controlplane.gateway import TenantHostGateway
from controlplane.plan_policy import (
    FEATURE_ANALYTICS,
    FEATURE_BROADCAST,
    Plan,
    plan_defaults,
)
from controlplane.secret_envelopes import EnvelopeCipher
from controlplane.server import create_controlplane_app
from controlplane.settings import ControlPlaneSettings
from controlplane.tenants import (
    effective_tenant_entitlements,
    get_tenant,
    set_secret_envelopes,
)
from db.schema import connect as connect_tenant_database
from runtime.factory import TenantResolutionError, tenant_context_for_host


TOKEN = "123456:control-plane-test-token"
PLATFORM_ADMIN_ID = 101


def signed_headers(user_id: int, token: str = TOKEN) -> dict[str, str]:
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "first_name": "Platform"}, separators=(",", ":"))),
    ]
    check = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


class ControlPlaneApiTests(unittest.TestCase):
    def setUp(self):
        self._environment_patch = patch.dict(os.environ, {
            "CONTROLPLANE_TEST_TENANT_BOT_TOKEN": "234567:controlplane-tenant-token",
            "CONTROLPLANE_TEST_TENANT_WEBHOOK_SECRET": "controlplane-tenant-webhook-secret",
        })
        self._environment_patch.start()
        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        self.settings = ControlPlaneSettings(
            database_path=root / "control.sqlite",
            tenant_data_root=root / "tenants",
            tenant_backup_root=root / "backups",
            tenant_base_domain="shops.example.test",
            bot_token=TOKEN,
            admin_telegram_ids=frozenset({PLATFORM_ADMIN_ID}),
            host="127.0.0.1",
            port=8100,
        )
        self.secret_sealer = EnvelopeCipher(b"e" * 32, "test")
        self.client = create_controlplane_app(
            self.settings, secret_sealer=self.secret_sealer
        ).test_client()
        self.managed_client = create_controlplane_app(
            replace(self.settings, managed_mode=True), secret_sealer=self.secret_sealer
        ).test_client()

    def test_managed_console_rejects_legacy_mutations(self):
        managed_client = create_controlplane_app(
            replace(self.settings, managed_mode=True), secret_sealer=self.secret_sealer
        ).test_client()
        response = managed_client.post(
            "/api/platform/tenants",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={
                "display_name": "Managed store",
                "slug": "managed-store",
                "owner_telegram_id": 202,
                "plan": "business",
            },
        )
        self.assertEqual(201, response.status_code)
        tenant_id = response.get_json()["id"]
        for path, method, payload in (
            (
                f"/api/platform/tenants/{tenant_id}/secret-references",
                "put",
                {"references": {}},
            ),
            (f"/api/platform/tenants/{tenant_id}/provision", "post", None),
            (f"/api/platform/tenants/{tenant_id}/activate", "post", None),
            (f"/api/platform/tenants/{tenant_id}/lifecycle", "put", {"state": "suspended"}),
        ):
            response = getattr(managed_client, method)(
                path,
                headers=signed_headers(PLATFORM_ADMIN_ID),
                json=payload,
            )
            self.assertEqual(409, response.status_code)
        response = managed_client.post(
            f"/api/platform/tenants/{tenant_id}/deployments",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"operation": "provision"},
        )
        self.assertEqual(409, response.status_code)
        self.assertIn("secrets", response.get_json()["error"])

    def test_managed_delete_queues_root_teardown_with_typed_slug(self):
        tenant = self.create_managed_tenant(slug="managed-delete-store")
        path = f"/api/platform/tenants/{tenant['id']}"
        headers = signed_headers(PLATFORM_ADMIN_ID)
        self.assertEqual(
            403,
            self.managed_client.delete(
                path,
                headers=signed_headers(999),
                json={"confirm_slug": tenant["slug"]},
            ).status_code,
        )
        self.assertEqual(
            400,
            self.managed_client.delete(
                path,
                headers=headers,
                json={"confirm_slug": "wrong-slug"},
            ).status_code,
        )
        response = self.managed_client.delete(
            path,
            headers=headers,
            json={"confirm_slug": tenant["slug"]},
        )
        self.assertEqual(202, response.status_code)
        payload = response.get_json()
        self.assertEqual("teardown", payload["job"]["operation"])
        self.assertEqual("deleting", payload["tenant"]["lifecycle_state"])
        self.assertEqual(
            409,
            self.managed_client.post(
                f"{path}/deployments",
                headers=headers,
                json={"operation": "redeploy"},
            ).status_code,
        )
        repeated = self.managed_client.delete(
            path,
            headers=headers,
            json={"confirm_slug": tenant["slug"]},
        )
        self.assertEqual(202, repeated.status_code)
        self.assertEqual(payload["job"]["id"], repeated.get_json()["job"]["id"])
        stored = get_tenant(self.settings.database_path, tenant["id"])
        self.assertEqual("managed", stored.tenant_kind)
        self.assertEqual("deleting", stored.lifecycle_state)
        connection = sqlite3.connect(self.settings.database_path)
        try:
            domains = connection.execute(
                "SELECT verification_state FROM tenant_domains WHERE tenant_id = ?",
                (tenant["id"],),
            ).fetchall()
            jobs = connection.execute(
                "SELECT operation, state FROM tenant_deployment_jobs WHERE tenant_id = ?",
                (tenant["id"],),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([("disabled",)], domains)
        self.assertEqual([("teardown", "pending")], jobs)


        response = self.client.get("/")
        try:
            self.assertEqual(200, response.status_code)
        finally:
            response.close()
        self.assertEqual(401, self.client.get("/api/platform/session").status_code)
        self.assertEqual(200, self.client.get(
            "/api/platform/session", headers=signed_headers(PLATFORM_ADMIN_ID)
        ).status_code)
        self.assertEqual(403, self.client.get(
            "/api/platform/session", headers=signed_headers(999)
        ).status_code)
        self.assertEqual(401, self.client.get(
            "/api/platform/session",
            headers=signed_headers(PLATFORM_ADMIN_ID, "123456:other-bot-token"),
        ).status_code)

    def test_sealing_does_not_hold_sqlite_writer_lock(self):
        tenant = self.create_managed_tenant(slug="nonblocking-sealer-store")
        cipher = self.secret_sealer
        database_path = self.settings.database_path

        class ConcurrentWriterSealer:
            def seal(self, tenant_id, secret_kind, generation, value):
                connection = sqlite3.connect(database_path, timeout=0.1)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.rollback()
                finally:
                    connection.close()
                return cipher.seal(tenant_id, secret_kind, generation, value)

        updated = set_secret_envelopes(
            self.settings.database_path,
            tenant_id=tenant["id"],
            values={"bot_proxy_url": "http://user:password@203.0.113.10:3128"},
            actor_telegram_id=PLATFORM_ADMIN_ID,
            sealer=ConcurrentWriterSealer(),
            managed_reconciliation=True,
        )
        self.assertIn("bot_proxy_url", self.managed_client.get(
            f"/api/platform/tenants/{updated.id}",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        ).get_json()["configured_secret_kinds"])

    def test_sealing_rejects_snapshot_changed_during_rpc(self):
        tenant = self.create_managed_tenant(slug="stale-sealer-store")
        cipher = self.secret_sealer
        database_path = self.settings.database_path

        class MutatingSealer:
            def seal(self, tenant_id, secret_kind, generation, value):
                connection = sqlite3.connect(database_path)
                try:
                    connection.execute(
                        "UPDATE platform_tenants SET runtime_generation = runtime_generation + 1 WHERE id = ?",
                        (tenant_id,),
                    )
                    connection.commit()
                finally:
                    connection.close()
                return cipher.seal(tenant_id, secret_kind, generation, value)

        with self.assertRaisesRegex(ValueError, "configuration changed"):
            set_secret_envelopes(
                self.settings.database_path,
                tenant_id=tenant["id"],
                values={"bot_proxy_url": "http://user:password@203.0.113.10:3128"},
                actor_telegram_id=PLATFORM_ADMIN_ID,
                sealer=MutatingSealer(),
                managed_reconciliation=True,
            )
        connection = sqlite3.connect(self.settings.database_path)
        try:
            envelopes = connection.execute(
                "SELECT COUNT(*) FROM tenant_secret_envelopes WHERE tenant_id = ?",
                (tenant["id"],),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, envelopes)

    def test_legacy_console_accepts_tenant_proxy_reference(self):
        tenant = self.create_tenant(slug="legacy-proxy-reference")
        response = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/secret-references/bot_proxy_url",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={
                "reference": "env:CONTROLPLANE_TEST_TENANT_PROXY",
                "version": "v1",
            },
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"configured": True, "secret_kind": "bot_proxy_url"}, response.get_json()
        )

    def test_legacy_console_rejects_managed_mutations_and_reports_mode(self):
        tenant = self.create_tenant()
        headers = signed_headers(PLATFORM_ADMIN_ID)
        self.assertEqual(
            409,
            self.client.put(
                f"/api/platform/tenants/{tenant['id']}/secrets",
                headers=headers,
                json={"secrets": {"bot_proxy_url": "http://user:password@203.0.113.10:3128"}},
            ).status_code,
        )
        self.assertEqual(
            409,
            self.client.post(
                f"/api/platform/tenants/{tenant['id']}/deployments",
                headers=headers,
                json={"operation": "provision"},
            ).status_code,
        )
        self.assertFalse(
            self.client.get("/api/platform/tenants", headers=headers).get_json()["managed_mode"]
        )
        self.assertTrue(
            self.managed_client.get("/api/platform/tenants", headers=headers).get_json()["managed_mode"]
        )

    def tearDown(self):
        self._temporary_directory.cleanup()
        self._environment_patch.stop()

    def create_tenant(self, *, plan="business", client=None, slug="client-store"):
        active_client = client or self.client
        response = active_client.post(
            "/api/platform/tenants",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={
                "display_name": "Магазин клиента",
                "slug": slug,
                "owner_telegram_id": 202,
                "plan": plan,
            },
        )
        self.assertEqual(201, response.status_code)
        return response.get_json()

    def create_managed_tenant(self, *, plan="business", slug="managed-client-store"):
        return self.create_tenant(plan=plan, client=self.managed_client, slug=slug)

    def configure_required_secret_references(self, tenant_id: str):
        references = {
            "telegram_bot_token": "env:CONTROLPLANE_TEST_TENANT_BOT_TOKEN",
            "telegram_webhook_secret": "env:CONTROLPLANE_TEST_TENANT_WEBHOOK_SECRET",
        }
        for kind, reference in references.items():
            response = self.client.put(
                f"/api/platform/tenants/{tenant_id}/secret-references/{kind}",
                headers=signed_headers(PLATFORM_ADMIN_ID),
                json={"reference": reference, "version": "v1"},
            )
            self.assertEqual(200, response.status_code)
            self.assertNotIn(reference, response.get_data(as_text=True))

    def record_owner_claim(self, tenant_id: str, telegram_user_id: int):
        tenant = get_tenant(self.settings.database_path, tenant_id)
        self.assertIsNotNone(tenant)
        database = connect_tenant_database(tenant.database_path)
        try:
            database.execute(
                """
                INSERT INTO tenant_owner_claims (id, telegram_user_id)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET telegram_user_id = excluded.telegram_user_id
                """,
                (telegram_user_id,),
            )
            database.commit()
        finally:
            database.close()

    def test_platform_admin_provisions_a_blank_isolated_tenant_database(self):
        tenant = self.create_tenant()
        self.configure_required_secret_references(tenant["id"])
        with patch("db.schema.DB_PATH", None):
            provisioned = self.client.post(
                f"/api/platform/tenants/{tenant['id']}/provision",
                headers=signed_headers(PLATFORM_ADMIN_ID),
            )
        self.assertEqual(200, provisioned.status_code)
        self.assertEqual("awaiting_owner_claim", provisioned.get_json()["lifecycle_state"])

        stored = get_tenant(self.settings.database_path, tenant["id"])
        self.assertTrue(stored.database_path.is_file())
        self.assertTrue(stored.media_root.is_dir())
        self.assertTrue(stored.backup_root.is_dir())
        database = connect_tenant_database(stored.database_path)
        try:
            metadata = database.execute(
                "SELECT tenant_id, canonical_host, owner_telegram_id FROM tenant_runtime_metadata"
            ).fetchone()
            settings = database.execute(
                "SELECT store_name FROM storefront_settings WHERE id = 1"
            ).fetchone()
            book_count = database.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        finally:
            database.close()
        self.assertEqual((tenant["id"], tenant["canonical_host"], 202), metadata)
        self.assertEqual(("Магазин клиента",), settings)
        self.assertEqual(0, book_count)

    def test_console_seals_secret_values_without_leaking_plaintext(self):
        tenant = self.create_managed_tenant()
        token = "234567:sealed-tenant-token"
        webhook_secret = "sealed-webhook-secret"
        response = self.managed_client.put(
            f"/api/platform/tenants/{tenant['id']}/secrets",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"secrets": {
                "telegram_bot_token": token,
                "telegram_webhook_secret": webhook_secret,
            }},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            ["telegram_bot_token", "telegram_webhook_secret"],
            response.get_json()["configured_secret_kinds"],
        )
        self.assertNotIn(token, response.get_data(as_text=True))
        self.assertNotIn(webhook_secret, response.get_data(as_text=True))
        connection = sqlite3.connect(self.settings.database_path)
        try:
            stored = connection.execute(
                """
                SELECT ciphertext, data_nonce, wrapped_key, wrap_nonce, key_version
                FROM tenant_secret_envelopes
                WHERE tenant_id = ?
                """,
                (tenant["id"],),
            ).fetchall()
            audit = connection.execute(
                """
                SELECT details_json FROM platform_audit_events
                WHERE tenant_id = ? AND action = 'tenant.secrets.enveloped'
                """,
                (tenant["id"],),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(2, len(stored))
        self.assertTrue(all(token not in row and webhook_secret not in row for row in stored))
        self.assertNotIn(token, audit)
        self.assertNotIn(webhook_secret, audit)

    def test_console_seals_valid_tenant_proxy_without_returning_it(self):
        tenant = self.create_managed_tenant()
        proxy_url = "http://proxy-user:proxy-password@203.0.113.10:3128"
        saved = self.managed_client.put(
            f"/api/platform/tenants/{tenant['id']}/secrets",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"secrets": {"bot_proxy_url": proxy_url}},
        )
        self.assertEqual(200, saved.status_code)
        self.assertIn("bot_proxy_url", saved.get_json()["configured_secret_kinds"])
        self.assertNotIn(proxy_url, saved.get_data(as_text=True))
        invalid = self.managed_client.put(
            f"/api/platform/tenants/{tenant['id']}/secrets",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"secrets": {"bot_proxy_url": "https://proxy.example.test:443"}},
        )
        self.assertEqual(400, invalid.status_code)

    def test_console_can_clear_optional_proxy_envelope(self):
        tenant = self.create_managed_tenant()
        path = f"/api/platform/tenants/{tenant['id']}/secrets"
        saved = self.managed_client.put(
            path,
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"secrets": {"bot_proxy_url": "http://user:password@203.0.113.10:3128"}},
        )
        self.assertEqual(200, saved.status_code)
        cleared = self.managed_client.put(
            path,
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"secrets": {}, "clear_bot_proxy": True},
        )
        self.assertEqual(200, cleared.status_code)
        self.assertNotIn("bot_proxy_url", cleared.get_json()["configured_secret_kinds"])

    def test_console_queues_managed_deployment_without_provisioning_in_request(self):
        tenant = self.create_managed_tenant()
        response = self.managed_client.put(
            f"/api/platform/tenants/{tenant['id']}/secrets",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"secrets": {
                "telegram_bot_token": "234567:queued-tenant-token",
                "telegram_webhook_secret": "queued-webhook-secret",
            }},
        )
        self.assertEqual(200, response.status_code)
        queued = self.managed_client.post(
            f"/api/platform/tenants/{tenant['id']}/deployments",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"operation": "provision"},
        )
        self.assertEqual(202, queued.status_code)
        payload = queued.get_json()
        self.assertEqual("pending", payload["job"]["state"])
        self.assertEqual("provision", payload["job"]["operation"])
        self.assertEqual("pending", payload["deployment"]["state"])
        self.assertFalse(get_tenant(self.settings.database_path, tenant["id"]).database_path.exists())
        self.assertNotIn("queued-tenant-token", queued.get_data(as_text=True))
        duplicate = self.managed_client.post(
            f"/api/platform/tenants/{tenant['id']}/deployments",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"operation": "provision"},
        )
        self.assertEqual(409, duplicate.status_code)
        connection = sqlite3.connect(self.settings.database_path)
        try:
            actor = connection.execute(
                """
                SELECT created_by_platform_admin_id FROM tenant_deployment_jobs
                WHERE id = ?
                """,
                (payload["job"]["id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual((PLATFORM_ADMIN_ID,), actor)

    def test_secret_reference_rotation_invalidates_tenant_runtime(self):
        tenant = self.create_tenant()
        before = get_tenant(self.settings.database_path, tenant["id"])
        response = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/secret-references/telegram_bot_token",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"reference": "env:CONTROLPLANE_TEST_TENANT_BOT_TOKEN", "version": "v2"},
        )
        self.assertEqual(200, response.status_code)
        after = get_tenant(self.settings.database_path, tenant["id"])
        self.assertEqual(before.runtime_generation + 1, after.runtime_generation)

    def test_plan_and_entitlement_overrides_are_platform_admin_only(self):
        tenant = self.create_tenant(plan="start")
        self.assertEqual(403, self.client.put(
            f"/api/platform/tenants/{tenant['id']}/plan",
            headers=signed_headers(999),
            json={"plan": "pro"},
        ).status_code)
        changed = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/plan",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"plan": "pro"},
        )
        self.assertEqual(200, changed.status_code)
        self.assertEqual("pro", changed.get_json()["plan"])
        updated = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/entitlements",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={
                "feature_overrides": {FEATURE_BROADCAST: False},
                "limit_overrides": {"books": 12_345},
            },
        )
        self.assertEqual(200, updated.status_code)
        payload = updated.get_json()
        self.assertNotIn(FEATURE_BROADCAST, payload["features"])
        self.assertEqual(12_345, payload["limits"]["books"])
        entitlements = effective_tenant_entitlements(self.settings.database_path, tenant["id"])
        self.assertNotIn(FEATURE_BROADCAST, entitlements.features)
        self.assertIn(FEATURE_ANALYTICS, entitlements.features)

    def test_platform_activation_requires_matching_owner_claim(self):
        tenant = self.create_tenant()
        self.configure_required_secret_references(tenant["id"])
        provisioned = self.client.post(
            f"/api/platform/tenants/{tenant['id']}/provision",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        )
        self.assertEqual(200, provisioned.status_code)
        def dispatch(context, _environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [context.tenant_id.encode()]
        gateway = TenantHostGateway(self.settings.database_path, dispatch)
        def gateway_response(path: str):
            response = {}
            body = b"".join(gateway(
                {"HTTP_HOST": tenant["canonical_host"], "PATH_INFO": path},
                lambda status, _headers: response.setdefault("status", status),
            ))
            return response["status"], body
        status, body = gateway_response("/setup")
        self.assertEqual("200 OK", status)
        self.assertEqual(tenant["id"].encode(), body)
        status, _ = gateway_response("/")
        self.assertEqual("404 Not Found", status)
        bypass = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/lifecycle",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"state": "active"},
        )
        self.assertEqual(400, bypass.status_code)
        indirect_bypass = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/lifecycle",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"state": "suspended"},
        )
        self.assertEqual(400, indirect_bypass.status_code)
        missing_claim = self.client.post(
            f"/api/platform/tenants/{tenant['id']}/activate",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        )
        self.assertEqual(409, missing_claim.status_code)
        self.assertIn("has not claimed", missing_claim.get_json()["error"])
        self.record_owner_claim(tenant["id"], 999)
        wrong_claim = self.client.post(
            f"/api/platform/tenants/{tenant['id']}/activate",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        )
        self.assertEqual(409, wrong_claim.status_code)
        self.assertIn("does not match", wrong_claim.get_json()["error"])
        self.record_owner_claim(tenant["id"], tenant["owner_telegram_id"])
        activated = self.client.post(
            f"/api/platform/tenants/{tenant['id']}/activate",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        )
        self.assertEqual(200, activated.status_code)
        self.assertEqual("active", activated.get_json()["lifecycle_state"])
        status, body = gateway_response("/")
        self.assertEqual("200 OK", status)
        self.assertEqual(tenant["id"].encode(), body)
        self.assertEqual(
            tenant["id"],
            tenant_context_for_host(self.settings.database_path, tenant["canonical_host"]).tenant_id,
        )

    def test_active_host_resolves_only_to_its_tenant_context(self):
        first = self.create_tenant(plan=Plan.BUSINESS)
        self.configure_required_secret_references(first["id"])
        self.client.post(
            f"/api/platform/tenants/{first['id']}/provision",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        )
        self.record_owner_claim(first["id"], first["owner_telegram_id"])
        activated = self.client.post(
            f"/api/platform/tenants/{first['id']}/activate",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        )
        self.assertEqual(200, activated.status_code)
        context = tenant_context_for_host(self.settings.database_path, first["canonical_host"])
        self.assertEqual(first["id"], context.tenant_id)
        self.assertTrue(context.database_path.is_file())
        with self.assertRaises(TenantResolutionError):
            tenant_context_for_host(self.settings.database_path, "other.shops.example.test")

    def test_custom_domain_requires_platform_verification_before_routing(self):
        tenant = self.create_tenant()
        self.configure_required_secret_references(tenant["id"])
        self.client.post(
            f"/api/platform/tenants/{tenant['id']}/provision",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        )
        self.record_owner_claim(tenant["id"], tenant["owner_telegram_id"])
        activated = self.client.post(
            f"/api/platform/tenants/{tenant['id']}/activate",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        )
        self.assertEqual(200, activated.status_code)
        requested = self.client.post(
            f"/api/platform/tenants/{tenant['id']}/domains",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"host": "books.client.example"},
        )
        self.assertEqual(201, requested.status_code)
        with self.assertRaises(TenantResolutionError):
            tenant_context_for_host(self.settings.database_path, "books.client.example")
        verified = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/domains/books.client.example/verification",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"state": "verified"},
        )
        self.assertEqual(200, verified.status_code)
        self.assertEqual(
            tenant["id"],
            tenant_context_for_host(
                self.settings.database_path, "books.client.example"
            ).tenant_id,
        )

    def test_atomic_configuration_and_secret_batch_are_safe(self):
        tenant = self.create_tenant(plan=Plan.START)
        before = self.client.get(
            f"/api/platform/tenants/{tenant['id']}",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        ).get_json()
        changed = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/configuration",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={
                "plan": "business",
                "feature_overrides": {"campaigns": True},
                "limit_overrides": {"books": 1_234},
            },
        )
        self.assertEqual(200, changed.status_code)
        configured = changed.get_json()
        self.assertEqual("business", configured["plan"])
        self.assertIn("campaigns", configured["entitlements"]["features"])
        self.assertEqual(1_234, configured["entitlements"]["limits"]["books"])
        self.assertEqual(before["runtime_generation"] + 1, configured["runtime_generation"])
        self.assertEqual(before["entitlement_version"] + 1, configured["entitlement_version"])

        rejected = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/configuration",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={
                "plan": "pro",
                "feature_overrides": {"not-a-feature": True},
                "limit_overrides": {},
            },
        )
        self.assertEqual(400, rejected.status_code)
        after_rejected = self.client.get(
            f"/api/platform/tenants/{tenant['id']}",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        ).get_json()
        self.assertEqual(configured["plan"], after_rejected["plan"])
        self.assertEqual(configured["runtime_generation"], after_rejected["runtime_generation"])

        reference_name = "env:CONTROLPLANE_TEST_TENANT_BOT_TOKEN"
        saved = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/secret-references",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"references": {
                "telegram_bot_token": {"reference": reference_name, "version": "v1"},
                "telegram_webhook_secret": {
                    "reference": "env:CONTROLPLANE_TEST_TENANT_WEBHOOK_SECRET",
                    "version": "v1",
                },
            }},
        )
        self.assertEqual(200, saved.status_code)
        saved_payload = saved.get_json()
        self.assertEqual(after_rejected["runtime_generation"] + 1, saved_payload["runtime_generation"])
        self.assertEqual(
            ["telegram_bot_token", "telegram_webhook_secret"],
            saved_payload["configured_secret_kinds"],
        )
        self.assertNotIn(reference_name, saved.get_data(as_text=True))

        invalid = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/secret-references",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"references": {
                "telegram_bot_token": {"reference": "123456:raw-token", "version": "v2"},
                "yookassa_credentials": {"reference": "vault:unsupported", "version": "v1"},
            }},
        )
        self.assertEqual(400, invalid.status_code)
        unchanged = self.client.get(
            f"/api/platform/tenants/{tenant['id']}",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        ).get_json()
        self.assertEqual(saved_payload["runtime_generation"], unchanged["runtime_generation"])
        self.assertEqual(saved_payload["configured_secret_kinds"], unchanged["configured_secret_kinds"])

    def test_provision_failure_records_only_safe_stage_and_type(self):
        tenant = self.create_tenant()
        self.configure_required_secret_references(tenant["id"])
        with patch("db.schema.initialize_database", side_effect=RuntimeError("SECRET_SENTINEL")):
            response = self.client.post(
                f"/api/platform/tenants/{tenant['id']}/provision",
                headers=signed_headers(PLATFORM_ADMIN_ID),
            )
        self.assertEqual(409, response.status_code)
        self.assertEqual("tenant provisioning failed", response.get_json()["error"])
        self.assertNotIn("SECRET_SENTINEL", response.get_data(as_text=True))
        stored = get_tenant(self.settings.database_path, tenant["id"])
        self.assertEqual("migration_failed", stored.lifecycle_state)
        connection = sqlite3.connect(self.settings.database_path)
        try:
            outcome = connection.execute(
                """
                SELECT outcome_json FROM tenant_provisioning_jobs
                WHERE tenant_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (tenant["id"],),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(
            {"stage": "tenant_schema_initialization", "error_type": "RuntimeError"},
            json.loads(outcome),
        )
        self.assertNotIn("SECRET_SENTINEL", outcome)

    def test_provision_preflight_keeps_draft_without_usable_references(self):
        tenant = self.create_tenant()
        response = self.client.post(
            f"/api/platform/tenants/{tenant['id']}/provision",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        )
        self.assertEqual(409, response.status_code)
        self.assertIn("required secret references", response.get_json()["error"])
        stored = get_tenant(self.settings.database_path, tenant["id"])
        self.assertEqual("draft", stored.lifecycle_state)
        self.assertFalse(stored.database_path.exists())
        connection = sqlite3.connect(self.settings.database_path)
        try:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM tenant_provisioning_jobs WHERE tenant_id = ?",
                (tenant["id"],),
            ).fetchone()[0])
        finally:
            connection.close()

    def test_provision_preflight_keeps_draft_when_reference_value_is_unavailable(self):
        tenant = self.create_tenant()
        saved = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/secret-references",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"references": {
                "telegram_bot_token": {"reference": "env:CONTROLPLANE_TEST_MISSING_BOT", "version": "v1"},
                "telegram_webhook_secret": {"reference": "env:CONTROLPLANE_TEST_TENANT_WEBHOOK_SECRET", "version": "v1"},
            }},
        )
        self.assertEqual(200, saved.status_code)
        response = self.client.post(
            f"/api/platform/tenants/{tenant['id']}/provision",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        )
        self.assertEqual(409, response.status_code)
        self.assertIn("telegram_bot_token", response.get_json()["error"])
        self.assertNotIn("CONTROLPLANE_TEST_MISSING_BOT", response.get_data(as_text=True))
        stored = get_tenant(self.settings.database_path, tenant["id"])
        self.assertEqual("draft", stored.lifecycle_state)
        self.assertFalse(stored.database_path.exists())

    def test_console_payload_exposes_plan_defaults_domains_and_safe_secret_statuses(self):
        tenant = self.create_tenant(plan=Plan.START)
        listed = self.client.get(
            "/api/platform/tenants", headers=signed_headers(PLATFORM_ADMIN_ID)
        )
        self.assertEqual(200, listed.status_code)
        payload = listed.get_json()
        self.assertEqual(plan_defaults(), payload["plan_defaults"])
        item = payload["tenants"][0]
        self.assertEqual({}, item["entitlement_overrides"]["feature_overrides"])
        self.assertEqual({}, item["entitlement_overrides"]["limit_overrides"])
        self.assertEqual([], item["configured_secret_kinds"])
        self.assertEqual(
            [{
                "host": tenant["canonical_host"],
                "verification_state": "verified",
                "is_canonical": True,
                "created_at": item["domains"][0]["created_at"],
                "verified_at": item["domains"][0]["verified_at"],
            }],
            item["domains"],
        )

        self.configure_required_secret_references(tenant["id"])
        provisioned = self.client.post(
            f"/api/platform/tenants/{tenant['id']}/provision",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        )
        self.assertEqual(200, provisioned.status_code)
        self.assertNotIn("yookassa_credentials", provisioned.get_json()["configured_secret_kinds"])
        before = provisioned.get_json()["runtime_generation"]
        yookassa = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/secret-references/yookassa_credentials",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"reference": "env:TEST_YOOKASSA_JSON", "version": "v1"},
        )
        self.assertEqual(200, yookassa.status_code)
        details = self.client.get(
            f"/api/platform/tenants/{tenant['id']}",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        ).get_json()
        self.assertIn("yookassa_credentials", details["configured_secret_kinds"])
        self.assertEqual(before + 1, details["runtime_generation"])
        self.assertNotIn("TEST_YOOKASSA_JSON", json.dumps(details))

    def test_managed_runtime_mutations_coalesce_and_domain_disable_queues_withdrawal(self):
        tenant = self.create_managed_tenant(slug="managed-domain-store")
        connection = sqlite3.connect(self.settings.database_path)
        try:
            connection.execute(
                "UPDATE platform_tenants SET lifecycle_state = 'active' WHERE id = ?",
                (tenant["id"],),
            )
            connection.commit()
        finally:
            connection.close()
        headers = signed_headers(PLATFORM_ADMIN_ID)
        requested = self.managed_client.post(
            f"/api/platform/tenants/{tenant['id']}/domains",
            headers=headers,
            json={"host": "books.managed.example"},
        )
        self.assertEqual(201, requested.status_code)
        before = get_tenant(self.settings.database_path, tenant["id"])
        verified = self.managed_client.put(
            f"/api/platform/tenants/{tenant['id']}/domains/books.managed.example/verification",
            headers=headers,
            json={"state": "verified"},
        )
        self.assertEqual(202, verified.status_code)
        self.assertTrue(verified.get_json()["reconciliation_queued"])
        after_verified = get_tenant(self.settings.database_path, tenant["id"])
        self.assertEqual(before.runtime_generation + 1, after_verified.runtime_generation)
        disabled = self.managed_client.put(
            f"/api/platform/tenants/{tenant['id']}/domains/books.managed.example/verification",
            headers=headers,
            json={"state": "disabled"},
        )
        self.assertEqual(202, disabled.status_code)
        after_disabled = get_tenant(self.settings.database_path, tenant["id"])
        self.assertEqual(after_verified.runtime_generation + 1, after_disabled.runtime_generation)
        repeated = self.managed_client.put(
            f"/api/platform/tenants/{tenant['id']}/domains/books.managed.example/verification",
            headers=headers,
            json={"state": "disabled"},
        )
        self.assertEqual(200, repeated.status_code)
        self.assertFalse(repeated.get_json()["changed"])
        self.assertEqual(after_disabled.runtime_generation, get_tenant(self.settings.database_path, tenant["id"]).runtime_generation)
        connection = sqlite3.connect(self.settings.database_path)
        try:
            jobs = connection.execute(
                "SELECT operation, desired_generation FROM tenant_deployment_jobs WHERE tenant_id = ? AND state = 'pending'",
                (tenant["id"],),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([("redeploy", after_disabled.runtime_generation)], jobs)

    def test_managed_entitlements_advance_runtime_generation_and_queue_redeploy(self):
        tenant = self.create_managed_tenant(slug="managed-entitlements-store")
        connection = sqlite3.connect(self.settings.database_path)
        try:
            connection.execute(
                "UPDATE platform_tenants SET lifecycle_state = 'active' WHERE id = ?",
                (tenant["id"],),
            )
            connection.commit()
        finally:
            connection.close()
        before = get_tenant(self.settings.database_path, tenant["id"])
        response = self.managed_client.put(
            f"/api/platform/tenants/{tenant['id']}/entitlements",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"feature_overrides": {FEATURE_BROADCAST: False}, "limit_overrides": {}},
        )
        self.assertEqual(200, response.status_code)
        after = get_tenant(self.settings.database_path, tenant["id"])
        self.assertEqual(before.runtime_generation + 1, after.runtime_generation)
        self.assertEqual(before.entitlement_version + 1, after.entitlement_version)
        details = self.managed_client.get(
            f"/api/platform/tenants/{tenant['id']}", headers=signed_headers(PLATFORM_ADMIN_ID)
        ).get_json()
        self.assertEqual(after.runtime_generation, details["deployment"]["desired_generation"])


        tenant = self.create_tenant()
        requested = self.client.post(
            f"/api/platform/tenants/{tenant['id']}/domains",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"host": "books.client.example"},
        )
        self.assertEqual(201, requested.status_code)
        details = self.client.get(
            f"/api/platform/tenants/{tenant['id']}",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        ).get_json()
        pending = next(domain for domain in details["domains"] if not domain["is_canonical"])
        self.assertEqual("pending", pending["verification_state"])
        verified = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/domains/books.client.example/verification",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"state": "verified"},
        )
        self.assertEqual(200, verified.status_code)
        disabled = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/domains/books.client.example/verification",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"state": "disabled"},
        )
        self.assertEqual(200, disabled.status_code)
        details = self.client.get(
            f"/api/platform/tenants/{tenant['id']}",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        ).get_json()
        self.assertEqual(
            "disabled",
            next(domain for domain in details["domains"] if not domain["is_canonical"])["verification_state"],
        )

    def test_soft_delete_requires_typed_slug_and_preserves_tenant_data(self):
        tenant = self.create_tenant()
        self.assertEqual(403, self.client.delete(
            f"/api/platform/tenants/{tenant['id']}",
            headers=signed_headers(999),
            json={"confirm_slug": tenant["slug"]},
        ).status_code)
        self.assertEqual(400, self.client.delete(
            f"/api/platform/tenants/{tenant['id']}",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"confirm_slug": "wrong-slug"},
        ).status_code)
        self.configure_required_secret_references(tenant["id"])
        self.assertEqual(200, self.client.post(
            f"/api/platform/tenants/{tenant['id']}/provision",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        ).status_code)
        self.record_owner_claim(tenant["id"], tenant["owner_telegram_id"])
        self.assertEqual(200, self.client.post(
            f"/api/platform/tenants/{tenant['id']}/activate",
            headers=signed_headers(PLATFORM_ADMIN_ID),
        ).status_code)
        stored = get_tenant(self.settings.database_path, tenant["id"])
        self.assertTrue(stored.database_path.is_file())
        self.assertTrue(stored.media_root.is_dir())
        self.assertTrue(stored.backup_root.is_dir())

        deleted = self.client.delete(
            f"/api/platform/tenants/{tenant['id']}",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"confirm_slug": tenant["slug"]},
        )
        self.assertEqual(200, deleted.status_code)
        self.assertTrue(deleted.get_json()["deleted"])
        self.assertEqual("deleted", get_tenant(self.settings.database_path, tenant["id"]).lifecycle_state)
        self.assertTrue(stored.database_path.is_file())
        self.assertTrue(stored.media_root.is_dir())
        self.assertTrue(stored.backup_root.is_dir())
        listed = self.client.get(
            "/api/platform/tenants", headers=signed_headers(PLATFORM_ADMIN_ID)
        ).get_json()
        self.assertEqual([], listed["tenants"])
        with self.assertRaises(TenantResolutionError):
            tenant_context_for_host(self.settings.database_path, tenant["canonical_host"])
        self.assertEqual(400, self.client.put(
            f"/api/platform/tenants/{tenant['id']}/plan",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"plan": "pro"},
        ).status_code)
        repeated = self.client.delete(
            f"/api/platform/tenants/{tenant['id']}",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"confirm_slug": tenant["slug"]},
        )
        self.assertEqual(200, repeated.status_code)
        self.assertFalse(repeated.get_json()["deleted"])
        connection = sqlite3.connect(self.settings.database_path)
        try:
            events = connection.execute(
                "SELECT action FROM platform_audit_events WHERE tenant_id = ?",
                (tenant["id"],),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(1, sum(action == "tenant.deleted" for action, in events))

    def test_soft_delete_rejects_provisioning_tenant(self):
        tenant = self.create_tenant()
        connection = sqlite3.connect(self.settings.database_path)
        try:
            connection.execute(
                "UPDATE platform_tenants SET lifecycle_state = 'provisioning' WHERE id = ?",
                (tenant["id"],),
            )
            connection.commit()
        finally:
            connection.close()
        response = self.client.delete(
            f"/api/platform/tenants/{tenant['id']}",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"confirm_slug": tenant["slug"]},
        )
        self.assertEqual(409, response.status_code)

    def test_platform_audit_log_is_immutable(self):
        self.create_tenant()
        connection = sqlite3.connect(self.settings.database_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM platform_audit_events")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
