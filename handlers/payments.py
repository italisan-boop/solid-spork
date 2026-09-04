from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext

import db
from db.orders import save_admin_notification_ids, clear_admin_notifications
from config import settings
from states import PaymentSettingsState

router = Router()

print("✅ payments.py загружен")


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


# ============================================
# НАСТРОЙКИ ОПЛАТЫ (КАРТА/СБП)
# ============================================

@router.callback_query(F.data == "admin_payments")
async def admin_payments_menu(callback: CallbackQuery):
    print(f"🔍 [DEBUG] admin_payments_menu вызван")
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    settings = await db.get_all_payment_settings()

    card = settings.get('card_number', '') or 'Не задан'
    sbp_phone = settings.get('sbp_phone', '') or 'Не задан'
    sbp_bank = settings.get('sbp_bank', '') or 'Не задан'
    recipient = settings.get('recipient_name', '') or 'Не задан'
    enabled = '✅ Включена' if settings.get('payment_enabled') == '1' else '❌ Отключена'

    builder = InlineKeyboardBuilder()
    builder.button(text=f"💳 Карта: {card[:8]}..." if len(card) > 8 else f"💳 Карта: {card}",
                   callback_data="pay_set_card")
    builder.button(text=f"📱 СБП: {sbp_phone[:6]}..." if len(sbp_phone) > 6 else f"📱 СБП: {sbp_phone}",
                   callback_data="pay_set_sbp_phone")
    builder.button(text=f"🏦 Банк: {sbp_bank}", callback_data="pay_set_sbp_bank")
    builder.button(text=f"👤 Получатель: {recipient}", callback_data="pay_set_recipient")
    builder.button(text="📝 Инструкция", callback_data="pay_set_instructions")
    status_text = "🔴 Отключить оплату" if settings.get('payment_enabled') == '1' else "🟢 Включить оплату"
    builder.button(text=status_text, callback_data="pay_toggle_enabled")
    builder.button(text="◀️ Назад", callback_data="admin_menu")
    builder.adjust(1)

    await callback.message.edit_text(
        f"💳 <b>Настройки оплаты</b>\n\n"
        f"Статус: {enabled}\n\n"
        f"📋 <b>Текущие реквизиты:</b>\n"
        f"💳 Карта: <code>{card}</code>\n"
        f"📱 СБП телефон: <code>{sbp_phone}</code>\n"
        f"🏦 Банк для СБП: {sbp_bank}\n"
        f"👤 Получатель: {recipient}\n\n"
        f"Выберите, что изменить:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "pay_toggle_enabled")
async def pay_toggle_enabled(callback: CallbackQuery):
    print(f"🔍 [DEBUG] pay_toggle_enabled вызван")
    if not is_admin(callback.from_user.id): return

    current = await db.get_payment_setting('payment_enabled', '1')
    new_value = '0' if current == '1' else '1'
    await db.set_payment_setting('payment_enabled', new_value)

    status = "✅ Включена" if new_value == '1' else "❌ Отключена"
    await callback.answer(f"Оплата {status}", show_alert=True)
    await admin_payments_menu(callback)


@router.callback_query(F.data == "pay_set_card")
async def pay_set_card_start(callback: CallbackQuery, state: FSMContext):
    print(f"🔍 [DEBUG] pay_set_card_start вызван")
    if not is_admin(callback.from_user.id): return
    await state.set_state(PaymentSettingsState.waiting_for_card)
    await callback.message.answer(
        "💳 Введите <b>номер карты</b>:\n\n"
        "Формат: <code>2200 1234 5678 9012</code>\n\n"
        "/cancel для отмены",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "pay_set_sbp_phone")
async def pay_set_sbp_phone_start(callback: CallbackQuery, state: FSMContext):
    print(f"🔍 [DEBUG] pay_set_sbp_phone_start вызван")
    if not is_admin(callback.from_user.id): return
    await state.set_state(PaymentSettingsState.waiting_for_sbp_phone)
    await callback.message.answer(
        "📱 Введите <b>номер телефона</b> для СБП:\n\n"
        "Формат: <code>+7 999 123 45 67</code>\n\n"
        "/cancel для отмены",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "pay_set_sbp_bank")
async def pay_set_sbp_bank_start(callback: CallbackQuery, state: FSMContext):
    print(f"🔍 [DEBUG] pay_set_sbp_bank_start вызван")
    if not is_admin(callback.from_user.id): return
    await state.set_state(PaymentSettingsState.waiting_for_sbp_bank)
    await callback.message.answer(
        "🏦 Введите <b>название банка</b>:\n\n"
        "Например: Сбербанк, Тинькофф\n\n"
        "/cancel для отмены",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "pay_set_recipient")
async def pay_set_recipient_start(callback: CallbackQuery, state: FSMContext):
    print(f"🔍 [DEBUG] pay_set_recipient_start вызван")
    if not is_admin(callback.from_user.id): return
    await state.set_state(PaymentSettingsState.waiting_for_recipient)
    await callback.message.answer(
        "👤 Введите <b>ФИО получателя</b>:\n\n"
        "Например: Иванов Иван Иванович\n\n"
        "/cancel для отмены",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "pay_set_instructions")
async def pay_set_instructions_start(callback: CallbackQuery, state: FSMContext):
    print(f"🔍 [DEBUG] pay_set_instructions_start вызван")
    if not is_admin(callback.from_user.id): return
    await state.set_state(PaymentSettingsState.waiting_for_instructions)

    current = await db.get_payment_setting('payment_instructions', '')
    await callback.message.answer(
        f"📝 Введите <b>инструкцию по оплате</b>:\n\n"
        f"Текущая:\n<i>{current}</i>\n\n"
        f"/cancel для отмены",
        parse_mode="HTML"
    )
    await callback.answer()


# Обработка текстовых сообщений для настройки оплаты
@router.message(PaymentSettingsState.waiting_for_card)
async def process_card(message: Message, state: FSMContext):
    print(f"🔍 [DEBUG] process_card: {message.text}")
    await db.set_payment_setting('card_number', message.text.strip())
    await state.clear()
    await message.answer(f"✅ Номер карты обновлён: <code>{message.text.strip()}</code>", parse_mode="HTML")


@router.message(PaymentSettingsState.waiting_for_sbp_phone)
async def process_sbp_phone(message: Message, state: FSMContext):
    print(f"🔍 [DEBUG] process_sbp_phone: {message.text}")
    await db.set_payment_setting('sbp_phone', message.text.strip())
    await state.clear()
    await message.answer(f"✅ Телефон для СБП обновлён: <code>{message.text.strip()}</code>", parse_mode="HTML")


@router.message(PaymentSettingsState.waiting_for_sbp_bank)
async def process_sbp_bank(message: Message, state: FSMContext):
    print(f"🔍 [DEBUG] process_sbp_bank: {message.text}")
    await db.set_payment_setting('sbp_bank', message.text.strip())
    await state.clear()
    await message.answer(f"✅ Банк для СБП обновлён: {message.text.strip()}")


@router.message(PaymentSettingsState.waiting_for_recipient)
async def process_recipient(message: Message, state: FSMContext):
    print(f"🔍 [DEBUG] process_recipient: {message.text}")
    await db.set_payment_setting('recipient_name', message.text.strip())
    await state.clear()
    await message.answer(f"✅ Получатель обновлён: {message.text.strip()}")


@router.message(PaymentSettingsState.waiting_for_instructions)
async def process_instructions(message: Message, state: FSMContext):
    print(f"🔍 [DEBUG] process_instructions: {message.text}")
    await db.set_payment_setting('payment_instructions', message.text.strip())
    await state.clear()
    await message.answer(f"✅ Инструкция обновлена!")


# ============================================
# НАСТРОЙКИ TELEGRAM STARS
# ============================================

@router.callback_query(F.data == "admin_stars_settings")
async def admin_stars_menu(callback: CallbackQuery):
    print(f"🔍 [DEBUG] admin_stars_menu вызван")
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    rubles_per_star = await db.get_stars_setting('rubles_per_star', '2')
    enabled = await db.get_stars_setting('stars_enabled', '0')
    status = '✅ Включена' if enabled == '1' else '❌ Отключена'

    builder = InlineKeyboardBuilder()
    builder.button(text=f"💱 Курс: 1 Star = {rubles_per_star}₽", callback_data="stars_set_rate")
    status_text = "🔴 Отключить Stars" if enabled == '1' else "🟢 Включить Stars"
    builder.button(text=status_text, callback_data="stars_toggle")
    builder.button(text="◀️ Назад", callback_data="admin_menu")
    builder.adjust(1)

    await callback.message.edit_text(
        f"⭐ <b>Настройки Telegram Stars</b>\n\n"
        f"Статус: {status}\n"
        f"💱 Курс: 1 Star = {rubles_per_star} ₽\n\n"
        f"<b>Как работает:</b>\n"
        f"• Пользователь оплачивает в Stars\n"
        f"• Telegram конвертирует автоматически\n"
        f"• Вы получаете Stars на свой аккаунт\n"
        f"• Комиссия Telegram: ~15-30%\n\n"
        f"⚠️ <b>Важно:</b> Stars можно использовать только для цифровых товаров!",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "stars_toggle")
async def stars_toggle(callback: CallbackQuery):
    print(f"🔍 [DEBUG] stars_toggle вызван")
    if not is_admin(callback.from_user.id): return

    current = await db.get_stars_setting('stars_enabled', '0')
    new_value = '0' if current == '1' else '1'
    await db.set_stars_setting('stars_enabled', new_value)

    status = "✅ Включена" if new_value == '1' else "❌ Отключена"
    await callback.answer(f"Оплата Stars {status}", show_alert=True)
    await admin_stars_menu(callback)


@router.callback_query(F.data == "stars_set_rate")
async def stars_set_rate_start(callback: CallbackQuery, state: FSMContext):
    print(f"🔍 [DEBUG] stars_set_rate_start вызван")
    if not is_admin(callback.from_user.id): return

    await state.set_state(PaymentSettingsState.waiting_for_stars_rate)

    current = await db.get_stars_setting('rubles_per_star', '2')
    await callback.message.answer(
        f"💱 Введите <b>курс конвертации</b>:\n\n"
        f"Сколько рублей в одном Star?\n\n"
        f"Текущий курс: <b>{current} ₽</b>\n\n"
        f"/cancel для отмены",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(PaymentSettingsState.waiting_for_stars_rate)
async def process_stars_rate(message: Message, state: FSMContext):
    print(f"🔍 [DEBUG] process_stars_rate: {message.text}")

    try:
        rate = int(message.text.strip())
        if rate <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число больше 0")
        return

    await db.set_stars_setting('rubles_per_star', str(rate))
    await state.clear()
    await message.answer(f"✅ Курс обновлён: 1 Star = {rate} ₽")


# ============================================
# ОБРАБОТКА ПЛАТЕЖЕЙ STARS
# ============================================

@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    print(f"🔍 [DEBUG] process_pre_checkout вызван")
    try:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    except Exception as e:
        print(f"❌ Ошибка pre_checkout: {e}")
        await bot.answer_pre_checkout_query(
            pre_checkout_query.id,
            ok=False,
            error_message="Ошибка обработки платежа"
        )


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, bot: Bot):
    print(f"🔍 [DEBUG] process_successful_payment вызван")
    payment = message.successful_payment

    try:
        order_id = int(payment.invoice_payload)
    except (ValueError, AttributeError):
        await message.answer("❌ Ошибка: не удалось определить заказ")
        return

    order = await db.get_order_full(order_id)
    if not order:
        await message.answer("❌ Заказ не найден")
        return

    await db.update_order_status(order_id, 'paid')

    items_list = "\n".join([f"• {item['title']}" for item in order['items']])

    await message.answer(
        f"✅ <b>Оплата успешна!</b>\n\n"
        f"📦 Заказ #{order_id}\n"
        f"{items_list}\n\n"
        f"💰 Оплачено: {payment.total_amount} Stars\n\n"
        f"Спасибо за покупку! 🌿",
        parse_mode="HTML"
    )

    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"💳 <b>Оплата через Stars получена!</b>\n\n"
                f"📦 Заказ: #{order_id}\n"
                f"👤 Клиент: {order['user_name']} (ID: {order['user_id']})\n"
                f"⭐ Сумма: {payment.total_amount} Stars\n"
                f"💵 В рублях: ~{order['total']} ₽",
                parse_mode="HTML"
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("user_paid_"))
async def user_confirm_payment(callback: CallbackQuery, bot: Bot):
    """Пользователь нажал 'Я оплатил'"""
    print(f"🔍 [DEBUG] user_confirm_payment вызван: {callback.data}")

    if not callback.data.startswith("user_paid_"):
        return

    try:
        order_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка: неверный номер заказа", show_alert=True)
        return

    print(f"🔍 [DEBUG] order_id: {order_id}")

    order = await db.get_order_full(order_id)
    if not order:
        print(f"❌ Заказ #{order_id} не найден")
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return

    print(f"🔍 [DEBUG] Текущий статус заказа: {order['status']}")

    # Более гибкая проверка: заказ не должен быть уже оплачен/подтверждён/выполнен
    if order['status'] in ['paid', 'confirmed', 'completed']:
        await callback.answer("Этот заказ уже обработан ✅", show_alert=True)
        return

    # Обновляем статус на "ожидает подтверждения"
    await db.update_order_status(order_id, 'payment_pending')

    await callback.message.edit_text(
        f"✅ <b>Отлично!</b>\n\n"
        f"Мы получили информацию о вашей оплате.\n"
        f"Администратор проверит поступление и подтвердит заказ.\n\n"
        f"Обычно это занимает 5-15 минут 🕐\n\n"
        f"Номер заказа: <b>#{order_id}</b>\n"
        f"Сумма: <b>{order['total']} ₽</b>\n\n"
        f"Если есть вопросы — нажмите 🆘 Поддержка",
        parse_mode="HTML"
    )
    await callback.answer("✅ Информация отправлена администратору!", show_alert=True)

    # Уведомляем админов и сохраняем ID сообщений для последующего удаления
    admin_ids = []
    message_ids = []
    
    for admin_id in settings.ADMIN_IDS:
        try:
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Подтвердить оплату", callback_data=f"admin_paid_{order_id}")

            msg = await bot.send_message(
                admin_id,
                f"💰 <b>Пользователь сообщил об оплате!</b>\n\n"
                f"📦 Заказ: #{order_id}\n"
                f"👤 Клиент: {order['user_name']} (ID: {order['user_id']})\n"
                f"💵 Сумма: {order['total']} ₽\n\n"
                f"Проверьте поступление и подтвердите:",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            print(f"✅ Уведомление отправлено админу {admin_id}, message_id={msg.message_id}")
            admin_ids.append(admin_id)
            message_ids.append(msg.message_id)
        except Exception as e:
            print(f"❌ Ошибка отправки админу {admin_id}: {e}")
    
    # Сохраняем ID сообщений в БД
    if admin_ids and message_ids:
        await save_admin_notification_ids(order_id, admin_ids, message_ids)


@router.callback_query(F.data.startswith("admin_paid_"))
async def admin_confirm_payment(callback: CallbackQuery, bot: Bot):
    """Админ подтверждает оплату"""
    print(f"🔍 [DEBUG] admin_confirm_payment вызван: {callback.data}")
    print(f"🔍 [DEBUG] От: {callback.from_user.id}")

    if not is_admin(callback.from_user.id):
        print(f"❌ Пользователь {callback.from_user.id} не админ")
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        order_id = int(callback.data.split("_")[-1])
        print(f"🔍 [DEBUG] order_id: {order_id}")
    except (ValueError, IndexError) as e:
        print(f"❌ Ошибка парсинга order_id: {e}")
        await callback.answer("❌ Неверный номер заказа", show_alert=True)
        return

    order = await db.get_order_full(order_id)
    if not order:
        print(f"❌ Заказ #{order_id} не найден")
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return

    print(f"🔍 [DEBUG] Текущий статус заказа: {order['status']}")

    # Обновляем статус на "подтверждён"
    try:
        await db.update_order_status(order_id, 'confirmed')
        print(f"✅ Статус заказа #{order_id} обновлён на 'confirmed'")
    except Exception as e:
        print(f"❌ Ошибка обновления статуса: {e}")
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)
        return

    # Удаляем уведомления у всех админов
    try:
        await clear_admin_notifications(order_id, bot)
        print(f"✅ Уведомления админам для заказа #{order_id} удалены")
    except Exception as e:
        print(f"⚠️ Не удалось удалить уведомления: {e}")

    # Обновляем сообщение
    try:
        await callback.message.edit_text(
            f"✅ <b>Оплата заказа #{order_id} подтверждена!</b>\n\n"
            f"👤 Клиент: {order['user_name']} (ID: {order['user_id']})\n"
            f"💰 Сумма: {order['total']} ₽\n\n"
            f"Пользователь уведомлён.",
            parse_mode="HTML"
        )
        print(f"✅ Сообщение обновлено")
    except Exception as e:
        print(f"⚠️ Не удалось обновить сообщение: {e}")
        # Продолжаем, даже если не удалось обновить сообщение

    await callback.answer("✅ Оплата подтверждена!", show_alert=True)

    # Уведомляем пользователя
    try:
        items_list = "\n".join([f"• {item['title']}" for item in order['items']])
        await bot.send_message(
            order['user_id'],
            f"✅ <b>Ваш заказ #{order_id} оплачен и подтверждён!</b>\n\n"
            f"📦 Товары:\n{items_list}\n\n"
            f"💰 Сумма: {order['total']} ₽\n\n"
            f"Мы готовим его к отправке 📦\n"
            f"Спасибо, что выбрали «Семена Знаний»! 🌿",
            parse_mode="HTML"
        )
        print(f"✅ Пользователь {order['user_id']} уведомлён")
    except Exception as e:
        print(f"⚠️ Не удалось уведомить пользователя {order['user_id']}: {e}")


