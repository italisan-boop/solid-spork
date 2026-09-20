"""Модуль для работы с книгами (каталог)"""
import aiosqlite
import json

from controlplane.plan_policy import FEATURE_CATALOG, LIMIT_BOOKS
from db.connection import connection
from db.categories import NO_CATEGORY_NAME

PAGE_SIZE = 20  # Количество книг на странице


async def add_book(
    title: str,
    price: int,
    category_id: int | None,
    *,
    author: str = "",
    description: str = "",
    stock_quantity: int | None = None,
) -> int:
    from runtime.context import maybe_current_tenant_context
    from runtime.features import require_feature
    from runtime.quota import require_count_quota

    tenant_context = maybe_current_tenant_context()
    if tenant_context is not None:
        require_feature(FEATURE_CATALOG, tenant_context)
    async with connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT name FROM categories WHERE id = ?",
                (category_id,),
            )
            row = await cursor.fetchone()
            category_name = row["name"] if row else NO_CATEGORY_NAME
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM books
                WHERE is_active = 1 AND COALESCE(is_archived, 0) = 0
                """
            )
            active_books = (await cursor.fetchone())[0]
            require_count_quota(LIMIT_BOOKS, active_books, context=tenant_context)
            cursor = await db.execute(
                """INSERT INTO books (
                       title, price, category, category_id, author, description, cover_photo,
                       images, emoji, stock_quantity, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, '', '[]', '', ?, CURRENT_TIMESTAMP)""",
                (
                    title, price, category_name, category_id, author, description, stock_quantity,
                ),
            )
            book_id = cursor.lastrowid
            if stock_quantity is not None and stock_quantity > 0:
                await db.execute(
                    """
                    INSERT INTO inventory_movements (
                        book_id, action, stock_delta, stock_after, reserved_after, reason, source_key
                    ) VALUES (?, 'opening_balance', ?, ?, 0, 'initial stock', ?)
                    """,
                    (book_id, stock_quantity, stock_quantity, f"opening:{book_id}"),
                )
            await db.commit()
            return book_id
        except Exception:
            await db.rollback()
            raise


async def find_book_by_title_author(title: str, author: str) -> dict | None:
    """Найти активную книгу с тем же (title, author). Регистр и пробелы по краям
    игнорируются, чтобы дубликат ловился и для 'Книга' / 'книга' / ' Книга '.

    Возвращает {'id', 'title', 'author', 'category'} или None.
    Используется в админке для предупреждения о дубликатах при добавлении.
    """
    if not title:
        return None
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, title, author, category
               FROM books
               WHERE is_active = 1 AND COALESCE(is_archived, 0) = 0
                 AND LOWER(TRIM(title)) = LOWER(TRIM(?))
                 AND LOWER(TRIM(COALESCE(author, ''))) = LOWER(TRIM(COALESCE(?, '')))
               LIMIT 1""",
            (title, author or '')
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_all_books() -> list:
    """Получить все активные книги"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT b.id, b.title, b.price, b.category, b.author, b.emoji, b.cover_photo,
                      b.description, b.images, b.category_id, b.sort_order,
                      b.stock_quantity,
                      c.emoji as category_emoji
               FROM books b
               LEFT JOIN categories c ON b.category_id = c.id
               WHERE b.is_active = 1 AND COALESCE(b.is_archived, 0) = 0
               ORDER BY b.sort_order ASC, b.id ASC"""
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_book_sort_order(book_id: int, sort_order: int):
    """Обновить порядок отображения книги"""
    async with connection() as db:
        await db.execute(
            "UPDATE books SET sort_order = ? WHERE id = ?",
            (sort_order, book_id)
        )
        await db.commit()


