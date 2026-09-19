from flask import Flask, Response, g, jsonify, make_response, redirect, request, send_file, send_from_directory
from werkzeug.serving import WSGIRequestHandler
from functools import wraps
from html import escape
import logging
import requests
import os
import re
import sqlite3
import json
import ipaddress
import uuid
import hashlib
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from authz import actor_role_sync, capabilities_for_role, has_permission_sync, is_owner_sync
from config import settings
from db.deliveries import (
    METHOD_RUSSIAN_POST_PICKUP,
    METHOD_SDEK_PICKUP,
    METHOD_SELF_PICKUP,
    SHIPMENT_AWAITING_PAYMENT,
    safe_delivery_summary,
)
from db.inventory import (
    InventoryUnavailableError,
    available_quantity,
    commit_order_inventory,
    adjust_stock_sync,
    inventory_summary_sync,
    list_inventory_movements_sync,
    list_inventory_sync,
    recent_movements_sync,
    reserve_order_inventory,
)
from db.operational_events import OperationalEvent, get_latest_event_sync, record_event_sync
from db.staff import (
    delete_inactive_staff_member_sync,
    list_staff_sync,
    set_staff_member_sync,
)
from db.audit import append_audit_event, list_audit_events_sync
from db.book_imports import BookImportError, commit_book_import_sync, preview_book_import_sync
from db.fulfillment import (
    FulfillmentError,
    claim_fulfillment_sync,
    get_fulfillment_sync,
    list_fulfillment_queue_sync,
    pack_fulfillment_sync,
    packing_print_payload_sync,
    set_picked_quantity_sync,
)
from db.schema import DB_PATH, connect, initialize_database
from utils import log_event, setup_logger
from utils.delivery_crypto import (
    DeliveryCryptoError,
    delivery_encryption_is_available,
    encrypt_destination,
    decrypt_destination,
)

from controlplane.plan_policy import LIMIT_CAMPAIGNS
from runtime.context import maybe_current_tenant_context
from runtime.features import QuotaExceededError
from runtime.quota import require_count_quota
from content_defaults import TEMPLATES
from storage.book_media import media_variants_for_book_sync, resolve_media_variant_sync
from telegram_auth import TelegramInitDataError, validate_telegram_init_data

BOT_TOKEN = settings.BOT_TOKEN
_BOT_USERNAME_CACHE: tuple[str, str, float] | None = None
_BOT_USERNAME_CACHE_TTL_SECONDS = 600

ADMIN_IDS = settings.ADMIN_IDS

app = Flask(__name__, static_folder='.')
logger = setup_logger(__name__)


def _record_operational_event(
    severity: str,
    event: str,
    outcome: str,
    *,
    reason: str = "",
    order_id: int | None = None,
    attempt_id: int | None = None,
    error: BaseException | None = None,
) -> None:
    """Persist and emit allowlisted diagnostics without request or payment data."""
    error_type = type(error).__name__ if error else ""
    log_event(
        logger,
        getattr(logging, severity.upper()),
        component="mini_app",
        event=event,
        outcome=outcome,
        reason=reason,
        order_id=order_id,
        attempt_id=attempt_id,
        error_type=error_type,
    )
    try:
        record_event_sync(
            OperationalEvent(
                severity=severity,
                component="mini_app",
                event=event,
                outcome=outcome,
                reason=reason,
                order_id=order_id,
                attempt_id=attempt_id,
                error_type=error_type,
            )
        )
    except Exception:
        logger.error("Could not persist operational event")


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


def require_telegram_permission(permission: str):
    def decorator(handler):
        @require_telegram_user
        @wraps(handler)
        def wrapped(*args, **kwargs):
            if not has_permission_sync(
                g.telegram_user.id, permission, legacy_admin_ids=ADMIN_IDS
            ):
                return jsonify({"error": "Forbidden"}), 403
            g.staff_role = actor_role_sync(
                g.telegram_user.id, legacy_admin_ids=ADMIN_IDS
            )
            response = make_response(handler(*args, **kwargs))
            response.headers["Cache-Control"] = "private, no-store"
            return response

        return wrapped

    return decorator


def require_telegram_admin(handler):
    return require_telegram_permission("admin.access")(handler)


def _telegram_request_options(timeout: int) -> dict[str, object]:
    options: dict[str, object] = {"timeout": timeout}
    if settings.BOT_PROXY_URL:
        options["proxies"] = {
            "http": settings.BOT_PROXY_URL,
            "https": settings.BOT_PROXY_URL,
        }
    return options


def _current_bot_username() -> str | None:
    global _BOT_USERNAME_CACHE
    token = BOT_TOKEN or ""
    if not token:
        return None
    token_key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    if _BOT_USERNAME_CACHE and _BOT_USERNAME_CACHE[0] == token_key and _BOT_USERNAME_CACHE[2] > now:
        return _BOT_USERNAME_CACHE[1]
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getMe", **_telegram_request_options(5)
        )
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    username = payload.get("result", {}).get("username") if isinstance(payload, dict) else None
    if not payload.get("ok") or not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None
    _BOT_USERNAME_CACHE = (token_key, username, now + _BOT_USERNAME_CACHE_TTL_SECONDS)
    return username


@app.route('/api/app/bot-identity', methods=['GET'])
@require_telegram_user
def api_bot_identity():
    username = _current_bot_username()
    if not username:
        return _private_json({"error": "Bot identity is unavailable"}, 503)
    return _private_json({"username": username})


def _is_unsafe_legacy_media(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized.startswith("telegram-file:") or "api.telegram.org/file/bot" in normalized


def _is_token_bearing_telegram_media(value: object) -> bool:
    return _is_unsafe_legacy_media(value)


def _safe_legacy_image(value: object) -> str:
    if not isinstance(value, str) or _is_unsafe_legacy_media(value):
        return ""
    normalized = value.strip()
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or len(normalized) > 2_000
        or any(character.isspace() for character in normalized)
    ):
        return ""
    return normalized


