"""Асинхронный repository API поверх канонической SQLite-схемы."""
import asyncio

from db.schema import DB_PATH, initialize_database

from db.categories import (
    get_all_categories,
    add_category,
    update_category,
    delete_category,
    get_category_books_count,
)
from db.promo_codes import (
    get_all_promo_codes,
    get_promo_code,
    add_promo_code,
    update_promo_code,
    delete_promo_code,
    increment_promo_usage,
    validate_promo_code,
)
from db.referrals import (
    get_referral_code,
    parse_referral_code,
    check_referral_exists,
    create_referral,
    add_user_bonus,
    get_user_active_bonus,
    mark_bonus_used,
    get_referral_stats,
)
from db.payments import (
    get_payment_setting,
    set_payment_setting,
    get_all_payment_settings,
)
from db.message_templates import (
    get_message_template,
    get_message_templates,
    set_message_template,
    reset_message_template,
)
from db.stars import (
    get_stars_setting,
    set_stars_setting,
    rubles_to_stars,
)
from db.orders import (
    create_order,
    get_order,
    get_order_full,
    get_user_orders,
    update_order_status,
    get_unnotified_pending_orders,
    mark_new_order_notified,
    get_all_orders,
    get_orders_count,
    get_stats,
    get_all_unique_users,
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
    archive_books,
    restore_book,
    get_archived_books,
    get_archived_books_count,
    classify_archived_book_ids,
    purge_archived_books,
    update_book,
    update_book_full,
)


async def init_db() -> None:
    await asyncio.to_thread(initialize_database)
