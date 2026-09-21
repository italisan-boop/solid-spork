from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable

from db.schema import connect


DEFAULT_LOW_STOCK_THRESHOLD = 3


class InventoryUnavailableError(ValueError):
    pass


def _book_quantities(cart: Iterable[dict]) -> dict[int, int]:
    quantities: dict[int, int] = {}
    for item in cart:
        book_id = item["id"]
        quantity = item["quantity"]
        quantities[book_id] = quantities.get(book_id, 0) + quantity
    return quantities


def _reserved_quantity(connection: sqlite3.Connection, book_id: int) -> int:
    return connection.execute(
        """
        SELECT COALESCE(SUM(quantity), 0)
        FROM inventory_reservations
        WHERE book_id = ? AND state = 'reserved'
        """,
        (book_id,),
    ).fetchone()[0]


def _stock_quantity(connection: sqlite3.Connection, book_id: int) -> int | None:
    row = connection.execute(
        "SELECT stock_quantity FROM books WHERE id = ?", (book_id,)
    ).fetchone()
    return row[0] if row else None


def _refresh_low_stock_state(
    connection: sqlite3.Connection, book_id: int, movement_id: int
) -> None:
    stock_quantity = _stock_quantity(connection, book_id)
    if stock_quantity is None:
        connection.execute(
            """
            UPDATE inventory_low_stock_state
            SET is_low = 0, updated_at = CURRENT_TIMESTAMP
            WHERE book_id = ?
            """,
            (book_id,),
        )
        return
    reserved_quantity = _reserved_quantity(connection, book_id)
    row = connection.execute(
        "SELECT threshold, is_low FROM inventory_low_stock_state WHERE book_id = ?",
        (book_id,),
    ).fetchone()
    if row is None:
        threshold, was_low = DEFAULT_LOW_STOCK_THRESHOLD, False
        connection.execute(
            "INSERT INTO inventory_low_stock_state (book_id, threshold) VALUES (?, ?)",
            (book_id, threshold),
        )
    else:
        threshold, was_low = row[0], bool(row[1])
    is_low = stock_quantity - reserved_quantity <= threshold
    connection.execute(
        """
        UPDATE inventory_low_stock_state
        SET is_low = ?, updated_at = CURRENT_TIMESTAMP
        WHERE book_id = ?
        """,
        (int(is_low), book_id),
    )
    if is_low and not was_low:
        connection.execute(
            """
            INSERT OR IGNORE INTO notification_outbox (kind, dedupe_key, book_id, payload_json)
            VALUES ('low_stock', ?, ?, ?)
            """,
            (
                f"low-stock:{book_id}:{movement_id}",
                book_id,
                json.dumps(
                    {
                        "available": stock_quantity - reserved_quantity,
                        "threshold": threshold,
                    },
                    separators=(",", ":"),
                ),
            ),
        )


def _enqueue_back_in_stock(
    connection: sqlite3.Connection, book_id: int, movement_id: int, was_available: int
) -> None:
    stock_quantity = _stock_quantity(connection, book_id)
    if was_available > 0:
        return
    if stock_quantity is not None:
        available = stock_quantity - _reserved_quantity(connection, book_id)
        if available <= 0:
            return
    subscribers = connection.execute(
        """
        SELECT user_id FROM back_in_stock_subscriptions
        WHERE book_id = ? AND active = 1
        """,
        (book_id,),
    ).fetchall()
    for (user_id,) in subscribers:
        connection.execute(
            """
            INSERT OR IGNORE INTO notification_outbox
                (kind, dedupe_key, user_id, book_id, payload_json)
            VALUES ('back_in_stock', ?, ?, ?, ?)
            """,
            (
                f"back-in-stock:{book_id}:{movement_id}:{user_id}",
                user_id,
                book_id,
                json.dumps({"available": available}, separators=(",", ":")),
            ),
        )


