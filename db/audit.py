from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping


_ALLOWED_SOURCES = {"telegram", "mini_app", "webhook", "scheduler", "system"}
_ALLOWED_ROLES = {"owner", "manager", "warehouse", "system"}
_ALLOWED_OUTCOMES = {"succeeded", "rejected", "failed"}
_ALLOWED_DETAIL_KEYS = {
    "batch_id",
    "book_id",
    "count",
    "delivery_status",
    "event_id",
    "from_role",
    "from_status",
    "import_hash",
    "movement_id",
    "new_stock",
    "old_stock",
    "reason_code",
    "request_id",
    "role",
    "to_role",
    "to_status",
}


def _details_json(details: Mapping[str, object] | None) -> str:
    if not details:
        return "{}"
    if set(details) - _ALLOWED_DETAIL_KEYS:
        raise ValueError("unsupported audit detail")
    normalized: dict[str, int | str | bool | None] = {}
    for key, value in details.items():
        if value is not None and not isinstance(value, (str, int, bool)):
            raise ValueError("unsupported audit detail value")
        if isinstance(value, str) and len(value) > 128:
            raise ValueError("audit detail is too long")
        normalized[key] = value
    return json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)


def append_audit_event(
    database: sqlite3.Connection,
    *,
    actor_user_id: int | None,
    actor_role: str,
    source: str,
    action: str,
    entity_type: str,
    entity_id: int | str | None = None,
    outcome: str = "succeeded",
    correlation_id: str = "",
    details: Mapping[str, object] | None = None,
) -> int:
    if actor_role not in _ALLOWED_ROLES:
        raise ValueError("unsupported audit actor role")
    if source not in _ALLOWED_SOURCES:
        raise ValueError("unsupported audit source")
    if outcome not in _ALLOWED_OUTCOMES:
        raise ValueError("unsupported audit outcome")
    if not action or len(action) > 80 or not entity_type or len(entity_type) > 40:
        raise ValueError("invalid audit event")
    if len(correlation_id) > 64:
        raise ValueError("invalid audit correlation")
    cursor = database.execute(
        """
        INSERT INTO audit_events (
            actor_user_id, actor_role, source, action, entity_type, entity_id,
            correlation_id, details_json, outcome
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor_user_id,
            actor_role,
            source,
            action,
            entity_type,
            str(entity_id or ""),
            correlation_id,
            _details_json(details),
            outcome,
        ),
    )
    return cursor.lastrowid


def list_audit_events_export_sync(*, limit: int = 10_000) -> list[dict]:
    from db.schema import connect

    database = connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT id, actor_user_id, actor_role, source, action, entity_type,
                   entity_id, correlation_id, details_json, outcome, created_at
            FROM audit_events
            ORDER BY id DESC LIMIT ?
            """,
            (max(1, min(limit, 10_000)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        database.close()


def list_audit_events_sync(
    *,
    limit: int = 50,
    before_id: int | None = None,
    action: str = "",
    entity_type: str = "",
) -> list[dict]:
    from db.schema import connect

    clauses = []
    params: list[object] = []
    if before_id is not None:
        clauses.append("id < ?")
        params.append(before_id)
    if action:
        clauses.append("action = ?")
        params.append(action)
    if entity_type:
        clauses.append("entity_type = ?")
        params.append(entity_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            f"""
            SELECT id, actor_user_id, actor_role, source, action, entity_type,
                   entity_id, correlation_id, details_json, outcome, created_at
            FROM audit_events {where}
            ORDER BY id DESC LIMIT ?
            """,
            (*params, max(1, min(limit, 100))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        database.close()
