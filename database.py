import aiosqlite

DB_NAME = "semena_znaniy.db"


# ============================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
# ============================================

async def init_db():
    """Создание таблиц и миграция схемы"""
    async with aiosqlite.connect(DB_NAME) as db:
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
                emoji TEXT DEFAULT '',
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


# ============================================
# КАТЕГОРИИ
# ============================================

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


# ============================================
# ПРОМОКОДЫ
# ============================================

async def init_promo_codes():
    """Создание таблицы промокодов"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                discount_percent INTEGER NOT NULL DEFAULT 0,
                discount_fixed INTEGER DEFAULT 0,
                min_order INTEGER DEFAULT 0,
                max_uses INTEGER DEFAULT 0,
                current_uses INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT
            )
        """)
        await db.commit()
    print("✅ Таблица промокодов инициализирована")


async def get_all_promo_codes() -> list:
    """Получить все промокоды"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM promo_codes ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_promo_code(code: str) -> dict:
    """Получить промокод по коду"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM promo_codes WHERE code = ? AND is_active = 1",
            (code.upper(),)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def add_promo_code(code: str, discount_percent: int = 0, discount_fixed: int = 0,
                         min_order: int = 0, max_uses: int = 0, expires_at: str = None) -> int:
    """Добавить новый промокод"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """INSERT INTO promo_codes (code, discount_percent, discount_fixed, 
               min_order, max_uses, expires_at) 
               VALUES (?, ?, ?, ?, ?, ?)""",
            (code.upper(), discount_percent, discount_fixed, min_order, max_uses, expires_at)
        )
        await db.commit()
        return cursor.lastrowid


async def update_promo_code(promo_id: int, **kwargs) -> bool:
    """Обновить промокод"""
    async with aiosqlite.connect(DB_NAME) as db:
        updates = []
        params = []
        for key, value in kwargs.items():
            if value is not None:
                updates.append(f"{key} = ?")
                params.append(value)
        if not updates:
            return False
        params.append(promo_id)
        query = f"UPDATE promo_codes SET {', '.join(updates)} WHERE id = ?"
        await db.execute(query, params)
        await db.commit()
        return True


async def delete_promo_code(promo_id: int):
    """Удалить промокод"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM promo_codes WHERE id = ?", (promo_id,))
        await db.commit()


async def increment_promo_usage(code: str):
    """Увеличить счётчик использований промокода"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE promo_codes SET current_uses = current_uses + 1 WHERE code = ?",
            (code.upper(),)
        )
        await db.commit()


async def validate_promo_code(code: str, order_total: int) -> dict:
    """Проверить промокод и вернуть информацию о скидке"""
    promo = await get_promo_code(code.upper())

    if not promo:
        return {'valid': False, 'error': 'Промокод не найден'}

    if promo.get('expires_at'):
        from datetime import datetime
        try:
            expires = datetime.fromisoformat(promo['expires_at'])
            if datetime.now() > expires:
                return {'valid': False, 'error': 'Промокод истёк'}
        except Exception:
            pass

    if promo['min_order'] > 0 and order_total < promo['min_order']:
        return {'valid': False, 'error': f"Минимальная сумма заказа: {promo['min_order']} ₽"}

    if promo['max_uses'] > 0 and promo['current_uses'] >= promo['max_uses']:
        return {'valid': False, 'error': 'Промокод больше не действует'}

    discount = 0
    if promo['discount_percent'] > 0:
        discount = int(order_total * promo['discount_percent'] / 100)
    elif promo['discount_fixed'] > 0:
        discount = promo['discount_fixed']

    discount = min(discount, order_total)

    return {
        'valid': True,
        'discount': discount,
        'discount_percent': promo['discount_percent'],
        'discount_fixed': promo['discount_fixed'],
        'final_total': order_total - discount
    }


# ============================================
# РЕФЕРАЛЬНАЯ ПРОГРАММА
# ============================================

async def init_referrals():
    """Создание таблиц для реферальной программы"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                bonus_given INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_bonuses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bonus_type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                is_used INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()
    print("✅ Таблицы реферальной программы инициализированы")


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
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id FROM referrals WHERE referred_id = ?", (referred_id,)
        )
        row = await cursor.fetchone()
        return row is not None


async def create_referral(referrer_id: int, referred_id: int):
    """Создать реферальную связь"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
            (referrer_id, referred_id)
        )
        await db.commit()


async def add_user_bonus(user_id: int, bonus_type: str, amount: int):
    """Начислить бонус пользователю"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO user_bonuses (user_id, bonus_type, amount) VALUES (?, ?, ?)",
            (user_id, bonus_type, amount)
        )
        await db.commit()


async def get_user_active_bonus(user_id: int) -> dict:
    """Получить неиспользованный бонус пользователя"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM user_bonuses WHERE user_id = ? AND is_used = 0 ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def mark_bonus_used(bonus_id: int):
    """Отметить бонус как использованный"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE user_bonuses SET is_used = 1 WHERE id = ?", (bonus_id,)
        )
        await db.commit()


async def get_referral_stats(user_id: int) -> dict:
    """Статистика по рефералам пользователя"""
    async with aiosqlite.connect(DB_NAME) as db:
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


# ============================================
# НАСТРОЙКИ ОПЛАТЫ
# ============================================

async def init_payments():
    """Создание таблицы настроек оплаты"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payment_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setting_key TEXT NOT NULL UNIQUE,
                setting_value TEXT NOT NULL
            )
        """)

        cursor = await db.execute("SELECT COUNT(*) FROM payment_settings")
        count = (await cursor.fetchone())[0]

        if count == 0:
            default_settings = [
                ('payment_enabled', '1'),
                ('card_number', ''),
                ('sbp_phone', ''),
                ('sbp_bank', ''),
                ('recipient_name', ''),
                ('payment_instructions', 'После перевода укажите номер заказа в комментарии')
            ]
            await db.executemany(
                "INSERT INTO payment_settings (setting_key, setting_value) VALUES (?, ?)",
                default_settings
            )
            print("✅ Добавлены настройки оплаты по умолчанию")

        await db.commit()
    print("✅ Таблица настроек оплаты инициализирована")


