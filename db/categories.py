"""Модуль для работы с категориями книг"""
import aiosqlite
from db import DB_NAME


async def init_categories():
    """Создание таблицы категорий и добавление дефолтных"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM categories")
        row = await cursor.fetchone()
        count = row[0] if row else 0

        if count == 0:
            default_categories = [
                ("Ботаника", "🌿", 1),
                ("Природа", "🌳", 2),
                ("Искусство", "🎨", 3),
                ("Садоводство", "🌱", 4),
                ("Травник", "🌾", 5),
                ("Флористика", "🌸", 6)
            ]
            await db.executemany(
                "INSERT INTO categories (name, emoji, sort_order) VALUES (?, ?, ?)",
                default_categories
            )
            print("✅ Добавлены категории по умолчанию")

        await db.commit()


async def get_all_categories() -> list:
    """Получить все активные категории"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, name, emoji, sort_order FROM categories WHERE is_active = 1 ORDER BY sort_order ASC, name ASC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def add_category(name: str, emoji: str = "", sort_order: int = 0) -> int:
    """Добавить новую категорию"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO categories (name, emoji, sort_order) VALUES (?, ?, ?)",
            (name, emoji, sort_order)
        )
        await db.commit()
        return cursor.lastrowid


async def update_category(category_id: int, **kwargs) -> bool:
    """Обновить категорию"""
    async with aiosqlite.connect(DB_NAME) as db:
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
        await db.commit()
        return True


async def delete_category(category_id: int) -> dict:
    """Удалить категорию"""
    async with aiosqlite.connect(DB_NAME) as db:
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
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM books WHERE category_id = ? AND is_active = 1",
            (category_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
