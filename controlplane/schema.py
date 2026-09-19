from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


_SCHEMA_VERSION = 6
_LOCK = threading.Lock()


def resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("control-plane database path must be absolute")
    return path.resolve()


def connect(path: str | Path) -> sqlite3.Connection:
    resolved = resolve_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(str(resolved), timeout=10)
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA busy_timeout = 10000")
    return database


def _upgrade_tenant_secret_envelopes(database: sqlite3.Connection) -> None:
    row = database.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tenant_secret_envelopes'"
    ).fetchone()
    if row is None or "bot_proxy_url" in row[0]:
        return
    database.execute("ALTER TABLE tenant_secret_envelopes RENAME TO tenant_secret_envelopes_legacy")
    database.execute(
        """
        CREATE TABLE tenant_secret_envelopes (
            tenant_id TEXT NOT NULL,
            secret_kind TEXT NOT NULL CHECK (secret_kind IN (
                'telegram_bot_token', 'telegram_webhook_secret', 'bot_proxy_url'
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
            PRIMARY KEY (tenant_id, secret_kind),
            FOREIGN KEY (tenant_id) REFERENCES platform_tenants (id) ON DELETE RESTRICT
        )
        """
    )
    database.execute(
        """
        INSERT INTO tenant_secret_envelopes (
            tenant_id, secret_kind, generation, algorithm, ciphertext, data_nonce,
            wrapped_key, wrap_nonce, key_version, configured_at,
            configured_by_platform_admin_id
        )
        SELECT tenant_id, secret_kind, generation, algorithm, ciphertext, data_nonce,
               wrapped_key, wrap_nonce, key_version, configured_at,
               configured_by_platform_admin_id
        FROM tenant_secret_envelopes_legacy
        """
    )
    database.execute("DROP TABLE tenant_secret_envelopes_legacy")
def _upgrade_tenant_secret_references(database: sqlite3.Connection) -> None:
    row = database.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tenant_secret_references'"
    ).fetchone()
    if row is None or "bot_proxy_url" in row[0]:
        return
    database.execute(
        "ALTER TABLE tenant_secret_references RENAME TO tenant_secret_references_legacy"
    )
    database.execute(
        """
        CREATE TABLE tenant_secret_references (
            tenant_id TEXT NOT NULL,
            secret_kind TEXT NOT NULL CHECK (secret_kind IN (
                'telegram_bot_token', 'telegram_webhook_secret', 'bot_proxy_url',
                'yookassa_credentials', 'delivery_encryption_keys'
            )),
            reference TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '',
            configured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            configured_by_platform_admin_id INTEGER NOT NULL,
            PRIMARY KEY (tenant_id, secret_kind),
            FOREIGN KEY (tenant_id) REFERENCES platform_tenants (id) ON DELETE RESTRICT
        )
        """
    )
    database.execute(
        """
        INSERT INTO tenant_secret_references (
            tenant_id, secret_kind, reference, version, configured_at,
            configured_by_platform_admin_id
        )
        SELECT tenant_id, secret_kind, reference, version, configured_at,
               configured_by_platform_admin_id
        FROM tenant_secret_references_legacy
        """
    )
    database.execute("DROP TABLE tenant_secret_references_legacy")


def _upgrade_runtime_deployments(database: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in database.execute("PRAGMA table_info(tenant_runtime_deployments)").fetchall()
    }
    if columns and "published_hosts_json" not in columns:
        database.execute(
            "ALTER TABLE tenant_runtime_deployments ADD COLUMN published_hosts_json TEXT"
        )


