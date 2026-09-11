"""Модуль для работы с книгами (каталог)"""
import aiosqlite
import json
from db import DB_NAME
from db.categories import NO_CATEGORY_NAME

PAGE_SIZE = 20  # Количество книг на странице


async def add_book(title: str, price: int, category_id: int,
                   author: str = "", description: str = "",
                   cover_photo: str = None, page_photos: list = None) -> int:
    """Добавить новую книгу в каталог"""
    # Преобразуем список фото страниц в JSON
    images_json = json.dumps(page_photos) if page_photos else "[]"

    # Получаем название категории по ID. Если категория не указана или
    # удалена — сохраняем плейсхолдер NO_CATEGORY_NAME, чтобы catalog.py /
    # Mini App корректно отрисовали «📦 Без категории» через category_display.
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT name FROM categories WHERE id = ?",
            (category_id,)
        )
        row = await cursor.fetchone()
        category_name = row['name'] if row else NO_CATEGORY_NAME

    async with aiosqlite.connect(DB_NAME) as db:
        # Зеркалим cover_photo в поле emoji — фронтенд Mini App и
        # catalog.py читают именно emoji. Без этого каталог рисует 📚
        # вместо присланной обложки.
        emoji_value = cover_photo or ''
        cursor = await db.execute(
            """INSERT INTO books (title, price, category, category_id, author, description, cover_photo, images, emoji)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, price, category_name, category_id, author, description, cover_photo, images_json, emoji_value)
        )
        await db.commit()
        return cursor.lastrowid


async def find_book_by_title_author(title: str, author: str) -> dict | None:
    """Найти активную книгу с тем же (title, author). Регистр и пробелы по краям
    игнорируются, чтобы дубликат ловился и для 'Книга' / 'книга' / ' Книга '.

    Возвращает {'id', 'title', 'author', 'category'} или None.
    Используется в админке для предупреждения о дубликатах при добавлении.
    """
    if not title:
        return None
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, title, author, category
               FROM books
               WHERE is_active = 1
                 AND LOWER(TRIM(title)) = LOWER(TRIM(?))
                 AND LOWER(TRIM(COALESCE(author, ''))) = LOWER(TRIM(COALESCE(?, '')))
               LIMIT 1""",
            (title, author or '')
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_all_books() -> list:
    """Получить все активные книги"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT b.id, b.title, b.price, b.category, b.emoji, b.description, b.images, b.category_id, b.sort_order,
                      c.emoji as category_emoji
               FROM books b
               LEFT JOIN categories c ON b.category_id = c.id
               WHERE b.is_active = 1
               ORDER BY b.sort_order ASC, b.id ASC"""
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_book_sort_order(book_id: int, sort_order: int):
    """Обновить порядок отображения книги"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE books SET sort_order = ? WHERE id = ?",
            (sort_order, book_id)
        )
        await db.commit()


async def get_book(book_id: int) -> dict:
    """Получить одну книгу по ID"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT b.id, b.title, b.price, b.category, b.emoji, b.description, b.images, b.category_id, b.sort_order,
                      c.emoji as category_emoji
               FROM books b
               LEFT JOIN categories c ON b.category_id = c.id
               WHERE b.id = ? AND b.is_active = 1""",
            (book_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_book(book_id: int):
    """Мягко удалить книгу"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE books SET is_active = 0 WHERE id = ?",
            (book_id,)
        )
        await db.commit()
    print(f"✅ Книга #{book_id} удалена из каталога")


async def update_book(book_id: int, **kwargs) -> bool:
    """Обновить поля книги"""
    async with aiosqlite.connect(DB_NAME) as db:
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
    async with aiosqlite.connect(DB_NAME) as db:
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


async def get_books_count(is_active: bool = True) -> int:
    """Получить количество книг"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM books WHERE is_active = ?",
            (1 if is_active else 0,)
        )
        result = await cursor.fetchone()
        return result[0] if result else 0


async def get_all_books_paginated(limit: int = PAGE_SIZE, offset: int = 0) -> list:
    """Получить книги с пагинацией"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT b.id, b.title, b.price, b.category, b.emoji, b.description, b.images, b.category_id, b.sort_order,
                      c.emoji as category_emoji
               FROM books b
               LEFT JOIN categories c ON b.category_id = c.id
               WHERE b.is_active = 1
               ORDER BY b.sort_order ASC, b.id ASC
               LIMIT ? OFFSET ?""",
            (limit, offset)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
