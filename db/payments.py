"""Модуль для работы с настройками оплаты"""
import aiosqlite
from db.connection import connection
from db.schema import connect


PAYMENT_SETTING_KEYS = frozenset({
    "payment_enabled", "card_number", "sbp_phone", "sbp_bank", "recipient_name",
    "payment_instructions", "yookassa_enabled", "delivery_enabled",
    "delivery_sdek_pickup_enabled", "delivery_sdek_pickup_price_rub",
    "delivery_russian_post_pickup_enabled", "delivery_russian_post_pickup_price_rub",
    "delivery_self_pickup_enabled", "delivery_self_pickup_price_rub",
    "delivery_self_pickup_location", "delivery_self_pickup_schedule",
    "delivery_self_pickup_instructions",
})
STARS_SETTING_KEYS = frozenset({"stars_enabled", "rubles_per_star"})


def get_payment_settings_sync() -> dict[str, str]:
    database = connect()
    try:
        return {
            row[0]: row[1]
            for row in database.execute(
                "SELECT setting_key, setting_value FROM payment_settings"
            ).fetchall()
        }
    finally:
        database.close()


def get_stars_settings_sync() -> dict[str, str]:
    database = connect()
    try:
        return {
            row[0]: row[1]
            for row in database.execute(
                "SELECT setting_key, setting_value FROM stars_settings"
            ).fetchall()
        }
    finally:
        database.close()


def replace_checkout_settings_sync(payment_values: dict[str, str], stars_values: dict[str, str]) -> None:
    if set(payment_values) != PAYMENT_SETTING_KEYS or set(stars_values) != STARS_SETTING_KEYS:
        raise ValueError("invalid checkout settings")
    database = connect()
    try:
        database.execute("BEGIN IMMEDIATE")
        database.executemany(
            """
            INSERT INTO payment_settings (setting_key, setting_value) VALUES (?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
            """,
            sorted(payment_values.items()),
        )
        database.executemany(
            """
            INSERT INTO stars_settings (setting_key, setting_value) VALUES (?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
            """,
            sorted(stars_values.items()),
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()




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
