import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from controlplane.deployments import DeploymentFailure, DeploymentJob, tenant_system_user
from controlplane.materializer import ManifestSigner
from controlplane.plan_policy import Plan, effective_entitlements
from controlplane.root_adapter import RootDeploymentAdapter
from controlplane.schema import initialize
from db.schema import initialize_database
from controlplane.secret_envelopes import EnvelopeCipher
from controlplane.tenants import (
    create_tenant,
    get_tenant,
    request_custom_domain,
    request_managed_deletion,
    set_custom_domain_verification,
    set_secret_envelopes,
)
from controlplane.unit_templates import ControllerPaths
from runtime.context import TenantContext
from runtime.managed_wsgi import create_managed_tenant_wsgi_app
from runtime.manifest import ManagedRuntimeManifest


class FakePrivilegedOperations:
    def __init__(self):
        self.calls = []
        self.material = None
        self.route = None
        self.health_error = False
        self.health_app = None
        self.health_payload = None
        self.database_path = None

    def ensure_tenant_identity(self, tenant_id):
        self.calls.append(("identity", tenant_id))
        return 20_001

    def prepare_tenant_storage(self, **kwargs):
        self.calls.append(("storage", kwargs["uid"]))

    def initialize_tenant_database(self, **kwargs):
        self.calls.append(("schema", kwargs["tenant_id"], kwargs["uid"]))
        initialize_database(self.database_path, seed_catalog=False)

    def owner_claim_verdict(self, **kwargs):
        self.calls.append(("claim", kwargs["tenant_id"]))
        database = sqlite3.connect(self.database_path)
        try:
            row = database.execute(
                "SELECT telegram_user_id FROM tenant_owner_claims WHERE id = 1"
            ).fetchone()
        finally:
            database.close()
        if row is None:
            return "missing"
        return "claimed" if row[0] == kwargs["owner_telegram_id"] else "owner_mismatch"

    def write_runtime_material(self, **kwargs):
        self.calls.append(("material", kwargs["tenant_id"]))
        self.material = kwargs

    def install_tenant_unit(self, **kwargs):
        self.calls.append(("unit", kwargs["tenant_id"]))

    def start_tenant_unit(self, tenant_id):
        self.calls.append(("start", tenant_id))

    def check_tenant_health(self, tenant_id, runtime_generation):
        self.calls.append(("health", tenant_id, runtime_generation))
        if self.health_error:
            raise ConnectionError("unavailable")
        if self.health_app is None:
            return
        response = {}
        result = self.health_app(
            {
                "HTTP_HOST": "localhost",
                "PATH_INFO": "/health",
                "REQUEST_METHOD": "GET",
                "SERVER_NAME": "localhost",
                "SERVER_PORT": "80",
                "wsgi.url_scheme": "http",
            },
            lambda status, _headers: response.setdefault("status", status),
        )
        try:
            payload = json.loads(b"".join(result))
        finally:
            close = getattr(result, "close", None)
            if close is not None:
                close()
        if self.health_payload is not None:
            payload = self.health_payload
        if response["status"] != "200 OK" or payload != {
            "tenant_id": tenant_id,
            "generation": runtime_generation,
        }:
            raise ValueError("tenant health response is invalid")

    def withdraw_tenant_route(self, tenant_id):
        self.calls.append(("withdraw", tenant_id))

    def stop_tenant_unit(self, tenant_id):
        self.calls.append(("stop", tenant_id))

    def remove_tenant_runtime_material(self, tenant_id):
        self.calls.append(("remove", tenant_id))

    def publish_tenant_route(self, **kwargs):
        self.calls.append(("route", kwargs["tenant_id"]))
        self.route = kwargs["hosts"]


class RootDeploymentAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.control_database = self.root / "control.sqlite"
        initialize(self.control_database)
        self.tenant = create_tenant(
            self.control_database,
            display_name="Controller tenant",
            slug="controller-tenant",
            owner_telegram_id=101,
            plan=Plan.BUSINESS,
            tenant_data_root=self.root / "tenants",
            tenant_backup_root=self.root / "backups",
            tenant_base_domain="shops.example.test",
            actor_telegram_id=1,
        )
        self.cipher = EnvelopeCipher(b"r" * 32, "v1")
        set_secret_envelopes(
            self.control_database,
            tenant_id=self.tenant.id,
            values={
                "telegram_bot_token": "123456:controller-token",
                "telegram_webhook_secret": "controller-webhook-secret",
            },
            actor_telegram_id=1,
            sealer=self.cipher,
        )
        self.tenant = get_tenant(self.control_database, self.tenant.id)
        self.operations = FakePrivilegedOperations()
        self.operations.database_path = self.tenant.database_path
        self.adapter = RootDeploymentAdapter(
            self.control_database,
            paths=ControllerPaths(
                release_root=self.root / "release",
                credential_root=self.root / "credentials",
                runtime_root=self.root / "runtime",
                tenant_data_root=self.root / "tenants",
                tenant_backup_root=self.root / "backups",
                public_key_file=self.root / "keys" / "manifest-public.key",
            ),
            cipher=self.cipher,
            signer=ManifestSigner(Ed25519PrivateKey.generate()),
            operations=self.operations,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_reconcile_uses_fixed_privileged_operations_in_order(self):
        job = DeploymentJob(
            id="job-id",
            tenant_id=self.tenant.id,
            operation="provision",
            desired_generation=self.tenant.runtime_generation,
        )
        self.assertEqual("routing_published", self.adapter.reconcile(job).stage)
        self.assertEqual(
            ["identity", "storage", "schema", "material", "unit", "start", "health", "route"],
            [item[0] for item in self.operations.calls],
        )
        self.assertEqual(
            {
                "telegram_bot_token",
                "telegram_webhook_secret",
                "delivery_encryption_keys",
            },
            set(self.operations.material["credentials"]),
        )
        self.assertIn(self.tenant.canonical_host, self.operations.route)
        self.assertNotIn("controller-token", self.operations.route)
        provisioned = get_tenant(self.control_database, self.tenant.id)
        self.assertEqual("awaiting_owner_claim", provisioned.lifecycle_state)
        connection = sqlite3.connect(self.control_database)
        try:
            audit = connection.execute(
                """
                SELECT actor_telegram_id, action FROM platform_audit_events
                WHERE tenant_id = ? AND action = 'tenant.managed_provisioning.completed'
                """,
                (self.tenant.id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual((0, "tenant.managed_provisioning.completed"), audit)

    def test_provision_completion_does_not_probe_tenant_database_from_controller(self):
        job = DeploymentJob(
            id="privilege-boundary-job",
            tenant_id=self.tenant.id,
            operation="provision",
            desired_generation=self.tenant.runtime_generation,
        )
        with patch(
            "controlplane.tenants.Path.is_file",
            side_effect=AssertionError("controller must not inspect tenant storage"),
        ):
            self.adapter.reconcile(job)
        self.assertEqual(
            "awaiting_owner_claim",
            get_tenant(self.control_database, self.tenant.id).lifecycle_state,
        )

    def test_provision_uses_real_managed_local_health_before_route_publication(self):
        self.operations.health_app = self._managed_health_app()
        job = DeploymentJob(
            id="health-job",
            tenant_id=self.tenant.id,
            operation="provision",
            desired_generation=self.tenant.runtime_generation,
            actor_telegram_id=17,
        )
        self.assertEqual("routing_published", self.adapter.reconcile(job).stage)
        self.assertLess(
            [item[0] for item in self.operations.calls].index("health"),
            [item[0] for item in self.operations.calls].index("route"),
        )
        self.assertEqual("awaiting_owner_claim", get_tenant(self.control_database, self.tenant.id).lifecycle_state)

    def test_mismatched_local_health_never_publishes_route(self):
        self.operations.health_app = self._managed_health_app()
        self.operations.health_payload = {
            "tenant_id": self.tenant.id,
            "generation": self.tenant.runtime_generation + 1,
        }
        job = DeploymentJob(
            id="health-mismatch-job",
            tenant_id=self.tenant.id,
            operation="provision",
            desired_generation=self.tenant.runtime_generation,
            actor_telegram_id=17,
        )
        with self.assertRaises(DeploymentFailure) as failed:
            self.adapter.reconcile(job)
        self.assertEqual("health_checked", failed.exception.stage)
        self.assertEqual("ValueError", failed.exception.error_type)
        self.assertNotIn("route", [item[0] for item in self.operations.calls])

    def _managed_health_app(self):
        context = TenantContext(
            tenant_id=self.tenant.id,
            canonical_host=self.tenant.canonical_host,
            database_path=self.tenant.database_path,
            media_root=self.tenant.media_root,
            backup_root=self.tenant.backup_root,
            owner_telegram_id=self.tenant.owner_telegram_id,
            entitlements=effective_entitlements(Plan.BUSINESS),
            runtime_generation=self.tenant.runtime_generation,
        )
        manifest = ManagedRuntimeManifest(
            tenant_id=context.tenant_id,
            canonical_host=context.canonical_host,
            allowed_hosts=frozenset({context.canonical_host}),
            database_path=context.database_path,
            media_root=context.media_root,
            backup_root=context.backup_root,
            socket_path=self.root / "runtime" / self.tenant.id / "tenant.sock",
            owner_telegram_id=context.owner_telegram_id,
            lifecycle_state="awaiting_owner_claim",
            runtime_generation=context.runtime_generation,
            entitlements=context.entitlements,
        )
        return create_managed_tenant_wsgi_app(
            manifest,
            context=context,
            storefront_app=lambda _environ, _start: [b"storefront"],
            bot_token="123456:test-token",
        )

    def _provision(self):
        job = DeploymentJob(
            id="provision-job",
            tenant_id=self.tenant.id,
            operation="provision",
            desired_generation=self.tenant.runtime_generation,
            actor_telegram_id=17,
        )
        self.adapter.reconcile(job)
        return get_tenant(self.control_database, self.tenant.id)

    def _record_owner_claim(self, tenant, telegram_user_id):
        database = sqlite3.connect(tenant.database_path)
        try:
            database.execute(
                "INSERT INTO tenant_owner_claims (id, telegram_user_id) VALUES (1, ?)",
                (telegram_user_id,),
            )
            database.commit()
        finally:
            database.close()

    def test_activation_requires_matching_owner_claim_and_audits_requesting_admin(self):
        provisioned = self._provision()
        job = DeploymentJob(
            id="activate-job",
            tenant_id=provisioned.id,
            operation="activate",
            desired_generation=provisioned.runtime_generation,
            actor_telegram_id=23,
        )
        self.operations.calls.clear()
        with self.assertRaises(DeploymentFailure) as missing_claim:
            self.adapter.reconcile(job)
        self.assertEqual("schema_initialized", missing_claim.exception.stage)
        self.assertEqual("ValueError", missing_claim.exception.error_type)
        self.assertEqual([("claim", provisioned.id)], self.operations.calls)

        self._record_owner_claim(provisioned, 999)
        with self.assertRaises(DeploymentFailure) as wrong_claim:
            self.adapter.reconcile(job)
        self.assertEqual("schema_initialized", wrong_claim.exception.stage)
        self.assertEqual("ValueError", wrong_claim.exception.error_type)
        self.assertEqual(
            [("claim", provisioned.id), ("claim", provisioned.id)],
            self.operations.calls,
        )

        database = sqlite3.connect(provisioned.database_path)
        try:
            database.execute(
                "UPDATE tenant_owner_claims SET telegram_user_id = ? WHERE id = 1",
                (provisioned.owner_telegram_id,),
            )
            database.commit()
        finally:
            database.close()
        activation = self.adapter.reconcile(job)
        self.assertEqual("routing_published", activation.stage)
        self.assertEqual(provisioned.runtime_generation + 1, activation.applied_generation)
        activated = get_tenant(self.control_database, provisioned.id)
        self.assertEqual("active", activated.lifecycle_state)
        connection = sqlite3.connect(self.control_database)
        try:
            audit = connection.execute(
                """
                SELECT actor_telegram_id FROM platform_audit_events
                WHERE tenant_id = ? AND action = 'tenant.activated_after_owner_claim'
                """,
                (provisioned.id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual((23,), audit)

    def test_redeploy_recreates_missing_delivery_credential_before_unit_materialization(self):
        provisioned = self._provision()
        self._record_owner_claim(provisioned, provisioned.owner_telegram_id)
        self.adapter.reconcile(
            DeploymentJob(
                id="activate-for-delivery-reconciliation",
                tenant_id=provisioned.id,
                operation="activate",
                desired_generation=provisioned.runtime_generation,
                actor_telegram_id=17,
            )
        )
        active = get_tenant(self.control_database, provisioned.id)
        database = sqlite3.connect(self.control_database)
        try:
            database.execute(
                """
                DELETE FROM tenant_secret_envelopes
                WHERE tenant_id = ? AND secret_kind = 'delivery_encryption_keys'
                """,
                (active.id,),
            )
            database.commit()
        finally:
            database.close()
        self.operations.calls.clear()
        self.adapter.reconcile(
            DeploymentJob(
                id="delivery-reconciliation",
                tenant_id=active.id,
                operation="redeploy",
                desired_generation=active.runtime_generation,
                actor_telegram_id=17,
            )
        )
        self.assertIn(
            "delivery_encryption_keys", self.operations.material["credentials"]
        )
        self.assertNotIn(
            self.operations.material["credentials"]["delivery_encryption_keys"],
            self.operations.material["manifest"],
        )
        self.assertLess(
            [call[0] for call in self.operations.calls].index("material"),
            [call[0] for call in self.operations.calls].index("unit"),
        )

    def test_managed_teardown_withdraws_route_before_unit_and_material_cleanup(self):
        connection = sqlite3.connect(self.control_database)
        try:
            connection.execute(
                "UPDATE platform_tenants SET tenant_kind = 'managed' WHERE id = ?",
                (self.tenant.id,),
            )
            connection.commit()
        finally:
            connection.close()
        self.tenant.database_path.parent.mkdir(parents=True)
        self.tenant.media_root.mkdir(parents=True)
        self.tenant.backup_root.mkdir(parents=True)
        job = request_managed_deletion(
            self.control_database,
            tenant_id=self.tenant.id,
            confirm_slug=self.tenant.slug,
            actor_telegram_id=17,
        )
        self.operations.calls.clear()
        result = self.adapter.reconcile(job)
        self.assertEqual("teardown_completed", result.stage)
        self.assertEqual(
            ["withdraw", "stop", "remove"],
            [item[0] for item in self.operations.calls],
        )
        self.assertNotIn("material", [item[0] for item in self.operations.calls])
        self.assertTrue(self.tenant.database_path.parent.exists())

    def test_redeploy_publishes_added_host_only_after_health(self):
        provisioned = self._provision()
        self._record_owner_claim(provisioned, provisioned.owner_telegram_id)
        self.adapter.reconcile(
            DeploymentJob(
                id="activate-for-addition-order",
                tenant_id=provisioned.id,
                operation="activate",
                desired_generation=provisioned.runtime_generation,
                actor_telegram_id=17,
            )
        )
        active = get_tenant(self.control_database, provisioned.id)
        request_custom_domain(
            self.control_database,
            tenant_id=active.id,
            host="new.controller.example",
            actor_telegram_id=17,
        )
        set_custom_domain_verification(
            self.control_database,
            tenant_id=active.id,
            host="new.controller.example",
            verification_state="verified",
            actor_telegram_id=17,
        )
        connection = sqlite3.connect(self.control_database)
        try:
            connection.execute(
                """
                INSERT INTO tenant_runtime_deployments (
                    tenant_id, system_user, desired_generation, applied_generation,
                    state, published_hosts_json
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (
                    active.id,
                    tenant_system_user(active.id),
                    active.runtime_generation,
                    active.runtime_generation,
                    json.dumps([active.canonical_host]),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        self.operations.calls.clear()
        self.adapter.reconcile(
            DeploymentJob(
                id="route-addition",
                tenant_id=active.id,
                operation="redeploy",
                desired_generation=active.runtime_generation,
                actor_telegram_id=17,
            )
        )
        calls = [call[0] for call in self.operations.calls]
        self.assertLess(calls.index("health"), calls.index("route"))
        self.assertEqual(
            sorted([active.canonical_host, "new.controller.example"]),
            self.operations.route,
        )

    def test_redeploy_withdraws_disabled_route_before_restart(self):
        provisioned = self._provision()
        self._record_owner_claim(provisioned, provisioned.owner_telegram_id)
        activated = self.adapter.reconcile(
            DeploymentJob(
                id="activate-for-route-order",
                tenant_id=provisioned.id,
                operation="activate",
                desired_generation=provisioned.runtime_generation,
                actor_telegram_id=17,
            )
        )
        active = get_tenant(self.control_database, provisioned.id)
        self.assertEqual(active.runtime_generation, activated.applied_generation)
        request_custom_domain(
            self.control_database,
            tenant_id=active.id,
            host="books.controller.example",
            actor_telegram_id=17,
        )
        set_custom_domain_verification(
            self.control_database,
            tenant_id=active.id,
            host="books.controller.example",
            verification_state="verified",
            actor_telegram_id=17,
        )
        set_custom_domain_verification(
            self.control_database,
            tenant_id=active.id,
            host="books.controller.example",
            verification_state="disabled",
            actor_telegram_id=17,
        )
        connection = sqlite3.connect(self.control_database)
        try:
            connection.execute(
                """
                INSERT INTO tenant_runtime_deployments (
                    tenant_id, system_user, desired_generation, applied_generation,
                    state, published_hosts_json
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (
                    active.id,
                    tenant_system_user(active.id),
                    active.runtime_generation,
                    active.runtime_generation,
                    json.dumps([active.canonical_host, "books.controller.example"]),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        self.operations.calls.clear()
        reconciliation = self.adapter.reconcile(
            DeploymentJob(
                id="route-withdrawal",
                tenant_id=active.id,
                operation="redeploy",
                desired_generation=active.runtime_generation,
                actor_telegram_id=17,
            )
        )
        calls = [call[0] for call in self.operations.calls]
        self.assertLess(calls.index("route"), calls.index("material"))
        self.assertLess(calls.index("route"), calls.index("start"))
        self.assertEqual([active.canonical_host], self.operations.route)
        manifest = json.loads(self.operations.material["manifest"])
        self.assertEqual([active.canonical_host], manifest["allowed_hosts"])
        self.assertEqual(active.runtime_generation, reconciliation.applied_generation)

    def test_final_route_publish_rechecks_control_generation(self):
        stale_generation = self.tenant.runtime_generation
        connection = sqlite3.connect(self.control_database)
        try:
            connection.execute(
                "UPDATE platform_tenants SET runtime_generation = runtime_generation + 1 WHERE id = ?",
                (self.tenant.id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(DeploymentFailure) as failed:
            self.adapter._publish_route_if_current(
                self.tenant, stale_generation, [self.tenant.canonical_host]
            )
        self.assertEqual("routing_published", failed.exception.stage)
        self.assertEqual("GenerationSuperseded", failed.exception.error_type)
        self.assertNotIn("route", [item[0] for item in self.operations.calls])

    def test_does_not_publish_route_when_health_check_fails(self):
        self.operations.health_error = True
        job = DeploymentJob(
            id="provision-job",
            tenant_id=self.tenant.id,
            operation="provision",
            desired_generation=self.tenant.runtime_generation,
            actor_telegram_id=17,
        )
        with self.assertRaises(DeploymentFailure) as failed:
            self.adapter.reconcile(job)
        self.assertEqual("health_checked", failed.exception.stage)
        self.assertEqual("ConnectionError", failed.exception.error_type)
        self.assertNotIn("route", [item[0] for item in self.operations.calls])
        self.assertNotIn("controller-token", str(self.operations.calls))

    def test_rejects_tampered_control_database_storage_path_before_operations(self):
        connection = sqlite3.connect(self.control_database)
        try:
            connection.execute(
                "UPDATE platform_tenants SET database_path = ? WHERE id = ?",
                (str(self.root / "outside.sqlite"), self.tenant.id),
            )
            connection.commit()
        finally:
            connection.close()
        job = DeploymentJob(
            id="provision-job",
            tenant_id=self.tenant.id,
            operation="provision",
            desired_generation=self.tenant.runtime_generation,
        )
        with self.assertRaisesRegex(ValueError, "storage paths are invalid"):
            self.adapter.reconcile(job)
        self.assertEqual([], self.operations.calls)


if __name__ == "__main__":
    unittest.main()
