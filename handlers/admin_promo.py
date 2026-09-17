"""Админский роутер для управления промокодами."""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

import db
from authz import has_permission_sync
from config import settings
from states import PromoCodeState

logger = logging.getLogger(__name__)
router = Router()


def is_admin(user_id: int) -> bool:
    return has_permission_sync(user_id, "catalog.manage")


def _format_promo_line(promo: dict) -> str:
    """Человекочитаемое описание одного промокода для списка."""
    if promo.get('discount_percent', 0) > 0:
        discount = f"{promo['discount_percent']}%"
    elif promo.get('discount_fixed', 0) > 0:
        discount = f"{promo['discount_fixed']} ₽"
    else:
        discount = "—"
    min_order = f"{promo['min_order']} ₽" if promo.get('min_order', 0) > 0 else "без мин."
    max_uses = "∞" if promo.get('max_uses', 0) == 0 else f"{promo['max_uses']}"
    expires = promo.get('expires_at') or "бессрочно"
    return (
        f"🎟️ <b>{promo['code']}</b>  —  {discount}\n"
        f"   мин. заказ: {min_order}  ·  лимит: {max_uses}  ·  до: {expires}\n"
        f"   использований: {promo.get('current_uses', 0)}"
    )


async def _render_promo_list(callback: CallbackQuery):
    """Перерисовывает список промокодов. Вызывается из двух обработчиков."""
    promos = await db.get_all_promo_codes()

    builder = InlineKeyboardBuilder()
    if promos:
        for promo in promos:
            builder.button(
                text=f"🗑 Удалить {promo['code']}",
                callback_data=f"admin_promo_delete_{promo['id']}",
            )
    builder.button(text="➕ Создать промокод", callback_data="admin_promo_create")
    builder.button(text="◀️ Назад", callback_data="admin_menu")
    builder.adjust(1)

    if not promos:
        text = (
            "🎟️ <b>Промокоды</b>\n\n"
            "Промокодов пока нет. Создайте пер��ый:"
        )
    else:
        text = "🎟️ <b>Промокоды</b>\n\n" + "\n\n".join(_format_promo_line(p) for p in promos)

    try:
        await callback.message.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logger.error("Ошибка при редактировании списка промокодов: %s", e)
    await callback.answer()


@router.callback_query(F.data == "admin_promo")
async def admin_promo_menu(callback: CallbackQuery, state: FSMContext):
    """Список промокодов + кнопки создать/удалить/назад."""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    # Прерываем любые висящие FSM-сценарии, чтобы «◀️ Назад» из user.py
    # вёл в чистое админ-меню.
    await state.clear()
    await _render_promo_list(callback)


@router.callback_query(F.data == "admin_promo_create")
async def admin_promo_create(callback: CallbackQuery, state: FSMContext):
    """Запускает 5-шаговый визард из user.py: код → скидка → мин.сумма → лимит → дата."""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await state.set_state(PromoCodeState.waiting_for_code)

    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад", callback_data="admin_promo")

    try:
        await callback.message.edit_text(
            "🎟️ <b>Создание промокода</b>\n\n"
            "Отправьте <b>код</b> промокода (например: <code>SUMMER2026</code>).\n\n"
            "Код будет сохранён в верхнем регистре.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logger.error("Ошибка при запуске создания промокода: %s", e)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_promo_delete_"))
async def admin_promo_delete(callback: CallbackQuery):
    """Удаляет промокод и перерисовывает список."""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        promo_id = int(callback.data.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    await db.delete_promo_code(promo_id)
    await _render_promo_list(callback)