def _record_movement(
    connection: sqlite3.Connection,
    *,
    book_id: int,
    action: str,
    stock_delta: int = 0,
    reserved_delta: int = 0,
    order_id: int | None = None,
    actor_admin_id: int | None = None,
    reason: str = "",
    source_key: str | None = None,
    was_available: int | None = None,
    record_unlimited_stock: bool = False,
) -> None:
    stock_after = _stock_quantity(connection, book_id)
    if stock_after is None and not record_unlimited_stock:
        return
    reserved_after = _reserved_quantity(connection, book_id)
    cursor = connection.execute(
        """
        INSERT INTO inventory_movements (
            book_id, order_id, action, stock_delta, reserved_delta,
            stock_after, reserved_after, actor_admin_id, reason, source_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            book_id,
            order_id,
            action,
            stock_delta,
            reserved_delta,
            stock_after,
            reserved_after,
            actor_admin_id,
            reason,
            source_key,
        ),
    )
    movement_id = cursor.lastrowid
    _refresh_low_stock_state(connection, book_id, movement_id)
    if was_available is not None:
        _enqueue_back_in_stock(connection, book_id, movement_id, was_available)


def reserve_order_inventory(
    connection: sqlite3.Connection,
    order_id: int,
    cart: Iterable[dict],
) -> None:
    """Reserve finite inventory inside the caller's write transaction."""
    for book_id, quantity in _book_quantities(cart).items():
        book = connection.execute(
            """
            SELECT stock_quantity
            FROM books
            WHERE id = ? AND is_active = 1 AND COALESCE(is_archived, 0) = 0
            """,
            (book_id,),
        ).fetchone()
        if book is None:
            raise InventoryUnavailableError("book is unavailable")
        stock_quantity = book[0]
        if stock_quantity is None:
            continue
        reserved_quantity = _reserved_quantity(connection, book_id)
        if stock_quantity - reserved_quantity < quantity:
            raise InventoryUnavailableError("book is unavailable")
        connection.execute(
            """
            INSERT INTO inventory_reservations (order_id, book_id, quantity, state)
            VALUES (?, ?, ?, 'reserved')
            """,
            (order_id, book_id, quantity),
        )
        _record_movement(
            connection,
            book_id=book_id,
            order_id=order_id,
            action="reservation_created",
            reserved_delta=quantity,
            source_key=f"reserve:{order_id}:{book_id}",
            was_available=stock_quantity - reserved_quantity,
        )


def commit_order_inventory(connection: sqlite3.Connection, order_id: int) -> None:
    """Convert an order's reservation to on-hand inventory consumption."""
    reservations = connection.execute(
        """
        SELECT book_id, quantity
        FROM inventory_reservations
        WHERE order_id = ? AND state = 'reserved'
        """,
        (order_id,),
    ).fetchall()
    for book_id, quantity in reservations:
        stock_before = _stock_quantity(connection, book_id)
        updated = connection.execute(
            """
            UPDATE books
            SET stock_quantity = stock_quantity - ?
            WHERE id = ? AND stock_quantity IS NOT NULL AND stock_quantity >= ?
            """,
            (quantity, book_id, quantity),
        )
        if updated.rowcount != 1:
            raise InventoryUnavailableError("book is unavailable")
        connection.execute(
            """
            UPDATE inventory_reservations
            SET state = 'committed', updated_at = CURRENT_TIMESTAMP
            WHERE order_id = ? AND book_id = ? AND state = 'reserved'
            """,
            (order_id, book_id),
        )
        _record_movement(
            connection,
            book_id=book_id,
            order_id=order_id,
            action="sale_committed",
            stock_delta=-quantity,
            reserved_delta=-quantity,
            source_key=f"commit:{order_id}:{book_id}",
            was_available=stock_before - _reserved_quantity(connection, book_id),
        )


def release_order_inventory(connection: sqlite3.Connection, order_id: int) -> None:
    """Release a reservation or restore already committed finite inventory."""
    reservations = connection.execute(
        """
        SELECT book_id, quantity, state
        FROM inventory_reservations
        WHERE order_id = ? AND state IN ('reserved', 'committed')
        """,
        (order_id,),
    ).fetchall()
    for book_id, quantity, state in reservations:
        stock_before = _stock_quantity(connection, book_id)
        reserved_before = _reserved_quantity(connection, book_id)
        available_before = (
            stock_before - reserved_before if stock_before is not None else None
        )
        if state == "committed":
            connection.execute(
                """
                UPDATE books
                SET stock_quantity = stock_quantity + ?
                WHERE id = ? AND stock_quantity IS NOT NULL
                """,
                (quantity, book_id),
            )
        connection.execute(
            """
            UPDATE inventory_reservations
            SET state = 'released', updated_at = CURRENT_TIMESTAMP
            WHERE order_id = ? AND book_id = ? AND state = ?
            """,
            (order_id, book_id, state),
        )
        _record_movement(
            connection,
            book_id=book_id,
            order_id=order_id,
            action="sale_reversed" if state == "committed" else "reservation_released",
            stock_delta=quantity if state == "committed" else 0,
            reserved_delta=-quantity if state == "reserved" else 0,
            source_key=f"release:{order_id}:{book_id}:{state}",
            was_available=available_before,
        )


def rereserve_order_inventory(connection: sqlite3.Connection, order_id: int) -> None:
    """Re-reserve a released order, failing atomically if stock is exhausted."""
    reservations = connection.execute(
        """
        SELECT book_id, quantity
        FROM inventory_reservations
        WHERE order_id = ? AND state = 'released'
        """,
        (order_id,),
    ).fetchall()
    for book_id, quantity in reservations:
        book = connection.execute(
            """
            SELECT stock_quantity
            FROM books
            WHERE id = ? AND is_active = 1 AND COALESCE(is_archived, 0) = 0
            """,
            (book_id,),
        ).fetchone()
        if book is None:
            raise InventoryUnavailableError("book is unavailable")
        stock_quantity = book[0]
        if stock_quantity is not None and stock_quantity - _reserved_quantity(connection, book_id) < quantity:
            raise InventoryUnavailableError("book is unavailable")
    for book_id, quantity in reservations:
        stock_quantity = _stock_quantity(connection, book_id)
        if stock_quantity is None:
            continue
        reserved_before = _reserved_quantity(connection, book_id)
        connection.execute(
            """
            UPDATE inventory_reservations
            SET state = 'reserved', updated_at = CURRENT_TIMESTAMP
            WHERE order_id = ? AND book_id = ? AND state = 'released'
            """,
            (order_id, book_id),
        )
        _record_movement(
            connection,
            book_id=book_id,
            order_id=order_id,
            action="reservation_created",
            reserved_delta=quantity,
            source_key=f"rereserve:{order_id}:{book_id}",
            was_available=stock_quantity - reserved_before,
        )


def adjust_stock_sync(
    book_id: int,
    quantity: int,
    admin_id: int,
    reason: str,
    *,
    actor_role: str = "owner",
) -> bool:
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError("quantity must be an integer")
    if reason not in {"received", "recount", "damaged", "return"}:
        raise ValueError("unsupported inventory operation")
    if reason == "recount":
        if quantity < 0:
            raise ValueError("recount quantity must be non-negative")
    elif quantity <= 0:
        raise ValueError("operation quantity must be positive")

    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        previous = _stock_quantity(database, book_id)
        if previous is None:
            if database.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone() is None:
                database.rollback()
                return False
            raise ValueError("finite stock is required for inventory operations")
        reserved = _reserved_quantity(database, book_id)
        if reason in {"received", "return"}:
            stock_quantity = previous + quantity
        elif reason == "damaged":
            stock_quantity = previous - quantity
        else:
            stock_quantity = quantity
        if stock_quantity < reserved:
            raise InventoryUnavailableError("stock cannot be lower than reservations")

        database.execute(
            "UPDATE books SET stock_quantity = ? WHERE id = ?", (stock_quantity, book_id)
        )
        delta = stock_quantity - previous
        _record_movement(
            database,
            book_id=book_id,
            action="manual_adjustment",
            stock_delta=delta,
            actor_admin_id=admin_id,
            reason=reason,
            source_key=None,
            was_available=previous - reserved,
        )
        from db.audit import append_audit_event

        append_audit_event(
            database,
            actor_user_id=admin_id,
            actor_role=actor_role,
            source="mini_app",
            action="inventory.stock.adjusted",
            entity_type="book",
            entity_id=book_id,
            details={
                "old_stock": previous,
                "new_stock": stock_quantity,
                "count": quantity,
                "reason_code": reason,
            },
        )
        database.commit()
        return True
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


async def adjust_stock(
    book_id: int, quantity: int, admin_id: int, reason: str
) -> bool:
    return await asyncio.to_thread(
        adjust_stock_sync, book_id, quantity, admin_id, reason
    )


def set_stock_quantity_sync(
    book_id: int,
    stock_quantity: int | None,
    admin_id: int,
    *,
    actor_role: str = "owner",
) -> bool:
    if isinstance(stock_quantity, bool) or (
        stock_quantity is not None and (
            not isinstance(stock_quantity, int) or stock_quantity < 0
        )
    ):
        raise ValueError("stock quantity must be a non-negative integer or unlimited")
    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT stock_quantity FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if row is None:
            database.rollback()
            return False
        previous = row[0]
        reserved = _reserved_quantity(database, book_id)
        if stock_quantity is not None and stock_quantity < reserved:
            raise InventoryUnavailableError("stock cannot be lower than reservations")
        if previous == stock_quantity:
            database.commit()
            return True
        if previous is not None and stock_quantity is None and reserved:
            raise InventoryUnavailableError("stock mode cannot change while reservations exist")
        was_available = previous - reserved if previous is not None else None
        database.execute(
            "UPDATE books SET stock_quantity = ? WHERE id = ?",
            (stock_quantity, book_id),
        )
        if previous is not None and stock_quantity is not None:
            _record_movement(
                database,
                book_id=book_id,
                action="manual_adjustment",
                stock_delta=stock_quantity - previous,
                actor_admin_id=admin_id,
                reason="recount",
                was_available=was_available,
            )
            action = "inventory.stock.adjusted"
            details = {
                "old_stock": previous,
                "new_stock": stock_quantity,
                "reason_code": "recount",
            }
        else:
            _record_movement(
                database,
                book_id=book_id,
                action="stock_mode_changed",
                actor_admin_id=admin_id,
                reason="unlimited" if stock_quantity is None else "finite",
                was_available=was_available,
                record_unlimited_stock=stock_quantity is None,
            )
            action = "inventory.stock.mode_changed"
            details = {
                "old_stock": previous,
                "new_stock": stock_quantity,
                "reason_code": "unlimited" if stock_quantity is None else "finite",
            }
        from db.audit import append_audit_event

        append_audit_event(
            database,
            actor_user_id=admin_id,
            actor_role=actor_role,
            source="telegram",
            action=action,
            entity_type="book",
            entity_id=book_id,
            details=details,
        )
        database.commit()
        return True
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


async def set_stock_quantity(
    book_id: int, stock_quantity: int | None, admin_id: int
) -> bool:
    return await asyncio.to_thread(
        set_stock_quantity_sync, book_id, stock_quantity, admin_id
    )


def available_quantity(connection: sqlite3.Connection, book_id: int) -> int | None:
    """Return remaining finite stock or None for unlimited/missing books."""
    stock_quantity = _stock_quantity(connection, book_id)
    if stock_quantity is None:
        return None
    return max(0, stock_quantity - _reserved_quantity(connection, book_id))


def inventory_summary_sync(book_id: int) -> dict | None:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        book = database.execute(
            "SELECT stock_quantity FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if not book:
            return None
        stock_quantity = book["stock_quantity"]
        reserved = _reserved_quantity(database, book_id)
        threshold = database.execute(
            "SELECT threshold, is_low FROM inventory_low_stock_state WHERE book_id = ?",
            (book_id,),
        ).fetchone()
        return {
            "stock_quantity": stock_quantity,
            "reserved_quantity": reserved,
            "available_quantity": None if stock_quantity is None else max(0, stock_quantity - reserved),
            "low_stock_threshold": threshold[0] if threshold else DEFAULT_LOW_STOCK_THRESHOLD,
            "is_low_stock": bool(threshold[1]) if threshold else False,
        }
    finally:
        database.close()


async def inventory_summary(book_id: int) -> dict | None:
    return await asyncio.to_thread(inventory_summary_sync, book_id)


def recent_movements_sync(book_id: int, limit: int = 50) -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT * FROM inventory_movements
            WHERE book_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (book_id, max(1, min(limit, 100))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        database.close()


async def recent_movements(book_id: int, limit: int = 50) -> list[dict]:
    return await asyncio.to_thread(recent_movements_sync, book_id)


def list_inventory_sync(
    *,
    limit: int = 50,
    offset: int = 0,
    query: str = "",
    low_stock_only: bool = False,
) -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        conditions = ["b.is_active = 1", "COALESCE(b.is_archived, 0) = 0"]
        params: list[object] = []
        if query:
            conditions.append("LOWER(b.title) LIKE LOWER(?)")
            params.append(f"%{query[:120]}%")
        if low_stock_only:
            conditions.append("b.stock_quantity IS NOT NULL")
            conditions.append("b.stock_quantity - COALESCE(r.reserved_quantity, 0) <= COALESCE(s.threshold, ?)")
            params.append(DEFAULT_LOW_STOCK_THRESHOLD)
        rows = database.execute(
            f"""
            SELECT b.id, b.title, b.stock_quantity,
                   COALESCE(r.reserved_quantity, 0) AS reserved_quantity,
                   s.threshold AS low_stock_threshold,
                   COALESCE(s.is_low, 0) AS is_low_stock
            FROM books b
            LEFT JOIN (
                SELECT book_id, SUM(quantity) AS reserved_quantity
                FROM inventory_reservations
                WHERE state = 'reserved'
                GROUP BY book_id
            ) r ON r.book_id = b.id
            LEFT JOIN inventory_low_stock_state s ON s.book_id = b.id
            WHERE {' AND '.join(conditions)}
            ORDER BY b.sort_order ASC, b.id ASC
            LIMIT ? OFFSET ?
            """,
            (*params, max(1, min(limit, 100)), max(0, offset)),
        ).fetchall()
        inventory = []
        for row in rows:
            result = dict(row)
            result["available_quantity"] = (
                None if result["stock_quantity"] is None
                else max(0, result["stock_quantity"] - result["reserved_quantity"])
            )
            result["low_stock_threshold"] = (
                result["low_stock_threshold"]
                if result["low_stock_threshold"] is not None
                else DEFAULT_LOW_STOCK_THRESHOLD
            )
            result["is_low_stock"] = bool(result["is_low_stock"])
            inventory.append(result)
        return inventory
    finally:
        database.close()


def list_inventory_movements_sync(
    *,
    limit: int = 50,
    before_id: int | None = None,
    book_id: int | None = None,
) -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        conditions = []
        params: list[object] = []
        if before_id is not None:
            conditions.append("m.id < ?")
            params.append(before_id)
        if book_id is not None:
            conditions.append("m.book_id = ?")
            params.append(book_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = database.execute(
            f"""
            SELECT m.id, m.book_id, b.title AS book_title, m.order_id, m.action,
                   m.stock_delta, m.reserved_delta, m.stock_after, m.reserved_after,
                   m.actor_admin_id, m.reason, m.created_at
            FROM inventory_movements m
            JOIN books b ON b.id = m.book_id
            {where}
            ORDER BY m.id DESC LIMIT ?
            """,
            (*params, max(1, min(limit, 100))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        database.close()


def list_inventory_movements_export_sync(*, limit: int = 10_000) -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT m.id, m.book_id, b.title AS book_title, m.order_id, m.action,
                   m.stock_delta, m.reserved_delta, m.stock_after, m.reserved_after,
                   m.actor_admin_id, m.reason, m.created_at
            FROM inventory_movements m
            JOIN books b ON b.id = m.book_id
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 10_000)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        database.close()


def claim_notification_outbox_sync(limit: int = 20, lease_seconds: int = 300) -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        rows = database.execute(
            """
            SELECT * FROM notification_outbox
            WHERE state = 'pending'
               OR (state = 'processing' AND claimed_at < datetime('now', ?))
            ORDER BY id ASC
            LIMIT ?
            """,
            (f"-{lease_seconds} seconds", max(1, min(limit, 100))),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            database.execute(
                f"""
                UPDATE notification_outbox
                SET state = 'processing', claimed_at = CURRENT_TIMESTAMP, attempts = attempts + 1
                WHERE id IN ({placeholders})
                """,
                ids,
            )
        database.commit()
        return [dict(row) for row in rows]
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


async def claim_notification_outbox(limit: int = 20, lease_seconds: int = 300) -> list[dict]:
    return await asyncio.to_thread(claim_notification_outbox_sync, limit, lease_seconds)


async def mark_notification_sent(notification_id: int) -> None:
    database = connect()
    try:
        database.execute(
            """
            UPDATE notification_outbox
            SET state = 'sent', sent_at = CURRENT_TIMESTAMP, claimed_at = NULL
            WHERE id = ? AND state = 'processing'
            """,
            (notification_id,),
        )
        database.commit()
    finally:
        database.close()


async def release_notification_claim(notification_id: int, error: str = "") -> None:
    database = connect()
    try:
        database.execute(
            """
            UPDATE notification_outbox
            SET state = 'pending', claimed_at = NULL, last_error = ?
            WHERE id = ? AND state = 'processing'
            """,
            (error[:200], notification_id),
        )
        database.commit()
    finally:
        database.close()


async def revoke_back_in_stock_subscription(user_id: int, book_id: int) -> None:
    database = connect()
    try:
        database.execute(
            """
            UPDATE back_in_stock_subscriptions
            SET active = 0, revoked_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND book_id = ? AND active = 1
            """,
            (user_id, book_id),
        )
        database.commit()
    finally:
        database.close()
