"""Модуль для работы с заказами"""
import asyncio
import aiosqlite
from db.connection import connection
from db.deliveries import (
    DeliveryTransitionError,
    SHIPMENT_DELIVERED,
    safe_delivery_summary,
    transition_delivery_sync,
)
from db.inventory import (
    InventoryUnavailableError,
    commit_order_inventory,
    release_order_inventory,
    rereserve_order_inventory,
)
from db.schema import connect


PAID_STATUSES = ('paid', 'confirmed', 'completed')
PENDING_STATUSES = ('new', 'payment_pending', 'paid')
AWAITING_PAYMENT_STATUSES = (
    'awaiting_payment',
    'awaiting_stars_payment',
    'awaiting_yookassa_payment',
)
AUTOMATIC_PAYMENT_METHODS = ('stars', 'yookassa')
STATUS_LABELS = {
    'new': 'Новый',
    'awaiting_payment': 'Ожидает оплаты',
    'awaiting_stars_payment': 'Ожидает оплаты (Stars)',
    'awaiting_yookassa_payment': 'Ожидает оплаты (ЮKassa)',
    'payment_pending': 'Ожидает подтверждения',
    'paid': 'Оплачен',
    'confirmed': 'Подтверждён',
    'completed': 'Выполнен',
    'cancelled': 'Отменён',
}
_ORDER_SORTS = {
    'new': 'created_at DESC, id DESC',
    'old': 'created_at ASC, id ASC',
}


async def create_order(user_id: int, user_name: str, cart: list, total: int) -> int:
    """Создать новый заказ"""
    async with connection() as db:
        cursor = await db.execute(
            "INSERT INTO orders (user_id, user_name, total, status) VALUES (?, ?, ?, 'new')",
            (user_id, user_name, total)
        )
        order_id = cursor.lastrowid

        for book in cart:
            await db.execute(
                "INSERT INTO order_items (order_id, book_id, title, price) VALUES (?, ?, ?, ?)",
                (order_id, book['id'], book['title'], book['price'])
            )

        await db.commit()
    return order_id


def aggregate_order_items(items) -> list[dict]:
    """Aggregate immutable order item snapshots without consulting the live catalog."""
    lines: dict[tuple[int, str, int], dict] = {}
    for item in items:
        row = dict(item)
        key = (row["book_id"], row["title"], row["price"])
        if key not in lines:
            lines[key] = {
                "book_id": row["book_id"],
                "title": row["title"],
                "price": row["price"],
                "quantity": 0,
                "line_total": 0,
            }
        lines[key]["quantity"] += 1
        lines[key]["line_total"] += row["price"]
    return list(lines.values())


def apply_order_receipt(order: dict, items) -> dict:
    """Attach the persisted financial snapshot and aggregated historical lines."""
    receipt = dict(order)
    receipt["items"] = aggregate_order_items(items)
    historical_subtotal = sum(item["line_total"] for item in receipt["items"])
    receipt["items_subtotal"] = receipt.get("items_subtotal") or historical_subtotal
    receipt["delivery_price"] = receipt.get("delivery_price") or 0
    receipt["promo_code_snapshot"] = receipt.get("promo_code_snapshot") or None
    receipt["promo_discount"] = receipt.get("promo_discount") or 0
    receipt["bonus_discount"] = receipt.get("bonus_discount") or 0
    receipt["total_discount"] = receipt["promo_discount"] + receipt["bonus_discount"]
    return receipt


