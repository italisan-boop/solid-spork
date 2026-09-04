"""Модуль для работы с настройками оплаты"""
import aiosqlite
from db import DB_NAME


async def init_payments():
    """Создание таблицы настроек оплаты"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payment_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setting_key TEXT NOT NULL UNIQUE,
                setting_value TEXT NOT NULL
            )
        """)

        cursor = await db.execute("SELECT COUNT(*) FROM payment_settings")
        count = (await cursor.fetchone())[0]

        if count == 0:
            default_settings = [
                ('payment_enabled', '1'),
                ('card_number', ''),
                ('sbp_phone', ''),
                ('sbp_bank', ''),
                ('recipient_name', ''),
                ('payment_instructions', 'После перевода укажите номер заказа в комментарии')
            ]
            await db.executemany(
                "INSERT INTO payment_settings (setting_key, setting_value) VALUES (?, ?)",
                default_settings
            )
            print("✅ Добавлены настройки оплаты по умолчанию")

        await db.commit()
    print("✅ Таблица настроек оплаты инициализирована")


async def get_payment_setting(key: str, default: str = '') -> str:
    """Получить настройку оплаты"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT setting_value FROM payment_settings WHERE setting_key = ?",
            (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_payment_setting(key: str, value: str):
    """Установить настройку оплаты"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """INSERT INTO payment_settings (setting_key, setting_value)
               VALUES (?, ?)
               ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value""",
            (key, value)
        )
        await db.commit()


async def get_all_payment_settings() -> dict:
    """Получить все настройки оплаты"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT setting_key, setting_value FROM payment_settings")
        rows = await cursor.fetchall()
        return {row['setting_key']: row['setting_value'] for row in rows}
