import json
import asyncio  # ← ДОБАВЬ ЭТО (было пропущено, из-за чего падала рассылка)
from aiogram import Bot, Router, F
from aiogram.types import Message, WebAppInfo, CallbackQuery, LabeledPrice
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

import db
# ↓ ОБНОВИ ЭТУ СТРОКУ, добавив broadcast_pending_users и support_pending_users
from config import BOT_TOKEN, WEBAPP_URL, ADMIN_IDS, broadcast_pending_users, support_pending_users
from states import AddBookState, EditBookState, CategoryState, PromoCodeState, ReferralState, PaymentSettingsState
from utils import format_local_time, parseBookImages

router = Router()
# !!! УДАЛИ отсюда эти две строки, если они остались:
# broadcast_pending_users = set()
# support_pending_users = set()

router = Router()

# Глобальные множества для рассылки и поддержки




def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.message(CommandStart(deep_link=True))
async def cmd_start_with_ref(message: Message, state: FSMContext):
    """Обработка /start с реферальным кодом"""
    ref_code = message.text.split()[1] if len(message.text.split()) > 1 else ""
    referrer_id = await db.parse_referral_code(ref_code)

    if referrer_id and referrer_id != message.from_user.id:
        already_referred = await db.check_referral_exists(message.from_user.id)

        if not already_referred:
            await state.update_data(referrer_id=referrer_id)
            await state.set_state(ReferralState.waiting_for_confirmation)

            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Да, получить скидку 10%", callback_data="ref_accept")
            builder.button(text="❌ Нет, спасибо", callback_data="ref_decline")
            builder.adjust(1)

            await message.answer(
                f"🎁 <b>Ваш друг приглашает вас в «Семена Знаний»!</b>\n\n"
                f"Примите подарок — <b>скидку 10%</b> на первый заказ?\n\n"
                f"А ваш друг получит <b>скидку 15%</b> на следующий заказ 🎉",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            return

    await _send_start_menu(message)


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обычный старт без параметров"""
    await _send_start_menu(message)


async def _send_start_menu(message: Message):
    """Отправка стартового меню"""
    builder = InlineKeyboardBuilder()
    builder.button(text="🌱 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))
    builder.button(text="📜 Мои заказы", callback_data="my_orders")
    builder.button(text="👥 Пригласить друга", callback_data="invite_friend")
    builder.button(text="🆘 Поддержка", callback_data="support")
    builder.button(text="ℹ️ О магазине", callback_data="about")
    builder.adjust(1)
    await message.answer(
        "🌿 Добро пожаловать в <b>Семена Знаний</b>!\n\n"
        "Книжный магазин, где цена указана за одну страницу.\n"
        "Нажмите кнопку ниже, чтобы открыть каталог 👇",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )


@router.message(Command("orders"))
async def cmd_orders(message: Message):
    await show_user_orders(message, message.from_user.id)


@router.message(F.web_app_data)
async def handle_webapp_data(message: Message, bot: Bot):
    try:
        data = json.loads(message.web_app_data.data)
        if data.get('action') == 'checkout':
            await process_checkout(message, data, bot)
    except Exception as e:
        print(f"Ошибка web_app_data: {e}")
        await message.answer("❌ Ошибка при обработке данных")


@router.callback_query(F.data == "my_orders")
async def my_orders_callback(callback: CallbackQuery):
    await show_user_orders(callback, callback.from_user.id)


@router.callback_query(F.data == "about")
async def about_callback(callback: CallbackQuery):
    await callback.message.answer(
        "📚 <b>Семена Знаний</b> — это:\n"
        "• Ботанические атласы и травники\n"
        "• Книги о садоводстве и флористике\n"
        "• Альбомы с акварельной иллюстрацией\n"
        "• Художественная литература о природе\n\n"
        "📍 Ждём вас в Mini App!",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "support")
async def support_callback(callback: CallbackQuery):
    """Обработка кнопки поддержки"""
    support_pending_users.add(callback.from_user.id)
    await callback.message.answer(
        "🆘 <b>Служба поддержки</b>\n\n"
        "Напишите ваш вопрос, и администратор ответит!\n\n"
        "/cancel для отмены",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_"))
async def confirm_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    order = await db.get_order_full(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return
    await db.update_order_status(order_id, 'confirmed')
    books_list = "\n".join([f"  • {item['title']}" for item in order['items']])
    await callback.message.answer(
        f"✅ <b>Заказ #{order_id} подтверждён!</b>\n\n{books_list}\n\n"
        f"💰 Сумма: <b>{order['total']} ₽</b>\n\n"
        f"Мы свяжемся с вами для уточнения доставки.",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_"))
async def cancel_order(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    await db.update_order_status(order_id, 'cancelled')
    await callback.message.answer(f"❌ Заказ #{order_id} отменён.\n\nВозвращайтесь, когда будете готовы! 🌱")
    await callback.answer()


@router.callback_query(F.data == "main_menu")
async def back_to_menu(callback: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="🌱 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))
    builder.button(text="📜 Мои заказы", callback_data="my_orders")
    builder.button(text="🆘 Поддержка", callback_data="support")
    builder.button(text="ℹ️ О магазине", callback_data="about")
    builder.adjust(1)
    await callback.message.edit_text(
        "🌿 <b>Семена Знаний</b>\n\nНажмите кнопку ниже, чтобы открыть каталог 👇",
        reply_markup=builder.as_markup(), parse_mode="HTML"
    )
    await callback.answer()


@router.message(F.text & ~F.text.startswith("/"))
async def universal_text_handler(message: Message, state: FSMContext, bot: Bot):
    """Единый обработчик всех текстовых сообщений с проверкой FSM"""
    user_id = message.from_user.id
    current_state = await state.get_state()

    # 🔧 ВАЖНО: пропускаем сообщения, если пользователь в состоянии настройки оплаты
    payment_states = [
        PaymentSettingsState.waiting_for_card.state,
        PaymentSettingsState.waiting_for_sbp_phone.state,
        PaymentSettingsState.waiting_for_sbp_bank.state,
        PaymentSettingsState.waiting_for_recipient.state,
        PaymentSettingsState.waiting_for_instructions.state,
        PaymentSettingsState.waiting_for_stars_rate.state,
    ]

    if current_state in payment_states:
        return  # Пропускаем — пусть обрабатывает payments.py

    # === FSM: ДОБАВЛЕНИЕ КНИГ ===
    if current_state == AddBookState.waiting_for_title.state:
        await state.update_data(title=message.text)
        await message.answer("💰 Теперь отправьте <b>цену</b> (только цифры):")
        await state.set_state(AddBookState.waiting_for_price)
        return

    if current_state == AddBookState.waiting_for_price.state:
        try:
            price = int(message.text)
            if price <= 0:
                raise ValueError
            await state.update_data(price=price)

            categories = await db.get_all_categories()
            if categories:
                builder = InlineKeyboardBuilder()
                for cat in categories:
                    builder.button(
                        text=f"{cat['emoji'] or ''} {cat['name']}".strip(),
                        callback_data=f"book_cat_{cat['id']}"
                    )
                builder.button(text="✏️ Ввести свою", callback_data="book_cat_custom")
                builder.adjust(2)

                await message.answer(
                    "📂 <b>Выберите категорию</b> из списка:\n\n"
                    "Или нажмите 'Ввести свою' для ручной настройки",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML"
                )
            else:
                await message.answer("📂 Отправьте <b>категорию</b>:")
                await state.set_state(AddBookState.waiting_for_category)
        except ValueError:
            await message.answer("❌ Некорректная цена. Отправьте целое число больше 0.")
        return

    if current_state == AddBookState.waiting_for_category.state:
        await state.update_data(category=message.text)
        await message.answer("🎨 Отправьте эмодзи или URL обложки (или 'нет'):")
        await state.set_state(AddBookState.waiting_for_emoji)
        return

    if current_state == AddBookState.waiting_for_emoji.state:
        emoji = message.text.strip()
        if emoji.lower() == 'нет':
            emoji = ''
        await state.update_data(emoji=emoji)

        await state.set_state(AddBookState.waiting_for_description)

        builder = InlineKeyboardBuilder()
        builder.button(text="⏭️ Пропустить", callback_data="skip_description")

        await message.answer(
            f"🎨 Обложка: {emoji or '📚'}\n\n"
            f"Теперь отправьте <b>описание книги</b>:\n\n"
            f"Можно несколько абзацев. Это поможет покупателям понять, о чём книга.\n\n"
            f"Или нажмите кнопку ниже, чтобы пропустить",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return

    if current_state == AddBookState.waiting_for_description.state:
        description = message.text.strip()
        data = await state.get_data()

        book_id = await db.add_book(
            data['title'],
            data['price'],
            data['category'],
            data.get('emoji', ''),
            description,
            '[]',
            data.get('category_id')
        )

        await message.answer(
            f"✅ <b>Книга добавлена!</b>\n\n"
            f"🆔 ID: {book_id}\n"
            f"📖 {data['title']}\n"
            f"💰 {data['price']} ₽\n"
            f"📂 {data['category']}\n"
            f"🎨 {data.get('emoji', '') or '📚'}\n"
            f"📝 {description[:100]}{'...' if len(description) > 100 else ''}\n\n"
            f"🎉 Теперь она доступна в Mini App!",
            parse_mode="HTML"
        )
        await state.clear()
        return

    # === FSM: РЕДАКТИРОВАНИЕ КНИГ ===
    if current_state == EditBookState.waiting_for_value.state:
        data = await state.get_data()
        book_id = data.get('edit_book_id')
        field = data.get('edit_field')

        if not book_id or not field:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        new_value = message.text.strip()

        if field == 'price':
            try:
                new_value = int(new_value)
                if new_value <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Некорректная цена.")
                return

        # 🔧 ИСПРАВЛЕНИЕ 1: При смене обложки (emoji) мы НЕ трогаем поле images!
        if field == 'emoji':
            await db.update_book_full(book_id, emoji=new_value)
        else:
            await db.update_book_full(book_id, **{field: new_value})

        book = await db.get_book(book_id)
        emoji_display = "[Картинка]" if book['emoji'].startswith('http') else book['emoji']

        builder = InlineKeyboardBuilder()
        builder.button(text="📚 В меню каталога", callback_data="admin_catalog")

        await message.answer(
            f"✅ <b>Книга обновлена!</b>\n\n"
            f"📖 {book['title']}\n"
            f"💰 {book['price']} ₽\n"
            f"📂 {book['category']}\n"
            f"🎨 {emoji_display}\n\n"
            f"✨ Изменения сразу появятся в Mini App!",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == EditBookState.waiting_for_description.state:
        data = await state.get_data()
        book_id = data.get('edit_book_id')

        if not book_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        new_description = message.text.strip()
        await db.update_book_full(book_id, description=new_description)

        builder = InlineKeyboardBuilder()
        builder.button(text="📚 В меню каталога", callback_data="admin_catalog")

        await message.answer(
            f"✅ <b>Описание обновлено!</b>\n\n"
            f"📖 {new_description[:100]}{'...' if len(new_description) > 100 else ''}\n\n"
            f"✨ Изменения сразу появятся в Mini App!",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == EditBookState.waiting_for_image_url.state:
        data = await state.get_data()
        book_id = data.get('edit_book_id')

        if not book_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        url = message.text.strip()
        book = await db.get_book(book_id)
        current_images = parseBookImages(book.get('images') or '[]')
        current_images.append(url)

        await db.update_book_full(book_id, images=json.dumps(current_images))

        await message.answer(
            f"✅ <b>Изображение добавлено в галерею!</b>\n\n"
            f"Всего фото: {len(current_images)}\n\n"
            f"Отправьте ещё URL или нажмите /cancel"
        )
        return

    # === FSM: КАТЕГОРИИ ===
    if current_state == CategoryState.waiting_for_name.state:
        name = message.text.strip()
        existing = await db.get_all_categories()
        if any(c['name'].lower() == name.lower() for c in existing):
            await message.answer("❌ Категория с таким названием уже существует. Введите другое название:")
            return

        await state.update_data(category_name=name)
        await state.set_state(CategoryState.waiting_for_emoji)

        builder = InlineKeyboardBuilder()
        builder.button(text="◀️ Назад", callback_data="admin_categories")

        await message.answer(
            f"📝 Название: <b>{name}</b>\n\n"
            f"Теперь отправьте <b>эмодзи</b> для категории (например: 📚, 🎨):\n\n"
            "Или нажмите кнопку ниже для отмены",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return

    if current_state == CategoryState.waiting_for_emoji.state:
        emoji = message.text.strip()
        data = await state.get_data()
        name = data.get('category_name')

        cat_id = await db.add_category(name, emoji)

        builder = InlineKeyboardBuilder()
        builder.button(text="📂 К категориям", callback_data="admin_categories")

        await message.answer(
            f"✅ <b>Категория добавлена!</b>\n\n"
            f"🆔 ID: {cat_id}\n"
            f"📂 Название: {name}\n"
            f"🎨 Эмодзи: {emoji}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == CategoryState.editing_name.state:
        data = await state.get_data()
        cat_id = data.get('edit_category_id')
        new_name = message.text.strip()

        if not cat_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        await db.update_category(cat_id, name=new_name)

        builder = InlineKeyboardBuilder()
        builder.button(text="📂 К категориям", callback_data="admin_categories")

        await message.answer(
            f"✅ <b>Название обновлено!</b>\n\n"
            f"Новое название: {new_name}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == CategoryState.editing_emoji.state:
        data = await state.get_data()
        cat_id = data.get('edit_category_id')
        new_emoji = message.text.strip()

        if not cat_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        await db.update_category(cat_id, emoji=new_emoji)

        builder = InlineKeyboardBuilder()
        builder.button(text="📂 К категориям", callback_data="admin_categories")

        await message.answer(
            f"✅ <b>Эмодзи обновлён!</b>\n\n"
            f"Новый эмодзи: {new_emoji}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    # === FSM: ПРОМОКОДЫ ===
    if current_state == PromoCodeState.waiting_for_code.state:
        code = message.text.strip().upper()
        existing = await db.get_all_promo_codes()
        if any(p['code'] == code for p in existing):
            await message.answer("❌ Промокод с таким кодом уже существует. Введите другой:")
            return

        await state.update_data(promo_code=code)
        await state.set_state(PromoCodeState.waiting_for_discount)

        builder = InlineKeyboardBuilder()
        builder.button(text="◀️ Назад", callback_data="admin_promo")

        await message.answer(
            f"🎟️ Код: <b>{code}</b>\n\n"
            f"Теперь введите <b>скидку</b>:\n"
            f"Формат: <b>10%</b> (процент) или <b>500</b> (фиксированная сумма в ₽)\n\n"
            f"Или нажмите кнопку ниже для отмены",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return

    if current_state == PromoCodeState.waiting_for_discount.state:
        discount_text = message.text.strip()
        discount_percent = 0
        discount_fixed = 0

        if discount_text.endswith('%'):
            try:
                discount_percent = int(discount_text[:-1])
                if discount_percent <= 0 or discount_percent > 100:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Неверный процент. Введите число от 1 до 100 с символом % (например: 10%):")
                return
        else:
            try:
                discount_fixed = int(discount_text)
                if discount_fixed <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Неверная сумма. Введите целое число больше 0 (например: 500) или процент с % (например: 10%):")
                return

        await state.update_data(discount_percent=discount_percent, discount_fixed=discount_fixed)
        await state.set_state(PromoCodeState.waiting_for_min_order)

        builder = InlineKeyboardBuilder()
        builder.button(text="◀️ Назад", callback_data="admin_promo")

        await message.answer(
            f"💰 Скидка: {discount_text}\n\n"
            f"Теперь введите <b>минимальную сумму заказа</b> в ₽ (или 0 для без ограничений):\n\n"
            f"Или нажмите кнопку ниже для отмены",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        return

    if current_state == PromoCodeState.waiting_for_min_order.state:
        try:
            min_order = int(message.text.strip())
            if min_order < 0:
                raise ValueError
            await state.update_data(min_order=min_order)
            await state.set_state(PromoCodeState.waiting_for_max_uses)

            builder = InlineKeyboardBuilder()
            builder.button(text="◀️ Назад", callback_data="admin_promo")

            await message.answer(
                f"📊 Мин. сумма: {min_order} ₽\n\n"
                f"Теперь введите <b>максимальное количество использований</b> (или 0 для без лимита):\n\n"
                f"Или нажмите кнопку ниже для отмены",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        except ValueError:
            await message.answer("❌ Введите целое число >= 0")
        return

    if current_state == PromoCodeState.waiting_for_max_uses.state:
        try:
            max_uses = int(message.text.strip())
            if max_uses < 0:
                raise ValueError
            await state.update_data(max_uses=max_uses)
            await state.set_state(PromoCodeState.waiting_for_expires)

            builder = InlineKeyboardBuilder()
            builder.button(text="◀️ Назад", callback_data="admin_promo")

            await message.answer(
                f"🔢 Лимит: {max_uses if max_uses > 0 else '∞'}\n\n"
                f"Теперь введите <b>дату окончания</b> в формате ГГГГ-ММ-ДД (например: 2026-12-31):\n\n"
                f"Или отправьте 'бессрочно' для промокода без срока\n\n"
                f"Или нажмите кнопку ниже для отмены",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        except ValueError:
            await message.answer("❌ Введите целое число >= 0")
        return

    if current_state == PromoCodeState.waiting_for_expires.state:
        expires_text = message.text.strip()
        expires_at = None

        if expires_text.lower() != 'бессрочно':
            try:
                from datetime import datetime
                expires_at = datetime.strptime(expires_text, '%Y-%m-%d').isoformat()
            except ValueError:
                await message.answer("❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД (например: 2026-12-31) или отправьте 'бессрочно':")
                return

        data = await state.get_data()
        promo_id = await db.add_promo_code(
            data['promo_code'],
            data.get('discount_percent', 0),
            data.get('discount_fixed', 0),
            data.get('min_order', 0),
            data.get('max_uses', 0),
            expires_at
        )

        discount_text = f"{data.get('discount_percent', 0)}%" if data.get('discount_percent', 0) > 0 else f"{data.get('discount_fixed', 0)}₽"

        builder = InlineKeyboardBuilder()
        builder.button(text="🎟️ К промокодам", callback_data="admin_promo")

        await message.answer(
            f"✅ <b>Промокод создан!</b>\n\n"
            f"🎟️ Код: <b>{data['promo_code']}</b>\n"
            f"💰 Скидка: {discount_text}\n"
            f"📊 Мин. сумма: {data.get('min_order', 0)} ₽\n"
            f"🔢 Лимит: {data.get('max_uses', 0) if data.get('max_uses', 0) > 0 else '∞'}\n"
            f"📅 Действует до: {expires_text}\n\n"
            f"ID: {promo_id}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    # Обработка редактирования промокода
    if current_state == PromoCodeState.editing_discount.state:
        data = await state.get_data()
        promo_id = data.get('edit_promo_id')

        if not promo_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        discount_text = message.text.strip()
        discount_percent = 0
        discount_fixed = 0

        if discount_text.endswith('%'):
            try:
                discount_percent = int(discount_text[:-1])
                if discount_percent <= 0 or discount_percent > 100:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Неверный процент.")
                return
        else:
            try:
                discount_fixed = int(discount_text)
                if discount_fixed <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("❌ Неверная сумма.")
                return

        await db.update_promo_code(promo_id, discount_percent=discount_percent, discount_fixed=discount_fixed)

        builder = InlineKeyboardBuilder()
        builder.button(text="🎟️ К промокодам", callback_data="admin_promo")

        await message.answer(
            f"✅ <b>Скидка обновлена!</b>\n\n"
            f"Новая скидка: {discount_text}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == PromoCodeState.editing_min_order.state:
        data = await state.get_data()
        promo_id = data.get('edit_promo_id')

        if not promo_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        try:
            min_order = int(message.text.strip())
            if min_order < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите целое число >= 0")
            return

        await db.update_promo_code(promo_id, min_order=min_order)

        builder = InlineKeyboardBuilder()
        builder.button(text="🎟️ К промокодам", callback_data="admin_promo")

        await message.answer(
            f"✅ <b>Минимальная сумма обновлена!</b>\n\n"
            f"Новая мин. сумма: {min_order} ₽",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == PromoCodeState.editing_max_uses.state:
        data = await state.get_data()
        promo_id = data.get('edit_promo_id')

        if not promo_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        try:
            max_uses = int(message.text.strip())
            if max_uses < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите целое число >= 0")
            return

        await db.update_promo_code(promo_id, max_uses=max_uses)

        builder = InlineKeyboardBuilder()
        builder.button(text="🎟️ К промокодам", callback_data="admin_promo")

        await message.answer(
            f"✅ <b>Лимит обновлён!</b>\n\n"
            f"Новый лимит: {max_uses if max_uses > 0 else '∞'}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    if current_state == PromoCodeState.editing_expires.state:
        data = await state.get_data()
        promo_id = data.get('edit_promo_id')

        if not promo_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        expires_text = message.text.strip()
        expires_at = None

        if expires_text.lower() != 'бессрочно':
            try:
                from datetime import datetime
                expires_at = datetime.strptime(expires_text, '%Y-%m-%d').isoformat()
            except ValueError:
                await message.answer("❌ Неверный формат даты.")
                return

        await db.update_promo_code(promo_id, expires_at=expires_at)

        builder = InlineKeyboardBuilder()
        builder.button(text="🎟️ К промокодам", callback_data="admin_promo")

        await message.answer(
            f"✅ <b>Срок действия обновлён!</b>\n\n"
            f"Новый срок: {expires_text}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )
        await state.clear()
        return

    # === FSM: НОВАЯ КАТЕГОРИЯ ПРИ РЕДАКТИРОВАНИИ ===
    if current_state == EditBookState.waiting_for_new_category.state:
        data = await state.get_data()
        book_id = data.get('edit_book_id')
        new_category_name = message.text.strip()

        if not book_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        categories = await db.get_all_categories()
        existing_cat = next((c for c in categories if c['name'].lower() == new_category_name.lower()), None)

        if existing_cat:
            await db.update_book_full(book_id, category=existing_cat['name'], category_id=existing_cat['id'])
            builder = InlineKeyboardBuilder()
            builder.button(text="📚 В меню каталога", callback_data="admin_catalog")
            builder.button(text="✏️ Продолжить редактирование", callback_data=f"catalog_edit_{book_id}")
            builder.adjust(1)
            await message.answer(
                f"✅ <b>Категория найдена и применена!</b>\n\n"
                f"📂 {existing_cat['emoji'] or ''} {existing_cat['name']}",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        else:
            new_cat_id = await db.add_category(new_category_name, "")
            await db.update_book_full(book_id, category=new_category_name, category_id=new_cat_id)
            builder = InlineKeyboardBuilder()
            builder.button(text="📚 В меню каталога", callback_data="admin_catalog")
            builder.button(text="✏️ Продолжить редактирование", callback_data=f"catalog_edit_{book_id}")
            builder.adjust(1)
            await message.answer(
                f"✅ <b>Новая категория создана и применена!</b>\n\n"
                f"📂 {new_category_name}\n\n"
                f"💡 Вы можете добавить эмодзи для неё через '📂 Управление категориями'",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        await state.clear()
        return

    # === РАССЫЛКА ===
    # === РАССЫЛКА ===
    if is_admin(user_id) and user_id in broadcast_pending_users:
        broadcast_pending_users.discard(user_id)
        user_ids = await db.get_all_unique_users()

        if not user_ids:
            await message.answer("📭 Нет пользователей для рассылки.")
            return

        await message.answer(
            f"📢 Начинаю рассылку <b>{len(user_ids)}</b> пользователям... Это может занять некоторое время.",
            parse_mode="HTML")

        success, failed = 0, 0
        for uid in user_ids:
            try:
                await bot.send_message(uid, message.text, parse_mode="HTML")
                success += 1
            except Exception:
                # Ошибка обычно означает, что пользователь заблокировал бота или удалил аккаунт
                failed += 1
            await asyncio.sleep(0.05)  # Небольшая задержка, чтобы не получить бан от Telegram API

        await message.answer(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"📤 Успешно отправлено: <b>{success}</b>\n"
            f"⚠️ Ошибок (заблокировали бота): <b>{failed}</b>",
            parse_mode="HTML"
        )
        return

    # === ПОДДЕРЖКА ===
    if user_id in support_pending_users:
        support_pending_users.discard(user_id)
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🆘 <b>Вопрос от пользователя</b>\n\n"
                    f"👤 {message.from_user.full_name} (ID: {message.from_user.id})\n"
                    f"💬 Текст: {message.text}\n\n"
                    f"Чтобы ответить:\n<code>/reply_{message.from_user.id} ваш_ответ</code>",
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"Не удалось отправить админу {admin_id}: {e}")
        await message.answer("✅ Ваш вопрос отправлен администратору!\nМы ответим в ближайшее время. 🌱")
        return


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    """Обработка загруженных фото"""
    current_state = await state.get_state()

    if current_state == EditBookState.waiting_for_image_photo.state:
        data = await state.get_data()
        book_id = data.get('edit_book_id')

        if not book_id:
            await message.answer("❌ Ошибка")
            await state.clear()
            return

        photo = message.photo[-1]
        file = await message.bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"

        book = await db.get_book(book_id)
        current_images = parseBookImages(book.get('images') or '[]')
        current_images.append(file_url)

        # 🔧 ИСПРАВЛЕНИЕ 2: При добавлении фото мы обновляем ТОЛЬКО images, НЕ трогаем emoji (обложку)!
        await db.update_book_full(book_id, images=json.dumps(current_images))

        await message.answer(
            f"✅ <b>Фото добавлено в галерею!</b>\n\n"
            f"Всего фото: {len(current_images)}\n\n"
            f"Отправьте ещё фото или нажмите /cancel"
        )
    else:
        await message.answer("❌ Сейчас не ожидаю фото. Используйте /cancel для отмены.")


@router.message(F.text.startswith("/reply_"))
async def admin_reply_to_user(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        parts = message.text.split(maxsplit=1)
        user_id = int(parts[0].replace("/reply_", ""))
        reply_text = parts[1] if len(parts) > 1 else "Без текста"
        await message.bot.send_message(
            user_id,
            f"💬 <b>Ответ от поддержки «Семена Знаний»:</b>\n\n{reply_text}",
            parse_mode="HTML"
        )
        await message.answer(f"✅ Ответ отправлен пользователю ID {user_id}!")
    except (ValueError, IndexError):
        await message.answer("❌ Неверный формат: `/reply_ID текст`", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("cancel"))
async def cancel_action(message: Message, state: FSMContext):
    user_id = message.from_user.id
    was_active = False

    current_state = await state.get_state()
    if current_state:
        await state.clear()
        was_active = True

    broadcast_pending_users.discard(user_id)
    support_pending_users.discard(user_id)

    if was_active:
        await message.answer("✅ Действие отменено.")


async def show_user_orders(message_or_callback, user_id: int):
    try:
        orders = await db.get_user_orders(user_id)
        if not orders:
            text = "📭 У вас пока нет заказов.\n\nЗагляните в магазин! 🌱"
        else:
            status_emoji = {'new': '🆕', 'confirmed': '✅', 'completed': '📦', 'cancelled': '❌', 'awaiting_payment': '💳', 'awaiting_stars_payment': '⭐', 'payment_pending': '⏳', 'paid': '💰'}
            status_names = {'new': 'Новый', 'confirmed': 'Подтверждён', 'completed': 'Выполнен', 'cancelled': 'Отменён', 'awaiting_payment': 'Ожидает оплаты', 'awaiting_stars_payment': 'Ожидает Stars', 'payment_pending': 'Ожидает подтверждения', 'paid': 'Оплачен'}
            orders_list = [
                f"{status_emoji.get(o['status'], '')} Заказ #{o['id']} · {o['total']} ₽ · {status_names.get(o['status'], o['status'])} · {format_local_time(o['created_at'])}"
                for o in orders]
            text = "📜 <b>Ваши заказы:</b>\n\n" + "\n".join(orders_list)
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text, parse_mode="HTML")
        else:
            await message_or_callback.message.answer(text, parse_mode="HTML")
            await message_or_callback.answer()
    except Exception as e:
        print(f"Ошибка заказов: {e}")
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer("❌ Ошибка при загрузке заказов")
        else:
            await message_or_callback.message.answer("❌ Ошибка при загрузке заказов")
            await message_or_callback.answer()


async def process_checkout(message: Message, data: dict, bot: Bot):
    """Заглушка — теперь заказы обрабатываются через сервер напрямую"""
    pass


@router.callback_query(F.data == "ref_accept")
async def ref_accept_callback(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    referrer_id = data.get('referrer_id')
    referred_id = callback.from_user.id

    if not referrer_id:
        await callback.answer("❌ Ошибка", show_alert=True)
        return

    await db.create_referral(referrer_id, referred_id)
    await db.add_user_bonus(referrer_id, 'percent', 15)
    await db.add_user_bonus(referred_id, 'percent', 10)

    await state.clear()

    await callback.message.edit_text(
        f"🎉 <b>Отлично!</b>\n\n"
        f"✅ Вам начислена <b>скидка 10%</b> на первый заказ!\n"
        f"✅ Ваш друг получил <b>скидку 15%</b> на следующий заказ!\n\n"
        f"Скидка применится автоматически при оформлении заказа в Mini App 🛒",
        parse_mode="HTML"
    )
    await callback.answer()

    try:
        await bot.send_message(
            referrer_id,
            f"🎁 <b>Ваш друг присоединился!</b>\n\n"
            f"Вы получили <b>скидку 15%</b> на следующий заказ!\n"
            f"Скидка применится автоматически 🎉",
            parse_mode="HTML"
        )
    except Exception:
        pass


@router.callback_query(F.data == "ref_decline")
async def ref_decline_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "Хорошо! Добро пожаловать в «Семена Знаний» 🌿\n\n"
        "Нажмите кнопку ниже, чтобы открыть каталог 👇",
    )
    await callback.answer()


@router.callback_query(F.data == "invite_friend")
async def invite_friend_callback(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    ref_code = await db.get_referral_code(user_id)
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={ref_code}"

    stats = await db.get_referral_stats(user_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="📤 Поделиться",
                   switch_inline_query=f"🌿 Приглашаю тебя в книжный магазин «Семена Знаний»!\n\nПолучи скидку 10% на первый заказ по моей ссылке:\n{ref_link}")
    builder.button(text="◀️ Назад", callback_data="main_menu")
    builder.adjust(1)

    await callback.message.answer(
        f"👥 <b>Пригласи друга — получи бонус!</b>\n\n"
        f"🔗 <b>Твоя ссылка:</b>\n<code>{ref_link}</code>\n\n"
        f"📊 <b>Твоя статистика:</b>\n"
        f"• Приглашено друзей: <b>{stats['total_invited']}</b>\n"
        f"• Получено бонусов: <b>{stats['bonuses_earned']}</b>\n"
        f"• Активных скидок: <b>{stats['active_bonuses']}</b>\n\n"
        f"🎁 <b>Как это работает:</b>\n"
        f"• Друг получает <b>скидку 10%</b> на первый заказ\n"
        f"• Ты получаешь <b>скидку 15%</b> на следующий заказ\n"
        f"• Скидки применяются автоматически в Mini App",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "skip_description")
async def skip_description(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    book_id = await db.add_book(
        data['title'],
        data['price'],
        data['category'],
        data.get('emoji', ''),
        '',
        '[]',
        data.get('category_id')
    )

    await callback.message.answer(
        f"✅ <b>Книга добавлена!</b>\n\n"
        f"🆔 ID: {book_id}\n"
        f"📖 {data['title']}\n"
        f"💰 {data['price']} ₽\n"
        f"📂 {data['category']}\n"
        f"🎨 {data.get('emoji', '') or '📚'}\n"
        f"📝 Описание не добавлено\n\n"
        f"🎉 Теперь она доступна в Mini App!\n\n"
        f"💡 Вы можете добавить описание позже через редактирование.",
        parse_mode="HTML"
    )
    await state.clear()
    await callback.answer()