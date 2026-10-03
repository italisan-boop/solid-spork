from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterable

from controlplane.plan_policy import FEATURE_STAFF, LIMIT_STAFF_MEMBERS, Plan
from db.audit import append_audit_event
from db.schema import connect


STAFF_ROLES = {"administrator", "editor", "manager", "warehouse"}
ROLE_PRIORITY = ("administrator", "manager", "warehouse", "editor")


def _validate_staff_id(user_id: int) -> None:
    if not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("staff Telegram ID must be positive")


def _primary_role(roles: Iterable[str]) -> str | None:
    selected = set(roles)
    return next((role for role in ROLE_PRIORITY if role in selected), None)


def _normalized_roles(roles: object) -> tuple[str, ...]:
    if isinstance(roles, str):
        selected = {roles}
    elif isinstance(roles, (list, tuple, set, frozenset)) and all(
        isinstance(role, str) for role in roles
    ):
        selected = set(roles)
    else:
        raise ValueError("roles must be a non-empty role list")
    if not selected or not selected.issubset(STAFF_ROLES):
        raise ValueError("unsupported staff role")
    if "administrator" in selected and len(selected) != 1:
        raise ValueError("administrator cannot be combined with other roles")
    return tuple(role for role in ROLE_PRIORITY if role in selected)


def get_staff_roles_sync(user_id: int) -> tuple[str, ...]:
    _validate_staff_id(user_id)
    database = connect()
    try:
        rows = database.execute(
            """
            SELECT roles.role
            FROM staff_members AS members
            JOIN staff_member_roles AS roles ON roles.telegram_user_id = members.telegram_user_id
            WHERE members.telegram_user_id = ? AND members.is_active = 1
            ORDER BY CASE roles.role
                WHEN 'administrator' THEN 1
                WHEN 'manager' THEN 2
                WHEN 'warehouse' THEN 3
                WHEN 'editor' THEN 4
            END
            """,
            (user_id,),
        ).fetchall()
        return tuple(row[0] for row in rows)
    finally:
        database.close()


def get_staff_role_sync(user_id: int) -> str | None:
    return _primary_role(get_staff_roles_sync(user_id))


def _roles_for_member(database: sqlite3.Connection, user_id: int) -> tuple[str, ...]:
    rows = database.execute(
        """
        SELECT role FROM staff_member_roles WHERE telegram_user_id = ?
        ORDER BY CASE role
            WHEN 'administrator' THEN 1
            WHEN 'manager' THEN 2
            WHEN 'warehouse' THEN 3
            WHEN 'editor' THEN 4
        END
        """,
        (user_id,),
    ).fetchall()
    return tuple(row[0] for row in rows)


def list_staff_sync() -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        members = database.execute(
            """
            SELECT telegram_user_id, is_active, created_at, updated_at, changed_by_user_id
            FROM staff_members ORDER BY is_active DESC, telegram_user_id ASC
            """
        ).fetchall()
        return [
            {
                **dict(member),
                "roles": list(_roles_for_member(database, member["telegram_user_id"])),
                "role": _primary_role(_roles_for_member(database, member["telegram_user_id"])),
            }
            for member in members
        ]
    finally:
        database.close()


def set_staff_member_sync(
    telegram_user_id: int,
    roles: object,
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
    if not isinstance(active, bool):
        raise ValueError("staff active state must be boolean")
    normalized_roles = _normalized_roles(roles)
    tenant_context = maybe_current_tenant_context()
    if "editor" in normalized_roles and (
        tenant_context is None or tenant_context.entitlements.plan not in {Plan.BUSINESS, Plan.PRO}
    ):
        raise ValueError("editor role requires Business or Pro plan")
    if tenant_context is not None:
        require_feature(FEATURE_STAFF, tenant_context)
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        previous = database.execute(
            "SELECT is_active FROM staff_members WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        previous_roles = _roles_for_member(database, telegram_user_id)
        if active and (previous is None or not previous["is_active"]):
            active_staff = database.execute(
                "SELECT COUNT(*) FROM staff_members WHERE is_active = 1"
            ).fetchone()[0]
            require_count_quota(LIMIT_STAFF_MEMBERS, active_staff, context=tenant_context)
        database.execute(
            """
            INSERT INTO staff_members (telegram_user_id, is_active, changed_by_user_id)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                is_active = excluded.is_active,
                changed_by_user_id = excluded.changed_by_user_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (telegram_user_id, int(active), actor_user_id),
        )
        database.execute(
            "DELETE FROM staff_member_roles WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        database.executemany(
            """
            INSERT INTO staff_member_roles (telegram_user_id, role, assigned_by_user_id)
            VALUES (?, ?, ?)
            """,
            [(telegram_user_id, role, actor_user_id) for role in normalized_roles],
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
                "from_role": ",".join(previous_roles),
                "to_role": ",".join(normalized_roles),
            },
        )
        database.commit()
        return {
            "telegram_user_id": telegram_user_id,
            "roles": list(normalized_roles),
            "role": _primary_role(normalized_roles),
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
        try:
            rows = database.execute(
                f"""
                SELECT DISTINCT members.telegram_user_id
                FROM staff_members AS members
                JOIN staff_member_roles AS roles ON roles.telegram_user_id = members.telegram_user_id
                WHERE members.is_active = 1 AND roles.role IN ({placeholders})
                ORDER BY members.telegram_user_id
                """,
                tuple(sorted(roles)),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error):
                raise
            return []
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
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT is_active FROM staff_members WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if row is None:
            database.rollback()
            return False
        if row["is_active"]:
            raise ValueError("deactivate the staff member before deletion")
        previous_roles = _roles_for_member(database, telegram_user_id)
        database.execute("DELETE FROM staff_members WHERE telegram_user_id = ?", (telegram_user_id,))
        append_audit_event(
            database,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            source="mini_app",
            action="staff.member.deleted",
            entity_type="staff_member",
            entity_id=telegram_user_id,
            details={"from_role": ",".join(previous_roles)},
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


async def get_staff_roles(user_id: int) -> tuple[str, ...]:
    return await asyncio.to_thread(get_staff_roles_sync, user_id)


async def list_staff() -> list[dict]:
    return await asyncio.to_thread(list_staff_sync)


async def set_staff_member(
    telegram_user_id: int,
    roles: object,
    *,
    active: bool,
    actor_user_id: int,
    actor_role: str = "owner",
) -> dict:
    return await asyncio.to_thread(
        set_staff_member_sync,
        telegram_user_id,
        roles,
        active=active,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
    )