def initialize(path: str | Path) -> None:
    with _LOCK:
        database = connect(path)
        try:
            database.execute("PRAGMA journal_mode = WAL")
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS control_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS platform_tenants (
                    id TEXT PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    owner_telegram_id INTEGER NOT NULL,
                    plan TEXT NOT NULL CHECK (plan IN ('start', 'business', 'pro')),
                    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN (
                        'draft', 'provisioning', 'awaiting_owner_claim', 'active',
                        'suspended', 'migration_failed', 'deleting', 'deleted'
                    )),
                    canonical_host TEXT NOT NULL UNIQUE,
                    database_path TEXT NOT NULL UNIQUE,
                    media_root TEXT NOT NULL UNIQUE,
                    backup_root TEXT NOT NULL UNIQUE,
                    runtime_generation INTEGER NOT NULL DEFAULT 1,
                    entitlement_version INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS tenant_domains (
                    host TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    verification_state TEXT NOT NULL CHECK (verification_state IN (
                        'pending', 'verified', 'disabled'
                    )),
                    is_canonical INTEGER NOT NULL DEFAULT 0 CHECK (is_canonical IN (0, 1)),
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    verified_at TIMESTAMP,
                    FOREIGN KEY (tenant_id) REFERENCES platform_tenants (id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS tenant_entitlement_overrides (
                    tenant_id TEXT PRIMARY KEY,
                    feature_overrides_json TEXT NOT NULL DEFAULT '{}',
                    limit_overrides_json TEXT NOT NULL DEFAULT '{}',
                    updated_by_platform_admin_id INTEGER NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tenant_id) REFERENCES platform_tenants (id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS tenant_secret_references (
                    tenant_id TEXT NOT NULL,
                    secret_kind TEXT NOT NULL CHECK (secret_kind IN (
                        'telegram_bot_token', 'telegram_webhook_secret', 'bot_proxy_url',
                        'yookassa_credentials', 'delivery_encryption_keys'
                    )),
                    reference TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '',
                    configured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    configured_by_platform_admin_id INTEGER NOT NULL,
                    PRIMARY KEY (tenant_id, secret_kind),
                    FOREIGN KEY (tenant_id) REFERENCES platform_tenants (id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS tenant_provisioning_jobs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    operation TEXT NOT NULL CHECK (operation IN ('provision', 'migrate', 'redeploy', 'backup_restore')),
                    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
                    outcome_json TEXT NOT NULL DEFAULT '{}',
                    created_by_platform_admin_id INTEGER NOT NULL,
                    claimed_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tenant_id) REFERENCES platform_tenants (id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS tenant_secret_envelopes (
                    tenant_id TEXT NOT NULL,
                    secret_kind TEXT NOT NULL CHECK (secret_kind IN (
                        'telegram_bot_token', 'telegram_webhook_secret', 'bot_proxy_url'
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
                    PRIMARY KEY (tenant_id, secret_kind),
                    FOREIGN KEY (tenant_id) REFERENCES platform_tenants (id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS tenant_runtime_deployments (
                    tenant_id TEXT PRIMARY KEY,
                    system_user TEXT NOT NULL UNIQUE,
                    system_uid INTEGER,
                    desired_generation INTEGER NOT NULL,
                    applied_generation INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL CHECK (state IN (
                        'pending', 'provisioning', 'running', 'failed', 'stopped'
                    )),
                    last_stage TEXT NOT NULL DEFAULT '',
                    last_error_type TEXT NOT NULL DEFAULT '',
                    published_hosts_json TEXT,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tenant_id) REFERENCES platform_tenants (id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS tenant_deployment_jobs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    operation TEXT NOT NULL CHECK (operation IN ('provision', 'activate', 'redeploy')),
                    desired_generation INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
                    outcome_json TEXT NOT NULL DEFAULT '{}',
                    created_by_platform_admin_id INTEGER NOT NULL,
                    claimed_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tenant_id) REFERENCES platform_tenants (id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS platform_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_telegram_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    tenant_id TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tenant_id) REFERENCES platform_tenants (id) ON DELETE RESTRICT
                );
                CREATE TRIGGER IF NOT EXISTS platform_audit_events_no_update
                BEFORE UPDATE ON platform_audit_events
                BEGIN SELECT RAISE(ABORT, 'platform audit events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS platform_audit_events_no_delete
                BEFORE DELETE ON platform_audit_events
                BEGIN SELECT RAISE(ABORT, 'platform audit events are immutable'); END;
                CREATE INDEX IF NOT EXISTS idx_tenant_domains_tenant ON tenant_domains (tenant_id, verification_state);
                CREATE INDEX IF NOT EXISTS idx_tenant_jobs_state ON tenant_provisioning_jobs (state, created_at);
                CREATE INDEX IF NOT EXISTS idx_tenant_deployment_jobs_state ON tenant_deployment_jobs (state, created_at);
                """
            )
            database.execute("BEGIN IMMEDIATE")
            _upgrade_tenant_secret_envelopes(database)
            _upgrade_tenant_secret_references(database)
            _upgrade_runtime_deployments(database)
            database.execute(
                "INSERT OR IGNORE INTO control_schema_migrations (version) VALUES (?)",
                (_SCHEMA_VERSION,),
            )
            database.commit()
        except Exception:
            database.rollback()
            raise
        finally:
            database.close()
