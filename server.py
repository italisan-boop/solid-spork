from flask import Flask, Response, g, jsonify, make_response, redirect, request, send_from_directory
from functools import wraps
import requests
import os
import sqlite3
import json
import ipaddress
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone

from config import settings
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


def create_order(
    user_id,
    user_name,
    cart,
    total,
    status='new',
    connection=None,
    *,
    payment_method='manual',
    checkout_key=None,
):
    """Создать заказ в переданной или новой транзакции."""
    owns_connection = connection is None
    conn = connection or connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO orders (user_id, user_name, total, status, payment_method, checkout_key)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, user_name, total, status, payment_method, checkout_key),
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


def _checkout_cart_is_available(connection: sqlite3.Connection, cart: list[dict]) -> bool:
    book_ids = [item["id"] for item in cart]
    placeholders = ",".join("?" for _ in book_ids)
    available = connection.execute(
        f"""
        SELECT COUNT(*) FROM books
        WHERE id IN ({placeholders})
          AND is_active = 1
          AND COALESCE(is_archived, 0) = 0
        """,
        book_ids,
    ).fetchone()[0]
    return available == len(book_ids)


PAYMENT_METHOD_MANUAL = "manual"
PAYMENT_METHOD_STARS = "stars"
PAYMENT_METHOD_YOOKASSA = "yookassa"
PAYMENT_METHOD_NONE = "none"
YOO_KASSA_SOURCE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "185.71.76.0/27",
        "185.71.77.0/27",
        "77.75.153.0/25",
        "77.75.156.11/32",
        "77.75.156.35/32",
        "77.75.154.128/25",
        "2a02:5180::/32",
    )
)


def _money_rub(amount: int) -> str:
    return f"{Decimal(amount):.2f}"


def _normalized_rub_amount(value) -> str | None:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return format(amount, ".2f")


def yookassa_is_configured() -> bool:
    return bool(
        settings.YOOKASSA_SHOP_ID
        and settings.YOOKASSA_SECRET_KEY
        and settings.YOOKASSA_RETURN_URL.startswith("https://")
    )


def _payment_options(connection: sqlite3.Connection) -> list[dict]:
    settings_rows = connection.execute(
        "SELECT setting_key, setting_value FROM payment_settings"
    ).fetchall()
    payment_settings = {row[0]: row[1] for row in settings_rows}
    stars_rows = connection.execute(
        "SELECT setting_key, setting_value FROM stars_settings"
    ).fetchall()
    stars_settings = {row[0]: row[1] for row in stars_rows}
    options = []
    if payment_settings.get("payment_enabled") == "1":
        options.append({"id": PAYMENT_METHOD_MANUAL, "title": "Карта или СБП"})
    try:
        stars_rate = int(stars_settings.get("rubles_per_star", "0"))
    except (TypeError, ValueError):
        stars_rate = 0
    if stars_settings.get("stars_enabled") == "1" and stars_rate > 0:
        options.append({"id": PAYMENT_METHOD_STARS, "title": "Telegram Stars"})
    if payment_settings.get("yookassa_enabled") == "1" and yookassa_is_configured():
        options.append({"id": PAYMENT_METHOD_YOOKASSA, "title": "ЮKassa"})
    if not options:
        options.append({"id": PAYMENT_METHOD_NONE, "title": "Без онлайн-оплаты"})
    return options


def _yookassa_create_payment(amount: str, order_id: int, attempt_id: int, idempotence_key: str):
    from yookassa import Configuration, Payment

    Configuration.configure(settings.YOOKASSA_SHOP_ID, settings.YOOKASSA_SECRET_KEY)
    return Payment.create(
        {
            "amount": {"value": amount, "currency": "RUB"},
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": settings.YOOKASSA_RETURN_URL,
            },
            "description": f"Заказ #{order_id}",
            "metadata": {"order_id": str(order_id), "attempt_id": str(attempt_id)},
        },
        idempotence_key,
    )


