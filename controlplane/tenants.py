from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from controlplane.deployments import queue_runtime_reconciliation
from controlplane.plan_policy import Entitlements, Plan, effective_entitlements, parse_plan
from proxy_url import normalize_authenticated_http_proxy
from controlplane.schema import connect, resolve_path
from controlplane.secret_envelopes import (
    SecretEnvelope,
    SecretEnvelopeError,
    UnixSocketSealer,
)
from controlplane.secret_store import SecretResolutionError, validate_secret_reference


_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])?\Z")
_HOST_PATTERN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
_SECRET_KINDS = {
    "telegram_bot_token",
    "telegram_webhook_secret",
    "bot_proxy_url",
    "yookassa_credentials",
    "delivery_encryption_keys",
}
_OWNER_CLAIM_VERDICTS = frozenset({"claimed", "missing", "owner_mismatch"})


@dataclass(frozen=True)
class Tenant:
    id: str
    slug: str
    display_name: str
    owner_telegram_id: int
    plan: Plan
    lifecycle_state: str
    canonical_host: str
    database_path: Path
    media_root: Path
    backup_root: Path
    runtime_generation: int
    entitlement_version: int


@dataclass(frozen=True)
class DomainVerificationUpdate:
    host: str
    verification_state: str
    changed: bool
    reconciliation_queued: bool


def normalize_host(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("host must be a string")
    normalized = value.strip().lower().rstrip(".")
    if ":" in normalized or not _HOST_PATTERN.fullmatch(normalized):
        raise ValueError("invalid host")
    return normalized


def normalize_slug(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("slug must be a string")
    normalized = value.strip().lower()
    if not _SLUG_PATTERN.fullmatch(normalized):
        raise ValueError("invalid tenant slug")
    return normalized


def _tenant_path(root: str | Path, tenant_id: str, name: str) -> Path:
    resolved_root = resolve_path(root)
    target = (resolved_root / tenant_id / name).resolve()
    if target.parent != (resolved_root / tenant_id).resolve():
        raise ValueError("invalid tenant storage path")
    return target


def _audit(
    database: sqlite3.Connection,
    *,
    actor_telegram_id: int,
    action: str,
    tenant_id: str | None,
    details: dict[str, object] | None = None,
) -> None:
    encoded = json.dumps(details or {}, separators=(",", ":"), ensure_ascii=False)
    database.execute(
        """
        INSERT INTO platform_audit_events (actor_telegram_id, action, tenant_id, details_json)
        VALUES (?, ?, ?, ?)
        """,
        (actor_telegram_id, action, tenant_id, encoded),
    )


def _tenant(row: sqlite3.Row) -> Tenant:
    return Tenant(
        id=row["id"],
        slug=row["slug"],
        display_name=row["display_name"],
        owner_telegram_id=row["owner_telegram_id"],
        plan=parse_plan(row["plan"]),
        lifecycle_state=row["lifecycle_state"],
        canonical_host=row["canonical_host"],
        database_path=Path(row["database_path"]),
        media_root=Path(row["media_root"]),
        backup_root=Path(row["backup_root"]),
        runtime_generation=row["runtime_generation"],
        entitlement_version=row["entitlement_version"],
    )


def create_tenant(
    control_database_path: str | Path,
    *,
    display_name: str,
    slug: str,
    owner_telegram_id: int,
    plan: Plan | str,
    tenant_data_root: str | Path,
    tenant_backup_root: str | Path,
    tenant_base_domain: str,
    actor_telegram_id: int,
) -> Tenant:
    if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 120:
        raise ValueError("invalid tenant display name")
    if not isinstance(owner_telegram_id, int) or isinstance(owner_telegram_id, bool) or owner_telegram_id <= 0:
        raise ValueError("invalid tenant owner")
    normalized_slug = normalize_slug(slug)
    base_domain = normalize_host(tenant_base_domain)
    tenant_id = str(uuid.uuid4())
    canonical_host = f"{normalized_slug}.{base_domain}"
    database_path = _tenant_path(tenant_data_root, tenant_id, "app.sqlite")
    media_root = _tenant_path(tenant_data_root, tenant_id, "media")
    backup_root = _tenant_path(tenant_backup_root, tenant_id, "backups")
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            """
            INSERT INTO platform_tenants (
                id, slug, display_name, owner_telegram_id, plan, lifecycle_state,
                canonical_host, database_path, media_root, backup_root
            ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)
            """,
            (
                tenant_id,
                normalized_slug,
                display_name.strip(),
                owner_telegram_id,
                parse_plan(plan).value,
                canonical_host,
                str(database_path),
                str(media_root),
                str(backup_root),
            ),
        )
        database.execute(
            """
            INSERT INTO tenant_domains (host, tenant_id, verification_state, is_canonical, verified_at)
            VALUES (?, ?, 'verified', 1, CURRENT_TIMESTAMP)
            """,
            (canonical_host, tenant_id),
        )
        database.execute(
            """
            INSERT INTO tenant_entitlement_overrides (
                tenant_id, updated_by_platform_admin_id
            ) VALUES (?, ?)
            """,
            (tenant_id, actor_telegram_id),
        )
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.created",
            tenant_id=tenant_id,
            details={"plan": parse_plan(plan).value, "slug": normalized_slug},
        )
        row = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        database.commit()
        return _tenant(row)
    except sqlite3.IntegrityError as exc:
        database.rollback()
        raise ValueError("tenant slug or host already exists") from exc
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def get_tenant(control_database_path: str | Path, tenant_id: str) -> Tenant | None:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        row = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        return _tenant(row) if row else None
    finally:
        database.close()


