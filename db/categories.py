"""Модуль для работы с категориями книг"""
import aiosqlite
from db.connection import connection




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
