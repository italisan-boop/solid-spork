from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
import logging
from config.settings import settings
from db.books import add_book, get_all_books, update_book, delete_book, get_book
from db.categories import get_all_categories
from utils import parseBookImages

logger = logging.getLogger(__name__)
router = Router()


class BookAddState(StatesGroup):
    waiting_for_title = State()
    waiting_for_author = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_category = State()
    waiting_for_cover_photo = State()
    waiting_for_page_photos = State()
    confirming = State()


@router.callback_query(F.data == "admin_add_book")
async def start_add_book(callback: CallbackQuery, state: FSMContext):
    """Начало процесса добавления книги"""
    await state.clear()
    await state.update_data(step=0, page_photos=[])
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    
    await callback.message.edit_message_text(
        "📚 <b>Добавление новой книги</b>\n\n"
        "Введите название книги:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(BookAddState.waiting_for_title)


@router.message(BookAddState.waiting_for_title)
async def process_title(message: Message, state: FSMContext):
    """Обработка названия книги"""
    if message.text and message.text.strip():
        await state.update_data(title=message.text.strip(), step=1)
        
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="admin_book_back_title")
        builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
        builder.adjust(1)
        
        await message.answer(
            "✍️ Введите автора книги:",
            reply_markup=builder.as_markup()
        )
        await state.set_state(BookAddState.waiting_for_author)
    else:
        await message.answer("❌ Название не может быть пустым. Попробуйте еще раз:")


@router.callback_query(F.data == "admin_book_back_title")
async def back_to_title(callback: CallbackQuery, state: FSMContext):
    """Возврат к вводу названия"""
    await state.set_state(BookAddState.waiting_for_title)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    
    await callback.message.edit_message_text(
        "📚 <b>Добавление новой книги</b>\n\n"
        "Введите название книги:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@router.message(BookAddState.waiting_for_author)
async def process_author(message: Message, state: FSMContext):
    """Обработка автора книги"""
    if message.text and message.text.strip():
        await state.update_data(author=message.text.strip(), step=2)
        
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="admin_book_back_author")
        builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
        builder.adjust(1)
        
        await message.answer(
            "📝 Введите описание книги:",
            reply_markup=builder.as_markup()
        )
        await state.set_state(BookAddState.waiting_for_description)
    else:
        await message.answer("❌ Автор не может быть пустым. Попробуйте еще раз:")


@router.callback_query(F.data == "admin_book_back_author")
async def back_to_author(callback: CallbackQuery, state: FSMContext):
    """Возврат к вводу автора"""
    data = await state.get_data()
    title = data.get('title', 'Неизвестно')
    
    await state.set_state(BookAddState.waiting_for_author)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_title")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(1)
    
    await callback.message.edit_message_text(
        f"📚 <b>{title}</b>\n\n"
        "✍️ Введите автора книги:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@router.message(BookAddState.waiting_for_description)
async def process_description(message: Message, state: FSMContext):
    """Обработка описания книги"""
    if message.text and message.text.strip():
        await state.update_data(description=message.text.strip(), step=3)
        
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="admin_book_back_description")
        builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
        builder.adjust(1)
        
        await message.answer(
            "💰 Введите цену книги (в рублях):",
            reply_markup=builder.as_markup()
        )
        await state.set_state(BookAddState.waiting_for_price)
    else:
        await message.answer("❌ Описание не может быть пустым. Попробуйте еще раз:")


