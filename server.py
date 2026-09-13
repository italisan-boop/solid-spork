from flask import Flask, request, jsonify, send_from_directory
import requests
import os
import sqlite3
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_NAME = "semena_znaniy.db"

# Читаем ID админов
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]

app = Flask(__name__, static_folder='.')


# ============================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
# ============================================

def init_db():
    """Создание таблиц и миграция схемы"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # Таблица заказов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            total INTEGER NOT NULL,
            status TEXT DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Таблица позиций заказа
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            price INTEGER NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        )
    ''')

    # Таблица книг
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price INTEGER NOT NULL,
            category TEXT NOT NULL,
            emoji TEXT DEFAULT '',
            description TEXT DEFAULT '',
            images TEXT DEFAULT '[]',
            category_id INTEGER,
            is_active INTEGER DEFAULT 1
        )
    ''')

    # Таблица категорий
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            emoji TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0
        )
    ''')

    # Таблица промокодов
    cursor.execute('''
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
    ''')

    # Таблица рефералов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL UNIQUE,
            bonus_given INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Таблица бонусов пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_bonuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bonus_type TEXT NOT NULL,
            amount INTEGER NOT NULL,
            is_used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Таблица настроек оплаты
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payment_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_key TEXT NOT NULL UNIQUE,
            setting_value TEXT NOT NULL
        )
    ''')

    # Таблица настроек Stars
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stars_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_key TEXT NOT NULL UNIQUE,
            setting_value TEXT NOT NULL
        )
    ''')

    conn.commit()

    # === МИГРАЦИЯ: категории по умолчанию ===
    cursor.execute("SELECT COUNT(*) FROM categories")
    count = cursor.fetchone()[0]

    if count == 0:
        default_categories = [
            ("Ботаника", "🌿", 1),
            ("Природа", "🌳", 2),
            ("Искусство", "🎨", 3),
            ("Садоводство", "🌱", 4),
            ("Травник", "🌾", 5),
            ("Флористика", "🌸", 6)
        ]
        cursor.executemany(
            "INSERT INTO categories (name, emoji, sort_order) VALUES (?, ?, ?)",
            default_categories
        )
        print("✅ Добавлены категории по умолчанию")
        conn.commit()

    # === МИГРАЦИЯ: связываем книги с категориями ===
    cursor.execute("PRAGMA table_info(books)")
    columns = [col[1] for col in cursor.fetchall()]

    if 'category_id' in columns:
        cursor.execute("SELECT COUNT(*) FROM books WHERE category_id IS NULL")
        null_count = cursor.fetchone()[0]

        if null_count > 0:
            cursor.execute("""
                UPDATE books SET category_id = (
                    SELECT id FROM categories 
                    WHERE categories.name = books.category 
                    LIMIT 1
                ) WHERE category_id IS NULL
            """)
            print(f"✅ Связано {null_count} книг с категориями")
            conn.commit()

    # === Книги по умолчанию ===
    cursor.execute("SELECT COUNT(*) FROM books")
    book_count = cursor.fetchone()[0]

    if book_count == 0:
        cursor.execute("SELECT id, name FROM categories")
        cat_rows = cursor.fetchall()
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
        cursor.executemany(
            "INSERT INTO books (title, price, category, emoji, description, images, category_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            default_books
        )
        print("✅ Добавлены книги по умолчанию")
        conn.commit()

    # === Настройки оплаты по умолчанию ===
    cursor.execute("SELECT COUNT(*) FROM payment_settings")
    if cursor.fetchone()[0] == 0:
        default_payment_settings = [
            ('payment_enabled', '1'),
            ('card_number', ''),
            ('sbp_phone', ''),
            ('sbp_bank', ''),
            ('recipient_name', ''),
            ('payment_instructions', 'После перевода укажите номер заказа в комментарии')
        ]
        cursor.executemany(
            "INSERT INTO payment_settings (setting_key, setting_value) VALUES (?, ?)",
            default_payment_settings
        )
        print("✅ Добавлены настройки оплаты по умолчанию")
        conn.commit()

    # === Настройки Stars по умолчанию ===
    cursor.execute("SELECT COUNT(*) FROM stars_settings")
    if cursor.fetchone()[0] == 0:
        default_stars_settings = [
            ('stars_enabled', '0'),  # По умолчанию Stars отключены
            ('rubles_per_star', '2')
        ]
        cursor.executemany(
            "INSERT INTO stars_settings (setting_key, setting_value) VALUES (?, ?)",
            default_stars_settings
        )
        print("✅ Добавлены настройки Stars по умолчанию")
        conn.commit()

    conn.close()
    print(f"✅ База данных инициализирована: {DB_NAME}")


# ============================================
# РАБОТА С БД
# ============================================

def get_books_sync(sort_by='default'):
    """Получить все активные книги с поддержкой сортировки"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Определяем порядок сортировки
    if sort_by == 'price_asc':
        order_clause = "b.price ASC, b.sort_order ASC, b.id ASC"
    elif sort_by == 'price_desc':
        order_clause = "b.price DESC, b.sort_order ASC, b.id ASC"
    elif sort_by == 'title':
        order_clause = "b.title ASC, b.sort_order ASC, b.id ASC"
    else:  # default
        order_clause = "b.sort_order ASC, b.id ASC"

    cursor.execute(f"""
        SELECT b.id, b.title, b.price, b.category, b.emoji, b.description, b.images, b.category_id,
               c.emoji as category_emoji
        FROM books b
        LEFT JOIN categories c ON b.category_id = c.id
        WHERE b.is_active = 1 
        ORDER BY {order_clause}
    """)
    books = cursor.fetchall()
    conn.close()
    return [dict(b) for b in books]


