from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import dataclass

from config import settings
from db.schema import connect


_ALERTABLE_SEVERITIES = {"error", "critical"}
_ALLOWED_SEVERITIES = {"info", "warning", "error", "critical"}


@dataclass(frozen=True)
class OperationalEvent:
    severity: str
    component: str
    event: str
    outcome: str
    reason: str = ""
    order_id: int | None = None
    attempt_id: int | None = None
    error_type: str = ""


def _fingerprint(event: OperationalEvent) -> str:
    value = "\x1f".join(
        [event.component, event.event, event.outcome, event.reason, event.error_type]
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def record_event_sync(event: OperationalEvent) -> int:
    if event.severity not in _ALLOWED_SEVERITIES:
        raise ValueError("unsupported event severity")
    connection = connect()
    try:
        fingerprint = _fingerprint(event)
        alert_state = "pending"
        if event.severity in _ALERTABLE_SEVERITIES:
            recently_alerted = connection.execute(
                """
                SELECT 1 FROM operational_events
                WHERE fingerprint = ?
                  AND alert_state IN ('pending', 'processing', 'sent')
                  AND created_at >= datetime('now', ?)
                LIMIT 1
                """,
                (fingerprint, f"-{settings.OPERATIONAL_ALERT_DEDUP_SECONDS} seconds"),
            ).fetchone()
            if recently_alerted:
                alert_state = "suppressed"
        cursor = connection.execute(
            """
            INSERT INTO operational_events
                (fingerprint, severity, component, event, outcome, reason, order_id, attempt_id, error_type, alert_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                event.severity,
                event.component,
                event.event,
                event.outcome,
                event.reason,
                event.order_id,
                event.attempt_id,
                event.error_type,
                alert_state,
            ),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


async def record_event(event: OperationalEvent) -> int:
    return await asyncio.to_thread(record_event_sync, event)


def claim_alertable_events_sync(limit: int = 10, lease_seconds: int = 300) -> list[dict]:
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT * FROM operational_events
            WHERE severity IN ('error', 'critical')
              AND (
                  alert_state = 'pending'
                  OR (alert_state = 'processing' AND alert_claimed_at < datetime('now', ?))
              )
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (f"-{lease_seconds} seconds", limit),
        ).fetchall()
        event_ids = [row["id"] for row in rows]
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            connection.execute(
                f"""
                UPDATE operational_events
                SET alert_state = 'processing', alert_claimed_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                event_ids,
            )
        connection.commit()
        return [dict(row) for row in rows]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


async def claim_alertable_events(limit: int = 10, lease_seconds: int = 300) -> list[dict]:
    return await asyncio.to_thread(claim_alertable_events_sync, limit, lease_seconds)


async def mark_alert_sent(event_id: int) -> None:
    from db.connection import connection

    async with connection() as database:
        await database.execute(
            """
            UPDATE operational_events
            SET alert_state = 'sent', alert_sent_at = CURRENT_TIMESTAMP
            WHERE id = ? AND alert_state = 'processing'
            """,
            (event_id,),
        )
        await database.commit()


async def release_alert_claim(event_id: int) -> None:
    from db.connection import connection

    async with connection() as database:
        await database.execute(
            """
            UPDATE operational_events
            SET alert_state = 'pending', alert_claimed_at = NULL
            WHERE id = ? AND alert_state = 'processing'
            """,
            (event_id,),
        )
        await database.commit()


def get_latest_event_sync(event: str) -> dict | None:
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM operational_events WHERE event = ? ORDER BY id DESC LIMIT 1",
            (event,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()
