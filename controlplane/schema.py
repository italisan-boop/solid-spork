from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


_SCHEMA_VERSION = 1
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


def initialize(path: str | Path) -> None:
    with _LOCK:
        database = connect(path)
        try:
            database.execute("PRAGMA journal_mode = WAL")
            database.execute("BEGIN IMMEDIATE")
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
                        'telegram_bot_token', 'telegram_webhook_secret',
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
                """
            )
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