def _safe_legacy_images(value: object) -> list[str]:
    try:
        images = json.loads(value) if isinstance(value, str) else []
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(images, list):
        return []
    return [image for image in (_safe_legacy_image(item) for item in images) if image]



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
        SELECT b.id, b.title, b.price, b.category, b.emoji, b.cover_photo, b.description, b.images, b.category_id,
               c.emoji as category_emoji,
               b.stock_quantity,
               CASE
                   WHEN b.stock_quantity IS NULL THEN NULL
                   ELSE MAX(
                       0,
                       b.stock_quantity - COALESCE((
                           SELECT SUM(reservation.quantity)
                           FROM inventory_reservations reservation
                           WHERE reservation.book_id = b.id AND reservation.state = 'reserved'
                       ), 0)
                   )
               END AS available_quantity
        FROM books b
        LEFT JOIN categories c ON b.category_id = c.id
        WHERE b.is_active = 1 AND COALESCE(b.is_archived, 0) = 0
        ORDER BY {order_clause}
    """)
    books = []
    for row in cursor.fetchall():
        book = dict(row)
        available = book.pop("available_quantity")
        book.pop("stock_quantity", None)
        book["available_quantity"] = available
        book["is_available"] = available is None or available > 0
        media = media_variants_for_book_sync(book["id"])
        legacy_cover = _safe_legacy_image(book.get("cover_photo")) or _safe_legacy_image(book.get("emoji"))
        book.pop("emoji", None)
        book.pop("cover_photo", None)
        book["images"] = json.dumps(_safe_legacy_images(book.get("images")))
        if media["cover"] is None and legacy_cover:
            media["legacy_cover_url"] = legacy_cover
        book["media"] = media
        books.append(book)
    conn.close()
    return books


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
    payment_details_json='',
    checkout_key=None,
    items_subtotal=0,
    delivery_price=0,
    promo_code_snapshot=None,
    promo_discount=0,
    bonus_discount=0,
    acquisition_channel='unknown',
    acquisition_source='unknown',
    acquisition_campaign='unknown',
    acquisition_referrer_id=None,
):
    """Создать заказ в переданной или новой транзакции."""
    owns_connection = connection is None
    conn = connection or connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO orders (
            user_id, user_name, total, status, payment_method, payment_details_json, checkout_key,
            items_subtotal, delivery_price, promo_code_snapshot, promo_discount, bonus_discount,
            acquisition_channel, acquisition_source, acquisition_campaign, acquisition_referrer_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id, user_name, total, status, payment_method, payment_details_json, checkout_key,
            items_subtotal, delivery_price, promo_code_snapshot, promo_discount, bonus_discount,
            acquisition_channel, acquisition_source, acquisition_campaign, acquisition_referrer_id,
        ),
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


class CartInventoryConflictError(ValueError):
    pass


_MAX_CART_ITEMS = 50
_MAX_CART_QUANTITY = 99


def _cart_response(cart: list[dict], revision: int, status: int = 200, **extra):
    response = jsonify({"cart": cart, "revision": revision, **extra})
    response.status_code = status
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _private_json(payload: dict, status: int = 200):
    response = make_response(jsonify(payload), status)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _ensure_authenticated_user(connection: sqlite3.Connection) -> None:
    telegram_user = g.telegram_user
    name = " ".join(
        value
        for value in (
            getattr(telegram_user, "first_name", ""),
            getattr(telegram_user, "last_name", ""),
        )
        if value
    )
    connection.execute(
        "INSERT OR IGNORE INTO users (user_id, user_name) VALUES (?, ?)",
        (telegram_user.id, name[:255]),
    )



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
    for item in normalized:
        remaining = available_quantity(connection, item["id"])
        if remaining is not None and item["quantity"] > remaining:
            raise CartInventoryConflictError("cart contains an unavailable quantity")
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


_DELIVERY_NAME_PATTERN = re.compile(r"[^\s].{0,119}\Z", re.DOTALL)
_DELIVERY_CITY_PATTERN = re.compile(r"[^\s].{0,119}\Z", re.DOTALL)
_DELIVERY_POINT_PATTERN = re.compile(r"[^\s].{1,279}\Z", re.DOTALL)
_DELIVERY_PUBLIC_TEXT_PATTERN = re.compile(r"[^\s].{0,499}\Z", re.DOTALL)
_DELIVERY_PHONE_PATTERN = re.compile(r"\+?[0-9 ()-]{7,24}\Z")
_DELIVERY_ENABLED_SETTING = "delivery_enabled"
_DELIVERY_METHOD_SETTINGS = {
    METHOD_SDEK_PICKUP: {
        "enabled": "delivery_sdek_pickup_enabled",
        "price": "delivery_sdek_pickup_price_rub",
        "title": "СДЭК — пункт выдачи",
        "required_fields": ["recipient_name", "recipient_phone", "city", "pickup_point"],
        "note": "Укажите город и код или адрес ПВЗ. Администратор проверит пункт перед отправкой.",
    },
    METHOD_RUSSIAN_POST_PICKUP: {
        "enabled": "delivery_russian_post_pickup_enabled",
        "price": "delivery_russian_post_pickup_price_rub",
        "title": "Почта России — отделение",
        "required_fields": ["recipient_name", "recipient_phone", "city", "post_office"],
        "note": "Укажите город и индекс или адрес отделения Почты России.",
    },
    METHOD_SELF_PICKUP: {
        "enabled": "delivery_self_pickup_enabled",
        "price": "delivery_self_pickup_price_rub",
        "title": "Самовывоз",
        "required_fields": [],
        "note": "Заберите заказ по адресу и графику, указанным ниже.",
    },
}


def _clean_delivery_text(value: object, pattern: re.Pattern, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} is required")
    cleaned = " ".join(value.split())
    if not pattern.fullmatch(cleaned):
        raise ValueError(f"{field} is invalid")
    return cleaned


def _delivery_price(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, str) or not value.isdecimal():
        raise ValueError("delivery price is invalid")
    price = int(value)
    if price > 100_000:
        raise ValueError("delivery price is invalid")
    return price


def _self_pickup_snapshot(payment_settings: dict[str, str]) -> tuple[dict[str, str], str]:
    location = _clean_delivery_text(
        payment_settings.get("delivery_self_pickup_location", ""),
        _DELIVERY_PUBLIC_TEXT_PATTERN,
        "self pickup location",
    )
    schedule = _clean_delivery_text(
        payment_settings.get("delivery_self_pickup_schedule", ""),
        _DELIVERY_PUBLIC_TEXT_PATTERN,
        "self pickup schedule",
    )
    instructions = _clean_delivery_text(
        payment_settings.get("delivery_self_pickup_instructions", ""),
        _DELIVERY_PUBLIC_TEXT_PATTERN,
        "self pickup instructions",
    )
    destination = {
        "pickup_location": location,
        "pickup_schedule": schedule,
        "pickup_instructions": instructions,
    }
    return destination, f"{location}\n{schedule}\n{instructions}"


def _delivery_options(payment_settings: dict[str, str]) -> dict[str, dict]:
    methods: dict[str, dict] = {}
    for method, contract in _DELIVERY_METHOD_SETTINGS.items():
        if payment_settings.get(contract["enabled"]) != "1":
            continue
        try:
            price = _delivery_price(payment_settings.get(contract["price"], ""))
            public_snapshot = ""
            if method == METHOD_SELF_PICKUP:
                _, public_snapshot = _self_pickup_snapshot(payment_settings)
        except ValueError:
            continue
        methods[method] = {
            "id": method,
            "title": contract["title"],
            "price": price,
            "required_fields": contract["required_fields"],
            "note": contract["note"],
            "public_instructions": public_snapshot or None,
        }
    return methods


def _validate_delivery_payload(
    value: object,
    methods: dict[str, dict],
    payment_settings: dict[str, str],
) -> tuple[str, dict[str, str], str]:
    if not isinstance(value, dict) or not isinstance(value.get("method"), str):
        raise ValueError("delivery details are required")
    method = value["method"]
    if method not in methods:
        raise ValueError("selected delivery method is unavailable")
    if method == METHOD_SELF_PICKUP:
        if set(value) != {"method"}:
            raise ValueError("self pickup details are invalid")
        destination, public_snapshot = _self_pickup_snapshot(payment_settings)
        return method, destination, public_snapshot
    expected = {
        "method", "recipient_name", "recipient_phone", "city",
        "pickup_point" if method == METHOD_SDEK_PICKUP else "post_office",
    }
    if set(value) != expected:
        raise ValueError("delivery details are required")
    phone = _clean_delivery_text(value["recipient_phone"], _DELIVERY_PHONE_PATTERN, "recipient phone")
    digits = "".join(character for character in phone if character.isdigit())
    if not 10 <= len(digits) <= 15:
        raise ValueError("recipient phone is invalid")
    destination = {
        "recipient_name": _clean_delivery_text(
            value["recipient_name"], _DELIVERY_NAME_PATTERN, "recipient name"
        ),
        "recipient_phone": "+" + digits,
        "city": _clean_delivery_text(value["city"], _DELIVERY_CITY_PATTERN, "city"),
    }
    point_field = "pickup_point" if method == METHOD_SDEK_PICKUP else "post_office"
    destination[point_field] = _clean_delivery_text(
        value[point_field], _DELIVERY_POINT_PATTERN, point_field
    )
    return method, destination, ""


def _delivery_capability(payment_settings: dict[str, str]) -> dict:
    enabled = payment_settings.get(_DELIVERY_ENABLED_SETTING) == "1"
    encryption_available = delivery_encryption_is_available()
    methods = _delivery_options(payment_settings) if enabled and encryption_available else {}
    available = enabled and encryption_available and bool(methods)
    if not enabled:
        message = ""
    elif not encryption_available:
        message = "Доставка временно недоступна. Попробуйте позже."
    else:
        message = "Настройте и включите хотя бы один способ доставки."
    return {
        "enabled": enabled,
        "available": available,
        "methods": list(methods.values()) if available else [],
        "message": message,
    }


def _insert_delivery(
    connection: sqlite3.Connection,
    order_id: int,
    method: str,
    destination: dict[str, str],
    delivery_price: int,
    public_snapshot: str = "",
) -> None:
    encrypted_destination = encrypt_destination(order_id, method, destination)
    connection.execute(
        """
        INSERT INTO order_deliveries (
            order_id, method, destination_encrypted, public_instructions_snapshot,
            delivery_price, shipment_status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            method,
            encrypted_destination,
            public_snapshot,
            delivery_price,
            SHIPMENT_AWAITING_PAYMENT,
        ),
    )


def _delivery_summary_for_order(connection: sqlite3.Connection, order_id: int) -> dict | None:
    row = connection.execute(
        "SELECT * FROM order_deliveries WHERE order_id = ?", (order_id,)
    ).fetchone()
    return safe_delivery_summary(row)


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


def _manual_payment_details(payment_settings: dict) -> dict[str, str]:
    return {
        "card": str(payment_settings.get("card_number") or ""),
        "sbp_phone": str(payment_settings.get("sbp_phone") or ""),
        "sbp_bank": str(payment_settings.get("sbp_bank") or ""),
        "recipient": str(payment_settings.get("recipient_name") or ""),
        "instructions": str(payment_settings.get("payment_instructions") or ""),
    }


def _manual_payment_text(order_id: int, total: int, details: dict[str, str]) -> str:
    lines = [
        f"📦 <b>Заказ #{order_id} создан!</b>",
        f"💰 К оплате: <b>{total} ₽</b>",
        "",
        "<b>Реквизиты для оплаты:</b>",
        "",
    ]
    if details.get("card"):
        lines.append(f"💳 Карта: <code>{escape(details['card'])}</code>")
    if details.get("sbp_phone"):
        lines.append(f"📱 СБП: <code>{escape(details['sbp_phone'])}</code>")
    if details.get("sbp_bank"):
        lines.append(f"🏦 Банк: {escape(details['sbp_bank'])}")
    if details.get("recipient"):
        lines.append(f"👤 Получатель: {escape(details['recipient'])}")
    if details.get("instructions"):
        lines.extend(["", f"📝 {escape(details['instructions'])}"])
    lines.extend(["", f"⚠️ Укажите номер заказа <b>#{order_id}</b> в комментарии."])
    return "\n".join(lines)


