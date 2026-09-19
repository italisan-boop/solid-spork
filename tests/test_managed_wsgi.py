import json
import tempfile
import unittest
import uuid
from pathlib import Path

from controlplane.plan_policy import Plan, effective_entitlements
from db.schema import initialize_database
from runtime.context import TenantContext
from runtime.managed_wsgi import create_managed_tenant_wsgi_app
from runtime.manifest import ManagedRuntimeManifest


class ManagedTenantWsgiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        database_path = root / "app.sqlite"
        initialize_database(database_path, seed_catalog=False)
        entitlements = effective_entitlements(Plan.BUSINESS)
        self.context = TenantContext(
            tenant_id=str(uuid.uuid4()),
            canonical_host="store.example.test",
            database_path=database_path,
            media_root=root / "media",
            backup_root=root / "backups",
            owner_telegram_id=101,
            entitlements=entitlements,
            runtime_generation=1,
        )
        self.manifest = ManagedRuntimeManifest(
            tenant_id=self.context.tenant_id,
            canonical_host=self.context.canonical_host,
            allowed_hosts=frozenset({self.context.canonical_host}),
            database_path=database_path,
            media_root=root / "media",
            backup_root=root / "backups",
            socket_path=root / "runtime" / "tenant.sock",
            owner_telegram_id=101,
            lifecycle_state="awaiting_owner_claim",
            runtime_generation=1,
            entitlements=entitlements,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _call(self, app, host: str, path: str, method: str = "GET"):
        response = {}
        result = app(
            {
                "HTTP_HOST": host,
                "PATH_INFO": path,
                "REQUEST_METHOD": method,
                "SERVER_NAME": host,
                "SERVER_PORT": "443",
                "wsgi.url_scheme": "https",
            },
            lambda status, _headers: response.setdefault("status", status),
        )
        try:
            body = b"".join(result)
        finally:
            close = getattr(result, "close", None)
            if close is not None:
                close()
        return response["status"], body

    def test_onboarding_is_bound_to_manifest_host(self):
        app = create_managed_tenant_wsgi_app(
            self.manifest,
            context=self.context,
            storefront_app=lambda _environ, _start: [b"storefront"],
            bot_token="123456:test-token",
        )
        status, body = self._call(app, "other.example.test", "/setup")
        self.assertEqual("404 Not Found", status)
        self.assertIn(b"tenant host is unavailable", body)
        status, body = self._call(app, "store.example.test", "/setup")
        self.assertEqual("200 OK", status)
        self.assertIn(b"telegram-web-app.js", body)
        status, _ = self._call(app, "store.example.test", "/")
        self.assertEqual("404 Not Found", status)

    def test_local_socket_health_is_available_without_public_preclaim_access(self):
        app = create_managed_tenant_wsgi_app(
            self.manifest,
            context=self.context,
            storefront_app=lambda _environ, _start: [b"storefront"],
            bot_token="123456:test-token",
        )
        status, body = self._call(app, "localhost", "/health")
        self.assertEqual("200 OK", status)
        self.assertEqual(
            {"tenant_id": self.context.tenant_id, "generation": 1}, json.loads(body)
        )
        status, _ = self._call(app, "store.example.test", "/health")
        self.assertEqual("404 Not Found", status)
        status, _ = self._call(app, "localhost", "/health", method="POST")
        self.assertEqual("404 Not Found", status)
        status, _ = self._call(app, "other.example.test", "/health")
        self.assertEqual("404 Not Found", status)

    def test_active_tenant_keeps_storefront_and_local_socket_health_separate(self):
        active_manifest = ManagedRuntimeManifest(
            tenant_id=self.manifest.tenant_id,
            canonical_host=self.manifest.canonical_host,
            allowed_hosts=self.manifest.allowed_hosts,
            database_path=self.manifest.database_path,
            media_root=self.manifest.media_root,
            backup_root=self.manifest.backup_root,
            socket_path=self.manifest.socket_path,
            owner_telegram_id=self.manifest.owner_telegram_id,
            lifecycle_state="active",
            runtime_generation=self.manifest.runtime_generation,
            entitlements=self.manifest.entitlements,
        )

        def storefront(_environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"storefront"]

        app = create_managed_tenant_wsgi_app(
            active_manifest,
            context=self.context,
            storefront_app=storefront,
            bot_token="123456:test-token",
        )
        status, body = self._call(app, "store.example.test", "/")
        self.assertEqual(("200 OK", b"storefront"), (status, body))
        status, body = self._call(app, "localhost", "/health")
        self.assertEqual("200 OK", status)
        self.assertEqual(
            {"tenant_id": self.context.tenant_id, "generation": 1}, json.loads(body)
        )
    def test_rematerialized_manifest_rejects_revoked_custom_host(self):
        active_manifest = ManagedRuntimeManifest(
            tenant_id=self.manifest.tenant_id,
            canonical_host=self.manifest.canonical_host,
            allowed_hosts=frozenset({self.manifest.canonical_host, "books.store.example.test"}),
            database_path=self.manifest.database_path,
            media_root=self.manifest.media_root,
            backup_root=self.manifest.backup_root,
            socket_path=self.manifest.socket_path,
            owner_telegram_id=self.manifest.owner_telegram_id,
            lifecycle_state="active",
            runtime_generation=2,
            entitlements=self.manifest.entitlements,
        )
        def storefront(_environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"storefront"]
        before = create_managed_tenant_wsgi_app(
            active_manifest, context=self.context, storefront_app=storefront, bot_token="123456:test-token"
        )
        self.assertEqual("200 OK", self._call(before, "books.store.example.test", "/")[0])
        revoked_manifest = ManagedRuntimeManifest(
            tenant_id=active_manifest.tenant_id,
            canonical_host=active_manifest.canonical_host,
            allowed_hosts=frozenset({active_manifest.canonical_host}),
            database_path=active_manifest.database_path,
            media_root=active_manifest.media_root,
            backup_root=active_manifest.backup_root,
            socket_path=active_manifest.socket_path,
            owner_telegram_id=active_manifest.owner_telegram_id,
            lifecycle_state="active",
            runtime_generation=3,
            entitlements=active_manifest.entitlements,
        )
        after = create_managed_tenant_wsgi_app(
            revoked_manifest, context=self.context, storefront_app=storefront, bot_token="123456:test-token"
        )
        self.assertEqual("404 Not Found", self._call(after, "books.store.example.test", "/")[0])
        self.assertEqual("200 OK", self._call(after, "store.example.test", "/")[0])


if __name__ == "__main__":
    unittest.main()
