"""Модуль для работы с Telegram Stars"""
import aiosqlite
from db.connection import connection




async def get_stars_setting(key: str, default: str = '') -> str:
    """Получить настройку Stars"""
    async with connection() as db:
        cursor = await db.execute(
            "SELECT setting_value FROM stars_settings WHERE setting_key = ?",
            (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_stars_setting(key: str, value: str):
    """Установить настройку Stars"""
    async with connection() as db:
        await db.execute(
            """INSERT INTO stars_settings (setting_key, setting_value)
               VALUES (?, ?)
               ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value""",
            (key, value)
        )
        await db.commit()


async def rubles_to_stars(rubles: int) -> int:
    """Конвертировать рубли в Stars"""
    rubles_per_star = int(await get_stars_setting('rubles_per_star', '2'))
    stars = max(1, rubles // rubles_per_star)
    return stars
