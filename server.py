from flask import Flask, Response, g, jsonify, make_response, request, send_from_directory
from functools import wraps
import requests
import os
import sqlite3
import json
from datetime import datetime, timedelta, timezone

from db.schema import DB_PATH, connect, initialize_database
from dotenv import load_dotenv

from content_defaults import TEMPLATES
from telegram_auth import TelegramInitDataError, validate_telegram_init_data

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Читаем ID админов
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]

app = Flask(__name__, static_folder='.')
initialize_database()


def _authentication_error(error: TelegramInitDataError):
    if error.kind == "unavailable":
        return jsonify({"error": "Telegram authentication is unavailable"}), 503
    if error.kind == "expired":
        return jsonify({"error": "Telegram init data has expired"}), 401
    if error.kind == "missing":
        return jsonify({"error": "Telegram init data is required"}), 401
    return jsonify({"error": "Invalid Telegram init data"}), 401


def require_telegram_user(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            g.telegram_user = validate_telegram_init_data(
                request.headers.get("X-Telegram-Init-Data", ""), BOT_TOKEN or ""
            )
        except TelegramInitDataError as error:
            return _authentication_error(error)
        return handler(*args, **kwargs)

    return wrapped


def require_telegram_admin(handler):
    @require_telegram_user
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if g.telegram_user.id not in ADMIN_IDS:
            return jsonify({"error": "Forbidden"}), 403
        response = make_response(handler(*args, **kwargs))
        response.headers["Cache-Control"] = "private, no-store"
        return response

    return wrapped


# ============================================
# РАБОТА С БД
# ============================================

def get_books_sync(sort_by='default'):
    """Получить все активные книги с поддержкой сортировки"""
    conn = connect()
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
        WHERE b.is_active = 1 AND COALESCE(b.is_archived, 0) = 0
        ORDER BY {order_clause}
    """)
    books = cursor.fetchall()
    conn.close()
    return [dict(b) for b in books]


def get_categories_sync():
    """Получить все активные категории"""
    conn = connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.id, c.name, c.emoji, c.sort_order,
               COUNT(b.id) as books_count
        FROM categories c
        LEFT JOIN books b ON b.category_id = c.id AND b.is_active = 1 AND COALESCE(b.is_archived, 0) = 0
        WHERE c.is_active = 1
        GROUP BY c.id
        ORDER BY c.sort_order ASC, c.name ASC
    """)
    cats = cursor.fetchall()
    conn.close()
    return [dict(c) for c in cats]


def create_order(user_id, user_name, cart, total, status='new', connection=None):
    """Создать заказ в переданной или новой транзакции."""
    owns_connection = connection is None
    conn = connection or connect()
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO orders (user_id, user_name, total, status) VALUES (?, ?, ?, ?)",
        (user_id, user_name, total, status)
    )
    order_id = cursor.lastrowid

    for item in cart:
        for _ in range(item['quantity']):
            cursor.execute(
                "INSERT INTO order_items (order_id, book_id, title, price) VALUES (?, ?, ?, ?)",
                (order_id, item['id'], item['title'], item['price'])
            )

    if owns_connection:
        conn.commit()
        conn.close()
    return order_id


def _positive_integer(value, field):
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if number <= 0 or str(number) != str(value).strip():
        raise ValueError(f"{field} must be a positive integer")
    return number


def resolve_checkout_cart(cart):
    if not isinstance(cart, list) or not cart:
        raise ValueError("cart must be a non-empty list")

    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        normalized = []
        for item in cart:
            if not isinstance(item, dict):
                raise ValueError("cart item must be an object")
            book_id = _positive_integer(item.get('id'), "book id")
            quantity = _positive_integer(item.get('quantity', 1), "quantity")
            book = connection.execute(
                """
                SELECT id, title, price FROM books
                WHERE id = ? AND is_active = 1 AND COALESCE(is_archived, 0) = 0
                """,
                (book_id,),
            ).fetchone()
            if not book:
                raise ValueError("book is unavailable")
            normalized.append({
                "id": book['id'],
                "title": book['title'],
                "price": book['price'],
                "quantity": quantity,
            })
        return normalized
    finally:
        connection.close()


_MAX_CART_ITEMS = 50
_MAX_CART_QUANTITY = 99


