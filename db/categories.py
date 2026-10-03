"""Модуль для работы с категориями книг"""
import aiosqlite
import sqlite3

from db.connection import connection
from db.schema import connect




async def get_all_categories() -> list:
    """Получить все активные категории"""
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, name, emoji, sort_order FROM categories WHERE is_active = 1 ORDER BY sort_order ASC, name ASC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def add_category(name: str, emoji: str = "", sort_order: int = 0) -> int:
    """Добавить новую категорию"""
    async with connection() as db:
        cursor = await db.execute(
            "INSERT INTO categories (name, emoji, sort_order) VALUES (?, ?, ?)",
            (name, emoji, sort_order)
        )
        await db.commit()
        return cursor.lastrowid


async def update_category(category_id: int, **kwargs) -> bool:
    """Обновить категорию.

    books.category — это денормализованная копия названия категории;
    mini app фильтрует книги именно по ней. Если переименовать категорию
    и оставить books.category как есть, карточки «пропадают» из вкладки
    с новым названием (хотя JOIN по category_id продолжает возвращать
    корректный emoji). Поэтому при изменении имени каскадим апдейт в
    books.category — держим денормализацию согласованной.
    """
    async with connection() as db:
        updates = []
        params = []
        for key, value in kwargs.items():
            if value is not None:
                updates.append(f"{key} = ?")
                params.append(value)
        if not updates:
            return False
        params.append(category_id)
        query = f"UPDATE categories SET {', '.join(updates)} WHERE id = ?"
        await db.execute(query, params)

        # Каскад переименования в books.category, чтобы mini app продолжал
        # находить книги по новому названию вкладки.
        if 'name' in kwargs and kwargs['name'] is not None:
            await db.execute(
                "UPDATE books SET category = ? WHERE category_id = ?",
                (kwargs['name'], category_id),
            )

        await db.commit()
        return True


async def delete_category(category_id: int) -> dict:
    """Удалить категорию"""
    async with connection() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM books WHERE category_id = ? AND is_active = 1",
            (category_id,)
        )
        row = await cursor.fetchone()
        books_count = row[0] if row else 0

        if books_count > 0:
            return {'success': False, 'books_count': books_count}

        await db.execute(
            "UPDATE categories SET is_active = 0 WHERE id = ?",
            (category_id,)
        )
        await db.commit()
        return {'success': True, 'books_count': 0}


async def get_category_books_count(category_id: int) -> int:
    """Получить количество книг в категории"""
    async with connection() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM books WHERE category_id = ? AND is_active = 1",
            (category_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


def _catalog_category_payload(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "emoji": row["emoji"] or "",
        "sort_order": row["sort_order"],
        "books_count": row["books_count"],
    }


def list_catalog_categories_sync() -> list[dict]:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            """
            SELECT c.id, c.name, c.emoji, c.sort_order,
                   COUNT(b.id) AS books_count
            FROM categories c
            LEFT JOIN books b ON b.category_id = c.id
                AND b.is_active = 1 AND COALESCE(b.is_archived, 0) = 0
            WHERE c.is_active = 1
            GROUP BY c.id
            ORDER BY c.sort_order ASC, c.name ASC
            """
        ).fetchall()
        return [_catalog_category_payload(row) for row in rows]
    finally:
        database.close()


def _audit_category(
    database: sqlite3.Connection,
    *,
    actor_user_id: int,
    actor_role: str,
    action: str,
    category_id: int,
    reason_code: str,
) -> None:
    from db.audit import append_audit_event

    append_audit_event(
        database,
        actor_user_id=actor_user_id,
        actor_role=actor_role,
        source="mini_app",
        action=action,
        entity_type="category",
        entity_id=category_id,
        details={"reason_code": reason_code},
    )


def _catalog_category_row(database: sqlite3.Connection, category_id: int):
    return database.execute(
        """
        SELECT c.id, c.name, c.emoji, c.sort_order,
               COUNT(b.id) AS books_count
        FROM categories c
        LEFT JOIN books b ON b.category_id = c.id
            AND b.is_active = 1 AND COALESCE(b.is_archived, 0) = 0
        WHERE c.id = ? AND c.is_active = 1
        GROUP BY c.id
        """,
        (category_id,),
    ).fetchone()


