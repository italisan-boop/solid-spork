import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from controlplane.gateway import TenantHostGateway
from controlplane.plan_policy import FEATURE_ANALYTICS, FEATURE_BROADCAST, Plan
from controlplane.server import create_controlplane_app
from controlplane.settings import ControlPlaneSettings
from controlplane.tenants import effective_tenant_entitlements, get_tenant
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
        self.client = create_controlplane_app(self.settings).test_client()

    def test_platform_console_requires_signed_platform_session(self):
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

    def tearDown(self):
        self._temporary_directory.cleanup()

    def create_tenant(self, *, plan="business"):
        response = self.client.post(
            "/api/platform/tenants",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={
                "display_name": "Магазин клиента",
                "slug": "client-store",
                "owner_telegram_id": 202,
                "plan": plan,
            },
        )
        self.assertEqual(201, response.status_code)
        return response.get_json()

    def configure_required_secret_references(self, tenant_id: str):
        for kind in ("telegram_bot_token", "telegram_webhook_secret"):
            response = self.client.put(
                f"/api/platform/tenants/{tenant_id}/secret-references/{kind}",
                headers=signed_headers(PLATFORM_ADMIN_ID),
                json={"reference": f"vault:tenants/{tenant_id}/{kind}", "version": "v1"},
            )
            self.assertEqual(200, response.status_code)
            self.assertNotIn("vault:", response.get_data(as_text=True))

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

    def test_secret_reference_rotation_invalidates_tenant_runtime(self):
        tenant = self.create_tenant()
        before = get_tenant(self.settings.database_path, tenant["id"])
        response = self.client.put(
            f"/api/platform/tenants/{tenant['id']}/secret-references/telegram_bot_token",
            headers=signed_headers(PLATFORM_ADMIN_ID),
            json={"reference": "vault:tenants/rotated/token", "version": "v2"},
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
