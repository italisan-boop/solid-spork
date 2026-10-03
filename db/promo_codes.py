"""Validated promo-code storage and checkout claiming."""
from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import UTC, datetime

import aiosqlite

from db.connection import connection
from db.schema import connect


_CODE_PATTERN = re.compile(r"[A-Z0-9_-]{3,48}\Z")
_MAX_MONEY = 10_000_000
_MAX_USES = 1_000_000


class PromoValidationError(ValueError):
    pass


def _integer(value: object, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise PromoValidationError(f"{field} is invalid")
    return value


def _expires_at(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise PromoValidationError("expires_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PromoValidationError("expires_at is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def promo_payload(payload: object) -> dict:
    fields = {
        "code", "discount_percent", "discount_fixed", "min_order", "max_uses", "expires_at"
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise PromoValidationError("invalid promo payload")
    code = payload["code"]
    if not isinstance(code, str):
        raise PromoValidationError("code is invalid")
    code = code.strip().upper()
    if not _CODE_PATTERN.fullmatch(code):
        raise PromoValidationError("code is invalid")
    discount_percent = _integer(payload["discount_percent"], "discount_percent", 100)
    discount_fixed = _integer(payload["discount_fixed"], "discount_fixed", _MAX_MONEY)
    if (discount_percent > 0) == (discount_fixed > 0):
        raise PromoValidationError("exactly one discount is required")
    return {
        "code": code,
        "discount_percent": discount_percent,
        "discount_fixed": discount_fixed,
        "min_order": _integer(payload["min_order"], "min_order", _MAX_MONEY),
        "max_uses": _integer(payload["max_uses"], "max_uses", _MAX_USES),
        "expires_at": _expires_at(payload["expires_at"]),
    }


def _promo_projection(row: sqlite3.Row | tuple) -> dict:
    return {
        "id": row["id"],
        "code": row["code"],
        "discount_percent": row["discount_percent"],
        "discount_fixed": row["discount_fixed"],
        "min_order": row["min_order"],
        "max_uses": row["max_uses"],
        "current_uses": row["current_uses"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


def list_promo_codes_sync() -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT id, code, discount_percent, discount_fixed, min_order, max_uses,
                   current_uses, is_active, created_at, expires_at
            FROM promo_codes ORDER BY is_active DESC, created_at DESC, id DESC
            """
        ).fetchall()
        return [_promo_projection(row) for row in rows]
    finally:
        database.close()


def get_promo_by_id_sync(promo_id: int) -> dict | None:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        row = database.execute(
            """
            SELECT id, code, discount_percent, discount_fixed, min_order, max_uses,
                   current_uses, is_active, created_at, expires_at
            FROM promo_codes WHERE id = ?
            """,
            (promo_id,),
        ).fetchone()
        return _promo_projection(row) if row else None
    finally:
        database.close()


def create_promo_code_sync(payload: object) -> dict:
    values = promo_payload(payload)
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        cursor = database.execute(
            """
            INSERT INTO promo_codes (
                code, discount_percent, discount_fixed, min_order, max_uses, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            tuple(values[field] for field in (
                "code", "discount_percent", "discount_fixed", "min_order", "max_uses", "expires_at"
            )),
        )
        row = database.execute(
            """
            SELECT id, code, discount_percent, discount_fixed, min_order, max_uses,
                   current_uses, is_active, created_at, expires_at
            FROM promo_codes WHERE id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        database.commit()
        return _promo_projection(row)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def update_promo_code_sync(promo_id: int, payload: object) -> dict | None:
    values = promo_payload(payload)
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        cursor = database.execute(
            """
            UPDATE promo_codes
            SET code = ?, discount_percent = ?, discount_fixed = ?, min_order = ?,
                max_uses = ?, expires_at = ?
            WHERE id = ?
            """,
            (*tuple(values[field] for field in (
                "code", "discount_percent", "discount_fixed", "min_order", "max_uses", "expires_at"
            )), promo_id),
        )
        if cursor.rowcount != 1:
            database.rollback()
            return None
        row = database.execute(
            """
            SELECT id, code, discount_percent, discount_fixed, min_order, max_uses,
                   current_uses, is_active, created_at, expires_at
            FROM promo_codes WHERE id = ?
            """,
            (promo_id,),
        ).fetchone()
        database.commit()
        return _promo_projection(row)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def disable_promo_code_sync(promo_id: int) -> dict | None:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        cursor = database.execute(
            "UPDATE promo_codes SET is_active = 0 WHERE id = ? AND is_active = 1",
            (promo_id,),
        )
        if cursor.rowcount != 1:
            row = database.execute(
                """
                SELECT id, code, discount_percent, discount_fixed, min_order, max_uses,
                       current_uses, is_active, created_at, expires_at
                FROM promo_codes WHERE id = ?
                """,
                (promo_id,),
            ).fetchone()
            database.rollback()
            return _promo_projection(row) if row else None
        row = database.execute(
            """
            SELECT id, code, discount_percent, discount_fixed, min_order, max_uses,
                   current_uses, is_active, created_at, expires_at
            FROM promo_codes WHERE id = ?
            """,
            (promo_id,),
        ).fetchone()
        database.commit()
        return _promo_projection(row)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def _promo_result(promo: sqlite3.Row | dict, order_total: int) -> dict:
    if isinstance(order_total, bool) or not isinstance(order_total, int) or order_total < 0:
        return {"valid": False, "error": "Некорректная сумма заказа"}
    if not promo or not promo["is_active"]:
        return {"valid": False, "error": "Промокод не найден"}
    expires_at = promo["expires_at"]
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if datetime.now(UTC) >= expiry.astimezone(UTC):
                return {"valid": False, "error": "Промокод истёк"}
        except ValueError:
            return {"valid": False, "error": "Промокод недействителен"}
    if promo["min_order"] > 0 and order_total < promo["min_order"]:
        return {"valid": False, "error": f"Минимальная сумма заказа: {promo['min_order']} ₽"}
    if promo["max_uses"] > 0 and promo["current_uses"] >= promo["max_uses"]:
        return {"valid": False, "error": "Промокод больше не действует"}
    discount = (
        order_total * promo["discount_percent"] // 100
        if promo["discount_percent"] > 0
        else promo["discount_fixed"]
    )
    discount = min(discount, order_total)
    return {
        "valid": True,
        "discount": discount,
        "discount_percent": promo["discount_percent"],
        "discount_fixed": promo["discount_fixed"],
        "final_total": order_total - discount,
        "promo_code": promo["code"],
    }


def validate_promo_code_sync(code: str, order_total: int) -> dict:
    normalized = str(code or "").strip().upper()
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        promo = database.execute(
            "SELECT * FROM promo_codes WHERE code = ? AND is_active = 1", (normalized,)
        ).fetchone()
        return _promo_result(promo, order_total)
    finally:
        database.close()


def validate_and_claim_promo(database: sqlite3.Connection, code: str, order_total: int) -> dict:
    """Claim a promo under the caller's checkout transaction."""
    normalized = str(code or "").strip().upper()
    database.row_factory = sqlite3.Row
    promo = database.execute(
        "SELECT * FROM promo_codes WHERE code = ? AND is_active = 1", (normalized,)
    ).fetchone()
    result = _promo_result(promo, order_total)
    if not result["valid"]:
        return result
    cursor = database.execute(
        """
        UPDATE promo_codes SET current_uses = current_uses + 1
        WHERE id = ? AND is_active = 1 AND (max_uses = 0 OR current_uses < max_uses)
        """,
        (promo["id"],),
    )
    if cursor.rowcount != 1:
        return {"valid": False, "error": "Промокод больше не действует"}
    return result


async def get_all_promo_codes() -> list:
    async with connection() as database:
        database.row_factory = aiosqlite.Row
        cursor = await database.execute("SELECT * FROM promo_codes ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_promo_code(code: str) -> dict | None:
    async with connection() as database:
        database.row_factory = aiosqlite.Row
        cursor = await database.execute(
            "SELECT * FROM promo_codes WHERE code = ? AND is_active = 1", (code.upper(),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def add_promo_code(code: str, discount_percent: int = 0, discount_fixed: int = 0,
                         min_order: int = 0, max_uses: int = 0, expires_at: str | None = None) -> int:
    payload = {
        "code": code,
        "discount_percent": discount_percent,
        "discount_fixed": discount_fixed,
        "min_order": min_order,
        "max_uses": max_uses,
        "expires_at": expires_at,
    }
    return (await asyncio.to_thread(create_promo_code_sync, payload))["id"]


async def update_promo_code(promo_id: int, **kwargs) -> bool:
    existing = await asyncio.to_thread(get_promo_by_id_sync, promo_id)
    if existing is None:
        return False
    payload = {field: kwargs.get(field, existing[field]) for field in (
        "code", "discount_percent", "discount_fixed", "min_order", "max_uses", "expires_at"
    )}
    return await asyncio.to_thread(update_promo_code_sync, promo_id, payload) is not None


async def delete_promo_code(promo_id: int):
    await asyncio.to_thread(disable_promo_code_sync, promo_id)


async def increment_promo_usage(code: str):
    async with connection() as database:
        await database.execute(
            "UPDATE promo_codes SET current_uses = current_uses + 1 WHERE code = ?", (code.upper(),)
        )
        await database.commit()


async def validate_promo_code(code: str, order_total: int) -> dict:
    return await asyncio.to_thread(validate_promo_code_sync, code, order_total)
