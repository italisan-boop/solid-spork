from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from config import settings
from content_defaults import TEMPLATES


SCHEMA_VERSION = 20
_CONNECTION_TIMEOUT_SECONDS = 10
_INITIALIZATION_LOCK = threading.Lock()
_CURRENT_DATABASE_PATH: ContextVar[Path | None] = ContextVar(
    "current_database_path", default=None
)


def _resolve_database_path(value: str | Path | None) -> Path:
    if not value:
        raise RuntimeError("DATABASE_PATH must point to the shared SQLite database.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError("DATABASE_PATH must be an absolute path.")
    return path.resolve()


DB_PATH: Path | None = (
    _resolve_database_path(settings.DATABASE_PATH)
    if settings.DATABASE_PATH
    else None
)


def current_database_path() -> Path:
    contextual_path = _CURRENT_DATABASE_PATH.get()
    return contextual_path if contextual_path is not None else _resolve_database_path(DB_PATH)


@contextmanager
def database_context(path: str | Path):
    token = _CURRENT_DATABASE_PATH.set(_resolve_database_path(path))
    try:
        yield current_database_path()
    finally:
        _CURRENT_DATABASE_PATH.reset(token)


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {_CONNECTION_TIMEOUT_SECONDS * 1000}")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    resolved_path = _resolve_database_path(path) if path is not None else current_database_path()
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
        CREATE TABLE IF NOT EXISTS support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
            sender_name TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            new_order_notification_state TEXT NOT NULL DEFAULT 'pending'
                CHECK (new_order_notification_state IN ('pending', 'processing', 'sent')),
            new_order_notification_claimed_at TIMESTAMP,
            payment_method TEXT NOT NULL DEFAULT 'manual',
            payment_details_json TEXT NOT NULL DEFAULT '',
            manual_details_last_sent_at TIMESTAMP,
            paid_at TIMESTAMP,
            checkout_key TEXT,
            items_subtotal INTEGER NOT NULL DEFAULT 0,
            delivery_price INTEGER NOT NULL DEFAULT 0,
            promo_code_snapshot TEXT,
            promo_discount INTEGER NOT NULL DEFAULT 0,
            bonus_discount INTEGER NOT NULL DEFAULT 0,
            acquisition_channel TEXT NOT NULL DEFAULT 'unknown',
            acquisition_source TEXT NOT NULL DEFAULT 'unknown',
            acquisition_campaign TEXT NOT NULL DEFAULT 'unknown',
            acquisition_referrer_id INTEGER
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
            stock_quantity INTEGER CHECK (stock_quantity IS NULL OR stock_quantity >= 0),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS order_deliveries (
            order_id INTEGER PRIMARY KEY,
            method TEXT NOT NULL CHECK (method IN (
                'sdek_pickup', 'russian_post_pickup', 'self_pickup'
            )),
            destination_encrypted TEXT NOT NULL,
            public_instructions_snapshot TEXT NOT NULL DEFAULT '',
            delivery_price INTEGER NOT NULL CHECK (delivery_price >= 0),
            shipment_status TEXT NOT NULL DEFAULT 'awaiting_payment'
                CHECK (shipment_status IN (
                    'awaiting_payment', 'preparing', 'packed', 'shipped',
                    'ready_for_pickup', 'delivered', 'returned', 'cancelled'
                )),
            tracking_carrier TEXT NOT NULL DEFAULT 'none'
                CHECK (tracking_carrier IN ('none', 'sdek', 'russian_post')),
            tracking_number TEXT,
            tracking_set_at TIMESTAMP,
            delivered_at TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_by_admin_id INTEGER,
            pii_redacted_at TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders (id),
            CHECK (
                (method = 'sdek_pickup' AND (
                    (tracking_carrier = 'none' AND tracking_number IS NULL)
                    OR (tracking_carrier = 'sdek' AND tracking_number IS NOT NULL)
                ))
                OR (method = 'russian_post_pickup' AND (
                    (tracking_carrier = 'none' AND tracking_number IS NULL)
                    OR (tracking_carrier = 'russian_post' AND tracking_number IS NOT NULL)
                ))
                OR (method = 'self_pickup' AND tracking_carrier = 'none' AND tracking_number IS NULL)
            )
        );
        CREATE TABLE IF NOT EXISTS inventory_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            state TEXT NOT NULL DEFAULT 'reserved'
                CHECK (state IN ('reserved', 'committed', 'released')),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (order_id, book_id),
            FOREIGN KEY (order_id) REFERENCES orders (id)
        );
        CREATE TABLE IF NOT EXISTS book_media (
            asset_id TEXT PRIMARY KEY,
            book_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('cover', 'page')),
            position INTEGER NOT NULL DEFAULT 0 CHECK (position >= 0),
            thumbnail_filename TEXT NOT NULL,
            display_filename TEXT NOT NULL,
            mime_type TEXT NOT NULL DEFAULT 'image/webp',
            width INTEGER NOT NULL CHECK (width > 0),
            height INTEGER NOT NULL CHECK (height > 0),
            content_hash TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (book_id, role, position),
            FOREIGN KEY (book_id) REFERENCES books (id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS inventory_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            order_id INTEGER,
            action TEXT NOT NULL CHECK (action IN (
                'opening_balance', 'manual_adjustment', 'stock_mode_changed',
                'reservation_created',
                'reservation_released', 'sale_committed', 'sale_reversed'
            )),
            stock_delta INTEGER NOT NULL DEFAULT 0,
            reserved_delta INTEGER NOT NULL DEFAULT 0,
            stock_after INTEGER,
            reserved_after INTEGER NOT NULL DEFAULT 0,
            actor_admin_id INTEGER,
            reason TEXT NOT NULL DEFAULT '',
            source_key TEXT UNIQUE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                stock_delta != 0 OR reserved_delta != 0 OR action = 'stock_mode_changed'
            ),
            FOREIGN KEY (book_id) REFERENCES books (id) ON DELETE RESTRICT,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        );
        CREATE TABLE IF NOT EXISTS inventory_low_stock_state (
            book_id INTEGER PRIMARY KEY,
            threshold INTEGER NOT NULL DEFAULT 3 CHECK (threshold >= 0),
            is_low INTEGER NOT NULL DEFAULT 0 CHECK (is_low IN (0, 1)),
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (book_id) REFERENCES books (id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK (kind IN ('low_stock', 'back_in_stock')),
            dedupe_key TEXT NOT NULL UNIQUE,
            user_id INTEGER,
            book_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'processing', 'sent', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            claimed_at TIMESTAMP,
            sent_at TIMESTAMP,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (book_id) REFERENCES books (id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS user_favorites (
            user_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, book_id),
            FOREIGN KEY (book_id) REFERENCES books (id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS back_in_stock_subscriptions (
            user_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            consented_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            consent_version TEXT NOT NULL DEFAULT 'v1',
            revoked_at TIMESTAMP,
            notified_at TIMESTAMP,
            PRIMARY KEY (user_id, book_id),
            FOREIGN KEY (book_id) REFERENCES books (id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS acquisition_campaigns (
            code TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            campaign TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_acquisition (
            user_id INTEGER PRIMARY KEY,
            channel TEXT NOT NULL DEFAULT 'unknown',
            source TEXT NOT NULL DEFAULT 'unknown',
            campaign TEXT NOT NULL DEFAULT 'unknown',
            referrer_user_id INTEGER,
            acquired_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS order_promo_redemptions (
            order_id INTEGER PRIMARY KEY,
            promo_code_snapshot TEXT NOT NULL,
            discount_amount INTEGER NOT NULL CHECK (discount_amount > 0),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        );
        CREATE TABLE IF NOT EXISTS order_support_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            receipt_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'processing', 'sent', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            claimed_at TIMESTAMP,
            sent_at TIMESTAMP,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        );
        CREATE TABLE IF NOT EXISTS operational_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
            component TEXT NOT NULL,
            event TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            order_id INTEGER,
            attempt_id INTEGER,
            error_type TEXT NOT NULL DEFAULT '',
            alert_state TEXT NOT NULL DEFAULT 'pending'
                CHECK (alert_state IN ('pending', 'processing', 'sent', 'suppressed')),
            alert_claimed_at TIMESTAMP,
            alert_sent_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS book_import_batches (
            id TEXT PRIMARY KEY,
            actor_user_id INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            source_format TEXT NOT NULL CHECK (source_format IN ('csv', 'xlsx')),
            state TEXT NOT NULL DEFAULT 'previewed'
                CHECK (state IN ('previewed', 'committed', 'expired')),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            committed_at TIMESTAMP,
            report_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS book_import_rows (
            batch_id TEXT NOT NULL,
            line_number INTEGER NOT NULL,
            row_json TEXT NOT NULL,
            errors_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (batch_id, line_number),
            FOREIGN KEY (batch_id) REFERENCES book_import_batches (id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_book_import_batches_actor_state
        ON book_import_batches (actor_user_id, state, expires_at);
        CREATE TABLE IF NOT EXISTS maintenance_leases (
            lease_key TEXT PRIMARY KEY,
            claimed_at TIMESTAMP NOT NULL,
            owner_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS order_fulfillments (
            order_id INTEGER PRIMARY KEY,
            warehouse_user_id INTEGER,
            state TEXT NOT NULL DEFAULT 'ready'
                CHECK (state IN ('ready', 'claimed', 'blocked', 'packed')),
            version INTEGER NOT NULL DEFAULT 0,
            claimed_at TIMESTAMP,
            packed_at TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        );
        CREATE TABLE IF NOT EXISTS order_packing_lines (
            order_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            price INTEGER NOT NULL,
            ordered_quantity INTEGER NOT NULL CHECK (ordered_quantity > 0),
            picked_quantity INTEGER NOT NULL DEFAULT 0
                CHECK (picked_quantity >= 0 AND picked_quantity <= ordered_quantity),
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (order_id, book_id, title, price),
            FOREIGN KEY (order_id) REFERENCES orders (id)
        );
        CREATE INDEX IF NOT EXISTS idx_order_fulfillments_queue
        ON order_fulfillments (state, warehouse_user_id, updated_at);
        CREATE TABLE IF NOT EXISTS staff_members (
            telegram_user_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL CHECK (role IN ('manager', 'warehouse')),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            changed_by_user_id INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER,
            actor_role TEXT NOT NULL CHECK (actor_role IN ('owner', 'manager', 'warehouse', 'system')),
            source TEXT NOT NULL CHECK (source IN ('telegram', 'mini_app', 'webhook', 'scheduler', 'system')),
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL DEFAULT '',
            correlation_id TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}',
            outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'rejected', 'failed')),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS staff_alert_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            recipient_user_id INTEGER NOT NULL,
            recipient_role TEXT NOT NULL CHECK (recipient_role IN ('owner', 'manager', 'warehouse')),
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'processing', 'sent', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            claimed_at TIMESTAMP,
            sent_at TIMESTAMP,
            last_error TEXT NOT NULL DEFAULT '',
            UNIQUE (event_id, recipient_user_id),
            FOREIGN KEY (event_id) REFERENCES operational_events (id)
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
        CREATE TABLE IF NOT EXISTS tenant_runtime_metadata (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            tenant_id TEXT NOT NULL UNIQUE,
            canonical_host TEXT NOT NULL,
            owner_telegram_id INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tenant_owner_claims (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            telegram_user_id INTEGER NOT NULL,
            claimed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tenant_daily_usage (
            usage_day TEXT NOT NULL,
            limit_name TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity >= 0),
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (usage_day, limit_name)
        );
        CREATE TABLE IF NOT EXISTS storefront_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            store_name TEXT NOT NULL DEFAULT '',
            primary_color TEXT NOT NULL DEFAULT '#2f7d4a',
            accent_color TEXT NOT NULL DEFAULT '#f2b84b',
            logo_asset_id TEXT,
            support_contact TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_order_support_requests_lease
        ON order_support_requests (state, claimed_at, id);
        CREATE INDEX IF NOT EXISTS idx_order_items_book_id
        ON order_items (book_id, order_id);
        CREATE INDEX IF NOT EXISTS idx_support_messages_user_id_id
        ON support_messages (user_id, id);
        CREATE INDEX IF NOT EXISTS idx_yookassa_payments_order_id
        ON yookassa_payments (order_id);
        CREATE INDEX IF NOT EXISTS idx_order_deliveries_status_updated
        ON order_deliveries (shipment_status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_book_media_book_role
        ON book_media (book_id, role, position);
        CREATE INDEX IF NOT EXISTS idx_inventory_movements_book_id
        ON inventory_movements (book_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_inventory_movements_order_id
        ON inventory_movements (order_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_notification_outbox_state
        ON notification_outbox (state, claimed_at, id);
        CREATE INDEX IF NOT EXISTS idx_user_favorites_created
        ON user_favorites (user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_back_in_stock_active
        ON back_in_stock_subscriptions (book_id, active, user_id);
        CREATE INDEX IF NOT EXISTS idx_staff_members_role_active
        ON staff_members (role, is_active, telegram_user_id);
        CREATE INDEX IF NOT EXISTS idx_audit_events_created
        ON audit_events (created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_events_entity
        ON audit_events (entity_type, entity_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_staff_alert_deliveries_lease
        ON staff_alert_deliveries (state, claimed_at, id);
        CREATE TRIGGER IF NOT EXISTS audit_events_no_update
        BEFORE UPDATE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit events are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
        BEFORE DELETE ON audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit events are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS inventory_movements_no_update
        BEFORE UPDATE ON inventory_movements
        BEGIN
            SELECT RAISE(ABORT, 'inventory movements are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS inventory_movements_no_delete
        BEFORE DELETE ON inventory_movements
        BEGIN
            SELECT RAISE(ABORT, 'inventory movements are immutable');
        END;
        """
    )


def _migrate_order_deliveries(connection: sqlite3.Connection) -> None:
    columns = _columns(connection, "order_deliveries")
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'order_deliveries'"
    ).fetchone()[0]
    if "public_instructions_snapshot" in columns and "russian_post_pickup" in table_sql:
        return
    connection.executescript(
        """
        CREATE TABLE order_deliveries_new (
            order_id INTEGER PRIMARY KEY,
            method TEXT NOT NULL CHECK (method IN (
                'sdek_pickup', 'russian_post_pickup', 'self_pickup'
            )),
            destination_encrypted TEXT NOT NULL,
            public_instructions_snapshot TEXT NOT NULL DEFAULT '',
            delivery_price INTEGER NOT NULL CHECK (delivery_price >= 0),
            shipment_status TEXT NOT NULL DEFAULT 'awaiting_payment'
                CHECK (shipment_status IN (
                    'awaiting_payment', 'preparing', 'packed', 'shipped',
                    'ready_for_pickup', 'delivered', 'returned', 'cancelled'
                )),
            tracking_carrier TEXT NOT NULL DEFAULT 'none'
                CHECK (tracking_carrier IN ('none', 'sdek', 'russian_post')),
            tracking_number TEXT,
            tracking_set_at TIMESTAMP,
            delivered_at TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_by_admin_id INTEGER,
            pii_redacted_at TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders (id),
            CHECK (
                (method = 'sdek_pickup' AND (
                    (tracking_carrier = 'none' AND tracking_number IS NULL)
                    OR (tracking_carrier = 'sdek' AND tracking_number IS NOT NULL)
                ))
                OR (method = 'russian_post_pickup' AND (
                    (tracking_carrier = 'none' AND tracking_number IS NULL)
                    OR (tracking_carrier = 'russian_post' AND tracking_number IS NOT NULL)
                ))
                OR (method = 'self_pickup' AND tracking_carrier = 'none' AND tracking_number IS NULL)
            )
        );
        """
    )
    connection.execute(
        """
        INSERT INTO order_deliveries_new (
            order_id, method, destination_encrypted, delivery_price, shipment_status,
            tracking_carrier, tracking_number, tracking_set_at, delivered_at,
            updated_at, updated_by_admin_id, pii_redacted_at
        )
        SELECT
            order_id, method, destination_encrypted, delivery_price, shipment_status,
            tracking_carrier, tracking_number, tracking_set_at, delivered_at,
            updated_at, updated_by_admin_id, pii_redacted_at
        FROM order_deliveries
        """
    )
    connection.execute("DROP TABLE order_deliveries")
    connection.execute("ALTER TABLE order_deliveries_new RENAME TO order_deliveries")


def _migrate_order_support_requests(connection: sqlite3.Connection) -> None:
    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'order_support_requests'"
    ).fetchone()
    if not table_sql_row or "'failed'" in table_sql_row[0].lower():
        return
    connection.executescript(
        """
        CREATE TABLE order_support_requests_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            receipt_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'processing', 'sent', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            claimed_at TIMESTAMP,
            sent_at TIMESTAMP,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        );
        INSERT INTO order_support_requests_new (
            id, order_id, user_id, receipt_json, state, attempts, claimed_at,
            sent_at, last_error, created_at
        )
        SELECT id, order_id, user_id, receipt_json, state, attempts, claimed_at,
               sent_at, last_error, created_at
        FROM order_support_requests;
        DROP TABLE order_support_requests;
        ALTER TABLE order_support_requests_new RENAME TO order_support_requests;
        """
    )


def _migrate_inventory_movements(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'inventory_movements'"
    ).fetchone()
    if row is None:
        return
    table_sql = row[0].lower()
    if "stock_mode_changed" in table_sql and "action = 'stock_mode_changed'" in table_sql:
        return
    connection.execute(
        """
        CREATE TABLE inventory_movements_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            order_id INTEGER,
            action TEXT NOT NULL CHECK (action IN (
                'opening_balance', 'manual_adjustment', 'stock_mode_changed',
                'reservation_created', 'reservation_released', 'sale_committed',
                'sale_reversed'
            )),
            stock_delta INTEGER NOT NULL DEFAULT 0,
            reserved_delta INTEGER NOT NULL DEFAULT 0,
            stock_after INTEGER,
            reserved_after INTEGER NOT NULL DEFAULT 0,
            actor_admin_id INTEGER,
            reason TEXT NOT NULL DEFAULT '',
            source_key TEXT UNIQUE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                stock_delta != 0 OR reserved_delta != 0 OR action = 'stock_mode_changed'
            ),
            FOREIGN KEY (book_id) REFERENCES books (id) ON DELETE RESTRICT,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO inventory_movements_new (
            id, book_id, order_id, action, stock_delta, reserved_delta,
            stock_after, reserved_after, actor_admin_id, reason, source_key,
            created_at
        )
        SELECT
            id, book_id, order_id, action, stock_delta, reserved_delta,
            stock_after, reserved_after, actor_admin_id, reason, source_key,
            created_at
        FROM inventory_movements
        """
    )
    connection.execute("DROP TABLE inventory_movements")
    connection.execute("ALTER TABLE inventory_movements_new RENAME TO inventory_movements")
    connection.execute(
        "CREATE INDEX idx_inventory_movements_book_id "
        "ON inventory_movements (book_id, id DESC)"
    )
    connection.execute(
        "CREATE INDEX idx_inventory_movements_order_id "
        "ON inventory_movements (order_id, id DESC)"
    )
    connection.execute(
        """
        CREATE TRIGGER inventory_movements_no_update
        BEFORE UPDATE ON inventory_movements
        BEGIN
            SELECT RAISE(ABORT, 'inventory movements are immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER inventory_movements_no_delete
        BEFORE DELETE ON inventory_movements
        BEGIN
            SELECT RAISE(ABORT, 'inventory movements are immutable');
        END
        """
    )


def _cleanup_expired_support_messages(connection: sqlite3.Connection) -> None:
    connection.execute(
        "DELETE FROM support_messages WHERE date(created_at) < date('now', '-90 days')"
    )


def _cleanup_expired_delivery_pii(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE order_deliveries
        SET destination_encrypted = '', pii_redacted_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE shipment_status = 'delivered'
          AND delivered_at IS NOT NULL
          AND date(delivered_at) < date('now', ?)
          AND pii_redacted_at IS NULL
        """,
        (f"-{settings.DELIVERY_PII_RETENTION_DAYS} days",),
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
            "new_order_notification_state": "new_order_notification_state TEXT NOT NULL DEFAULT 'pending'",
            "new_order_notification_claimed_at": "new_order_notification_claimed_at TIMESTAMP",
            "payment_method": "payment_method TEXT NOT NULL DEFAULT 'manual'",
            "payment_details_json": "payment_details_json TEXT NOT NULL DEFAULT ''",
            "manual_details_last_sent_at": "manual_details_last_sent_at TIMESTAMP",
            "paid_at": "paid_at TIMESTAMP",
            "checkout_key": "checkout_key TEXT",
            "items_subtotal": "items_subtotal INTEGER NOT NULL DEFAULT 0",
            "delivery_price": "delivery_price INTEGER NOT NULL DEFAULT 0",
            "promo_code_snapshot": "promo_code_snapshot TEXT",
            "promo_discount": "promo_discount INTEGER NOT NULL DEFAULT 0",
            "bonus_discount": "bonus_discount INTEGER NOT NULL DEFAULT 0",
            "acquisition_channel": "acquisition_channel TEXT NOT NULL DEFAULT 'unknown'",
            "acquisition_source": "acquisition_source TEXT NOT NULL DEFAULT 'unknown'",
            "acquisition_campaign": "acquisition_campaign TEXT NOT NULL DEFAULT 'unknown'",
            "acquisition_referrer_id": "acquisition_referrer_id INTEGER",
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
            "stock_quantity": "stock_quantity INTEGER",
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
    connection.execute(
        """
        UPDATE orders
        SET new_order_notification_state = CASE
            WHEN new_order_notified = 1 THEN 'sent'
            ELSE 'pending'
        END
        WHERE (new_order_notified = 1 AND new_order_notification_state != 'sent')
           OR new_order_notification_state IS NULL
           OR new_order_notification_state NOT IN ('pending', 'processing', 'sent')
        """
    )


def _create_operational_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_books_catalog_stock
        ON books (is_active, is_archived, stock_quantity);
        CREATE INDEX IF NOT EXISTS idx_inventory_reservations_book_state
        ON inventory_reservations (book_id, state);
        CREATE INDEX IF NOT EXISTS idx_inventory_reservations_order
        ON inventory_reservations (order_id);
        CREATE INDEX IF NOT EXISTS idx_orders_user_created
        ON orders (user_id, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_notification_lease
        ON orders (new_order_notification_state, new_order_notification_claimed_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_operational_events_alert
        ON operational_events (alert_state, alert_claimed_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_order_deliveries_status_updated
        ON order_deliveries (shipment_status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_promo_created
        ON orders (promo_code_snapshot, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_orders_acquisition_created
        ON orders (acquisition_channel, acquisition_campaign, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_order_support_requests_lease
        ON order_support_requests (state, claimed_at, id);
        """
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
            ("delivery_enabled", "0"),
            ("delivery_sdek_pickup_enabled", "1"),
            ("delivery_sdek_pickup_price_rub", "500"),
            ("delivery_russian_post_pickup_enabled", "0"),
            ("delivery_russian_post_pickup_price_rub", "500"),
            ("delivery_self_pickup_enabled", "0"),
            ("delivery_self_pickup_price_rub", "0"),
            ("delivery_self_pickup_location", ""),
            ("delivery_self_pickup_schedule", ""),
            ("delivery_self_pickup_instructions", ""),
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


def initialize_database(
    path: str | Path | None = None, *, seed_catalog: bool = True
) -> None:
    resolved_path = _resolve_database_path(path) if path is not None else current_database_path()
    with _INITIALIZATION_LOCK:
        connection = connect(resolved_path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            _create_tables(connection)
            _migrate_columns(connection)
            _migrate_inventory_movements(connection)
            _migrate_order_deliveries(connection)
            _migrate_order_support_requests(connection)
            _create_operational_indexes(connection)
            _cleanup_expired_support_messages(connection)
            _cleanup_expired_delivery_pii(connection)
            if seed_catalog:
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
