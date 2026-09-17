from datetime import datetime
from html import escape
import asyncio
import json
import aiosqlite

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import db
from db.deliveries import (
    DeliveryTransitionError,
    METHOD_RUSSIAN_POST_PICKUP,
    METHOD_SELF_PICKUP,
    SHIPMENT_DELIVERED,
    SHIPMENT_PACKED,
    SHIPMENT_READY_FOR_PICKUP,
    SHIPMENT_RETURNED,
    SHIPMENT_SHIPPED,
    method_supports_tracking,
    normalize_tracking_number,
)
from db.inventory import InventoryUnavailableError
from db.orders import (
    AUTOMATIC_PAYMENT_METHODS,
    PAID_STATUSES,
    PENDING_STATUSES,
    clear_admin_notifications,
    format_order_receipt_html,
    save_admin_notification_ids,
)
from authz import has_permission_sync, recipient_ids_for_event_sync
from config import settings
from states import DeliveryAdminState
from utils import setup_logger, format_local_time
from utils.delivery_crypto import DeliveryCryptoError, decrypt_destination

router = Router()
logger = setup_logger(__name__)

PAGE_SIZE = 20  # Количество заказов на странице
_ORDER_LISTS = {
    "all": (None, "📋 Все заказы"),
    "new": ("new", "🆕 Новые заказы"),
    "confirmed": ("confirmed", "✅ Подтверждённые"),
    "completed": ("completed", "📦 Выполненные"),
    "cancelled": ("cancelled", "❌ Отменённые"),
}
_ORDER_SORT_LABELS = {
    "new": "📅 Новые сначала",
    "old": "📅 Старые сначала",
}

# Интервал опроса новых заказов для авто-уведомлений админам
NEW_ORDER_POLL_INTERVAL = 15  # секунд

def is_admin(user_id: int) -> bool:
    return has_permission_sync(user_id, "orders.read")


def _can_cancel_order(order: dict) -> bool:
    if order['status'] in {'completed', 'cancelled'}:
        return False
    return not (
        order.get('payment_method') in AUTOMATIC_PAYMENT_METHODS
        and order['status'] in PAID_STATUSES
    )


def admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Все заказы", callback_data="admin_orders_all")
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="📢 Рассылка", callback_data="admin_broadcast")
    builder.button(text="📚 Управление книгами", callback_data="admin_books_menu")
    builder.button(text="📂 Управление категориями", callback_data="admin_categories")
    builder.button(text="💳 Настройки оплаты", callback_data="admin_payments")
    builder.button(text="🚚 Доставка", callback_data="payment_settings:delivery")
    builder.button(text="🎟️ Промокоды", callback_data="admin_promo")
    builder.button(text="🗑 Сброс кэша", callback_data="admin_drop_cache")
    builder.button(text="🧹 Сброс рефералов", callback_data="admin_reset_referrals")
    builder.button(text="🎧 Поддержка", callback_data="admin_support_menu")
    builder.button(text="✏️ Тексты", callback_data="admin_texts")
    builder.adjust(2)
    return builder.as_markup()


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

    await message.answer(
        f"👨‍💼 <b>Админ-панель</b>\n\n"
        f"Добро пожаловать, {message.from_user.full_name}!",
        reply_markup=admin_keyboard(), parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_menu")
async def admin_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await safe_edit_text(
        callback,
        f"👨‍💼 <b>Админ-панель</b>\n\n"
        f"Добро пожаловать, {callback.from_user.full_name}!",
        reply_markup=admin_keyboard(),
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
    await admin_orders_list(callback)


@router.callback_query(F.data == "admin_orders_new")
async def admin_orders_new(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    await admin_orders_list(callback, status_key="new")


async def admin_orders_pending(callback: CallbackQuery, page: int = 0):
    """Compatibility route for historical attention-queue messages."""
    await admin_orders_list(callback, status_key="new", page=page)


# Переключение страниц устаревшей очереди «Новые»
@router.callback_query(F.data.startswith("pending_page_"))
async def pending_page_switch(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        page = int(callback.data.split("_")[-1])
        if page < 0:
            raise ValueError
        await admin_orders_pending(callback, page=page)
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)

@router.callback_query(F.data == "admin_orders_confirmed")
async def admin_orders_confirmed(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    await admin_orders_list(callback, status_key="confirmed")


@router.callback_query(F.data == "admin_orders_completed")
async def admin_orders_completed(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    await admin_orders_list(callback, status_key="completed")


@router.callback_query(F.data == "admin_orders_cancelled")
async def admin_orders_cancelled(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    await admin_orders_list(callback, status_key="cancelled")


def _parse_order_list_context(data: str, prefix: str) -> tuple[str, str, int] | None:
    if not data.startswith(prefix):
        return None
    parts = data[len(prefix):].split("_")
    if len(parts) == 2:
        status_key, page_text = parts
        sort_by = "new"
    elif len(parts) == 3:
        status_key, sort_by, page_text = parts
    else:
        return None
    if status_key not in _ORDER_LISTS or sort_by not in _ORDER_SORT_LABELS:
        return None
    try:
        page = int(page_text)
    except ValueError:
        return None
    return status_key, sort_by, page


@router.callback_query(F.data.startswith("orders_page_"))
async def orders_page_switch(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    context = _parse_order_list_context(callback.data, "orders_page_")
    if context is None:
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    status_key, sort_by, page = context
    await admin_orders_list(callback, status_key=status_key, sort_by=sort_by, page=page)


@router.callback_query(F.data.startswith("orders_filter_"))
async def orders_filter(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    parts = callback.data.removeprefix("orders_filter_").split("_")
    if len(parts) != 2 or parts[0] not in _ORDER_LISTS or parts[1] not in _ORDER_SORT_LABELS:
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    await admin_orders_list(callback, status_key=parts[0], sort_by=parts[1])


@router.callback_query(F.data.startswith("orders_sort_"))
async def orders_sort(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    parts = callback.data.removeprefix("orders_sort_").split("_")
    if len(parts) != 2 or parts[0] not in _ORDER_LISTS or parts[1] not in _ORDER_SORT_LABELS:
        await callback.answer("❌ Ошибка", show_alert=True)
        return
    await admin_orders_list(callback, status_key=parts[0], sort_by=parts[1])


async def admin_orders_list(
    callback: CallbackQuery,
    *,
    status_key: str = "all",
    sort_by: str = "new",
    page: int = 0,
):
    status, title = _ORDER_LISTS[status_key]
    total_count = await db.get_orders_count(status=status)
    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    orders = await db.get_all_orders(
        limit=PAGE_SIZE,
        offset=page * PAGE_SIZE,
        status=status,
        sort_by=sort_by,
    )

    controls = InlineKeyboardBuilder()
    for key, (_, filter_title) in _ORDER_LISTS.items():
        marker = "✅ " if key == status_key else ""
        controls.button(
            text=f"{marker}{filter_title}",
            callback_data=f"orders_filter_{key}_{sort_by}",
        )
    for key, sort_title in _ORDER_SORT_LABELS.items():
        marker = "✅ " if key == sort_by else ""
        controls.button(
            text=f"{marker}{sort_title}",
            callback_data=f"orders_sort_{status_key}_{key}",
        )
    controls.adjust(3, 2, 2)

    navigation = InlineKeyboardBuilder()
    if not orders:
        navigation.button(
            text="🔄 Обновить",
            callback_data=f"refresh_{status_key}_{sort_by}_{page}",
        )
        navigation.button(text="◀️ В меню", callback_data="admin_menu")
        navigation.adjust(2)
        controls.attach(navigation)
        await safe_edit_text(
            callback,
            f"{title}\n\n📭 Заказов пока нет.",
            reply_markup=controls.as_markup(),
            parse_mode="HTML",
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
        'awaiting_yookassa_payment': '🟣 Ожидает ЮKassa',
        'payment_pending': '⏳ Ожидает подтверждения',
        'paid': '💰 Оплачен',
    }
    orders_text = (
        f"{title}\n"
        f"📊 Всего: {total_count} | Страница {page + 1} из {total_pages}\n\n"
    )
    order_buttons = InlineKeyboardBuilder()
    for order in orders:
        status_name = status_names.get(order['status'], order['status'])
        preview_lines = []
        for item in order.get("items", [])[:3]:
            quantity_suffix = f" ×{item['quantity']}" if item["quantity"] > 1 else ""
            preview_lines.append(f"{escape(item['title'])}{quantity_suffix}")
        preview = ", ".join(preview_lines) or "состав не сохранён"
        discount = f" · скидка {order['total_discount']} ₽" if order.get("total_discount") else ""
        orders_text += (
            f"#{order['id']} | {status_name} | {order['total']} ₽ | "
            f"{format_local_time(order['created_at'])}\n"
            f"📚 {preview}{discount}\n"
        )
        order_buttons.button(
            text=f"#{order['id']} — {order['total']} ₽",
            callback_data=f"order_detail_{order['id']}",
        )
    order_buttons.adjust(3)

    if page > 0:
        navigation.button(text="⏮️ Первая", callback_data=f"orders_page_{status_key}_{sort_by}_0")
        navigation.button(
            text=f"◀️ Стр. {page}",
            callback_data=f"orders_page_{status_key}_{sort_by}_{page - 1}",
        )
    navigation.button(
        text="🔄 Обновить",
        callback_data=f"refresh_{status_key}_{sort_by}_{page}",
    )
    if page < total_pages - 1:
        navigation.button(
            text=f"Стр. {page + 2} ▶️",
            callback_data=f"orders_page_{status_key}_{sort_by}_{page + 1}",
        )
        navigation.button(
            text="Последняя ⏭️",
            callback_data=f"orders_page_{status_key}_{sort_by}_{total_pages - 1}",
        )
    navigation.button(text="◀️ В меню", callback_data="admin_menu")
    navigation.adjust(2)
    controls.attach(order_buttons)
    controls.attach(navigation)
    await safe_edit_text(
        callback,
        orders_text,
        reply_markup=controls.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("refresh_"))
async def refresh_orders(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    context = _parse_order_list_context(callback.data, "refresh_")
    if context is None:
        if callback.data == "refresh_all":
            await admin_orders_list(callback)
        else:
            await callback.answer("❌ Ошибка", show_alert=True)
        return
    status_key, sort_by, page = context
    await admin_orders_list(callback, status_key=status_key, sort_by=sort_by, page=page)


def _tracking_action_label(method: str) -> str:
    return "Почту России" if method == METHOD_RUSSIAN_POST_PICKUP else "СДЭК"


def _add_delivery_action_buttons(
    builder: InlineKeyboardBuilder, order_id: int, delivery: dict
) -> None:
    builder.button(text="📍 Данные доставки", callback_data=f"delivery_details:{order_id}")
    shipment_status = delivery["shipment_status"]
    method = delivery["method"]
    if shipment_status == "preparing":
        builder.button(text="📦 Собран", callback_data=f"delivery_status:{order_id}:packed")
    if shipment_status == "packed":
        if method_supports_tracking(method):
            builder.button(
                text=f"🚚 Отправить {_tracking_action_label(method)} / трек",
                callback_data=f"delivery_track:{order_id}",
            )
        else:
            builder.button(
                text="📍 Готов к выдаче",
                callback_data=f"delivery_status:{order_id}:ready_for_pickup",
            )
    if shipment_status == "shipped":
        builder.button(
            text="📍 Готов к выдаче",
            callback_data=f"delivery_status:{order_id}:ready_for_pickup",
        )
        builder.button(
            text=f"✏️ Изменить трек {_tracking_action_label(method)}",
            callback_data=f"delivery_track:{order_id}",
        )
    if shipment_status == "ready_for_pickup":
        builder.button(
            text="✅ Выдан / доставлен",
            callback_data=f"delivery_status:{order_id}:delivered",
        )
        builder.button(text="↩️ Возврат", callback_data=f"delivery_status:{order_id}:returned")


def _confirmed_order_card(order: dict):
    order_id = order["id"]
    builder = InlineKeyboardBuilder()
    delivery_summary = order.get("delivery_summary")
    text = (
        f"✅ <b>Заказ #{order_id} подтверждён</b>\n\n"
        f"👤 Клиент: {escape(order['user_name'])} (ID: <code>{order['user_id']}</code>)\n\n"
        f"{format_order_receipt_html(order)}"
    )
    if delivery_summary:
        text += (
            f"\n\n🚚 <b>Доставка:</b> {delivery_summary['method_label']}\n"
            f"Статус: {delivery_summary['shipment_label']}"
        )
        if delivery_summary["tracking"]:
            text += f"\nТрек: <code>{delivery_summary['tracking']['number']}</code>"
    if order.get("delivery"):
        _add_delivery_action_buttons(builder, order_id, order["delivery"])
    if _can_cancel_order(order):
        builder.button(text="❌ Отменить", callback_data=f"cancel_order_{order_id}")
    builder.button(text="🔄 Обновить", callback_data=f"order_detail_{order_id}")
    builder.adjust(2)
    return text, builder.as_markup()


async def notify_confirmed_order(bot: Bot, order_id: int) -> bool:
    order = await db.get_order_full(order_id)
    if not order or order["status"] != "confirmed":
        return False
    await clear_admin_notifications(order_id, bot)
    text, markup = _confirmed_order_card(order)
    pairs = []
    for admin_id in recipient_ids_for_event_sync("delivery"):
        try:
            message = await bot.send_message(
                admin_id, text, reply_markup=markup, parse_mode="HTML"
            )
            pairs.append((admin_id, message.message_id))
        except Exception:
            logger.warning("Could not send confirmed order card for order %s", order_id)
    if pairs:
        await save_admin_notification_ids(
            order_id,
            [admin_id for admin_id, _ in pairs],
            [message_id for _, message_id in pairs],
        )
    return bool(pairs)


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
        'awaiting_yookassa_payment': '🟣 Ожидает ЮKassa',
        'payment_pending': '⏳ Ожидает подтверждения',
        'paid': '💰 Оплачен'
    }

    st = status_names.get(order['status'], order['status'])
    date_str = format_local_time(order['created_at'])
    receipt = format_order_receipt_html(order)

    text = (
        f"📦 <b>Заказ #{order['id']}</b>\n\n"
        f"👤 Клиент: {escape(order['user_name'])}\n"
        f"🆔 ID: <code>{order['user_id']}</code>\n"
        f"📅 Дата: {date_str}\n"
        f"📊 Статус: {st}\n\n"
        f"{receipt}"
    )

    delivery = order.get("delivery")
    delivery_summary = order.get("delivery_summary")
    if delivery_summary:
        text += (
            f"\n\n🚚 <b>Доставка:</b> {delivery_summary['method_label']}\n"
            f"Статус: {delivery_summary['shipment_label']}\n"
            f"Стоимость: {delivery_summary['price']} ₽"
        )
        if delivery_summary["tracking"]:
            text += f"\nТрек: <code>{delivery_summary['tracking']['number']}</code>"
    elif delivery is None:
        text += "\n\n🚚 Доставка не указана (заказ создан до внедрения)."

    builder = InlineKeyboardBuilder()

    # Кнопки действий в зависимости от статуса
    if order['status'] in PENDING_STATUSES:
        builder.button(text="✅ Подтвердить", callback_data=f"confirm_{order_id}")

    if _can_cancel_order(order):
        builder.button(text="❌ Отменить", callback_data=f"cancel_order_{order_id}")

    if order['status'] == 'confirmed' and not delivery:
        builder.button(text="📦 Выполнен", callback_data=f"complete_{order_id}")

    if order['status'] == 'cancelled':
        builder.button(text="🔄 Восстановить", callback_data=f"restore_{order_id}")

    if delivery:
        _add_delivery_action_buttons(builder, order_id, delivery)

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


async def _notify_customer_delivery(bot: Bot, order: dict, text: str) -> None:
    try:
        await bot.send_message(order["user_id"], text, parse_mode="HTML")
    except Exception:
        logger.warning("Could not deliver shipment notification for order %s", order["id"])


@router.callback_query(F.data.regexp(r"^delivery_details:\d+$"))
async def delivery_details(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    order = await db.get_order_full(order_id)
    delivery = order.get("delivery") if order else None
    if not delivery:
        await callback.answer("Данные доставки недоступны", show_alert=True)
        return
    method = delivery["method"]
    if method == METHOD_SELF_PICKUP:
        snapshot = delivery.get("public_instructions_snapshot") or "Данные самовывоза недоступны"
        await bot.send_message(
            callback.from_user.id,
            f"📍 <b>Самовывоз для заказа #{order_id}</b>\n\n{escape(snapshot)}",
            parse_mode="HTML",
            protect_content=True,
        )
        await callback.answer()
        return
    if delivery.get("pii_redacted_at"):
        await callback.answer("Данные доставки удалены по сроку хранения", show_alert=True)
        return
    try:
        destination = decrypt_destination(
            order_id, method, delivery["destination_encrypted"]
        )
    except DeliveryCryptoError:
        logger.warning("Delivery data unavailable for order %s", order_id)
        await callback.answer("Данные доставки недоступны", show_alert=True)
        return
    point_label = "ПВЗ" if method == "sdek_pickup" else "Отделение Почты России"
    point_key = "pickup_point" if method == "sdek_pickup" else "post_office"
    await bot.send_message(
        callback.from_user.id,
        "📍 <b>Данные доставки для заказа #{}</b>\n\n"
        "Получатель: {}\nТелефон: <code>{}</code>\nГород: {}\n{}: {}".format(
            order_id,
            escape(destination["recipient_name"]),
            escape(destination["recipient_phone"]),
            escape(destination["city"]),
            point_label,
            escape(destination[point_key]),
        ),
        parse_mode="HTML",
        protect_content=True,
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^delivery_track:\d+$"))
async def delivery_track_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await state.clear()
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    order_id = int(callback.data.split(":", 1)[1])
    order = await db.get_order_full(order_id)
    delivery = order.get("delivery") if order else None
    if (
        not delivery
        or not method_supports_tracking(delivery["method"])
        or delivery["shipment_status"] not in {"packed", "shipped", "ready_for_pickup"}
    ):
        await callback.answer("Трек нельзя изменить в текущем статусе", show_alert=True)
        return
    carrier_label = "СДЭК" if delivery["method"] != METHOD_RUSSIAN_POST_PICKUP else "Почты России"
    tracking_rule = (
        "Для СДЭК разрешены буквы, цифры и дефис."
        if delivery["method"] != METHOD_RUSSIAN_POST_PICKUP
        else "Для Почты России укажите 14 цифр."
    )
    await state.set_state(DeliveryAdminState.waiting_for_tracking_number)
    await state.update_data(delivery_order_id=order_id)
    await callback.message.answer(
        f"🚚 Введите номер отправления {carrier_label} для заказа #{order_id}.\n\n"
        f"{tracking_rule} /cancel для отмены."
    )
    await callback.answer()


@router.message(DeliveryAdminState.waiting_for_tracking_number, F.text)
async def delivery_track_save(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return
    data = await state.get_data()
    order_id = data.get("delivery_order_id")
    if not isinstance(order_id, int):
        await state.clear()
        await message.answer("❌ Не удалось определить заказ.")
        return
    tracking_number = message.text
    order = await db.get_order_full(order_id)
    delivery = order.get("delivery") if order else None
    if (
        not delivery
        or not method_supports_tracking(delivery["method"])
        or delivery["shipment_status"] not in {"packed", "shipped", "ready_for_pickup"}
    ):
        await state.clear()
        await message.answer("❌ Трек нельзя изменить в текущем статусе.")
        return
    try:
        tracking_number = normalize_tracking_number(delivery["method"], tracking_number)
        if delivery["shipment_status"] == "packed":
            changed = await db.update_delivery_status(
                order_id, SHIPMENT_SHIPPED, admin_id=message.from_user.id,
                tracking_number=tracking_number,
            )
        else:
            changed = await db.update_tracking(
                order_id, tracking_number, message.from_user.id
            )
    except (DeliveryTransitionError, ValueError):
        changed = False
    if not changed:
        await state.clear()
        await message.answer("❌ Не удалось сохранить трек: заказ изменился. Обновите карточку.")
        return
    await state.clear()
    carrier_label = "СДЭК" if delivery["method"] != METHOD_RUSSIAN_POST_PICKUP else "Почта России"
    await message.answer(
        f"✅ Трек {carrier_label} сохранён для заказа #{order_id}: <code>{tracking_number}</code>",
        parse_mode="HTML",
    )
    await _notify_customer_delivery(
        bot,
        order,
        f"🚚 <b>Заказ #{order_id} отправлен через {carrier_label}.</b>\n\n"
        f"Трек-номер: <code>{tracking_number}</code>\n"
        "Откройте «Мои заказы», чтобы отслеживать отправление.",
    )


@router.callback_query(F.data.regexp(r"^delivery_status:\d+:(packed|ready_for_pickup|delivered|returned)$"))
async def delivery_status_update(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    _, order_id_text, target_status = callback.data.split(":", 2)
    order_id = int(order_id_text)
    order = await db.get_order_full(order_id)
    delivery = order.get("delivery") if order else None
    if not delivery:
        await callback.answer("Доставка не найдена", show_alert=True)
        return
    if target_status == SHIPMENT_DELIVERED and order["status"] != "confirmed":
        await callback.answer("Сначала подтвердите заказ", show_alert=True)
        return
    try:
        if target_status == SHIPMENT_DELIVERED:
            changed = await db.complete_delivery_and_order(
                order_id, callback.from_user.id
            )
        else:
            changed = await db.update_delivery_status(
                order_id, target_status, admin_id=callback.from_user.id
            )
    except DeliveryTransitionError:
        changed = False
    if not changed:
        await callback.answer("Статус доставки уже изменился", show_alert=True)
        return
    method = delivery.get("method", "sdek_pickup")
    if target_status == SHIPMENT_DELIVERED:
        await clear_admin_notifications(order_id, bot)
        notification = f"✅ <b>Заказ #{order_id} выдан / доставлен.</b> Спасибо за покупку!"
    elif target_status == SHIPMENT_PACKED:
        notification = f"📦 <b>Заказ #{order_id} собран.</b> Скоро передадим его выбранной службе доставки."
    elif target_status == SHIPMENT_READY_FOR_PICKUP:
        if method == METHOD_SELF_PICKUP:
            pickup = delivery.get("public_instructions_snapshot") or "Данные самовывоза доступны в «Моих заказах»."
            notification = f"📍 <b>Заказ #{order_id} готов к самовывозу.</b>\n\n{escape(pickup)}"
        else:
            notification = f"📍 <b>Заказ #{order_id} готов к выдаче.</b> Трек доступен в «Моих заказах»."
    else:
        notification = f"↩️ <b>Отправление по заказу #{order_id} возвращено.</b> Свяжитесь с поддержкой."
    await _notify_customer_delivery(bot, order, notification)
    await callback.answer("✅ Статус доставки обновлён", show_alert=True)
    try:
        await order_detail(callback)
    except TelegramBadRequest:
        pass

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
        'awaiting_yookassa_payment': '🟣 Ожидает ЮKassa',
        'payment_pending': '⏳ Ожидает подтверждения',
        'paid': '💰 Оплачен'
    }

    st = status_names.get(order['status'], order['status'])
    date_str = format_local_time(order['created_at'])
    receipt = format_order_receipt_html(order)

    text = (
        f"📦 <b>Заказ #{order['id']}</b>\n\n"
        f"👤 Клиент: {escape(order['user_name'])}\n"
        f"🆔 ID: <code>{order['user_id']}</code>\n"
        f"📅 Дата: {date_str}\n"
        f"📊 Статус: {st}\n\n"
        f"{receipt}"
    )

    delivery = order.get("delivery")
    delivery_summary = order.get("delivery_summary")
    if delivery_summary:
        text += (
            f"\n\n🚚 <b>Доставка:</b> {delivery_summary['method_label']}\n"
            f"Статус: {delivery_summary['shipment_label']}\n"
            f"Стоимость: {delivery_summary['price']} ₽"
        )
        if delivery_summary["tracking"]:
            text += f"\nТрек: <code>{delivery_summary['tracking']['number']}</code>"
    elif delivery is None:
        text += "\n\n🚚 Доставка не указана (заказ создан до внедрения)."

    builder = InlineKeyboardBuilder()

    if order['status'] in PENDING_STATUSES:
        builder.button(text="✅ Подтвердить", callback_data=f"confirm_{order_id}")

    if _can_cancel_order(order):
        builder.button(text="❌ Отменить", callback_data=f"cancel_order_{order_id}")

    if order['status'] == 'confirmed' and not delivery:
        builder.button(text="📦 Выполнен", callback_data=f"complete_{order_id}")

    if order['status'] == 'cancelled':
        builder.button(text="🔄 Восстановить", callback_data=f"restore_{order_id}")

    if delivery:
        _add_delivery_action_buttons(builder, order_id, delivery)

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

    order = await db.get_order_full(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return
    if order['status'] not in PENDING_STATUSES:
        await callback.answer(
            f"⚠️ Заказ уже обработан (статус: {order['status']})",
            show_alert=True,
        )
        return

    if not await db.update_order_status(
        order_id, 'confirmed', expected_statuses=tuple(PENDING_STATUSES)
    ):
        await callback.answer("⚠️ Заказ уже обработан", show_alert=True)
        return
    await notify_confirmed_order(bot, order_id)
    await callback.answer("✅ Заказ подтверждён!", show_alert=True)

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

    try:
        await order_detail(callback)
    except TelegramBadRequest:
        pass


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

    if not _can_cancel_order(order):
        await callback.answer(
            "❌ Нельзя отменить заказ в текущем статусе",
            show_alert=True,
        )
        return

    try:
        cancelled = await db.update_order_status(
            order_id, 'cancelled', expected_statuses=(order['status'],)
        )
    except DeliveryTransitionError:
        await callback.answer("❌ Нельзя отменить заказ после отправки", show_alert=True)
        return
    if not cancelled:
        await callback.answer("⚠️ Заказ уже обработан", show_alert=True)
        return
    await callback.answer("❌ Заказ отменён!", show_alert=True)
    await clear_admin_notifications(order_id, bot)

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

    try:
        await order_detail(callback)
    except TelegramBadRequest:
        pass


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

    order = await db.get_order_full(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return
    if order.get("delivery"):
        await callback.answer(
            "❌ Завершите отправление через статус доставки",
            show_alert=True,
        )
        return
    if order['status'] != 'confirmed':
        await callback.answer(
            f"❌ Нельзя завершить заказ со статусом '{order['status']}'",
            show_alert=True,
        )
        return

    if not await db.update_order_status(
        order_id, 'completed', expected_statuses=('confirmed',)
    ):
        await callback.answer("⚠️ Заказ уже обработан", show_alert=True)
        return
    await callback.answer("📦 Заказ выполнен!", show_alert=True)
    await clear_admin_notifications(order_id, bot)

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

    try:
        await order_detail(callback)
    except TelegramBadRequest:
        pass


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

    order = await db.get_order_full(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return
    if order['status'] != 'cancelled':
        await callback.answer(
            f"❌ Нельзя восстановить заказ со статусом '{order['status']}'",
            show_alert=True,
        )
        return

    try:
        restored = await db.update_order_status(
            order_id, 'new', expected_statuses=('cancelled',)
        )
    except InventoryUnavailableError:
        await callback.answer("❌ Недостаточно экземпляров для восстановления", show_alert=True)
        return
    if not restored:
        await callback.answer("⚠️ Заказ уже обработан", show_alert=True)
        return
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
        'awaiting_yookassa_payment': '🟣 Ожидает ЮKassa',
        'payment_pending': '⏳ Ожидает подтверждения',
        'paid': '💰 Оплачен'
    }
    st = status_names.get(order['status'], order['status'])

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Принять", callback_data=f"new_order_accept_{order_id}")
    builder.button(text="❌ Отклонить", callback_data=f"new_order_reject_{order_id}")
    builder.adjust(2)

    detailed_order = await db.get_order_full(order_id)
    receipt = format_order_receipt_html(detailed_order) if isinstance(detailed_order, dict) else ""
    text = (
        f"🔔 <b>Заказ #{order_id} требует внимания!</b>\n\n"
        f"👤 Клиент: {escape(order['user_name'])} (ID: <code>{order['user_id']}</code>)\n"
        f"📅 Дата: {date_str}\n"
        f"📊 Статус: {st}\n"
        f"{receipt}\n\n"
        f"Примите заказ в работу или отклоните:"
    )

    admin_ids = []
    message_ids = []

    for admin_id in recipient_ids_for_event_sync("payment"):
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
            orders = await db.claim_unnotified_pending_orders(
                lease_seconds=settings.NEW_ORDER_NOTIFICATION_LEASE_SECONDS
            )
            for order in orders:
                # Заказ могли обработать (принять/отклонить) между claim и отправкой.
                current = await db.get_order(order['id'])
                if not current or current['status'] not in PENDING_STATUSES:
                    await db.mark_new_order_notified(order['id'])
                    continue

                if await _notify_admins_new_order(bot, order):
                    await db.mark_new_order_notified(order['id'])
                else:
                    await db.release_new_order_notification_claim(order['id'])
        except Exception as e:
            logger.warning(f"⚠️ Ошибка поллера новых заказов: {e}")
        await asyncio.sleep(NEW_ORDER_POLL_INTERVAL)


async def inventory_notification_loop(bot: Bot):
    """Deliver durable stock notifications created by inventory transactions."""
    from db.inventory import (
        claim_notification_outbox,
        mark_notification_sent,
        release_notification_claim,
        revoke_back_in_stock_subscription,
    )

    logger.info("Inventory notification worker started")
    while True:
        try:
            notifications = await claim_notification_outbox(
                lease_seconds=settings.OPERATIONAL_ALERT_LEASE_SECONDS
            )
            for notification in notifications:
                book = await db.get_book(notification["book_id"])
                if not book:
                    await mark_notification_sent(notification["id"])
                    continue
                if notification["kind"] == "low_stock":
                    try:
                        payload = json.loads(notification["payload_json"])
                    except (TypeError, json.JSONDecodeError):
                        payload = {}
                    text = (
                        "⚠️ <b>Низкий остаток</b>\n\n"
                        f"Книга: {escape(book['title'])}\n"
                        f"Доступно: <b>{payload.get('available', 0)}</b>\n"
                        f"Порог: {payload.get('threshold', 3)}"
                    )
                    delivered = False
                    for admin_id in recipient_ids_for_event_sync("stock"):
                        try:
                            await bot.send_message(admin_id, text, parse_mode="HTML")
                            delivered = True
                        except Exception:
                            logger.warning("Low-stock alert delivery failed for admin %s", admin_id)
                    if delivered:
                        await mark_notification_sent(notification["id"])
                    else:
                        await release_notification_claim(notification["id"], "delivery_failed")
                    continue

                user_id = notification.get("user_id")
                if not user_id:
                    await mark_notification_sent(notification["id"])
                    continue
                try:
                    await bot.send_message(
                        user_id,
                        f"📚 <b>«{escape(book['title'])}» снова в наличии.</b>\n\n"
                        "Откройте Mini App, чтобы оформить заказ.",
                        parse_mode="HTML",
                    )
                except TelegramForbiddenError:
                    await revoke_back_in_stock_subscription(user_id, notification["book_id"])
                    await mark_notification_sent(notification["id"])
                except Exception:
                    await release_notification_claim(notification["id"], "delivery_failed")
                else:
                    await revoke_back_in_stock_subscription(user_id, notification["book_id"])
                    await mark_notification_sent(notification["id"])
        except Exception:
            logger.exception("Inventory notification worker failed")
        await asyncio.sleep(NEW_ORDER_POLL_INTERVAL)


async def operational_alert_loop(bot: Bot):
    """Deliver deduplicated critical operational events to administrators."""
    from db.operational_events import (
        claim_alertable_events,
        mark_alert_sent,
        release_alert_claim,
    )

    logger.info("Operational alert poller started")
    while True:
        try:
            events = await claim_alertable_events(
                lease_seconds=settings.OPERATIONAL_ALERT_LEASE_SECONDS
            )
            for event in events:
                text = (
                    "⚠️ <b>Операционная ошибка</b>\n\n"
                    f"Компонент: <code>{event['component']}</code>\n"
                    f"Событие: <code>{event['event']}</code>\n"
                    f"Причина: <code>{event['reason'] or 'unknown'}</code>\n"
                )
                if event["order_id"] is not None:
                    text += f"Заказ: <code>#{event['order_id']}</code>\n"
                if event["attempt_id"] is not None:
                    text += f"Попытка оплаты: <code>#{event['attempt_id']}</code>\n"
                delivered = False
                for admin_id in recipient_ids_for_event_sync("critical"):
                    try:
                        await bot.send_message(admin_id, text, parse_mode="HTML")
                        delivered = True
                    except Exception:
                        logger.warning("Operational alert delivery failed for admin %s", admin_id)
                if delivered:
                    await mark_alert_sent(event["id"])
                else:
                    await release_alert_claim(event["id"])
        except Exception:
            logger.exception("Operational alert poller failed")
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

    if not await db.update_order_status(
        order_id, 'confirmed', expected_statuses=tuple(PENDING_STATUSES)
    ):
        await callback.answer("⚠️ Заказ уже обработан", show_alert=True)
        return
    await callback.answer("✅ Заказ принят в работу!", show_alert=True)

    await notify_confirmed_order(bot, order_id)

    try:
        await callback.message.edit_text(
            f"✅ <b>Заказ #{order_id} принят админом</b>",
            parse_mode="HTML"
        )
    except TelegramBadRequest:
        pass

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
    if not _can_cancel_order(order):
        await callback.answer(
            "❌ Оплаченный заказ через платёжного провайдера нельзя отклонить",
            show_alert=True,
        )
        return

    if not await db.update_order_status(
        order_id, 'cancelled', expected_statuses=tuple(PENDING_STATUSES)
    ):
        await callback.answer("⚠️ Заказ уже обработан", show_alert=True)
        return
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
        'awaiting_yookassa_payment': '🟣 Ожидает ЮKassa',
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