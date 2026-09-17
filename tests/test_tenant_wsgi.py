import io
import sys
import tempfile
import unittest
from pathlib import Path

from controlplane.plan_policy import Plan
from controlplane.provisioning import provision_tenant
from controlplane.schema import initialize
from controlplane.tenants import (
    activate_after_owner_claim,
    create_tenant,
    get_tenant,
    set_secret_reference,
)
from runtime.tenant_wsgi import create_tenant_wsgi_app


class TenantWsgiTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.control_database = self.root / "control.sqlite"
        initialize(self.control_database)

    def tearDown(self):
        self._temporary_directory.cleanup()

    def create_tenant(self, slug: str):
        tenant = create_tenant(
            self.control_database,
            display_name=slug,
            slug=slug,
            owner_telegram_id=202,
            plan=Plan.BUSINESS,
            tenant_data_root=self.root / "tenants",
            tenant_backup_root=self.root / "backups",
            tenant_base_domain="shops.example.test",
            actor_telegram_id=101,
        )
        for kind in ("telegram_bot_token", "telegram_webhook_secret"):
            set_secret_reference(
                self.control_database,
                tenant_id=tenant.id,
                secret_kind=kind,
                reference=f"vault:tenants/{tenant.id}/{kind}",
                version="v1",
                actor_telegram_id=101,
            )
        provision_tenant(self.control_database, tenant_id=tenant.id, actor_telegram_id=101)
        return get_tenant(self.control_database, tenant.id)

    def activate(self, tenant):
        import sqlite3

        database = sqlite3.connect(tenant.database_path)
        try:
            database.execute(
                "INSERT INTO tenant_owner_claims (id, telegram_user_id) VALUES (1, ?)",
                (tenant.owner_telegram_id,),
            )
            database.commit()
        finally:
            database.close()
        return activate_after_owner_claim(
            self.control_database, tenant_id=tenant.id, actor_telegram_id=101
        )

    def call(self, app, host: str, path: str):
        response = {}
        iterable = app(
            {
                "HTTP_HOST": host,
                "PATH_INFO": path,
                "REQUEST_METHOD": "GET",
                "SERVER_NAME": host,
                "SERVER_PORT": "443",
                "wsgi.url_scheme": "https",
                "wsgi.input": io.BytesIO(),
                "wsgi.errors": sys.stderr,
                "wsgi.version": (1, 0),
                "wsgi.multithread": False,
                "wsgi.multiprocess": False,
                "wsgi.run_once": False,
            },
            lambda status, _headers: response.setdefault("status", status),
        )
        try:
            body = b"".join(iterable)
        finally:
            close = getattr(iterable, "close", None)
            if close:
                close()
        return response["status"], body

    def test_bound_worker_rejects_another_active_tenant_host(self):
        first = self.activate(self.create_tenant("first-store"))
        second = self.activate(self.create_tenant("second-store"))

        def storefront(_environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"storefront"]

        app = create_tenant_wsgi_app(
            self.control_database,
            tenant_id=first.id,
            storefront_app=storefront,
            bot_token="123456:tenant-token",
        )
        self.assertEqual(("200 OK", b"storefront"), self.call(app, first.canonical_host, "/"))
        status, body = self.call(app, second.canonical_host, "/")
        self.assertEqual("404 Not Found", status)
        self.assertIn(b"tenant host is unavailable", body)

    def test_owner_onboarding_host_exposes_only_setup_routes(self):
        tenant = self.create_tenant("claim-store")

        def storefront(_environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"storefront"]

        app = create_tenant_wsgi_app(
            self.control_database,
            tenant_id=tenant.id,
            storefront_app=storefront,
            bot_token="123456:tenant-token",
        )
        status, body = self.call(app, tenant.canonical_host, "/")
        self.assertEqual("404 Not Found", status)
        self.assertIn(b"tenant host is unavailable", body)
        status, body = self.call(app, tenant.canonical_host, "/setup")
        self.assertEqual("200 OK", status)
        self.assertIn("Первоначальная настройка".encode(), body)


if __name__ == "__main__":
    unittest.main()