def _order_payment_details(order: sqlite3.Row | dict) -> dict[str, str] | None:
    raw_details = order["payment_details_json"] or ""
    try:
        details = json.loads(raw_details)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(details, dict):
        return None
    return {key: str(details.get(key) or "") for key in ("card", "sbp_phone", "sbp_bank", "recipient", "instructions")}


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
            _record_operational_event("error", "yookassa_create", "failed", reason="attempt_missing", order_id=order_id)
            return None, "Платёж не найден"
        attempt = dict(attempt)
        if attempt["status"] == "canceled":
            return attempt, "Платёж отменён. Создайте новый платёж."
        if attempt["provider_payment_id"] and attempt["confirmation_url"]:
            return attempt, None
        if not yookassa_is_configured():
            _record_operational_event(
                "critical", "yookassa_create", "failed", reason="provider_unconfigured",
                order_id=order_id, attempt_id=attempt["id"]
            )
            return attempt, "ЮKassa временно недоступна"
        try:
            payment = _yookassa_create_payment(
                attempt["amount"], order_id, attempt["id"], attempt["idempotence_key"]
            )
        except Exception as exc:
            connection.execute(
                "UPDATE yookassa_payments SET status = 'creation_pending', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (attempt["id"],),
            )
            connection.commit()
            _record_operational_event(
                "critical", "yookassa_create", "failed", reason="provider_request_failed",
                order_id=order_id, attempt_id=attempt["id"], error=exc
            )
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
            _record_operational_event(
                "critical", "yookassa_create", "failed", reason="provider_response_invalid",
                order_id=order_id, attempt_id=attempt["id"]
            )
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
        _record_operational_event(
            "info", "yookassa_create", "succeeded",
            order_id=order_id, attempt_id=attempt["id"]
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
    delivery_connection = connect()
    delivery_connection.row_factory = sqlite3.Row
    try:
        delivery = _delivery_summary_for_order(delivery_connection, order["id"])
        items_total = delivery_connection.execute(
            "SELECT COALESCE(SUM(price), 0) FROM order_items WHERE order_id = ?",
            (order["id"],),
        ).fetchone()[0]
    finally:
        delivery_connection.close()
    response = {
        "success": True,
        **_safe_payment_response(order, attempt),
        "items_total": items_total,
        "delivery_price": delivery["price"] if delivery else 0,
        "delivery": delivery,
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


def _csv_cell(value):
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def _csv_response(rows, headers, filename):
    """CSV-ответ с BOM и разделителем ';' — Excel (ru-RU) открывает сразу."""
    import csv
    from io import StringIO
    buf = StringIO()
    writer = csv.writer(buf, delimiter=';', quoting=csv.QUOTE_MINIMAL)
    writer.writerow([_csv_cell(header) for header in headers])
    writer.writerows([[_csv_cell(value) for value in row] for row in rows])
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
    # поэтому окна строим в UTC.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
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

    latest_backup = get_latest_event_sync("sqlite_backup")
    return {
        'periods': periods,
        'top_books': top_books,
        'avg_check': avg_check,
        'total_orders': t_orders,
        'total_revenue': t_revenue,
        'latest_backup': (
            {
                'outcome': latest_backup['outcome'],
                'created_at': latest_backup['created_at'],
            }
            if latest_backup
            else None
        ),
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
        }, **_telegram_request_options(10))
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
        response = requests.post(url, json=data, **_telegram_request_options(10))
        result = response.json()
        _record_operational_event(
            "info" if result.get("ok") else "error",
            "telegram_stars_invoice",
            "succeeded" if result.get("ok") else "failed",
            reason="telegram_response",
            order_id=order_id,
        )
        return result
    except Exception as exc:
        _record_operational_event(
            "critical", "telegram_stars_invoice", "failed", reason="request_failed",
            order_id=order_id, error=exc
        )
        return {'ok': False, 'description': 'Telegram request failed'}


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
        response = requests.post(url, json=data, **_telegram_request_options(10))
        result = response.json()
        log_event(
            logger,
            logging.INFO if result.get("ok") else logging.ERROR,
            component="telegram_message",
            event="send_with_keyboard",
            outcome="succeeded" if result.get("ok") else "failed",
            reason="telegram_response",
        )
        return result
    except Exception as exc:
        _record_operational_event(
            "critical", "telegram_message", "failed", reason="request_failed", error=exc
        )
        return {'ok': False, 'description': 'Telegram request failed'}
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


@app.route('/api/storefront/config', methods=['GET'])
def api_storefront_config():
    connection = connect()
    try:
        row = connection.execute(
            """
            SELECT store_name, primary_color, accent_color, logo_asset_id, support_contact
            FROM storefront_settings WHERE id = 1
            """
        ).fetchone()
    finally:
        connection.close()
    tenant_context = maybe_current_tenant_context()
    if row is None:
        payload = {
            "store_name": "Семена Знаний",
            "primary_color": "#111111",
            "accent_color": "#111111",
            "logo_asset_id": None,
            "support_contact": "",
        }
    else:
        payload = {
            "store_name": row[0] or "Семена Знаний",
            "primary_color": row[1] or "#111111",
            "accent_color": row[2] or "#111111",
            "logo_asset_id": row[3],
            "support_contact": row[4] or "",
        }
    payload["tenant_id"] = tenant_context.tenant_id if tenant_context else "legacy"
    payload["features"] = sorted(tenant_context.entitlements.features) if tenant_context else []
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/favicon.ico', methods=['GET'])
def favicon():
    return Response(status=204, headers={'Cache-Control': 'public, max-age=86400'})


@app.route('/media/books/<asset_id>/<variant>', methods=['GET'])
def book_media(asset_id: str, variant: str):
    resolved = resolve_media_variant_sync(asset_id, variant)
    if not resolved:
        return Response(status=404)
    path, mime_type = resolved
    response = send_file(path, mimetype=mime_type, conditional=True, max_age=31536000)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


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


@app.route('/api/admin/inventory', methods=['GET'])
@require_telegram_permission("inventory.read")
def api_admin_inventory_list():
    try:
        limit = int(request.args.get("limit", "50"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        return jsonify({"error": "Invalid pagination"}), 400
    query = request.args.get("query", "")
    if len(query) > 120:
        return jsonify({"error": "Search query is too long"}), 400
    low_stock_only = request.args.get("low_stock") == "1"
    return jsonify({
        "inventory": list_inventory_sync(
            limit=limit,
            offset=offset,
            query=query,
            low_stock_only=low_stock_only,
        ),
    })


@app.route('/api/admin/inventory/movements', methods=['GET'])
@require_telegram_permission("inventory.read")
def api_admin_inventory_movements():
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    before_value = request.args.get("before_id")
    try:
        before_id = int(before_value) if before_value is not None else None
    except ValueError:
        return jsonify({"error": "before_id must be an integer"}), 400
    if before_id is not None and before_id <= 0:
        return jsonify({"error": "before_id must be positive"}), 400
    book_value = request.args.get("book_id")
    try:
        book_id = int(book_value) if book_value is not None else None
    except ValueError:
        return jsonify({"error": "book_id must be an integer"}), 400
    if book_id is not None and book_id <= 0:
        return jsonify({"error": "book_id must be positive"}), 400
    movements = list_inventory_movements_sync(
        limit=max(1, min(limit, 100)), before_id=before_id, book_id=book_id
    )
    return jsonify({
        "movements": movements,
        "next_before_id": movements[-1]["id"] if len(movements) == max(1, min(limit, 100)) else None,
    })


@app.route('/api/admin/inventory/<int:book_id>', methods=['GET', 'POST'])
@require_telegram_permission("inventory.read")
def api_admin_inventory(book_id: int):
    if request.method == 'GET':
        summary = inventory_summary_sync(book_id)
        if summary is None:
            return jsonify({"error": "book not found"}), 404
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            return jsonify({"error": "limit must be an integer"}), 400
        return jsonify({"summary": summary, "movements": recent_movements_sync(book_id, limit)})

    if not has_permission_sync(
        g.telegram_user.id, "inventory.adjust", legacy_admin_ids=ADMIN_IDS
    ):
        return jsonify({"error": "Forbidden"}), 403
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("quantity"), int) or isinstance(payload.get("quantity"), bool):
        return jsonify({"error": "quantity must be an integer"}), 400
    reason = payload.get("reason", "")
    if not isinstance(reason, str) or reason not in {"received", "recount", "damaged", "return"}:
        return jsonify({"error": "A valid inventory operation is required"}), 400
    try:
        changed = adjust_stock_sync(
            book_id,
            payload["quantity"],
            g.telegram_user.id,
            reason,
            actor_role=g.staff_role,
        )
    except InventoryUnavailableError as error:
        return jsonify({"error": str(error)}), 409
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    if not changed:
        return jsonify({"error": "book not found"}), 404
    return jsonify({"summary": inventory_summary_sync(book_id)})


@app.route('/api/admin/session', methods=['GET'])
@require_telegram_permission("admin.access")
def api_admin_session():
    role = g.staff_role
    return jsonify({
        "user_id": g.telegram_user.id,
        "role": role,
        "capabilities": capabilities_for_role(role),
    })


@app.route('/api/admin/staff', methods=['GET', 'PUT', 'DELETE'])
@require_telegram_permission("staff.manage")
def api_admin_staff():
    if request.method == 'GET':
        return jsonify({"staff": list_staff_sync()})
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object is required"}), 400
    user_id = payload.get("telegram_user_id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return jsonify({"error": "telegram_user_id is required"}), 400
    if is_owner_sync(user_id, legacy_admin_ids=ADMIN_IDS):
        return jsonify({"error": "Owner role is configured outside the database"}), 400
    if request.method == 'DELETE':
        try:
            deleted = delete_inactive_staff_member_sync(
                user_id,
                actor_user_id=g.telegram_user.id,
                actor_role=g.staff_role,
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 409
        if not deleted:
            return jsonify({"error": "staff member not found"}), 404
        return jsonify({"deleted": True})

    role = payload.get("role")
    active = payload.get("is_active")
    if not isinstance(role, str) or not isinstance(active, bool):
        return jsonify({"error": "role and is_active are required"}), 400
    try:
        staff = set_staff_member_sync(
            user_id,
            role,
            active=active,
            actor_user_id=g.telegram_user.id,
            actor_role=g.staff_role,
        )
    except QuotaExceededError as error:
        return jsonify({"error": str(error), "limit": error.limit}), 409
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"staff": staff})


@app.route('/api/admin/books/import/preview', methods=['POST'])
@require_telegram_permission("book.import")
def api_preview_book_import():
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "Import file is required"}), 400
    try:
        result = preview_book_import_sync(
            upload.filename or "",
            upload.read(2 * 1024 * 1024 + 1),
            g.telegram_user.id,
        )
    except BookImportError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(result)


