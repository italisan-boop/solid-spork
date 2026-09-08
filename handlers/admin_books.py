from aiogram import Bot, Router, F
from aiogram.types import CallbackQuery, Message, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
import logging
import json
from config.settings import settings
from db.books import add_book, get_all_books, update_book, update_book_full, delete_book, get_book, get_books_count, get_all_books_paginated
from db.categories import get_all_categories
from utils import parseBookImages
from states import EditBookState, AddBookState as BookAddState
import re

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 20  # Количество книг на странице


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором"""
    return user_id in settings.ADMIN_IDS


def is_url(text: str) -> bool:
    """Проверяет, является ли строка URL.
    Достаточно наличия схемы http(s):// и непустого хоста — расширения
    TLD растут быстрее, чем старые регексы успевают покрывать, поэтому
    строгая валидация TLD только мешает."""
    if not text:
        return False
    text = text.strip()
    return re.match(r'^https?://[^\s]+$', text, re.IGNORECASE) is not None


async def get_telegram_file_url(bot: Bot, file_id: str) -> str:
    """Строит публичный URL файла на api.telegram.org по его file_id.
    file_unique_id — это стабильный идентификатор, его НЕЛЬЗЯ подставлять
    в путь; нужен реальный file_path, который возвращает get_file()."""
    file = await bot.get_file(file_id)
    return f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"


@router.callback_query(F.data == "admin_add_book")
async def start_add_book(callback: CallbackQuery, state: FSMContext):
    """Начало процесса добавления книги"""
    await state.clear()
    await state.update_data(step=0, page_photos=[])
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=(
            "📚 <b>Добавление новой книги</b>\n\n"
            "Введите название книги:"
        ),
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
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=(
            "📚 <b>Добавление новой книги</b>\n\n"
            "Введите название книги:"
        ),
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
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=
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
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=
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
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=
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
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=
        "📸 <b>Отправьте обложку книги</b>\n\n"
        "Можно прикрепить фото Telegram-сообщением или прислать ссылку "
        "(http://… или https://…). Это главное изображение в каталоге.",
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
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=
        "📂 Выберите категорию для книги:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(BookAddState.waiting_for_category)


@router.message(BookAddState.waiting_for_cover_photo, F.photo)
async def process_cover_photo(message: Message, state: FSMContext, bot: Bot):
    """Обработка фото обложки (вложение Telegram)"""
    photo = message.photo[-1]
    # Сохраняем file_id для отправки в Mini App и URL для хранения в БД
    file_id = photo.file_id
    try:
        file_url = await get_telegram_file_url(bot, file_id)
    except Exception as e:
        logger.error(f"Не удалось получить file_path для обложки: {e}")
        # Без URL обложка всё равно будет работать в боте (через file_id),
        # но в Mini App может не отображаться. Сообщаем админу.
        await message.answer(
            "⚠️ Не удалось получить ссылку на фото. Попробуйте отправить "
            "обложку ещё раз или пришлите URL изображения."
        )
        return
    await state.update_data(cover_photo=file_url, cover_photo_id=file_id, step=6)
    
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


@router.message(BookAddState.waiting_for_cover_photo)
async def process_cover_photo_url(message: Message, state: FSMContext):
    """Обработка URL обложки"""
    text = message.text.strip() if message.text else ""
    if is_url(text):
        await state.update_data(cover_photo=text, step=6)
        
        builder = InlineKeyboardBuilder()
        builder.button(text="➕ Добавить фото страниц", callback_data="admin_book_add_pages")
        builder.button(text="⏭️ Пропустить", callback_data="admin_book_skip_pages")
        builder.button(text="⬅️ Назад", callback_data="admin_book_back_cover")
        builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
        builder.adjust(2)
        
        await message.answer(
            "📸 <b>URL обложки принят!</b>\n\n"
            "Хотите добавить фото страниц книги?\n"
            "Это поможет покупателям лучше рассмотреть товар.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.set_state(BookAddState.waiting_for_page_photos)
    else:
        await message.answer(
            "❌ Это не похоже на valid URL. Пожалуйста, отправьте фото или введите корректный URL:"
        )


@router.callback_query(F.data == "admin_book_back_cover")
async def back_to_cover(callback: CallbackQuery, state: FSMContext):
    """Возврат к загрузке обложки"""
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_category")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(1)
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=
        "📸 <b>Отправьте обложку книги</b>\n\n"
        "Можно прикрепить фото Telegram-сообщением или прислать ссылку "
        "(http://… или https://…). Это главное изображение в каталоге.",
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
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=
        "📸 <b>Добавление фото страниц</b>\n\n"
        "Отправляйте фото страниц по одному.\n"
        "Когда закончите, нажмите 'Готово'.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@router.message(BookAddState.waiting_for_page_photos, F.photo)
async def process_page_photo(message: Message, state: FSMContext, bot: Bot):
    """Обработка фото страницы (вложение Telegram)"""
    data = await state.get_data()
    page_photos = data.get('page_photos', [])

    photo = message.photo[-1]
    try:
        file_url = await get_telegram_file_url(bot, photo.file_id)
    except Exception as e:
        logger.error(f"Не удалось получить file_path для фото страницы: {e}")
        await message.answer(
            "⚠️ Не удалось сохранить фото страницы. Попробуйте ещё раз или пришлите URL."
        )
        return
    page_photos.append(file_url)
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


@router.message(BookAddState.waiting_for_page_photos)
async def process_page_photo_url(message: Message, state: FSMContext):
    """Обработка URL фото страницы"""
    text = message.text.strip() if message.text else ""
    if is_url(text):
        data = await state.get_data()
        page_photos = data.get('page_photos', [])
        page_photos.append(text)
        await state.update_data(page_photos=page_photos)
        
        count = len(page_photos)
        
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Готово", callback_data="admin_book_pages_done")
        builder.button(text="➕ Еще фото", callback_data="admin_book_add_more_pages")
        builder.button(text="⬅️ Назад", callback_data="admin_book_back_cover")
        builder.adjust(2)
        
        await message.answer(
            f"✅ <b>URL фото #{count} добавлен!</b>\n\n"
            f"Всего фото страниц: {count}\n\n"
            "Добавить еще или завершить?",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "❌ Это не похоже на valid URL. Пожалуйста, отправьте фото или введите корректный URL:"
        )


@router.callback_query(F.data == "admin_book_add_more_pages")
async def add_more_pages(callback: CallbackQuery, state: FSMContext):
    """Продолжение добавления фото страниц"""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готово", callback_data="admin_book_pages_done")
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_cover")
    builder.adjust(1)
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=
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
    cover_photo_url = data.get('cover_photo')
    has_cover = bool(cover_photo_id or cover_photo_url)
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
        f"📸 Фото обложки: {'✅' if has_cover else '❌'}\n"
        f"📄 Фото страниц: {len(page_photos)} шт.\n\n"
        "Все верно?"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data="admin_book_confirm_add")
    builder.button(text="✏️ Изменить", callback_data="admin_book_back_title")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(1)

    try:
        # Показываем обложку независимо от того, как её прислали:
        # Telegram file_id (attachment) — бот пересылает по file_id.
        # Внешний URL — Telegram сам подгружает картинку и шлёт превью.
        cover_to_show = cover_photo_id or cover_photo_url
        if cover_to_show:
            await callback.message.delete()
            await callback.message.answer_photo(
                photo=cover_to_show,
                caption=text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        else:
            await callback.bot.edit_message_text(
                chat_id=callback.from_user.id,
                message_id=callback.message.message_id,
                text=text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )

        await state.set_state(BookAddState.confirming)
    except Exception as e:
        logger.error(f"Ошибка при отображении подтверждения: {e}")
        await callback.message.answer("❌ Произошла ошибка. Попробуйте еще раз.")
        await state.clear()


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
            cover_photo=data.get('cover_photo'),  # URL или None
            page_photos=data.get('page_photos', [])
        )
        
        # Отправляем сообщение об успехе с кнопкой возврата в админ-панель
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 В админ-панель", callback_data="admin_menu")
        
        await callback.message.answer(
            f"✅ <b>Книга успешно добавлена!</b>\n\n"
            f"ID: {book_id}\n"
            f"Название: {data['title']}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        
        logger.info(f"Книга '{data['title']}' добавлена админом {callback.from_user.id}")
    except Exception as e:
        logger.error(f"Ошибка при добавлении книги: {e}")
        
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 В админ-панель", callback_data="admin_menu")
        
        await callback.message.answer(
            f"❌ <b>Ошибка при добавлении книги</b>\n\n"
            f"{str(e)}",
            reply_markup=builder.as_markup(),
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
    
    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=
        "❌ <b>Добавление книги отменено</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "admin_books_menu")
async def books_menu(callback: CallbackQuery, state: FSMContext):
    """Меню управления книгами с пагинацией"""
    await state.clear()
    
    # Получаем общее количество книг
    total_books = await get_books_count()
    total_pages = (total_books + PAGE_SIZE - 1) // PAGE_SIZE if total_books > 0 else 1
    
    # Сохраняем текущую страницу в состоянии
    await state.update_data(current_page=0, total_pages=total_pages)
    
    await show_books_list(callback, 0)


async def show_books_list(callback: CallbackQuery, page: int):
    """Отображение списка книг с пагинацией"""
    offset = page * PAGE_SIZE
    books = await get_all_books_paginated(limit=PAGE_SIZE, offset=offset)
    total_books = await get_books_count()
    total_pages = (total_books + PAGE_SIZE - 1) // PAGE_SIZE if total_books > 0 else 1
    
    text = f"📚 <b>Управление книгами</b>\n\n"
    text += f"Страница {page + 1} из {total_pages}\n"
    text += f"Всего книг: {total_books}\n\n"
    
    if books:
        for book in books:
            emoji = book.get('category_emoji') or '📖'
            text += f"{emoji} <b>{book['title']}</b> - {book['price']} ₽\n"
    else:
        text += "Пока нет добавленных книг."
    
    builder = InlineKeyboardBuilder()
    
    if books:
        # Кнопки для каждой книги на странице
        for book in books:
            builder.button(text=f"📝 {book['title']}", callback_data=f"admin_book_edit_{book['id']}")
        
        # Навигация
        nav_buttons = []
        if page > 0:
            nav_buttons.append(("⬅️ Назад", f"admin_books_page_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(("➡️ Вперед", f"admin_books_page_{page + 1}"))
        
        for btn_text, btn_data in nav_buttons:
            builder.button(text=btn_text, callback_data=btn_data)
    
    builder.button(text="➕ Добавить книгу", callback_data="admin_add_book")
    builder.button(text="🔙 В меню админа", callback_data="admin_menu")
    builder.adjust(1)
    
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")


@router.callback_query(F.data.startswith("admin_books_page_"))
async def books_page_navigation(callback: CallbackQuery, state: FSMContext):
    """Навигация по страницам списка книг"""
    page = int(callback.data.split("_")[-1])
    await show_books_list(callback, page)


@router.callback_query(F.data.regexp(r"^admin_book_edit_\d+$"))
async def edit_book_menu(callback: CallbackQuery, state: FSMContext):
    """Меню редактирования конкретной книги.
    Узкий regex, чтобы не перехватывать admin_book_edit_title_/author_/desc_/price_{id}."""
    # Очищаем состояние перед показом меню
    await state.clear()
    
    book_id = int(callback.data.split("_")[-1])
    book = await get_book(book_id)
    
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return
    
    text = (
        f"📚 <b>Редактирование книги</b>\n\n"
        f"ID: {book['id']}\n"
        f"📖 Название: {book['title']}\n"
        f"✍️ Автор: {book.get('author', 'Не указан')}\n"
        f"💰 Цена: {book['price']} ₽\n"
        f"📝 Описание: {book.get('description', 'Нет описания')[:100]}{'...' if len(book.get('description', '')) > 100 else ''}\n\n"
        "Выберите действие:"
    )
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить данные", callback_data=f"admin_book_change_{book_id}")
    builder.button(text="🗑️ Удалить книгу", callback_data=f"admin_book_delete_{book_id}")
    builder.button(text="🔙 Назад к списку", callback_data="admin_books_menu")
    builder.adjust(1)
    
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")
        # Если не удалось отредактировать, отправляем новое сообщение
        await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


# ============================================
# ИЗМЕНЕНИЕ КАТЕГОРИИ КНИГИ
#
# ВАЖНО: эти хендлеры должны быть зарегистрированы ДО start_change_book
# ниже, потому что admin_book_change_<id> начинается с admin_book_change_,
# и startswith-prefix-matching в aiogram матчит более ранний обработчик
# первым — иначе клик по 'Изменить категорию' будет уводить обратно в
# меню изменения книги.
# ============================================

@router.callback_query(F.data.startswith("admin_book_change_category_"))
async def admin_book_change_category(callback: CallbackQuery, state: FSMContext):
    """Меню выбора категории: существующие + 'ввести новую' + 'назад'."""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    book_id = int(callback.data.rsplit("_", 1)[-1])
    book = await get_book(book_id)
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    await state.update_data(edit_book_id=book_id)

    categories = await db.get_all_categories()

    builder = InlineKeyboardBuilder()
    if categories:
        for cat in categories:
            builder.button(
                text=f"{cat['emoji'] or ''} {cat['name']}".strip(),
                callback_data=f"admin_book_set_category_{cat['id']}",
            )
    builder.button(
        text="✏️ Ввести новую",
        callback_data="admin_book_set_category_custom",
    )
    builder.button(
        text="◀️ Назад",
        callback_data=f"admin_book_change_{book_id}",
    )
    builder.adjust(2)

    text = (
        f"📂 <b>Изменение категории</b>\n\n"
        f"📖 Текущая категория: <b>{book.get('category') or '—'}</b>\n\n"
        f"Выберите новую категорию из списка или введите свою:"
    )

    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Ошибка при показе меню категорий: %s", e)
        await callback.message.answer(
            text, reply_markup=builder.as_markup(), parse_mode="HTML"
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_book_set_category_"))
async def admin_book_set_category(callback: CallbackQuery, state: FSMContext):
    """Применяет выбранную существующую категорию ИЛИ просит ввести новую.

    `_custom` ветка переходит в FSM; остальные — это `admin_book_set_category_<id>`.
    """
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    data = await state.get_data()
    book_id = data.get('edit_book_id')
    if not book_id:
        await callback.answer("❌ Ошибка: книга не выбрана", show_alert=True)
        return

    # Ветка «ввести новую» — запрашиваем имя в FSM.
    if callback.data == "admin_book_set_category_custom":
        await state.set_state(EditBookState.waiting_for_new_category_admin)

        builder = InlineKeyboardBuilder()
        builder.button(
            text="◀️ Назад",
            callback_data=f"admin_book_change_category_{book_id}",
        )

        try:
            await callback.bot.edit_message_text(
                chat_id=callback.from_user.id,
                message_id=callback.message.message_id,
                text=(
                    "📂 Введите <b>название новой категории</b>:\n\n"
                    "Если категория с таким именем уже существует — будет использована она."
                ),
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Ошибка при запросе новой категории: %s", e)
            await callback.message.answer(
                "📂 Введите название новой категории:",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
        await callback.answer()
        return

    # Ветка «существующая категория».
    try:
        cat_id = int(callback.data.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    categories = await db.get_all_categories()
    cat = next((c for c in categories if c['id'] == cat_id), None)
    if not cat:
        await callback.answer("❌ Категория не найдена", show_alert=True)
        return

    await update_book_full(book_id, category=cat['name'], category_id=cat_id)
    await state.clear()

    book = await get_book(book_id)
    title = book['title'] if book else "Книга"

    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔙 Назад к книге", callback_data=f"admin_book_edit_{book_id}"
    )

    await callback.message.answer(
        f"✅ <b>Категория обновлена!</b>\n\n"
        f"📖 {title}\n"
        f"📂 Новая категория: <b>{cat['emoji'] or ''} {cat['name']}</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(EditBookState.waiting_for_new_category_admin)
async def process_new_category_admin(message: Message, state: FSMContext):
    """Применяет введённую категорию: переиспользует существующую или создаёт новую."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет прав")
        await state.clear()
        return

    data = await state.get_data()
    book_id = data.get('edit_book_id')
    new_category_name = message.text.strip()

    if not book_id:
        await message.answer("❌ Ошибка: книга не выбрана")
        await state.clear()
        return

    if not new_category_name:
        await message.answer("❌ Название не может быть пустым. Попробуйте ещё раз:")
        return

    categories = await db.get_all_categories()
    existing_cat = next(
        (c for c in categories if c['name'].lower() == new_category_name.lower()),
        None,
    )

    if existing_cat:
        await update_book_full(
            book_id,
            category=existing_cat['name'],
            category_id=existing_cat['id'],
        )
        cat_label = f"{existing_cat['emoji'] or ''} {existing_cat['name']}".strip()
        response_text = (
            f"✅ <b>Категория найдена и применена!</b>\n\n"
            f"📂 {cat_label}"
        )
    else:
        new_cat_id = await db.add_category(new_category_name, "")
        await update_book_full(
            book_id,
            category=new_category_name,
            category_id=new_cat_id,
        )
        response_text = (
            f"✅ <b>Новая категория создана и применена!</b>\n\n"
            f"📂 {new_category_name}\n\n"
            f"💡 Вы можете добавить эмодзи для неё через '📂 Управление категориями'"
        )

    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔙 Назад к книге", callback_data=f"admin_book_edit_{book_id}"
    )

    await message.answer(
        response_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await state.clear()


@router.callback_query(F.data.startswith("admin_book_change_"))
async def start_change_book(callback: CallbackQuery, state: FSMContext):
    """Начало изменения книги"""
    book_id = int(callback.data.split("_")[-1])
    book = await get_book(book_id)
    
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return
    
    await state.update_data(edit_book_id=book_id)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить название", callback_data=f"admin_book_edit_title_{book_id}")
    builder.button(text="✏️ Изменить автора", callback_data=f"admin_book_edit_author_{book_id}")
    builder.button(text="✏️ Изменить описание", callback_data=f"admin_book_edit_desc_{book_id}")
    builder.button(text="✏️ Изменить цену", callback_data=f"admin_book_edit_price_{book_id}")
    builder.button(text="🖼️ Изменить обложку", callback_data=f"admin_book_edit_cover_{book_id}")
    builder.button(text="📄 Изменить фото страниц", callback_data=f"admin_book_edit_pages_{book_id}")
    builder.button(text="📂 Изменить категорию", callback_data=f"admin_book_change_category_{book_id}")
    builder.button(text="🔙 Назад к списку", callback_data="admin_books_menu")
    builder.adjust(1)
    
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text=f"📚 <b>Изменение книги: {book['title']}</b>\n\nВыберите поле для редактирования:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")
        await callback.message.answer(
            f"📚 <b>Изменение книги: {book['title']}</b>\n\nВыберите поле для редактирования:",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("admin_book_edit_title_"))