def get_categories_sync():
    """Получить все активные категории"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.id, c.name, c.emoji, c.sort_order,
               COUNT(b.id) as books_count
        FROM categories c
        LEFT JOIN books b ON b.category_id = c.id AND b.is_active = 1
        WHERE c.is_active = 1
        GROUP BY c.id
        ORDER BY c.sort_order ASC, c.name ASC
    """)
    cats = cursor.fetchall()
    conn.close()
    return [dict(c) for c in cats]


def create_order(user_id, user_name, cart, total, status='new'):
    """Создать заказ"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO orders (user_id, user_name, total, status) VALUES (?, ?, ?, ?)",
        (user_id, user_name, total, status)
    )
    order_id = cursor.lastrowid

    for item in cart:
        qty = item.get('quantity', 1)
        for _ in range(qty):
            cursor.execute(
                "INSERT INTO order_items (order_id, book_id, title, price) VALUES (?, ?, ?, ?)",
                (order_id, item['id'], item['title'], item['price'])
            )

    conn.commit()
    conn.close()
    return order_id


def get_all_unique_users():
    """Получить всех уникальных пользователей"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT user_id FROM orders")
    users = cursor.fetchall()
    conn.close()
    return [u['user_id'] for u in users]


# ============================================
# ДАШБОРД АДМИНА
# ============================================

# Статусы, которые считаем состоявшейся продажей (оплачено / принято в работу):
SALES_STATUSES = "'paid','confirmed','completed'"


