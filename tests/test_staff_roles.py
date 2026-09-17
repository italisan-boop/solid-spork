import hashlib
import hmac
import json
import sqlite3
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


TEST_TOKEN = "123456:staff-role-test-token"
OWNER_ID = 101
MANAGER_ID = 202
WAREHOUSE_ID = 303


def signed_headers(user_id: int) -> dict[str, str]:
    pairs = [
        ("auth_date", str(int(time.time()))),
        ("user", json.dumps({"id": user_id, "first_name": "Staff"}, separators=(",", ":"))),
    ]
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode([*pairs, ("hash", signature)])}


class StaffRoleApiTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "staff.sqlite"
        self._patches = ExitStack()
        self._patches.enter_context(patch.object(schema, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(db_connection, "DB_PATH", self.database_path))
        self._patches.enter_context(patch.object(server, "BOT_TOKEN", TEST_TOKEN))
        self._patches.enter_context(patch.object(server, "ADMIN_IDS", []))
        self._patches.enter_context(patch.object(settings, "OWNER_TELEGRAM_ID", OWNER_ID))
        schema.initialize_database(self.database_path)
        self.client = server.app.test_client()

    def tearDown(self):
        self._patches.close()
        self._temporary_directory.cleanup()

    def request(self, method: str, path: str, user_id: int, json_body=None):
        return self.client.open(
            path,
            method=method,
            headers=signed_headers(user_id),
            json=json_body,
        )

    def test_owner_assigns_roles_and_permissions_are_server_enforced(self):
        owner_session = self.request("GET", "/api/admin/session", OWNER_ID)
        self.assertEqual(200, owner_session.status_code)
        self.assertEqual("owner", owner_session.get_json()["role"])

        manager = self.request(
            "PUT",
            "/api/admin/staff",
            OWNER_ID,
            {"telegram_user_id": MANAGER_ID, "role": "manager", "is_active": True},
        )
        warehouse = self.request(
            "PUT",
            "/api/admin/staff",
            OWNER_ID,
            {"telegram_user_id": WAREHOUSE_ID, "role": "warehouse", "is_active": True},
        )
        self.assertEqual(200, manager.status_code)
        self.assertEqual(200, warehouse.status_code)

        self.assertEqual("manager", self.request("GET", "/api/admin/session", MANAGER_ID).get_json()["role"])
        self.assertEqual("warehouse", self.request("GET", "/api/admin/session", WAREHOUSE_ID).get_json()["role"])
        self.assertEqual(403, self.request("GET", "/api/admin/dashboard", MANAGER_ID).status_code)
        self.assertEqual(403, self.request("GET", "/api/admin/inventory/1", MANAGER_ID).status_code)
        self.assertEqual(200, self.request("GET", "/api/admin/inventory/1", WAREHOUSE_ID).status_code)
        self.assertEqual(403, self.request("GET", "/api/admin/audit", MANAGER_ID).status_code)
        self.assertEqual(403, self.request("PUT", "/api/admin/staff", MANAGER_ID, {
            "telegram_user_id": 404,
            "role": "warehouse",
            "is_active": True,
        }).status_code)

    def test_staff_deactivation_takes_effect_and_audit_is_immutable(self):
        self.request(
            "PUT",
            "/api/admin/staff",
            OWNER_ID,
            {"telegram_user_id": WAREHOUSE_ID, "role": "warehouse", "is_active": True},
        )
        self.assertEqual(200, self.request("GET", "/api/admin/inventory/1", WAREHOUSE_ID).status_code)
        self.request(
            "PUT",
            "/api/admin/staff",
            OWNER_ID,
            {"telegram_user_id": WAREHOUSE_ID, "role": "warehouse", "is_active": False},
        )
        self.assertEqual(403, self.request("GET", "/api/admin/session", WAREHOUSE_ID).status_code)

        audit = self.request("GET", "/api/admin/audit", OWNER_ID)
        self.assertEqual(200, audit.status_code)
        self.assertGreaterEqual(len(audit.get_json()["events"]), 2)
        connection = schema.connect(self.database_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM audit_events")
        finally:
            connection.close()
    def test_inactive_staff_record_can_be_deleted_but_active_record_cannot(self):
        self.assertEqual(200, self.request("PUT", "/api/admin/staff", OWNER_ID, {
            "telegram_user_id": WAREHOUSE_ID,
            "role": "warehouse",
            "is_active": True,
        }).status_code)
        self.assertEqual(409, self.request("DELETE", "/api/admin/staff", OWNER_ID, {
            "telegram_user_id": WAREHOUSE_ID,
        }).status_code)
        self.assertEqual(200, self.request("PUT", "/api/admin/staff", OWNER_ID, {
            "telegram_user_id": WAREHOUSE_ID,
            "role": "manager",
            "is_active": False,
        }).status_code)
        deleted = self.request("DELETE", "/api/admin/staff", OWNER_ID, {
            "telegram_user_id": WAREHOUSE_ID,
        })
        self.assertEqual(200, deleted.status_code)
        self.assertTrue(deleted.get_json()["deleted"])
        self.assertEqual([], self.request("GET", "/api/admin/staff", OWNER_ID).get_json()["staff"])

        connection = schema.connect(self.database_path)
        try:
            actions = connection.execute("SELECT action FROM audit_events ORDER BY id").fetchall()
        finally:
            connection.close()
        self.assertIn(("staff.member.deleted",), actions)


if __name__ == "__main__":
    unittest.main()
