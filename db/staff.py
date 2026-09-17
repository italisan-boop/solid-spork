from __future__ import annotations

import asyncio
import sqlite3

from controlplane.plan_policy import FEATURE_STAFF, LIMIT_STAFF_MEMBERS
from db.audit import append_audit_event
from db.schema import connect


STAFF_ROLES = {"manager", "warehouse"}


def _validate_staff_id(user_id: int) -> None:
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("staff Telegram ID must be positive")


def get_staff_role_sync(user_id: int) -> str | None:
    _validate_staff_id(user_id)
    database = connect()
    try:
        row = database.execute(
            "SELECT role FROM staff_members WHERE telegram_user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        database.close()


def list_staff_sync() -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT telegram_user_id, role, is_active, created_at, updated_at, changed_by_user_id
            FROM staff_members
            ORDER BY is_active DESC, role ASC, telegram_user_id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        database.close()


def set_staff_member_sync(
    telegram_user_id: int,
    role: str,
    *,
    active: bool,
    actor_user_id: int,
    actor_role: str = "owner",
) -> dict:
    from runtime.context import maybe_current_tenant_context
    from runtime.features import require_feature
    from runtime.quota import require_count_quota

    _validate_staff_id(telegram_user_id)
    _validate_staff_id(actor_user_id)
    if role not in STAFF_ROLES:
        raise ValueError("unsupported staff role")
    tenant_context = maybe_current_tenant_context()
    if tenant_context is not None:
        require_feature(FEATURE_STAFF, tenant_context)
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        previous = database.execute(
            "SELECT role, is_active FROM staff_members WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if active and (previous is None or not previous["is_active"]):
            active_staff = database.execute(
                "SELECT COUNT(*) FROM staff_members WHERE is_active = 1"
            ).fetchone()[0]
            require_count_quota(
                LIMIT_STAFF_MEMBERS, active_staff, context=tenant_context
            )
        database.execute(
            """
            INSERT INTO staff_members (
                telegram_user_id, role, is_active, changed_by_user_id
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                role = excluded.role,
                is_active = excluded.is_active,
                changed_by_user_id = excluded.changed_by_user_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (telegram_user_id, role, int(active), actor_user_id),
        )
        append_audit_event(
            database,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            source="mini_app",
            action="staff.member.updated",
            entity_type="staff_member",
            entity_id=telegram_user_id,
            details={
                "from_role": previous["role"] if previous else None,
                "to_role": role,
                "role": role,
            },
        )
        database.commit()
        return {
            "telegram_user_id": telegram_user_id,
            "role": role,
            "is_active": active,
        }
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def active_staff_ids_for_roles_sync(roles: set[str]) -> list[int]:
    if not roles.issubset(STAFF_ROLES):
        raise ValueError("unsupported staff role")
    if not roles:
        return []
    database = connect()
    try:
        placeholders = ",".join("?" for _ in roles)
        rows = database.execute(
            f"""
            SELECT telegram_user_id FROM staff_members
            WHERE is_active = 1 AND role IN ({placeholders})
            ORDER BY telegram_user_id
            """,
            tuple(sorted(roles)),
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        database.close()


def delete_inactive_staff_member_sync(
    telegram_user_id: int,
    *,
    actor_user_id: int,
    actor_role: str = "owner",
) -> bool:
    _validate_staff_id(telegram_user_id)
    _validate_staff_id(actor_user_id)
    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT role, is_active FROM staff_members WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if row is None:
            database.rollback()
            return False
        if row[1]:
            raise ValueError("deactivate the staff member before deletion")
        database.execute(
            "DELETE FROM staff_members WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        append_audit_event(
            database,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            source="mini_app",
            action="staff.member.deleted",
            entity_type="staff_member",
            entity_id=telegram_user_id,
            details={"from_role": row[0]},
        )
        database.commit()
        return True
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


async def get_staff_role(user_id: int) -> str | None:
    return await asyncio.to_thread(get_staff_role_sync, user_id)


async def list_staff() -> list[dict]:
    return await asyncio.to_thread(list_staff_sync)


async def set_staff_member(
    telegram_user_id: int,
    role: str,
    *,
    active: bool,
    actor_user_id: int,
    actor_role: str = "owner",
) -> dict:
    return await asyncio.to_thread(
        set_staff_member_sync,
        telegram_user_id,
        role,
        active=active,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
    )
