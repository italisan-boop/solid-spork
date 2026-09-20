import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from controlplane.controller import DeploymentController, DeploymentFailure
from controlplane.deployments import (
    claim_next_deployment_job,
    complete_deployment_job,
    complete_teardown_job,
    queue_teardown_job,
    recover_abandoned_deployment_jobs,
    request_deployment,
    tenant_system_user,
)
from controlplane.plan_policy import Plan
from controlplane.schema import initialize
from controlplane.tenants import create_tenant, set_secret_envelopes
from controlplane.secret_envelopes import EnvelopeCipher


class DeploymentJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database_path = root / "control.sqlite"
        initialize(self.database_path)
        self.tenant = create_tenant(
            self.database_path,
            display_name="Managed runtime",
            slug="managed-runtime",
            owner_telegram_id=101,
            plan=Plan.BUSINESS,
            tenant_data_root=root / "tenants",
            tenant_backup_root=root / "backups",
            tenant_base_domain="shops.example.test",
            actor_telegram_id=1,
        )
        set_secret_envelopes(
            self.database_path,
            tenant_id=self.tenant.id,
            values={
                "telegram_bot_token": "123456:managed-token",
                "telegram_webhook_secret": "managed-webhook-secret",
            },
            actor_telegram_id=1,
            sealer=EnvelopeCipher(b"k" * 32, "v1"),
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_system_user_is_stable_and_uuid_derived(self):
        self.assertEqual(
            tenant_system_user(self.tenant.id), tenant_system_user(self.tenant.id)
        )
        self.assertRegex(tenant_system_user(self.tenant.id), r"\Atenant-[0-9a-f]{16}\Z")
        with self.assertRaises(ValueError):
            tenant_system_user("../../root")

    def test_teardown_job_prioritizes_cleanup_and_finalizes_tombstone(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE platform_tenants SET tenant_kind = 'managed', lifecycle_state = 'deleting', runtime_generation = runtime_generation + 1 WHERE id = ?",
                (self.tenant.id,),
            )
            connection.commit()
            connection.row_factory = sqlite3.Row
            tenant = connection.execute(
                "SELECT * FROM platform_tenants WHERE id = ?", (self.tenant.id,)
            ).fetchone()
            connection.execute("BEGIN IMMEDIATE")
            job = queue_teardown_job(
                connection, tenant=tenant, actor_telegram_id=1
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual("teardown", job.operation)
        self.assertEqual(job, claim_next_deployment_job(self.database_path))
        complete_teardown_job(
            self.database_path,
            job=job,
            succeeded=True,
            stage="teardown_completed",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            tenant_state = connection.execute(
                "SELECT tenant_kind, lifecycle_state FROM platform_tenants WHERE id = ?",
                (self.tenant.id,),
            ).fetchone()
            runtime = connection.execute(
                "SELECT state, published_hosts_json FROM tenant_runtime_deployments WHERE tenant_id = ?",
                (self.tenant.id,),
            ).fetchone()
            secrets = connection.execute(
                "SELECT COUNT(*) FROM tenant_secret_envelopes WHERE tenant_id = ?",
                (self.tenant.id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(("managed", "deleted"), tenant_state)
        self.assertEqual(("stopped", None), runtime)
        self.assertEqual(0, secrets)

    def test_failed_teardown_can_be_requeued_without_duplicate_live_jobs(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE platform_tenants SET tenant_kind = 'managed', lifecycle_state = 'deleting', runtime_generation = runtime_generation + 1 WHERE id = ?",
                (self.tenant.id,),
            )
            connection.commit()
            connection.row_factory = sqlite3.Row
            tenant = connection.execute(
                "SELECT * FROM platform_tenants WHERE id = ?", (self.tenant.id,)
            ).fetchone()
            connection.execute("BEGIN IMMEDIATE")
            first = queue_teardown_job(connection, tenant=tenant, actor_telegram_id=1)
            connection.commit()
        finally:
            connection.close()
        claimed = claim_next_deployment_job(self.database_path)
        complete_teardown_job(
            self.database_path,
            job=claimed,
            succeeded=False,
            stage="teardown_route",
            error_type="LinuxOperationsError",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            tenant = connection.execute(
                "SELECT * FROM platform_tenants WHERE id = ?", (self.tenant.id,)
            ).fetchone()
            connection.execute("BEGIN IMMEDIATE")
            retry = queue_teardown_job(connection, tenant=tenant, actor_telegram_id=1)
            connection.commit()
            live = connection.execute(
                "SELECT COUNT(*) FROM tenant_deployment_jobs WHERE tenant_id = ? AND operation = 'teardown' AND state IN ('pending', 'running')",
                (self.tenant.id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotEqual(first.id, retry.id)
        self.assertEqual(1, live)
    def test_claim_and_complete_deployment_job_uses_safe_outcome(self):
        requested = request_deployment(
            self.database_path,
            tenant_id=self.tenant.id,
            operation="provision",
            actor_telegram_id=1,
        )
        claimed = claim_next_deployment_job(self.database_path)
        self.assertEqual(requested, claimed)
        complete_deployment_job(
            self.database_path,
            job=claimed,
            succeeded=False,
            stage="health_checked",
            error_type="ConnectionError",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            job = connection.execute(
                "SELECT state, outcome_json FROM tenant_deployment_jobs WHERE id = ?",
                (requested.id,),
            ).fetchone()
            deployment = connection.execute(
                """
                SELECT system_user, state, last_stage, last_error_type
                FROM tenant_runtime_deployments WHERE tenant_id = ?
                """,
                (self.tenant.id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual("failed", job[0])
        self.assertEqual('{"stage":"health_checked","error_type":"ConnectionError"}', job[1])
        self.assertEqual(
            (tenant_system_user(self.tenant.id), "failed", "health_checked", "ConnectionError"),
            deployment,
        )

    def test_successful_deployment_records_published_hosts(self):
        requested = request_deployment(
            self.database_path,
            tenant_id=self.tenant.id,
            operation="provision",
            actor_telegram_id=1,
        )
        claimed = claim_next_deployment_job(self.database_path)
        complete_deployment_job(
            self.database_path,
            job=claimed,
            succeeded=True,
            stage="routing_published",
            applied_hosts=[self.tenant.canonical_host],
        )
        connection = sqlite3.connect(self.database_path)
        try:
            hosts = connection.execute(
                "SELECT published_hosts_json FROM tenant_runtime_deployments WHERE tenant_id = ?",
                (requested.tenant_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual([self.tenant.canonical_host], json.loads(hosts))

    def test_draft_secret_update_advances_existing_provision_job_only(self):
        requested = request_deployment(
            self.database_path,
            tenant_id=self.tenant.id,
            operation="provision",
            actor_telegram_id=1,
        )
        updated = set_secret_envelopes(
            self.database_path,
            tenant_id=self.tenant.id,
            values={"bot_proxy_url": "http://user:password@203.0.113.10:3128"},
            actor_telegram_id=1,
            sealer=EnvelopeCipher(b"k" * 32, "v1"),
            managed_reconciliation=True,
        )
        connection = sqlite3.connect(self.database_path)
        try:
            job = connection.execute(
                "SELECT desired_generation FROM tenant_deployment_jobs WHERE id = ?",
                (requested.id,),
            ).fetchone()
            runtime = connection.execute(
                "SELECT desired_generation FROM tenant_runtime_deployments WHERE tenant_id = ?",
                (self.tenant.id,),
            ).fetchone()
            jobs = connection.execute(
                "SELECT COUNT(*) FROM tenant_deployment_jobs WHERE tenant_id = ?",
                (self.tenant.id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual((updated.runtime_generation,), job)
        self.assertEqual((updated.runtime_generation,), runtime)
        self.assertEqual((1,), jobs)

    def test_request_does_not_reap_a_running_job(self):
        first = request_deployment(
            self.database_path,
            tenant_id=self.tenant.id,
            operation="provision",
            actor_telegram_id=1,
        )
        self.assertEqual(first, claim_next_deployment_job(self.database_path))
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """
                UPDATE tenant_deployment_jobs
                SET claimed_at = datetime('now', '-301 seconds')
                WHERE id = ?
                """,
                (first.id,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(ValueError, "already pending"):
            request_deployment(
                self.database_path,
                tenant_id=self.tenant.id,
                operation="provision",
                actor_telegram_id=1,
            )
        connection = sqlite3.connect(self.database_path)
        try:
            outcome = connection.execute(
                "SELECT state, outcome_json FROM tenant_deployment_jobs WHERE id = ?",
                (first.id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(("running", "{}"), outcome)

    def test_singleton_startup_requeues_abandoned_running_job(self):
        first = request_deployment(
            self.database_path,
            tenant_id=self.tenant.id,
            operation="provision",
            actor_telegram_id=1,
        )
        self.assertEqual(first, claim_next_deployment_job(self.database_path))
        self.assertEqual(1, recover_abandoned_deployment_jobs(self.database_path))
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT operation, state, outcome_json FROM tenant_deployment_jobs ORDER BY created_at, id"
            ).fetchall()
        finally:
            connection.close()
        self.assertIn(("provision", "failed", '{"stage":"identity_allocated","error_type":"ControllerRestarted"}'), rows)
        self.assertIn(("provision", "pending", "{}"), rows)

    def test_activation_generation_advance_completes_without_redeploy(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE platform_tenants SET lifecycle_state = 'awaiting_owner_claim' WHERE id = ?",
                (self.tenant.id,),
            )
            connection.commit()
        finally:
            connection.close()
        requested = request_deployment(
            self.database_path,
            tenant_id=self.tenant.id,
            operation="activate",
            actor_telegram_id=1,
        )
        claimed = claim_next_deployment_job(self.database_path)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """
                UPDATE platform_tenants
                SET lifecycle_state = 'active', runtime_generation = runtime_generation + 1
                WHERE id = ?
                """,
                (self.tenant.id,),
            )
            connection.commit()
            generation = connection.execute(
                "SELECT runtime_generation FROM platform_tenants WHERE id = ?",
                (self.tenant.id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertTrue(
            complete_deployment_job(
                self.database_path,
                job=claimed,
                succeeded=True,
                stage="routing_published",
                applied_generation=generation,
            )
        )
        connection = sqlite3.connect(self.database_path)
        try:
            job = connection.execute(
                "SELECT state, outcome_json FROM tenant_deployment_jobs WHERE id = ?",
                (requested.id,),
            ).fetchone()
            deployment = connection.execute(
                """
                SELECT state, desired_generation, applied_generation
                FROM tenant_runtime_deployments WHERE tenant_id = ?
                """,
                (self.tenant.id,),
            ).fetchone()
            pending = connection.execute(
                """
                SELECT COUNT(*) FROM tenant_deployment_jobs
                WHERE tenant_id = ? AND state = 'pending'
                """,
                (self.tenant.id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(("succeeded", '{"stage":"routing_published"}'), job)
        self.assertEqual(("running", generation, generation), deployment)
        self.assertEqual(0, pending)

    def test_activation_after_managed_mutation_completes_at_latest_generation(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE platform_tenants SET lifecycle_state = 'awaiting_owner_claim' WHERE id = ?",
                (self.tenant.id,),
            )
            connection.commit()
        finally:
            connection.close()
        request_deployment(
            self.database_path,
            tenant_id=self.tenant.id,
            operation="activate",
            actor_telegram_id=1,
        )
        claimed = claim_next_deployment_job(self.database_path)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE platform_tenants SET runtime_generation = runtime_generation + 1 WHERE id = ?",
                (self.tenant.id,),
            )
            connection.execute(
                "UPDATE tenant_runtime_deployments SET desired_generation = desired_generation + 1 WHERE tenant_id = ?",
                (self.tenant.id,),
            )
            connection.execute(
                """
                UPDATE platform_tenants
                SET lifecycle_state = 'active', runtime_generation = runtime_generation + 1
                WHERE id = ?
                """,
                (self.tenant.id,),
            )
            connection.commit()
            generation = connection.execute(
                "SELECT runtime_generation FROM platform_tenants WHERE id = ?",
                (self.tenant.id,),
            ).fetchone()[0]
        finally:
            connection.close()
        complete_deployment_job(
            self.database_path,
            job=claimed,
            succeeded=True,
            stage="routing_published",
            applied_generation=generation,
        )
        connection = sqlite3.connect(self.database_path)
        try:
            job = connection.execute(
                "SELECT state, outcome_json FROM tenant_deployment_jobs WHERE id = ?",
                (claimed.id,),
            ).fetchone()
            deployment = connection.execute(
                """
                SELECT state, desired_generation, applied_generation
                FROM tenant_runtime_deployments WHERE tenant_id = ?
                """,
                (self.tenant.id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(("succeeded", '{"stage":"routing_published"}'), job)
        self.assertEqual(("running", generation, generation), deployment)

    def test_superseded_completion_never_marks_old_generation_current(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE platform_tenants SET lifecycle_state = 'active' WHERE id = ?",
                (self.tenant.id,),
            )
            connection.commit()
        finally:
            connection.close()
        requested = request_deployment(
            self.database_path,
            tenant_id=self.tenant.id,
            operation="redeploy",
            actor_telegram_id=1,
        )
        claimed = claim_next_deployment_job(self.database_path)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE platform_tenants SET runtime_generation = runtime_generation + 1 WHERE id = ?",
                (self.tenant.id,),
            )
            connection.execute(
                "UPDATE tenant_runtime_deployments SET desired_generation = desired_generation + 1 WHERE tenant_id = ?",
                (self.tenant.id,),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertTrue(
            complete_deployment_job(
                self.database_path,
                job=claimed,
                succeeded=True,
                stage="routing_published",
                applied_generation=requested.desired_generation,
            )
        )
        connection = sqlite3.connect(self.database_path)
        try:
            completed = connection.execute(
                "SELECT state, outcome_json FROM tenant_deployment_jobs WHERE id = ?",
                (requested.id,),
            ).fetchone()
            deployment = connection.execute(
                "SELECT state, desired_generation, applied_generation FROM tenant_runtime_deployments WHERE tenant_id = ?",
                (self.tenant.id,),
            ).fetchone()
            pending = connection.execute(
                "SELECT operation, desired_generation FROM tenant_deployment_jobs WHERE tenant_id = ? AND state = 'pending'",
                (self.tenant.id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(("failed", '{"stage":"routing_published","error_type":"GenerationSuperseded"}'), completed)
        self.assertEqual("pending", deployment[0])
        self.assertGreater(deployment[1], deployment[2])
        self.assertEqual(("redeploy", deployment[1]), pending)

    def test_controller_records_allowlisted_failure_without_exception_text(self):
        request_deployment(
            self.database_path,
            tenant_id=self.tenant.id,
            operation="provision",
            actor_telegram_id=1,
        )

        class FailingAdapter:
            def reconcile(self, _job):
                raise DeploymentFailure("schema_initialized", "PermissionError")

        self.assertTrue(
            DeploymentController(self.database_path, FailingAdapter()).run_once()
        )
        connection = sqlite3.connect(self.database_path)
        try:
            outcome = connection.execute(
                "SELECT outcome_json FROM tenant_deployment_jobs"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(
            '{"stage":"schema_initialized","error_type":"PermissionError"}',
            outcome,
        )


if __name__ == "__main__":
    unittest.main()