def get_dashboard_stats():
    """Сводка для дашборда админа.

    Продажи за день/неделю/месяц (скользящие окна от текущего момента),
    топ-5 книг по количеству проданных экземпляров и средний чек.
    """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # created_at хранится в UTC (дефолт SQLite CURRENT_TIMESTAMP),
    # поэтому окна строим от datetime.utcnow().
    now = datetime.utcnow()
    windows = {
        'day': now - timedelta(days=1),
        'week': now - timedelta(days=7),
        'month': now - timedelta(days=30),
    }

    periods = {}
    for key, cutoff in windows.items():
        cursor.execute(
            f"SELECT COALESCE(SUM(total), 0) AS revenue, COUNT(*) AS orders "
            f"FROM orders "
            f"WHERE status IN ({SALES_STATUSES}) AND created_at >= ?",
            (cutoff.strftime('%Y-%m-%d %H:%M:%S'),),
        )
        row = cursor.fetchone()
        periods[key] = {'revenue': row['revenue'], 'orders': row['orders']}

    # Топ-5 книг по продажам. quantity не хранится — каждая позиция
    # в order_items это одна единица товара, считаем по строкам.
    cursor.execute(
        f"""SELECT oi.title, COUNT(*) AS qty, SUM(oi.price) AS revenue
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            WHERE o.status IN ({SALES_STATUSES})
            GROUP BY oi.book_id, oi.title
            ORDER BY qty DESC, revenue DESC
            LIMIT 5"""
    )
    top_books = [dict(r) for r in cursor.fetchall()]

    # Итоговая статистика для среднего чека
    cursor.execute(
        f"SELECT COUNT(*) AS orders, COALESCE(SUM(total), 0) AS revenue "
        f"FROM orders WHERE status IN ({SALES_STATUSES})"
    )
    totals = cursor.fetchone()
    conn.close()

    t_orders = totals['orders']
    t_revenue = totals['revenue']
    avg_check = round(t_revenue / t_orders) if t_orders else 0

    return {
        'periods': periods,
        'top_books': top_books,
        'avg_check': avg_check,
        'total_orders': t_orders,
        'total_revenue': t_revenue,
    }


# ============================================
# ПРОМОКОДЫ
# ============================================

def validate_promo_code_sync(code: str, order_total: int) -> dict:
    """Проверить промокод"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM promo_codes WHERE code = ? AND is_active = 1",
        (code.upper(),)
    )
    promo = cursor.fetchone()

    if not promo:
        conn.close()
        return {'valid': False, 'error': 'Промокод не найден'}

    promo = dict(promo)

    # Проверка срока действия
    if promo.get('expires_at'):
        try:
            expires = datetime.fromisoformat(promo['expires_at'])
            if datetime.now() > expires:
                conn.close()
                return {'valid': False, 'error': 'Промокод истёк'}
        except Exception:
            pass

    # Проверка минимальной суммы
    if promo['min_order'] > 0 and order_total < promo['min_order']:
        conn.close()
        return {'valid': False, 'error': f"Минимальная сумма заказа: {promo['min_order']} ₽"}

    # Проверка лимита использований
    if promo['max_uses'] > 0 and promo['current_uses'] >= promo['max_uses']:
        conn.close()
        return {'valid': False, 'error': 'Промокод больше не действует'}

    # Расчёт скидки
    discount = 0
    if promo['discount_percent'] > 0:
        discount = int(order_total * promo['discount_percent'] / 100)
    elif promo['discount_fixed'] > 0:
        discount = promo['discount_fixed']

    discount = min(discount, order_total)

    conn.close()

    return {
        'valid': True,
        'discount': discount,
        'discount_percent': promo['discount_percent'],
        'discount_fixed': promo['discount_fixed'],
        'final_total': order_total - discount,
        'promo_code': promo['code']
    }


def increment_promo_usage_sync(code: str):
    """Увеличить счётчик использований промокода"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE promo_codes SET current_uses = current_uses + 1 WHERE code = ?",
        (code.upper(),)
    )
    conn.commit()
    conn.close()


# ============================================
# ОТПРАВКА В TELEGRAM
# ============================================

def send_telegram_message(chat_id, text):
    """Отправить сообщение через Telegram Bot API"""
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN не задан!")
        return {'ok': False, 'description': 'No token'}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML'
        }, timeout=10)
        return response.json()
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")
        return {'ok': False, 'description': str(e)}


