from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
import logging

import db
from authz import has_permission_sync
from config import settings
from states import CategoryState

logger = logging.getLogger(__name__)
router = Router()

CATEGORY_PAGE_SIZE = 20  # сколько категорий показывать на одной странице


def is_admin(user_id: int) -> bool:
    return has_permission_sync(user_id, "catalog.manage")


async def show_categories_list(callback: CallbackQuery, page: int):
    """Список категорий текстом + по кнопке редактирования на каждую (с пагинацией)."""
    offset = page * CATEGORY_PAGE_SIZE
    all_categories = await db.get_all_categories()
    total = len(all_categories)
    total_pages = (total + CATEGORY_PAGE_SIZE - 1) // CATEGORY_PAGE_SIZE if total > 0 else 1

    categories = all_categories[offset:offset + CATEGORY_PAGE_SIZE]

    text = f"📂 <b>Управление категориями</b>\n\n"
    text += f"Страница {page + 1} из {total_pages}\n"
    text += f"Всего категорий: <b>{total}</b>\n\n"

    if categories:
        for cat in categories:
            books_count = await db.get_category_books_count(cat['id'])
            emoji = cat['emoji'] or '📂'
            text += f"{emoji} <b>{cat['name']}</b> — {books_count} книг\n"
    else:
        text += "Пока нет категорий."

    builder = InlineKeyboardBuilder()

    if categories:
        for cat in categories:
            builder.button(
                text=f"✏️ {cat['emoji'] or ''} {cat['name']}".strip(),
                callback_data=f"category_edit_{cat['id']}",
            )

        nav = []
        if page > 0:
            nav.append(("⬅️ Назад", f"admin_categories_page_{page - 1}"))
        if page < total_pages - 1:
            nav.append(("➡️ Вперёд", f"admin_categories_page_{page + 1}"))
        for btn_text, btn_data in nav:
            builder.button(text=btn_text, callback_data=btn_data)

    builder.button(text="➕ Добавить категорию", callback_data="category_add")
    builder.button(text="◀️ В меню админа", callback_data="admin_menu")
    builder.adjust(1)

    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения категорий: {e}")


@router.callback_query(F.data == "admin_categories")
async def admin_categories_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(" Нет прав", show_alert=True)
        return
    await show_categories_list(callback, page=0)
    await callback.answer()


@router.callback_query(F.data.startswith("admin_categories_page_"))
async def categories_page_navigation(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer(" Нет прав", show_alert=True)
        return
    try:
        page = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        page = 0
    await show_categories_list(callback, page=page)
    await callback.answer()


@router.callback_query(F.data == "category_add")
async def category_add_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await state.set_state(CategoryState.waiting_for_name)

    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад", callback_data="admin_categories")

    await callback.message.answer(
        "📂 <b>Добавление новой категории</b>\n\n"
        "Отправьте <b>название</b> категории (например: Фантастика):\n\n"
        "Или нажмите кнопку ниже для отмены",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("category_edit_"))
async def category_edit_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        cat_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    await state.update_data(edit_category_id=cat_id)

    categories = await db.get_all_categories()
    cat = next((c for c in categories if c['id'] == cat_id), None)
    if not cat:
        await callback.answer("❌ Категория не найдена", show_alert=True)
        return

    books_count = await db.get_category_books_count(cat_id)

    builder = InlineKeyboardBuilder()
    builder.button(text=" Изменить название", callback_data="cat_edit_name")
    builder.button(text="🎨 Изменить эмодзи", callback_data="cat_edit_emoji")
    if books_count == 0:
        builder.button(text="🗑️ Удалить категорию", callback_data=f"cat_delete_{cat_id}")
    else:
        builder.button(text=f"️ Нельзя удалить ({books_count} книг)", callback_data="noop")
    builder.button(text="️ Назад", callback_data="admin_categories")
    builder.adjust(1)

    await callback.message.edit_text(
        f"✏️ <b>Редактирование категории</b>\n\n"
        f"📂 Название: {cat['name']}\n"
        f"🎨 Эмодзи: {cat['emoji'] or '—'}\n"
        f"📚 Книг в категории: {books_count}\n\n"
        f"Что изменить?",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "cat_edit_name")
async def cat_edit_name_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await state.set_state(CategoryState.editing_name)

    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад", callback_data="admin_categories")

    await callback.message.answer(
        "📝 Введите <b>новое название</b> категории:\n\n"
        "Или нажмите кнопку ниже для отмены",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "cat_edit_emoji")
async def cat_edit_emoji_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await state.set_state(CategoryState.editing_emoji)

    builder = InlineKeyboardBuilder()
    builder.button(text="️ Назад", callback_data="admin_categories")

    await callback.message.answer(
        " Отправьте <b>новый эмодзи</b> для категории:\n\n"
        "Или нажмите кнопку ниже для отмены",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cat_delete_"))
async def cat_delete_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        cat_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer(" Ошибка", show_alert=True)
        return

    result = await db.delete_category(cat_id)

    if result['success']:
        await callback.message.answer(f"✅ Категория удалена.")
    else:
        await callback.message.answer(f"❌ Нельзя удалить: в категории {result['books_count']} книг.")

    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer("⚠️ Сначала удалите или перенесите книги из этой категории", show_alert=True)