def list_tenants(control_database_path: str | Path) -> list[Tenant]:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT * FROM platform_tenants
            WHERE lifecycle_state != 'deleted'
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
        return [_tenant(row) for row in rows]
    finally:
        database.close()


def tenant_domains(control_database_path: str | Path, tenant_id: str) -> list[dict[str, object]]:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT host, verification_state, is_canonical, created_at, verified_at
            FROM tenant_domains
            WHERE tenant_id = ?
            ORDER BY is_canonical DESC, host ASC
            """,
            (tenant_id,),
        ).fetchall()
        return [
            {
                "host": row["host"],
                "verification_state": row["verification_state"],
                "is_canonical": bool(row["is_canonical"]),
                "created_at": row["created_at"],
                "verified_at": row["verified_at"],
            }
            for row in rows
        ]
    finally:
        database.close()


def configured_secret_kinds(control_database_path: str | Path, tenant_id: str) -> list[str]:
    database = connect(control_database_path)
    try:
        envelope_rows = database.execute(
            """
            SELECT secret_kind FROM tenant_secret_envelopes
            WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchall()
        configured = {row[0] for row in envelope_rows}
        rows = database.execute(
            """
            SELECT secret_kind, reference FROM tenant_secret_references
            WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchall()
        for secret_kind, reference in rows:
            try:
                validate_secret_reference(reference)
            except SecretResolutionError:
                continue
            configured.add(secret_kind)
        return sorted(configured)
    finally:
        database.close()


def sealed_secret_kinds(control_database_path: str | Path, tenant_id: str) -> list[str]:
    database = connect(control_database_path)
    try:
        rows = database.execute(
            """
            SELECT secret_kind FROM tenant_secret_envelopes
            WHERE tenant_id = ?
            ORDER BY secret_kind
            """,
            (tenant_id,),
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        database.close()

def set_secret_envelopes(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    values: dict[str, object],
    actor_telegram_id: int,
    sealer=None,
    clear_bot_proxy: bool = False,
    managed_reconciliation: bool = False,
) -> Tenant:
    supported_kinds = {
        "telegram_bot_token",
        "telegram_webhook_secret",
        "bot_proxy_url",
    }
    if (
        not isinstance(clear_bot_proxy, bool)
        or (not values and not clear_bot_proxy)
        or not set(values).issubset(supported_kinds)
        or (clear_bot_proxy and "bot_proxy_url" in values)
    ):
        raise ValueError("unsupported tenant secret kind")
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ValueError("invalid tenant secret value")
    normalized_values = dict(values)
    if "bot_proxy_url" in normalized_values:
        normalized_values["bot_proxy_url"] = normalize_authenticated_http_proxy(
            normalized_values["bot_proxy_url"], setting_name="bot_proxy_url"
        )

    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        snapshot = _ensure_mutable_tenant(database, tenant_id)
        generation = snapshot["runtime_generation"] + 1
        expected_lifecycle_state = snapshot["lifecycle_state"]
    finally:
        database.close()

    socket_path = os.getenv("PLATFORM_SEALER_SOCKET", "").strip()
    if normalized_values and not sealer and not socket_path:
        raise ValueError("tenant secret sealer is unavailable")
    active_sealer = sealer or UnixSocketSealer(socket_path)
    try:
        envelopes = {
            secret_kind: active_sealer.seal(tenant_id, secret_kind, generation, value)
            for secret_kind, value in normalized_values.items()
        }
    except (SecretEnvelopeError, OSError) as exc:
        raise ValueError("tenant secret sealer is unavailable") from exc

    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        tenant = _ensure_mutable_tenant(database, tenant_id)
        if (
            tenant["runtime_generation"] + 1 != generation
            or tenant["lifecycle_state"] != expected_lifecycle_state
        ):
            raise ValueError("tenant configuration changed; retry")
        for secret_kind, envelope in envelopes.items():
            database.execute(
                """
                INSERT INTO tenant_secret_envelopes (
                    tenant_id, secret_kind, generation, algorithm, ciphertext,
                    data_nonce, wrapped_key, wrap_nonce, key_version,
                    configured_by_platform_admin_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, secret_kind) DO UPDATE SET
                    generation = excluded.generation,
                    algorithm = excluded.algorithm,
                    ciphertext = excluded.ciphertext,
                    data_nonce = excluded.data_nonce,
                    wrapped_key = excluded.wrapped_key,
                    wrap_nonce = excluded.wrap_nonce,
                    key_version = excluded.key_version,
                    configured_at = CURRENT_TIMESTAMP,
                    configured_by_platform_admin_id = excluded.configured_by_platform_admin_id
                """,
                (
                    tenant_id,
                    secret_kind,
                    generation,
                    envelope.algorithm,
                    envelope.ciphertext,
                    envelope.data_nonce,
                    envelope.wrapped_key,
                    envelope.wrap_nonce,
                    envelope.key_version,
                    actor_telegram_id,
                ),
            )
        if clear_bot_proxy:
            database.execute(
                """
                DELETE FROM tenant_secret_envelopes
                WHERE tenant_id = ? AND secret_kind = 'bot_proxy_url'
                """,
                (tenant_id,),
            )
        database.execute(
            """
            UPDATE platform_tenants
            SET runtime_generation = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (generation, tenant_id),
        )
        row = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if managed_reconciliation:
            queue_runtime_reconciliation(
                database, tenant=row, actor_telegram_id=actor_telegram_id
            )
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.secrets.enveloped",
            tenant_id=tenant_id,
            details={
                "kinds": sorted(envelopes),
                "cleared_bot_proxy": clear_bot_proxy,
                "generation": generation,
            },
        )
        database.commit()
        return _tenant(row)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def entitlement_overrides(
    control_database_path: str | Path, tenant_id: str
) -> dict[str, dict[str, object]]:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        row = database.execute(
            """
            SELECT feature_overrides_json, limit_overrides_json
            FROM tenant_entitlement_overrides WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise ValueError("tenant not found")
        return {
            "feature_overrides": json.loads(row["feature_overrides_json"]),
            "limit_overrides": json.loads(row["limit_overrides_json"]),
        }
    finally:
        database.close()


def _ensure_mutable_tenant(database: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
    row = database.execute(
        "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
    ).fetchone()
    if row is None:
        raise ValueError("tenant not found")
    if row["lifecycle_state"] in {"deleting", "deleted"}:
        raise ValueError("tenant has been deleted")
    return row


def request_custom_domain(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    host: str,
    actor_telegram_id: int,
) -> str:
    normalized_host = normalize_host(host)
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        _ensure_mutable_tenant(database, tenant_id)
        database.execute(
            """
            INSERT INTO tenant_domains (host, tenant_id, verification_state, is_canonical)
            VALUES (?, ?, 'pending', 0)
            """,
            (normalized_host, tenant_id),
        )
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.domain.requested",
            tenant_id=tenant_id,
            details={"host": normalized_host},
        )
        database.commit()
        return normalized_host
    except sqlite3.IntegrityError as exc:
        database.rollback()
        raise ValueError("custom domain is already registered") from exc
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def set_custom_domain_verification(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    host: str,
    verification_state: str,
    actor_telegram_id: int,
    managed_reconciliation: bool = False,
) -> DomainVerificationUpdate:
    if verification_state not in {"verified", "disabled"}:
        raise ValueError("invalid domain verification state")
    normalized_host = normalize_host(host)
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        tenant = _ensure_mutable_tenant(database, tenant_id)
        domain = database.execute(
            """
            SELECT verification_state FROM tenant_domains
            WHERE tenant_id = ? AND host = ? AND is_canonical = 0
            """,
            (tenant_id, normalized_host),
        ).fetchone()
        if domain is None:
            raise ValueError("custom domain not found")
        changed = domain["verification_state"] != verification_state
        reconciliation_queued = False
        if changed:
            database.execute(
                """
                UPDATE tenant_domains
                SET verification_state = ?,
                    verified_at = CASE WHEN ? = 'verified' THEN CURRENT_TIMESTAMP ELSE NULL END
                WHERE tenant_id = ? AND host = ? AND is_canonical = 0
                """,
                (verification_state, verification_state, tenant_id, normalized_host),
            )
            if managed_reconciliation and tenant["lifecycle_state"] == "active":
                database.execute(
                    """
                    UPDATE platform_tenants
                    SET runtime_generation = runtime_generation + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (tenant_id,),
                )
                refreshed = database.execute(
                    "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
                ).fetchone()
                queue_runtime_reconciliation(
                    database, tenant=refreshed, actor_telegram_id=actor_telegram_id
                )
                reconciliation_queued = True
            _audit(
                database,
                actor_telegram_id=actor_telegram_id,
                action="tenant.domain.verification.updated",
                tenant_id=tenant_id,
                details={"host": normalized_host, "state": verification_state},
            )
        database.commit()
        return DomainVerificationUpdate(
            host=normalized_host,
            verification_state=verification_state,
            changed=changed,
            reconciliation_queued=reconciliation_queued,
        )
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def resolve_active_host(control_database_path: str | Path, host: object) -> Tenant | None:
    normalized_host = normalize_host(host)
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        row = database.execute(
            """
            SELECT t.* FROM tenant_domains d
            JOIN platform_tenants t ON t.id = d.tenant_id
            WHERE d.host = ? AND d.verification_state = 'verified' AND t.lifecycle_state = 'active'
            """,
            (normalized_host,),
        ).fetchone()
        return _tenant(row) if row else None
    finally:
        database.close()