def _yookassa_find_payment(provider_payment_id: str):
    from yookassa import Configuration, Payment

    Configuration.configure(settings.YOOKASSA_SHOP_ID, settings.YOOKASSA_SECRET_KEY)
    return Payment.find_one(provider_payment_id)


def _payment_value(payment, path: str, default=None):
    value = payment
    for key in path.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        else:
            value = getattr(value, key, None)
        if value is None:
            return default
    return value


def _yookassa_attempt(connection: sqlite3.Connection, order_id: int):
    return connection.execute(
        "SELECT * FROM yookassa_payments WHERE order_id = ? ORDER BY id DESC LIMIT 1",
        (order_id,),
    ).fetchone()


def _start_yookassa_attempt(order_id: int) -> tuple[dict | None, str | None]:
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        attempt = _yookassa_attempt(connection, order_id)
        if not attempt:
            return None, "Платёж не найден"
        attempt = dict(attempt)
        if attempt["status"] == "canceled":
            return attempt, "Платёж отменён. Создайте новый платёж."
        if attempt["provider_payment_id"] and attempt["confirmation_url"]:
            return attempt, None
        if not yookassa_is_configured():
            return attempt, "ЮKassa временно недоступна"
        try:
            payment = _yookassa_create_payment(
                attempt["amount"], order_id, attempt["id"], attempt["idempotence_key"]
            )
        except Exception:
            connection.execute(
                "UPDATE yookassa_payments SET status = 'creation_pending', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (attempt["id"],),
            )
            connection.commit()
            attempt["status"] = "creation_pending"
            return attempt, "Не удалось создать платёж ЮKassa"
        provider_payment_id = _payment_value(payment, "id")
        confirmation_url = _payment_value(payment, "confirmation.confirmation_url", "")
        status = _payment_value(payment, "status", "pending")
        if not provider_payment_id or not confirmation_url:
            connection.execute(
                "UPDATE yookassa_payments SET status = 'creation_pending', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (attempt["id"],),
            )
            connection.commit()
            attempt["status"] = "creation_pending"
            return attempt, "ЮKassa вернула неполный ответ"
        connection.execute(
            """
            UPDATE yookassa_payments
            SET provider_payment_id = ?, confirmation_url = ?, status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (provider_payment_id, confirmation_url, status, attempt["id"]),
        )
        connection.commit()
        attempt.update(
            provider_payment_id=provider_payment_id,
            confirmation_url=confirmation_url,
            status=status,
        )
        return attempt, None
    finally:
        connection.close()


def _safe_payment_response(order: sqlite3.Row | dict, attempt=None) -> dict:
    order = dict(order)
    result = {
        "order_id": order["id"],
        "payment_method": order["payment_method"],
        "status": order["status"],
        "final_total": order["total"],
    }
    if order["payment_method"] == PAYMENT_METHOD_YOOKASSA and attempt:
        provider_status = attempt["status"]
        result["confirmation_url"] = (
            attempt["confirmation_url"] or None
            if provider_status not in {"canceled", "succeeded"}
            else None
        )
        result["provider_status"] = provider_status
    return result


def _existing_checkout_response(
    user_id: int,
    order: sqlite3.Row | dict,
    attempt: sqlite3.Row | dict | None,
    cart_revision: int,
):
    order = dict(order)
    payment_error = None
    if order["payment_method"] == PAYMENT_METHOD_YOOKASSA:
        if attempt is None:
            payment_error = "Платёж не найден"
        elif not attempt["confirmation_url"] or attempt["status"] == "canceled":
            attempt, payment_error = _start_yookassa_attempt(order["id"])
    response = {
        "success": True,
        **_safe_payment_response(order, attempt),
        "reused": True,
        "cart_revision": cart_revision,
    }
    if payment_error:
        response["payment_error"] = payment_error
    return jsonify(response)


def _checkout_key(value) -> str:
    if not isinstance(value, str):
        raise ValueError("checkout_key must be a UUID")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("checkout_key must be a UUID") from exc


def _is_yookassa_source(address: str | None) -> bool:
    try:
        peer = ipaddress.ip_address(address or "")
    except ValueError:
        return False
    return any(peer in network for network in YOO_KASSA_SOURCE_NETWORKS)

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
    'awaiting_yookassa_payment': 'ожидает оплаты (ЮKassa)',
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


@app.route('/api/checkout/options', methods=['GET'])
@require_telegram_user
def api_checkout_options():
    connection = connect()
    try:
        return make_response(jsonify({"methods": _payment_options(connection)}), 200, {"Cache-Control": "private, no-store"})
    finally:
        connection.close()


@app.route('/api/orders/<int:order_id>/payment', methods=['GET'])
@require_telegram_user
def api_order_payment(order_id: int):
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order or order["user_id"] != g.telegram_user.id:
            return jsonify({"error": "Заказ не найден"}), 404
        attempt = _yookassa_attempt(connection, order_id) if order["payment_method"] == PAYMENT_METHOD_YOOKASSA else None
        response = jsonify(_safe_payment_response(order, attempt))
        response.headers["Cache-Control"] = "private, no-store"
        return response
    finally:
        connection.close()


@app.route('/api/orders/<int:order_id>/yookassa/retry', methods=['POST'])
@require_telegram_user
def retry_yookassa_payment(order_id: int):
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if (
            not order
            or order["user_id"] != g.telegram_user.id
            or order["payment_method"] != PAYMENT_METHOD_YOOKASSA
            or order["status"] != "awaiting_yookassa_payment"
        ):
            connection.rollback()
            return jsonify({"error": "Платёж недоступен"}), 404
        attempt = _yookassa_attempt(connection, order_id)
        if not attempt:
            connection.rollback()
            return jsonify({"error": "Платёж не найден"}), 404
        if attempt["status"] == "canceled":
            connection.execute(
                """
                INSERT INTO yookassa_payments (order_id, idempotence_key, amount, currency)
                VALUES (?, ?, ?, 'RUB')
                """,
                (order_id, str(uuid.uuid4()), _money_rub(order["total"])),
            )
        connection.commit()
    finally:
        connection.close()
    attempt, error = _start_yookassa_attempt(order_id)
    if not attempt:
        return jsonify({"error": error}), 503
    response = jsonify(_safe_payment_response({"id": order_id, "payment_method": PAYMENT_METHOD_YOOKASSA, "status": "awaiting_yookassa_payment", "total": order["total"]}, attempt))
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route('/payments/yookassa/return', methods=['GET'])
def yookassa_return():
    return redirect(settings.WEBAPP_URL)


@app.route('/webhooks/yookassa', methods=['POST'])
def yookassa_webhook():
    if request.content_length is not None and request.content_length > 65536:
        return jsonify({"error": "Payload too large"}), 413
    if not _is_yookassa_source(request.remote_addr):
        return jsonify({"error": "Forbidden"}), 403
    payload = request.get_json(silent=True)
    provider_payment_id = (
        payload.get("object", {}).get("id") if isinstance(payload, dict) and isinstance(payload.get("object"), dict) else None
    )
    if not isinstance(provider_payment_id, str) or not provider_payment_id:
        return jsonify({"error": "Invalid payload"}), 400
    if not yookassa_is_configured():
        return jsonify({"error": "Verification unavailable"}), 503
    try:
        payment = _yookassa_find_payment(provider_payment_id)
    except Exception:
        return jsonify({"error": "Verification unavailable"}), 503

    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        attempt = connection.execute(
            "SELECT * FROM yookassa_payments WHERE provider_payment_id = ?",
            (provider_payment_id,),
        ).fetchone()
        if not attempt:
            connection.rollback()
            return "", 200
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (attempt["order_id"],)).fetchone()
        active_attempt = _yookassa_attempt(connection, attempt["order_id"])
        provider_id = _payment_value(payment, "id")
        provider_status = _payment_value(payment, "status", "unknown")
        paid = _payment_value(payment, "paid", False)
        amount = _normalized_rub_amount(_payment_value(payment, "amount.value"))
        currency = _payment_value(payment, "amount.currency")
        metadata = _payment_value(payment, "metadata", {}) or {}
        valid_identity = (
            order is not None
            and active_attempt is not None
            and active_attempt["id"] == attempt["id"]
            and provider_id == provider_payment_id
            and amount == attempt["amount"]
            and currency == attempt["currency"]
            and isinstance(metadata, dict)
            and str(metadata.get("order_id", "")) == str(order["id"])
            and str(metadata.get("attempt_id", "")) == str(attempt["id"])
        )
        connection.execute(
            "UPDATE yookassa_payments SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (provider_status, attempt["id"]),
        )
        if (
            provider_status == "succeeded"
            and paid is True
            and valid_identity
            and order["status"] == "awaiting_yookassa_payment"
        ):
            connection.execute(
                "UPDATE orders SET status = 'paid', new_order_notified = 0 WHERE id = ? AND status = 'awaiting_yookassa_payment'",
                (order["id"],),
            )
        connection.commit()
        return "", 200
    except sqlite3.Error:
        connection.rollback()
        return jsonify({"error": "Verification unavailable"}), 503
    finally:
        connection.close()


@app.route('/api/validate-promo', methods=['POST'])
@require_telegram_user
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
        user_id = g.telegram_user.id
        user_name = g.telegram_user.name
        try:
            checkout_key = _checkout_key(data.get("checkout_key"))
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        existing_connection = connect()
        existing_connection.row_factory = sqlite3.Row
        try:
            existing_order = existing_connection.execute(
                "SELECT * FROM orders WHERE user_id = ? AND checkout_key = ?",
                (user_id, checkout_key),
            ).fetchone()
            existing_attempt = _yookassa_attempt(existing_connection, existing_order["id"]) if existing_order else None
            _, existing_cart_revision = _read_cart(existing_connection, user_id)
        finally:
            existing_connection.close()
        if existing_order:
            return _existing_checkout_response(
                user_id,
                existing_order,
                existing_attempt,
                existing_cart_revision,
            )
        try:
            cart = resolve_checkout_cart(data.get('cart'))
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        promo_code = str(data.get('promo_code') or '').strip()
        payment_method = data.get("payment_method")
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
        raced_order = cursor.execute(
            "SELECT * FROM orders WHERE user_id = ? AND checkout_key = ?",
            (user_id, checkout_key),
        ).fetchone()
        if raced_order:
            raced_attempt = _yookassa_attempt(conn, raced_order["id"])
            _, raced_cart_revision = _read_cart(conn, user_id)
            conn.rollback()
            conn.close()
            return _existing_checkout_response(
                user_id,
                raced_order,
                raced_attempt,
                raced_cart_revision,
            )
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
        if not _checkout_cart_is_available(conn, cart):
            conn.rollback()
            conn.close()
            return jsonify({'error': 'book is unavailable'}), 400

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


        # Покупатель выбирает способ, но сервер разрешает только включённые методы.
        options = {option["id"] for option in _payment_options(conn)}
        if final_total == 0:
            payment_method = PAYMENT_METHOD_NONE
        elif payment_method not in options:
            conn.rollback()
            conn.close()
            return jsonify({'error': 'Выбранный способ оплаты недоступен'}), 400
        try:
            rubles_per_star = int(stars_settings.get('rubles_per_star', '0'))
        except (TypeError, ValueError):
            rubles_per_star = 0
        if payment_method == PAYMENT_METHOD_STARS and rubles_per_star <= 0:
            conn.rollback()
            conn.close()
            return jsonify({'error': 'Invalid Stars rate'}), 503
        if payment_method == PAYMENT_METHOD_STARS:
            status = 'awaiting_stars_payment'
            stars_amount = (final_total + rubles_per_star - 1) // rubles_per_star
        elif payment_method == PAYMENT_METHOD_MANUAL:
            status = 'awaiting_payment'
            stars_amount = None
        elif payment_method == PAYMENT_METHOD_YOOKASSA:
            status = 'awaiting_yookassa_payment'
            stars_amount = None
        else:
            status = 'new'
            stars_amount = None

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

        order_id = create_order(
            user_id,
            user_name,
            cart,
            final_total,
            status,
            connection=conn,
            payment_method=payment_method,
            checkout_key=checkout_key,
        )
        if payment_method == PAYMENT_METHOD_YOOKASSA:
            cursor.execute(
                """
                INSERT INTO yookassa_payments (order_id, idempotence_key, amount, currency)
                VALUES (?, ?, ?, 'RUB')
                """,
                (order_id, str(uuid.uuid4()), _money_rub(final_total)),
            )
        cart_revision = _clear_cart(conn, user_id)
        conn.commit()
        conn.close()
        print(f"Order #{order_id}: {final_total} RUB, method={payment_method}, status={status}")

        # Формируем ответ
        response_data = {
            'success': True,
            'order_id': order_id,
            'discount': discount,
            'final_total': final_total,
            'payment_method': payment_method,
            'status': status,
            'payment_required': payment_method == PAYMENT_METHOD_MANUAL,
            'applied_promo': applied_promo,
            'applied_bonus': applied_bonus,
            'cart_revision': cart_revision
        }

        # Добавляем информацию о Stars
        if payment_method == PAYMENT_METHOD_STARS:
            response_data['stars_amount'] = stars_amount

        # Добавляем реквизиты для ручной оплаты картой/СБП
        if payment_method == PAYMENT_METHOD_MANUAL:
            response_data['payment_info'] = {
                'card': payment_settings.get('card_number', ''),
                'sbp_phone': payment_settings.get('sbp_phone', ''),
                'sbp_bank': payment_settings.get('sbp_bank', ''),
                'recipient': payment_settings.get('recipient_name', ''),
                'instructions': payment_settings.get('payment_instructions', '')
            }

        if payment_method == PAYMENT_METHOD_YOOKASSA:
            attempt, payment_error = _start_yookassa_attempt(order_id)
            if attempt:
                response_data["confirmation_url"] = (
                    attempt["confirmation_url"] or None
                    if attempt["status"] not in {"canceled", "succeeded"}
                    else None
                )
                response_data["provider_status"] = attempt["status"]
            if payment_error:
                response_data["payment_error"] = payment_error

        # Уведомление пользователю
        # === ОТПРАВКА СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЮ ===
        items_list = "\n".join([f"• {item['title']} ({item['price']} ₽ × {item.get('quantity', 1)})" for item in cart])

        if payment_method == PAYMENT_METHOD_STARS:
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

        elif payment_method == PAYMENT_METHOD_MANUAL:
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

        elif payment_method == PAYMENT_METHOD_NONE:
            # Заказ без онлайн-оплаты.
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

    except Exception:
        print("Order checkout failed")
        return jsonify({'error': 'Не удалось оформить заказ'}), 500


@app.route('/health', methods=['GET'])
def health():
    """Проверка работоспособности"""
    return jsonify({'status': 'ok'})


# ============================================
# ЗАПУСК
# ============================================

def run_server():
    initialize_database()
    app.run(
        host=settings.HOST,
        port=settings.PORT,
        debug=settings.FLASK_DEBUG,
        use_reloader=settings.FLASK_DEBUG,
    )


if __name__ == '__main__':
    print(f"Mini App server: http://{settings.HOST}:{settings.PORT}")
    run_server()
