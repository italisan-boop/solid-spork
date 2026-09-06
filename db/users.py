"""Модуль для работы с пользователями"""
import aiosqlite
from db import DB_NAME


async def get_all_users() -> list:
    """Получить всех пользователей из таблицы users"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT user_id, user_name FROM users")
        rows = await cursor.fetchall()
        return [{'user_id': r['user_id'], 'user_name': r['user_name']} for r in rows]


async def get_user(user_id: int) -> dict:
    """Получить информацию о пользователе"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, user_name FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