@app.route('/api/admin/books/import/<batch_id>/commit', methods=['POST'])
@require_telegram_permission("book.import")
def api_commit_book_import(batch_id: str):
    if request.get_data(cache=False):
        return jsonify({"error": "Request body is not supported"}), 400
    try:
        return jsonify(commit_book_import_sync(batch_id, g.telegram_user.id))
    except QuotaExceededError as error:
        return jsonify({"error": str(error), "limit": error.limit}), 409
    except BookImportError as error:
        return jsonify({"error": str(error)}), 409


@app.route('/api/admin/fulfillment', methods=['GET'])
@require_telegram_permission("fulfillment.manage")
def api_fulfillment_queue():
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    return jsonify({"orders": list_fulfillment_queue_sync(limit=limit)})


@app.route('/api/admin/fulfillment/<int:order_id>', methods=['GET'])
@require_telegram_permission("fulfillment.manage")
def api_fulfillment_detail(order_id: int):
    try:
        record = get_fulfillment_sync(
            order_id,
            g.telegram_user.id,
            is_owner=g.staff_role == "owner",
        )
    except FulfillmentError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"fulfillment": record})


@app.route('/api/admin/fulfillment/<int:order_id>/claim', methods=['POST'])
@require_telegram_permission("fulfillment.manage")
def api_fulfillment_claim(order_id: int):
    if request.get_data(cache=False):
        return jsonify({"error": "Request body is not supported"}), 400
    try:
        record = claim_fulfillment_sync(order_id, g.telegram_user.id, g.staff_role)
    except FulfillmentError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"fulfillment": record})


@app.route('/api/admin/fulfillment/<int:order_id>/lines', methods=['PUT'])
@require_telegram_permission("fulfillment.manage")
def api_fulfillment_line(order_id: int):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object is required"}), 400
    book_id = payload.get("book_id")
    title = payload.get("title")
    price = payload.get("price")
    picked_quantity = payload.get("picked_quantity")
    if (
        not isinstance(book_id, int) or not isinstance(title, str) or len(title) > 255
        or not isinstance(price, int) or not isinstance(picked_quantity, int)
    ):
        return jsonify({"error": "Invalid packing line"}), 400
    try:
        record = set_picked_quantity_sync(
            order_id,
            book_id,
            title,
            price,
            picked_quantity,
            g.telegram_user.id,
            g.staff_role,
            is_owner=g.staff_role == "owner",
        )
    except FulfillmentError as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({"fulfillment": record})


@app.route('/api/admin/fulfillment/<int:order_id>/pack', methods=['POST'])
@require_telegram_permission("fulfillment.manage")
def api_fulfillment_pack(order_id: int):
    if request.get_data(cache=False):
        return jsonify({"error": "Request body is not supported"}), 400
    try:
        record = pack_fulfillment_sync(
            order_id,
            g.telegram_user.id,
            g.staff_role,
            is_owner=g.staff_role == "owner",
        )
    except FulfillmentError as error:
        payload = {"error": str(error), "code": error.code}
        if error.remaining_quantity:
            payload["remaining_quantity"] = error.remaining_quantity
        return jsonify(payload), 409
    return jsonify({"fulfillment": record, "already_packed": record.get("already_packed", False)})


@app.route('/api/admin/fulfillment/<int:order_id>/print', methods=['GET'])
@require_telegram_permission("fulfillment.manage")
def api_fulfillment_print(order_id: int):
    try:
        payload = packing_print_payload_sync(
            order_id,
            g.telegram_user.id,
            is_owner=g.staff_role == "owner",
        )
        if payload["method"] == METHOD_SELF_PICKUP:
            recipient = {"instructions": payload["public_instructions_snapshot"]}
        else:
            recipient = decrypt_destination(
                order_id, payload["method"], payload["destination_encrypted"]
            )
    except (FulfillmentError, DeliveryCryptoError) as error:
        return jsonify({"error": str(error)}), 409
    return jsonify({
        "order_id": payload["id"],
        "created_at": payload["created_at"],
        "method": payload["method"],
        "shipment_status": payload["shipment_status"],
        "recipient": recipient,
        "fulfillment": payload["fulfillment"],
    })


@app.route('/api/admin/audit', methods=['GET'])
@require_telegram_permission("audit.read")
def api_admin_audit():
    try:
        limit = int(request.args.get("limit", "50"))
        before_id = request.args.get("before_id")
        before = int(before_id) if before_id else None
    except ValueError:
        return jsonify({"error": "Invalid cursor"}), 400
    action = request.args.get("action", "")
    entity_type = request.args.get("entity_type", "")
    if len(action) > 80 or len(entity_type) > 40:
        return jsonify({"error": "Invalid filter"}), 400
    return jsonify({
        "events": list_audit_events_sync(
            limit=limit,
            before_id=before,
            action=action,
            entity_type=entity_type,
        )
    })


@app.route('/api/admin/dashboard', methods=['GET'])
@require_telegram_permission("reports.view")
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
@require_telegram_permission("reports.view")
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


def _report_timezone():
    try:
        return ZoneInfo(settings.REPORT_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        if settings.REPORT_TIMEZONE == "Europe/Moscow":
            return timezone(timedelta(hours=3))
        raise ValueError("REPORT_TIMEZONE is unavailable") from exc


def _sales_date_range() -> tuple[str | None, str | None]:
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    if not date_from and not date_to:
        return None, None
    try:
        zone = _report_timezone()
        start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=zone) if date_from else None
        end = (
            (datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=zone)
            if date_to
            else None
        )
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("date_from and date_to must use YYYY-MM-DD") from exc
    if start and end and start >= end:
        raise ValueError("date_from must not be after date_to")
    if start and end and end - start > timedelta(days=366):
        raise ValueError("date range must not exceed 366 days")
    return (
        start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if start else None,
        end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if end else None,
    )