def format_order_receipt_html(order: dict) -> str:
    """Render immutable order lines and financial adjustments for Telegram HTML."""
    from html import escape

    lines = []
    for item in order.get("items", []):
        quantity = item["quantity"]
        suffix = f" ×{quantity}" if quantity > 1 else ""
        lines.append(f"• {escape(str(item['title']))}{suffix} — {item['line_total']} ₽")
    receipt = "📚 Товары:\n" + ("\n".join(lines) if lines else "• Не сохранены")
    receipt += f"\n\nТовары: <b>{order.get('items_subtotal', 0)} ₽</b>"
    if order.get("delivery_price"):
        receipt += f"\nДоставка: {order['delivery_price']} ₽"
    if order.get("promo_code_snapshot"):
        receipt += f"\nПромокод: <code>{escape(str(order['promo_code_snapshot']))}</code>"
    if order.get("promo_discount"):
        receipt += f"\nСкидка промокода: −{order['promo_discount']} ₽"
    if order.get("bonus_discount"):
        receipt += f"\nСкидка бонуса: −{order['bonus_discount']} ₽"
    if order.get("total_discount"):
        receipt += f"\nОбщая скидка: <b>−{order['total_discount']} ₽</b>"
    receipt += f"\nИтого: <b>{order['total']} ₽</b>"
    return receipt


async def get_order(order_id: int) -> dict:
    """Получить заказ по ID"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_order_full(order_id: int) -> dict:
    """Получить заказ со всеми товарами"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        order = await cursor.fetchone()
        if not order:
            return None

        cursor = await db.execute(
            "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
        )
        items = await cursor.fetchall()
        cursor = await db.execute(
            "SELECT * FROM order_deliveries WHERE order_id = ?", (order_id,)
        )
        delivery = await cursor.fetchone()

        # Получаем ID сообщений уведомлений
        admin_notification_ids = []
        if 'admin_notification_ids' in order.keys() and order['admin_notification_ids']:
            try:
                import json
                admin_notification_ids = json.loads(order['admin_notification_ids'])
            except Exception:
                admin_notification_ids = []

        order_data = apply_order_receipt(dict(order), items)
        order_data.update({
            'admin_notification_ids': admin_notification_ids,
            'delivery': dict(delivery) if delivery else None,
            'delivery_summary': safe_delivery_summary(delivery) if delivery else None,
        })
        return order_data