from datetime import datetime
from aiogram.filters import Command


# ... (весь предыдущий код) ...

@router.message(Command("balance"))
async def cmd_balance(message: Message, bot: Bot):
    """Проверка транзакций Stars для админа"""
    if not is_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав для просмотра баланса.")
        return

    await message.answer("⏳ Загружаю историю транзакций...")

    try:
        # Получаем последние 10 транзакций через Bot API
        # type='in' означает входящие платежи (оплаты от пользователей)
        transactions = await bot.get_star_transactions(offset=0, limit=10)

        if not transactions.transactions:
            await message.answer(
                "⭐ У бота пока нет записей о транзакциях через Stars.\n\n"
                "💡 *Полный текущий баланс всегда можно посмотреть в:* \n"
                "@BotFather → Ваши боты → Выбор бота → **Balance**",
                parse_mode="Markdown"
            )
            return

        text = "⭐ <b>Последние операции со Stars:</b>\n\n"
        recent_stars = 0

        for tx in transactions.transactions:
            # Нас интересуют только входящие платежи (оплаты заказов)
            if tx.type == 'in':
                recent_stars += tx.amount
                # Конвертируем timestamp в читаемую дату
                date_str = datetime.fromtimestamp(tx.date).strftime("%d.%m.%Y %H:%M")
                description = tx.description or "Оплата заказа"

                text += f"✅ <b>+{tx.amount} Stars</b>\n"
                text += f"📅 {date_str}\n"
                text += f"📝 {description}\n\n"

        text += "━━━━━━━━━━━━━━━━━━\n"
        text += f"📊 За последние операции: <b>+{recent_stars} Stars</b>\n\n"
        text += "💡 <i>Полный текущий баланс бота можно посмотреть только в:\n"
        text += "@BotFather → Ваши боты → Выбор бота → <b>Balance</b>\n"
        text += "или на сайте <a href='https://fragment.com/bots'>fragment.com</a></i>"

        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

    except Exception as e:
        print(f"❌ Ошибка получения транзакций Stars: {e}")
        await message.answer(
            f"❌ Не удалось получить данные.\n\n"
            f"💡 Полный баланс смотрите в @BotFather → Ваш бот → **Balance**"
        )