@router.callback_query(F.data == "admin_book_back_description")
async def back_to_description(callback: CallbackQuery, state: FSMContext):
    """Возврат к вводу описания"""
    data = await state.get_data()
    title = data.get('title', 'Неизвестно')
    author = data.get('author', 'Неизвестно')
    
    await state.set_state(BookAddState.waiting_for_description)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_author")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(1)
    
    await callback.message.edit_message_text(
        f"📚 <b>{title}</b>\n"
        f"✍️ Автор: {author}\n\n"
        "📝 Введите описание книги:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@router.message(BookAddState.waiting_for_price)
async def process_price(message: Message, state: FSMContext):
    """Обработка цены книги"""
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError
        
        await state.update_data(price=price, step=4)
        
        # Получаем категории
        categories = await get_all_categories()
        
        builder = InlineKeyboardBuilder()
        for cat in categories:
            builder.button(text=f"📁 {cat['name']}", callback_data=f"admin_book_cat_{cat['id']}")
        builder.button(text="⬅️ Назад", callback_data="admin_book_back_price")
        builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
        builder.adjust(2)
        
        await message.answer(
            "📂 Выберите категорию для книги:",
            reply_markup=builder.as_markup()
        )
        await state.set_state(BookAddState.waiting_for_category)
    except (ValueError, TypeError):
        await message.answer("❌ Введите корректную цену (положительное число):")


@router.callback_query(F.data == "admin_book_back_price")
async def back_to_price(callback: CallbackQuery, state: FSMContext):
    """Возврат к вводу цены"""
    data = await state.get_data()
    title = data.get('title', 'Неизвестно')
    author = data.get('author', 'Неизвестно')
    description = data.get('description', 'Нет описания')
    
    await state.set_state(BookAddState.waiting_for_price)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_description")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(1)
    
    await callback.message.edit_message_text(
        f"📚 <b>{title}</b>\n"
        f"✍️ Автор: {author}\n"
        f"📝 {description[:100]}...\n\n"
        "💰 Введите цену книги (в рублях):",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("admin_book_cat_"))
async def process_category(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора категории"""
    category_id = int(callback.data.split("_")[-1])
    await state.update_data(category_id=category_id, step=5)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_category")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(1)
    
    await callback.message.edit_message_text(
        "📸 <b>Отправьте фото обложки книги</b>\n\n"
        "Это главное изображение, которое будет видно в каталоге.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(BookAddState.waiting_for_cover_photo)


@router.callback_query(F.data == "admin_book_back_category")
async def back_to_category(callback: CallbackQuery, state: FSMContext):
    """Возврат к выбору категории"""
    categories = await get_all_categories()
    
    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=f"📁 {cat['name']}", callback_data=f"admin_book_cat_{cat['id']}")
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_price")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(2)
    
    await callback.message.edit_message_text(
        "📂 Выберите категорию для книги:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(BookAddState.waiting_for_category)


@router.message(BookAddState.waiting_for_cover_photo, F.photo)
async def process_cover_photo(message: Message, state: FSMContext):
    """Обработка фото обложки"""
    photo = message.photo[-1]
    await state.update_data(cover_photo_id=photo.file_id, step=6)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить фото страниц", callback_data="admin_book_add_pages")
    builder.button(text="⏭️ Пропустить", callback_data="admin_book_skip_pages")
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_cover")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(2)
    
    await message.answer(
        "📸 <b>Фото обложки получено!</b>\n\n"
        "Хотите добавить фото страниц книги?\n"
        "Это поможет покупателям лучше рассмотреть товар.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(BookAddState.waiting_for_page_photos)


@router.callback_query(F.data == "admin_book_back_cover")
async def back_to_cover(callback: CallbackQuery, state: FSMContext):
    """Возврат к загрузке обложки"""
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_category")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(1)
    
    await callback.message.edit_message_text(
        "📸 <b>Отправьте фото обложки книги</b>\n\n"
        "Это главное изображение, которое будет видно в каталоге.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(BookAddState.waiting_for_cover_photo)


@router.callback_query(F.data == "admin_book_add_pages")
async def start_add_pages(callback: CallbackQuery, state: FSMContext):
    """Начало добавления фото страниц"""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готово", callback_data="admin_book_pages_done")
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_cover")
    builder.adjust(1)
    
    await callback.message.edit_message_text(
        "📸 <b>Добавление фото страниц</b>\n\n"
        "Отправляйте фото страниц по одному.\n"
        "Когда закончите, нажмите 'Готово'.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@router.message(BookAddState.waiting_for_page_photos, F.photo)
async def process_page_photo(message: Message, state: FSMContext):
    """Обработка фото страницы"""
    data = await state.get_data()
    page_photos = data.get('page_photos', [])
    
    photo = message.photo[-1]
    page_photos.append(photo.file_id)
    await state.update_data(page_photos=page_photos)
    
    count = len(page_photos)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готово", callback_data="admin_book_pages_done")
    builder.button(text="➕ Еще фото", callback_data="admin_book_add_more_pages")
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_cover")
    builder.adjust(2)
    
    await message.answer(
        f"✅ <b>Фото #{count} добавлено!</b>\n\n"
        f"Всего фото страниц: {count}\n\n"
        "Добавить еще или завершить?",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_book_add_more_pages")
async def add_more_pages(callback: CallbackQuery, state: FSMContext):
    """Продолжение добавления фото страниц"""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готово", callback_data="admin_book_pages_done")
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_cover")
    builder.adjust(1)
    
    await callback.message.edit_message_text(
        "📸 <b>Отправьте следующее фото страницы</b>\n\n"
        "Когда закончите, нажмите 'Готово'.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_book_skip_pages")
@router.callback_query(F.data == "admin_book_pages_done")
async def finish_pages(callback: CallbackQuery, state: FSMContext):
    """Завершение добавления фото и переход к подтверждению"""
    data = await state.get_data()
    page_photos = data.get('page_photos', [])
    
    # Формируем итоговое сообщение
    cover_photo_id = data.get('cover_photo_id')
    title = data.get('title')
    author = data.get('author')
    description = data.get('description')
    price = data.get('price')
    category_id = data.get('category_id')
    
    # Получаем название категории
    categories = await get_all_categories()
    category_name = next((cat['name'] for cat in categories if cat['id'] == category_id), "Неизвестно")
    
    text = (
        f"📚 <b>Подтверждение добавления книги</b>\n\n"
        f"📖 Название: {title}\n"
        f"✍️ Автор: {author}\n"
        f"📝 Описание: {description[:200]}{'...' if len(description) > 200 else ''}\n"
        f"💰 Цена: {price} ₽\n"
        f"📁 Категория: {category_name}\n"
        f"📸 Фото обложки: ✅\n"
        f"📄 Фото страниц: {len(page_photos)} шт.\n\n"
        "Все верно?"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data="admin_book_confirm_add")
    builder.button(text="✏️ Изменить", callback_data="admin_book_back_title")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(1)
    
    # Если есть фото обложки, отправляем с ним
    if cover_photo_id:
        await callback.message.delete()
        await message.answer_photo(
            photo=FSInputFile(cover_photo_id) if cover_photo_id.startswith('/') else cover_photo_id,
            caption=text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_message_text(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    
    await state.set_state(BookAddState.confirming)


@router.callback_query(F.data == "admin_book_confirm_add")
async def confirm_add_book(callback: CallbackQuery, state: FSMContext):
    """Подтверждение и добавление книги в БД"""
    data = await state.get_data()
    
    try:
        # Добавляем книгу в БД
        book_id = await add_book(
            title=data['title'],
            author=data['author'],
            description=data['description'],
            price=data['price'],
            category_id=data['category_id'],
            cover_photo=data.get('cover_photo_id'),
            page_photos=data.get('page_photos', [])
        )
        
        await callback.message.edit_message_text(
            f"✅ <b>Книга успешно добавлена!</b>\n\n"
            f"ID: {book_id}\n"
            f"Название: {data['title']}",
            parse_mode="HTML"
        )
        
        logger.info(f"Книга '{data['title']}' добавлена админом {callback.from_user.id}")
    except Exception as e:
        logger.error(f"Ошибка при добавлении книги: {e}")
        await callback.message.edit_message_text(
            f"❌ <b>Ошибка при добавлении книги</b>\n\n"
            f"{str(e)}",
            parse_mode="HTML"
        )
    
    await state.clear()


@router.callback_query(F.data == "admin_books_cancel")
async def cancel_add_book(callback: CallbackQuery, state: FSMContext):
    """Отмена добавления книги"""
    await state.clear()
    
    builder = InlineKeyboardBuilder()
    builder.button(text="📚 Управление книгами", callback_data="admin_books_menu")
    builder.button(text="🔙 В меню админа", callback_data="admin_menu")
    builder.adjust(1)
    
    await callback.message.edit_message_text(
        "❌ <b>Добавление книги отменено</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_books_menu")
async def books_menu(callback: CallbackQuery):
    """Меню управления книгами"""
    books = await get_all_books()
    
    text = "📚 <b>Управление книгами</b>\n\n"
    if books:
        text += f"Всего книг: {len(books)}\n\n"
        for book in books[:5]:  # Показываем первые 5
            text += f"📖 {book['title']} - {book['price']} ₽\n"
        if len(books) > 5:
            text += f"... и еще {len(books) - 5} книг\n"
    else:
        text += "Пока нет добавленных книг."
    
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить книгу", callback_data="admin_add_book")
    builder.button(text="🔙 В меню админа", callback_data="admin_menu")
    builder.adjust(1)
    
    await callback.message.edit_message_text(
        text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