async def get_user_orders(user_id: int, limit: int = 10) -> list:
    """Получить заказы пользователя"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT o.*, d.method, d.public_instructions_snapshot, d.delivery_price, d.shipment_status,
                   d.tracking_carrier, d.tracking_number, d.pii_redacted_at
            FROM orders o
            LEFT JOIN order_deliveries d ON d.order_id = o.id
            WHERE o.user_id = ?
            ORDER BY o.created_at DESC, o.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        order_ids = [row["id"] for row in rows]
        items_by_order = {order_id: [] for order_id in order_ids}
        if order_ids:
            placeholders = ",".join("?" for _ in order_ids)
            cursor = await db.execute(
                f"SELECT order_id, book_id, title, price FROM order_items WHERE order_id IN ({placeholders}) ORDER BY id ASC",
                order_ids,
            )
            for item in await cursor.fetchall():
                items_by_order[item["order_id"]].append(item)
        orders = []
        for row in rows:
            order = apply_order_receipt(dict(row), items_by_order[row["id"]])
            if order.get("method"):
                delivery = {
                    "method": order.pop("method"),
                    "public_instructions_snapshot": order.pop("public_instructions_snapshot"),
                    "delivery_price": order.pop("delivery_price"),
                    "shipment_status": order.pop("shipment_status"),
                    "tracking_carrier": order.pop("tracking_carrier"),
                    "tracking_number": order.pop("tracking_number"),
                    "pii_redacted_at": order.pop("pii_redacted_at"),
                }
                order["delivery"] = safe_delivery_summary(delivery)
                order["delivery_price"] = delivery["delivery_price"]
            else:
                for key in (
                    "method", "public_instructions_snapshot", "shipment_status", "tracking_carrier",
                    "tracking_number", "pii_redacted_at"
                ):
                    order.pop(key, None)
                order["delivery"] = None
            orders.append(order)
        return orders


def transition_order_status_sync(
    order_id: int,
    status: str,
    *,
    expected_statuses: tuple[str, ...] | None = None,
) -> bool:
    """Apply an order transition and matching inventory movement atomically."""
    if status not in STATUS_LABELS:
        raise ValueError("unsupported order status")
    database = connect()
    database.row_factory = None
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if row is None or (expected_statuses and row[0] not in expected_statuses):
            database.rollback()
            return False
        current_status = row[0]
        if current_status == status:
            database.rollback()
            return False
        delivery = database.execute(
            "SELECT shipment_status FROM order_deliveries WHERE order_id = ?", (order_id,)
        ).fetchone()
        if status == "completed" and delivery and delivery[0] != "delivered":
            raise DeliveryTransitionError("Завершите доставку перед выполнением заказа")
        if current_status == "cancelled" and status == "new" and delivery and delivery[0] != "cancelled":
            raise DeliveryTransitionError("Нельзя восстановить заказ после отправки")
        if status in PAID_STATUSES:
            commit_order_inventory(database, order_id)
        if status == "cancelled":
            delivery = database.execute(
                "SELECT shipment_status FROM order_deliveries WHERE order_id = ?", (order_id,)
            ).fetchone()
            if delivery and delivery[0] in {"shipped", "ready_for_pickup", "delivered"}:
                raise DeliveryTransitionError("Нельзя отменить заказ после отправки")
            release_order_inventory(database, order_id)
            database.execute(
                """
                UPDATE order_deliveries
                SET shipment_status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE order_id = ?
                  AND shipment_status IN ('awaiting_payment', 'preparing', 'packed')
                """,
                (order_id,),
            )
        elif current_status == "cancelled" and status == "new":
            rereserve_order_inventory(database, order_id)
            database.execute(
                """
                UPDATE order_deliveries
                SET shipment_status = 'awaiting_payment', updated_at = CURRENT_TIMESTAMP
                WHERE order_id = ? AND shipment_status = 'cancelled'
                """,
                (order_id,),
            )
        elif status == "confirmed":
            database.execute(
                """
                UPDATE order_deliveries
                SET shipment_status = 'preparing', updated_at = CURRENT_TIMESTAMP
                WHERE order_id = ? AND shipment_status = 'awaiting_payment'
                """,
                (order_id,),
            )

        actionable = status in PENDING_STATUSES
        database.execute(
            """
            UPDATE orders
            SET status = ?,
                paid_at = CASE
                    WHEN ? IN ('paid', 'confirmed', 'completed')
                    THEN COALESCE(paid_at, CURRENT_TIMESTAMP)
                    ELSE paid_at
                END,
                new_order_notified = CASE WHEN ? THEN 0 ELSE new_order_notified END,
                new_order_notification_state = CASE WHEN ? THEN 'pending' ELSE new_order_notification_state END,
                new_order_notification_claimed_at = CASE WHEN ? THEN NULL ELSE new_order_notification_claimed_at END
            WHERE id = ?
            """,
            (status, status, actionable, actionable, actionable, order_id),
        )
        database.commit()
        return True
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def complete_delivery_and_order_sync(order_id: int, admin_id: int) -> bool:
    """Mark a confirmed order delivered and completed in one transaction."""
    database = connect()
    database.row_factory = None
    try:
        database.execute("BEGIN IMMEDIATE")
        order = database.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None or order[0] != "confirmed":
            database.rollback()
            return False
        changed = transition_delivery_sync(
            database,
            order_id,
            SHIPMENT_DELIVERED,
            admin_id=admin_id,
        )
        if not changed:
            database.rollback()
            return False
        cursor = database.execute(
            """
            UPDATE orders
            SET status = 'completed', paid_at = COALESCE(paid_at, CURRENT_TIMESTAMP)
            WHERE id = ? AND status = 'confirmed'
            """,
            (order_id,),
        )
        if cursor.rowcount != 1:
            database.rollback()
            return False
        database.commit()
        return True
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


async def complete_delivery_and_order(order_id: int, admin_id: int) -> bool:
    return await asyncio.to_thread(complete_delivery_and_order_sync, order_id, admin_id)


async def update_order_status(
    order_id: int,
    status: str,
    *,
    expected_statuses: tuple[str, ...] | None = None,
) -> bool:
    """Update status through the guarded order/inventory lifecycle."""
    return await asyncio.to_thread(
        transition_order_status_sync,
        order_id,
        status,
        expected_statuses=expected_statuses,
    )


async def get_unnotified_pending_orders(limit: int = 20) -> list:
    """Compatibility read of actionable, not-yet-sent orders."""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(PENDING_STATUSES))
        cursor = await db.execute(
            f"""SELECT id, user_id, user_name, total, status, created_at
               FROM orders
               WHERE status IN ({placeholders})
                 AND new_order_notified = 0
                 AND new_order_notification_state = 'pending'
               ORDER BY created_at ASC
               LIMIT ?""",
            (*PENDING_STATUSES, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def claim_unnotified_pending_orders(
    limit: int = 20,
    lease_seconds: int = 300,
) -> list:
    """Lease actionable orders before delivery, so pollers cannot double-send."""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(PENDING_STATUSES))
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                f"""SELECT id, user_id, user_name, total, status, created_at
                   FROM orders
                   WHERE status IN ({placeholders})
                     AND new_order_notified = 0
                     AND (
                        new_order_notification_state = 'pending'
                        OR (
                            new_order_notification_state = 'processing'
                            AND new_order_notification_claimed_at < datetime('now', ?)
                        )
                     )
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (*PENDING_STATUSES, f"-{lease_seconds} seconds", limit),
            )
            rows = await cursor.fetchall()
            order_ids = [row["id"] for row in rows]
            if order_ids:
                placeholders = ",".join("?" * len(order_ids))
                await db.execute(
                    f"""UPDATE orders
                        SET new_order_notification_state = 'processing',
                            new_order_notification_claimed_at = CURRENT_TIMESTAMP
                        WHERE id IN ({placeholders})""",
                    order_ids,
                )
            await db.commit()
            return [dict(row) for row in rows]
        except Exception:
            await db.rollback()
            raise


