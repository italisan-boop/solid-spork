"""Модуль для работы с заказами"""
import aiosqlite
from db import DB_NAME


async def create_order(user_id: int, user_name: str, cart: list, total: int) -> int:
    """Создать новый заказ"""
    async with aiosqlite.connect(DB_NAME) as db:
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


async def get_order(order_id: int) -> dict:
    """Получить заказ по ID"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_order_full(order_id: int) -> dict:
    """Получить заказ со всеми товарами"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        order = await cursor.fetchone()
        if not order:
            return None

        cursor = await db.execute(
            "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
        )
        items = await cursor.fetchall()

        # Получаем ID сообщений уведомлений
        admin_notification_ids = []
        if 'admin_notification_ids' in order.keys() and order['admin_notification_ids']:
            try:
                import json
                admin_notification_ids = json.loads(order['admin_notification_ids'])
            except Exception:
                admin_notification_ids = []

        return {
            'id': order['id'],
            'user_id': order['user_id'],
            'user_name': order['user_name'],
            'total': order['total'],
            'status': order['status'],
            'created_at': order['created_at'],
            'admin_notification_ids': admin_notification_ids,
            'items': [dict(item) for item in items]
        }


async def get_user_orders(user_id: int, limit: int = 10) -> list:
    """Получить заказы пользователя"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_order_status(order_id: int, status: str):
    """Обновить статус заказа"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (status, order_id)
        )
        await db.commit()
    print(f"✅ Статус заказа #{order_id} изменён на '{status}'")


async def get_unnotified_new_orders(limit: int = 20) -> list:
    """Заказы в статусе 'new', о которых админы ещё не уведомлены.

    Поллер авто-уведомлений крутит эту функцию и по результату шлёт
    админам карточку с кнопками «Принять / Отклонить».
    """
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, user_id, user_name, total, status, created_at
               FROM orders
               WHERE status = 'new' AND new_order_notified = 0
               ORDER BY created_at ASC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def mark_new_order_notified(order_id: int) -> None:
    """Отметить, что админы уже получили авто-уведомление о заказе."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE orders SET new_order_notified = 1 WHERE id = ?",
            (order_id,),
        )
        await db.commit()


async def save_admin_notification_ids(order_id: int, admin_ids: list, message_ids: list):
    """Сохранить ID сообщений уведомлений админам для последующего удаления"""
    import json
    async with aiosqlite.connect(DB_NAME) as db:
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


async def clear_admin_notifications(order_id: int, bot):
    """Удалить уведомления у всех админов после подтверждения одним из них"""
    import json
    async with aiosqlite.connect(DB_NAME) as db:
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


async def get_all_orders(limit: int = 20, offset: int = 0, status: str = None) -> list:
    """Получить заказы с пагинацией"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row

        if status:
            cursor = await db.execute(
                """SELECT * FROM orders
                   WHERE status = ?
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (status, limit, offset)
            )
        else:
            cursor = await db.execute(
                """SELECT * FROM orders
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset)
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_orders_count(status: str = None) -> int:
    """Получить количество заказов (с фильтром по статусу)"""
    async with aiosqlite.connect(DB_NAME) as db:
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
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("SELECT COUNT(*) as cnt FROM orders")
        total_orders = (await cursor.fetchone())['cnt']

        cursor = await db.execute(
            "SELECT status, COUNT(*) as cnt, SUM(total) as sum FROM orders GROUP BY status"
        )
        by_status = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT SUM(total) as sum FROM orders WHERE status != 'cancelled'"
        )
        total_revenue = (await cursor.fetchone())['sum'] or 0

        return {
            'total_orders': total_orders,
            'total_revenue': total_revenue,
            'by_status': [dict(s) for s in by_status]
        }


async def get_all_unique_users() -> list:
    """Получить всех уникальных пользователей"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT DISTINCT user_id FROM orders")
        rows = await cursor.fetchall()
        return [r['user_id'] for r in rows]
