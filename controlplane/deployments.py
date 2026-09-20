from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from controlplane.schema import connect


_OPERATIONS = frozenset({"provision", "activate", "redeploy", "teardown"})
_RECONCILABLE_STATES = frozenset({"awaiting_owner_claim", "active"})
_SAFE_STAGES = frozenset({
    "identity_allocated",
    "storage_prepared",
    "schema_initialized",
    "credentials_materialized",
    "unit_started",
    "health_checked",
    "routing_published",
    "teardown_route",
    "teardown_unit",
    "teardown_material",
    "teardown_completed",
})


@dataclass(frozen=True)
class DeploymentFailure(Exception):
    stage: str
    error_type: str


@dataclass(frozen=True)
class DeploymentJob:
    id: str
    tenant_id: str
    operation: str
    desired_generation: int
    actor_telegram_id: int = 0


def deployment_status(
    control_database_path: str | Path, tenant_id: str
) -> dict[str, object] | None:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        row = database.execute(
            """
            SELECT system_user, desired_generation, applied_generation, state,
                   last_stage, last_error_type, updated_at
            FROM tenant_runtime_deployments WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "system_user": row["system_user"],
            "desired_generation": row["desired_generation"],
            "applied_generation": row["applied_generation"],
            "state": row["state"],
            "last_stage": row["last_stage"],
            "last_error_type": row["last_error_type"],
            "updated_at": row["updated_at"],
        }
    finally:
        database.close()


def published_route_hosts(
    control_database_path: str | Path, tenant_id: str
) -> list[str] | None:
    database = connect(control_database_path)
    try:
        row = database.execute(
            "SELECT published_hosts_json FROM tenant_runtime_deployments WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
    finally:
        database.close()
    if row is None or row[0] is None:
        return None
    try:
        hosts = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("published tenant hosts are invalid") from exc
    if not isinstance(hosts, list) or not hosts or any(
        not isinstance(host, str) for host in hosts
    ):
        raise ValueError("published tenant hosts are invalid")
    return hosts


def tenant_system_user(tenant_id: str) -> str:

    try:
        parsed = uuid.UUID(tenant_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid tenant id") from exc
    return f"tenant-{parsed.hex[:16]}"


def _audit(
    database: sqlite3.Connection,
    *,
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


def _upsert_runtime_deployment(
    database: sqlite3.Connection,
    *,
    tenant_id: str,
    generation: int,
    state: str,
) -> None:
    database.execute(
        """
        INSERT INTO tenant_runtime_deployments (
            tenant_id, system_user, desired_generation, state, updated_at
        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(tenant_id) DO UPDATE SET
            desired_generation = MAX(tenant_runtime_deployments.desired_generation,
                                     excluded.desired_generation),
            state = excluded.state,
            last_stage = CASE WHEN excluded.state = 'pending' THEN ''
                              ELSE tenant_runtime_deployments.last_stage END,
            last_error_type = CASE WHEN excluded.state = 'pending' THEN ''
                                   ELSE tenant_runtime_deployments.last_error_type END,
            updated_at = CURRENT_TIMESTAMP
        """,
        (tenant_id, tenant_system_user(tenant_id), generation, state),
    )


def queue_runtime_reconciliation(
    database: sqlite3.Connection,
    *,
    tenant: sqlite3.Row,
    actor_telegram_id: int,
) -> DeploymentJob | None:
    """Coalesce a managed runtime update in the caller's transaction."""
    tenant_id = tenant["id"]
    generation = tenant["runtime_generation"]
    live = database.execute(
        """
        SELECT id, operation, state FROM tenant_deployment_jobs
        WHERE tenant_id = ? AND state IN ('pending', 'running')
        ORDER BY created_at, id
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    if tenant["lifecycle_state"] not in _RECONCILABLE_STATES:
        if (
            tenant["lifecycle_state"] not in {"draft", "migration_failed"}
            or live is None
            or live["operation"] != "provision"
        ):
            return None
        if live["state"] == "pending":
            database.execute(
                """
                UPDATE tenant_deployment_jobs
                SET desired_generation = ?
                WHERE id = ? AND state = 'pending'
                """,
                (generation, live["id"]),
            )
        database.execute(
            """
            UPDATE tenant_runtime_deployments
            SET desired_generation = MAX(desired_generation, ?),
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = ?
            """,
            (generation, tenant_id),
        )
        return None
    if live is not None:
        if live["state"] == "pending":
            database.execute(
                """
                UPDATE tenant_deployment_jobs
                SET desired_generation = ?
                WHERE id = ? AND state = 'pending'
                """,
                (generation, live["id"]),
            )
        database.execute(
            """
            UPDATE tenant_runtime_deployments
            SET desired_generation = MAX(desired_generation, ?),
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = ?
            """,
            (generation, tenant_id),
        )
        return None

    _upsert_runtime_deployment(
        database, tenant_id=tenant_id, generation=generation, state="pending"
    )
    job = DeploymentJob(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        operation="redeploy",
        desired_generation=generation,
        actor_telegram_id=actor_telegram_id,
    )
    database.execute(
        """
        INSERT INTO tenant_deployment_jobs (
            id, tenant_id, operation, desired_generation, state,
            created_by_platform_admin_id
        ) VALUES (?, ?, 'redeploy', ?, 'pending', ?)
        """,
        (job.id, tenant_id, generation, actor_telegram_id),
    )
    _audit(
        database,
        actor_telegram_id=actor_telegram_id,
        action="tenant.deployment.reconciliation_queued",
        tenant_id=tenant_id,
        details={"generation": generation},
    )
    return job


def queue_teardown_job(
    database: sqlite3.Connection,
    *,
    tenant: sqlite3.Row,
    actor_telegram_id: int,
) -> DeploymentJob:
    if tenant["lifecycle_state"] != "deleting" or tenant["tenant_kind"] != "managed":
        raise ValueError("tenant is not ready for managed teardown")
    existing = database.execute(
        """
        SELECT id, tenant_id, operation, desired_generation, created_by_platform_admin_id
        FROM tenant_deployment_jobs
        WHERE tenant_id = ? AND operation = 'teardown'
          AND state IN ('pending', 'running')
        ORDER BY created_at, id
        LIMIT 1
        """,
        (tenant["id"],),
    ).fetchone()
    if existing is not None:
        return DeploymentJob(
            id=existing["id"],
            tenant_id=existing["tenant_id"],
            operation=existing["operation"],
            desired_generation=existing["desired_generation"],
            actor_telegram_id=existing["created_by_platform_admin_id"],
        )
    database.execute(
        """
        UPDATE tenant_deployment_jobs
        SET state = 'failed',
            outcome_json = ?, completed_at = CURRENT_TIMESTAMP
        WHERE tenant_id = ? AND state = 'pending' AND operation != 'teardown'
        """,
        (json.dumps({"error_type": "DeletionRequested"}, separators=(",", ":")), tenant["id"]),
    )
    _upsert_runtime_deployment(
        database,
        tenant_id=tenant["id"],
        generation=tenant["runtime_generation"],
        state="pending",
    )
    job = DeploymentJob(
        id=str(uuid.uuid4()),
        tenant_id=tenant["id"],
        operation="teardown",
        desired_generation=tenant["runtime_generation"],
        actor_telegram_id=actor_telegram_id,
    )
    database.execute(
        """
        INSERT INTO tenant_deployment_jobs (
            id, tenant_id, operation, desired_generation, state,
            created_by_platform_admin_id
        ) VALUES (?, ?, 'teardown', ?, 'pending', ?)
        """,
        (job.id, job.tenant_id, job.desired_generation, actor_telegram_id),
    )
    _audit(
        database,
        actor_telegram_id=actor_telegram_id,
        action="tenant.teardown.queued",
        tenant_id=tenant["id"],
        details={"generation": tenant["runtime_generation"]},
    )
    return job


def request_deployment(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    operation: str,
    actor_telegram_id: int,
) -> DeploymentJob:
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise ValueError("unsupported deployment operation")
    if operation == "teardown":
        raise ValueError("managed teardown requires the tenant deletion endpoint")
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        tenant = database.execute(
            "SELECT runtime_generation, lifecycle_state FROM platform_tenants WHERE id = ?",
            (tenant_id,),
        ).fetchone()
        if tenant is None:
            raise ValueError("tenant not found")
        lifecycle_state = tenant["lifecycle_state"]
        if lifecycle_state in {"deleting", "deleted"}:
            raise ValueError("tenant has been deleted")
        if operation == "provision" and lifecycle_state not in {"draft", "migration_failed"}:
            raise ValueError("tenant is not ready for managed provisioning")
        if operation == "provision":
            configured = {
                row[0]
                for row in database.execute(
                    """
                    SELECT secret_kind FROM tenant_secret_envelopes
                    WHERE tenant_id = ?
                    """,
                    (tenant_id,),
                ).fetchall()
            }
            if not {"telegram_bot_token", "telegram_webhook_secret"}.issubset(configured):
                raise ValueError("managed tenant secrets are unavailable")
        if operation == "activate" and lifecycle_state != "awaiting_owner_claim":
            raise ValueError("tenant is not awaiting owner claim")
        if operation == "redeploy" and lifecycle_state not in _RECONCILABLE_STATES:
            raise ValueError("tenant is not ready for managed redeployment")
        pending = database.execute(
            """
            SELECT 1 FROM tenant_deployment_jobs
            WHERE tenant_id = ? AND state IN ('pending', 'running')
            LIMIT 1
            """,
            (tenant_id,),
        ).fetchone()
        if pending is not None:
            raise ValueError("tenant deployment is already pending")
        generation = tenant["runtime_generation"]
        _upsert_runtime_deployment(
            database, tenant_id=tenant_id, generation=generation, state="pending"
        )
        job = DeploymentJob(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            operation=operation,
            desired_generation=generation,
            actor_telegram_id=actor_telegram_id,
        )
        database.execute(
            """
            INSERT INTO tenant_deployment_jobs (
                id, tenant_id, operation, desired_generation, state,
                created_by_platform_admin_id
            ) VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (job.id, job.tenant_id, job.operation, job.desired_generation, actor_telegram_id),
        )
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.deployment.requested",
            tenant_id=tenant_id,
            details={"operation": operation, "generation": generation},
        )
        database.commit()
        return job
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def claim_next_deployment_job(
    control_database_path: str | Path,
) -> DeploymentJob | None:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            """
            SELECT id, tenant_id, operation, desired_generation, created_by_platform_admin_id
            FROM tenant_deployment_jobs
            WHERE state = 'pending'
            ORDER BY CASE WHEN operation = 'teardown' THEN 0 ELSE 1 END,
                     created_at, id
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            database.commit()
            return None
        updated = database.execute(
            """
            UPDATE tenant_deployment_jobs
            SET state = 'running', claimed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND state = 'pending'
            """,
            (row["id"],),
        )
        if updated.rowcount != 1:
            database.rollback()
            return None
        database.execute(
            """
            UPDATE tenant_runtime_deployments
            SET state = CASE WHEN ? = 'teardown' THEN 'pending' ELSE 'provisioning' END,
                updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = ?
            """,
            (row["operation"], row["tenant_id"]),
        )
        database.commit()
        return DeploymentJob(
            id=row["id"],
            tenant_id=row["tenant_id"],
            operation=row["operation"],
            desired_generation=row["desired_generation"],
            actor_telegram_id=row["created_by_platform_admin_id"],
        )
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def _recovery_operation(tenant: sqlite3.Row, operation: str) -> str | None:
    lifecycle_state = tenant["lifecycle_state"]
    if lifecycle_state == "deleting":
        return "teardown"
    if lifecycle_state in {"draft", "migration_failed"}:
        return "provision" if operation == "provision" else None
    if lifecycle_state == "awaiting_owner_claim":
        return "activate" if operation == "activate" else "redeploy"
    if lifecycle_state == "active":
        return "redeploy"
    return None


def recover_abandoned_deployment_jobs(control_database_path: str | Path) -> int:
    """Recover jobs only after the controller singleton lock has been acquired."""
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    recovered = 0
    try:
        database.execute("BEGIN IMMEDIATE")
        jobs = database.execute(
            """
            SELECT id, tenant_id, operation, created_by_platform_admin_id
            FROM tenant_deployment_jobs WHERE state = 'running'
            """
        ).fetchall()
        for job in jobs:
            database.execute(
                """
                UPDATE tenant_deployment_jobs
                SET state = 'failed', outcome_json = ?, completed_at = CURRENT_TIMESTAMP
                WHERE id = ? AND state = 'running'
                """,
                (
                    json.dumps(
                        {"stage": "identity_allocated", "error_type": "ControllerRestarted"},
                        separators=(",", ":"),
                    ),
                    job["id"],
                ),
            )
            tenant = database.execute(
                "SELECT * FROM platform_tenants WHERE id = ?", (job["tenant_id"],)
            ).fetchone()
            if tenant is None:
                continue
            operation = _recovery_operation(tenant, job["operation"])
            if operation is None:
                database.execute(
                    """
                    UPDATE tenant_runtime_deployments
                    SET state = 'failed', last_stage = 'identity_allocated',
                        last_error_type = 'ControllerRestarted', updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = ?
                    """,
                    (job["tenant_id"],),
                )
                continue
            generation = tenant["runtime_generation"]
            _upsert_runtime_deployment(
                database, tenant_id=tenant["id"], generation=generation, state="pending"
            )
            retry = DeploymentJob(
                id=str(uuid.uuid4()),
                tenant_id=tenant["id"],
                operation=operation,
                desired_generation=generation,
                actor_telegram_id=job["created_by_platform_admin_id"],
            )
            database.execute(
                """
                INSERT INTO tenant_deployment_jobs (
                    id, tenant_id, operation, desired_generation, state,
                    created_by_platform_admin_id
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (
                    retry.id,
                    retry.tenant_id,
                    retry.operation,
                    retry.desired_generation,
                    retry.actor_telegram_id,
                ),
            )
            recovered += 1
        database.commit()
        return recovered
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def complete_teardown_job(
    control_database_path: str | Path,
    *,
    job: DeploymentJob,
    succeeded: bool,
    stage: str,
    error_type: str = "",
) -> bool:
    if job.operation != "teardown" or stage not in {
        "teardown_route",
        "teardown_unit",
        "teardown_material",
        "teardown_completed",
    }:
        raise ValueError("invalid teardown completion")
    if error_type and not error_type.isidentifier():
        raise ValueError("invalid deployment error type")
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        tenant = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (job.tenant_id,)
        ).fetchone()
        runtime = database.execute(
            "SELECT * FROM tenant_runtime_deployments WHERE tenant_id = ?",
            (job.tenant_id,),
        ).fetchone()
        if tenant is None or runtime is None:
            database.rollback()
            return False
        if succeeded and tenant["lifecycle_state"] == "deleting":
            if tenant["tenant_kind"] != "managed" or tenant["runtime_generation"] != job.desired_generation:
                succeeded = False
                error_type = "GenerationSuperseded"
        elif succeeded and tenant["lifecycle_state"] != "deleted":
            succeeded = False
            error_type = "GenerationSuperseded"
        outcome = (
            {"stage": stage}
            if succeeded
            else {"stage": stage, "error_type": error_type or "TeardownFailed"}
        )
        cursor = database.execute(
            """
            UPDATE tenant_deployment_jobs
            SET state = ?, outcome_json = ?, completed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND state = 'running'
            """,
            (
                "succeeded" if succeeded else "failed",
                json.dumps(outcome, separators=(",", ":")),
                job.id,
            ),
        )
        if cursor.rowcount != 1:
            database.rollback()
            return False
        if succeeded:
            if tenant["lifecycle_state"] == "deleting":
                database.execute(
                    """
                    UPDATE platform_tenants
                    SET lifecycle_state = 'deleted', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND lifecycle_state = 'deleting'
                    """,
                    (job.tenant_id,),
                )
                database.execute(
                    "DELETE FROM tenant_secret_envelopes WHERE tenant_id = ?",
                    (job.tenant_id,),
                )
                database.execute(
                    "DELETE FROM tenant_secret_references WHERE tenant_id = ?",
                    (job.tenant_id,),
                )
                _audit(
                    database,
                    actor_telegram_id=job.actor_telegram_id,
                    action="tenant.teardown.completed",
                    tenant_id=job.tenant_id,
                    details={"generation": job.desired_generation},
                )
            database.execute(
                """
                UPDATE tenant_runtime_deployments
                SET state = 'stopped', applied_generation = MAX(applied_generation, ?),
                    last_stage = ?, last_error_type = '', published_hosts_json = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = ?
                """,
                (job.desired_generation, stage, job.tenant_id),
            )
        else:
            database.execute(
                """
                UPDATE tenant_runtime_deployments
                SET state = 'failed', last_stage = ?, last_error_type = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = ?
                """,
                (stage, error_type or "TeardownFailed", job.tenant_id),
            )
        database.commit()
        return True
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def complete_deployment_job(
    control_database_path: str | Path,
    *,
    job: DeploymentJob,
    succeeded: bool,
    stage: str,
    error_type: str = "",
    applied_generation: int | None = None,
    applied_hosts: list[str] | None = None,
) -> bool:
    if applied_hosts is not None and (
        not applied_hosts or any(not isinstance(host, str) for host in applied_hosts)
    ):
        raise ValueError("invalid applied tenant hosts")
    if stage not in _SAFE_STAGES:
        raise ValueError("invalid deployment stage")
    if error_type and not error_type.isidentifier():
        raise ValueError("invalid deployment error type")
    if applied_generation is not None and applied_generation <= 0:
        raise ValueError("invalid applied generation")
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        tenant = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (job.tenant_id,)
        ).fetchone()
        runtime = database.execute(
            "SELECT desired_generation, applied_generation FROM tenant_runtime_deployments WHERE tenant_id = ?",
            (job.tenant_id,),
        ).fetchone()
        if tenant is None or runtime is None:
            database.rollback()
            return False
        resolved_generation = applied_generation or job.desired_generation
        activation_applied_current_generation = (
            succeeded
            and job.operation == "activate"
            and tenant["lifecycle_state"] == "active"
            and tenant["runtime_generation"] == resolved_generation
            and runtime["desired_generation"] <= resolved_generation
        )
        generation_is_current = (
            succeeded
            and tenant["runtime_generation"] == resolved_generation
            and (
                runtime["desired_generation"] == resolved_generation
                or activation_applied_current_generation
            )
        )
        superseded = succeeded and not generation_is_current
        outcome = (
            {"stage": stage}
            if generation_is_current
            else {
                "stage": stage,
                "error_type": "GenerationSuperseded" if superseded else error_type,
            }
        )
        terminal_state = "succeeded" if generation_is_current else "failed"
        cursor = database.execute(
            """
            UPDATE tenant_deployment_jobs
            SET state = ?, outcome_json = ?, completed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND state = 'running'
            """,
            (terminal_state, json.dumps(outcome, separators=(",", ":")), job.id),
        )
        if cursor.rowcount != 1:
            database.rollback()
            return False
        newer_generation_exists = (
            tenant["runtime_generation"] > job.desired_generation
            or runtime["desired_generation"] > job.desired_generation
        )
        if generation_is_current:
            database.execute(
                """
                UPDATE tenant_runtime_deployments
                SET state = 'running', desired_generation = ?, applied_generation = ?,
                    last_stage = ?, last_error_type = '',
                    published_hosts_json = COALESCE(?, published_hosts_json),
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = ?
                """,
                (
                    resolved_generation,
                    resolved_generation,
                    stage,
                    json.dumps(sorted(set(applied_hosts))) if applied_hosts is not None else None,
                    job.tenant_id,
                ),
            )
        elif newer_generation_exists and tenant["lifecycle_state"] in _RECONCILABLE_STATES:
            database.execute(
                """
                UPDATE tenant_runtime_deployments
                SET desired_generation = MAX(desired_generation, ?),
                    applied_generation = MAX(applied_generation, ?), state = 'pending',
                    last_stage = ?, last_error_type = 'GenerationSuperseded',
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = ?
                """,
                (tenant["runtime_generation"], resolved_generation if succeeded else 0, stage, job.tenant_id),
            )
            latest = database.execute(
                "SELECT * FROM platform_tenants WHERE id = ?", (job.tenant_id,)
            ).fetchone()
            queue_runtime_reconciliation(
                database,
                tenant=latest,
                actor_telegram_id=job.actor_telegram_id,
            )
        else:
            database.execute(
                """
                UPDATE tenant_runtime_deployments
                SET state = 'failed', last_stage = ?, last_error_type = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = ?
                """,
                (stage, "GenerationSuperseded" if superseded else error_type, job.tenant_id),
            )
        database.commit()
        return True
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
