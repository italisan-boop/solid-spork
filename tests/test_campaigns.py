import hashlib
import hmac
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import db.connection as db_connection
import db.schema as schema
import server
from config import settings
from controlplane.plan_policy import Plan, effective_entitlements
from runtime.context import TenantContext


TEST_TOKEN = "123456:campaign-test-token"
OWNER_ID = 101


def signed_headers(user_id: int = OWNER_ID) -> dict[str, str]:
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":"))),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


class CampaignApiTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "campaigns.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(server, "BOT_TOKEN", TEST_TOKEN))
        self._patches.enter_context(patch.object(server, "ADMIN_IDS", []))
        self._patches.enter_context(patch.object(settings, "OWNER_TELEGRAM_ID", OWNER_ID))
        schema.initialize_database(self.database_path)
        self.client = server.app.test_client()
        self.payload = {
            "code": "vk_september",
            "channel": "vk",
            "source": "community",
            "campaign": "september_2026",
        }

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def test_owner_can_disable_reenable_and_delete_campaign(self):
        self.assertEqual(200, self.client.put(
            "/api/admin/campaigns", headers=signed_headers(), json=self.payload
        ).status_code)
        disabled = self.client.patch(
            "/api/admin/campaigns/vk_september",
            headers=signed_headers(),
            json={"is_active": False},
        )
        self.assertEqual(200, disabled.status_code)
        self.assertFalse(disabled.get_json()["is_active"])

        listed = self.client.get("/api/admin/campaigns", headers=signed_headers())
        self.assertEqual(200, listed.status_code)
        self.assertFalse(listed.get_json()["campaigns"][0]["is_active"])

        self.assertEqual(200, self.client.patch(
            "/api/admin/campaigns/vk_september",
            headers=signed_headers(),
            json={"is_active": True},
        ).status_code)
        deleted = self.client.delete(
            "/api/admin/campaigns/vk_september", headers=signed_headers()
        )
        self.assertEqual(200, deleted.status_code)
        self.assertTrue(deleted.get_json()["deleted"])
        self.assertEqual([], self.client.get(
            "/api/admin/campaigns", headers=signed_headers()
        ).get_json()["campaigns"])

        connection = schema.connect(self.database_path)
        try:
            actions = connection.execute("SELECT action FROM audit_events ORDER BY id").fetchall()
        finally:
            connection.close()
        self.assertIn(("campaign.status.updated",), actions)
        self.assertIn(("campaign.deleted",), actions)

    def test_campaign_api_returns_quota_conflict_for_tenant_runtime(self):
        context = TenantContext(
            tenant_id="campaign-tenant",
            canonical_host="campaigns.example.test",
            database_path=self.database_path,
            media_root=self.database_path.parent / "media",
            backup_root=self.database_path.parent / "backups",
            owner_telegram_id=OWNER_ID,
            entitlements=effective_entitlements(
                Plan.PRO, limit_overrides={"campaigns": 1}
            ),
            runtime_generation=1,
        )
        second = {**self.payload, "code": "tg_october"}
        with context.scope():
            self.assertEqual(200, self.client.put(
                "/api/admin/campaigns", headers=signed_headers(), json=self.payload
            ).status_code)
            limited = self.client.put(
                "/api/admin/campaigns", headers=signed_headers(), json=second
            )
        self.assertEqual(409, limited.status_code)
        self.assertEqual(1, limited.get_json()["limit"])


if __name__ == "__main__":
    unittest.main()