def send_stars_invoice(chat_id, order_id, title, description, stars_amount):
    """Отправить инвойс Stars через Bot API"""
    if not BOT_TOKEN:
        return {'ok': False, 'description': 'No token'}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendInvoice"
    data = {
        'chat_id': chat_id,
        'title': title,
        'description': description,
        'payload': str(order_id),
        'provider_token': '',
        'currency': 'XTR',
        'prices': json.dumps([{'label': f'Заказ #{order_id}', 'amount': stars_amount}])
    }
    try:
        response = requests.post(url, json=data, timeout=10)
        result = response.json()
        print(f"📤 Инвойс Stars отправлен: {result.get('ok')}")
        return result
    except Exception as e:
        print(f"❌ Ошибка отправки инвойса: {e}")
        return {'ok': False, 'description': str(e)}


def send_telegram_with_keyboard(chat_id, text, buttons):
    """Отправить сообщение с inline-кнопкой"""
    if not BOT_TOKEN:
        return {'ok': False, 'description': 'No token'}

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'reply_markup': {'inline_keyboard': buttons}
    }
    try:
        print(f"📤 Отправляем сообщение с кнопкой в чат {chat_id}")
        print(f"   Текст: {text[:100]}...")
        print(f"   Кнопки: {buttons}")
        response = requests.post(url, json=data, timeout=10)
        result = response.json()
        print(f"   Результат: {result.get('ok')} - {result.get('description', '')}")
        return result
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return {'ok': False, 'description': str(e)}
# ============================================
# МАРШРУТЫ
# ============================================

@app.route('/', methods=['GET'])
def index():
    """Главная страница"""
    # Файл в репозитории называется Index.html (с большой буквы). На
    # case-sensitive системах (Linux/macOS) send_from_directory с
    # 'index.html' не найдёт файл, поэтому выбираем имя по факту наличия.
    page = 'Index.html' if os.path.exists('Index.html') else 'index.html'
    return send_from_directory('.', page)