async def get_book(book_id: int) -> dict:
    """Получить одну книгу по ID"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT b.id, b.title, b.price, b.category, b.author, b.emoji, b.cover_photo,
                      b.description, b.images, b.category_id, b.sort_order,
                      b.stock_quantity,
                      c.emoji as category_emoji
               FROM books b
               LEFT JOIN categories c ON b.category_id = c.id
               WHERE b.id = ? AND b.is_active = 1 AND COALESCE(b.is_archived, 0) = 0""",
            (book_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


def _normalize_book_ids(book_ids) -> list[int]:
    normalized = []
    seen = set()
    for book_id in book_ids:
        if isinstance(book_id, bool) or not isinstance(book_id, int) or book_id <= 0:
            raise ValueError("book IDs must be positive integers")
        if book_id not in seen:
            seen.add(book_id)
            normalized.append(book_id)
    return sorted(normalized)


async def archive_books(book_ids) -> dict[str, list[int]]:
    """Архивировать только ещё активные книги из указанного набора IDs."""
    ids = _normalize_book_ids(book_ids)
    if not ids:
        return {"archived_ids": [], "skipped_ids": []}
    placeholders = ",".join("?" for _ in ids)
    async with connection() as db:
        cursor = await db.execute(
            f"""
            SELECT id FROM books
            WHERE id IN ({placeholders})
              AND is_active = 1
              AND COALESCE(is_archived, 0) = 0
            """,
            ids,
        )
        archived_ids = sorted(row[0] for row in await cursor.fetchall())
        if archived_ids:
            selected = ",".join("?" for _ in archived_ids)
            await db.execute(
                f"""
                UPDATE books
                SET is_archived = 1, is_active = 0
                WHERE id IN ({selected})
                  AND is_active = 1
                  AND COALESCE(is_archived, 0) = 0
                """,
                archived_ids,
            )
        await db.commit()
    archived_set = set(archived_ids)
    return {
        "archived_ids": archived_ids,
        "skipped_ids": [book_id for book_id in ids if book_id not in archived_set],
    }


async def reassign_active_books_category(book_ids, category_id: int) -> dict[str, list[int]]:
    ids = _normalize_book_ids(book_ids)
    if isinstance(category_id, bool) or not isinstance(category_id, int) or category_id <= 0:
        raise ValueError("category ID must be a positive integer")
    if not ids:
        return {"updated_ids": [], "skipped_ids": []}
    placeholders = ",".join("?" for _ in ids)
    async with connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT name FROM categories WHERE id = ? AND is_active = 1",
                (category_id,),
            )
            category = await cursor.fetchone()
            if category is None:
                raise ValueError("category is unavailable")
            cursor = await db.execute(
                f"""
                SELECT id FROM books
                WHERE id IN ({placeholders})
                  AND is_active = 1
                  AND COALESCE(is_archived, 0) = 0
                """,
                ids,
            )
            updated_ids = sorted(row[0] for row in await cursor.fetchall())
            if updated_ids:
                selected = ",".join("?" for _ in updated_ids)
                await db.execute(
                    f"""
                    UPDATE books
                    SET category = ?, category_id = ?
                    WHERE id IN ({selected})
                      AND is_active = 1
                      AND COALESCE(is_archived, 0) = 0
                    """,
                    (category[0], category_id, *updated_ids),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    updated = set(updated_ids)
    return {
        "updated_ids": updated_ids,
        "skipped_ids": [book_id for book_id in ids if book_id not in updated],
    }

async def delete_book(book_id: int) -> bool:
    """Мягко удалить (архивировать) одну активную книгу."""
    result = await archive_books([book_id])
    archived = bool(result["archived_ids"])
    return archived


async def restore_book(book_id: int) -> bool:
    """Вернуть книгу из архива в каталог (is_archived=0, is_active=1)."""
    from runtime.context import maybe_current_tenant_context
    from runtime.features import require_feature
    from runtime.quota import require_count_quota

    tenant_context = maybe_current_tenant_context()
    if tenant_context is not None:
        require_feature(FEATURE_CATALOG, tenant_context)
    async with connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT 1 FROM books WHERE id = ? AND is_archived = 1", (book_id,)
            )
            if await cursor.fetchone() is None:
                await db.commit()
                return False
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM books
                WHERE is_active = 1 AND COALESCE(is_archived, 0) = 0
                """
            )
            active_books = (await cursor.fetchone())[0]
            require_count_quota(LIMIT_BOOKS, active_books, context=tenant_context)
            await db.execute(
                "UPDATE books SET is_archived = 0, is_active = 1 WHERE id = ?",
                (book_id,),
            )
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise


async def get_archived_books(limit: int | None = None, offset: int = 0) -> list:
    """Получить архивные книги вместе с числом исторических позиций заказа."""
    suffix = "" if limit is None else " LIMIT ? OFFSET ?"
    params = () if limit is None else (limit, offset)
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT b.id, b.title, b.price, b.category, b.emoji, b.category_id,
                       c.emoji AS category_emoji,
                       (SELECT COUNT(*) FROM order_items oi WHERE oi.book_id = b.id) AS order_items_count
                FROM books b
                LEFT JOIN categories c ON b.category_id = c.id
                WHERE b.is_archived = 1
                ORDER BY b.created_at DESC, b.id DESC{suffix}""",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_archived_books_count() -> int:
    async with connection() as db:
        cursor = await db.execute("SELECT COUNT(*) FROM books WHERE is_archived = 1")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def _classify_archived_book_ids(db, ids: list[int]) -> dict[str, list[int]]:
    if not ids:
        return {"eligible_ids": [], "referenced_ids": [], "not_archived_ids": [], "missing_ids": []}
    placeholders = ",".join("?" for _ in ids)
    cursor = await db.execute(
        f"""SELECT b.id, b.is_archived,
                   EXISTS(SELECT 1 FROM order_items oi WHERE oi.book_id = b.id) AS has_order_items
            FROM books b
            WHERE b.id IN ({placeholders})""",
        ids,
    )
    rows = {row[0]: row for row in await cursor.fetchall()}
    eligible_ids = []
    referenced_ids = []
    not_archived_ids = []
    missing_ids = []
    for book_id in ids:
        row = rows.get(book_id)
        if row is None:
            missing_ids.append(book_id)
        elif not row[1]:
            not_archived_ids.append(book_id)
        elif row[2]:
            referenced_ids.append(book_id)
        else:
            eligible_ids.append(book_id)
    return {
        "eligible_ids": eligible_ids,
        "referenced_ids": referenced_ids,
        "not_archived_ids": not_archived_ids,
        "missing_ids": missing_ids,
    }