async def mark_new_order_notified(order_id: int) -> None:
    """Mark a claimed notification as sent after at least one delivery."""
    async with connection() as db:
        await db.execute(
            """
            UPDATE orders
            SET new_order_notified = 1,
                new_order_notification_state = 'sent',
                new_order_notification_claimed_at = NULL
            WHERE id = ?
            """,
            (order_id,),
        )
        await db.commit()


async def release_new_order_notification_claim(order_id: int) -> None:
    async with connection() as db:
        await db.execute(
            """
            UPDATE orders
            SET new_order_notification_state = 'pending',
                new_order_notification_claimed_at = NULL
            WHERE id = ? AND new_order_notification_state = 'processing'
            """,
            (order_id,),
        )
        await db.commit()


async def save_admin_notification_ids(order_id: int, admin_ids: list, message_ids: list):
    """Сохранить ID сообщений уведомлений админам для последующего удаления"""
    import json
    async with connection() as db:
        # Получаем текущие ID
        cursor = await db.execute("SELECT admin_notification_ids FROM orders WHERE id = ?", (order_id,))
        row = await cursor.fetchone()
        current_ids = []
        if row and row[0]:
            try:
                current_ids = json.loads(row[0])
            except Exception:
                current_ids = []
        
        # Добавляем новые пары (admin_id, message_id)
        for admin_id, msg_id in zip(admin_ids, message_ids):
            if msg_id:
                current_ids.append({"admin_id": admin_id, "message_id": msg_id})
        
        await db.execute(
            "UPDATE orders SET admin_notification_ids = ? WHERE id = ?",
            (json.dumps(current_ids), order_id)
        )
        await db.commit()