async def edit_book_title(callback: CallbackQuery, state: FSMContext):
    """Изменение названия книги"""
    book_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_book_id=book_id)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"admin_book_edit_{book_id}")
    
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="✏️ Введите новое название книги:",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")
        await callback.message.answer("✏️ Введите новое название книги:", reply_markup=builder.as_markup())
    
    await state.set_state(EditBookState.waiting_for_new_title)


@router.message(EditBookState.waiting_for_new_title)
async def process_new_title(message: Message, state: FSMContext):
    """Обработка нового названия"""
    if message.text and message.text.strip():
        data = await state.get_data()
        book_id = data.get('edit_book_id')
        
        await update_book(book_id, title=message.text.strip())
        
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад к книге", callback_data=f"admin_book_edit_{book_id}")
        
        await message.answer(
            f"✅ Название книги изменено на: {message.text.strip()}",
            reply_markup=builder.as_markup()
        )
        await state.clear()
    else:
        await message.answer("❌ Название не может быть пустым. Попробуйте еще раз:")


@router.callback_query(F.data.startswith("admin_book_edit_author_"))
async def edit_book_author(callback: CallbackQuery, state: FSMContext):
    """Изменение автора книги"""
    book_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_book_id=book_id)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"admin_book_edit_{book_id}")
    
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="✏️ Введите нового автора:",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")
        await callback.message.answer("✏️ Введите нового автора:", reply_markup=builder.as_markup())
    
    await state.set_state(EditBookState.waiting_for_new_author)