async def classify_archived_book_ids(book_ids) -> dict[str, list[int]]:
    ids = _normalize_book_ids(book_ids)
    async with connection() as db:
        return await _classify_archived_book_ids(db, ids)


async def purge_archived_books(book_ids) -> dict[str, list[int]]:
    """Безвозвратно удалить архивные книги без истории заказов."""
    ids = _normalize_book_ids(book_ids)
    if not ids:
        return {"deleted_ids": [], "referenced_ids": [], "not_archived_ids": [], "missing_ids": []}
    async with connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            classified = await _classify_archived_book_ids(db, ids)
            deleted_ids = classified["eligible_ids"]
            if deleted_ids:
                placeholders = ",".join("?" for _ in deleted_ids)
                await db.execute(
                    f"""DELETE FROM books
                        WHERE id IN ({placeholders})
                          AND is_archived = 1
                          AND NOT EXISTS (
                              SELECT 1 FROM order_items oi WHERE oi.book_id = books.id
                          )""",
                    deleted_ids,
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return {"deleted_ids": deleted_ids, **{key: classified[key] for key in classified if key != "eligible_ids"}}


async def update_book(book_id: int, **kwargs) -> bool:
    """Обновить поля книги"""
    async with connection() as db:
        updates = []
        params = []

        for key, value in kwargs.items():
            if value is not None:
                updates.append(f"{key} = ?")
                params.append(value)

        if not updates:
            return False

        params.append(book_id)
        query = f"UPDATE books SET {', '.join(updates)} WHERE id = ?"

        await db.execute(query, params)
        await db.commit()
    return True


async def update_book_full(book_id: int, **kwargs) -> bool:
    """Полное обновление книги"""
    async with connection() as db:
        updates = []
        params = []

        for key, value in kwargs.items():
            if value is not None:
                updates.append(f"{key} = ?")
                params.append(value)

        if not updates:
            return False

        params.append(book_id)
        query = f"UPDATE books SET {', '.join(updates)} WHERE id = ?"

        await db.execute(query, params)
        await db.commit()
    return True


async def set_book_stock(book_id: int, stock_quantity: int | None) -> bool:
    if stock_quantity is not None and stock_quantity < 0:
        raise ValueError("stock quantity must be non-negative")
    async with connection() as db:
        cursor = await db.execute(
            "UPDATE books SET stock_quantity = ? WHERE id = ?",
            (stock_quantity, book_id),
        )
        await db.commit()
    return cursor.rowcount > 0


async def get_books_count(is_active: bool = True, search_query: str = "") -> int:
    """Получить количество книг с опциональным поиском по названию.

    Поиск регистронезависимый и матчит подстроку в title, чтобы админ
    мог быстро найти книгу, созданную «условно несколько месяцев назад».
    """
    async with connection() as db:
        if search_query:
            like = f"%{search_query}%"
            cursor = await db.execute(
                "SELECT COUNT(*) FROM books WHERE is_active = ? AND COALESCE(is_archived, 0) = 0 AND LOWER(title) LIKE LOWER(?)",
                (1 if is_active else 0, like),
            )
        else:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM books WHERE is_active = ? AND COALESCE(is_archived, 0) = 0",
                (1 if is_active else 0,),
            )
        result = await cursor.fetchone()
        return result[0] if result else 0