@app.route('/api/admin/export/sales', methods=['GET'])
@require_telegram_permission("reports.view")
def api_export_sales():
    try:
        date_from, date_to = _sales_date_range()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        conditions = ["o.status IN ('paid', 'confirmed', 'completed')"]
        params: list[str] = []
        if date_from:
            conditions.append("COALESCE(o.paid_at, o.created_at) >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("COALESCE(o.paid_at, o.created_at) < ?")
            params.append(date_to)
        rows = connection.execute(
            f"""
            SELECT COALESCE(o.paid_at, o.created_at) AS sale_at,
                   o.id AS order_id,
                   o.payment_method,
                   o.status,
                   oi.book_id,
                   oi.title,
                   oi.price AS unit_price,
                   COUNT(*) AS quantity,
                   SUM(oi.price) AS line_total,
                   o.total AS order_total
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            WHERE {' AND '.join(conditions)}
            GROUP BY o.id, oi.book_id, oi.title, oi.price
            ORDER BY sale_at DESC, o.id DESC, oi.title ASC
            """,
            params,
        ).fetchall()
        output = [
            [
                _export_ts(row["sale_at"]),
                row["order_id"],
                row["payment_method"],
                STATUS_LABELS.get(row["status"], row["status"]),
                row["book_id"],
                row["title"],
                row["unit_price"],
                row["quantity"],
                row["line_total"],
                row["order_total"],
            ]
            for row in rows
        ]
        return _csv_response(
            output,
            [
                f"дата продажи ({settings.REPORT_TIMEZONE})",
                "заказ",
                "способ оплаты",
                "статус",
                "id книги",
                "товар",
                "цена за шт",
                "количество",
                "сумма позиции",
                "сумма заказа",
            ],
            "sales.csv",
        )
    finally:
        connection.close()


@app.route('/api/admin/campaigns', methods=['GET', 'PUT'])
@require_telegram_permission("campaign.manage")
def api_admin_campaigns():
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        if request.method == 'GET':
            rows = connection.execute(
                """
                SELECT code, channel, source, campaign, is_active, created_at
                FROM acquisition_campaigns ORDER BY created_at DESC, code ASC
                """
            ).fetchall()
            return make_response(
                jsonify({"campaigns": [dict(row) for row in rows]}),
                200,
                {"Cache-Control": "private, no-store"},
            )
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"code", "channel", "source", "campaign"}:
            return jsonify({"error": "Expected code, channel, source and campaign"}), 400
        values = {key: data[key].strip() if isinstance(data[key], str) else "" for key in data}
        if (
            not re.fullmatch(r"[a-z0-9_-]{3,48}", values["code"])
            or not re.fullmatch(r"[a-z0-9_-]{2,48}", values["channel"])
            or len(values["source"]) > 120
            or len(values["campaign"]) > 120
        ):
            return jsonify({"error": "Invalid campaign fields"}), 400
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT 1 FROM acquisition_campaigns WHERE code = ?", (values["code"],)
        ).fetchone()
        if existing is None:
            campaign_count = connection.execute(
                "SELECT COUNT(*) FROM acquisition_campaigns"
            ).fetchone()[0]
            require_count_quota(LIMIT_CAMPAIGNS, campaign_count)
        connection.execute(
            """
            INSERT INTO acquisition_campaigns (code, channel, source, campaign)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                channel = excluded.channel,
                source = excluded.source,
                campaign = excluded.campaign,
                is_active = 1
            """,
            (values["code"], values["channel"], values["source"], values["campaign"]),
        )
        append_audit_event(
            connection,
            actor_user_id=g.telegram_user.id,
            actor_role=g.staff_role,
            source="mini_app",
            action="campaign.saved",
            entity_type="campaign",
            entity_id=values["code"],
            details={"to_status": "active"},
        )
        connection.commit()
        return make_response(
            jsonify({"code": values["code"], "deep_link_payload": f"c_{values['code']}"}),
            200,
            {"Cache-Control": "private, no-store"},
        )
    except QuotaExceededError as error:
        connection.rollback()
        return jsonify({"error": str(error), "limit": error.limit}), 409
    finally:
        connection.close()