@app.route('/api/books', methods=['GET'])
def api_books():
    """API: список книг с поддержкой сортировки"""
    try:
        sort_by = request.args.get('sort', 'default')
        books = get_books_sync(sort_by)
        return jsonify({'books': books})
    except Exception as e:
        print(f"❌ Ошибка /api/books: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/categories', methods=['GET'])
def api_categories():
    """API: список категорий"""
    try:
        cats = get_categories_sync()
        return jsonify({'categories': cats})
    except Exception as e:
        print(f"❌ Ошибка /api/categories: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/dashboard', methods=['GET'])
def api_admin_dashboard():
    """API: статистика дашборда админа.

    Только для админов (user_id из ADMIN_IDS). Возвращает продажи за
    день/неделю/месяц, топ-5 книг и средний чек.
    """
    try:
        user_id = int(request.args.get('user_id', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'user_id is required'}), 400

    if user_id not in ADMIN_IDS:
        return jsonify({'error': 'Forbidden'}), 403

    try:
        return jsonify(get_dashboard_stats())
    except Exception as e:
        print(f"❌ Ошибка /api/admin/dashboard: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/validate-promo', methods=['POST'])
def api_validate_promo():
    """API: проверка промокода"""
    try:
        data = request.json
        code = data.get('code', '').strip()
        order_total = data.get('total', 0)

        if not code:
            return jsonify({'valid': False, 'error': 'Введите промокод'})

        result = validate_promo_code_sync(code, order_total)
        return jsonify(result)
    except Exception as e:
        return jsonify({'valid': False, 'error': str(e)}), 500


@app.route('/order', methods=['POST'])
def receive_order():
    """API: приём заказа от Mini App"""
    try:
        data = request.json
        user_id = data.get('user_id')
        user_name = data.get('user_name', 'Неизвестно')
        cart = data.get('cart', [])
        promo_code = data.get('promo_code', '')

        if not user_id or not cart:
            return jsonify({'error': 'Missing data'}), 400

        # Считаем сумму с учётом количества
        total = sum(item['price'] * item.get('quantity', 1) for item in cart)

        discount = 0
        final_total = total
        applied_promo = None

        # 1. Применяем промокод
        if promo_code:
            promo_result = validate_promo_code_sync(promo_code, total)
            if promo_result['valid']:
                discount = promo_result['discount']
                final_total = total - discount
                applied_promo = promo_result['promo_code']
                increment_promo_usage_sync(promo_code)

        # 2. Применяем бонусы пользователя
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM user_bonuses WHERE user_id = ? AND is_used = 0 ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        )
        bonus = cursor.fetchone()
        applied_bonus = None

        if bonus:
            bonus = dict(bonus)
            bonus_discount = 0
            if bonus['bonus_type'] == 'percent':
                bonus_discount = int(final_total * bonus['amount'] / 100)
            else:
                bonus_discount = bonus['amount']

            bonus_discount = min(bonus_discount, final_total)
            discount += bonus_discount
            final_total -= bonus_discount
            applied_bonus = f"{bonus['amount']}{'%' if bonus['bonus_type'] == 'percent' else '₽'}"

            cursor.execute("UPDATE user_bonuses SET is_used = 1 WHERE id = ?", (bonus['id'],))
            conn.commit()

        # 3. Получаем настройки оплаты
        cursor.execute("SELECT setting_key, setting_value FROM payment_settings")
        payment_settings = {row['setting_key']: row['setting_value'] for row in cursor.fetchall()}

        # 4. Получаем настройки Stars
        cursor.execute("SELECT setting_key, setting_value FROM stars_settings")
        stars_settings = {row['setting_key']: row['setting_value'] for row in cursor.fetchall()}

        conn.close()

        # Определяем метод оплаты
        stars_enabled = stars_settings.get('stars_enabled', '0') == '1'
        payment_enabled = payment_settings.get('payment_enabled', '1') == '1'
        rubles_per_star = int(stars_settings.get('rubles_per_star', '2'))

        # Конвертируем в Stars
        stars_amount = max(1, final_total // rubles_per_star) if rubles_per_star > 0 else 0

        # Определяем статус заказа
        if stars_enabled:
            status = 'awaiting_stars_payment'
            payment_method = 'stars'
        elif payment_enabled:
            status = 'awaiting_payment'
            payment_method = 'card'
        else:
            status = 'new'
            payment_method = 'none'

        # Создаём заказ
        order_id = create_order(user_id, user_name, cart, final_total, status)
        print(f"✅ Заказ #{order_id} от {user_name} | {final_total}₽ | Метод: {payment_method} | Статус: {status}")

        # Формируем ответ
        response_data = {
            'success': True,
            'order_id': order_id,
            'discount': discount,
            'final_total': final_total,
            'payment_method': payment_method,
            'applied_promo': applied_promo,
            'applied_bonus': applied_bonus
        }

        # Добавляем информацию о Stars
        if payment_method == 'stars':
            response_data['stars_amount'] = stars_amount

        # Добавляем реквизиты для оплаты картой
        if payment_method == 'card':
            response_data['payment_info'] = {
                'card': payment_settings.get('card_number', ''),
                'sbp_phone': payment_settings.get('sbp_phone', ''),
                'sbp_bank': payment_settings.get('sbp_bank', ''),
                'recipient': payment_settings.get('recipient_name', ''),
                'instructions': payment_settings.get('payment_instructions', '')
            }

        # Уведомление пользователю
        # === ОТПРАВКА СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЮ ===
        items_list = "\n".join([f"• {item['title']} ({item['price']} ₽ × {item.get('quantity', 1)})" for item in cart])

        if payment_method == 'stars':
            # === ОТПРАВКА ИНВОЙСА STARS ===
            invoice_title = f"Заказ #{order_id} — Семена Знаний"
            invoice_desc = f"📚 {len(cart)} книг\n\n{items_list}\n\n💰 {final_total} ₽"

            invoice_result = send_stars_invoice(
                user_id, order_id, invoice_title, invoice_desc, stars_amount
            )

            if invoice_result.get('ok'):
                # Инвойс отправлен — просто подтверждаем
                send_telegram_message(
                    user_id,
                    f"📦 <b>Заказ #{order_id} создан!</b>\n\n"
                    f"⬆️ <b>Нажмите «Оплатить» в инвойсе выше</b>\n\n"
                    f"💰 Сумма: <b>{stars_amount} Stars</b> (≈ {final_total} ₽)"
                )
            else:
                # Ошибка отправки инвойса
                send_telegram_message(
                    user_id,
                    f"📦 <b>Заказ #{order_id} создан!</b>\n\n"
                    f"⚠️ Не удалось создать платёж через Stars.\n"
                    f"Свяжитесь с поддержкой для оплаты.\n\n"
                    f"💰 Сумма: <b>{final_total} ₽</b>"
                )

        elif payment_method == 'card':
            # === ОТПРАВКА РЕКВИЗИТОВ ===
            payment_info = response_data.get('payment_info', {})

            payment_text = f"📦 <b>Заказ #{order_id} создан!</b>\n\n💰 К оплате: <b>{final_total} ₽</b>\n\n"
            payment_text += "<b>Реквизиты для оплаты:</b>\n\n"

            if payment_info.get('card'):
                payment_text += f"💳 Карта: <code>{payment_info['card']}</code>\n"
            if payment_info.get('sbp_phone'):
                payment_text += f"📱 СБП: <code>{payment_info['sbp_phone']}</code>\n"
            if payment_info.get('sbp_bank'):
                payment_text += f"🏦 Банк: {payment_info['sbp_bank']}\n"
            if payment_info.get('recipient'):
                payment_text += f"👤 Получатель: {payment_info['recipient']}\n"

            if payment_info.get('instructions'):
                payment_text += f"\n📝 {payment_info['instructions']}\n"

            payment_text += f"\n⚠️ Обязательно укажите номер заказа <b>#{order_id}</b> в комментарии!"

            # Кнопка "Я оплатил"
            buttons = [[{'text': '✅ Я оплатил', 'callback_data': f'user_paid_{order_id}'}]]
            send_telegram_with_keyboard(user_id, payment_text, buttons)

        else:
            # === БЕЗ ОПЛАТЫ ===
            send_telegram_message(
                user_id,
                f"🛒 <b>Заказ #{order_id} создан!</b>\n\n{items_list}\n\n"
                f"💰 Итого: <b>{final_total} ₽</b>\n\n"
                f"Мы свяжемся с вами для доставки 🌿"
            )

        # === УВЕДОМЛЕНИЕ АДМИНАМ ===
        # Карточку с кнопками «Принять / Отклонить» отправляет сам бот:
        # фоновый поллер в handlers/admin_orders.py подхватывает заказ из БД
        # (статусы из PENDING_STATUSES) и шлёт уведомление автоматически.
        # Здесь шлём только подтверждение пользователю.

        return jsonify(response_data)

    except Exception as e:
        print(f"❌ Ошибка в /order: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    """Проверка работоспособности"""
    return jsonify({'status': 'ok'})


# ============================================
# ЗАПУСК
# ============================================

if __name__ == '__main__':
    init_db()
    print(f"🚀 Сервер запущен на порту 8080")
    print(f"📱 Mini App: http://localhost:8080")
    print(f"📚 API книг: http://localhost:8080/api/books")
    print(f"📂 API категорий: http://localhost:8080/api/categories")
    print(f"👥 Админы: {ADMIN_IDS}")
    print(f"🤖 Bot token: {'✅' if BOT_TOKEN else '❌ НЕ ЗАДАН'}")
    app.run(host='0.0.0.0', port=8080, debug=True)