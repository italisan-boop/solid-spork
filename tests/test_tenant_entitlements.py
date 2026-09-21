import asyncio
import tempfile
import unittest
from pathlib import Path

from authz import capabilities_for_role, has_permission_sync
from controlplane.plan_policy import Plan, effective_entitlements
from db.acquisition import create_campaign
from db.books import add_book
from db.book_imports import commit_book_import_sync, preview_book_import_sync
from db.schema import initialize_database
from db.staff import set_staff_member_sync
from runtime.context import TenantContext
from runtime.features import QuotaExceededError
from runtime.quota import reserve_daily_quota_sync


class TenantEntitlementAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self._temporary_directory.name)
        self.database_path = root / "tenant.sqlite"
        initialize_database(self.database_path, seed_catalog=False)
        self.root = root

    def tearDown(self):
        self._temporary_directory.cleanup()

    def context(self, plan: Plan) -> TenantContext:
        return TenantContext(
            tenant_id="tenant",
            canonical_host="tenant.example.test",
            database_path=self.database_path,
            media_root=self.root / "media",
            backup_root=self.root / "backups",
            owner_telegram_id=101,
            entitlements=effective_entitlements(plan),
            runtime_generation=1,
        )

    def test_start_owner_is_denied_business_and_pro_modules(self):
        with self.context(Plan.START).scope():
            self.assertTrue(has_permission_sync(101, "catalog.manage"))
            self.assertTrue(has_permission_sync(101, "branding.manage"))
            self.assertFalse(has_permission_sync(101, "inventory.read"))
            self.assertFalse(has_permission_sync(101, "book.import"))
            self.assertFalse(has_permission_sync(101, "reports.view"))
            self.assertFalse(has_permission_sync(101, "campaign.manage"))
            self.assertFalse(has_permission_sync(101, "broadcast.send"))
            capabilities = capabilities_for_role("owner")
        self.assertIn("branding.manage", capabilities)
        self.assertNotIn("inventory.read", capabilities)
        self.assertNotIn("campaign.manage", capabilities)

    def test_branding_override_denies_owner_edit_permission(self):
        context = TenantContext(
            tenant_id="tenant",
            canonical_host="tenant.example.test",
            database_path=self.database_path,
            media_root=self.root / "media",
            backup_root=self.root / "backups",
            owner_telegram_id=101,
            entitlements=effective_entitlements(
                Plan.START, feature_overrides={"branding": False}
            ),
            runtime_generation=1,
        )
        with context.scope():
            self.assertFalse(has_permission_sync(101, "branding.manage"))
            self.assertNotIn("branding.manage", capabilities_for_role("owner"))

    def test_business_and_pro_expose_only_their_modules(self):
        with self.context(Plan.BUSINESS).scope():
            self.assertTrue(has_permission_sync(101, "inventory.adjust"))
            self.assertTrue(has_permission_sync(101, "book.import"))
            self.assertTrue(has_permission_sync(101, "reports.view"))
            self.assertFalse(has_permission_sync(101, "campaign.manage"))
            self.assertFalse(has_permission_sync(101, "broadcast.send"))
        with self.context(Plan.PRO).scope():
            self.assertTrue(has_permission_sync(101, "campaign.manage"))
            self.assertTrue(has_permission_sync(101, "broadcast.send"))
    def test_book_limit_is_enforced_at_the_catalog_write_boundary(self):
        context = TenantContext(
            tenant_id="tenant",
            canonical_host="tenant.example.test",
            database_path=self.database_path,
            media_root=self.root / "media",
            backup_root=self.root / "backups",
            owner_telegram_id=101,
            entitlements=effective_entitlements(Plan.START, limit_overrides={"books": 1}),
            runtime_generation=1,
        )
        with context.scope():
            asyncio.run(add_book("Первая", 100, 0))
            with self.assertRaises(QuotaExceededError):
                asyncio.run(add_book("Вторая", 100, 0))

    def test_staff_limit_is_enforced_at_the_repository_write_boundary(self):
        context = TenantContext(
            tenant_id="tenant",
            canonical_host="tenant.example.test",
            database_path=self.database_path,
            media_root=self.root / "media",
            backup_root=self.root / "backups",
            owner_telegram_id=101,
            entitlements=effective_entitlements(
                Plan.BUSINESS, limit_overrides={"staff_members": 1}
            ),
            runtime_generation=1,
        )
        with context.scope():
            set_staff_member_sync(201, "manager", active=True, actor_user_id=101)
            with self.assertRaises(QuotaExceededError):
                set_staff_member_sync(202, "warehouse", active=True, actor_user_id=101)

    def test_import_limit_and_catalog_limit_are_enforced_transactionally(self):
        context = TenantContext(
            tenant_id="tenant",
            canonical_host="tenant.example.test",
            database_path=self.database_path,
            media_root=self.root / "media",
            backup_root=self.root / "backups",
            owner_telegram_id=101,
            entitlements=effective_entitlements(
                Plan.BUSINESS,
                limit_overrides={"books": 1, "imports_per_day": 1},
            ),
            runtime_generation=1,
        )
        header = "book_id,title,author,description,price_rub,category,stock_mode,stock_quantity\n"
        with context.scope():
            first = preview_book_import_sync(
                "books.csv", (header + ",Первая,,,100,,unlimited,\n").encode(), 101
            )
            committed = commit_book_import_sync(first["batch_id"], 101)
            self.assertEqual(1, committed["created"])
            second = preview_book_import_sync(
                "books.csv", (header + ",Вторая,,,100,,unlimited,\n").encode(), 101
            )
            with self.assertRaises(QuotaExceededError):
                commit_book_import_sync(second["batch_id"], 101)

    def test_campaign_limit_is_enforced_at_the_repository_write_boundary(self):
        context = TenantContext(
            tenant_id="tenant",
            canonical_host="tenant.example.test",
            database_path=self.database_path,
            media_root=self.root / "media",
            backup_root=self.root / "backups",
            owner_telegram_id=101,
            entitlements=effective_entitlements(
                Plan.PRO, limit_overrides={"campaigns": 1}
            ),
            runtime_generation=1,
        )
        with context.scope():
            asyncio.run(create_campaign("first", "vk", "community", "September"))
            with self.assertRaises(QuotaExceededError):
                asyncio.run(create_campaign("second", "vk", "community", "October"))
    def test_daily_recipient_reservation_is_atomic(self):
        context = TenantContext(
            tenant_id="tenant",
            canonical_host="tenant.example.test",
            database_path=self.database_path,
            media_root=self.root / "media",
            backup_root=self.root / "backups",
            owner_telegram_id=101,
            entitlements=effective_entitlements(
                Plan.PRO, limit_overrides={"broadcast_recipients_per_day": 2}
            ),
            runtime_generation=1,
        )
        with context.scope():
            reserve_daily_quota_sync("broadcast_recipients_per_day", 2)
            with self.assertRaises(QuotaExceededError):
                reserve_daily_quota_sync("broadcast_recipients_per_day", 1)


if __name__ == "__main__":
    unittest.main()
