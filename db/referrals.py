"""Модуль для работы с реферальной программой"""
import aiosqlite
from db.connection import connection




async def get_referral_code(user_id: int) -> str:
    """Получить реферальный код пользователя"""
    return f"ref_{user_id}"


async def parse_referral_code(code: str) -> int:
    """Парсинг реферального кода"""
    if code and code.startswith("ref_"):
        try:
            return int(code.replace("ref_", ""))
        except (ValueError, AttributeError):
            pass
    return 0


async def check_referral_exists(referred_id: int) -> bool:
    """Проверить, был ли пользователь уже приглашён"""
    async with connection() as db:
        cursor = await db.execute(
            "SELECT id FROM referrals WHERE referred_id = ?", (referred_id,)
        )
        row = await cursor.fetchone()
        return row is not None


async def create_referral(referrer_id: int, referred_id: int):
    """Создать реферальную связь"""
    async with connection() as db:
        await db.execute(
            "INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
            (referrer_id, referred_id)
        )
        await db.commit()


async def add_user_bonus(user_id: int, bonus_type: str, amount: int):
    """Начислить бонус пользователю"""
    async with connection() as db:
        await db.execute(
            "INSERT INTO user_bonuses (user_id, bonus_type, amount) VALUES (?, ?, ?)",
            (user_id, bonus_type, amount)
        )
        await db.commit()


async def get_user_active_bonus(user_id: int) -> dict:
    """Получить неиспользованный бонус пользователя"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM user_bonuses WHERE user_id = ? AND is_used = 0 ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def mark_bonus_used(bonus_id: int):
    """Отметить бонус как использованный"""
    async with connection() as db:
        await db.execute(
            "UPDATE user_bonuses SET is_used = 1 WHERE id = ?", (bonus_id,)
        )
        await db.commit()


async def get_referral_stats(user_id: int) -> dict:
    """Статистика по рефералам пользователя"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id = ?",
            (user_id,)
        )
        total_invited = (await cursor.fetchone())['cnt']

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id = ? AND bonus_given = 1",
            (user_id,)
        )
        bonuses_earned = (await cursor.fetchone())['cnt']

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM user_bonuses WHERE user_id = ? AND is_used = 0",
            (user_id,)
        )
        active_bonuses = (await cursor.fetchone())['cnt']

        return {
            'total_invited': total_invited,
            'bonuses_earned': bonuses_earned,
            'active_bonuses': active_bonuses
        }
