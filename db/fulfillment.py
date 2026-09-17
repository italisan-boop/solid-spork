from __future__ import annotations

import sqlite3

from db.audit import append_audit_event
from db.deliveries import DeliveryTransitionError, SHIPMENT_PACKED, transition_delivery_sync
from db.schema import connect


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


def _assert_ready_for_packing(database: sqlite3.Connection, order_id: int) -> None:
    row = database.execute(
        """
        SELECT o.status, d.shipment_status
        FROM orders o JOIN order_deliveries d ON d.order_id = o.id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()
    if not row or row[0] != "confirmed" or row[1] not in {"preparing", "packed"}:
        raise FulfillmentError("Order is unavailable for fulfillment")


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


def _packing_record(database: sqlite3.Connection, order_id: int) -> dict:
    database.row_factory = sqlite3.Row
    fulfillment = database.execute(
        """
        SELECT order_id, warehouse_user_id, state, version, claimed_at, packed_at, updated_at
        FROM order_fulfillments WHERE order_id = ?
        """,
        (order_id,),
    ).fetchone()
    if not fulfillment:
        raise FulfillmentError("Fulfillment is unavailable")
    lines = database.execute(
        """
        SELECT book_id, title, price, ordered_quantity, picked_quantity, updated_at
        FROM order_packing_lines WHERE order_id = ?
        ORDER BY title COLLATE NOCASE, book_id
        """,
        (order_id,),
    ).fetchall()
    return {**dict(fulfillment), "lines": [dict(line) for line in lines]}


def list_fulfillment_queue_sync(*, limit: int = 50) -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT o.id AS order_id, o.created_at, d.method, d.shipment_status,
                   COALESCE(f.state, 'ready') AS fulfillment_state,
                   f.warehouse_user_id, f.updated_at
            FROM orders o
            JOIN order_deliveries d ON d.order_id = o.id
            LEFT JOIN order_fulfillments f ON f.order_id = o.id
            WHERE o.status = 'confirmed' AND d.shipment_status IN ('preparing', 'packed')
            ORDER BY CASE COALESCE(f.state, 'ready') WHEN 'blocked' THEN 0 WHEN 'ready' THEN 1 ELSE 2 END,
                     o.created_at ASC, o.id ASC
            LIMIT ?
            """,
            (max(1, min(limit, 100)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        database.close()


def claim_fulfillment_sync(order_id: int, actor_user_id: int, actor_role: str) -> dict:
    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        _assert_ready_for_packing(database, order_id)
        _ensure_fulfillment(database, order_id)
        fulfillment = database.execute(
            "SELECT warehouse_user_id, state FROM order_fulfillments WHERE order_id = ?", (order_id,)
        ).fetchone()
        if fulfillment[0] not in {None, actor_user_id}:
            raise FulfillmentError("Order is already assigned")
        if fulfillment[1] == "packed":
            raise FulfillmentError("Order is already packed")
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
        result = _packing_record(database, order_id)
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
        _assert_ready_for_packing(database, order_id)
        _ensure_fulfillment(database, order_id)
        record = _packing_record(database, order_id)
        if not is_owner and record["warehouse_user_id"] not in {None, actor_user_id}:
            raise FulfillmentError("Order is assigned to another worker")
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
        raise FulfillmentError("Picked quantity is invalid")
    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        _assert_ready_for_packing(database, order_id)
        _ensure_fulfillment(database, order_id)
        record = _packing_record(database, order_id)
        if record["state"] == "packed":
            raise FulfillmentError(
                "Order is already packed", code="already_packed"
            )
        if not is_owner and record["warehouse_user_id"] != actor_user_id:
            raise FulfillmentError("Claim the order before changing its checklist", code="claim_required")
        line = database.execute(
            """
            SELECT ordered_quantity FROM order_packing_lines
            WHERE order_id = ? AND book_id = ? AND title = ? AND price = ?
            """,
            (order_id, book_id, title, price),
        ).fetchone()
        if not line or picked_quantity > line[0]:
            raise FulfillmentError("Picked quantity exceeds the order")
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
        result = _packing_record(database, order_id)
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
        _assert_ready_for_packing(database, order_id)
        _ensure_fulfillment(database, order_id)
        record = _packing_record(database, order_id)
        if not is_owner and record["warehouse_user_id"] != actor_user_id:
            raise FulfillmentError("Claim the order before packing it", code="claim_required")
        if record["state"] == "packed":
            record["already_packed"] = True
            database.commit()
            return record
        delivery_status = database.execute(
            "SELECT shipment_status FROM order_deliveries WHERE order_id = ?", (order_id,)
        ).fetchone()[0]
        if delivery_status == SHIPMENT_PACKED:
            raise FulfillmentError(
                "Delivery is already packed. Refresh the order.",
                code="shipment_already_packed",
            )
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
        result = _packing_record(database, order_id)
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
        record = _packing_record(database, order_id)
        if not is_owner and record["warehouse_user_id"] != actor_user_id:
            raise FulfillmentError("Order is assigned to another worker")
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
            raise FulfillmentError("Order is unavailable")
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