@app.route('/api/admin/campaigns/<string:code>', methods=['PATCH', 'DELETE'])
@require_telegram_permission("campaign.manage")
def api_admin_campaign(code: str):
    if not re.fullmatch(r"[a-z0-9_-]{3,48}", code):
        return jsonify({"error": "Invalid campaign code"}), 400
    connection = connect()
    try:
        if request.method == "PATCH":
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or set(data) != {"is_active"} or not isinstance(data["is_active"], bool):
                return jsonify({"error": "is_active is required"}), 400
            cursor = connection.execute(
                "UPDATE acquisition_campaigns SET is_active = ? WHERE code = ?",
                (int(data["is_active"]), code),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return jsonify({"error": "campaign not found"}), 404
            status = "active" if data["is_active"] else "inactive"
            append_audit_event(
                connection,
                actor_user_id=g.telegram_user.id,
                actor_role=g.staff_role,
                source="mini_app",
                action="campaign.status.updated",
                entity_type="campaign",
                entity_id=code,
                details={"to_status": status},
            )
            connection.commit()
            return jsonify({"code": code, "is_active": data["is_active"]})

        cursor = connection.execute(
            "DELETE FROM acquisition_campaigns WHERE code = ?", (code,)
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return jsonify({"error": "campaign not found"}), 404
        append_audit_event(
            connection,
            actor_user_id=g.telegram_user.id,
            actor_role=g.staff_role,
            source="mini_app",
            action="campaign.deleted",
            entity_type="campaign",
            entity_id=code,
        )
        connection.commit()
        return jsonify({"deleted": True})
    finally:
        connection.close()


@app.route('/api/admin/analytics', methods=['GET'])
@require_telegram_permission("reports.view")
def api_admin_analytics():
    try:
        date_from, date_to = _sales_date_range()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        conditions = ["status IN ('paid', 'confirmed', 'completed')"]
        params: list[str] = []
        if date_from:
            conditions.append("COALESCE(paid_at, created_at) >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("COALESCE(paid_at, created_at) < ?")
            params.append(date_to)
        where = " AND ".join(conditions)
        promos = connection.execute(
            f"""
            SELECT COALESCE(promo_code_snapshot, '(без промокода)') AS promo_code,
                   COUNT(*) AS orders,
                   SUM(items_subtotal) AS gross_revenue,
                   SUM(promo_discount) AS promo_discount,
                   SUM(bonus_discount) AS bonus_discount,
                   SUM(total) AS net_revenue,
                   COUNT(DISTINCT user_id) AS buyers
            FROM orders
            WHERE {where}
            GROUP BY promo_code_snapshot
            ORDER BY net_revenue DESC, orders DESC, promo_code ASC
            """,
            params,
        ).fetchall()
        acquisition = connection.execute(
            f"""
            SELECT acquisition_channel AS channel, acquisition_source AS source,
                   acquisition_campaign AS campaign, COUNT(*) AS orders,
                   COUNT(DISTINCT user_id) AS buyers, SUM(items_subtotal) AS gross_revenue,
                   SUM(promo_discount) AS promo_discount, SUM(bonus_discount) AS bonus_discount,
                   SUM(total) AS net_revenue
            FROM orders
            WHERE {where}
            GROUP BY acquisition_channel, acquisition_source, acquisition_campaign
            ORDER BY net_revenue DESC, orders DESC, channel ASC
            """,
            params,
        ).fetchall()
        return make_response(
            jsonify(
                {
                    "basis": "paid_sales",
                    "promos": [dict(row) for row in promos],
                    "acquisition": [dict(row) for row in acquisition],
                }
            ),
            200,
            {"Cache-Control": "private, no-store"},
        )
    finally:
        connection.close()


def _referral_analytics_payload() -> dict:
    date_from, date_to = _sales_date_range()
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        referral_conditions: list[str] = []
        sale_conditions = ["o.status IN ('paid', 'confirmed', 'completed')"]
        referral_params: list[str] = []
        sale_params: list[str] = []
        if date_from:
            referral_conditions.append("r.created_at >= ?")
            referral_params.append(date_from)
            sale_conditions.append("COALESCE(o.paid_at, o.created_at) >= ?")
            sale_params.append(date_from)
        if date_to:
            referral_conditions.append("r.created_at < ?")
            referral_params.append(date_to)
            sale_conditions.append("COALESCE(o.paid_at, o.created_at) < ?")
            sale_params.append(date_to)

        referrals_where = f"WHERE {' AND '.join(referral_conditions)}" if referral_conditions else ""
        accepted_referrals = connection.execute(
            f"SELECT COUNT(*) FROM referrals r {referrals_where}", referral_params
        ).fetchone()[0]
        sales = connection.execute(
            f"""
            SELECT COUNT(DISTINCT r.referred_id) AS buyers,
                   COUNT(*) AS orders,
                   COALESCE(SUM(o.items_subtotal), 0) AS gross_revenue,
                   COALESCE(SUM(o.promo_discount), 0) AS promo_discount,
                   COALESCE(SUM(o.bonus_discount), 0) AS bonus_discount,
                   COALESCE(SUM(o.total), 0) AS net_revenue
            FROM orders o
            JOIN referrals r
              ON r.referred_id = o.user_id
             AND r.created_at <= COALESCE(o.paid_at, o.created_at)
            WHERE {' AND '.join(sale_conditions)}
            """,
            sale_params,
        ).fetchone()
        return {
            "basis": "paid_referral_sales_after_acceptance",
            "accepted_referrals": accepted_referrals,
            **dict(sales),
        }
    finally:
        connection.close()


@app.route('/api/admin/analytics/referrals', methods=['GET'])
@require_telegram_permission("reports.view")
def api_admin_referral_analytics():
    try:
        return _private_json(_referral_analytics_payload())
    except ValueError as exc:
        return _private_json({"error": str(exc)}, 400)

@app.route('/api/admin/export/analytics', methods=['GET'])
@require_telegram_permission("reports.view")
def api_export_analytics():
    dimension = request.args.get("dimension", "promo")
    if dimension not in {"promo", "acquisition", "referral"}:
        return jsonify({"error": "dimension must be promo, acquisition or referral"}), 400
    if dimension == "referral":
        try:
            referral = _referral_analytics_payload()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        rows = [[
            referral["accepted_referrals"], referral["buyers"], referral["orders"],
            referral["gross_revenue"], referral["promo_discount"],
            referral["bonus_discount"], referral["net_revenue"],
        ]]
        headers = [
            "принятые_приглашения", "реферальные_покупатели", "оплаченные_заказы",
            "сумма_до_скидок", "скидка_промо", "скидка_бонус", "выручка",
        ]
    else:
        result = api_admin_analytics()
        response = make_response(result)
        if response.status_code != 200:
            return response
        payload = response.get_json()
        if dimension == "promo":
            rows = [
                [
                    row["promo_code"], row["orders"], row["buyers"], row["gross_revenue"],
                    row["promo_discount"], row["bonus_discount"], row["net_revenue"],
                ]
                for row in payload["promos"]
            ]
            headers = ["промокод", "заказы", "покупатели", "сумма_до_скидок", "скидка_промо", "скидка_бонус", "выручка"]
        else:
            rows = [
                [
                    row["channel"], row["source"], row["campaign"], row["orders"], row["buyers"],
                    row["gross_revenue"], row["promo_discount"], row["bonus_discount"], row["net_revenue"],
                ]
                for row in payload["acquisition"]
            ]
            headers = ["канал", "источник", "кампания", "заказы", "покупатели", "сумма_до_скидок", "скидка_промо", "скидка_бонус", "выручка"]
    response = _csv_response(rows, headers, f"analytics-{dimension}.csv")
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route('/api/admin/export/books', methods=['GET'])
@require_telegram_permission("reports.view")
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
    except CartInventoryConflictError as exc:
        connection.rollback()
        stored_cart, stored_revision = _read_cart(connection, g.telegram_user.id)
        return _cart_response(
            stored_cart,
            stored_revision,
            409,
            error=str(exc),
            code="inventory_changed",
        )
    except ValueError as exc:
        connection.rollback()
        return jsonify({'error': str(exc)}), 400
    except sqlite3.Error:
        connection.rollback()
        return jsonify({'error': 'Cart is unavailable'}), 503
    finally:
        connection.close()


@app.route('/api/favorites', methods=['GET'])
@require_telegram_user
def api_favorites():
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT f.book_id, f.created_at, b.title, b.price, b.is_active,
                   COALESCE(b.is_archived, 0) AS is_archived, b.stock_quantity
            FROM user_favorites f
            JOIN books b ON b.id = f.book_id
            WHERE f.user_id = ?
            ORDER BY f.created_at DESC
            """,
            (g.telegram_user.id,),
        ).fetchall()
        return _private_json({"favorites": [dict(row) for row in rows]})
    finally:
        connection.close()


@app.route('/api/favorites/<int:book_id>', methods=['PUT', 'DELETE'])
@require_telegram_user
def api_favorite(book_id: int):
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_authenticated_user(connection)
        if request.method == 'PUT':
            book = connection.execute(
                """
                SELECT 1 FROM books
                WHERE id = ? AND is_active = 1 AND COALESCE(is_archived, 0) = 0
                """,
                (book_id,),
            ).fetchone()
            if not book:
                connection.rollback()
                return _private_json({"error": "book is unavailable"}, 404)
            connection.execute(
                "INSERT OR IGNORE INTO user_favorites (user_id, book_id) VALUES (?, ?)",
                (g.telegram_user.id, book_id),
            )
            active = True
        else:
            connection.execute(
                "DELETE FROM user_favorites WHERE user_id = ? AND book_id = ?",
                (g.telegram_user.id, book_id),
            )
            active = False
        connection.commit()
        return _private_json({"book_id": book_id, "favorite": active})
    except sqlite3.Error:
        connection.rollback()
        return _private_json({"error": "favorites are unavailable"}, 503)
    finally:
        connection.close()


@app.route('/api/back-in-stock-subscriptions', methods=['GET'])
@require_telegram_user
def api_back_in_stock_subscriptions():
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT book_id, consented_at, consent_version
            FROM back_in_stock_subscriptions
            WHERE user_id = ? AND active = 1
            ORDER BY consented_at DESC
            """,
            (g.telegram_user.id,),
        ).fetchall()
        return _private_json(
            {"book_ids": [row[0] for row in rows], "subscriptions": [dict(row) for row in rows]}
        )
    finally:
        connection.close()


@app.route('/api/books/<int:book_id>/back-in-stock-subscription', methods=['PUT', 'DELETE'])
@require_telegram_user
def api_back_in_stock_subscription(book_id: int):
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_authenticated_user(connection)
        if request.method == 'PUT':
            book = connection.execute(
                """
                SELECT stock_quantity
                FROM books
                WHERE id = ? AND is_active = 1 AND COALESCE(is_archived, 0) = 0
                """,
                (book_id,),
            ).fetchone()
            if not book:
                connection.rollback()
                return _private_json({"error": "book is unavailable"}, 404)
            stock_quantity = book[0]
            remaining = available_quantity(connection, book_id)
            if stock_quantity is None or remaining is None or remaining > 0:
                connection.rollback()
                return _private_json({"error": "book is currently available"}, 409)
            connection.execute(
                """
                INSERT INTO back_in_stock_subscriptions (user_id, book_id, active, consent_version, revoked_at, notified_at)
                VALUES (?, ?, 1, 'v1', NULL, NULL)
                ON CONFLICT(user_id, book_id) DO UPDATE SET
                    active = 1,
                    consented_at = CURRENT_TIMESTAMP,
                    consent_version = 'v1',
                    revoked_at = NULL,
                    notified_at = NULL
                """,
                (g.telegram_user.id, book_id),
            )
            active = True
        else:
            connection.execute(
                """
                UPDATE back_in_stock_subscriptions
                SET active = 0, revoked_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND book_id = ? AND active = 1
                """,
                (g.telegram_user.id, book_id),
            )
            active = False
        connection.commit()
        return _private_json({"book_id": book_id, "subscribed": active})
    except sqlite3.Error:
        connection.rollback()
        return _private_json({"error": "subscriptions are unavailable"}, 503)
    finally:
        connection.close()

@app.route('/api/checkout/options', methods=['GET'])
@require_telegram_user
def api_checkout_options():
    connection = connect()
    try:
        payment_settings = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT setting_key, setting_value FROM payment_settings"
            ).fetchall()
        }
        delivery = _delivery_capability(payment_settings)
        return make_response(
            jsonify({
                "methods": _payment_options(connection),
                "delivery": delivery,
                "delivery_methods": delivery["methods"],
            }),
            200,
            {"Cache-Control": "private, no-store"},
        )
    finally:
        connection.close()


@app.route('/api/orders', methods=['GET'])
@require_telegram_user
def api_orders():
    """Return the authenticated shopper's order history without payment details."""
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        orders = connection.execute(
            """
            SELECT id, total, status, payment_method, created_at,
                   items_subtotal, delivery_price, promo_code_snapshot, promo_discount, bonus_discount,
                   payment_details_json, manual_details_last_sent_at
            FROM orders
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 50
            """,
            (g.telegram_user.id,),
        ).fetchall()
        order_ids = [row["id"] for row in orders]
        items_by_order: dict[int, list[dict]] = {order_id: [] for order_id in order_ids}
        if order_ids:
            placeholders = ",".join("?" for _ in order_ids)
            items = connection.execute(
                f"""
                SELECT order_id, book_id, title, price, COUNT(*) AS quantity
                FROM order_items
                WHERE order_id IN ({placeholders})
                GROUP BY order_id, book_id, title, price
                ORDER BY order_id DESC, id ASC
                """,
                order_ids,
            ).fetchall()
            for item in items:
                items_by_order[item["order_id"]].append(
                    {
                        "book_id": item["book_id"],
                        "title": item["title"],
                        "price": item["price"],
                        "quantity": item["quantity"],
                        "line_total": item["price"] * item["quantity"],
                    }
                )
        deliveries_by_order: dict[int, dict] = {}
        if order_ids:
            placeholders = ",".join("?" for _ in order_ids)
            deliveries = connection.execute(
                f"""
                SELECT order_id, method, public_instructions_snapshot, delivery_price, shipment_status,
                       tracking_carrier, tracking_number, pii_redacted_at
                FROM order_deliveries
                WHERE order_id IN ({placeholders})
                """,
                order_ids,
            ).fetchall()
            deliveries_by_order = {
                delivery["order_id"]: safe_delivery_summary(delivery)
                for delivery in deliveries
            }
        payload = []
        for order in orders:
            details_available = bool(_order_payment_details(order))
            payload.append(
                {
                    "id": order["id"],
                    "total": order["total"],
                    "status": order["status"],
                    "status_label": STATUS_LABELS.get(order["status"], order["status"]),
                    "payment_method": order["payment_method"],
                    "created_at": order["created_at"],
                    "items": items_by_order[order["id"]],
                    "items_subtotal": order["items_subtotal"] or sum(
                        item["line_total"] for item in items_by_order[order["id"]]
                    ),
                    "delivery_price": order["delivery_price"] or 0,
                    "promo_code_snapshot": order["promo_code_snapshot"],
                    "promo_discount": order["promo_discount"] or 0,
                    "bonus_discount": order["bonus_discount"] or 0,
                    "total_discount": (order["promo_discount"] or 0) + (order["bonus_discount"] or 0),
                    "delivery": deliveries_by_order.get(order["id"]),
                    "can_contact_support": order["status"] not in {"completed", "cancelled"},
                    "can_resend_manual_details": (
                        order["payment_method"] == PAYMENT_METHOD_MANUAL
                        and order["status"] == "awaiting_payment"
                        and details_available
                    ),
                    "can_check_yookassa_payment": (
                        order["payment_method"] == PAYMENT_METHOD_YOOKASSA
                        and order["status"] == "awaiting_yookassa_payment"
                    ),
                }
            )
        response = jsonify({"orders": payload})
        response.headers["Cache-Control"] = "private, no-store"
        return response
    finally:
        connection.close()


