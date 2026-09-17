from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from runtime.context import TenantContext, maybe_current_tenant_context
from runtime.features import QuotaExceededError, require_quota


def _tenant_context(context: TenantContext | None = None) -> TenantContext | None:
    return context or maybe_current_tenant_context()


def require_count_quota(
    limit_name: str,
    current_value: int,
    increment: int = 1,
    *,
    context: TenantContext | None = None,
) -> None:
    selected = _tenant_context(context)
    if selected is not None:
        require_quota(limit_name, current_value, increment, context=selected)


def reserve_daily_quota(
    database: sqlite3.Connection,
    limit_name: str,
    amount: int,
    *,
    context: TenantContext | None = None,
    usage_day: str | None = None,
) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError("quota amount must be a non-negative integer")
    selected = _tenant_context(context)
    if selected is None or amount == 0:
        return
    require_quota(limit_name, 0, amount, context=selected)
    day = usage_day or datetime.now(UTC).date().isoformat()
    cursor = database.execute(
        """
        INSERT INTO tenant_daily_usage (usage_day, limit_name, quantity, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(usage_day, limit_name) DO UPDATE SET
            quantity = tenant_daily_usage.quantity + excluded.quantity,
            updated_at = CURRENT_TIMESTAMP
        WHERE tenant_daily_usage.quantity + excluded.quantity <= ?
        """,
        (day, limit_name, amount, selected.entitlements.limits[limit_name]),
    )
    if cursor.rowcount != 1:
        raise QuotaExceededError(limit_name, selected.entitlements.limits[limit_name])


def reserve_daily_quota_sync(
    limit_name: str,
    amount: int,
    *,
    context: TenantContext | None = None,
    usage_day: str | None = None,
) -> None:
    selected = _tenant_context(context)
    if selected is None or amount == 0:
        return
    from db.schema import connect

    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        reserve_daily_quota(
            database,
            limit_name,
            amount,
            context=selected,
            usage_day=usage_day,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