async def get_payment_setting(key: str, default: str = '') -> str:
    """Получить настройку оплаты"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT setting_value FROM payment_settings WHERE setting_key = ?",
            (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_payment_setting(key: str, value: str):
    """Установить настройку оплаты"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """INSERT INTO payment_settings (setting_key, setting_value) 
               VALUES (?, ?) 
               ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value""",
            (key, value)
        )
        await db.commit()


async def get_all_payment_settings() -> dict:
    """Получить все настройки оплаты"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT setting_key, setting_value FROM payment_settings")
        rows = await cursor.fetchall()
        return {row['setting_key']: row['setting_value'] for row in rows}


# ============================================
# TELEGRAM STARS
# ============================================

async def init_stars():
    """Создание таблицы настроек Stars"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stars_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setting_key TEXT NOT NULL UNIQUE,
                setting_value TEXT NOT NULL
            )
        """)

        cursor = await db.execute("SELECT COUNT(*) FROM stars_settings")
        count = (await cursor.fetchone())[0]

        if count == 0:
            default_settings = [
                ('stars_enabled', '0'),
                ('rubles_per_star', '2')
            ]
            await db.executemany(
                "INSERT INTO stars_settings (setting_key, setting_value) VALUES (?, ?)",
                default_settings
            )
            print("✅ Добавлены настройки Stars по умолчанию")

        await db.commit()
    print("✅ Таблица настроек Stars инициализирована")


async def get_stars_setting(key: str, default: str = '') -> str:
    """Получить настройку Stars"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT setting_value FROM stars_settings WHERE setting_key = ?",
            (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_stars_setting(key: str, value: str):
    """Установить настройку Stars"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """INSERT INTO stars_settings (setting_key, setting_value) 
               VALUES (?, ?) 
               ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value""",
            (key, value)
        )
        await db.commit()


async def rubles_to_stars(rubles: int) -> int:
    """Конвертировать рубли в Stars"""
    rubles_per_star = int(await get_stars_setting('rubles_per_star', '2'))
    stars = max(1, rubles // rubles_per_star)
    return stars


# ============================================
# ЗАКАЗЫ
# ============================================

async def create_order(user_id: int, user_name: str, cart: list, total: int) -> int:
    """Создать новый заказ"""
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO orders (user_id, user_name, total, status) VALUES (?, ?, ?, 'new')",
            (user_id, user_name, total)
        )
        order_id = cursor.lastrowid

        for book in cart:
            await db.execute(
                "INSERT INTO order_items (order_id, book_id, title, price) VALUES (?, ?, ?, ?)",
                (order_id, book['id'], book['title'], book['price'])
            )

        await db.commit()
    return order_id


async def get_order(order_id: int) -> dict:
    """Получить заказ по ID"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_order_full(order_id: int) -> dict:
    """Получить заказ со всеми товарами"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        order = await cursor.fetchone()
        if not order:
            return None

        cursor = await db.execute(
            "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
        )
        items = await cursor.fetchall()

        return {
            'id': order['id'],
            'user_id': order['user_id'],
            'user_name': order['user_name'],
            'total': order['total'],
            'status': order['status'],
            'created_at': order['created_at'],
            'items': [dict(item) for item in items]
        }


async def get_user_orders(user_id: int, limit: int = 10) -> list:
    """Получить заказы пользователя"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_order_status(order_id: int, status: str):
    """Обновить статус заказа"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (status, order_id)
        )
        await db.commit()
    print(f"✅ Статус заказа #{order_id} изменён на '{status}'")


# ============================================
# АДМИН-ПАНЕЛЬ
# ============================================

async def get_all_orders(limit: int = 20, offset: int = 0, status: str = None) -> list:
    """Получить заказы с пагинацией"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row

        if status:
            cursor = await db.execute(
                """SELECT * FROM orders
                   WHERE status = ?
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (status, limit, offset)
            )
        else:
            cursor = await db.execute(
                """SELECT * FROM orders
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset)
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

async def get_orders_count(status: str = None) -> int:
    """Получить количество заказов (с фильтром по статусу)"""
    async with aiosqlite.connect(DB_NAME) as db:
        if status:
            cursor = await db.execute(
                "SELECT COUNT(*) as cnt FROM orders WHERE status = ?",
                (status,)
            )
        else:
            cursor = await db.execute("SELECT COUNT(*) as cnt FROM orders")
        row = await cursor.fetchone()
        return row[0] if row else 0

async def get_stats() -> dict:
    """Получить статистику магазина"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("SELECT COUNT(*) as cnt FROM orders")
        total_orders = (await cursor.fetchone())['cnt']

        cursor = await db.execute(
            "SELECT status, COUNT(*) as cnt, SUM(total) as sum FROM orders GROUP BY status"
        )
        by_status = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT SUM(total) as sum FROM orders WHERE status != 'cancelled'"
        )
        total_revenue = (await cursor.fetchone())['sum'] or 0

        return {
            'total_orders': total_orders,
            'total_revenue': total_revenue,
            'by_status': [dict(s) for s in by_status]
        }


async def get_all_unique_users() -> list:
    """Получить всех уникальных пользователей"""
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT DISTINCT user_id FROM orders")
        rows = await cursor.fetchall()
        return [r['user_id'] for r in rows]


# ============================================
# КАТАЛОГ КНИГ
# ============================================

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