@app.route('/api/orders/<int:order_id>', methods=['GET'])
@require_telegram_user
def api_order_detail(order_id: int):
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        order = connection.execute(
            """
            SELECT id, total, status, payment_method, created_at,
                   items_subtotal, delivery_price, promo_code_snapshot, promo_discount, bonus_discount
            FROM orders WHERE id = ? AND user_id = ?
            """,
            (order_id, g.telegram_user.id),
        ).fetchone()
        if not order:
            return _private_json({"error": "Заказ не найден"}, 404)
        items = connection.execute(
            """
            SELECT book_id, title, price, COUNT(*) AS quantity
            FROM order_items WHERE order_id = ?
            GROUP BY book_id, title, price ORDER BY id ASC
            """,
            (order_id,),
        ).fetchall()
        delivery = connection.execute(
            """
            SELECT method, public_instructions_snapshot, delivery_price, shipment_status,
                   tracking_carrier, tracking_number, pii_redacted_at
            FROM order_deliveries WHERE order_id = ?
            """,
            (order_id,),
        ).fetchone()
        item_payload = [
            {
                "book_id": item["book_id"],
                "title": item["title"],
                "price": item["price"],
                "quantity": item["quantity"],
                "line_total": item["price"] * item["quantity"],
            }
            for item in items
        ]
        support_request = connection.execute(
            "SELECT state FROM order_support_requests WHERE order_id = ?", (order_id,)
        ).fetchone()
        payload = {
            "id": order["id"],
            "total": order["total"],
            "status": order["status"],
            "status_label": STATUS_LABELS.get(order["status"], order["status"]),
            "payment_method": order["payment_method"],
            "created_at": order["created_at"],
            "items": item_payload,
            "items_subtotal": order["items_subtotal"] or sum(item["line_total"] for item in item_payload),
            "delivery_price": order["delivery_price"] or 0,
            "promo_code_snapshot": order["promo_code_snapshot"],
            "promo_discount": order["promo_discount"] or 0,
            "bonus_discount": order["bonus_discount"] or 0,
            "total_discount": (order["promo_discount"] or 0) + (order["bonus_discount"] or 0),
            "delivery": safe_delivery_summary(delivery) if delivery else None,
            "can_contact_support": order["status"] not in {"completed", "cancelled"},
            "support_request_state": support_request["state"] if support_request else None,
        }
        return _private_json({"order": payload})
    finally:
        connection.close()

@app.route('/api/orders/<int:order_id>/support', methods=['POST'])
@require_telegram_user
def request_order_support(order_id: int):
    if request.get_data(cache=False):
        return _private_json({"error": "Request body is not supported"}, 400)
    try:
        from db.order_support_requests import enqueue_order_support_request_sync

        result, outcome = enqueue_order_support_request_sync(order_id, g.telegram_user.id)
    except sqlite3.Error:
        return _private_json({"error": "Support is temporarily unavailable"}, 503)
    if outcome == "not_found":
        return _private_json({"error": "Заказ не найден"}, 404)
    if outcome == "terminal":
        return _private_json({"error": "Поддержка по завершённому заказу недоступна"}, 409)
    return _private_json({"accepted": True, **result}, 202)