@router.message(EditBookState.waiting_for_new_author)
async def process_new_author(message: Message, state: FSMContext):
    """Обработка нового автора"""
    if message.text and message.text.strip():
        data = await state.get_data()
        book_id = data.get('edit_book_id')
        
        await update_book(book_id, author=message.text.strip())
        
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад к книге", callback_data=f"admin_book_edit_{book_id}")
        
        await message.answer(
            f"✅ Автор изменен на: {message.text.strip()}",
            reply_markup=builder.as_markup()
        )
        await state.clear()
    else:
        await message.answer("❌ Автор не может быть пустым. Попробуйте еще раз:")


@router.callback_query(F.data.startswith("admin_book_edit_desc_"))
async def edit_book_description(callback: CallbackQuery, state: FSMContext):
    """Изменение описания книги"""
    book_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_book_id=book_id)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"admin_book_edit_{book_id}")
    
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="📝 Введите новое описание книги:",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")
        await callback.message.answer("📝 Введите новое описание книги:", reply_markup=builder.as_markup())
    
    await state.set_state(EditBookState.waiting_for_new_description)


@router.message(EditBookState.waiting_for_new_description)
async def process_new_description(message: Message, state: FSMContext):
    """Обработка нового описания"""
    if message.text and message.text.strip():
        data = await state.get_data()
        book_id = data.get('edit_book_id')
        
        await update_book(book_id, description=message.text.strip())
        
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад к книге", callback_data=f"admin_book_edit_{book_id}")
        
        await message.answer(
            "✅ Описание книги изменено",
            reply_markup=builder.as_markup()
        )
        await state.clear()
    else:
        await message.answer("❌ Описание не может быть пустым. Попробуйте еще раз:")


