import tempfile
import unittest
from pathlib import Path

from controlplane.plan_policy import Plan, effective_entitlements
from db.connection import connection
from db.schema import connect, initialize_database
from runtime.context import TenantContext, current_tenant_context


class TenantDatabaseContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        self.first_path = root / "first.sqlite"
        self.second_path = root / "second.sqlite"
        initialize_database(self.first_path, seed_catalog=False)
        initialize_database(self.second_path, seed_catalog=False)
        entitlements = effective_entitlements(Plan.BUSINESS)
        self.first = TenantContext(
            tenant_id="first",
            canonical_host="first.example.test",
            database_path=self.first_path,
            media_root=root / "first-media",
            backup_root=root / "first-backups",
            owner_telegram_id=101,
            entitlements=entitlements,
            runtime_generation=1,
        )
        self.second = TenantContext(
            tenant_id="second",
            canonical_host="second.example.test",
            database_path=self.second_path,
            media_root=root / "second-media",
            backup_root=root / "second-backups",
            owner_telegram_id=202,
            entitlements=entitlements,
            runtime_generation=1,
        )

    def tearDown(self):
        self._temporary_directory.cleanup()

    async def test_sync_and_async_connections_follow_the_active_tenant_context(self):
        with self.first.scope():
            self.assertEqual("first", current_tenant_context().tenant_id)
            database = connect()
            try:
                database.execute("INSERT INTO categories (name) VALUES ('First')")
                database.commit()
            finally:
                database.close()
            async with connection() as database:
                await database.execute("INSERT INTO categories (name) VALUES ('First async')")
                await database.commit()

        with self.second.scope():
            database = connect()
            try:
                database.execute("INSERT INTO categories (name) VALUES ('Second')")
                database.commit()
                categories = database.execute("SELECT name FROM categories ORDER BY id").fetchall()
            finally:
                database.close()
        self.assertEqual([("Second",)], categories)

        database = connect(self.first_path)
        try:
            categories = database.execute("SELECT name FROM categories ORDER BY id").fetchall()
        finally:
            database.close()
        self.assertEqual([("First",), ("First async",)], categories)


if __name__ == "__main__":
    unittest.main()
