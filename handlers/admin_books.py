from aiogram import BaseMiddleware, Bot, Router, F
from aiogram.types import CallbackQuery, Message, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncio
import json
from authz import has_permission_sync
from config.settings import settings
from db.books import add_book, get_all_books, update_book, update_book_full, delete_book, archive_books, reassign_active_books_category, restore_book, get_archived_books, get_archived_books_count, classify_archived_book_ids, purge_archived_books, get_book, get_books_count, get_all_books_paginated, find_book_by_title_author
from db.categories import get_all_categories, add_category, get_category_by_id, category_display, NO_CATEGORY_NAME
from db.inventory import InventoryUnavailableError, set_stock_quantity
from runtime.features import QuotaExceededError
from storage.book_media import (
    BookMediaError,
    attach_telegram_media,
    clear_book_media_sync,
    delete_book_media_sync,
    list_book_media_sync,
    media_variants_for_book_sync,
    next_page_position_sync,
)
from utils import parseBookImages, setup_logger
from utils.otp_confirm import (
    ACTION_PURGE_ARCHIVED_BOOKS,
    OTP_TTL_SECONDS,
    consume_otp,
    issue_otp,
    revoke_otp,
)
from states import EditBookState, AddBookState as BookAddState, AdminBooksState
from html import escape
import re

logger = setup_logger(__name__)
router = Router()

PAGE_SIZE = 10  # Количество книг на странице (было 20; книг стало больше — пора уменьшить)
ARCHIVE_PAGE_SIZE = 8
MAX_BOOK_SELECTION = 100


# Ключи сортировки админского списка книг. Каждому соответствует строка
# ORDER BY в db.books._BOOKS_SORT_ORDERS и подпись в кнопке.
_BOOKS_SORT_LABELS = {
    "default": ("📋 По умолчанию", "Сортировка по умолчанию"),
    "title": ("🔤 По названию", "По алфавиту"),
    "category": ("📂 По категории", "Группировка по категории"),
    "date_new": ("🆕 Сначала новые", "Сначала новые"),
    "date_old": ("📅 Сначала старые", "Сначала старые"),
}

# Лимиты полей книги. Telegram ограничивает caption 1024 символами, а текст
# сообщения бота — 4096 символами; описание в каталоге рендерится без обрезки,
# поэтому держим запас и просим админа уложиться в 3500.
MAX_DESCRIPTION_LENGTH = 3500
# Телеграм нормально показывает превью альбома до 10 фото, но Mini App
# загружает все URL по сети — слишком много = долго и падает на слабом Wi-Fi.
MAX_PAGE_PHOTOS = 20
# Telegram Bot API не принимает файлы больше 10 МБ на одну загрузку.
# Оставляем запас до 9 МБ, чтобы учесть кодеки и обёртку multipart.
MAX_PHOTO_FILE_BYTES = 9 * 1024 * 1024

# Дружелюбный текст ограничений — используется в ошибках валидации.
DESCRIPTION_LIMIT_NOTE = (
    f"\n\n📏 Лимит: не более {MAX_DESCRIPTION_LENGTH} символов "
    f"(сейчас {{current}})."
)
PAGE_PHOTOS_LIMIT_NOTE = (
    f"\n\n📏 Лимит: не более {MAX_PAGE_PHOTOS} фото страниц "
    f"(сейчас {{current}})."
)


def is_admin(user_id: int) -> bool:
    """Catalog mutations remain owner-only."""
    return has_permission_sync(user_id, "catalog.manage")


class AdminBooksMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and not is_admin(user.id):
            state = data.get("state")
            if state:
                await state.clear()
            if isinstance(event, CallbackQuery):
                await event.answer("❌ Нет прав", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("❌ Нет прав администратора.")
            return None
        return await handler(event, data)


router.message.middleware(AdminBooksMiddleware())
router.callback_query.middleware(AdminBooksMiddleware())


@router.message(F.text == "/cancel")
async def cancel_admin_state(message: Message, state: FSMContext):
    from handlers.user import cancel_action

    await cancel_action(message, state)


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
    """Return a private upload marker; never persist a Bot API download URL."""
    return f"telegram-file:{file_id}"


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
    await callback.answer()


@router.message(BookAddState.waiting_for_title)
async def process_title(message: Message, state: FSMContext, bot: Bot):
    """Обработка названия книги"""
    if message.text and message.text.strip():
        await state.update_data(title=message.text.strip(), step=1)
        if await _maybe_render_after_edit(message, state, bot):
            return
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
async def process_author(message: Message, state: FSMContext, bot: Bot):
    """Обработка автора книги"""
    if not (message.text and message.text.strip()):
        await message.answer("❌ Автор не может быть пустым. Попробуйте еще раз:")
        return

    author = message.text.strip()
    await state.update_data(author=author, step=2)

    # Если это правка из карточки — сразу возвращаемся к ней.
    if await _maybe_render_after_edit(message, state, bot):
        return

    # Проверка дубликатов по (title, author). Сравнение по LOWER+TRIM
    # уже сделано в db.find_book_by_title_author.
    data = await state.get_data()
    title = data.get('title', '')
    duplicate = await find_book_by_title_author(title, author)

    builder = InlineKeyboardBuilder()
    if duplicate:
        # Дубликат — спрашиваем, продолжать ли
        await state.set_state(BookAddState.waiting_for_description)
        builder.button(text="✅ Всё равно добавить", callback_data="admin_book_duplicate_continue")
        builder.button(text="✏️ Изменить название/автора", callback_data="admin_book_back_title")
        builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
        builder.adjust(2, 1)
        await message.answer(
            f"⚠️ <b>Такая книга уже есть в каталоге.</b>\n\n"
            f"📖 <b>{duplicate['title']}</b>\n"
            f"✍️ {duplicate['author'] or 'Автор не указан'}\n"
            f"📁 {duplicate['category'] or 'Без категории'}\n"
            f"🆔 ID: {duplicate['id']}\n\n"
            f"Хотите добавить её ещё раз (например, другое издание)?",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return

    builder.button(text="⬅️ Назад", callback_data="admin_book_back_author")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(1)

    await message.answer(
        "📝 Введите описание книги:",
        reply_markup=builder.as_markup()
    )
    await state.set_state(BookAddState.waiting_for_description)


@router.callback_query(F.data == "admin_book_duplicate_continue")
async def duplicate_continue(callback: CallbackQuery, state: FSMContext):
    """Админ подтвердил добавление книги-дубликата — продолжаем обычный флоу."""
    await callback.message.edit_text(
        "📝 Введите описание книги:",
        reply_markup=InlineKeyboardBuilder()
            .button(text="⬅️ Назад", callback_data="admin_book_back_author")
            .button(text="❌ Отмена", callback_data="admin_books_cancel")
            .adjust(1)
            .as_markup()
    )
    await state.set_state(BookAddState.waiting_for_description)
    await callback.answer()


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
async def process_description(message: Message, state: FSMContext, bot: Bot):
    """Обработка описания книги"""
    if message.text and message.text.strip():
        description = message.text.strip()
        if len(description) > MAX_DESCRIPTION_LENGTH:
            await message.answer(
                "❌ Описание слишком длинное."
                + DESCRIPTION_LIMIT_NOTE.format(current=len(description))
                + "\n\nСократите и пришлите заново:"
            )
            return
        await state.update_data(description=description, step=3)
        if await _maybe_render_after_edit(message, state, bot):
            return
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
async def process_price(message: Message, state: FSMContext, bot: Bot):
    """Обработка цены книги"""
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError

        await state.update_data(price=price, step=4)

        # Если это правка из карточки — сразу возвращаемся к ней.
        if await _maybe_render_after_edit(message, state, bot):
            return

        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="admin_book_back_description")
        builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
        builder.adjust(1)
        await message.answer(
            "📦 Введите остаток книги: целое число от 0 или «безлимит» для неограниченного наличия.",
            reply_markup=builder.as_markup(),
        )
        await state.set_state(BookAddState.waiting_for_stock)
    except (ValueError, TypeError):
        await message.answer("❌ Введите корректную цену (положительное число):")


@router.message(BookAddState.waiting_for_stock)
async def process_stock(message: Message, state: FSMContext):
    value = (message.text or "").strip().lower()
    if value in {"безлимит", "безлимита", "unlimited", "∞"}:
        stock_quantity = None
    else:
        try:
            stock_quantity = int(value)
            if stock_quantity < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите неотрицательное целое число или «безлимит».")
            return
    await state.update_data(stock_quantity=stock_quantity)
    categories = await get_all_categories()
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Без категории", callback_data="admin_book_cat_none")
    for category in categories:
        builder.button(text=f"📁 {category['name']}", callback_data=f"admin_book_cat_{category['id']}")
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_price")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(2)
    await message.answer("📂 Выберите категорию для книги:", reply_markup=builder.as_markup())
    await state.set_state(BookAddState.waiting_for_category)

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
async def process_category(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Обработка выбора категории"""
    suffix = callback.data.split("_")[-1]
    if suffix == "none":
        # Шаблон «Без категории» — сбрасываем category_id, чтобы карточка
        # показывалась с плейсхолдером category_display(cat=None).
        await state.update_data(category_id=None, step=5)
    else:
        category_id = int(suffix)
        await state.update_data(category_id=category_id, step=5)

    # Если это правка из карточки подтверждения (editing=True) — возвращаемся
    # на карточку, а не движем flow дальше к загрузке обложки.
    data = await state.get_data()
    if data.get('editing'):
        await state.update_data(editing=False)
        await render_book_confirmation(callback, state, bot)
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_category")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(1)

    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=
        "📸 <b>Отправьте обложку книги</b>\n\n"
        "Прикрепите фото Telegram-сообщением. Это главное изображение в каталоге.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(BookAddState.waiting_for_cover_photo)


@router.callback_query(F.data == "admin_book_back_category")
async def back_to_category(callback: CallbackQuery, state: FSMContext):
    """Возврат к выбору категории"""
    categories = await get_all_categories()

    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Без категории", callback_data="admin_book_cat_none")
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


@router.message(BookAddState.waiting_for_category)
async def process_category_text_fallback(message: Message, state: FSMContext):
    """Перехватываем текст во время выбора категории.

    В admin-флоу категория выбирается кнопками, поэтому любой текст —
    случайный ввод, а не команда. Без этого хендлера сообщение проваливается
    в universal_text_handler в user.py и тот запускает устаревший flow
    («Отправьте эмодзи…»), который тут вообще неуместен.
    """
    categories = await get_all_categories()
    builder = InlineKeyboardBuilder()
    for cat in categories:
        builder.button(text=f"📁 {cat['name']}", callback_data=f"admin_book_cat_{cat['id']}")
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_price")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(2)

    await message.answer(
        "📂 Выберите категорию для книги, нажав на кнопку ниже.\n\n"
        "Текст в этом шаге не принимается.",
        reply_markup=builder.as_markup()
    )


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
            "⚠️ Не удалось сохранить обложку. Отправьте фото Telegram ещё раз."
        )
        return
    await state.update_data(cover_photo=file_url, cover_photo_id=file_id, step=6)

    # Если это правка из карточки подтверждения (editing=True) — возвращаемся
    # на карточку, а не идём дальше на шаг фото страниц.
    data = await state.get_data()
    if data.get('editing'):
        await state.update_data(editing=False)
        await render_book_confirmation(message, state, bot)
        return

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
async def process_cover_photo_url(message: Message, state: FSMContext, bot: Bot):
    """Reject remote URLs so every new cover uses the safe media pipeline."""
    await message.answer(
        "❌ Ссылки на изображения не поддерживаются. Отправьте фото Telegram, чтобы бот безопасно обработал его."
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
        "Прикрепите фото Telegram-сообщением. Это главное изображение в каталоге.",
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


# Буфер для альбомов фото страниц: ключ (user_id, media_group_id) → {messages, task}.
# Не держим в FSMStorage, потому что сообщения одного альбома приходят
# быстрее, чем FSMStorage успевает среагировать — проще ждать в памяти.
_album_buffers: dict = {}
_album_locks: dict[int, asyncio.Lock] = {}
_ALBUM_FLUSH_SECONDS = 0.7


def _album_lock(user_id: int) -> asyncio.Lock:
    return _album_locks.setdefault(user_id, asyncio.Lock())


async def _save_page_photo_url(bot: Bot, photo) -> str | None:
    """Получить публичный URL фото по file_id, либо None если не удалось."""
    if photo.file_size and photo.file_size > MAX_PHOTO_FILE_BYTES:
        return None
    try:
        return await get_telegram_file_url(bot, photo.file_id)
    except Exception as e:
        logger.error(f"Не удалось получить file_path для фото страницы: {e}")
        return None


async def _flush_album(user_id: int, media_group_id: str, state: FSMContext, bot: Bot):
    """Достать собранный альбом из буфера и добавить фото в состояние."""
    key = (user_id, media_group_id)
    entry = _album_buffers.pop(key, None)
    if not entry:
        return

    async with _album_lock(user_id):
        messages = entry['messages']
        page_photos = list((await state.get_data()).get('page_photos', []))
        added = 0
        skipped_big = 0
        for message in messages:
            if len(page_photos) >= MAX_PAGE_PHOTOS:
                break
            photo = message.photo[-1] if message.photo else None
            if not photo:
                continue
            file_marker = await _save_page_photo_url(bot, photo)
            if file_marker is None:
                if photo.file_size and photo.file_size > MAX_PHOTO_FILE_BYTES:
                    skipped_big += 1
                continue
            page_photos.append(file_marker)
            added += 1
        await state.update_data(page_photos=page_photos)

    count = len(page_photos)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Готово", callback_data="admin_book_pages_done")
    builder.button(text="➕ Еще фото", callback_data="admin_book_add_more_pages")
    builder.button(text="⬅️ Назад", callback_data="admin_book_back_cover")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")
    builder.adjust(2)

    note = ""
    if skipped_big:
        note = f"\n\n⚠️ {skipped_big} фото пропущены: больше 9 МБ."
    if added == 0 and not skipped_big:
        return

    await messages[-1].answer(
        f"📥 <b>Альбом принят: +{added} фото</b>\n\n"
        f"Всего фото страниц: {count}"
        + note + "\n\nДобавить ещё или завершить?",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


async def _flush_pending_albums(user_id: int, state: FSMContext, bot: Bot) -> None:
    entries = [
        (media_group_id, entry)
        for (owner_id, media_group_id), entry in _album_buffers.items()
        if owner_id == user_id
    ]
    for _, entry in entries:
        task = entry.get("task")
        if task and not task.done():
            task.cancel()
    await asyncio.gather(
        *(entry["task"] for _, entry in entries if entry.get("task")),
        return_exceptions=True,
    )
    for media_group_id, _ in entries:
        await _flush_album(user_id, media_group_id, state, bot)
async def collect_album_photo(message: Message, state: FSMContext, bot: Bot):
    """Собрать фото из Telegram-альбома и одним пакетом добавить в состояние."""
    user_id = message.from_user.id
    media_group_id = message.media_group_id
    key = (user_id, media_group_id)

    entry = _album_buffers.setdefault(key, {'messages': [], 'task': None})
    entry['messages'].append(message)
    previous_task = entry.get('task')
    if previous_task and not previous_task.done():
        previous_task.cancel()

    async def _schedule_flush():
        try:
            await asyncio.sleep(_ALBUM_FLUSH_SECONDS)
            await _flush_album(user_id, media_group_id, state, bot)
        except asyncio.CancelledError:
            return

    entry['task'] = asyncio.create_task(_schedule_flush())


@router.message(BookAddState.waiting_for_page_photos, F.photo, ~F.media_group_id)
async def process_page_photo(message: Message, state: FSMContext, bot: Bot):
    """Обработка одиночного фото страницы (вложение Telegram).

    Альбомы (media_group) обрабатываются отдельным хендлером ниже —
    ждём ~0.7с, пока Telegram дошлёт все фото в группе, и добавляем их
    одним пакетом, чтобы админ мог прислать сразу 5–10 страниц.
    """
    async with _album_lock(message.from_user.id):
        data = await state.get_data()
        page_photos = list(data.get('page_photos', []))
        if len(page_photos) >= MAX_PAGE_PHOTOS:
            await message.answer(
                "❌ Уже загружено максимум фото страниц."
                + PAGE_PHOTOS_LIMIT_NOTE.format(current=len(page_photos))
                + "\n\nНажмите «✅ Готово» чтобы продолжить."
            )
            return

        photo = message.photo[-1]
        if photo.file_size and photo.file_size > MAX_PHOTO_FILE_BYTES:
            size_mb = round(photo.file_size / (1024 * 1024), 1)
            await message.answer(
                f"❌ Фото слишком большое ({size_mb} МБ). "
                f"Telegram Bot API принимает файлы до 10 МБ; "
                f"сожмите изображение и отправьте его ещё раз."
            )
            return
        try:
            file_marker = await get_telegram_file_url(bot, photo.file_id)
        except Exception as error:
            logger.error(f"Не удалось сохранить фото страницы: {error}")
            await message.answer(
                "⚠️ Не удалось сохранить фото страницы. Попробуйте отправить его ещё раз."
            )
            return
        page_photos.append(file_marker)
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
    """Reject remote URLs so every new page uses the safe media pipeline."""
    await message.answer(
        "❌ Ссылки на изображения не поддерживаются. Отправьте фото Telegram, чтобы бот безопасно обработал его."
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
async def finish_pages(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Завершение добавления фото и переход к подтверждению"""
    await _flush_pending_albums(callback.from_user.id, state, bot)
    await render_book_confirmation(callback, state, callback.bot)


async def render_book_confirmation(target, state: FSMContext, bot: Bot):
    """Показать карточку книги в том виде, как её увидит покупатель в Mini App,
    плюс кнопки редактирования конкретного поля и подтверждения.

    Используется и в finish_pages, и в per-field edit хендлерах — они
    подменяют данные в state и снова вызывают эту функцию.
    """
    data = await state.get_data()
    page_photos = data.get('page_photos', [])
    cover_photo_id = data.get('cover_photo_id')
    cover_photo_url = data.get('cover_photo')
    has_cover = bool(cover_photo_id or cover_photo_url)
    title = data.get('title') or '—'
    author = data.get('author') or '—'
    description = data.get('description') or ''
    price = data.get('price')
    stock_quantity = data.get('stock_quantity')
    category_id = data.get('category_id')

    cat = await get_category_by_id(category_id) if category_id else None
    cat_disp = category_display(cat)
    cat_label = f"{cat_disp['emoji']} {cat_disp['name']}".strip()

    desc_preview = description if len(description) <= 300 else description[:300] + '…'

    text = (
        f"📚 <b>Карточка книги</b> — так её увидит покупатель\n\n"
        f"📖 <b>{title}</b>\n"
        f"✍️ {author}\n"
        f"📂 {cat_label}\n"
        f"💰 {price} ₽\n"
        f"📦 Остаток: {'безлимит' if stock_quantity is None else f'{stock_quantity} шт.'}\n\n"
        f"{desc_preview or '<i>Без описания</i>'}\n\n"
        f"📸 Обложка: {'✅' if has_cover else '❌'}\n"
        f"📄 Фото страниц: {len(page_photos)} шт.\n\n"
        f"<b>Что хотите изменить?</b>"
    )

    builder = InlineKeyboardBuilder()
    # Per-field edits (2 в строку)
    builder.button(text="📝 Название", callback_data="admin_book_edit_title")
    builder.button(text="✍️ Автор", callback_data="admin_book_edit_author")
    builder.button(text="💰 Цена", callback_data="admin_book_edit_price")
    builder.button(text="📂 Категория", callback_data="admin_book_edit_category")
    builder.button(text="📄 Описание", callback_data="admin_book_edit_description")
    builder.button(text="📸 Обложка", callback_data="admin_book_edit_cover")
    builder.button(text="🖼 Страницы", callback_data="admin_book_edit_pages")
    builder.adjust(2)
    # Подтверждение / отмена — на отдельной строке (последний adjust(2) уже
    # действует, поэтому две кнопки уйдут в один ряд).
    builder.button(text="✅ Подтвердить", callback_data="admin_book_confirm_add")
    builder.button(text="❌ Отмена", callback_data="admin_books_cancel")

    cover_to_show = cover_photo_id or cover_photo_url
    await state.set_state(BookAddState.confirming)

    # Если у карточки есть обложка — перерисуем сообщение с фото,
    # иначе edit_message_text (если сообщение уже было фото — удалим и пришлём текстом).
    msg = target.message if hasattr(target, 'message') else target
    if cover_to_show:
        try:
            await target.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except Exception as e:
            logger.warning(f"Не удалось удалить старое сообщение подтверждения: {e}")
        # Telegram не всегда может скачать обложку по HTTP (например, истёк
        # подписанный URL или хост блокирует ботов). Если не вышло — шлём
        # текстом, чтобы админ хотя бы увидел карточку и кнопки.
        try:
            await msg.answer_photo(
                photo=cover_to_show,
                caption=text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            logger.warning(
                f"Telegram не смог загрузить обложку по URL/фото_id "
                f"({cover_to_show[:80]!r}): {e}. Шлю карточку текстом."
            )
            await msg.answer(
                text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Неожиданная ошибка при отправке обложки: {e}")
            await msg.answer(
                text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
    else:
        try:
            await target.bot.edit_message_text(
                chat_id=msg.chat.id,
                message_id=msg.message_id,
                text=text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        except Exception as e:
            # Если текущее сообщение было фото, edit_message_text упадёт —
            # удалим и пришлём заново текстом.
            logger.warning(f"edit_message_text упал на карточке: {e}; шлём новое")
            try:
                await target.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
            except Exception:
                pass
            await msg.answer(
                text,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )


# Словарь edit-обработчиков: callback_data → (state, prompt_text)
# Используем общий хендлер admin_book_edit_field ниже.
_EDIT_FIELD_PROMPTS = {
    "admin_book_edit_title": (BookAddState.waiting_for_title, "📝 Введите новое название книги:"),
    "admin_book_edit_author": (BookAddState.waiting_for_author, "✍️ Введите нового автора книги:"),
    "admin_book_edit_price": (BookAddState.waiting_for_price, "💰 Введите новую цену (в рублях):"),
    "admin_book_edit_category": (BookAddState.waiting_for_category, "📂 Выберите новую категорию:"),
    "admin_book_edit_description": (BookAddState.waiting_for_description, "📄 Введите новое описание книги:"),
    "admin_book_edit_cover": (BookAddState.waiting_for_cover_photo, "📸 Пришлите новую обложку Telegram-фото:"),
    "admin_book_edit_pages": (BookAddState.waiting_for_page_photos, "📸 Пришлите новые фото страниц (или нажмите «Готово»):"),
}


@router.callback_query(F.data.in_(list(_EDIT_FIELD_PROMPTS.keys())))
async def edit_book_field(callback: CallbackQuery, state: FSMContext):
    """Админ нажал кнопку «изменить поле» — переключаем на нужный шаг.

    Помечаем state.editing = True, чтобы хендлер поля после получения
    нового значения не шёл дальше по флоу, а вернулся к карточке.
    """
    target_state, prompt_text = _EDIT_FIELD_PROMPTS[callback.data]
    await state.update_data(editing=True)
    await state.set_state(target_state)

    # Для категории показываем клавиатуру выбора; иначе — поле ввода.
    if target_state == BookAddState.waiting_for_category:
        categories = await get_all_categories()
        builder = InlineKeyboardBuilder()
        # Шаблон «Без категории» — админ может снять категорию с книги,
        # тогда category_display() в карточке подставит плейсхолдер.
        builder.button(text="📦 Без категории", callback_data="admin_book_cat_none")
        for cat in categories:
            builder.button(
                text=f"{cat['emoji'] or ''} {cat['name']}".strip(),
                callback_data=f"admin_book_cat_{cat['id']}"
            )
        builder.button(text="⬅️ Назад к карточке", callback_data="admin_book_back_to_confirm")
        builder.adjust(2)
        await callback.message.answer(prompt_text, reply_markup=builder.as_markup())
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад к карточке", callback_data="admin_book_back_to_confirm")
        await callback.message.answer(prompt_text, reply_markup=builder.as_markup())

    await callback.answer()


async def _maybe_render_after_edit(message: Message, state: FSMContext, bot: Bot):
    """Если поле было отредактировано (editing=True), возвращаемся к карточке;
    иначе ничего не делаем — вызвавший код сам двинет флоу дальше."""
    data = await state.get_data()
    if data.get('editing'):
        await state.update_data(editing=False)
        # НЕ сбрасываем cover_photo_id/cover_photo — render_book_confirmation
        # читает их, чтобы показать обложку в карточке. Без них правка
        # описания/категории/цены стирала бы обложку у будущей книги.
        await render_book_confirmation(message, state, bot)
        return True
    return False


@router.callback_query(F.data == "admin_book_back_to_confirm")
async def back_to_confirmation(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Возврат к экрану подтверждения без изменения поля."""
    await render_book_confirmation(callback, state, bot)
    await callback.answer()


@router.callback_query(F.data == "admin_book_confirm_add")
async def confirm_add_book(callback: CallbackQuery, state: FSMContext):
    """Создаёт книгу, затем независимо обрабатывает staged Telegram media."""
    data = await state.get_data()
    try:
        book_id = await add_book(
            title=data['title'],
            author=data['author'],
            description=data['description'],
            price=data['price'],
            category_id=data['category_id'],
            stock_quantity=data.get('stock_quantity'),
        )
    except QuotaExceededError as error:
        result_text = (
            f"❌ <b>Лимит каталога исчерпан</b>\n\n"
            f"Максимум книг по текущему тарифу: {error.limit}."
        )
    except Exception as error:
        logger.error(f"Ошибка при создании книги: {error}")
        result_text = "❌ <b>Не удалось создать книгу.</b>"
    else:
        staged = []
        cover_file_id = data.get('cover_photo_id')
        if isinstance(cover_file_id, str) and cover_file_id:
            staged.append(("cover", 0, cover_file_id))
        staged.extend(
            ("page", position, marker.removeprefix("telegram-file:"))
            for position, marker in enumerate(data.get("page_photos", []))
            if isinstance(marker, str) and marker.startswith("telegram-file:")
        )
        saved_media = 0
        failed_media = 0
        for role, position, file_id in staged:
            try:
                await attach_telegram_media(callback.bot, book_id, role, position, file_id)
                saved_media += 1
            except Exception as error:
                failed_media += 1
                logger.warning(
                    "Не удалось сохранить %s фото книги %s: %s", role, book_id, type(error).__name__
                )
        logger.info("Книга '%s' добавлена админом %s", data['title'], callback.from_user.id)
        media_note = (
            f"\n\n⚠️ Сохранено фото: {saved_media}; не удалось: {failed_media}. "
            "Откройте редактирование книги и повторите загрузку неудачных фото."
            if failed_media else ""
        )
        result_text = (
            f"✅ <b>Книга успешно добавлена!</b>\n\n"
            f"ID: {book_id}\n"
            f"Название: {data['title']}"
            + media_note
        )

    try:
        await callback.message.delete()
    except Exception as error:
        logger.warning(f"Не удалось удалить сообщение подтверждения: {error}")
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 В админ-панель", callback_data="admin_menu")
    await callback.message.answer(
        result_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await state.clear()
    await callback.answer()


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
    """Меню управления книгами с пагинацией, поиском и сортировкой."""
    await state.clear()
    await state.update_data(
        current_page=0,
        sort_by="default",
        search_query="",
    )
    await show_books_list(callback, state, page=0)


BOOK_BUTTON_TITLE_LIMIT = 48
BOOK_TEXT_TITLE_LIMIT = 100


def _short_book_title(title: str, limit: int) -> str:
    title = title or "Без названия"
    return title if len(title) <= limit else title[: limit - 1] + "…"


async def _build_books_list_view(state: FSMContext, page: int):
    data = await state.get_data()
    sort_by = data.get("sort_by", "default") or "default"
    search_query = data.get("search_query", "") or ""
    selection_mode = data.get("book_selection_mode")
    selected_book_ids = set(data.get("selected_book_ids", []))

    total_books = await get_books_count(search_query=search_query)
    total_pages = max(1, (total_books + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    books = await get_all_books_paginated(
        limit=PAGE_SIZE,
        offset=page * PAGE_SIZE,
        sort_by=sort_by,
        search_query=search_query,
    )
    sort_label, _ = _BOOKS_SORT_LABELS.get(sort_by, _BOOKS_SORT_LABELS["default"])

    text = "📚 <b>Управление книгами</b>\n\n"
    text += f"🔀 Сортировка: {sort_label}\n"
    if search_query:
        text += f"🔎 Поиск: <i>{escape(search_query, quote=False)}</i>\n"
    text += f"Страница {page + 1} из {total_pages}\n"
    text += f"Всего книг: {total_books}\n\n"
    if books:
        for book in books:
            emoji = book.get("category_emoji") or "📖"
            title = escape(_short_book_title(book["title"], BOOK_TEXT_TITLE_LIMIT), quote=False)
            stock_label = "∞" if book.get("stock_quantity") is None else f"{book['stock_quantity']} шт."
            text += f"{emoji} <b>{title}</b> — {book['price']} ₽ · {stock_label}\n"
    elif search_query:
        text += "По этому запросу ничего не найдено."
    else:
        text += "Пока нет добавленных книг."

    builder = InlineKeyboardBuilder()
    if selection_mode == "archive":
        builder.button(
            text=f"📥 В архив выбранные ({len(selected_book_ids)})",
            callback_data="admin_books_archive_review",
        )
        builder.button(text="✖ Отменить выбор", callback_data="admin_books_selection_cancel")
    elif selection_mode == "category":
        builder.button(
            text=f"📂 Сменить категорию ({len(selected_book_ids)})",
            callback_data="admin_books_category_choose",
        )
        builder.button(text="✖ Отменить выбор", callback_data="admin_books_selection_cancel")
    else:
        builder.button(text="➕ Добавить книгу", callback_data="admin_add_book")
        builder.button(
            text="☑️ Выбрать несколько для архива",
            callback_data="admin_books_archive_select",
        )
        builder.button(
            text="📂 Сменить категорию нескольким",
            callback_data="admin_books_category_select",
        )

    for book in books:
        title = _short_book_title(book["title"], BOOK_BUTTON_TITLE_LIMIT)
        if selection_mode in {"archive", "category"}:
            selected = book["id"] in selected_book_ids
            operation = "archive" if selection_mode == "archive" else "category"
            builder.button(
                text=f"{'☑️' if selected else '☐'} {title}",
                callback_data=f"admin_books_{operation}_toggle_{book['id']}",
            )
        else:
            builder.button(
                text=f"📝 {title}", callback_data=f"admin_book_edit_{book['id']}"
            )

    nav_count = 0
    if page > 0:
        builder.button(text="⬅️ Назад", callback_data=f"admin_books_page_{page - 1}")
        nav_count += 1
    if page < total_pages - 1:
        builder.button(text="➡️ Вперёд", callback_data=f"admin_books_page_{page + 1}")
        nav_count += 1

    builder.button(text="🔎 Поиск по названию", callback_data="admin_books_search")
    if search_query:
        builder.button(text="✖ Сбросить поиск", callback_data="admin_books_clear_search")
    for key, (label, _hint) in _BOOKS_SORT_LABELS.items():
        prefix = "✅ " if key == sort_by else ""
        builder.button(text=f"{prefix}{label}", callback_data=f"admin_books_sort_{key}")
    builder.button(text="🗂 Архив", callback_data="admin_books_archive")
    builder.button(text="🔙 В меню админа", callback_data="admin_menu")

    row_sizes: list[int] = [2] if selection_mode else [2, 1]
    row_sizes.extend([1] * len(books))
    if nav_count:
        row_sizes.append(nav_count)
    row_sizes.append(2 if search_query else 1)
    row_sizes.extend([2, 2, 1, 2])
    builder.adjust(*row_sizes)
    return text, builder.as_markup(), page


async def show_books_list(callback: CallbackQuery, state: FSMContext, page: int):
    """Отобразить текущую страницу управления книгами в callback-сообщении."""
    text, markup, page = await _build_books_list_view(state, page)
    await state.update_data(current_page=page)
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось отобразить список книг")
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")


async def render_books_list_for_message(message: Message, state: FSMContext):
    """Отправить список книг после ввода поискового запроса."""
    data = await state.get_data()
    text, markup, page = await _build_books_list_view(
        state, data.get("current_page", 0) or 0
    )
    await state.update_data(current_page=page)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin_books_page_"))
async def books_page_navigation(callback: CallbackQuery, state: FSMContext):
    """Навигация по страницам списка книг (с учётом текущей сортировки/поиска)."""
    page = int(callback.data.split("_")[-1])
    await state.update_data(current_page=page)
    await show_books_list(callback, state, page=page)


@router.callback_query(F.data.startswith("admin_books_sort_"))
async def books_sort(callback: CallbackQuery, state: FSMContext):
    """Сменить сортировку админского списка книг. Возвращаемся на 1-ю страницу."""
    sort_by = callback.data.split("admin_books_sort_")[-1]
    if sort_by not in _BOOKS_SORT_LABELS:
        await callback.answer("Неизвестная сортировка", show_alert=True)
        return
    await state.update_data(sort_by=sort_by, current_page=0)
    await show_books_list(callback, state, page=0)
    await callback.answer()


@router.callback_query(F.data == "admin_books_search")
async def books_search_prompt(callback: CallbackQuery, state: FSMContext):
    """Предлагаем админу ввести подстроку для поиска по названию."""
    await state.set_state(AdminBooksState.waiting_for_search)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_books_menu")
    await callback.message.answer(
        "🔎 <b>Поиск книги по названию</b>\n\n"
        "Введите часть названия (без учёта регистра). Бот покажет только "
        "книги, у которых в названии встречается эта подстрока.\n\n"
        "Чтобы выйти без поиска — нажмите «Отмена».",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminBooksState.waiting_for_search)
async def books_search_apply(message: Message, state: FSMContext):
    """Применяем введённую подстроку и возвращаемся к списку книг."""
    query = (message.text or "").strip()
    if not query:
        await message.answer("Введите непустую подстроку для поиска.")
        return
    # Ограничиваем длину, чтобы админ случайно не вставил многотомный роман.
    if len(query) > 100:
        query = query[:100]
    await state.update_data(search_query=query, current_page=0)
    await state.set_state(None)
    # Прячем подсказку про поиск, чтобы она не висела в чате.
    try:
        await message.delete()
    except Exception:
        pass
    # Шлём обновлённый список как новое сообщение (не edit, т.к. message —
    # это текст от админа, а список рисуется с клавиатурой).
    await render_books_list_for_message(message, state)


@router.callback_query(F.data == "admin_books_clear_search")
async def books_clear_search(callback: CallbackQuery, state: FSMContext):
    """Сбросить поисковую подстроку, остаться на текущей странице/сортировке."""
    await state.update_data(search_query="", current_page=0)
    await show_books_list(callback, state, page=0)
    await callback.answer("Поиск сброшен")


@router.callback_query(F.data == "admin_books_archive_select")
async def start_archive_selection(callback: CallbackQuery, state: FSMContext):
    await state.update_data(book_selection_mode="archive", selected_book_ids=[])
    data = await state.get_data()
    await show_books_list(callback, state, page=data.get("current_page", 0) or 0)
    await callback.answer("Выберите книги для архива")


@router.callback_query(F.data.regexp(r"^admin_books_archive_toggle_\d+$"))
async def toggle_archive_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("book_selection_mode") != "archive":
        await callback.answer("Откройте выбор книг заново", show_alert=True)
        return
    book_id = int(callback.data.rsplit("_", 1)[-1])
    selected = set(data.get("selected_book_ids", []))
    if book_id in selected:
        selected.remove(book_id)
    else:
        if len(selected) >= MAX_BOOK_SELECTION:
            await callback.answer(f"Можно выбрать не более {MAX_BOOK_SELECTION} книг", show_alert=True)
            return
        if not await get_book(book_id):
            await callback.answer("Книга больше недоступна", show_alert=True)
            return
        selected.add(book_id)
    await state.update_data(selected_book_ids=sorted(selected))
    await show_books_list(callback, state, page=data.get("current_page", 0) or 0)
    await callback.answer()


@router.callback_query(F.data == "admin_books_archive_review")
async def review_archive_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_book_ids", [])
    if data.get("book_selection_mode") != "archive" or not selected:
        await callback.answer("Выберите хотя бы одну книгу", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text=f"📥 Архивировать {len(selected)} книг", callback_data="admin_books_archive_selected_confirm")
    builder.button(text="✖ Отменить выбор", callback_data="admin_books_selection_cancel")
    builder.adjust(1)
    await callback.message.edit_text(
        f"⚠️ <b>Перенести в архив {len(selected)} книг?</b>\n\n"
        "Книги исчезнут из каталога, но останутся в базе и их можно будет восстановить.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_books_archive_selected_confirm")
async def confirm_archive_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_book_ids", [])
    if data.get("book_selection_mode") != "archive" or not selected:
        await callback.answer("Выбор книг устарел", show_alert=True)
        return
    result = await archive_books(selected)
    await state.update_data(book_selection_mode=None, selected_book_ids=[])
    await show_books_list(callback, state, page=data.get("current_page", 0) or 0)
    await callback.answer(
        f"В архив перенесено: {len(result['archived_ids'])}; пропущено: {len(result['skipped_ids'])}",
        show_alert=True,
    )


@router.callback_query(F.data == "admin_books_category_select")
async def start_category_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("book_selection_mode") != "category":
        await state.update_data(book_selection_mode="category", selected_book_ids=[])
        data = await state.get_data()
    await show_books_list(callback, state, page=data.get("current_page", 0) or 0)
    await callback.answer("Выберите книги для смены категории")


@router.callback_query(F.data.regexp(r"^admin_books_category_toggle_\d+$"))
async def toggle_category_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("book_selection_mode") != "category":
        await callback.answer("Откройте выбор книг заново", show_alert=True)
        return
    book_id = int(callback.data.rsplit("_", 1)[-1])
    selected = set(data.get("selected_book_ids", []))
    if book_id in selected:
        selected.remove(book_id)
    else:
        if len(selected) >= MAX_BOOK_SELECTION:
            await callback.answer(
                f"Можно выбрать не более {MAX_BOOK_SELECTION} книг", show_alert=True
            )
            return
        if not await get_book(book_id):
            await callback.answer("Книга больше недоступна", show_alert=True)
            return
        selected.add(book_id)
    await state.update_data(selected_book_ids=sorted(selected))
    await show_books_list(callback, state, page=data.get("current_page", 0) or 0)
    await callback.answer()


@router.callback_query(F.data == "admin_books_category_choose")
async def choose_bulk_category(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_book_ids", [])
    if data.get("book_selection_mode") != "category" or not selected:
        await callback.answer("Выберите хотя бы одну книгу", show_alert=True)
        return
    categories = await get_all_categories()
    if not categories:
        await callback.answer("Нет доступных категорий", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    for category in categories:
        label = category_display(category)
        builder.button(
            text=f"{label['emoji']} {label['name']}",
            callback_data=f"admin_books_category_target_{category['id']}",
        )
    builder.button(text="◀️ К выбору книг", callback_data="admin_books_category_select")
    builder.button(text="✖ Отменить выбор", callback_data="admin_books_selection_cancel")
    builder.adjust(*([1] * (len(categories) + 2)))
    await callback.message.edit_text(
        f"📂 <b>Выберите категорию для {len(selected)} книг</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_books_category_target_\d+$"))
async def confirm_bulk_category_target(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_book_ids", [])
    if data.get("book_selection_mode") != "category" or not selected:
        await callback.answer("Выбор книг устарел", show_alert=True)
        return
    category_id = int(callback.data.rsplit("_", 1)[-1])
    category = await get_category_by_id(category_id)
    if category is None:
        await callback.answer("Категория больше недоступна", show_alert=True)
        return
    label = category_display(category)
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"✅ Сменить категорию у {len(selected)} книг",
        callback_data=f"admin_books_category_confirm_{category_id}",
    )
    builder.button(text="◀️ К категориям", callback_data="admin_books_category_choose")
    builder.button(text="✖ Отменить выбор", callback_data="admin_books_selection_cancel")
    builder.adjust(1)
    await callback.message.edit_text(
        f"⚠️ <b>Перенести {len(selected)} книг в категорию «{escape(label['name'], quote=False)}»?</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_books_category_confirm_\d+$"))
async def apply_bulk_category(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_book_ids", [])
    if data.get("book_selection_mode") != "category" or not selected:
        await callback.answer("Выбор книг устарел", show_alert=True)
        return
    category_id = int(callback.data.rsplit("_", 1)[-1])
    try:
        result = await reassign_active_books_category(selected, category_id)
    except ValueError:
        await callback.answer("Категория больше недоступна", show_alert=True)
        return
    await state.update_data(book_selection_mode=None, selected_book_ids=[])
    await show_books_list(callback, state, page=data.get("current_page", 0) or 0)
    await callback.answer(
        f"Категория изменена: {len(result['updated_ids'])}; пропущено: {len(result['skipped_ids'])}",
        show_alert=True,
    )

async def cancel_book_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(book_selection_mode=None, selected_book_ids=[])
    await show_books_list(callback, state, page=data.get("current_page", 0) or 0)
    await callback.answer("Выбор отменён")


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
    builder.adjust(2, 1)
    
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

    categories = await get_all_categories()

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

    categories = await get_all_categories()
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

    categories = await get_all_categories()
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
        new_cat_id = await add_category(new_category_name, "")
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
    # Текстовые поля — парами; медиа — парой; категория и «Назад» отдельно.
    builder.button(text="✏️ Название", callback_data=f"admin_book_edit_title_{book_id}")
    builder.button(text="✏️ Автор", callback_data=f"admin_book_edit_author_{book_id}")
    builder.button(text="✏️ Описание", callback_data=f"admin_book_edit_desc_{book_id}")
    builder.button(text="✏️ Цена", callback_data=f"admin_book_edit_price_{book_id}")
    stock_label = "безлимит" if book.get("stock_quantity") is None else f"{book['stock_quantity']} шт."
    builder.button(text=f"📦 Остаток: {stock_label}", callback_data=f"admin_book_edit_stock_{book_id}")
    builder.button(text="🖼 Обложка", callback_data=f"admin_book_edit_cover_{book_id}")
    builder.button(text="📄 Страницы", callback_data=f"admin_book_edit_pages_{book_id}")
    builder.button(text="📂 Категория", callback_data=f"admin_book_change_category_{book_id}")
    builder.button(text="🔙 Назад к списку", callback_data="admin_books_menu")
    builder.adjust(2, 2, 2, 1, 1)
    
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


@router.callback_query(F.data.regexp(r"^admin_book_edit_stock_\d+$"))
async def edit_book_stock(callback: CallbackQuery, state: FSMContext):
    book_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_book_id=book_id)
    await state.set_state(EditBookState.waiting_for_new_stock)
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data=f"admin_book_edit_{book_id}")
    await callback.message.edit_text(
        "📦 Введите остаток: целое число от 0 или «безлимит».",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.message(EditBookState.waiting_for_new_stock)
async def process_new_stock(message: Message, state: FSMContext):
    value = (message.text or "").strip().lower()
    if value in {"безлимит", "безлимита", "unlimited", "∞"}:
        stock_quantity = None
    else:
        try:
            stock_quantity = int(value)
            if stock_quantity < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите неотрицательное число или «безлимит».")
            return
    data = await state.get_data()
    book_id = data.get("edit_book_id")
    if not isinstance(book_id, int):
        await state.clear()
        await message.answer("❌ Книга не найдена.")
        return
    try:
        changed = await set_stock_quantity(book_id, stock_quantity, message.from_user.id)
    except InventoryUnavailableError:
        await message.answer("❌ Остаток нельзя сделать меньше уже зарезервированных экземпляров или переключить в «безлимит» до их обработки.")
        return
    except ValueError:
        await state.clear()
        await message.answer("❌ Не удалось изменить остаток. Откройте книгу заново.")
        return
    if not changed:
        await message.answer("❌ Книга не найдена.")
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    label = "безлимит" if stock_quantity is None else f"{stock_quantity} шт."
    await message.answer(f"✅ Остаток изменён: {label}", reply_markup=builder.as_markup())
    await state.clear()

@router.callback_query(F.data.regexp(r"^admin_book_edit_cover_\d+$"))
async def edit_book_cover(callback: CallbackQuery, state: FSMContext):
    """Показывает native обложку и предлагает прислать новую."""
    book_id = int(callback.data.split("_")[-1])
    book = await get_book(book_id)
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    await state.update_data(edit_book_id=book_id)
    await state.set_state(EditBookState.waiting_for_new_cover)
    has_cover = media_variants_for_book_sync(book_id)["cover"] is not None
    preview_text = "🖼 <b>Текущая обложка:</b> задана" if has_cover else "🖼 <b>Текущая обложка:</b> не задана"
    builder = InlineKeyboardBuilder()
    if has_cover:
        builder.button(text="🗑 Удалить обложку", callback_data=f"admin_book_clear_cover_{book_id}")
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    builder.adjust(1)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        f"{preview_text}\n\n📸 <b>Пришлите новую обложку Telegram-фото</b>",
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
        await attach_telegram_media(bot, book_id, "cover", 0, photo.file_id)
    except BookMediaError:
        await message.answer("⚠️ Не удалось сохранить фото. Пришлите другое изображение.")
        return

    await update_book_full(book_id, cover_photo="", emoji="")

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

    await message.answer("❌ Внешние ссылки на изображения больше не поддерживаются. Пришлите фото в Telegram.")
    return


@router.callback_query(F.data.regexp(r"^admin_book_clear_cover_\d+$"))
async def clear_book_cover(callback: CallbackQuery, state: FSMContext):
    """Сбрасывает обложку книги."""
    book_id = int(callback.data.split("_")[-1])
    clear_book_media_sync(book_id, "cover")
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
    """Список native фото страниц с кнопками удалить / добавить / очистить."""
    book_id = int(callback.data.split("_")[-1])
    book = await get_book(book_id)
    if not book:
        await callback.answer("❌ Книга не найдена", show_alert=True)
        return

    pages = [item for item in list_book_media_sync(book_id) if item["role"] == "page"]
    text_lines = [f"📄 <b>Фото страниц книги</b> «{book['title']}»", ""]
    text_lines.append(f"Сейчас: {len(pages)} шт." if pages else "Пока ни одного фото.")
    builder = InlineKeyboardBuilder()
    for page in pages:
        builder.button(
            text=f"🗑 Удалить #{page['position'] + 1}",
            callback_data=f"admin_book_page_del_{book_id}_{page['asset_id']}",
        )
    builder.button(text="➕ Добавить фото", callback_data=f"admin_book_page_add_{book_id}")
    if pages:
        builder.button(text="🧹 Очистить все", callback_data=f"admin_book_pages_clear_{book_id}")
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    builder.adjust(*([1] * len(pages)), 2 if pages else 1, 1)
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
        "Прикрепите фото Telegram-сообщением.",
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
    position = next_page_position_sync(book_id)
    try:
        await attach_telegram_media(bot, book_id, "page", position, photo.file_id)
    except BookMediaError:
        await message.answer("⚠️ Не удалось сохранить фото. Пришлите другое изображение.")
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Ещё фото", callback_data=f"admin_book_page_add_{book_id}")
    builder.button(text="📄 К списку фото", callback_data=f"admin_book_edit_pages_{book_id}")
    builder.button(text="◀️ Назад к книге", callback_data=f"admin_book_edit_{book_id}")
    builder.adjust(2, 1)
    await message.answer(
        f"✅ Фото #{position + 1} добавлено. Всего: {position + 1}.",
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

    await message.answer("❌ Внешние ссылки на изображения больше не поддерживаются. Пришлите фото в Telegram.")
    return


@router.callback_query(F.data.regexp(r"^admin_book_page_del_\d+_[0-9a-f]{32}$"))
async def delete_book_page(callback: CallbackQuery, state: FSMContext):
    """Удаляет native фото страницы по opaque asset id."""
    parts = callback.data.split("_")
    book_id = int(parts[-2])
    asset_id = parts[-1]
    if not delete_book_media_sync(book_id, asset_id):
        await callback.answer("❌ Фото не найдено", show_alert=True)
        return
    await callback.answer("✅ Удалено")
    await edit_book_pages(callback, state)


@router.callback_query(F.data.regexp(r"^admin_book_pages_clear_\d+$"))
async def clear_book_pages(callback: CallbackQuery, state: FSMContext):
    """Очищает все native фото страниц."""
    book_id = int(callback.data.split("_")[-1])
    clear_book_media_sync(book_id, "page")
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
    builder.adjust(2)
    
    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text=(
                f"⚠️ <b>Удаление книги</b>\n\n"
                f"Вы уверены, что хотите удалить книгу:\n"
                f"📖 {book['title']}\n\n"
                f"Книга уйдёт в <b>архив</b> (мягкое удаление): строка останется "
                f"в базе, ссылки из старых заказов и выгрузок для бухгалтерии "
                f"сохранятся. Позже её можно восстановить из архива."
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
            text=f"✅ Книга перенесена в архив (старые заказы не потеряны)",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        logger.error(f"Ошибка при удалении книги: {e}")
        await callback.answer("❌ Ошибка при удалении", show_alert=True)


@router.callback_query(F.data == "admin_books_archive")
async def show_archive_list(callback: CallbackQuery, state: FSMContext):
    """Открыть архив без сохранённых destructive selections."""
    await state.clear()
    await state.update_data(archive_page=0, book_selection_mode=None, selected_book_ids=[])
    await render_archive_list(callback, state, page=0)
    await callback.answer()


async def render_archive_list(callback: CallbackQuery, state: FSMContext, page: int):
    data = await state.get_data()
    selection_mode = data.get("book_selection_mode")
    selected_ids = set(data.get("selected_book_ids", []))
    total_books = await get_archived_books_count()
    total_pages = max(1, (total_books + ARCHIVE_PAGE_SIZE - 1) // ARCHIVE_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    await state.update_data(archive_page=page)
    books = await get_archived_books(limit=ARCHIVE_PAGE_SIZE, offset=page * ARCHIVE_PAGE_SIZE)

    text = f"🗂 <b>Архив книг</b>\n\nСтраница {page + 1} из {total_pages}\n"
    if selection_mode == "purge":
        text += f"Выбрано для удаления: {len(selected_ids)}\n\n"
    elif books:
        text += "Книги с заказами нельзя удалить навсегда — они остаются в архиве для учёта.\n\n"
    else:
        text += "Архив пуст — сюда попадают удалённые книги."

    builder = InlineKeyboardBuilder()
    for book in books:
        emoji = book.get("category_emoji") or "📖"
        order_count = book.get("order_items_count", 0)
        if selection_mode == "purge":
            if order_count:
                builder.button(
                    text=f"🔒 {emoji} {book['title']} — есть заказы",
                    callback_data=f"admin_books_purge_protected_{book['id']}",
                )
            else:
                selected = book["id"] in selected_ids
                builder.button(
                    text=f"{'☑️' if selected else '☐'} {emoji} {book['title']}",
                    callback_data=f"admin_books_purge_toggle_{book['id']}",
                )
            continue

        builder.button(
            text=f"↩️ {emoji} {book['title']} — {book['price']} ₽",
            callback_data=f"admin_book_restore_{book['id']}",
        )
        if order_count:
            builder.button(
                text=f"🔒 Не удалять: {book['title']} ({order_count} заказов)",
                callback_data=f"admin_books_purge_protected_{book['id']}",
            )
        else:
            builder.button(
                text=f"🗑 Удалить навсегда: {book['title']}",
                callback_data=f"admin_book_purge_{book['id']}",
            )

    if page > 0:
        builder.button(text="⬅️ Назад", callback_data=f"admin_books_archive_page_{page - 1}")
    if page < total_pages - 1:
        builder.button(text="➡️ Вперёд", callback_data=f"admin_books_archive_page_{page + 1}")

    if selection_mode == "purge":
        builder.button(
            text=f"🗑 Удалить выбранные ({len(selected_ids)})",
            callback_data="admin_books_purge_review",
        )
        builder.button(text="✖ Отменить выбор", callback_data="admin_books_purge_cancel")
    else:
        builder.button(text="☑️ Выбрать несколько для удаления", callback_data="admin_books_purge_select")
    builder.button(text="📚 Управление книгами", callback_data="admin_books_menu")
    builder.button(text="🔙 В меню админа", callback_data="admin_menu")
    builder.adjust(1)

    try:
        await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.regexp(r"^admin_books_archive_page_\d+$"))
async def archive_page_navigation(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.rsplit("_", 1)[-1])
    await render_archive_list(callback, state, page)
    await callback.answer()


@router.callback_query(F.data == "admin_books_purge_select")
async def start_purge_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(book_selection_mode="purge", selected_book_ids=[])
    await render_archive_list(callback, state, data.get("archive_page", 0) or 0)
    await callback.answer("Выберите архивные книги без заказов")


@router.callback_query(F.data.regexp(r"^admin_books_purge_toggle_\d+$"))
async def toggle_purge_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("book_selection_mode") != "purge":
        await callback.answer("Откройте выбор заново", show_alert=True)
        return
    book_id = int(callback.data.rsplit("_", 1)[-1])
    selected = set(data.get("selected_book_ids", []))
    if book_id in selected:
        selected.remove(book_id)
    else:
        if len(selected) >= MAX_BOOK_SELECTION:
            await callback.answer(f"Можно выбрать не более {MAX_BOOK_SELECTION} книг", show_alert=True)
            return
        classification = await classify_archived_book_ids([book_id])
        if not classification["eligible_ids"]:
            await callback.answer("Эту книгу нельзя удалить навсегда", show_alert=True)
            return
        selected.add(book_id)
    await state.update_data(selected_book_ids=sorted(selected))
    await render_archive_list(callback, state, data.get("archive_page", 0) or 0)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^admin_book_purge_\d+$"))
async def start_single_purge(callback: CallbackQuery, state: FSMContext):
    book_id = int(callback.data.rsplit("_", 1)[-1])
    await state.update_data(book_selection_mode="purge", selected_book_ids=[book_id])
    await review_purge_selection(callback, state)


@router.callback_query(F.data == "admin_books_purge_review")
async def review_purge_selection(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_book_ids", [])
    if data.get("book_selection_mode") != "purge" or not selected:
        await callback.answer("Выберите хотя бы одну книгу", show_alert=True)
        return
    classification = await classify_archived_book_ids(selected)
    eligible = classification["eligible_ids"]
    blocked = len(selected) - len(eligible)
    if not eligible:
        await state.update_data(selected_book_ids=[])
        await render_archive_list(callback, state, data.get("archive_page", 0) or 0)
        await callback.answer("Выбранные книги нельзя удалить", show_alert=True)
        return
    await state.update_data(selected_book_ids=eligible)
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🔐 Получить код для удаления ({len(eligible)})", callback_data="admin_books_purge_issue_code")
    builder.button(text="◀️ К архиву", callback_data="admin_books_purge_cancel")
    builder.adjust(1)
    note = f"\nПропущено недоступных книг: {blocked}." if blocked else ""
    await callback.message.edit_text(
        f"⚠️ <b>Безвозвратно удалить {len(eligible)} книг?</b>\n\n"
        "Восстановить их будет нельзя. Книги с заказами не удаляются."
        f"{note}",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "admin_books_purge_issue_code")
async def issue_purge_code(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await state.clear()
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    data = await state.get_data()
    selected = data.get("selected_book_ids", [])
    if data.get("book_selection_mode") != "purge" or not selected:
        await callback.answer("Выбор книг устарел", show_alert=True)
        return
    classification = await classify_archived_book_ids(selected)
    eligible = classification["eligible_ids"]
    if not eligible:
        await state.clear()
        await callback.answer("Выбранные книги больше нельзя удалить", show_alert=True)
        return
    code = issue_otp(callback.from_user.id, ACTION_PURGE_ARCHIVED_BOOKS)
    await state.update_data(purge_book_ids=eligible, book_selection_mode="purge_pending")
    await state.set_state(AdminBooksState.waiting_for_archive_purge_code)
    builder = InlineKeyboardBuilder()
    builder.button(text="✖ Отмена", callback_data="admin_books_purge_cancel")
    await callback.message.edit_text(
        f"🔐 <b>Подтверждение удаления {len(eligible)} книг</b>\n\n"
        f"Отправьте одноразовый код: <code>{code}</code>\n"
        f"Код действует {OTP_TTL_SECONDS} сек.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminBooksState.waiting_for_archive_purge_code, F.text & ~F.text.startswith("/"))
async def confirm_purge_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return
    data = await state.get_data()
    selected = data.get("purge_book_ids", [])
    if data.get("book_selection_mode") != "purge_pending" or not selected:
        await state.clear()
        await message.answer("⚠️ Выбор книг устарел.")
        return
    if not consume_otp(message.from_user.id, ACTION_PURGE_ARCHIVED_BOOKS, (message.text or "").strip()):
        await state.clear()
        await message.answer("✖ Неверный или истёкший код. Удаление отменено.")
        return
    result = await purge_archived_books(selected)
    await state.clear()
    await message.answer(
        f"✅ Удалено безвозвратно: {len(result['deleted_ids'])}.\n"
        f"Оставлено в архиве: {len(result['referenced_ids']) + len(result['not_archived_ids']) + len(result['missing_ids'])}.",
    )


@router.callback_query(F.data == "admin_books_purge_cancel")
async def cancel_purge(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    page = data.get("archive_page", 0) or 0
    revoke_otp(callback.from_user.id, ACTION_PURGE_ARCHIVED_BOOKS)
    await state.clear()
    await state.update_data(archive_page=page, book_selection_mode=None, selected_book_ids=[])
    await render_archive_list(callback, state, page)
    await callback.answer("Удаление отменено")


@router.callback_query(F.data.regexp(r"^admin_books_purge_protected_\d+$"))
async def protected_purge_notice(callback: CallbackQuery):
    await callback.answer("Книга есть в заказах и остаётся в архиве для учёта", show_alert=True)


@router.callback_query(F.data.regexp(r"^admin_book_restore_\d+$"))
async def restore_book_handler(callback: CallbackQuery, state: FSMContext):
    """Вернуть книгу из архива в каталог."""
    book_id = int(callback.data.split("_")[-1])
    restored = await restore_book(book_id)
    if not restored:
        await callback.answer("❌ Книга не найдена в архиве", show_alert=True)
        return
    data = await state.get_data()
    await render_archive_list(callback, state, data.get("archive_page", 0) or 0)
    await callback.answer("Книга восстановлена")
