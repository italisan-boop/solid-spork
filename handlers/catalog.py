import json
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

import db
from config import settings
from states import AddBookState, EditBookState
from utils import parseBookImages

router = Router()


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


# ============================================
# МЕНЮ КАТАЛОГА
# ============================================

@router.callback_query(F.data == "admin_catalog")
async def admin_catalog_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    books = await db.get_all_books()
    count = len(books)

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить книгу", callback_data="catalog_add")
    builder.button(text="✏️ Редактировать книгу", callback_data="catalog_edit_list")
    builder.button(text="🗑️ Удалить книгу", callback_data="catalog_delete_list")
    builder.button(text="📖 Показать все книги", callback_data="catalog_list")
    builder.button(text="◀️ Назад", callback_data="admin_menu")
    builder.adjust(1)

    await callback.message.edit_text(
        f"📚 <b>Управление каталогом</b>\n\n"
        f"Всего книг: <b>{count}</b>\n\n"
        f"Выберите действие:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================
# ДОБАВЛЕНИЕ КНИГИ
# ============================================

@router.callback_query(F.data == "catalog_add")
async def catalog_add_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await state.set_state(AddBookState.waiting_for_title)
    await callback.message.answer(
        "📚 <b>Добавление новой книги</b>\n\n"
        "Отправьте <b>название</b> книги:\n\n"
        "Или /cancel для отмены",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("book_cat_"))
async def book_select_category(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    if callback.data == "book_cat_custom":
        await state.set_state(AddBookState.waiting_for_category)
        await callback.message.answer("📂 Введите название категории вручную:")
        await callback.answer()
        return

    try:
        cat_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    categories = await db.get_all_categories()
    cat = next((c for c in categories if c['id'] == cat_id), None)
    if not cat:
        await callback.answer("❌ Категория не найдена", show_alert=True)
        return

    await state.update_data(category=cat['name'], category_id=cat_id)
    await state.set_state(AddBookState.waiting_for_emoji)

    await callback.message.answer(
        f"✅ Категория: <b>{cat['emoji'] or ''} {cat['name']}</b>\n\n"
        f"Теперь отправьте эмодзи или URL обложки (или 'нет'):",
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================
# СПИСОК КНИГ ДЛЯ РЕДАКТИРОВАНИЯ
# ============================================

@router.callback_query(F.data == "catalog_edit_list")
async def catalog_edit_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    books = await db.get_all_books()
    if not books:
        await callback.message.answer("📭 Каталог пуст. Сначала добавьте книги.")
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for book in books[:10]:
        emoji_display = "🖼️" if book['emoji'].startswith('http') else book['emoji']
        builder.button(
            text=f"{emoji_display} {book['title']} ({book['price']} ₽)",
            callback_data=f"catalog_edit_{book['id']}"
        )

    if len(books) > 10:
        builder.button(text=f"... и ещё {len(books) - 10} книг", callback_data="catalog_edit_more")

    builder.button(text="◀️ Назад", callback_data="admin_catalog")
    builder.adjust(1)

    await callback.message.edit_text(
        "✏️ <b>Редактирование книги</b>\n\n"
        "Выберите книгу для изменения:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================
# МЕНЮ РЕДАКТИРОВАНИЯ КНИГИ
# ============================================

async def _show_edit_book_menu(callback: CallbackQuery, state: FSMContext, book_id: int):
    """Вспомогательная функция для показа меню редактирования книги"""
    from aiogram.exceptions import TelegramBadRequest

    book = await db.get_book(book_id)
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    await state.update_data(edit_book_id=book_id)

    emoji_display = "[Картинка]" if book.get('emoji', '').startswith('http') else book.get('emoji', '📚')
    current_order = book.get('sort_order', 0) or 0

    builder = InlineKeyboardBuilder()
    builder.button(text="📖 Изменить название", callback_data="edit_field_title")
    builder.button(text="💰 Изменить цену", callback_data="edit_field_price")
    builder.button(text="📂 Изменить категорию", callback_data="edit_book_category")
    builder.button(text="🎨 Изменить обложку", callback_data="edit_field_emoji")
    builder.button(text="📝 Редактировать описание", callback_data="edit_book_description")
    builder.button(text="🖼️ Управление фото", callback_data="edit_book_images")
    builder.button(text="⬆️ Поднять выше", callback_data=f"book_order_up_{book_id}")
    builder.button(text="⬇️ Опустить ниже", callback_data=f"book_order_down_{book_id}")
    builder.button(text="◀️ Назад", callback_data="catalog_edit_list")
    builder.adjust(1)

    text = (
        f"✏️ <b>Редактирование</b>\n\n"
        f"📖 {book['title']}\n"
        f"💰 {book['price']} ₽\n"
        f"📂 {book['category']}\n"
        f"🎨 {emoji_display}\n"
        f"📊 Порядок: {current_order}\n\n"
        f"Что изменить?"
    )

    # 🔧 ВАЖНО: игнорируем ошибку "message is not modified"
    # Она возникает, когда порядок не изменился (например, книга уже внизу)
    try:
        await callback.message.edit_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            # Сообщение не изменилось — это нормально, просто игнорируем
            pass
        else:
            # Другая ошибка — пробрасываем дальше
            raise


@router.callback_query(F.data.startswith("catalog_edit_"))
async def catalog_edit_select(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        book_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка выбора", show_alert=True)
        return

    await _show_edit_book_menu(callback, state, book_id)
    await callback.answer()


# ============================================
# ИЗМЕНЕНИЕ ПОЛЕЙ КНИГИ
# ============================================

@router.callback_query(F.data.startswith("edit_field_"))
async def catalog_edit_field(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    field_map = {
        'edit_field_title': ('title', '📖', 'название'),
        'edit_field_price': ('price', '💰', 'цену'),
        'edit_field_category': ('category', '📂', 'категорию'),
        'edit_field_emoji': ('emoji', '🎨', 'обложку (эмодзи или URL картинки)')
    }

    field_key = callback.data
    if field_key not in field_map:
        await callback.answer("❌ Неверное поле", show_alert=True)
        return

    field, icon, field_name = field_map[field_key]
    await state.update_data(edit_field=field)
    await state.set_state(EditBookState.waiting_for_value)

    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад", callback_data="cancel_edit_book")

    await callback.message.answer(
        f"{icon} Введите новое <b>{field_name}</b>:\n\n"
        "Или нажмите кнопку ниже для отмены",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================
# ИЗМЕНЕНИЕ КАТЕГОРИИ КНИГИ
# ============================================

@router.callback_query(F.data == "edit_book_category")
async def edit_book_category_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    data = await state.get_data()
    book_id = data.get('edit_book_id')

    if not book_id:
        await callback.answer("❌ Ошибка: книга не выбрана", show_alert=True)
        return

    categories = await db.get_all_categories()

    if categories:
        builder = InlineKeyboardBuilder()
        for cat in categories:
            builder.button(
                text=f"{cat['emoji'] or ''} {cat['name']}".strip(),
                callback_data=f"edit_cat_{cat['id']}"
            )
        builder.button(text="✏️ Ввести новую", callback_data="edit_cat_custom")
        builder.button(text="◀️ Назад", callback_data=f"catalog_edit_{book_id}")
        builder.adjust(2)

        await callback.message.edit_text(
            "📂 <b>Выберите новую категорию</b> из списка:\n\n"
            "Или нажмите 'Ввести новую' для создания категории",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text="✏️ Создать категорию", callback_data="edit_cat_custom")
        builder.button(text="◀️ Назад", callback_data=f"catalog_edit_{book_id}")

        await callback.message.edit_text(
            "📂 <b>Категорий пока нет</b>\n\n"
            "Создайте первую категорию:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )

    await callback.answer()


@router.callback_query(F.data.startswith("edit_cat_"))
async def edit_book_select_category(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    data = await state.get_data()
    book_id = data.get('edit_book_id')

    if not book_id:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    if callback.data == "edit_cat_custom":
        await state.set_state(EditBookState.waiting_for_new_category)

        builder = InlineKeyboardBuilder()
        builder.button(text="◀️ Назад", callback_data=f"catalog_edit_{book_id}")

        await callback.message.answer(
            "📂 Введите <b>название новой категории</b>:\n\n"
            "Или нажмите кнопку ниже для отмены",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    try:
        cat_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    categories = await db.get_all_categories()
    cat = next((c for c in categories if c['id'] == cat_id), None)
    if not cat:
        await callback.answer("❌ Категория не найдена", show_alert=True)
        return

    await db.update_book_full(book_id, category=cat['name'], category_id=cat_id)

    book = await db.get_book(book_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="📚 В меню каталога", callback_data="admin_catalog")
    builder.button(text="✏️ Продолжить редактирование", callback_data=f"catalog_edit_{book_id}")
    builder.adjust(1)

    await callback.message.answer(
        f"✅ <b>Категория обновлена!</b>\n\n"
        f"📖 {book['title']}\n"
        f"📂 Новая категория: <b>{cat['emoji'] or ''} {cat['name']}</b>\n\n"
        f"✨ Изменения сразу появятся в Mini App!",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================
# РЕДАКТИРОВАНИЕ ОПИСАНИЯ
# ============================================

@router.callback_query(F.data == "edit_book_description")
async def edit_book_desc_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    data = await state.get_data()
    book_id = data.get('edit_book_id')

    if not book_id:
        await callback.answer("❌ Ошибка: книга не выбрана", show_alert=True)
        return

    book = await db.get_book(book_id)
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    await state.set_state(EditBookState.waiting_for_description)

    current_desc = book.get('description') or 'Не задано'
    if len(current_desc) > 200:
        current_desc = current_desc[:200] + '...'

    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад", callback_data=f"catalog_edit_{book_id}")

    await callback.message.answer(
        f"📝 <b>Редактирование описания</b>\n\n"
        f"Книга: {book['title']}\n\n"
        f"Текущее описание:\n<i>{current_desc}</i>\n\n"
        f"Отправьте новое описание (можно несколько абзацев):\n\n"
        f"Или нажмите кнопку ниже для отмены",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================
# УПРАВЛЕНИЕ ФОТО
# ============================================

@router.callback_query(F.data == "edit_book_images")
async def edit_book_images_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    data = await state.get_data()
    book_id = data.get('edit_book_id')

    if not book_id:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    book = await db.get_book(book_id)
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    images = parseBookImages(book.get('images') or book.get('emoji') or '[]')

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить URL", callback_data="add_image_url")
    builder.button(text="📸 Отправить фото", callback_data="add_image_photo")
    if images:
        builder.button(text=f"🗑️ Удалить все ({len(images)})", callback_data="clear_all_images")
    builder.button(text="◀️ Назад", callback_data=f"catalog_edit_{book_id}")
    builder.adjust(1)

    await callback.message.answer(
        f"🖼️ <b>Управление изображениями</b>\n\n"
        f"Книга: {book['title']}\n\n"
        f"Сейчас загружено: <b>{len(images)} фото</b>\n\n"
        f"Выберите действие:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "add_image_url")
async def add_image_url_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await state.set_state(EditBookState.waiting_for_image_url)

    await callback.message.answer(
        "🔗 <b>Добавление по URL</b>\n\n"
        f"Отправьте ссылку на изображение:\n\n"
        f"Например: https://example.com/photo.jpg\n\n"
        f"Или /cancel для отмены",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "add_image_photo")
async def add_image_photo_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await state.set_state(EditBookState.waiting_for_image_photo)

    await callback.message.answer(
        "📸 <b>Загрузка фото</b>\n\n"
        f"Отправьте фотографию (как фото):\n\n"
        f"Или /cancel для отмены"
    )
    await callback.answer()


@router.callback_query(F.data == "clear_all_images")
async def clear_all_images(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    data = await state.get_data()
    book_id = data.get('edit_book_id')

    if not book_id:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    # 🔧 ВАЖНО: очищаем ТОЛЬКО images, НЕ трогаем emoji (обложку)
    await db.update_book_full(book_id, images='[]')

    await callback.message.answer("✅ Все фото из галереи удалены (обложка сохранена)")
    await callback.answer()


# ============================================
# УПРАВЛЕНИЕ ПОРЯДКОМ ОТОБРАЖЕНИЯ
# ============================================

@router.callback_query(F.data.startswith("book_order_up_"))
async def book_order_up(callback: CallbackQuery, state: FSMContext):
    """Поднять книгу выше в списке"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        book_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    book = await db.get_book(book_id)
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    current_order = book.get('sort_order', 0) or 0
    new_order = max(0, current_order - 1)

    await db.update_book_sort_order(book_id, new_order)

    await callback.answer(f"⬆️ Книга поднята (порядок: {new_order})", show_alert=True)

    # Перерисовываем меню редактирования книги
    await _show_edit_book_menu(callback, state, book_id)


@router.callback_query(F.data.startswith("book_order_down_"))
async def book_order_down(callback: CallbackQuery, state: FSMContext):
    """Опустить книгу ниже в списке"""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        book_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    book = await db.get_book(book_id)
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    current_order = book.get('sort_order', 0) or 0
    new_order = current_order + 1

    await db.update_book_sort_order(book_id, new_order)

    await callback.answer(f"⬇️ Книга опущена (порядок: {new_order})", show_alert=True)

    # Перерисовываем меню редактирования книги
    await _show_edit_book_menu(callback, state, book_id)


# ============================================
# ОТМЕНА ДЕЙСТВИЙ
# ============================================

@router.callback_query(F.data == "cancel_add_book")
async def cancel_add_book(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await state.clear()

    books = await db.get_all_books()
    count = len(books)

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить книгу", callback_data="catalog_add")
    builder.button(text="✏️ Редактировать книгу", callback_data="catalog_edit_list")
    builder.button(text="🗑️ Удалить книгу", callback_data="catalog_delete_list")
    builder.button(text="📖 Показать все книги", callback_data="catalog_list")
    builder.button(text="◀️ Назад", callback_data="admin_menu")
    builder.adjust(1)

    await callback.message.edit_text(
        f"📚 <b>Управление каталогом</b>\n\n"
        f"Всего книг: <b>{count}</b>\n\n"
        f"Выберите действие:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer("✅ Добавление отменено")


@router.callback_query(F.data == "cancel_edit_book")
async def cancel_edit_book(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await state.clear()

    books = await db.get_all_books()
    if not books:
        await callback.message.answer("📭 Каталог пуст.")
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for book in books[:10]:
        emoji_display = "🖼️" if book['emoji'].startswith('http') else book['emoji']
        builder.button(
            text=f"{emoji_display} {book['title']} ({book['price']} ₽)",
            callback_data=f"catalog_edit_{book['id']}"
        )

    if len(books) > 10:
        builder.button(text=f"... и ещё {len(books) - 10} книг", callback_data="catalog_edit_more")

    builder.button(text="◀️ Назад", callback_data="admin_catalog")
    builder.adjust(1)

    await callback.message.edit_text(
        "✏️ <b>Редактирование книги</b>\n\n"
        "Выберите книгу для изменения:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer("✅ Редактирование отменено")


# ============================================
# УДАЛЕНИЕ КНИГИ
# ============================================

@router.callback_query(F.data == "catalog_delete_list")
async def catalog_delete_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    books = await db.get_all_books()
    if not books:
        await callback.message.answer("📭 Каталог пуст.")
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    for book in books[:10]:
        emoji_display = "🖼️" if book['emoji'].startswith('http') else book['emoji']
        builder.button(
            text=f"❌ {emoji_display} {book['title']}",
            callback_data=f"catalog_delete_{book['id']}"
        )

    builder.button(text="◀️ Назад", callback_data="admin_catalog")
    builder.adjust(1)

    await callback.message.edit_text(
        "🗑️ <b>Удаление книги</b>\n\n"
        "Выберите книгу для удаления:\n\n"
        "⚠️ Книга будет помечена как неактивная",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("catalog_delete_"))
async def catalog_delete_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        book_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    book = await db.get_book(book_id)
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    await db.delete_book(book_id)

    await callback.message.answer(
        f"✅ Книга <b>{book['title']}</b> удалена из каталога.",
        parse_mode="HTML"
    )
    await callback.answer()


# ============================================
# ПОКАЗ ВСЕХ КНИГ
# ============================================

@router.callback_query(F.data == "catalog_list")
async def catalog_show_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    books = await db.get_all_books()
    if not books:
        await callback.message.answer("📭 Каталог пуст.")
        await callback.answer()
        return

    text = "📚 <b>Весь каталог:</b>\n\n"
    for i, b in enumerate(books, 1):
        emoji_display = "🖼️ [Картинка]" if b['emoji'].startswith('http') else b['emoji']
        order = b.get('sort_order', 0) or 0
        text += f"{i}. {emoji_display} <b>{b['title']}</b>\n   💰 {b['price']} ₽ | 📂 {b['category']} | 📊 {order}\n\n"

    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()