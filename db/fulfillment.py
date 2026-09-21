from __future__ import annotations

import sqlite3

from db.audit import append_audit_event
from db.deliveries import (
    METHOD_LABELS,
    SHIPMENT_LABELS,
    SHIPMENT_PACKED,
    SHIPMENT_PREPARING,
    DeliveryTransitionError,
    transition_delivery_sync,
)
from db.schema import connect


FULFILLMENT_STATE_LABELS = {
    "ready": "Готов к сборке",
    "claimed": "Собирается",
    "blocked": "Требует внимания",
    "packed": "Собран",
}


class FulfillmentError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "fulfillment_conflict",
        remaining_quantity: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.remaining_quantity = remaining_quantity


def _shipment_status(database: sqlite3.Connection, order_id: int) -> str:
    row = database.execute(
        """
        SELECT o.status, d.shipment_status
        FROM orders o JOIN order_deliveries d ON d.order_id = o.id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()
    if not row or row[0] != "confirmed" or row[1] not in {SHIPMENT_PREPARING, SHIPMENT_PACKED}:
        raise FulfillmentError("Order is unavailable for fulfillment", code="unavailable")
    return row[1]


def _assert_not_assembled(database: sqlite3.Connection, order_id: int) -> None:
    if _shipment_status(database, order_id) == SHIPMENT_PACKED:
        raise FulfillmentError("Order is already packed", code="already_packed")


def _ensure_fulfillment(database: sqlite3.Connection, order_id: int) -> None:
    database.execute(
        "INSERT OR IGNORE INTO order_fulfillments (order_id) VALUES (?)", (order_id,)
    )
    database.execute(
        """
        INSERT OR IGNORE INTO order_packing_lines (
            order_id, book_id, title, price, ordered_quantity
        )
        SELECT order_id, book_id, title, price, COUNT(*)
        FROM order_items
        WHERE order_id = ?
        GROUP BY order_id, book_id, title, price
        """,
        (order_id,),
    )


def _fulfillment_actions(
    record: dict, actor_user_id: int, *, is_owner: bool
) -> dict[str, bool]:
    is_assembled = record["shipment_status"] == SHIPMENT_PACKED
    claimed_by_current_user = record["warehouse_user_id"] == actor_user_id
    can_work = not is_assembled and (is_owner or claimed_by_current_user)
    return {
        "can_open": is_owner or (not is_assembled and record["warehouse_user_id"] in {None, actor_user_id}),
        "can_claim": not is_assembled and not is_owner and record["warehouse_user_id"] is None,
        "can_update_lines": can_work,
        "can_pack": can_work,
        "can_print": is_owner or (not is_assembled and claimed_by_current_user),
        "status_only": is_assembled and not is_owner,
    }


def _decorate_record(record: dict, actor_user_id: int, *, is_owner: bool) -> dict:
    is_assembled = record["shipment_status"] == SHIPMENT_PACKED
    fulfillment_state = "packed" if is_assembled else record["state"]
    record.update(
        {
            "fulfillment_state": fulfillment_state,
            "fulfillment_state_label": FULFILLMENT_STATE_LABELS.get(
                fulfillment_state, fulfillment_state
            ),
            "method_label": METHOD_LABELS.get(record["method"], record["method"]),
            "shipment_label": SHIPMENT_LABELS.get(
                record["shipment_status"], record["shipment_status"]
            ),
            "is_assembled": is_assembled,
        }
    )
    record.update(_fulfillment_actions(record, actor_user_id, is_owner=is_owner))
    return record


def _packing_record(
    database: sqlite3.Connection, order_id: int, actor_user_id: int, *, is_owner: bool
) -> dict:
    database.row_factory = sqlite3.Row
    fulfillment = database.execute(
        """
        SELECT f.order_id, f.warehouse_user_id, f.state, f.version, f.claimed_at,
               f.packed_at, f.updated_at, d.method, d.shipment_status
        FROM order_fulfillments f
        JOIN order_deliveries d ON d.order_id = f.order_id
        WHERE f.order_id = ?
        """,
        (order_id,),
    ).fetchone()
    if not fulfillment:
        raise FulfillmentError("Fulfillment is unavailable", code="unavailable")
    lines = database.execute(
        """
        SELECT book_id, title, price, ordered_quantity, picked_quantity, updated_at
        FROM order_packing_lines WHERE order_id = ?
        ORDER BY title COLLATE NOCASE, book_id
        """,
        (order_id,),
    ).fetchall()
    record = {**dict(fulfillment), "lines": [dict(line) for line in lines]}
    return _decorate_record(record, actor_user_id, is_owner=is_owner)


def list_fulfillment_queue_sync(
    *, actor_user_id: int, is_owner: bool, limit: int = 50
) -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT o.id AS order_id, o.created_at, d.method, d.shipment_status,
                   COALESCE(f.state, 'ready') AS state, f.warehouse_user_id, f.updated_at
            FROM orders o
            JOIN order_deliveries d ON d.order_id = o.id
            LEFT JOIN order_fulfillments f ON f.order_id = o.id
            WHERE o.status = 'confirmed' AND d.shipment_status IN ('preparing', 'packed')
            ORDER BY CASE WHEN d.shipment_status = 'packed' THEN 2
                          WHEN COALESCE(f.state, 'ready') = 'blocked' THEN 0
                          WHEN COALESCE(f.state, 'ready') = 'ready' THEN 1 ELSE 2 END,
                     o.created_at ASC, o.id ASC
            LIMIT ?
            """,
            (max(1, min(limit, 100)),),
        ).fetchall()
        return [
            _decorate_record(dict(row), actor_user_id, is_owner=is_owner)
            for row in rows
        ]
    finally:
        database.close()


