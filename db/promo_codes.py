"""Модуль для работы с промокодами"""
import aiosqlite
from datetime import datetime
from db.connection import connection




async def get_all_promo_codes() -> list:
    """Получить все промокоды"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM promo_codes ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_promo_code(code: str) -> dict:
    """Получить промокод по коду"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM promo_codes WHERE code = ? AND is_active = 1",
            (code.upper(),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def add_promo_code(code: str, discount_percent: int = 0, discount_fixed: int = 0,
                         min_order: int = 0, max_uses: int = 0, expires_at: str = None) -> int:
    """Добавить новый промокод"""
    async with connection() as db:
        cursor = await db.execute(
            """INSERT INTO promo_codes (code, discount_percent, discount_fixed,
               min_order, max_uses, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (code.upper(), discount_percent, discount_fixed, min_order, max_uses, expires_at)
        )
        await db.commit()
        return cursor.lastrowid


async def update_promo_code(promo_id: int, **kwargs) -> bool:
    """Обновить промокод"""
    async with connection() as db:
        updates = []
        params = []
        for key, value in kwargs.items():
            if value is not None:
                updates.append(f"{key} = ?")
                params.append(value)
        if not updates:
            return False
        params.append(promo_id)
        query = f"UPDATE promo_codes SET {', '.join(updates)} WHERE id = ?"
        await db.execute(query, params)
        await db.commit()
        return True


async def delete_promo_code(promo_id: int):
    """Удалить промокод"""
    async with connection() as db:
        await db.execute("DELETE FROM promo_codes WHERE id = ?", (promo_id,))
        await db.commit()


async def increment_promo_usage(code: str):
    """Увеличить счётчик использований промокода"""
    async with connection() as db:
        await db.execute(
            "UPDATE promo_codes SET current_uses = current_uses + 1 WHERE code = ?",
            (code.upper(),)
        )
        await db.commit()


async def validate_promo_code(code: str, order_total: int) -> dict:
    """Проверить промокод и вернуть информацию о скидке"""
    promo = await get_promo_code(code.upper())

    if not promo:
        return {'valid': False, 'error': 'Промокод не найден'}

    if promo.get('expires_at'):
        try:
            expires = datetime.fromisoformat(promo['expires_at'])
            if datetime.now() > expires:
                return {'valid': False, 'error': 'Промокод истёк'}
        except Exception:
            pass

    if promo['min_order'] > 0 and order_total < promo['min_order']:
        return {'valid': False, 'error': f"Минимальная сумма заказа: {promo['min_order']} ₽"}

    if promo['max_uses'] > 0 and promo['current_uses'] >= promo['max_uses']:
        return {'valid': False, 'error': 'Промокод больше не действует'}

    discount = 0
    if promo['discount_percent'] > 0:
        discount = int(order_total * promo['discount_percent'] / 100)
    elif promo['discount_fixed'] > 0:
        discount = promo['discount_fixed']

    discount = min(discount, order_total)

    return {
        'valid': True,
        'discount': discount,
        'discount_percent': promo['discount_percent'],
        'discount_fixed': promo['discount_fixed'],
        'final_total': order_total - discount
    }