@router.callback_query(F.data.startswith("admin_book_edit_price_"))
async def edit_book_price(callback: CallbackQuery, state: FSMContext):
    """Изменение цены книги"""
    book_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_book_id=book_id)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"admin_book_edit_{book_id}")
    
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="💰 Введите новую цену (в рублях):",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")
        await callback.message.answer("💰 Введите новую цену (в рублях):", reply_markup=builder.as_markup())
    
    await state.set_state(EditBookState.waiting_for_new_price)


@router.message(EditBookState.waiting_for_new_price)
async def process_new_price(message: Message, state: FSMContext):
    """Обработка новой цены"""
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError
        
        data = await state.get_data()
        book_id = data.get('edit_book_id')
        
        await update_book(book_id, price=price)
        
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад к книге", callback_data=f"admin_book_edit_{book_id}")
        
        await message.answer(
            f"✅ Цена изменена на: {price} ₽",
            reply_markup=builder.as_markup()
        )
        await state.clear()
    except (ValueError, TypeError):
        await message.answer("❌ Введите корректную цену (положительное число):")


# ============================================
# РЕДАКТИРОВАНИЕ ОБЛОЖКИ
# ============================================

@router.callback_query(F.data.regexp(r"^admin_book_edit_cover_\d+$"))
async def edit_book_cover(callback: CallbackQuery, state: FSMContext):
    """Показывает текущую обложку и предлагает прислать новую."""
    book_id = int(callback.data.split("_")[-1])
    book = await get_book(book_id)

    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    await state.update_data(edit_book_id=book_id)
    await state.set_state(EditBookState.waiting_for_new_cover)

    current = book.get("cover_photo") or book.get("emoji") or ""
    if current.startswith("http"):
        preview_text = "🖼 <b>Текущая обложка:</b> картинка по ссылке"
    elif current:
        preview_text = f"🖼 <b>Текущая обложка:</b> {current}"
    else:
        preview_text = "🖼 <b>Текущая обложка:</b> не задана"

    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Удалить обложку", callback_data=f"admin_book_clear_cover_{book_id}")
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    builder.adjust(1)

    try:
        await callback.message.delete()
        if current.startswith("http"):
            await callback.message.answer_photo(
                photo=current,
                caption=(
                    f"{preview_text}\n\n"
                    "📸 <b>Пришлите новую обложку</b>\n\n"
                    "Можно приложить фото Telegram-сообщением или прислать ссылку http(s)://…"
                ),
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
        else:
            await callback.message.answer(
                f"{preview_text}\n\n"
                "📸 <b>Пришлите новую обложку</b>\n\n"
                "Можно приложить фото Telegram-сообщением или прислать ссылку http(s)://…",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"Ошибка при показе обложки: {e}")
        await callback.message.answer(
            "📸 <b>Пришлите новую обложку</b>\n\n"
            "Можно приложить фото Telegram-сообщением или прислать ссылку http(s)://…",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(EditBookState.waiting_for_new_cover, F.photo)
async def process_new_cover_photo(message: Message, state: FSMContext, bot: Bot):
    """Приём обложки как Telegram-вложения."""
    data = await state.get_data()
    book_id = data.get("edit_book_id")
    if not book_id:
        await message.answer("❌ Ошибка: книга не выбрана")
        await state.clear()
        return

    photo = message.photo[-1]
    try:
        file_url = await get_telegram_file_url(bot, photo.file_id)
    except Exception as e:
        logger.error(f"Не удалось получить file_path для обложки: {e}")
        await message.answer(
            "⚠️ Не удалось сохранить фото. Попробуйте ещё раз или пришлите URL."
        )
        return

    # cover_photo хранит URL для БД; emoji — поле, которое читает
    # Mini App / catalog.py для отрисовки обложки. Держим их в синхроне.
    await update_book_full(book_id, cover_photo=file_url, emoji=file_url)

    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    await message.answer("✅ Обложка обновлена!", reply_markup=builder.as_markup())
    await state.clear()


@router.message(EditBookState.waiting_for_new_cover)
async def process_new_cover_url(message: Message, state: FSMContext):
    """Приём обложки как URL."""
    data = await state.get_data()
    book_id = data.get("edit_book_id")
    if not book_id:
        await message.answer("❌ Ошибка: книга не выбрана")
        await state.clear()
        return

    text = (message.text or "").strip()
    if not is_url(text):
        await message.answer(
            "❌ Это не похоже на ссылку. Пришлите файл-фото или URL вида https://…"
        )
        return

    await update_book_full(book_id, cover_photo=text, emoji=text)

    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    await message.answer("✅ Обложка обновлена!", reply_markup=builder.as_markup())
    await state.clear()


@router.callback_query(F.data.regexp(r"^admin_book_clear_cover_\d+$"))
async def clear_book_cover(callback: CallbackQuery, state: FSMContext):
    """Сбрасывает обложку книги."""
    book_id = int(callback.data.split("_")[-1])
    await update_book_full(book_id, cover_photo="", emoji="")
    await state.clear()

    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    await callback.message.answer("🗑 Обложка удалена.", reply_markup=builder.as_markup())
    await callback.answer()


# ============================================
# РЕДАКТИРОВАНИЕ ФОТО СТРАНИЦ
# ============================================

@router.callback_query(F.data.regexp(r"^admin_book_edit_pages_\d+$"))
async def edit_book_pages(callback: CallbackQuery, state: FSMContext):
    """Список фото страниц с кнопками удалить / добавить / очистить."""
    book_id = int(callback.data.split("_")[-1])
    book = await get_book(book_id)

    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    images = parseBookImages(book.get("images") or "[]")

    text_lines = [f"📄 <b>Фото страниц книги</b> «{book['title']}»", ""]
    if images:
        text_lines.append(f"Сейчас: {len(images)} шт.")
    else:
        text_lines.append("Пока ни одного фото.")

    builder = InlineKeyboardBuilder()
    if images:
        for idx in range(len(images)):
            builder.button(
                text=f"🗑 Удалить #{idx + 1}",
                callback_data=f"admin_book_page_del_{book_id}_{idx}",
            )
    builder.button(text="➕ Добавить фото", callback_data=f"admin_book_page_add_{book_id}")
    if images:
        builder.button(text="🧹 Очистить все", callback_data=f"admin_book_pages_clear_{book_id}")
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    builder.adjust(1)

    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("\n".join(text_lines), reply_markup=builder.as_markup(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_book_page_add_\d+$"))
async def add_page_start(callback: CallbackQuery, state: FSMContext):
    """Просит прислать новое фото страницы."""
    book_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_book_id=book_id)
    await state.set_state(EditBookState.waiting_for_new_page)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"admin_book_edit_pages_{book_id}")

    await callback.message.answer(
        "📄 <b>Пришлите фото страницы</b>\n\n"
        "Можно приложить фото Telegram-сообщением или прислать ссылку http(s)://…",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(EditBookState.waiting_for_new_page, F.photo)
async def process_new_page_photo(message: Message, state: FSMContext, bot: Bot):
    """Добавляет фото страницы как Telegram-вложение."""
    data = await state.get_data()
    book_id = data.get("edit_book_id")
    if not book_id:
        await message.answer("❌ Ошибка: книга не выбрана")
        await state.clear()
        return

    photo = message.photo[-1]
    try:
        file_url = await get_telegram_file_url(bot, photo.file_id)
    except Exception as e:
        logger.error(f"Не удалось получить file_path для фото страницы: {e}")
        await message.answer("⚠️ Не удалось сохранить фото. Попробуйте ещё раз или пришлите URL.")
        return

    book = await get_book(book_id)
    current_images = parseBookImages(book.get("images") or "[]")
    current_images.append(file_url)
    await update_book_full(book_id, images=json.dumps(current_images))

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Ещё фото", callback_data=f"admin_book_page_add_{book_id}")
    builder.button(text="📄 К списку фото", callback_data=f"admin_book_edit_pages_{book_id}")
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    builder.adjust(1)
    await message.answer(
        f"✅ Фото #{len(current_images)} добавлено. Всего: {len(current_images)}.",
        reply_markup=builder.as_markup(),
    )
    # Состояние оставляем — пользователь может добавить ещё или нажать кнопку.


@router.message(EditBookState.waiting_for_new_page)
async def process_new_page_url(message: Message, state: FSMContext):
    """Добавляет фото страницы как URL."""
    data = await state.get_data()
    book_id = data.get("edit_book_id")
    if not book_id:
        await message.answer("❌ Ошибка: книга не выбрана")
        await state.clear()
        return

    text = (message.text or "").strip()
    if not is_url(text):
        await message.answer(
            "❌ Это не похоже на ссылку. Пришлите фото или URL вида https://…"
        )
        return

    book = await get_book(book_id)
    current_images = parseBookImages(book.get("images") or "[]")
    current_images.append(text)
    await update_book_full(book_id, images=json.dumps(current_images))

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Ещё фото", callback_data=f"admin_book_page_add_{book_id}")
    builder.button(text="📄 К списку фото", callback_data=f"admin_book_edit_pages_{book_id}")
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    builder.adjust(1)
    await message.answer(
        f"✅ Фото #{len(current_images)} добавлено. Всего: {len(current_images)}.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.regexp(r"^admin_book_page_del_\d+_\d+$"))
async def delete_book_page(callback: CallbackQuery, state: FSMContext):
    """Удаляет одну фото страницы по индексу."""
    parts = callback.data.split("_")
    # admin_book_page_del_{book_id}_{idx}
    book_id = int(parts[-2])
    idx = int(parts[-1])

    book = await get_book(book_id)
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    images = parseBookImages(book.get("images") or "[]")
    if 0 <= idx < len(images):
        images.pop(idx)
        await update_book_full(book_id, images=json.dumps(images))
        await callback.answer("✅ Удалено")
    else:
        await callback.answer("❌ Не найдено", show_alert=True)
        return

    # Перерисовываем список фото
    text_lines = [f"📄 <b>Фото страниц книги</b> «{book['title']}»", ""]
    if images:
        text_lines.append(f"Сейчас: {len(images)} шт.")
    else:
        text_lines.append("Пока ни одного фото.")

    builder = InlineKeyboardBuilder()
    if images:
        for i in range(len(images)):
            builder.button(
                text=f"🗑 Удалить #{i + 1}",
                callback_data=f"admin_book_page_del_{book_id}_{i}",
            )
    builder.button(text="➕ Добавить фото", callback_data=f"admin_book_page_add_{book_id}")
    if images:
        builder.button(text="🧹 Очистить все", callback_data=f"admin_book_pages_clear_{book_id}")
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    builder.adjust(1)

    try:
        await callback.message.edit_text("\n".join(text_lines), reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer("\n".join(text_lines), reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.regexp(r"^admin_book_pages_clear_\d+$"))
async def clear_book_pages(callback: CallbackQuery, state: FSMContext):
    """Очищает все фото страниц."""
    book_id = int(callback.data.split("_")[-1])
    await update_book_full(book_id, images="[]")
    await state.clear()
    await callback.answer("🧹 Очищено")

    book = await get_book(book_id)
    title = book['title'] if book else ""

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить фото", callback_data=f"admin_book_page_add_{book_id}")
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    builder.adjust(1)
    await callback.message.answer(
        f"🧹 Все фото страниц книги «{title}» удалены.",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.regexp(r"^admin_book_delete_\d+$"))
async def confirm_delete_book(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления книги.
    Узкий regex, чтобы не перехватывать admin_book_delete_confirm_{id}."""
    # Очищаем состояние перед подтверждением
    await state.clear()
    
    book_id = int(callback.data.split("_")[-1])
    book = await get_book(book_id)
    
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑️ Да, удалить", callback_data=f"admin_book_delete_confirm_{book_id}")
    builder.button(text="❌ Нет, отмена", callback_data=f"admin_book_edit_{book_id}")
    builder.adjust(1)
    
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text=(
                f"⚠️ <b>Удаление книги</b>\n\n"
                f"Вы уверены, что хотите удалить книгу:\n"
                f"📖 {book['title']}\n\n"
                f"Это действие нельзя отменить!"
            ),
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка при редактировании сообщения: {e}")
        # Если не удалось отредактировать, отправляем новое сообщение
        await callback.message.answer(
            f"⚠️ <b>Удаление книги</b>\n\n"
            f"Вы уверены, что хотите удалить книгу:\n"
            f"📖 {book['title']}\n\n"
            f"Это действие нельзя отменить!",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("admin_book_delete_confirm_"))
async def delete_book_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтвержденное удаление книги"""
    # Очищаем состояние перед удалением
    await state.clear()
    
    book_id = int(callback.data.split("_")[-1])
    
    try:
        await delete_book(book_id)
        
        builder = InlineKeyboardBuilder()
        builder.button(text="🔙 Назад к списку книг", callback_data="admin_books_menu")
        
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text=f"✅ Книга успешно удалена",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка при удалении книги: {e}")
        await callback.answer("❌ Ошибка при удалении", show_alert=True)
