from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from config import settings
from content_defaults import TEMPLATES


SCHEMA_VERSION = 4
_CONNECTION_TIMEOUT_SECONDS = 10
_INITIALIZATION_LOCK = threading.Lock()


def _resolve_database_path(value: str | Path | None) -> Path:
    if not value:
        raise RuntimeError("DATABASE_PATH must point to the shared SQLite database.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError("DATABASE_PATH must be an absolute path.")
    return path.resolve()


DB_PATH = _resolve_database_path(settings.DATABASE_PATH)


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {_CONNECTION_TIMEOUT_SECONDS * 1000}")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    resolved_path = _resolve_database_path(path) if path is not None else DB_PATH
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(resolved_path), timeout=_CONNECTION_TIMEOUT_SECONDS)
    configure_connection(connection)
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    if column not in _columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _create_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            user_name TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_support_active INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL DEFAULT '',
            total INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            admin_notification_ids TEXT DEFAULT '[]',
            new_order_notified INTEGER NOT NULL DEFAULT 0,
            payment_method TEXT NOT NULL DEFAULT 'manual',
            checkout_key TEXT
        );
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            price INTEGER NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        );
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price INTEGER NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            category_id INTEGER DEFAULT 0,
            author TEXT DEFAULT '',
            description TEXT DEFAULT '',
            cover_photo TEXT DEFAULT '',
            images TEXT DEFAULT '[]',
            emoji TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            is_archived INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            emoji TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0
        );
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
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL UNIQUE,
            bonus_given INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_bonuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bonus_type TEXT NOT NULL,
            amount INTEGER NOT NULL,
            is_used INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS payment_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_key TEXT NOT NULL UNIQUE,
            setting_value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stars_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_key TEXT NOT NULL UNIQUE,
            setting_value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS yookassa_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            idempotence_key TEXT NOT NULL UNIQUE,
            provider_payment_id TEXT UNIQUE,
            amount TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'RUB',
            status TEXT NOT NULL DEFAULT 'created',
            confirmation_url TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        );
        CREATE TABLE IF NOT EXISTS message_templates (
            template_key TEXT PRIMARY KEY,
            template_value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS fsm_records (
            fsm_key TEXT PRIMARY KEY,
            state TEXT,
            data TEXT
        );
        CREATE TABLE IF NOT EXISTS mini_app_carts (
            user_id INTEGER PRIMARY KEY,
            cart_json TEXT NOT NULL DEFAULT '[]',
            revision INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_order_items_book_id
        ON order_items (book_id, order_id);
        CREATE INDEX IF NOT EXISTS idx_yookassa_payments_order_id
        ON yookassa_payments (order_id);
        """
    )


def _migrate_columns(connection: sqlite3.Connection) -> None:
    columns = {
        "users": {
            "user_name": "user_name TEXT NOT NULL DEFAULT ''",
            "created_at": "created_at TIMESTAMP",
            "is_support_active": "is_support_active INTEGER NOT NULL DEFAULT 0",
        },
        "orders": {
            "user_name": "user_name TEXT NOT NULL DEFAULT ''",
            "status": "status TEXT NOT NULL DEFAULT 'new'",
            "created_at": "created_at TIMESTAMP",
            "admin_notification_ids": "admin_notification_ids TEXT DEFAULT '[]'",
            "new_order_notified": "new_order_notified INTEGER NOT NULL DEFAULT 0",
            "payment_method": "payment_method TEXT NOT NULL DEFAULT 'manual'",
            "checkout_key": "checkout_key TEXT",
        },
        "books": {
            "category_id": "category_id INTEGER",
            "author": "author TEXT DEFAULT ''",
            "description": "description TEXT DEFAULT ''",
            "cover_photo": "cover_photo TEXT DEFAULT ''",
            "images": "images TEXT DEFAULT '[]'",
            "emoji": "emoji TEXT DEFAULT ''",
            "sort_order": "sort_order INTEGER DEFAULT 0",
            "is_active": "is_active INTEGER DEFAULT 1",
            "is_archived": "is_archived INTEGER DEFAULT 0",
            "created_at": "created_at TIMESTAMP",
        },
        "categories": {
            "emoji": "emoji TEXT DEFAULT ''",
            "is_active": "is_active INTEGER DEFAULT 1",
            "sort_order": "sort_order INTEGER DEFAULT 0",
        },
    }
    for table, definitions in columns.items():
        for column, definition in definitions.items():
            _ensure_column(connection, table, column, definition)
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_user_checkout_key "
        "ON orders (user_id, checkout_key) WHERE checkout_key IS NOT NULL"
    )
    connection.execute(
        "UPDATE orders SET payment_method = 'manual' "
        "WHERE payment_method IS NULL OR payment_method = ''"
    )


def _seed_categories(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0]:
        return
    connection.executemany(
        "INSERT INTO categories (name, emoji, sort_order) VALUES (?, ?, ?)",
        [
            ("Ботаника", "🌿", 1),
            ("Природа", "🌳", 2),
            ("Искусство", "🎨", 3),
            ("Садоводство", "🌱", 4),
            ("Травник", "🌾", 5),
            ("Флористика", "🌸", 6),
        ],
    )


def _seed_books(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT COUNT(*) FROM books").fetchone()[0]:
        return
    category_ids = {
        name: category_id
        for category_id, name in connection.execute("SELECT id, name FROM categories")
    }
    connection.executemany(
        """
        INSERT INTO books (title, price, category, emoji, description, images, category_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("Атлас лекарственных растений", 1200, "Ботаника", "", "Подробный атлас с описанием более 500 лекарственных растений.", '["https://images.unsplash.com/photo-1544947950-fa07a98d237f?w=400"]', category_ids.get("Ботаника")),
            ("Тайная жизнь деревьев", 850, "Природа", "🌳", "Увлекательное исследование лесных экосистем.", '["https://images.unsplash.com/photo-1448375240586-882707db888b?w=400"]', category_ids.get("Природа")),
            ("Ботанические иллюстрации", 2500, "Искусство", "🎨", "Роскошный альбом с акварельными иллюстрациями.", '["https://images.unsplash.com/photo-1490750967868-88aa4486c946?w=400"]', category_ids.get("Искусство")),
            ("Сад на подоконнике", 650, "Садоводство", "", "Практическое руководство по выращиванию растений.", '["https://images.unsplash.com/photo-1416879595882-3373a0480b5b?w=400"]', category_ids.get("Садоводство")),
            ("Энциклопедия трав", 1800, "Травник", "🌾", "Полная энциклопедия лекарственных трав.", '["https://images.unsplash.com/photo-1466692476868-aef1dfb1e735?w=400"]', category_ids.get("Травник")),
            ("Цветы мира", 1500, "Флористика", "🌸", "Красочный путеводитель по цветам.", '["https://images.unsplash.com/photo-1490750967868-88aa4486c946?w=400"]', category_ids.get("Флористика")),
        ],
    )


def _seed_settings(connection: sqlite3.Connection) -> None:
    connection.executemany(
        "INSERT OR IGNORE INTO payment_settings (setting_key, setting_value) VALUES (?, ?)",
        [
            ("payment_enabled", "1"),
            ("card_number", ""),
            ("sbp_phone", ""),
            ("sbp_bank", ""),
            ("recipient_name", ""),
            ("payment_instructions", "После перевода укажите номер заказа в комментарии"),
            ("yookassa_enabled", "0"),
        ],
    )
    connection.executemany(
        "INSERT OR IGNORE INTO stars_settings (setting_key, setting_value) VALUES (?, ?)",
        [("stars_enabled", "0"), ("rubles_per_star", "2")],
    )
    connection.executemany(
        "INSERT OR IGNORE INTO message_templates (template_key, template_value) VALUES (?, ?)",
        [(template.key, template.default) for template in TEMPLATES],
    )


def _backfill_books(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE books SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
    )
    connection.execute(
        """
        UPDATE books
        SET category_id = (
            SELECT id FROM categories WHERE categories.name = books.category LIMIT 1
        )
        WHERE (category_id IS NULL OR category_id = 0)
          AND EXISTS (SELECT 1 FROM categories WHERE categories.name = books.category)
        """
    )


def initialize_database(path: str | Path | None = None) -> None:
    resolved_path = _resolve_database_path(path) if path is not None else DB_PATH
    with _INITIALIZATION_LOCK:
        connection = connect(resolved_path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            _create_tables(connection)
            _migrate_columns(connection)
            _seed_categories(connection)
            _backfill_books(connection)
            _seed_books(connection)
            _seed_settings(connection)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
