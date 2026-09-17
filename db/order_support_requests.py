from __future__ import annotations

import asyncio
import json
import sqlite3

from db.orders import apply_order_receipt
from db.schema import connect


TERMINAL_ORDER_STATUSES = {"completed", "cancelled"}


def _history_text(receipt: dict) -> str:
    item_lines = []
    for item in receipt["items"]:
        quantity = item["quantity"]
        quantity_suffix = f" ×{quantity}" if quantity > 1 else ""
        item_lines.append(f"• {item['title']}{quantity_suffix} — {item['line_total']} ₽")
    lines = [
        f"Обращение по заказу #{receipt['id']}",
        "",
        "Состав:",
        *item_lines,
        "",
        f"Товары: {receipt['items_subtotal']} ₽",
    ]
    if receipt["delivery_price"]:
        lines.append(f"Доставка: {receipt['delivery_price']} ₽")
    if receipt["promo_code_snapshot"]:
        lines.append(f"Промокод: {receipt['promo_code_snapshot']}")
    if receipt["promo_discount"]:
        lines.append(f"Скидка промокода: −{receipt['promo_discount']} ₽")
    if receipt["bonus_discount"]:
        lines.append(f"Скидка бонуса: −{receipt['bonus_discount']} ₽")
    if receipt["total_discount"]:
        lines.append(f"Общая скидка: −{receipt['total_discount']} ₽")
    lines.append(f"Итого: {receipt['total']} ₽")
    return "\n".join(lines)


def enqueue_order_support_request_sync(order_id: int, user_id: int) -> tuple[dict | None, str]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        order = database.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)
        ).fetchone()
        if not order:
            database.rollback()
            return None, "not_found"
        if order["status"] in TERMINAL_ORDER_STATUSES:
            database.rollback()
            return None, "terminal"
        existing = database.execute(
            "SELECT id, state FROM order_support_requests WHERE order_id = ?", (order_id,)
        ).fetchone()
        if existing:
            database.commit()
            return {"request_id": existing["id"], "state": existing["state"], "reused": True}, "ok"

        items = database.execute(
            "SELECT book_id, title, price FROM order_items WHERE order_id = ? ORDER BY id ASC",
            (order_id,),
        ).fetchall()
        receipt = apply_order_receipt(dict(order), items)
        snapshot = {
            key: receipt[key]
            for key in (
                "id", "created_at", "status", "payment_method", "total", "items",
                "items_subtotal", "delivery_price", "promo_code_snapshot", "promo_discount",
                "bonus_discount", "total_discount",
            )
        }
        cursor = database.execute(
            """
            INSERT INTO order_support_requests (order_id, user_id, receipt_json)
            VALUES (?, ?, ?)
            """,
            (order_id, user_id, json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))),
        )
        database.execute(
            """
            INSERT INTO support_messages (user_id, role, sender_name, text)
            VALUES (?, 'user', ?, ?)
            """,
            (user_id, str(order["user_name"])[:255], _history_text(snapshot)),
        )
        database.execute(
            "INSERT OR IGNORE INTO users (user_id, user_name) VALUES (?, ?)",
            (user_id, str(order["user_name"])[:255]),
        )
        database.execute(
            "UPDATE users SET is_support_active = 1 WHERE user_id = ?", (user_id,)
        )
        database.commit()
        return {"request_id": cursor.lastrowid, "state": "pending", "reused": False}, "ok"
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def claim_order_support_requests_sync(limit: int = 20, lease_seconds: int = 300) -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        rows = database.execute(
            """
            SELECT * FROM order_support_requests
            WHERE state = 'pending'
               OR (state = 'processing' AND claimed_at < datetime('now', ?))
            ORDER BY id ASC LIMIT ?
            """,
            (f"-{lease_seconds} seconds", max(1, min(limit, 100))),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            database.execute(
                f"""
                UPDATE order_support_requests
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


def _update_request_sync(request_id: int, state: str, error: str = "") -> None:
    database = connect()
    try:
        if state == "sent":
            database.execute(
                """
                UPDATE order_support_requests
                SET state = 'sent', sent_at = CURRENT_TIMESTAMP, claimed_at = NULL, last_error = ''
                WHERE id = ? AND state = 'processing'
                """,
                (request_id,),
            )
        elif state == "failed":
            database.execute(
                """
                UPDATE order_support_requests
                SET state = 'failed', claimed_at = NULL, last_error = ?
                WHERE id = ? AND state = 'processing'
                """,
                (error[:120], request_id),
            )
        else:
            database.execute(
                """
                UPDATE order_support_requests
                SET state = 'pending', claimed_at = NULL, last_error = ?
                WHERE id = ? AND state = 'processing'
                """,
                (error[:120], request_id),
            )
        database.commit()
    finally:
        database.close()


async def claim_order_support_requests(limit: int = 20, lease_seconds: int = 300) -> list[dict]:
    return await asyncio.to_thread(claim_order_support_requests_sync, limit, lease_seconds)


async def mark_order_support_request_sent(request_id: int) -> None:
    await asyncio.to_thread(_update_request_sync, request_id, "sent")


async def release_order_support_request(request_id: int, error: str = "") -> None:
    await asyncio.to_thread(_update_request_sync, request_id, "pending", error)


async def fail_order_support_request(request_id: int, error: str = "") -> None:
    await asyncio.to_thread(_update_request_sync, request_id, "failed", error)