def resolve_owner_onboarding_host(
    control_database_path: str | Path, host: object
) -> Tenant | None:
    normalized_host = normalize_host(host)
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        row = database.execute(
            """
            SELECT t.* FROM tenant_domains d
            JOIN platform_tenants t ON t.id = d.tenant_id
            WHERE d.host = ?
              AND d.verification_state = 'verified'
              AND d.is_canonical = 1
              AND t.lifecycle_state = 'awaiting_owner_claim'
            """,
            (normalized_host,),
        ).fetchone()
        return _tenant(row) if row else None
    finally:
        database.close()

def effective_tenant_entitlements(
    control_database_path: str | Path, tenant_id: str
) -> Entitlements:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        row = database.execute(
            """
            SELECT t.plan, e.feature_overrides_json, e.limit_overrides_json
            FROM platform_tenants t
            JOIN tenant_entitlement_overrides e ON e.tenant_id = t.id
            WHERE t.id = ?
            """,
            (tenant_id,),
        ).fetchone()
        if not row:
            raise ValueError("tenant not found")
        return effective_entitlements(
            row["plan"],
            feature_overrides=json.loads(row["feature_overrides_json"]),
            limit_overrides=json.loads(row["limit_overrides_json"]),
        )
    finally:
        database.close()


def update_entitlements(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    feature_overrides: dict[str, object],
    limit_overrides: dict[str, object],
    actor_telegram_id: int,
    managed_reconciliation: bool = False,
) -> Entitlements:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        tenant = _ensure_mutable_tenant(database, tenant_id)
        entitlements = effective_entitlements(
            tenant["plan"],
            feature_overrides=feature_overrides,
            limit_overrides=limit_overrides,
        )
        database.execute(
            """
            UPDATE tenant_entitlement_overrides
            SET feature_overrides_json = ?, limit_overrides_json = ?,
                updated_by_platform_admin_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = ?
            """,
            (
                json.dumps(feature_overrides, separators=(",", ":")),
                json.dumps(limit_overrides, separators=(",", ":")),
                actor_telegram_id,
                tenant_id,
            ),
        )
        database.execute(
            """
            UPDATE platform_tenants
            SET entitlement_version = entitlement_version + 1,
                runtime_generation = runtime_generation + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (tenant_id,),
        )
        refreshed = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if managed_reconciliation:
            queue_runtime_reconciliation(
                database, tenant=refreshed, actor_telegram_id=actor_telegram_id
            )
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.entitlements.updated",
            tenant_id=tenant_id,
            details={"plan": entitlements.plan.value},
        )
        database.commit()
        return entitlements
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def update_plan(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    plan: Plan | str,
    actor_telegram_id: int,
    managed_reconciliation: bool = False,
) -> Tenant:
    target_plan = parse_plan(plan)
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        _ensure_mutable_tenant(database, tenant_id)
        cursor = database.execute(
            """
            UPDATE platform_tenants
            SET plan = ?, entitlement_version = entitlement_version + 1,
                runtime_generation = runtime_generation + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (target_plan.value, tenant_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("tenant not found")
        row = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if managed_reconciliation:
            queue_runtime_reconciliation(
                database, tenant=row, actor_telegram_id=actor_telegram_id
            )
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.plan.updated",
            tenant_id=tenant_id,
            details={"plan": target_plan.value},
        )
        database.commit()
        return _tenant(row)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def update_tenant_configuration(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    plan: Plan | str,
    feature_overrides: dict[str, object],
    limit_overrides: dict[str, object],
    actor_telegram_id: int,
    managed_reconciliation: bool = False,
) -> Tenant:
    target_plan = parse_plan(plan)
    if not isinstance(feature_overrides, dict) or not isinstance(limit_overrides, dict):
        raise ValueError("entitlement overrides must be objects")
    entitlements = effective_entitlements(
        target_plan,
        feature_overrides=feature_overrides,
        limit_overrides=limit_overrides,
    )
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        _ensure_mutable_tenant(database, tenant_id)
        database.execute(
            """
            UPDATE platform_tenants
            SET plan = ?, entitlement_version = entitlement_version + 1,
                runtime_generation = runtime_generation + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (target_plan.value, tenant_id),
        )
        database.execute(
            """
            UPDATE tenant_entitlement_overrides
            SET feature_overrides_json = ?, limit_overrides_json = ?,
                updated_by_platform_admin_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE tenant_id = ?
            """,
            (
                json.dumps(feature_overrides, separators=(",", ":")),
                json.dumps(limit_overrides, separators=(",", ":")),
                actor_telegram_id,
                tenant_id,
            ),
        )
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.configuration.updated",
            tenant_id=tenant_id,
            details={"plan": entitlements.plan.value},
        )
        row = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if managed_reconciliation:
            queue_runtime_reconciliation(
                database, tenant=row, actor_telegram_id=actor_telegram_id
            )
        database.commit()
        return _tenant(row)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def secret_references(
    control_database_path: str | Path, tenant_id: str
) -> dict[str, str]:
    database = connect(control_database_path)
    try:
        rows = database.execute(
            """
            SELECT secret_kind, reference FROM tenant_secret_references
            WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchall()
        return {row[0]: row[1] for row in rows}
    finally:
        database.close()


def set_secret_references(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    references: dict[str, dict[str, object]],
    actor_telegram_id: int,
) -> Tenant:
    if not references:
        raise ValueError("at least one secret reference is required")
    normalized: dict[str, tuple[str, str]] = {}
    for secret_kind, configuration in references.items():
        if secret_kind not in _SECRET_KINDS:
            raise ValueError("unsupported secret kind")
        if not isinstance(configuration, dict) or set(configuration) != {"reference", "version"}:
            raise ValueError("invalid secret reference configuration")
        reference = configuration["reference"]
        version = configuration["version"]
        try:
            validated_reference = validate_secret_reference(reference)
        except SecretResolutionError as exc:
            raise ValueError("invalid secret reference") from exc
        if not isinstance(version, str) or len(version) > 80:
            raise ValueError("invalid secret version")
        normalized[secret_kind] = (validated_reference, version)

    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        _ensure_mutable_tenant(database, tenant_id)
        for secret_kind, (reference, version) in normalized.items():
            database.execute(
                """
                INSERT INTO tenant_secret_references (
                    tenant_id, secret_kind, reference, version, configured_by_platform_admin_id
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, secret_kind) DO UPDATE SET
                    reference = excluded.reference,
                    version = excluded.version,
                    configured_at = CURRENT_TIMESTAMP,
                    configured_by_platform_admin_id = excluded.configured_by_platform_admin_id
                """,
                (tenant_id, secret_kind, reference, version, actor_telegram_id),
            )
        database.execute(
            """
            UPDATE platform_tenants
            SET runtime_generation = runtime_generation + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (tenant_id,),
        )
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.secret_references.updated",
            tenant_id=tenant_id,
            details={"kinds": sorted(normalized)},
        )
        row = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        database.commit()
        return _tenant(row)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def set_secret_reference(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    secret_kind: str,
    reference: str,
    version: str,
    actor_telegram_id: int,
) -> None:
    set_secret_references(
        control_database_path,
        tenant_id=tenant_id,
        references={secret_kind: {"reference": reference, "version": version}},
        actor_telegram_id=actor_telegram_id,
    )


def complete_managed_provisioning(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    actor_telegram_id: int,
) -> Tenant:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if row is None:
            raise ValueError("tenant not found")
        tenant = _tenant(row)
        if tenant.lifecycle_state not in {"draft", "migration_failed"}:
            raise ValueError("tenant is not ready for managed provisioning")
        cursor = database.execute(
            """
            UPDATE platform_tenants
            SET lifecycle_state = 'awaiting_owner_claim', updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND lifecycle_state IN ('draft', 'migration_failed')
            """,
            (tenant_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("tenant provisioning state changed")
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.managed_provisioning.completed",
            tenant_id=tenant_id,
        )
        completed = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        database.commit()
        return _tenant(completed)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def activate_after_owner_claim(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    actor_telegram_id: int,
    tenant_database_path: str | Path | None = None,
) -> Tenant:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if row is None:
            raise ValueError("tenant not found")
        tenant = _tenant(row)
        if tenant.lifecycle_state != "awaiting_owner_claim":
            raise ValueError("tenant is not awaiting owner claim")
        database_path = (
            tenant.database_path
            if tenant_database_path is None
            else Path(tenant_database_path).resolve()
        )
        if tenant.database_path.resolve() != database_path or not database_path.is_file():
            raise ValueError("tenant database is unavailable")
        tenant_database: sqlite3.Connection | None = None
        try:
            tenant_database = sqlite3.connect(database_path)
            claim = tenant_database.execute(
                "SELECT telegram_user_id FROM tenant_owner_claims WHERE id = 1"
            ).fetchone()
        except sqlite3.Error as error:
            raise ValueError("tenant owner claim is unavailable") from error
        finally:
            if tenant_database is not None:
                tenant_database.close()
        if claim is None:
            raise ValueError("tenant owner has not claimed the store")
        if claim[0] != tenant.owner_telegram_id:
            raise ValueError("tenant owner claim does not match configured owner")
        database.execute(
            """
            UPDATE platform_tenants
            SET lifecycle_state = 'active', runtime_generation = runtime_generation + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND lifecycle_state = 'awaiting_owner_claim'
            """,
            (tenant_id,),
        )
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.activated_after_owner_claim",
            tenant_id=tenant_id,
        )
        activated = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        database.commit()
        return _tenant(activated)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def activate_managed_after_owner_claim(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    actor_telegram_id: int,
    claim_verdict: str,
) -> Tenant:
    if claim_verdict not in _OWNER_CLAIM_VERDICTS:
        raise ValueError("tenant owner claim is unavailable")
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if row is None:
            raise ValueError("tenant not found")
        tenant = _tenant(row)
        if tenant.lifecycle_state != "awaiting_owner_claim":
            raise ValueError("tenant is not awaiting owner claim")
        if claim_verdict == "missing":
            raise ValueError("tenant owner has not claimed the store")
        if claim_verdict == "owner_mismatch":
            raise ValueError("tenant owner claim does not match configured owner")
        database.execute(
            """
            UPDATE platform_tenants
            SET lifecycle_state = 'active', runtime_generation = runtime_generation + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND lifecycle_state = 'awaiting_owner_claim'
            """,
            (tenant_id,),
        )
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.activated_after_owner_claim",
            tenant_id=tenant_id,
        )
        activated = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        database.commit()
        return _tenant(activated)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()

def soft_delete_tenant(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    confirm_slug: str,
    actor_telegram_id: int,
) -> bool:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT slug, lifecycle_state FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if row is None:
            raise ValueError("tenant not found")
        if not isinstance(confirm_slug, str) or confirm_slug != row["slug"]:
            raise ValueError("tenant slug confirmation does not match")
        if row["lifecycle_state"] == "deleted":
            database.commit()
            return False
        if row["lifecycle_state"] == "provisioning":
            raise RuntimeError("tenant provisioning is in progress")
        database.execute(
            """
            UPDATE platform_tenants
            SET lifecycle_state = 'deleted', runtime_generation = runtime_generation + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (tenant_id,),
        )
        database.execute(
            """
            UPDATE tenant_domains
            SET verification_state = 'disabled', verified_at = NULL
            WHERE tenant_id = ?
            """,
            (tenant_id,),
        )
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.deleted",
            tenant_id=tenant_id,
            details={"previous_state": row["lifecycle_state"], "slug": row["slug"]},
        )
        database.commit()
        return True
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def set_lifecycle_state(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    lifecycle_state: str,
    actor_telegram_id: int,
) -> Tenant:
    allowed_states = {
        "active": {"suspended", "deleting"},
        "suspended": {"active", "deleting"},
        "deleting": {"deleted"},
    }
    if lifecycle_state not in {state for states in allowed_states.values() for state in states}:
        raise ValueError("invalid tenant lifecycle state")
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        current = database.execute(
            "SELECT lifecycle_state FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if current is None:
            raise ValueError("tenant not found")
        if lifecycle_state not in allowed_states.get(current["lifecycle_state"], set()):
            raise ValueError("invalid tenant lifecycle transition")
        cursor = database.execute(
            """
            UPDATE platform_tenants
            SET lifecycle_state = ?, runtime_generation = runtime_generation + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (lifecycle_state, tenant_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("tenant not found")
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.lifecycle.updated",
            tenant_id=tenant_id,
            details={"state": lifecycle_state},
        )
        row = database.execute(
            "SELECT * FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        database.commit()
        return _tenant(row)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
