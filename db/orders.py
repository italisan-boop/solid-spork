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

        return {
            'id': order['id'],
            'user_id': order['user_id'],
            'user_name': order['user_name'],
            'total': order['total'],
            'status': order['status'],
            'created_at': order['created_at'],
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
