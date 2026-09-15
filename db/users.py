"""Модуль для работы с пользователями"""
import aiosqlite
from db.connection import connection


async def get_all_users() -> list:
    """Получить всех пользователей из таблицы users"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT user_id, user_name FROM users")
        rows = await cursor.fetchall()
        return [{'user_id': r['user_id'], 'user_name': r['user_name']} for r in rows]


async def get_user(user_id: int) -> dict:
    """Получить информацию о пользователе"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, user_name FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def add_user(user_id: int, user_name: str) -> bool:
    """Добавить пользователя в базу данных"""
    async with connection() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, user_name) VALUES (?, ?)",
            (user_id, user_name)
        )
        await db.commit()
        return True


async def set_support_active(user_id: int, active: bool) -> None:
    """Поставить/снять флаг активного диалога с поддержкой.

    Запись идемпотентна: ставит 1 или 0. Если пользователя ещё нет
    в таблице users — создаёт строку, чтобы не терять состояние для
    только что зашедших пользователей.
    """
    async with connection() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, user_name) VALUES (?, ?)",
            (user_id, "")
        )
        await db.execute(
            "UPDATE users SET is_support_active = ? WHERE user_id = ?",
            (1 if active else 0, user_id)
        )
        await db.commit()


async def is_support_active(user_id: int) -> bool:
    """Проверить, активен ли диалог с поддержкой у пользователя."""
    async with connection() as db:
        cursor = await db.execute(
            "SELECT is_support_active FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return bool(row and row[0])


async def get_all_support_active_user_ids() -> list:
    """Список user_id пользователей с активным диалогом поддержки (для гидратации кэша при старте)."""
    async with connection() as db:
        cursor = await db.execute(
            "SELECT user_id FROM users WHERE is_support_active = 1"
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def clear_support_active_users() -> None:
    """Закрыть все активные диалоги поддержки без удаления пользователей."""
    async with connection() as db:
        await db.execute("UPDATE users SET is_support_active = 0 WHERE is_support_active = 1")
        await db.commit()