@app.route('/api/orders/<int:order_id>/manual-details/resend', methods=['POST'])
@require_telegram_user
def resend_manual_order_details(order_id: int):
    connection = connect()
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?",
            (order_id, g.telegram_user.id),
        ).fetchone()
        if (
            not order
            or order["payment_method"] != PAYMENT_METHOD_MANUAL
            or order["status"] != "awaiting_payment"
        ):
            connection.rollback()
            return jsonify({"error": "Реквизиты недоступны"}), 404
        details = _order_payment_details(order)
        if not details:
            connection.rollback()
            return jsonify({"error": "Реквизиты этого заказа недоступны. Обратитесь в поддержку."}), 409
        recently_sent = connection.execute(
            """
            SELECT 1
            WHERE ? IS NOT NULL
              AND ? > datetime('now', ?)
            """,
            (
                order["manual_details_last_sent_at"],
                order["manual_details_last_sent_at"],
                f"-{settings.MANUAL_DETAILS_RESEND_COOLDOWN_SECONDS} seconds",
            ),
        ).fetchone()
        if recently_sent:
            connection.rollback()
            return jsonify({"error": "Реквизиты уже отправлены. Повторите чуть позже."}), 429
        connection.execute(
            "UPDATE orders SET manual_details_last_sent_at = CURRENT_TIMESTAMP WHERE id = ?",
            (order_id,),
        )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        _record_operational_event(
            "critical", "manual_details_resend", "failed", reason="database_error",
            order_id=order_id, error=exc
        )
        return jsonify({"error": "Не удалось отправить реквизиты"}), 503
    finally:
        connection.close()

    result = send_telegram_with_keyboard(
        g.telegram_user.id,
        _manual_payment_text(order_id, order["total"], details),
        [[{"text": "✅ Я оплатил", "callback_data": f"user_paid_{order_id}"}]],
    )
    if not result.get("ok"):
        _record_operational_event(
            "critical", "manual_details_resend", "failed", reason="telegram_delivery_failed",
            order_id=order_id,
        )
        return jsonify({"error": "Не удалось отправить реквизиты в Telegram"}), 503
    _record_operational_event("info", "manual_details_resend", "succeeded", order_id=order_id)
    response = jsonify({"success": True})
    response.headers["Cache-Control"] = "private, no-store"
    return response


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
        _record_operational_event("warning", "yookassa_webhook", "rejected", reason="payload_too_large")
        return jsonify({"error": "Payload too large"}), 413
    if not _is_yookassa_source(request.remote_addr):
        _record_operational_event("warning", "yookassa_webhook", "rejected", reason="untrusted_source")
        return jsonify({"error": "Forbidden"}), 403
    payload = request.get_json(silent=True)
    provider_payment_id = (
        payload.get("object", {}).get("id") if isinstance(payload, dict) and isinstance(payload.get("object"), dict) else None
    )
    if not isinstance(provider_payment_id, str) or not provider_payment_id:
        _record_operational_event("warning", "yookassa_webhook", "rejected", reason="invalid_payload")
        return jsonify({"error": "Invalid payload"}), 400
    if not yookassa_is_configured():
        _record_operational_event("critical", "yookassa_webhook", "failed", reason="provider_unconfigured")
        return jsonify({"error": "Verification unavailable"}), 503
    try:
        payment = _yookassa_find_payment(provider_payment_id)
    except Exception as exc:
        _record_operational_event(
            "critical", "yookassa_webhook", "failed", reason="provider_unavailable", error=exc
        )
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
            commit_order_inventory(connection, order["id"])
            connection.execute(
                """
                UPDATE orders
                SET status = 'paid',
                    paid_at = COALESCE(paid_at, CURRENT_TIMESTAMP),
                    new_order_notified = 0,
                    new_order_notification_state = 'pending',
                    new_order_notification_claimed_at = NULL
                WHERE id = ? AND status = 'awaiting_yookassa_payment'
                """,
                (order["id"],),
            )
        connection.commit()
        _record_operational_event(
            "info",
            "yookassa_webhook",
            "processed",
            reason="verified" if valid_identity else "identity_mismatch",
            order_id=attempt["order_id"],
            attempt_id=attempt["id"],
        )
        return "", 200
    except (sqlite3.Error, InventoryUnavailableError) as exc:
        connection.rollback()
        _record_operational_event(
            "critical",
            "yookassa_webhook",
            "failed",
            reason="database_or_inventory_error",
            error=exc,
        )
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
        delivery_destination = None
        delivery_method = None
        delivery_public_snapshot = ""
        delivery_price = 0
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

        items_total = sum(item['price'] * item['quantity'] for item in cart)

        discount = 0
        discounted_items_total = items_total
        applied_promo = None
        promo_code_to_consume = None

        # 1. Применяем промокод
        if promo_code:
            promo_result = validate_promo_code_sync(promo_code, items_total)
            if promo_result['valid']:
                discount = promo_result['discount']
                discounted_items_total = items_total - discount
                applied_promo = promo_result['promo_code']
                promo_code_to_consume = promo_result['promo_code']
        promo_discount = discount
        bonus_discount = 0

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

        # 2. Получаем актуальные настройки внутри checkout-транзакции.
        cursor.execute("SELECT setting_key, setting_value FROM payment_settings")
        payment_settings = {row['setting_key']: row['setting_value'] for row in cursor.fetchall()}
        cursor.execute("SELECT setting_key, setting_value FROM stars_settings")
        stars_settings = {row['setting_key']: row['setting_value'] for row in cursor.fetchall()}
        delivery = _delivery_capability(payment_settings)
        if delivery["enabled"]:
            if not delivery["available"]:
                conn.rollback()
                conn.close()
                return jsonify({
                    "error": "Доставка временно недоступна. Попробуйте позже.",
                    "code": "delivery_unavailable",
                }), 503
            try:
                methods = _delivery_options(payment_settings)
                (
                    delivery_method,
                    delivery_destination,
                    delivery_public_snapshot,
                ) = _validate_delivery_payload(
                    data.get("delivery"), methods, payment_settings
                )
                delivery_price = methods[delivery_method]["price"]
            except ValueError as exc:
                conn.rollback()
                conn.close()
                return jsonify({'error': str(exc)}), 400

        # 3. Применяем бонусы пользователя только после проверки доставки.
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
                bonus_discount = int(discounted_items_total * bonus['amount'] / 100)
            else:
                bonus_discount = bonus['amount']

            bonus_discount = min(bonus_discount, discounted_items_total)
            discount += bonus_discount
            discounted_items_total -= bonus_discount
            applied_bonus = f"{bonus['amount']}{'%' if bonus['bonus_type'] == 'percent' else '₽'}"

            cursor.execute("UPDATE user_bonuses SET is_used = 1 WHERE id = ?", (bonus['id'],))

        final_total = discounted_items_total + delivery_price

        # Покупатель выбирает способ, но сервер разрешает только включённые методы.
        options = {
            option["id"]
            for option in _payment_options(conn)
        }
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

        _ensure_authenticated_user(conn)
        acquisition = cursor.execute(
            """
            SELECT channel, source, campaign, referrer_user_id
            FROM user_acquisition WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        acquisition = dict(acquisition) if acquisition else {
            "channel": "unknown",
            "source": "unknown",
            "campaign": "unknown",
            "referrer_user_id": None,
        }

        manual_payment_details = (
            _manual_payment_details(payment_settings)
            if payment_method == PAYMENT_METHOD_MANUAL
            else {}
        )
        order_id = create_order(
            user_id,
            user_name,
            cart,
            final_total,
            status,
            connection=conn,
            payment_method=payment_method,
            payment_details_json=json.dumps(manual_payment_details, separators=(",", ":")),
            checkout_key=checkout_key,
            items_subtotal=items_total,
            delivery_price=delivery_price,
            promo_code_snapshot=applied_promo,
            promo_discount=promo_discount,
            bonus_discount=bonus_discount,
            acquisition_channel=acquisition["channel"],
            acquisition_source=acquisition["source"],
            acquisition_campaign=acquisition["campaign"],
            acquisition_referrer_id=acquisition["referrer_user_id"],
        )
        if applied_promo and promo_discount > 0:
            cursor.execute(
                """
                INSERT INTO order_promo_redemptions (order_id, promo_code_snapshot, discount_amount)
                VALUES (?, ?, ?)
                """,
                (order_id, applied_promo, promo_discount),
            )
        if delivery_destination is not None:
            try:
                _insert_delivery(
                    conn,
                    order_id,
                    delivery_method,
                    delivery_destination,
                    delivery_price,
                    delivery_public_snapshot,
                )
            except DeliveryCryptoError as exc:
                conn.rollback()
                conn.close()
                _record_operational_event(
                    "critical", "delivery_checkout", "failed", reason="encryption_failed",
                    order_id=order_id, error=exc
                )
                return jsonify({'error': 'Доставка временно недоступна. Попробуйте позже.'}), 503
        if payment_method == PAYMENT_METHOD_MANUAL:
            cursor.execute(
                "UPDATE orders SET manual_details_last_sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                (order_id,),
            )
        try:
            reserve_order_inventory(conn, order_id, cart)
        except InventoryUnavailableError:
            conn.rollback()
            conn.close()
            return jsonify({'error': 'book is unavailable'}), 409
        if payment_method == PAYMENT_METHOD_YOOKASSA:
            cursor.execute(
                """
                INSERT INTO yookassa_payments (order_id, idempotence_key, amount, currency)
                VALUES (?, ?, ?, 'RUB')
                """,
                (order_id, str(uuid.uuid4()), _money_rub(final_total)),
            )
        delivery_summary = _delivery_summary_for_order(conn, order_id)
        cart_revision = _clear_cart(conn, user_id)
        conn.commit()
        conn.close()
        _record_operational_event(
            "info",
            "checkout",
            "succeeded",
            order_id=order_id,
        )

        # Формируем ответ
        response_data = {
            'success': True,
            'order_id': order_id,
            'discount': discount,
            'items_total': items_total,
            'delivery_price': delivery_price,
            'final_total': final_total,
            'delivery': delivery_summary,
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

        if payment_method == PAYMENT_METHOD_MANUAL:
            response_data['payment_info'] = manual_payment_details

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
        items_list = "\n".join([f"• {item['title']}{f' ×{item.get('quantity', 1)}' if item.get('quantity', 1) > 1 else ''} — {item['price'] * item.get('quantity', 1)} ₽" for item in cart])
        receipt_adjustments = ""
        if applied_promo:
            receipt_adjustments += f"\nПромокод: {applied_promo}"
        if promo_discount:
            receipt_adjustments += f"\nСкидка промокода: −{promo_discount} ₽"
        if bonus_discount:
            receipt_adjustments += f"\nСкидка бонуса: −{bonus_discount} ₽"
        if discount:
            receipt_adjustments += f"\nОбщая скидка: −{discount} ₽"

        if payment_method == PAYMENT_METHOD_STARS:
            # === ОТПРАВКА ИНВОЙСА STARS ===
            invoice_title = f"Заказ #{order_id} — Семена Знаний"
            invoice_desc = f"📚 {len(cart)} книг\n\n{items_list}{receipt_adjustments}\n\n💰 {final_total} ₽"

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
            payment_text = (
                _manual_payment_text(order_id, final_total, manual_payment_details)
                + f"\n\n{items_list}{receipt_adjustments}\n\nИтого: {final_total} ₽"
            )
            buttons = [[{'text': '✅ Я оплатил', 'callback_data': f'user_paid_{order_id}'}]]
            send_telegram_with_keyboard(user_id, payment_text, buttons)

        elif payment_method == PAYMENT_METHOD_NONE:
            # Заказ без онлайн-оплаты.
            send_telegram_message(
                user_id,
                f"🛒 <b>Заказ #{order_id} создан!</b>\n\n{items_list}{receipt_adjustments}\n\n"
                f"💰 Итого: <b>{final_total} ₽</b>\n\n"
                f"Мы свяжемся с вами для доставки 🌿"
            )

        # === УВЕДОМЛЕНИЕ АДМИНАМ ===
        # Карточку с кнопками «Принять / Отклонить» отправляет сам бот:
        # фоновый поллер в handlers/admin_orders.py подхватывает заказ из БД
        # (статусы из PENDING_STATUSES) и шлёт уведомление автоматически.
        # Здесь шлём только подтверждение пользователю.

        return jsonify(response_data)

    except Exception as exc:
        _record_operational_event(
            "critical",
            "checkout",
            "failed",
            reason="unexpected_error",
            error=exc,
        )
        return jsonify({'error': 'Не удалось оформить заказ'}), 500


@app.route('/health', methods=['GET'])
def health():
    """Проверка работоспособности"""
    return jsonify({'status': 'ok'})


# ============================================
# ЗАПУСК
# ============================================

def _should_suppress_loopback_success_access_log(address: str, code: object) -> bool:
    if not settings.SUPPRESS_LOOPBACK_SUCCESS_ACCESS_LOGS:
        return False
    try:
        return ipaddress.ip_address(address).is_loopback and 200 <= int(code) < 400
    except (TypeError, ValueError):
        return False


class _LoopbackSuccessQuietRequestHandler(WSGIRequestHandler):
    def log_request(self, code: object = "-", size: object = "-") -> None:
        if not _should_suppress_loopback_success_access_log(self.client_address[0], code):
            super().log_request(code, size)


def run_server():
    initialize_database()
    options = {
        "host": settings.HOST,
        "port": settings.PORT,
        "debug": settings.FLASK_DEBUG,
        "use_reloader": settings.FLASK_DEBUG,
    }
    if settings.SUPPRESS_LOOPBACK_SUCCESS_ACCESS_LOGS:
        options["request_handler"] = _LoopbackSuccessQuietRequestHandler
    app.run(**options)


if __name__ == '__main__':
    print(f"Mini App server: http://{settings.HOST}:{settings.PORT}")
    run_server()
