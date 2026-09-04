"""Модуль для работы с книгами (каталог)"""
import aiosqlite
from db import DB_NAME


async def add_book(title: str, price: int, category: str, emoji: str = "",
                   description: str = "", images: str = "[]", category_id: int = None) -> int:
    """Добавить новую книгу в каталог"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO books (title, price, category, emoji, description, images, category_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, price, category, emoji, description, images, category_id)
        )
        await db.commit()
        return cursor.lastrowid


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
            """SELECT b.id, b.title, b.price, b.category, b.emoji, b.description, b.images, b.category_id,
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
