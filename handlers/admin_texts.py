import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
from config import settings
from content_defaults import TEMPLATES_BY_KEY, templates_for_group
from db.message_templates import TemplateValidationError
from states import TextSettingsState


router = Router()


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


def _template_id(key: str) -> str:
    return key.replace("support.", "s.")


def _template_key(template_id: str) -> str | None:
    key = template_id.replace("s.", "support.", 1)
    return key if key in TEMPLATES_BY_KEY else None


def _escaped_preview(value: str, limit: int = 800) -> str:
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return html.escape(value, quote=False)


async def _show_templates(callback: CallbackQuery) -> None:
    templates = templates_for_group("support")
    values = await db.get_message_templates()
    builder = InlineKeyboardBuilder()
    for template in templates:
        current = values[template.key]
        label = f"{template.title}: {current[:28]}"
        builder.button(text=label, callback_data=f"admin_text:{_template_id(template.key)}")
    builder.button(text="◀️ Назад", callback_data="admin_menu")
    builder.adjust(1)
    text = (
        "✏️ <b>Тексты для покупателей</b>\n\n"
        "Выберите шаблон для редактирования. Динамические подстановки, если они есть, "
        "должны остаться в точности как указано в карточке."
    )
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")


async def _show_template(callback: CallbackQuery, key: str) -> None:
    template = TEMPLATES_BY_KEY[key]
    current = await db.get_message_template(key)
    placeholders = ", ".join(f"<code>{{{name}}}</code>" for name in sorted(template.placeholders)) or "нет"
    builder = InlineKeyboardBuilder()
    template_id = _template_id(key)
    builder.button(text="✏️ Изменить", callback_data=f"admin_text_edit:{template_id}")
    builder.button(text="↩️ Сбросить", callback_data=f"admin_text_reset:{template_id}")
    builder.button(text="◀️ К списку", callback_data="admin_texts")
    builder.adjust(1)
    text = (
        f"✏️ <b>{html.escape(template.title)}</b>\n\n"
        f"Текущее значение:\n<pre>{_escaped_preview(current)}</pre>\n\n"
        f"Плейсхолдеры: {placeholders}\n\n"
        "Поддерживается корректная Telegram HTML-разметка."
    )
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "admin_texts")
async def admin_texts(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    await state.clear()
    await _show_templates(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_text:") & ~F.data.startswith("admin_text_edit:") & ~F.data.startswith("admin_text_reset:"))
async def admin_text_card(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    key = _template_key(callback.data.removeprefix("admin_text:"))
    if key is None:
        await callback.answer("❌ Шаблон не найден", show_alert=True)
        return
    await _show_template(callback, key)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_text_edit:"))
async def admin_text_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    key = _template_key(callback.data.removeprefix("admin_text_edit:"))
    if key is None:
        await callback.answer("❌ Шаблон не найден", show_alert=True)
        return
    template = TEMPLATES_BY_KEY[key]
    placeholders = ", ".join(f"{{{name}}}" for name in sorted(template.placeholders)) or "нет"
    await state.set_state(TextSettingsState.waiting_for_value)
    await state.update_data(template_key=key)
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Отмена", callback_data="admin_texts")
    await callback.message.answer(
        f"Отправьте новый текст для «{template.title}».\n\n"
        f"Плейсхолдеры: {placeholders}.",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.message(TextSettingsState.waiting_for_value, F.text & ~F.text.startswith("/"))
async def save_template_value(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return
    data = await state.get_data()
    key = data.get("template_key")
    if key not in TEMPLATES_BY_KEY:
        await state.clear()
        await message.answer("⚠️ Шаблон не определён. Откройте раздел заново.")
        return
    try:
        await db.set_message_template(key, message.text)
    except TemplateValidationError as exc:
        await message.answer(f"❌ {html.escape(str(exc), quote=False)}", parse_mode="HTML")
        return
    await state.clear()
    await message.answer("✅ Текст сохранён.")


@router.callback_query(F.data.startswith("admin_text_reset:"))
async def admin_text_reset(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    key = _template_key(callback.data.removeprefix("admin_text_reset:"))
    if key is None:
        await callback.answer("❌ Шаблон не найден", show_alert=True)
        return
    await db.reset_message_template(key)
    await _show_template(callback, key)
    await callback.answer("Заводской текст восстановлен")