async def replace_admin_notification_ids(order_id: int, pairs: list):
    """Полностью заменить хранимые ID уведомлений (текстовый квиток -> фото-чек)."""
    import json
    data = json.dumps([{"admin_id": a, "message_id": m} for a, m in pairs if m])
    async with connection() as db:
        await db.execute(
            "UPDATE orders SET admin_notification_ids = ? WHERE id = ?",
            (data, order_id)
        )
        await db.commit()


async def clear_admin_notifications(order_id: int, bot):
    """Удалить уведомления у всех админов после подтверждения одним из них"""
    import json
    async with connection() as db:
        cursor = await db.execute("SELECT admin_notification_ids FROM orders WHERE id = ?", (order_id,))
        row = await cursor.fetchone()
        
        if not row or not row[0]:
            return
        
        try:
            notifications = json.loads(row[0])
        except Exception:
            return
        
        # Удаляем сообщения у всех админов
        for notif in notifications:
            admin_id = notif.get('admin_id')
            msg_id = notif.get('message_id')
            if admin_id and msg_id:
                try:
                    await bot.delete_message(chat_id=admin_id, message_id=msg_id)
                    print(f"✅ Уведомление удалено у админа {admin_id}")
                except Exception as e:
                    print(f"⚠️ Не удалось удалить уведомление у админа {admin_id}: {e}")
        
        # Очищаем поле в БД
        await db.execute(
            "UPDATE orders SET admin_notification_ids = ? WHERE id = ?",
            ('[]', order_id)
        )
        await db.commit()


async def get_all_orders(
    limit: int = 20,
    offset: int = 0,
    status: str = None,
    sort_by: str = 'new',
) -> list:
    """Получить заказы с пагинацией."""
    order_by = _ORDER_SORTS.get(sort_by, _ORDER_SORTS['new'])
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        if status:
            cursor = await db.execute(
                f"""SELECT * FROM orders
                   WHERE status = ?
                   ORDER BY {order_by}
                   LIMIT ? OFFSET ?""",
                (status, limit, offset),
            )
        else:
            cursor = await db.execute(
                f"""SELECT * FROM orders
                   ORDER BY {order_by}
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        order_ids = [row["id"] for row in rows]
        items_by_order = {order_id: [] for order_id in order_ids}
        if order_ids:
            placeholders = ",".join("?" for _ in order_ids)
            cursor = await db.execute(
                f"SELECT order_id, book_id, title, price FROM order_items WHERE order_id IN ({placeholders}) ORDER BY id ASC",
                order_ids,
            )
            for item in await cursor.fetchall():
                items_by_order[item["order_id"]].append(item)
        return [apply_order_receipt(dict(row), items_by_order[row["id"]]) for row in rows]


async def get_orders_count(status: str = None) -> int:
    """Получить количество заказов (с фильтром по статусу)"""
    async with connection() as db:
        if status:
            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM orders WHERE status = ?",
                (status,)
            )
        else:
            cursor = await db.execute("SELECT COUNT(*) as cnt FROM orders")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_stats() -> dict:
    """Получить статистику магазина"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("SELECT COUNT(*) as cnt FROM orders")
        total_orders = (await cursor.fetchone())['cnt']

        cursor = await db.execute(
            "SELECT status, COUNT(*) as cnt, SUM(total) as sum FROM orders GROUP BY status"
        )
        by_status = await cursor.fetchall()

        placeholders = ",".join("?" for _ in PAID_STATUSES)
        cursor = await db.execute(
            f"SELECT SUM(total) as sum FROM orders WHERE status IN ({placeholders})",
            PAID_STATUSES,
        )
        total_revenue = (await cursor.fetchone())['sum'] or 0

        return {
            'total_orders': total_orders,
            'total_revenue': total_revenue,
            'by_status': [dict(s) for s in by_status]
        }


async def get_all_unique_users() -> list:
    """Получить всех уникальных пользователей"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT DISTINCT user_id FROM orders")
        rows = await cursor.fetchall()
        return [r['user_id'] for r in rows]
