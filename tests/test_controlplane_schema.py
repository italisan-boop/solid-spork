import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from controlplane.schema import connect, initialize


class ControlPlaneSchemaMigrationTests(unittest.TestCase):
    def test_current_schema_contains_managed_tenant_and_teardown_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "control.sqlite"
            initialize(database_path)
            database = sqlite3.connect(database_path)
            try:
                tenant_columns = {
                    row[1]
                    for row in database.execute("PRAGMA table_info(platform_tenants)")
                }
                jobs_schema = database.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'tenant_deployment_jobs'"
                ).fetchone()[0]
            finally:
                database.close()
        self.assertIn("tenant_kind", tenant_columns)
        self.assertIn("teardown", jobs_schema)

    def test_control_database_allows_platform_control_group_writes(self):
        if os.name != "posix":
            self.skipTest("control database modes require POSIX")
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "control.sqlite"
            previous_umask = os.umask(0o022)
            try:
                initialize(database_path)
                database = connect(database_path)
                try:
                    database.execute("BEGIN IMMEDIATE")
                    for candidate in (
                        database_path,
                        database_path.with_name("control.sqlite-shm"),
                        database_path.with_name("control.sqlite-wal"),
                    ):
                        self.assertTrue(candidate.is_file())
                        self.assertEqual(0o660, stat.S_IMODE(candidate.stat().st_mode))
                finally:
                    database.rollback()
                    database.close()
            finally:
                os.umask(previous_umask)

    def test_upgrades_existing_secret_envelope_constraint_for_delivery_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "control.sqlite"
            database = sqlite3.connect(database_path)
            try:
                database.execute(
                    """
                    CREATE TABLE tenant_secret_envelopes (
                        tenant_id TEXT NOT NULL,
                        secret_kind TEXT NOT NULL CHECK (secret_kind IN (
                            'telegram_bot_token', 'telegram_webhook_secret'
                        )),
                        generation INTEGER NOT NULL CHECK (generation > 0),
                        algorithm TEXT NOT NULL,
                        ciphertext TEXT NOT NULL,
                        data_nonce TEXT NOT NULL,
                        wrapped_key TEXT NOT NULL,
                        wrap_nonce TEXT NOT NULL,
                        key_version TEXT NOT NULL,
                        configured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        configured_by_platform_admin_id INTEGER NOT NULL,
                        PRIMARY KEY (tenant_id, secret_kind)
                    )
                    """
                )
                database.commit()
            finally:
                database.close()

            initialize(database_path)
            database = sqlite3.connect(database_path)
            try:
                schema = database.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'tenant_secret_envelopes'"
                ).fetchone()[0]
            finally:
                database.close()
        self.assertIn("bot_proxy_url", schema)
        self.assertIn("delivery_encryption_keys", schema)

    def test_upgrades_secret_reference_constraint_for_tenant_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "control.sqlite"
            database = sqlite3.connect(database_path)
            try:
                database.execute(
                    """
                    CREATE TABLE tenant_secret_references (
                        tenant_id TEXT NOT NULL,
                        secret_kind TEXT NOT NULL CHECK (secret_kind IN (
                            'telegram_bot_token', 'telegram_webhook_secret',
                            'yookassa_credentials', 'delivery_encryption_keys'
                        )),
                        reference TEXT NOT NULL,
                        version TEXT NOT NULL DEFAULT '',
                        configured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        configured_by_platform_admin_id INTEGER NOT NULL,
                        PRIMARY KEY (tenant_id, secret_kind)
                    )
                    """
                )
                database.commit()
            finally:
                database.close()
            initialize(database_path)
            database = sqlite3.connect(database_path)
            try:
                schema = database.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'tenant_secret_references'"
                ).fetchone()[0]
            finally:
                database.close()
        self.assertIn("bot_proxy_url", schema)

    def test_upgrades_runtime_deployment_with_published_host_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "control.sqlite"
            database = sqlite3.connect(database_path)
            try:
                database.execute(
                    """
                    CREATE TABLE tenant_runtime_deployments (
                        tenant_id TEXT PRIMARY KEY,
                        system_user TEXT NOT NULL UNIQUE,
                        system_uid INTEGER,
                        desired_generation INTEGER NOT NULL,
                        applied_generation INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL,
                        last_stage TEXT NOT NULL DEFAULT '',
                        last_error_type TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                database.commit()
            finally:
                database.close()
            initialize(database_path)
            database = sqlite3.connect(database_path)
            try:
                columns = {
                    row[1]
                    for row in database.execute(
                        "PRAGMA table_info(tenant_runtime_deployments)"
                    ).fetchall()
                }
            finally:
                database.close()
        self.assertIn("published_hosts_json", columns)
