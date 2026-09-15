"""Асинхронное хранилище истории обращений в поддержку."""
import aiosqlite

from db.connection import connection


SUPPORT_HISTORY_RETENTION_DAYS = 90


async def append_support_message(
    user_id: int,
    role: str,
    sender_name: str,
    text: str,
) -> int:
    if role not in {"user", "admin"}:
        raise ValueError("Unsupported support message role")

    async with connection() as db:
        if role == "user" and sender_name:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, user_name) VALUES (?, ?)",
                (user_id, sender_name),
            )
            await db.execute(
                "UPDATE users SET user_name = ? WHERE user_id = ?",
                (sender_name, user_id),
            )
        cursor = await db.execute(
            """
            INSERT INTO support_messages (user_id, role, sender_name, text)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, role, sender_name, text),
        )
        await db.commit()
        return cursor.lastrowid


async def get_support_messages(user_id: int, limit: int, offset: int = 0) -> list[dict]:
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, user_id, role, sender_name, text, created_at
            FROM (
                SELECT id, user_id, role, sender_name, text, created_at
                FROM support_messages
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
            )
            ORDER BY id ASC
            """,
            (user_id, limit, offset),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_support_message_count(user_id: int) -> int:
    async with connection() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM support_messages WHERE user_id = ?", (user_id,)
        )
        return (await cursor.fetchone())[0]


async def get_support_dialogs(limit: int, offset: int = 0) -> list[dict]:
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            WITH latest AS (
                SELECT user_id, MAX(id) AS last_message_id
                FROM support_messages
                GROUP BY user_id
            )
            SELECT
                message.user_id,
                message.id AS last_message_id,
                COALESCE(NULLIF(users.user_name, ''), message.sender_name, '') AS user_name,
                message.role AS last_role,
                message.text AS last_text,
                message.created_at AS last_created_at
            FROM latest
            JOIN support_messages AS message ON message.id = latest.last_message_id
            LEFT JOIN users ON users.user_id = message.user_id
            ORDER BY message.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_support_dialog_count() -> int:
    async with connection() as db:
        cursor = await db.execute(
            "SELECT COUNT(DISTINCT user_id) FROM support_messages"
        )
        return (await cursor.fetchone())[0]


async def clear_support_history() -> None:
    async with connection() as db:
        await db.execute("DELETE FROM support_messages")
        await db.commit()
