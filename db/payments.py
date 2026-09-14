"""Модуль для работы с настройками оплаты"""
import aiosqlite
from db.connection import connection




async def get_payment_setting(key: str, default: str = '') -> str:
    """Получить настройку оплаты"""
    async with connection() as db:
        cursor = await db.execute(
            "SELECT setting_value FROM payment_settings WHERE setting_key = ?",
            (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_payment_setting(key: str, value: str):
    """Установить настройку оплаты"""
    async with connection() as db:
        await db.execute(
            """INSERT INTO payment_settings (setting_key, setting_value)
               VALUES (?, ?)
               ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value""",
            (key, value)
        )
        await db.commit()


async def get_all_payment_settings() -> dict:
    """Получить все настройки оплаты"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT setting_key, setting_value FROM payment_settings")
        rows = await cursor.fetchall()
        return {row['setting_key']: row['setting_value'] for row in rows}
