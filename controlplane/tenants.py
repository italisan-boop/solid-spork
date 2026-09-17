from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from controlplane.plan_policy import Entitlements, Plan, effective_entitlements, parse_plan
from controlplane.schema import connect, resolve_path


_SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])?\Z")
_HOST_PATTERN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")
_SECRET_REFERENCE_PATTERN = re.compile(r"[a-z][a-z0-9+.-]{1,31}:[^\s]{1,240}\Z")
_SECRET_KINDS = {
    "telegram_bot_token",
    "telegram_webhook_secret",
    "yookassa_credentials",
    "delivery_encryption_keys",
}


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
            "SELECT * FROM platform_tenants ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [_tenant(row) for row in rows]
    finally:
        database.close()


def request_custom_domain(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    host: str,
    actor_telegram_id: int,
) -> str:
    normalized_host = normalize_host(host)
    database = connect(control_database_path)
    try:
        database.execute("BEGIN IMMEDIATE")
        if not database.execute(
            "SELECT 1 FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone():
            raise ValueError("tenant not found")
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
) -> None:
    if verification_state not in {"verified", "disabled"}:
        raise ValueError("invalid domain verification state")
    normalized_host = normalize_host(host)
    database = connect(control_database_path)
    try:
        database.execute("BEGIN IMMEDIATE")
        cursor = database.execute(
            """
            UPDATE tenant_domains
            SET verification_state = ?,
                verified_at = CASE WHEN ? = 'verified' THEN CURRENT_TIMESTAMP ELSE NULL END
            WHERE tenant_id = ? AND host = ? AND is_canonical = 0
            """,
            (verification_state, verification_state, tenant_id, normalized_host),
        )
        if cursor.rowcount != 1:
            raise ValueError("custom domain not found")
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.domain.verification.updated",
            tenant_id=tenant_id,
            details={"host": normalized_host, "state": verification_state},
        )
        database.commit()
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
) -> Entitlements:
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        tenant = database.execute(
            "SELECT plan FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if not tenant:
            raise ValueError("tenant not found")
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
            SET entitlement_version = entitlement_version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (tenant_id,),
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
) -> Tenant:
    target_plan = parse_plan(plan)
    database = connect(control_database_path)
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
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
        _audit(
            database,
            actor_telegram_id=actor_telegram_id,
            action="tenant.plan.updated",
            tenant_id=tenant_id,
            details={"plan": target_plan.value},
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


def set_secret_reference(
    control_database_path: str | Path,
    *,
    tenant_id: str,
    secret_kind: str,
    reference: str,
    version: str,
    actor_telegram_id: int,
) -> None:
    if secret_kind not in _SECRET_KINDS:
        raise ValueError("unsupported secret kind")
    if not isinstance(reference, str) or not _SECRET_REFERENCE_PATTERN.fullmatch(reference):
        raise ValueError("invalid secret reference")
    if not isinstance(version, str) or len(version) > 80:
        raise ValueError("invalid secret version")
    database = connect(control_database_path)
    try:
        database.execute("BEGIN IMMEDIATE")
        if not database.execute(
            "SELECT 1 FROM platform_tenants WHERE id = ?", (tenant_id,)
        ).fetchone():
            raise ValueError("tenant not found")
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
            action="tenant.secret_reference.updated",
            tenant_id=tenant_id,
            details={"kind": secret_kind},
        )
        database.commit()
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
        if not tenant.database_path.is_file():
            raise ValueError("tenant database is unavailable")
        tenant_database: sqlite3.Connection | None = None
        try:
            tenant_database = sqlite3.connect(tenant.database_path)
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
