from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from controlplane.schema import connect
from controlplane.secret_store import (
    EnvironmentSecretStore,
    SecretResolutionError,
    SecretStore,
    validate_secret_reference,
)
from controlplane.tenants import Tenant, get_tenant, secret_references


_REQUIRED_SECRETS = {"telegram_bot_token", "telegram_webhook_secret"}


class ProvisioningError(ValueError):
    pass


def _update_job(
    database: sqlite3.Connection,
    job_id: str,
    state: str,
    outcome: dict[str, object],
) -> None:
    database.execute(
        """
        UPDATE tenant_provisioning_jobs
        SET state = ?, outcome_json = ?, completed_at = CASE WHEN ? IN ('succeeded', 'failed')
            THEN CURRENT_TIMESTAMP ELSE completed_at END
        WHERE id = ?
        """,
        (state, json.dumps(outcome, separators=(",", ":")), state, job_id),
    )


def _audit(
    database: sqlite3.Connection,
    actor_telegram_id: int,
    action: str,
    tenant_id: str,
    details: dict[str, object],
) -> None:
    database.execute(
        """
        INSERT INTO platform_audit_events (actor_telegram_id, action, tenant_id, details_json)
        VALUES (?, ?, ?, ?)
        """,
        (
            actor_telegram_id,
            action,
            tenant_id,
            json.dumps(details, separators=(",", ":")),
        ),
    )


def _preflight_secrets(
    control_database_path: str | Path,
    tenant_id: str,
    secret_store: SecretStore,
) -> None:
    references = secret_references(control_database_path, tenant_id)
    missing = sorted(_REQUIRED_SECRETS - set(references))
    if missing:
        raise ProvisioningError("required secret references are missing")
    for secret_kind in _REQUIRED_SECRETS:
        try:
            validate_secret_reference(references[secret_kind])
            secret_store.resolve(references[secret_kind])
        except SecretResolutionError as exc:
            raise ProvisioningError(f"required {secret_kind} is unavailable") from exc
    yookassa_reference = references.get("yookassa_credentials")
    if not yookassa_reference:
        return
    try:
        validate_secret_reference(yookassa_reference)
        credentials = json.loads(secret_store.resolve(yookassa_reference))
    except (SecretResolutionError, json.JSONDecodeError) as exc:
        raise ProvisioningError("configured yookassa_credentials are unavailable") from exc
    if not isinstance(credentials, dict) or not all(
        isinstance(credentials.get(name), str) and credentials[name]
        for name in ("shop_id", "secret_key")
    ):
        raise ProvisioningError("configured yookassa_credentials are invalid")


def provision_tenant(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    actor_telegram_id: int,
    secret_store: SecretStore | None = None,
) -> Tenant:
    tenant = get_tenant(control_database_path, tenant_id)
    if tenant is None:
        raise ProvisioningError("tenant not found")
    if tenant.lifecycle_state not in {"draft", "migration_failed"}:
        raise ProvisioningError("tenant is not ready for provisioning")
    _preflight_secrets(
        control_database_path,
        tenant_id,
        secret_store or EnvironmentSecretStore(),
    )

    job_id = str(uuid.uuid4())
    control = connect(control_database_path)
    try:
        control.execute("BEGIN IMMEDIATE")
        cursor = control.execute(
            """
            UPDATE platform_tenants
            SET lifecycle_state = 'provisioning', updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND lifecycle_state IN ('draft', 'migration_failed')
            """,
            (tenant_id,),
        )
        if cursor.rowcount != 1:
            raise ProvisioningError("tenant provisioning state changed")
        control.execute(
            """
            INSERT INTO tenant_provisioning_jobs (
                id, tenant_id, operation, state, created_by_platform_admin_id, claimed_at
            ) VALUES (?, ?, 'provision', 'running', ?, CURRENT_TIMESTAMP)
            """,
            (job_id, tenant_id, actor_telegram_id),
        )
        _audit(control, actor_telegram_id, "tenant.provision.started", tenant_id, {})
        control.commit()
    except Exception:
        control.rollback()
        raise
    finally:
        control.close()

    try:
        from db.schema import connect as connect_tenant_database
        from db.schema import initialize_database

        tenant.database_path.parent.mkdir(parents=True, exist_ok=True)
        tenant.media_root.mkdir(parents=True, exist_ok=True)
        tenant.backup_root.mkdir(parents=True, exist_ok=True)
        initialize_database(tenant.database_path, seed_catalog=False)
        tenant_database = connect_tenant_database(tenant.database_path)
        try:
            tenant_database.execute("BEGIN IMMEDIATE")
            tenant_database.execute(
                """
                INSERT INTO tenant_runtime_metadata (
                    id, tenant_id, canonical_host, owner_telegram_id
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    canonical_host = excluded.canonical_host,
                    owner_telegram_id = excluded.owner_telegram_id
                """,
                (tenant.id, tenant.canonical_host, tenant.owner_telegram_id),
            )
            tenant_database.execute(
                """
                INSERT INTO storefront_settings (id, store_name)
                VALUES (1, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (tenant.display_name,),
            )
            tenant_database.commit()
        except Exception:
            tenant_database.rollback()
            raise
        finally:
            tenant_database.close()
    except Exception as exc:
        control = connect(control_database_path)
        try:
            control.execute("BEGIN IMMEDIATE")
            control.execute(
                """
                UPDATE platform_tenants
                SET lifecycle_state = 'migration_failed', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (tenant_id,),
            )
            _update_job(control, job_id, "failed", {"reason": type(exc).__name__})
            _audit(
                control,
                actor_telegram_id,
                "tenant.provision.failed",
                tenant_id,
                {"reason": type(exc).__name__},
            )
            control.commit()
        except Exception:
            control.rollback()
            raise
        finally:
            control.close()
        raise ProvisioningError("tenant provisioning failed") from exc

    control = connect(control_database_path)
    control.row_factory = sqlite3.Row
    try:
        control.execute("BEGIN IMMEDIATE")
        control.execute(
            """
            UPDATE platform_tenants
            SET lifecycle_state = 'awaiting_owner_claim', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (tenant_id,),
        )
        _update_job(control, job_id, "succeeded", {"database_initialized": True})
        _audit(control, actor_telegram_id, "tenant.provision.succeeded", tenant_id, {})
        row = control.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        control.commit()
        from controlplane.tenants import _tenant

        return _tenant(row)
    except Exception:
        control.rollback()
        raise
    finally:
        control.close()
