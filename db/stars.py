"""Модуль для работы с Telegram Stars"""
import aiosqlite
from db import DB_NAME


async def init_stars():
    """Создание таблицы настроек Stars"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stars_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setting_key TEXT NOT NULL UNIQUE,
                setting_value TEXT NOT NULL
            )
        """)

        cursor = await db.execute("SELECT COUNT(*) FROM stars_settings")
        count = (await cursor.fetchone())[0]

        if count == 0:
            default_settings = [
                ('stars_enabled', '0'),
                ('rubles_per_star', '2')
            ]
            await db.executemany(
                "INSERT INTO stars_settings (setting_key, setting_value) VALUES (?, ?)",
                default_settings
            )
            print("✅ Добавлены настройки Stars по умолчанию")

        await db.commit()
    print("✅ Таблица настроек Stars инициализирована")


async def get_stars_setting(key: str, default: str = '') -> str:
    """Получить настройку Stars"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT setting_value FROM stars_settings WHERE setting_key = ?",
            (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_stars_setting(key: str, value: str):
    """Установить настройку Stars"""
    async with aiosqlite.connect(DB_NAME) as db:
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