def _cart_response(cart: list[dict], revision: int, status: int = 200, **extra):
    response = jsonify({"cart": cart, "revision": revision, **extra})
    response.status_code = status
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _read_cart(connection: sqlite3.Connection, user_id: int) -> tuple[list[dict], int]:
    row = connection.execute(
        "SELECT cart_json, revision FROM mini_app_carts WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return [], 0
    try:
        cart = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Stored cart is invalid") from exc
    if not isinstance(cart, list):
        raise RuntimeError("Stored cart is invalid")
    return cart, int(row[1])


def _validate_cart_snapshot(cart: object, connection: sqlite3.Connection) -> list[dict]:
    if not isinstance(cart, list):
        raise ValueError("cart must be a list")
    if len(cart) > _MAX_CART_ITEMS:
        raise ValueError(f"cart must contain at most {_MAX_CART_ITEMS} items")

    normalized = []
    book_ids = set()
    for item in cart:
        if not isinstance(item, dict) or set(item) != {"id", "quantity"}:
            raise ValueError("cart items must contain only id and quantity")
        book_id = _positive_integer(item["id"], "book id")
        quantity = _positive_integer(item["quantity"], "quantity")
        if quantity > _MAX_CART_QUANTITY:
            raise ValueError(f"quantity must not exceed {_MAX_CART_QUANTITY}")
        if book_id in book_ids:
            raise ValueError("cart must not contain duplicate books")
        book_ids.add(book_id)
        normalized.append({"id": book_id, "quantity": quantity})

    if not normalized:
        return normalized

    placeholders = ",".join("?" for _ in normalized)
    active_ids = {
        row[0]
        for row in connection.execute(
            f"""
            SELECT id FROM books
            WHERE id IN ({placeholders})
              AND is_active = 1
              AND COALESCE(is_archived, 0) = 0
            """,
            [item["id"] for item in normalized],
        )
    }
    if len(active_ids) != len(normalized):
        raise ValueError("cart contains an unavailable book")
    return normalized


def _save_cart_snapshot(
    connection: sqlite3.Connection,
    user_id: int,
    cart: list[dict],
    expected_revision: int,
) -> tuple[list[dict], int, bool]:
    stored_cart, stored_revision = _read_cart(connection, user_id)
    if stored_revision != expected_revision:
        return stored_cart, stored_revision, False

    next_revision = stored_revision + 1
    payload = json.dumps(cart, separators=(",", ":"))
    if stored_revision:
        connection.execute(
            """
            UPDATE mini_app_carts
            SET cart_json = ?, revision = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND revision = ?
            """,
            (payload, next_revision, user_id, stored_revision),
        )
    else:
        connection.execute(
            """
            INSERT INTO mini_app_carts (user_id, cart_json, revision)
            VALUES (?, ?, ?)
            """,
            (user_id, payload, next_revision),
        )
    return cart, next_revision, True


def _clear_cart(connection: sqlite3.Connection, user_id: int) -> int:
    _, revision = _read_cart(connection, user_id)
    _, next_revision, _ = _save_cart_snapshot(connection, user_id, [], revision)
    return next_revision


def get_all_unique_users():
    """Получить всех уникальных пользователей"""
    conn = connect()
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

# Человекочитаемые названия статусов для CSV-экспорта (бухгалтерия).
STATUS_LABELS = {
    'new': 'новый',
    'awaiting_payment': 'ожидает оплаты',
    'awaiting_stars_payment': 'ожидает оплаты (Stars)',
    'payment_pending': 'платёж в обработке',
    'confirmed': 'подтверждён',
    'paid': 'оплачен',
    'completed': 'завершён',
    'cancelled': 'отменён',
}


def _csv_response(rows, headers, filename):
    """CSV-ответ с BOM и разделителем ';' — Excel (ru-RU) открывает сразу."""
    import csv
    from io import StringIO
    buf = StringIO()
    writer = csv.writer(buf, delimiter=';', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    response = Response('\ufeff' + buf.getvalue(), mimetype='text/csv; charset=utf-8')
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def _export_ts(utc_str):
    """created_at из БД (UTC) → местное время сервера 'YYYY-MM-DD HH:MM'.

    В CSV-выгрузку для бухгалтерии пишем локальное время, а не сырое UTC,
    иначе время в импортированном файле расходится с реальным на офсет
    часового пояса.
    """
    if not utc_str:
        return ''
    try:
        return (
            datetime.fromisoformat(str(utc_str))
            .replace(tzinfo=timezone.utc)
            .astimezone()
            .strftime('%Y-%m-%d %H:%M')
        )
    except Exception:
        return str(utc_str)


def get_dashboard_stats():
    """Сводка для дашборда админа.

    Продажи за день/неделю/месяц (скользящие окна от текущего момента),
    топ-5 книг по количеству проданных экземпляров и средний чек.
    """
    conn = connect()
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
    conn = connect()
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
    conn = connect()
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
@require_telegram_admin
def api_admin_dashboard():
    """API: статистика дашборда админа."""
    try:
        return jsonify(get_dashboard_stats())
    except Exception as e:
        print(f"❌ Ошибка /api/admin/dashboard: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/export/orders', methods=['GET'])
@require_telegram_admin
def api_export_orders():
    """API: CSV-выгрузка всех заказов для бухгалтерии.

    Только для админов. Реестр заказов: id, дата, статус, покупатель,
    состав заказа и сумма — закрытая сделка целиком.
    """
    try:
        conn = connect()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id, created_at, status, user_id, user_name, total "
            "FROM orders ORDER BY id ASC"
        )
        orders = [dict(r) for r in cursor.fetchall()]

        if orders:
            placeholders = ','.join(['?'] * len(orders))
            cursor.execute(
                f"SELECT order_id, title, COUNT(*) AS qty "
                f"FROM order_items WHERE order_id IN ({placeholders}) "
                f"GROUP BY order_id, title ORDER BY order_id ASC, title ASC",
                [o['id'] for o in orders],
            )
        else:
            cursor.execute(
                "SELECT order_id, title, COUNT(*) AS qty "
                "FROM order_items WHERE 0 GROUP BY order_id, title"
            )
        items_map = {}
        for r in cursor.fetchall():
            items_map.setdefault(r['order_id'], []).append(f"{r['title']} ×{r['qty']}")
        conn.close()

        headers = ['id', 'дата', 'статус', 'user_id', 'имя', 'товары', 'сумма']
        rows = [
            [
                o['id'],
                _export_ts(o['created_at']),
                STATUS_LABELS.get(o['status'], o['status']),
                o['user_id'],
                o['user_name'] or '',
                ', '.join(items_map.get(o['id'], [])),
                o['total'],
            ]
            for o in orders
        ]
        return _csv_response(rows, headers, 'orders.csv')
    except Exception as e:
        print(f"❌ Ошибка /api/admin/export/orders: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/export/books', methods=['GET'])
@require_telegram_admin
def api_export_books():
    """API: CSV-выгрузка книг с продажами для бухгалтерии.

    Только для админов. Каталог книг дополнен количеством проданных
    экземпляров и выручкой по оплаченным заказам.
    """
    try:
        conn = connect()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT b.id, b.title, b.price,
                       COALESCE(c.name, b.category) AS category,
                       CASE WHEN b.is_active = 1 THEN 'да' ELSE 'нет' END AS is_active,
                       COALESCE(s.qty, 0) AS qty,
                       COALESCE(s.revenue, 0) AS revenue
                FROM books b
                LEFT JOIN categories c ON c.id = b.category_id
                LEFT JOIN (
                    SELECT oi.book_id, COUNT(*) AS qty, SUM(oi.price) AS revenue
                    FROM order_items oi
                    JOIN orders o ON o.id = oi.order_id
                    WHERE o.status IN ({SALES_STATUSES})
                    GROUP BY oi.book_id
                ) s ON s.book_id = b.id
                ORDER BY revenue DESC, b.title ASC"""
        )
        rows = [
            [r['id'], r['title'], r['price'], r['category'], r['is_active'], r['qty'], r['revenue']]
            for r in cursor.fetchall()
        ]
        conn.close()

        headers = ['id', 'название', 'цена', 'категория', 'активна', 'продано_шт', 'выручка']
        return _csv_response(rows, headers, 'books.csv')
    except Exception as e:
        print(f"❌ Ошибка /api/admin/export/books: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/cart', methods=['GET'])
@require_telegram_user
def api_cart():
    connection = connect()
    try:
        cart, revision = _read_cart(connection, g.telegram_user.id)
        return _cart_response(cart, revision)
    except RuntimeError:
        return jsonify({'error': 'Cart is unavailable'}), 503
    finally:
        connection.close()


@app.route('/api/cart', methods=['PUT'])
@require_telegram_user
def save_cart():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'cart', 'revision'}:
        return jsonify({'error': 'Expected cart and revision'}), 400
    revision = data.get('revision')
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return jsonify({'error': 'revision must be a non-negative integer'}), 400

    connection = connect()
    try:
        connection.execute('BEGIN IMMEDIATE')
        cart = _validate_cart_snapshot(data['cart'], connection)
        saved_cart, saved_revision, saved = _save_cart_snapshot(
            connection,
            g.telegram_user.id,
            cart,
            revision,
        )
        if not saved:
            connection.rollback()
            return _cart_response(
                saved_cart,
                saved_revision,
                409,
                error='Cart has changed in another session',
            )
        connection.commit()
        return _cart_response(saved_cart, saved_revision)
    except ValueError as exc:
        connection.rollback()
        return jsonify({'error': str(exc)}), 400
    except sqlite3.Error:
        connection.rollback()
        return jsonify({'error': 'Cart is unavailable'}), 503
    finally:
        connection.close()

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
@require_telegram_user
def receive_order():
    """API: приём заказа от Mini App"""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'Invalid JSON body'}), 400
        try:
            cart = resolve_checkout_cart(data.get('cart'))
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        user_id = g.telegram_user.id
        user_name = g.telegram_user.name
        promo_code = str(data.get('promo_code') or '').strip()
        cart_revision = data.get('cart_revision')
        if (
            isinstance(cart_revision, bool)
            or not isinstance(cart_revision, int)
            or cart_revision < 0
        ):
            return jsonify({'error': 'cart_revision must be a non-negative integer'}), 400

        total = sum(item['price'] * item['quantity'] for item in cart)

        discount = 0
        final_total = total
        applied_promo = None
        promo_code_to_consume = None

        # 1. Применяем промокод
        if promo_code:
            promo_result = validate_promo_code_sync(promo_code, total)
            if promo_result['valid']:
                discount = promo_result['discount']
                final_total = total - discount
                applied_promo = promo_result['promo_code']
                promo_code_to_consume = promo_result['promo_code']

        # 2. Применяем бонусы пользователя
        conn = connect()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        conn.execute('BEGIN IMMEDIATE')
        stored_cart, stored_revision = _read_cart(conn, user_id)
        if stored_revision != cart_revision:
            conn.rollback()
            conn.close()
            return _cart_response(
                stored_cart,
                stored_revision,
                409,
                error='Cart has changed in another session',
            )

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

        # 3. Получаем настройки оплаты
        cursor.execute("SELECT setting_key, setting_value FROM payment_settings")
        payment_settings = {row['setting_key']: row['setting_value'] for row in cursor.fetchall()}

        # 4. Получаем настройки Stars
        cursor.execute("SELECT setting_key, setting_value FROM stars_settings")
        stars_settings = {row['setting_key']: row['setting_value'] for row in cursor.fetchall()}


        # Определяем метод оплаты
        stars_enabled = stars_settings.get('stars_enabled', '0') == '1'
        payment_enabled = payment_settings.get('payment_enabled', '1') == '1'
        rubles_per_star = int(stars_settings.get('rubles_per_star', '2'))

        # Конвертируем в Stars
        stars_amount = (final_total + rubles_per_star - 1) // rubles_per_star if rubles_per_star > 0 else 0

        if stars_enabled and rubles_per_star <= 0:
            conn.rollback()
            conn.close()
            return jsonify({'error': 'Invalid Stars rate'}), 503

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

        if promo_code_to_consume:
            updated = cursor.execute(
                """
                UPDATE promo_codes
                SET current_uses = current_uses + 1
                WHERE code = ? AND (max_uses = 0 OR current_uses < max_uses)
                """,
                (promo_code_to_consume,),
            )
            if updated.rowcount != 1:
                conn.rollback()
                conn.close()
                return jsonify({'error': 'Промокод больше не действует'}), 409

        order_id = create_order(user_id, user_name, cart, final_total, status, connection=conn)
        cart_revision = _clear_cart(conn, user_id)
        conn.commit()
        conn.close()
        print(f"✅ Заказ #{order_id} от {user_name} | {final_total}₽ | Метод: {payment_method} | Статус: {status}")

        # Формируем ответ
        response_data = {
            'success': True,
            'order_id': order_id,
            'discount': discount,
            'final_total': final_total,
            'payment_method': payment_method,
            'payment_required': payment_method == 'card',
            'applied_promo': applied_promo,
            'applied_bonus': applied_bonus,
            'cart_revision': cart_revision
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
    print(f"🚀 Сервер запущен на порту 8080")
    print(f"📱 Mini App: http://localhost:8080")
    print(f"📚 API книг: http://localhost:8080/api/books")
    print(f"📂 API категорий: http://localhost:8080/api/categories")
    print(f"👥 Админы: {ADMIN_IDS}")
    print(f"🤖 Bot token: {'✅' if BOT_TOKEN else '❌ НЕ ЗАДАН'}")
    app.run(host='0.0.0.0', port=8080, debug=True)