def claim_fulfillment_sync(order_id: int, actor_user_id: int, actor_role: str) -> dict:
    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        _assert_not_assembled(database, order_id)
        _ensure_fulfillment(database, order_id)
        fulfillment = database.execute(
            "SELECT warehouse_user_id, state FROM order_fulfillments WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        if fulfillment[0] not in {None, actor_user_id}:
            raise FulfillmentError("Order is already assigned", code="assigned")
        if fulfillment[1] == "packed":
            raise FulfillmentError("Order is already packed", code="already_packed")
        database.execute(
            """
            UPDATE order_fulfillments
            SET warehouse_user_id = ?, state = 'claimed', version = version + 1,
                claimed_at = COALESCE(claimed_at, CURRENT_TIMESTAMP), updated_at = CURRENT_TIMESTAMP
            WHERE order_id = ?
            """,
            (actor_user_id, order_id),
        )
        append_audit_event(
            database,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            source="mini_app",
            action="fulfillment.claimed",
            entity_type="order",
            entity_id=order_id,
        )
        result = _packing_record(
            database, order_id, actor_user_id, is_owner=actor_role == "owner"
        )
        database.commit()
        return result
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def get_fulfillment_sync(order_id: int, actor_user_id: int, *, is_owner: bool) -> dict:
    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        _shipment_status(database, order_id)
        _ensure_fulfillment(database, order_id)
        record = _packing_record(database, order_id, actor_user_id, is_owner=is_owner)
        if not record["can_open"]:
            if record["is_assembled"]:
                raise FulfillmentError("Order is already packed", code="already_packed")
            raise FulfillmentError("Order is assigned to another worker", code="assigned")
        database.commit()
        return record
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def set_picked_quantity_sync(
    order_id: int,
    book_id: int,
    title: str,
    price: int,
    picked_quantity: int,
    actor_user_id: int,
    actor_role: str,
    *,
    is_owner: bool,
) -> dict:
    if not isinstance(picked_quantity, int) or picked_quantity < 0:
        raise FulfillmentError("Picked quantity is invalid", code="invalid_line")
    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        _assert_not_assembled(database, order_id)
        _ensure_fulfillment(database, order_id)
        record = _packing_record(database, order_id, actor_user_id, is_owner=is_owner)
        if record["state"] == "packed":
            raise FulfillmentError("Order is already packed", code="already_packed")
        if not record["can_update_lines"]:
            raise FulfillmentError(
                "Claim the order before changing its checklist", code="claim_required"
            )
        line = database.execute(
            """
            SELECT ordered_quantity FROM order_packing_lines
            WHERE order_id = ? AND book_id = ? AND title = ? AND price = ?
            """,
            (order_id, book_id, title, price),
        ).fetchone()
        if not line or picked_quantity > line[0]:
            raise FulfillmentError("Picked quantity exceeds the order", code="invalid_line")
        database.execute(
            """
            UPDATE order_packing_lines
            SET picked_quantity = ?, updated_at = CURRENT_TIMESTAMP
            WHERE order_id = ? AND book_id = ? AND title = ? AND price = ?
            """,
            (picked_quantity, order_id, book_id, title, price),
        )
        database.execute(
            """
            UPDATE order_fulfillments
            SET version = version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE order_id = ?
            """,
            (order_id,),
        )
        append_audit_event(
            database,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            source="mini_app",
            action="fulfillment.line.updated",
            entity_type="order",
            entity_id=order_id,
            details={"book_id": book_id, "count": picked_quantity},
        )
        result = _packing_record(database, order_id, actor_user_id, is_owner=is_owner)
        database.commit()
        return result
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def pack_fulfillment_sync(
    order_id: int, actor_user_id: int, actor_role: str, *, is_owner: bool
) -> dict:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        _assert_not_assembled(database, order_id)
        _ensure_fulfillment(database, order_id)
        record = _packing_record(database, order_id, actor_user_id, is_owner=is_owner)
        if record["state"] == "packed":
            raise FulfillmentError("Order is already packed", code="already_packed")
        if not record["can_pack"]:
            raise FulfillmentError("Claim the order before packing it", code="claim_required")
        remaining_quantity = sum(
            line["ordered_quantity"] - line["picked_quantity"]
            for line in record["lines"]
        )
        if not record["lines"] or remaining_quantity:
            raise FulfillmentError(
                "Complete every packing line before marking the order packed",
                code="incomplete_checklist",
                remaining_quantity=remaining_quantity,
            )
        transition_delivery_sync(database, order_id, SHIPMENT_PACKED, admin_id=actor_user_id)
        database.execute(
            """
            UPDATE order_fulfillments
            SET state = 'packed', version = version + 1, packed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE order_id = ?
            """,
            (order_id,),
        )
        append_audit_event(
            database,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            source="mini_app",
            action="fulfillment.packed",
            entity_type="order",
            entity_id=order_id,
            details={"to_status": "packed"},
        )
        result = _packing_record(database, order_id, actor_user_id, is_owner=is_owner)
        result["already_packed"] = False
        database.commit()
        return result
    except (DeliveryTransitionError, Exception):
        database.rollback()
        raise
    finally:
        database.close()


def packing_print_payload_sync(order_id: int, actor_user_id: int, *, is_owner: bool) -> dict:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        _shipment_status(database, order_id)
        _ensure_fulfillment(database, order_id)
        record = _packing_record(database, order_id, actor_user_id, is_owner=is_owner)
        if not record["can_print"]:
            if record["is_assembled"]:
                raise FulfillmentError("Order is already packed", code="already_packed")
            raise FulfillmentError("Order is assigned to another worker", code="assigned")
        order = database.execute(
            """
            SELECT o.id, o.created_at, d.method, d.shipment_status,
                   d.destination_encrypted, d.public_instructions_snapshot
            FROM orders o JOIN order_deliveries d ON d.order_id = o.id
            WHERE o.id = ?
            """,
            (order_id,),
        ).fetchone()
        if not order:
            raise FulfillmentError("Order is unavailable", code="unavailable")
        append_audit_event(
            database,
            actor_user_id=actor_user_id,
            actor_role="owner" if is_owner else "warehouse",
            source="mini_app",
            action="fulfillment.print.viewed",
            entity_type="order",
            entity_id=order_id,
        )
        database.commit()
        return {**dict(order), "fulfillment": record}
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