# ORDER BY сортировки для админского списка книг. Каждый ключ — это
# callback_data `admin_books_sort_<key>`, см. handlers/admin_books.py.
_BOOKS_SORT_ORDERS = {
    "default": "b.sort_order ASC, b.id ASC",
    "title": "b.title COLLATE NOCASE ASC",
    "category": "b.category COLLATE NOCASE ASC, b.id ASC",
    "date_new": "b.created_at DESC, b.id DESC",
    "date_old": "b.created_at ASC, b.id ASC",
}


async def get_all_books_paginated(
    limit: int = PAGE_SIZE,
    offset: int = 0,
    sort_by: str = "default",
    search_query: str = "",
) -> list:
    """Получить книги с пагинацией, сортировкой и поиском.

    sort_by: ключ из _BOOKS_SORT_ORDERS; неизвестные значения трактуются
    как «default», чтобы случайный callback не сломал SQL.
    search_query: подстрока для LOWER(title) LIKE LOWER('%…%').
    """
    order_clause = _BOOKS_SORT_ORDERS.get(sort_by, _BOOKS_SORT_ORDERS["default"])

    async with connection() as db:
        db.row_factory = aiosqlite.Row
        if search_query:
            like = f"%{search_query}%"
            cursor = await db.execute(
                f"""SELECT b.id, b.title, b.price, b.category, b.emoji, b.description, b.images,
                           b.category_id, b.sort_order, b.created_at, b.stock_quantity,
                           c.emoji as category_emoji
                    FROM books b
                    LEFT JOIN categories c ON b.category_id = c.id
                    WHERE b.is_active = 1 AND COALESCE(b.is_archived, 0) = 0 AND LOWER(b.title) LIKE LOWER(?)
                    ORDER BY {order_clause}
                    LIMIT ? OFFSET ?""",
                (like, limit, offset),
            )
        else:
            cursor = await db.execute(
                f"""SELECT b.id, b.title, b.price, b.category, b.emoji, b.description, b.images,
                           b.category_id, b.sort_order, b.created_at, b.stock_quantity,
                           c.emoji as category_emoji
                    FROM books b
                LEFT JOIN categories c ON b.category_id = c.id
                    WHERE b.is_active = 1 AND COALESCE(b.is_archived, 0) = 0
                    ORDER BY {order_clause}
                    LIMIT ? OFFSET ?""",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
