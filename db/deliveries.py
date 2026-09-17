from __future__ import annotations

import asyncio
import re
import sqlite3
from urllib.parse import urlencode

from db.connection import connection


METHOD_SDEK_PICKUP = "sdek_pickup"
METHOD_RUSSIAN_POST_PICKUP = "russian_post_pickup"
METHOD_SELF_PICKUP = "self_pickup"

CARRIER_NONE = "none"
CARRIER_SDEK = "sdek"
CARRIER_RUSSIAN_POST = "russian_post"

SHIPMENT_AWAITING_PAYMENT = "awaiting_payment"
SHIPMENT_PREPARING = "preparing"
SHIPMENT_PACKED = "packed"
SHIPMENT_SHIPPED = "shipped"
SHIPMENT_READY_FOR_PICKUP = "ready_for_pickup"
SHIPMENT_DELIVERED = "delivered"
SHIPMENT_RETURNED = "returned"
SHIPMENT_CANCELLED = "cancelled"

SHIPMENT_LABELS = {
    SHIPMENT_AWAITING_PAYMENT: "Ожидает оплаты",
    SHIPMENT_PREPARING: "Готовится к отправке",
    SHIPMENT_PACKED: "Собран",
    SHIPMENT_SHIPPED: "Отправлен",
    SHIPMENT_READY_FOR_PICKUP: "Готов к выдаче",
    SHIPMENT_DELIVERED: "Выдан / доставлен",
    SHIPMENT_RETURNED: "Возвращён",
    SHIPMENT_CANCELLED: "Доставка отменена",
}
METHOD_LABELS = {
    METHOD_SDEK_PICKUP: "СДЭК — пункт выдачи",
    METHOD_RUSSIAN_POST_PICKUP: "Почта России — отделение",
    METHOD_SELF_PICKUP: "Самовывоз",
}

_SDEK_TRACKING_PATTERN = re.compile(r"[A-Z0-9-]{5,40}\Z")
_RUSSIAN_POST_TRACKING_PATTERN = re.compile(r"\d{14}\Z")


class DeliveryTransitionError(ValueError):
    pass


def method_supports_tracking(method: str) -> bool:
    return method in {METHOD_SDEK_PICKUP, METHOD_RUSSIAN_POST_PICKUP}


def tracking_carrier_for_method(method: str) -> str:
    if method == METHOD_SDEK_PICKUP:
        return CARRIER_SDEK
    if method == METHOD_RUSSIAN_POST_PICKUP:
        return CARRIER_RUSSIAN_POST
    raise ValueError("Трек-номер недоступен для этого способа доставки")


