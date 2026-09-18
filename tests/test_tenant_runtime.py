import hashlib
import hmac
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

from controlplane.plan_policy import Plan, effective_entitlements
from db.schema import connect as connect_tenant_database
from db.schema import initialize_database
from runtime.context import TenantContext
from runtime.server import create_tenant_setup_app


TOKEN = "123456:tenant-runtime-test-token"
OWNER_ID = 101


def signed_headers(user_id: int) -> dict[str, str]:
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":"))),
    ]
    check = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


class TenantSetupApiTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        database_path = root / "tenant.sqlite"
        initialize_database(database_path, seed_catalog=False)
        self.context = TenantContext(
            tenant_id="tenant-a",
            canonical_host="tenant-a.example.test",
            database_path=database_path,
            media_root=root / "media",
            backup_root=root / "backups",
            owner_telegram_id=OWNER_ID,
            entitlements=effective_entitlements(Plan.BUSINESS),
            runtime_generation=1,
        )
        self.client = create_tenant_setup_app(self.context, TOKEN).test_client()

    def tearDown(self):
        self._temporary_directory.cleanup()

    def test_tenant_owner_setup_page_is_served(self):
        response = self.client.get("/setup")
        try:
            self.assertEqual(200, response.status_code)
            page = response.get_data(as_text=True)
            self.assertIn("Первоначальная настройка", page)
            self.assertIn("telegram-web-app.js", page)
        finally:
            response.close()

    def test_owner_can_configure_branding_but_outsider_cannot(self):
        initial = self.client.get("/api/storefront/config")
        self.assertEqual(200, initial.status_code)
        self.assertEqual("", initial.get_json()["store_name"])
        self.assertIn("inventory", initial.get_json()["features"])

        payload = {
            "store_name": "Книжный сад",
            "primary_color": "#123456",
            "accent_color": "#abcdef",
            "support_contact": "@support",
        }
        denied = self.client.put(
            "/api/tenant/onboarding/storefront", headers=signed_headers(202), json=payload
        )
        self.assertEqual(403, denied.status_code)
        saved = self.client.put(
            "/api/tenant/onboarding/storefront", headers=signed_headers(OWNER_ID), json=payload
        )
        self.assertEqual(200, saved.status_code)
        for key, value in payload.items():
            self.assertEqual(value, saved.get_json()[key])
        current = self.client.get("/api/storefront/config").get_json()
        for key, value in payload.items():
            self.assertEqual(value, current[key])

    def test_owner_claim_is_idempotent_and_requires_the_configured_owner(self):
        denied = self.client.post(
            "/api/tenant/onboarding/owner-claim", headers=signed_headers(202)
        )
        self.assertEqual(403, denied.status_code)
        invalid_body = self.client.post(
            "/api/tenant/onboarding/owner-claim",
            headers=signed_headers(OWNER_ID),
            json={"unexpected": True},
        )
        self.assertEqual(400, invalid_body.status_code)
        claimed = self.client.post(
            "/api/tenant/onboarding/owner-claim", headers=signed_headers(OWNER_ID)
        )
        self.assertEqual(200, claimed.status_code)
        self.assertEqual({"claimed": True}, claimed.get_json())
        repeated = self.client.post(
            "/api/tenant/onboarding/owner-claim", headers=signed_headers(OWNER_ID)
        )
        self.assertEqual(200, repeated.status_code)
        database = connect_tenant_database(self.context.database_path)
        try:
            claim = database.execute(
                "SELECT telegram_user_id FROM tenant_owner_claims WHERE id = 1"
            ).fetchone()
        finally:
            database.close()
        self.assertEqual((OWNER_ID,), claim)

    def test_owner_claim_remains_available_when_storefront_features_are_disabled(self):
        restricted_context = replace(
            self.context,
            entitlements=effective_entitlements(
                Plan.START,
                feature_overrides={"branding": False, "store_settings": False},
            ),
        )
        client = create_tenant_setup_app(restricted_context, TOKEN).test_client()
        claimed = client.post(
            "/api/tenant/onboarding/owner-claim", headers=signed_headers(OWNER_ID)
        )
        self.assertEqual(200, claimed.status_code)
        storefront = client.get(
            "/api/tenant/onboarding/storefront", headers=signed_headers(OWNER_ID)
        )
        self.assertEqual(403, storefront.status_code)

    def test_session_never_grants_plan_control_to_tenant_owner(self):
        session = self.client.get("/api/tenant/session", headers=signed_headers(OWNER_ID))
        self.assertEqual(200, session.status_code)
        payload = session.get_json()
        self.assertTrue(payload["is_owner"])
        self.assertNotIn("plan", payload)
        self.assertNotIn("secret", json.dumps(payload).lower())


if __name__ == "__main__":
    unittest.main()
