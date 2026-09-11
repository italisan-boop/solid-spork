"""Пакет модулей для работы с базой данных"""
import aiosqlite

DB_NAME = "semena_znaniy.db"

from db.categories import (
    init_categories,
    get_all_categories,
    add_category,
    update_category,
    delete_category,
    get_category_books_count
)

from db.promo_codes import (
    init_promo_codes,
    get_all_promo_codes,
    get_promo_code,
    add_promo_code,
    update_promo_code,
    delete_promo_code,
    increment_promo_usage,
    validate_promo_code
)

from db.referrals import (
    init_referrals,
    get_referral_code,
    parse_referral_code,
    check_referral_exists,
    create_referral,
    add_user_bonus,
    get_user_active_bonus,
    mark_bonus_used,
    get_referral_stats
)

from db.payments import (
    init_payments,
    get_payment_setting,
    set_payment_setting,
    get_all_payment_settings
)

from db.stars import (
    init_stars,
    get_stars_setting,
    set_stars_setting,
    rubles_to_stars
)

from db.orders import (
    create_order,
    get_order,
    get_order_full,
    get_user_orders,
    update_order_status,
    get_all_orders,
    get_orders_count,
    get_stats,
    get_all_unique_users
)

from db.users import (
    get_all_users,
    get_user,
    add_user,
    set_support_active,
    is_support_active,
    get_all_support_active_user_ids,
)

from db.books import (
    add_book,
    get_all_books,
    update_book_sort_order,
    get_book,
    delete_book,
    update_book,
    update_book_full
)


async def init_db():
    """Создание таблиц и миграция схемы"""
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Миграция: флаг активного диалога с поддержкой (выживает рестарт бота)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_support_active INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # колонка уже есть
        
        # Таблица заказов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                total INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Добавляем колонку для хранения ID сообщений уведомлений админам
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN admin_notification_ids TEXT DEFAULT '[]'")
        except Exception:
            pass  # Колонка уже существует

        # Таблица позиций заказа
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                book_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                price INTEGER NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders (id)
            )
        """)

        # Таблица книг
        await db.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                price INTEGER NOT NULL,
                category TEXT NOT NULL,
                category_id INTEGER DEFAULT 0,
                author TEXT DEFAULT '',
                description TEXT DEFAULT '',
                cover_photo TEXT DEFAULT '',
                images TEXT DEFAULT '[]',
                emoji TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            )
        """)

        # Таблица категорий
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                emoji TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                sort_order INTEGER DEFAULT 0
            )
        """)

        # ==========================================
        # МИГРАЦИИ: добавляем новые колонки
        # ==========================================
        cursor = await db.execute("PRAGMA table_info(books)")
        columns_info = await cursor.fetchall()
        existing_columns = [col[1] for col in columns_info]

        if 'description' not in existing_columns:
            await db.execute("ALTER TABLE books ADD COLUMN description TEXT DEFAULT ''")
            print("✅ Добавлена колонка 'description'")

        if 'sort_order' not in existing_columns:
            await db.execute("ALTER TABLE books ADD COLUMN sort_order INTEGER DEFAULT 0")
            print("✅ Добавлена колонка 'sort_order'")

        if 'images' not in existing_columns:
            await db.execute("ALTER TABLE books ADD COLUMN images TEXT DEFAULT '[]'")
            print("✅ Добавлена колонка 'images'")

        if 'category_id' not in existing_columns:
            await db.execute("ALTER TABLE books ADD COLUMN category_id INTEGER")
            print("✅ Добавлена колонка 'category_id'")

        if 'author' not in existing_columns:
            await db.execute("ALTER TABLE books ADD COLUMN author TEXT DEFAULT ''")
            print("✅ Добавлена колонка 'author'")

        if 'cover_photo' not in existing_columns:
            await db.execute("ALTER TABLE books ADD COLUMN cover_photo TEXT")
            print("✅ Добавлена колонка 'cover_photo'")

        if 'created_at' not in existing_columns:
            await db.execute(
                "ALTER TABLE books ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            )
            # Проставляем created_at для существующих книг, чтобы сортировка
            # «по дате добавления» не свалила их всех в один «сейчас».
            await db.execute(
                "UPDATE books SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
            )
            print("✅ Добавлена колонка 'created_at'")

        # Инициализация категорий
        await init_categories()

        # Связываем книги с категориями
        if 'category_id' not in existing_columns:
            await db.execute("""
                UPDATE books SET category_id = (
                    SELECT id FROM categories
                    WHERE categories.name = books.category
                    LIMIT 1
                )
            """)
            print("✅ Существующие книги связаны с категориями")

        # Добавляем книги по умолчанию
        cursor = await db.execute("SELECT COUNT(*) FROM books")
        row = await cursor.fetchone()
        count = row[0] if row else 0

        if count == 0:
            cursor = await db.execute("SELECT id, name FROM categories")
            cat_rows = await cursor.fetchall()
            cat_map = {name: cat_id for cat_id, name in cat_rows}

            default_books = [
                ("Атлас лекарственных растений", 1200, "Ботаника", "",
                 "Подробный атлас с описанием более 500 лекарственных растений.",
                 '["https://images.unsplash.com/photo-1544947950-fa07a98d237f?w=400"]',
                 cat_map.get("Ботаника")),
                ("Тайная жизнь деревьев", 850, "Природа", "🌳",
                 "Увлекательное исследование лесных экосистем.",
                 '["https://images.unsplash.com/photo-1448375240586-882707db888b?w=400"]',
                 cat_map.get("Природа")),
                ("Ботанические иллюстрации", 2500, "Искусство", "🎨",
                 "Роскошный альбом с акварельными иллюстрациями.",
                 '["https://images.unsplash.com/photo-1490750967868-88aa4486c946?w=400"]',
                 cat_map.get("Искусство")),
                ("Сад на подоконнике", 650, "Садоводство", "",
                 "Практическое руководство по выращиванию растений.",
                 '["https://images.unsplash.com/photo-1416879595882-3373a0480b5b?w=400"]',
                 cat_map.get("Садоводство")),
                ("Энциклопедия трав", 1800, "Травник", "🌾",
                 "Полная энциклопедия лекарственных трав.",
                 '["https://images.unsplash.com/photo-1466692476868-aef1dfb1e735?w=400"]',
                 cat_map.get("Травник")),
                ("Цветы мира", 1500, "Флористика", "🌸",
                 "Красочный путеводитель по цветам.",
                 '["https://images.unsplash.com/photo-1490750967868-88aa4486c946?w=400"]',
                 cat_map.get("Флористика"))
            ]
            await db.executemany(
                "INSERT INTO books (title, price, category, emoji, description, images, category_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                default_books
            )
            print("✅ Добавлены книги по умолчанию")

        # Инициализация промокодов
        await init_promo_codes()

        # Инициализация рефералов
        await init_referrals()

        # Инициализация настроек оплаты
        await init_payments()

        # Инициализация настроек Stars
        await init_stars()

        await db.commit()
    print(f"✅ База данных инициализирована: {DB_NAME}")