def normalize_sdek_tracking_number(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Введите корректный номер отправления СДЭК")
    normalized = value.strip().upper()
    if not _SDEK_TRACKING_PATTERN.fullmatch(normalized):
        raise ValueError("Введите корректный номер отправления СДЭК")
    return normalized


def normalize_russian_post_tracking_number(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Введите 14-значный номер отправления Почты России")
    normalized = "".join(value.split())
    if not _RUSSIAN_POST_TRACKING_PATTERN.fullmatch(normalized):
        raise ValueError("Введите 14-значный номер отправления Почты России")
    return normalized


def normalize_tracking_number(method: str, value: object) -> str:
    if method == METHOD_SDEK_PICKUP:
        return normalize_sdek_tracking_number(value)
    if method == METHOD_RUSSIAN_POST_PICKUP:
        return normalize_russian_post_tracking_number(value)
    raise ValueError("Трек-номер недоступен для самовывоза")


def sdek_tracking_url(tracking_number: str) -> str:
    return "https://www.cdek.ru/ru/tracking?" + urlencode({"order_id": tracking_number})


def russian_post_tracking_url(_: str) -> str:
    return "https://www.pochta.ru/tracking"


def tracking_url(carrier: str, tracking_number: str) -> str | None:
    if carrier == CARRIER_SDEK:
        return sdek_tracking_url(tracking_number)
    if carrier == CARRIER_RUSSIAN_POST:
        return russian_post_tracking_url(tracking_number)
    return None


def _allowed_transitions(method: str, current: str) -> set[str]:
    if current == SHIPMENT_AWAITING_PAYMENT:
        return {SHIPMENT_PREPARING, SHIPMENT_CANCELLED}
    if current == SHIPMENT_PREPARING:
        return {SHIPMENT_PACKED, SHIPMENT_CANCELLED}
    if current == SHIPMENT_PACKED:
        if method == METHOD_SELF_PICKUP:
            return {SHIPMENT_READY_FOR_PICKUP, SHIPMENT_CANCELLED}
        return {SHIPMENT_SHIPPED, SHIPMENT_CANCELLED}
    if current == SHIPMENT_SHIPPED:
        return {SHIPMENT_READY_FOR_PICKUP, SHIPMENT_DELIVERED, SHIPMENT_RETURNED}
    if current == SHIPMENT_READY_FOR_PICKUP:
        return {SHIPMENT_DELIVERED, SHIPMENT_RETURNED}
    return set()


def safe_delivery_summary(row: sqlite3.Row | dict | None) -> dict | None:
    if not row:
        return None
    delivery = dict(row)
    method = delivery["method"]
    tracking_number = delivery.get("tracking_number")
    carrier = delivery.get("tracking_carrier")
    url = tracking_url(carrier, tracking_number) if tracking_number else None
    tracking = None
    if url:
        tracking = {
            "carrier": carrier,
            "number": tracking_number,
            "url": url,
        }
    public_instructions = delivery.get("public_instructions_snapshot") or ""
    return {
        "method": method,
        "method_label": METHOD_LABELS[method],
        "price": delivery["delivery_price"],
        "shipment_status": delivery["shipment_status"],
        "shipment_label": SHIPMENT_LABELS[delivery["shipment_status"]],
        "tracking": tracking,
        "public_instructions": public_instructions or None,
        "pii_redacted": delivery.get("pii_redacted_at") is not None,
    }


def transition_delivery_sync(
    database: sqlite3.Connection,
    order_id: int,
    target_status: str,
    *,
    admin_id: int | None = None,
    tracking_number: str | None = None,
) -> bool:
    row = database.execute(
        "SELECT * FROM order_deliveries WHERE order_id = ?", (order_id,)
    ).fetchone()
    if row is None:
        return False
    delivery = dict(row) if isinstance(row, sqlite3.Row) else {
        "method": row[1],
        "shipment_status": row[5],
        "tracking_carrier": row[6],
        "tracking_number": row[7],
    }
    method = delivery["method"]
    current = delivery["shipment_status"]
    if target_status not in _allowed_transitions(method, current):
        raise DeliveryTransitionError("Недопустимый статус доставки")
    if target_status == SHIPMENT_SHIPPED:
        normalized_tracking = normalize_tracking_number(method, tracking_number)
        tracking_carrier = tracking_carrier_for_method(method)
    else:
        normalized_tracking = delivery["tracking_number"]
        tracking_carrier = delivery["tracking_carrier"]
    database.execute(
        """
        UPDATE order_deliveries
        SET shipment_status = ?, tracking_carrier = ?, tracking_number = ?,
            tracking_set_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE tracking_set_at END,
            delivered_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE delivered_at END,
            updated_at = CURRENT_TIMESTAMP, updated_by_admin_id = COALESCE(?, updated_by_admin_id)
        WHERE order_id = ?
        """,
        (
            target_status,
            tracking_carrier,
            normalized_tracking,
            target_status == SHIPMENT_SHIPPED,
            target_status == SHIPMENT_DELIVERED,
            admin_id,
            order_id,
        ),
    )
    return True


def set_tracking_sync(
    database: sqlite3.Connection,
    order_id: int,
    tracking_number: str,
    admin_id: int,
) -> bool:
    row = database.execute(
        "SELECT method FROM order_deliveries WHERE order_id = ?", (order_id,)
    ).fetchone()
    if not row:
        return False
    method = row[0]
    if not method_supports_tracking(method):
        return False
    normalized = normalize_tracking_number(method, tracking_number)
    cursor = database.execute(
        """
        UPDATE order_deliveries
        SET tracking_carrier = ?, tracking_number = ?, tracking_set_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP, updated_by_admin_id = ?
        WHERE order_id = ? AND method = ?
          AND shipment_status IN ('packed', 'shipped', 'ready_for_pickup')
        """,
        (tracking_carrier_for_method(method), normalized, admin_id, order_id, method),
    )
    return cursor.rowcount == 1


def set_sdek_tracking_sync(
    database: sqlite3.Connection,
    order_id: int,
    tracking_number: str,
    admin_id: int,
) -> bool:
    return set_tracking_sync(database, order_id, tracking_number, admin_id)


async def get_delivery(order_id: int) -> dict | None:
    async with connection() as database:
        database.row_factory = sqlite3.Row
        cursor = await database.execute(
            "SELECT * FROM order_deliveries WHERE order_id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_delivery_summary(order_id: int) -> dict | None:
    delivery = await get_delivery(order_id)
    return safe_delivery_summary(delivery)


async def get_delivery_for_admin(order_id: int) -> dict | None:
    return await get_delivery(order_id)


async def update_delivery_status(
    order_id: int,
    target_status: str,
    *,
    admin_id: int,
    tracking_number: str | None = None,
) -> bool:
    def operation() -> bool:
        from db.schema import connect

        database = connect()
        database.row_factory = sqlite3.Row
        try:
            database.execute("BEGIN IMMEDIATE")
            changed = transition_delivery_sync(
                database,
                order_id,
                target_status,
                admin_id=admin_id,
                tracking_number=tracking_number,
            )
            database.commit()
            return changed
        except Exception:
            database.rollback()
            raise
        finally:
            database.close()

    return await asyncio.to_thread(operation)


async def update_tracking(order_id: int, tracking_number: str, admin_id: int) -> bool:
    def operation() -> bool:
        from db.schema import connect

        database = connect()
        try:
            database.execute("BEGIN IMMEDIATE")
            changed = set_tracking_sync(database, order_id, tracking_number, admin_id)
            database.commit()
            return changed
        except Exception:
            database.rollback()
            raise
        finally:
            database.close()

    return await asyncio.to_thread(operation)


async def update_sdek_tracking(order_id: int, tracking_number: str, admin_id: int) -> bool:
    return await update_tracking(order_id, tracking_number, admin_id)


def redact_expired_delivery_pii_sync() -> int:
    from config import settings
    from db.schema import connect

    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        cursor = database.execute(
            """
            UPDATE order_deliveries
            SET destination_encrypted = '', pii_redacted_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE shipment_status = 'delivered'
              AND delivered_at IS NOT NULL
              AND date(delivered_at) < date('now', ?)
              AND pii_redacted_at IS NULL
            """,
            (f"-{settings.DELIVERY_PII_RETENTION_DAYS} days",),
        )
        database.commit()
        return cursor.rowcount
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


async def redact_expired_delivery_pii() -> int:
    return await asyncio.to_thread(redact_expired_delivery_pii_sync)