def create_catalog_category_sync(
    name: str, emoji: str, *, actor_user_id: int, actor_role: str
) -> dict:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        existing = database.execute(
            "SELECT 1 FROM categories WHERE is_active = 1 AND unicode_casefold(name) = unicode_casefold(?)",
            (name,),
        ).fetchone()
        if existing:
            raise ValueError("category already exists")
        sort_order = database.execute(
            "SELECT COALESCE(MAX(sort_order) + 1, 0) FROM categories WHERE is_active = 1"
        ).fetchone()[0]
        cursor = database.execute(
            "INSERT INTO categories (name, emoji, sort_order) VALUES (?, ?, ?)",
            (name, emoji, sort_order),
        )
        category_id = cursor.lastrowid
        _audit_category(
            database,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            action="catalog.category.created",
            category_id=category_id,
            reason_code="created",
        )
        row = _catalog_category_row(database, category_id)
        database.commit()
        return _catalog_category_payload(row)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def update_catalog_category_sync(
    category_id: int, name: str, emoji: str, *, actor_user_id: int, actor_role: str
) -> dict | None:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        if _catalog_category_row(database, category_id) is None:
            database.rollback()
            return None
        duplicate = database.execute(
            """
            SELECT 1 FROM categories
            WHERE is_active = 1 AND id != ? AND unicode_casefold(name) = unicode_casefold(?)
            """,
            (category_id, name),
        ).fetchone()
        if duplicate:
            raise ValueError("category already exists")
        database.execute(
            "UPDATE categories SET name = ?, emoji = ? WHERE id = ? AND is_active = 1",
            (name, emoji, category_id),
        )
        database.execute("UPDATE books SET category = ? WHERE category_id = ?", (name, category_id))
        _audit_category(
            database,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            action="catalog.category.updated",
            category_id=category_id,
            reason_code="metadata",
        )
        row = _catalog_category_row(database, category_id)
        database.commit()
        return _catalog_category_payload(row)
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def deactivate_catalog_category_sync(
    category_id: int, *, actor_user_id: int, actor_role: str
) -> dict:
    database = connect()
    database.row_factory = sqlite3.Row
    try:
        database.execute("BEGIN IMMEDIATE")
        row = _catalog_category_row(database, category_id)
        if row is None:
            database.rollback()
            return {"found": False, "books_count": 0}
        books_count = row["books_count"]
        if books_count:
            database.rollback()
            return {"found": True, "books_count": books_count}
        database.execute("UPDATE categories SET is_active = 0 WHERE id = ?", (category_id,))
        _audit_category(
            database,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            action="catalog.category.deactivated",
            category_id=category_id,
            reason_code="deactivated",
        )
        database.commit()
        return {"found": True, "books_count": 0}
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


NO_CATEGORY_NAME = "Без категории"
NO_CATEGORY_EMOJI = "📦"


async def get_category_by_id(category_id: int) -> dict | None:
    """Получить категорию по id, или None если её нет/она скрыта."""
    if not category_id:
        return None
    async with connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, name, emoji FROM categories WHERE id = ? AND is_active = 1",
            (category_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


def category_display(category: dict | None) -> dict:
    """Нормализовать категорию к виду {name, emoji} для UI.

    Если категория None / пустая / нет в БД / заглушка «Неизвестно»,
    возвращает «Без категории», чтобы карточка в каталоге никогда не
    показывалась с пустой строкой или устаревшим плейсхолдером.
    """
    if not category:
        return {'name': NO_CATEGORY_NAME, 'emoji': NO_CATEGORY_EMOJI}
    name = (category.get('name') or '').strip()
    # Старые книги без категории сохранялись как «Неизвестно» — продолжаем
    # показывать их под «Без категории», чтобы вкладки и фильтры каталога
    # работали одинаково.
    if not name or name == 'Неизвестно':
        return {'name': NO_CATEGORY_NAME, 'emoji': NO_CATEGORY_EMOJI}
    return category
