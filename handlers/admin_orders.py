from datetime import datetime
import asyncio
import json
import aiosqlite

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

import db
from db.orders import save_admin_notification_ids, clear_admin_notifications, PENDING_STATUSES
from config import settings
from utils import setup_logger, format_local_time

router = Router()
logger = setup_logger(__name__)

PAGE_SIZE = 20  # Количество заказов на странице

# Интервал опроса новых заказов для авто-уведомлений админам
NEW_ORDER_POLL_INTERVAL = 15  # секунд

# Множество пользователей, ожидающих сообщение для рассылки
broadcast_pending_users = set()


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


async def safe_edit_text(callback: CallbackQuery, text: str, reply_markup=None, parse_mode=None):
    """Безопасное редактирование сообщения (игнорирует 'message is not modified')"""
    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            await callback.answer("✅ Данные актуальны", show_alert=False)
        else:
            raise


# ============================================
# КОМАНДА /admin
# ============================================

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора.")
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Все заказы", callback_data="admin_orders_all")
    builder.button(text="🆕 Новые", callback_data="admin_orders_new")
    builder.button(text="✅ Подтверждённые", callback_data="admin_orders_confirmed")
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="📢 Рассылка", callback_data="admin_broadcast")
    builder.button(text="📚 Управление книгами", callback_data="admin_books_menu")
    builder.button(text="📂 Управление категориями", callback_data="admin_categories")
    builder.button(text="💳 Настройки оплаты", callback_data="admin_payments")
    builder.button(text="⭐ Настройки Stars", callback_data="admin_stars_settings")
    builder.button(text="🎟️ Промокоды", callback_data="admin_promo")
    builder.button(text="🗑 Сброс кэша", callback_data="admin_drop_cache")
    builder.adjust(2)

    await message.answer(
        f"👨‍💼 <b>Админ-панель</b>\n\n"
        f"Добро пожаловать, {message.from_user.full_name}!",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_menu")
async def admin_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Все заказы", callback_data="admin_orders_all")
    builder.button(text="🆕 Новые", callback_data="admin_orders_new")
    builder.button(text="✅ Подтверждённые", callback_data="admin_orders_confirmed")
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="📢 Рассылка", callback_data="admin_broadcast")
    builder.button(text="📚 Управление книгами", callback_data="admin_books_menu")
    builder.button(text="📂 Управление категориями", callback_data="admin_categories")
    builder.button(text="💳 Настройки оплаты", callback_data="admin_payments")
    builder.button(text="⭐ Настройки Stars", callback_data="admin_stars_settings")
    builder.button(text="🎟️ Промокоды", callback_data="admin_promo")
    builder.button(text="🗑 Сброс кэша", callback_data="admin_drop_cache")
    builder.adjust(2)

    await safe_edit_text(
        callback,
        f"👨‍💼 <b>Админ-панель</b>\n\n"
        f"Добро пожаловать, {callback.from_user.full_name}!",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================
# СПИСКИ ЗАКАЗОВ (С ПАГИНАЦИЕЙ)
# ============================================

@router.callback_query(F.data == "admin_orders_all")
async def admin_orders_all(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    await admin_orders_list(callback, status=None, title="📋 Все заказы", page=0)


@router.callback_query(F.data == "admin_orders_new")
async def admin_orders_new(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    # Показываем заказы, которые требуют внимания админа
    # (не только 'new', но и ожидающие оплаты/подтверждения)
    await admin_orders_pending(callback)


async def admin_orders_pending(callback: CallbackQuery, page: int = 0):
    """Показать заказы, требующие внимания админа"""
    # Получаем заказы со статусами, требующими обработки
    statuses = ['new', 'awaiting_payment', 'awaiting_stars_payment', 'payment_pending']

    all_orders = []
    for status in statuses:
        orders = await db.get_all_orders(limit=100, offset=0, status=status)
        all_orders.extend(orders)

    # Сортируем по дате (новые сначала)
    all_orders.sort(key=lambda x: x['created_at'], reverse=True)

    # Пагинация
    total_count = len(all_orders)
    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)

    if page >= total_pages:
        page = total_pages - 1
    if page < 0:
        page = 0

    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    orders = all_orders[start_idx:end_idx]

    if not orders:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Обновить", callback_data="admin_orders_new")
        builder.button(text="◀️ Назад", callback_data="admin_menu")
        builder.adjust(1)

        await safe_edit_text(
            callback,
            "⏳ <b>Заказы, требующие внимания</b>\n\n📭 Нет заказов, ожидающих обработки.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    status_names = {
        'new': '🆕 Новый',
        'awaiting_payment': '💳 Ожидает оплаты',
        'awaiting_stars_payment': '⭐ Ожидает Stars',
        'payment_pending': '⏳ Ожидает подтверждения'
    }

    orders_text = "⏳ <b>Заказы, требующие внимания</b>\n"
    orders_text += f"📊 Всего: {total_count} | Страница {page + 1} из {total_pages}\n\n"

    order_buttons = []
    for order in orders:
        st = status_names.get(order['status'], order['status'])
        date_str = format_local_time(order['created_at'])
        orders_text += f"#{order['id']} | {st} | {order['total']} ₽ | {date_str}\n"
        order_buttons.append(
            (f"#{order['id']} — {order['total']} ₽", f"order_detail_{order['id']}")
        )

    # Навигация
    nav_buttons = []

    if page > 0:
        nav_buttons.append(("⏮️ Первая", f"pending_page_0"))
        nav_buttons.append((f"◀️ Стр. {page}", f"pending_page_{page - 1}"))

    nav_buttons.append(("🔄 Обновить", "admin_orders_new"))

    if page < total_pages - 1:
        nav_buttons.append((f"Стр. {page + 2} ▶️", f"pending_page_{page + 1}"))
        nav_buttons.append(("Последняя ⏭️", f"pending_page_{total_pages - 1}"))

    nav_buttons.append(("◀️ В меню", "admin_menu"))

    builder = InlineKeyboardBuilder()

    for text, cb in order_buttons:
        builder.button(text=text, callback_data=cb)
    builder.adjust(2)

    for text, cb in nav_buttons:
        builder.button(text=text, callback_data=cb)
    builder.adjust(2)

    await safe_edit_text(
        callback,
        orders_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# Переключение страниц для "Ожидают обработки"
@router.callback_query(F.data.startswith("pending_page_"))
async def pending_page_switch(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        page = int(callback.data.split("_")[-1])
        await admin_orders_pending(callback, page=page)
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)

@router.callback_query(F.data == "admin_orders_confirmed")
async def admin_orders_confirmed(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    await admin_orders_list(callback, status="confirmed", title="✅ Подтверждённые", page=0)


# Переключение страниц
@router.callback_query(F.data.startswith("orders_page_"))
async def orders_page_switch(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        # Формат: orders_page_{status}_{page}
        parts = callback.data.split("_")
        page = int(parts[-1])
        status_key = parts[-2]
        status = None if status_key == "all" else status_key

        titles = {
            None: "📋 Все заказы",
            "new": "🆕 Новые заказы",
            "confirmed": "✅ Подтверждённые"
        }
        title = titles.get(status, "📋 Заказы")

        await admin_orders_list(callback, status=status, title=title, page=page)
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)


async def admin_orders_list(callback: CallbackQuery, status: str = None, title: str = "Заказы", page: int = 0):
    # Получаем общее количество заказов
    total_count = await db.get_orders_count(status=status)
    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)

    # Корректируем страницу
    if page >= total_pages:
        page = total_pages - 1
    if page < 0:
        page = 0

    # Получаем заказы с учётом пагинации
    offset = page * PAGE_SIZE
    orders = await db.get_all_orders(limit=PAGE_SIZE, offset=offset, status=status)

    status_key = status or "all"

    if not orders:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔄 Обновить", callback_data=f"refresh_{status_key}_{page}")
        builder.button(text="◀️ Назад", callback_data="admin_menu")
        builder.adjust(1)

        await safe_edit_text(
            callback,
            f"{title}\n\n📭 Заказов пока нет.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    status_names = {
        'new': '🆕 Новый',
        'confirmed': '✅ Подтверждён',
        'completed': '📦 Выполнен',
        'cancelled': '❌ Отменён',
        'awaiting_payment': '💳 Ожидает оплаты',
        'awaiting_stars_payment': '⭐ Ожидает Stars',
        'payment_pending': '⏳ Ожидает подтверждения',
        'paid': '💰 Оплачен'
    }

    # Заголовок с информацией о странице
    orders_text = f"{title}\n"
    orders_text += f"📊 Всего: {total_count} | Страница {page + 1} из {total_pages}\n\n"

    # Кнопки заказов
    order_buttons = []
    for order in orders:
        st = status_names.get(order['status'], order['status'])
        date_str = format_local_time(order['created_at'])
        orders_text += f"#{order['id']} | {st} | {order['total']} ₽ | {date_str}\n"
        order_buttons.append(
            (f"#{order['id']} — {order['total']} ₽", f"order_detail_{order['id']}")
        )

    # Навигация по страницам
    nav_buttons = []

    if page > 0:
        nav_buttons.append(("⏮️ Первая", f"orders_page_{status_key}_0"))
        nav_buttons.append((f"◀️ Стр. {page}", f"orders_page_{status_key}_{page - 1}"))

    nav_buttons.append(("🔄 Обновить", f"refresh_{status_key}_{page}"))

    if page < total_pages - 1:
        nav_buttons.append((f"Стр. {page + 2} ▶️", f"orders_page_{status_key}_{page + 1}"))
        nav_buttons.append(("Последняя ⏭️", f"orders_page_{status_key}_{total_pages - 1}"))

    nav_buttons.append(("◀️ В меню", "admin_menu"))

    # Собираем клавиатуру
    builder = InlineKeyboardBuilder()

    # Сначала кнопки заказов (по 2 в ряд)
    for text, cb in order_buttons:
        builder.button(text=text, callback_data=cb)
    builder.adjust(2)

    # Потом кнопки навигации (по 2 в ряд)
    for text, cb in nav_buttons:
        builder.button(text=text, callback_data=cb)
    builder.adjust(2)

    await safe_edit_text(
        callback,
        orders_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================
# ОБНОВЛЕНИЕ СПИСКА (С УЧЁТОМ СТРАНИЦЫ)
# ============================================

@router.callback_query(F.data.startswith("refresh_"))
async def refresh_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        # Формат: refresh_{status}_{page}
        parts = callback.data.split("_")
        page = int(parts[-1])
        status_key = parts[-2]
        status = None if status_key == "all" else status_key

        titles = {
            None: "📋 Все заказы",
            "new": "🆕 Новые заказы",
            "confirmed": "✅ Подтверждённые"
        }
        title = titles.get(status, "📋 Заказы")

        await admin_orders_list(callback, status=status, title=title, page=page)
    except (ValueError, IndexError):
        # Фолбэк для старых callback_data без страницы
        await admin_orders_list(callback, status=None, title="📋 Все заказы", page=0)


# ============================================
# ДЕТАЛИ ЗАКАЗА
# ============================================

@router.callback_query(F.data.startswith("order_detail_"))
async def order_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        order_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    order = await db.get_order_full(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return

    status_names = {
        'new': '🆕 Новый',
        'confirmed': '✅ Подтверждён',
        'completed': '📦 Выполнен',
        'cancelled': '❌ Отменён',
        'awaiting_payment': '💳 Ожидает оплаты',
        'awaiting_stars_payment': '⭐ Ожидает Stars',
        'payment_pending': '⏳ Ожидает подтверждения',
        'paid': '💰 Оплачен'
    }

    st = status_names.get(order['status'], order['status'])
    date_str = format_local_time(order['created_at'])
    items_list = "\n".join([f"  • {item['title']} — {item['price']} ₽" for item in order['items']])

    text = (
        f"📦 <b>Заказ #{order['id']}</b>\n\n"
        f"👤 Клиент: {order['user_name']}\n"
        f"🆔 ID: <code>{order['user_id']}</code>\n"
        f"📅 Дата: {date_str}\n"
        f"📊 Статус: {st}\n\n"
        f"📚 Товары:\n{items_list}\n\n"
        f"💰 <b>Итого: {order['total']} ₽</b>"
    )

    builder = InlineKeyboardBuilder()

    # Кнопки действий в зависимости от статуса
    if order['status'] in ['new', 'awaiting_payment', 'awaiting_stars_payment', 'payment_pending']:
        builder.button(text="✅ Подтвердить", callback_data=f"confirm_{order_id}")

    if order['status'] in ['new', 'awaiting_payment', 'awaiting_stars_payment', 'payment_pending', 'confirmed']:
        builder.button(text="❌ Отменить", callback_data=f"cancel_order_{order_id}")

    if order['status'] == 'confirmed':
        builder.button(text="📦 Выполнен", callback_data=f"complete_{order_id}")

    if order['status'] == 'cancelled':
        builder.button(text="🔄 Восстановить", callback_data=f"restore_{order_id}")

    builder.button(text="🔄 Обновить", callback_data=f"order_detail_{order_id}")
    builder.button(text="◀️ Назад", callback_data="admin_orders_all")
    builder.adjust(2)

    await safe_edit_text(
        callback,
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================
# КОМАНДА /order_{id} — быстрый доступ к заказу
# ============================================

@router.message(F.text.regexp(r"^/order_(\d+)$"))
async def cmd_order_by_id(message: Message):
    """Команда /order_65 — показать детали заказа #65"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав администратора.")
        return

    try:
        order_id = int(message.text.split("_")[-1])
    except (ValueError, IndexError):
        await message.answer("❌ Неверный формат. Используйте: /order_123")
        return

    order = await db.get_order_full(order_id)
    if not order:
        await message.answer(f"❌ Заказ #{order_id} не найден.")
        return

    status_names = {
        'new': '🆕 Новый',
        'confirmed': '✅ Подтверждён',
        'completed': '📦 Выполнен',
        'cancelled': '❌ Отменён',
        'awaiting_payment': '💳 Ожидает оплаты',
        'awaiting_stars_payment': '⭐ Ожидает Stars',
        'payment_pending': '⏳ Ожидает подтверждения',
        'paid': '💰 Оплачен'
    }

    st = status_names.get(order['status'], order['status'])
    date_str = format_local_time(order['created_at'])
    items_list = "\n".join([f"  • {item['title']} — {item['price']} ₽" for item in order['items']])

    text = (
        f"📦 <b>Заказ #{order['id']}</b>\n\n"
        f"👤 Клиент: {order['user_name']}\n"
        f"🆔 ID: <code>{order['user_id']}</code>\n"
        f"📅 Дата: {date_str}\n"
        f"📊 Статус: {st}\n\n"
        f"📚 Товары:\n{items_list}\n\n"
        f"💰 <b>Итого: {order['total']} ₽</b>"
    )

    builder = InlineKeyboardBuilder()

    if order['status'] in ['new', 'awaiting_payment', 'awaiting_stars_payment', 'payment_pending']:
        builder.button(text="✅ Подтвердить", callback_data=f"confirm_{order_id}")

    if order['status'] in ['new', 'awaiting_payment', 'awaiting_stars_payment', 'payment_pending', 'confirmed']:
        builder.button(text="❌ Отменить", callback_data=f"cancel_order_{order_id}")

    if order['status'] == 'confirmed':
        builder.button(text="📦 Выполнен", callback_data=f"complete_{order_id}")

    if order['status'] == 'cancelled':
        builder.button(text="🔄 Восстановить", callback_data=f"restore_{order_id}")

    builder.button(text="◀️ Назад", callback_data="admin_orders_all")
    builder.adjust(2)

    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

# ============================================
# ДЕЙСТВИЯ С ЗАКАЗАМИ
# ============================================

@router.callback_query(F.data.startswith("confirm_"))
async def confirm_order(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        order_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    await db.update_order_status(order_id, 'confirmed')
    await callback.answer("✅ Заказ подтверждён!", show_alert=True)

    order = await db.get_order_full(order_id)
    if order:
        try:
            await bot.send_message(
                order['user_id'],
                f"✅ <b>Ваш заказ #{order_id} подтверждён!</b>\n\n"
                f"Мы готовим его к отправке 📦\n"
                f"Спасибо, что выбрали «Семена Знаний»! 🌿",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await order_detail(callback)


@router.callback_query(F.data.startswith("cancel_order_"))
async def cancel_order(callback: CallbackQuery, bot: Bot):
    """Отмена заказа (работает для всех активных статусов)"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        order_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    order = await db.get_order_full(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return

    # Проверяем, можно ли отменить
    non_cancellable = ['completed', 'cancelled']
    if order['status'] in non_cancellable:
        await callback.answer(
            f"❌ Нельзя отменить заказ со статусом '{order['status']}'",
            show_alert=True
        )
        return

    await db.update_order_status(order_id, 'cancelled')
    await callback.answer("❌ Заказ отменён!", show_alert=True)

    # Уведомляем пользователя
    try:
        await bot.send_message(
            order['user_id'],
            f"❌ <b>Ваш заказ #{order_id} отменён.</b>\n\n"
            f"Если у вас есть вопросы, обратитесь в поддержку 🆘",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await order_detail(callback)


@router.callback_query(F.data.startswith("complete_"))
async def complete_order(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        order_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    await db.update_order_status(order_id, 'completed')
    await callback.answer("📦 Заказ выполнен!", show_alert=True)

    order = await db.get_order_full(order_id)
    if order:
        try:
            await bot.send_message(
                order['user_id'],
                f"📦 <b>Ваш заказ #{order_id} выполнен!</b>\n\n"
                f"Спасибо за покупку! 🌿\n\n"
                f"Будем рады видеть вас снова!",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await order_detail(callback)


@router.callback_query(F.data.startswith("restore_"))
async def restore_order(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        order_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    await db.update_order_status(order_id, 'new')
    await callback.answer("🔄 Заказ восстановлен!", show_alert=True)
    await order_detail(callback)


# ============================================
# АВТО-УВЕДОМЛЕНИЯ О НОВЫХ ЗАКАЗАХ
# ============================================

async def _notify_admins_new_order(bot: Bot, order: dict) -> bool:
    """Отправить админам карточку о новом заказе с кнопками «Принять / Отклонить».

    Возвращает True, если хотя бы один админ получил сообщение — только тогда
    заказ помечается уведомлённым, чтобы не потерять уведомление навсегда.
    """
    order_id = order['id']
    date_str = format_local_time(order['created_at'])
    status_names = {
        'new': '🆕 Новый',
        'awaiting_payment': '💳 Ожидает оплаты',
        'awaiting_stars_payment': '⭐ Ожидает Stars',
        'payment_pending': '⏳ Ожидает подтверждения'
    }
    st = status_names.get(order['status'], order['status'])

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Принять", callback_data=f"new_order_accept_{order_id}")
    builder.button(text="❌ Отклонить", callback_data=f"new_order_reject_{order_id}")
    builder.adjust(2)

    text = (
        f"🔔 <b>Заказ #{order_id} требует внимания!</b>\n\n"
        f"👤 Клиент: {order['user_name']} (ID: <code>{order['user_id']}</code>)\n"
        f"📅 Дата: {date_str}\n"
        f"📊 Статус: {st}\n"
        f"💰 Сумма: <b>{order['total']} ₽</b>\n\n"
        f"Примите заказ в работу или отклоните:"
    )

    admin_ids = []
    message_ids = []

    for admin_id in settings.ADMIN_IDS:
        try:
            msg = await bot.send_message(
                admin_id,
                text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            admin_ids.append(admin_id)
            message_ids.append(msg.message_id)
        except Exception as e:
            logger.warning(f"⚠️ Ошибка отправки уведомления админу {admin_id}: {e}")

    if admin_ids:
        await save_admin_notification_ids(order_id, admin_ids, message_ids)
        logger.info(f"🔔 Уведомление о новом заказе #{order_id} отправлено {len(admin_ids)} админам")
    return bool(admin_ids)


async def new_orders_notify_loop(bot: Bot):
    """Фоновая задача: поллер новых заказов.

    Заказы создаются веб-приложением (server.py) в отдельном процессе, поэтому
    бот опрашивает БД и шлёт админам карточки с кнопками «Принять / Отклонить».
    """
    logger.info("🔄 Поллер новых заказов запущен")
    while True:
        try:
            orders = await db.get_unnotified_pending_orders()
            for order in orders:
                # Заказ могли обработать (принять/отклонить) между тиками поллера
                current = await db.get_order(order['id'])
                if not current or current['status'] not in PENDING_STATUSES:
                    await db.mark_new_order_notified(order['id'])
                    continue

                if await _notify_admins_new_order(bot, order):
                    await db.mark_new_order_notified(order['id'])
        except Exception as e:
            logger.warning(f"⚠️ Ошибка поллера новых заказов: {e}")
        await asyncio.sleep(NEW_ORDER_POLL_INTERVAL)


@router.callback_query(F.data.startswith("new_order_accept_"))
async def new_order_accept(callback: CallbackQuery, bot: Bot):
    """Админ принял новый заказ."""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        order_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    order = await db.get_order_full(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return

    if order['status'] not in PENDING_STATUSES:
        await callback.answer(
            f"⚠️ Заказ уже обработан (статус: {order['status']})",
            show_alert=True
        )
        return

    await db.update_order_status(order_id, 'confirmed')
    await callback.answer("✅ Заказ принят в работу!", show_alert=True)

    # Удаляем дубли уведомления у остальных админов
    try:
        await clear_admin_notifications(order_id, bot)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить уведомления для заказа #{order_id}: {e}")

    try:
        await callback.message.edit_text(
            f"✅ <b>Заказ #{order_id} принят админом</b>\n\n"
            f"Дубли уведомлений у остальных админов удалены.",
            parse_mode="HTML"
        )
    except TelegramBadRequest:
        pass  # сообщение могло быть удалено clear_admin_notifications

    # Уведомляем пользователя
    try:
        await bot.send_message(
            order['user_id'],
            f"✅ <b>Ваш заказ #{order_id} подтверждён!</b>\n\n"
            f"Мы готовим его к отправке 📦\n"
            f"Спасибо, что выбрали «Семена Знаний»! 🌿",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"⚠️ Не удалось уведомить пользователя {order['user_id']}: {e}")


@router.callback_query(F.data.startswith("new_order_reject_"))
async def new_order_reject(callback: CallbackQuery, bot: Bot):
    """Админ отклонил новый заказ."""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        order_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    order = await db.get_order_full(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return

    if order['status'] not in PENDING_STATUSES:
        await callback.answer(
            f"⚠️ Заказ уже обработан (статус: {order['status']})",
            show_alert=True
        )
        return

    await db.update_order_status(order_id, 'cancelled')
    await callback.answer("❌ Заказ отклонён!", show_alert=True)

    try:
        await clear_admin_notifications(order_id, bot)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить уведомления для заказа #{order_id}: {e}")

    try:
        await callback.message.edit_text(
            f"❌ <b>Заказ #{order_id} отклонён админом</b>\n\n"
            f"Пользователь уведомлён.",
            parse_mode="HTML"
        )
    except TelegramBadRequest:
        pass  # сообщение могло быть удалено clear_admin_notifications

    # Уведомляем пользователя
    try:
        await bot.send_message(
            order['user_id'],
            f"❌ <b>Ваш заказ #{order_id} отклонён.</b>\n\n"
            f"Если у вас есть вопросы, обратитесь в поддержку 🆘",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"⚠️ Не удалось уведомить пользователя {order['user_id']}: {e}")


# ============================================
# СТАТИСТИКА
# ============================================

@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    stats = await db.get_stats()
    status_names = {
        'new': '🆕 Новые',
        'confirmed': '✅ Подтверждённые',
        'completed': '📦 Выполненные',
        'cancelled': '❌ Отменённые',
        'awaiting_payment': '💳 Ожидает оплаты',
        'awaiting_stars_payment': '⭐ Ожидает Stars',
        'payment_pending': '⏳ Ожидает подтверждения',
        'paid': '💰 Оплачен'
    }

    stats_text = (
        f"📊 <b>Статистика магазина</b>\n\n"
        f"📦 Всего заказов: <b>{stats['total_orders']}</b>\n"
        f"💰 Общая выручка: <b>{stats['total_revenue']} ₽</b>\n\n"
        f"<b>По статусам:</b>\n"
    )

    for s in stats['by_status']:
        name = status_names.get(s['status'], s['status'])
        stats_text += f"  {name}: {s['cnt']} шт. ({s['sum'] or 0} ₽)\n"

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="admin_stats")
    builder.button(text="◀️ Назад", callback_data="admin_menu")
    builder.adjust(1)

    await safe_edit_text(
        callback,
        stats_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================
# КОНЕЦ ФАЙЛА
# ============================================