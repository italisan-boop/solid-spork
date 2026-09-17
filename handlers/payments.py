from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from datetime import datetime, timedelta
from html import escape
from utils import setup_logger
from utils.delivery_crypto import delivery_encryption_is_available

import db
from db.orders import format_order_receipt_html, replace_admin_notification_ids, save_admin_notification_ids
from authz import has_permission_sync, recipient_ids_for_event_sync
from config import settings
from states import DeliverySettingsState, PaymentSettingsState, PaymentReceiptState

router = Router()
logger = setup_logger(__name__)

# Окно ожидания фото чека после «Я оплатил»: после него состояние сбрасывается.
RECEIPT_WAIT_TIMEOUT = timedelta(minutes=30)

logger.info("payments.py загружен")


def is_admin(user_id: int) -> bool:
    return has_permission_sync(user_id, "payment.configure")


async def _require_admin_state(message: Message, state: FSMContext) -> bool:
    if is_admin(message.from_user.id):
        return True
    await state.clear()
    await message.answer("❌ Нет прав администратора.")
    return False


@router.message(F.text == "/cancel")
async def cancel_admin_state(message: Message, state: FSMContext):
    from handlers.user import cancel_action

    await cancel_action(message, state)


# ============================================
# НАСТРОЙКИ ОПЛАТЫ (КАРТА/СБП)
# ============================================

@router.callback_query(F.data == "admin_payments")
async def admin_payments_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    payment_settings = await db.get_all_payment_settings()
    stars_enabled = await db.get_stars_setting("stars_enabled", "0")
    yookassa_enabled = payment_settings.get("yookassa_enabled") == "1"
    configured = bool(
        settings.YOOKASSA_SHOP_ID
        and settings.YOOKASSA_SECRET_KEY
        and settings.YOOKASSA_RETURN_URL.startswith("https://")
    )
    builder = InlineKeyboardBuilder()
    builder.button(
        text="💳 Реквизиты: " + ("включены" if payment_settings.get("payment_enabled") == "1" else "выключены"),
        callback_data="payment_settings:manual",
    )
    builder.button(
        text="⭐ Telegram Stars: " + ("включены" if stars_enabled == "1" else "выключены"),
        callback_data="payment_settings:stars",
    )
    builder.button(
        text="🟣 ЮKassa: " + ("включена" if yookassa_enabled else "выключена"),
        callback_data="payment_settings:yookassa",
    )
    builder.button(
        text="🚚 Доставка: " + (
            "включена" if payment_settings.get("delivery_enabled") == "1" else "выключена"
        ),
        callback_data="payment_settings:delivery",
    )
    builder.button(text="◀️ Назад", callback_data="admin_menu")
    builder.adjust(1)
    status = "✅ ключи настроены" if configured else "⚠️ ключи не настроены в .env"
    await callback.message.edit_text(
        "💳 <b>Настройки оплаты</b>\n\n"
        "Выберите способ, который нужно настроить. Покупатель увидит только включённые способы.\n\n"
        f"ЮKassa: {status}",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "payment_settings:manual")
async def admin_manual_settings(callback: CallbackQuery):
    await show_manual_settings(callback)


async def show_manual_settings(callback: CallbackQuery):
    logger.debug(f" admin_payments_menu вызван")
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
    builder.button(text="◀️ К способам оплаты", callback_data="admin_payments")
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
    logger.debug(f" pay_toggle_enabled вызван")
    if not is_admin(callback.from_user.id): return

    current = await db.get_payment_setting('payment_enabled', '1')
    new_value = '0' if current == '1' else '1'
    await db.set_payment_setting('payment_enabled', new_value)

    status = "✅ Включена" if new_value == '1' else "❌ Отключена"
    await callback.answer(f"Оплата {status}", show_alert=True)
    await show_manual_settings(callback)


@router.callback_query(F.data == "pay_set_card")
async def pay_set_card_start(callback: CallbackQuery, state: FSMContext):
    logger.debug(f" pay_set_card_start вызван")
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
    logger.debug(f" pay_set_sbp_phone_start вызван")
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
    logger.debug(f" pay_set_sbp_bank_start вызван")
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
    logger.debug(f" pay_set_recipient_start вызван")
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
    logger.debug(f" pay_set_instructions_start вызван")
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
@router.message(PaymentSettingsState.waiting_for_card, F.text & ~F.text.startswith("/"))
async def process_card(message: Message, state: FSMContext):
    if not await _require_admin_state(message, state):
        return

    logger.debug("Manual card details updated")
    await db.set_payment_setting('card_number', message.text.strip())
    await state.clear()
    await message.answer(f"✅ Номер карты обновлён: <code>{message.text.strip()}</code>", parse_mode="HTML")


@router.message(PaymentSettingsState.waiting_for_sbp_phone, F.text & ~F.text.startswith("/"))
async def process_sbp_phone(message: Message, state: FSMContext):
    if not await _require_admin_state(message, state):
        return

    logger.debug("SBP phone details updated")
    await db.set_payment_setting('sbp_phone', message.text.strip())
    await state.clear()
    await message.answer(f"✅ Телефон для СБП обновлён: <code>{message.text.strip()}</code>", parse_mode="HTML")


@router.message(PaymentSettingsState.waiting_for_sbp_bank, F.text & ~F.text.startswith("/"))
async def process_sbp_bank(message: Message, state: FSMContext):
    if not await _require_admin_state(message, state):
        return

    logger.debug("SBP bank details updated")
    await db.set_payment_setting('sbp_bank', message.text.strip())
    await state.clear()
    await message.answer(f"✅ Банк для СБП обновлён: {message.text.strip()}")


@router.message(PaymentSettingsState.waiting_for_recipient, F.text & ~F.text.startswith("/"))
async def process_recipient(message: Message, state: FSMContext):
    if not await _require_admin_state(message, state):
        return

    logger.debug("Manual payment recipient updated")
    await db.set_payment_setting('recipient_name', message.text.strip())
    await state.clear()
    await message.answer(f"✅ Получатель обновлён: {message.text.strip()}")


@router.message(PaymentSettingsState.waiting_for_instructions, F.text & ~F.text.startswith("/"))
async def process_instructions(message: Message, state: FSMContext):
    if not await _require_admin_state(message, state):
        return

    logger.debug("Manual payment instructions updated")
    await db.set_payment_setting('payment_instructions', message.text.strip())
    await state.clear()
    await message.answer(f"✅ Инструкция обновлена!")


# ============================================
# НАСТРОЙКИ ДОСТАВКИ
# ============================================

_DELIVERY_METHOD_SETTINGS = {
    "sdek_pickup": {
        "title": "СДЭК ПВЗ",
        "enabled_key": "delivery_sdek_pickup_enabled",
        "price_key": "delivery_sdek_pickup_price_rub",
    },
    "russian_post_pickup": {
        "title": "Почта России",
        "enabled_key": "delivery_russian_post_pickup_enabled",
        "price_key": "delivery_russian_post_pickup_price_rub",
    },
    "self_pickup": {
        "title": "Самовывоз",
        "enabled_key": "delivery_self_pickup_enabled",
        "price_key": "delivery_self_pickup_price_rub",
    },
}
_SELF_PICKUP_SETTING_STATES = {
    "location": (
        "delivery_self_pickup_location",
        DeliverySettingsState.waiting_for_self_pickup_location,
        "адрес самовывоза",
    ),
    "schedule": (
        "delivery_self_pickup_schedule",
        DeliverySettingsState.waiting_for_self_pickup_schedule,
        "график самовывоза",
    ),
    "instructions": (
        "delivery_self_pickup_instructions",
        DeliverySettingsState.waiting_for_self_pickup_instructions,
        "инструкцию самовывоза",
    ),
}


def _delivery_price_text(value: str) -> str:
    return value if value.isdecimal() else "не задана"


def _self_pickup_configured(values: dict[str, str]) -> bool:
    return all(
        values.get(key, "").strip()
        for key in (
            "delivery_self_pickup_location",
            "delivery_self_pickup_schedule",
            "delivery_self_pickup_instructions",
        )
    )


@router.callback_query(F.data == "payment_settings:delivery")
async def admin_delivery_settings(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    values = await db.get_all_payment_settings()
    enabled = values.get("delivery_enabled") == "1"
    configured = delivery_encryption_is_available()
    self_pickup_configured = _self_pickup_configured(values)
    pickup_location = escape(values.get("delivery_self_pickup_location", "").strip() or "Не задан")
    pickup_schedule = escape(values.get("delivery_self_pickup_schedule", "").strip() or "Не задан")
    pickup_instructions = escape(
        values.get("delivery_self_pickup_instructions", "").strip() or "Не задан"
    )
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔴 Отключить доставку" if enabled else "🟢 Включить доставку",
        callback_data="delivery_toggle",
    )
    for method, contract in _DELIVERY_METHOD_SETTINGS.items():
        method_enabled = values.get(contract["enabled_key"]) == "1"
        builder.button(
            text=("🔴 " if method_enabled else "🟢 ") + contract["title"],
            callback_data=f"delivery_method_toggle:{method}",
        )
        builder.button(
            text=f"💰 {contract['title']}: {_delivery_price_text(values.get(contract['price_key'], ''))} ₽",
            callback_data=f"delivery_price:{method}",
        )
    builder.button(text="📍 Адрес самовывоза", callback_data="delivery_self_pickup:location")
    builder.button(text="🕒 График самовывоза", callback_data="delivery_self_pickup:schedule")
    builder.button(text="📝 Инструкция самовывоза", callback_data="delivery_self_pickup:instructions")
    builder.button(text="◀️ В меню", callback_data="admin_menu")
    builder.adjust(1)
    methods_status = "\n".join(
        f"• {contract['title']}: {'✅ включён' if values.get(contract['enabled_key']) == '1' else '❌ выключен'} · {_delivery_price_text(values.get(contract['price_key'], ''))} ₽"
        for contract in _DELIVERY_METHOD_SETTINGS.values()
    )
    await callback.message.edit_text(
        "🚚 <b>Настройки доставки</b>\n\n"
        f"Общий статус: {'✅ включена' if enabled else '❌ выключена'}\n"
        f"Шифрование адресов: {'✅ готово' if configured else '⚠️ не настроено'}\n"
        f"Самовывоз: {'✅ адрес, график и инструкция заданы' if self_pickup_configured else '⚠️ заполните адрес, график и инструкцию'}\n"
        f"📍 Адрес: {pickup_location}\n"
        f"🕒 График: {pickup_schedule}\n"
        f"📝 Инструкция: {pickup_instructions}\n\n"
        f"{methods_status}\n\n"
        "Почта России и СДЭК используют ручной ввод трек-номера. Ключи шифрования в Telegram не отображаются.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "delivery_toggle")
async def delivery_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    current = await db.get_payment_setting("delivery_enabled", "0")
    if current != "1" and not delivery_encryption_is_available():
        await callback.answer(
            "Сначала настройте шифрование доставки в .env.", show_alert=True
        )
        return
    await db.set_payment_setting("delivery_enabled", "0" if current == "1" else "1")
    await admin_delivery_settings(callback)


@router.callback_query(F.data.regexp(r"^delivery_method_toggle:(sdek_pickup|russian_post_pickup|self_pickup)$"))
async def delivery_method_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    method = callback.data.split(":", 1)[1]
    contract = _DELIVERY_METHOD_SETTINGS[method]
    values = await db.get_all_payment_settings()
    current = values.get(contract["enabled_key"], "0")
    if current != "1" and not delivery_encryption_is_available():
        await callback.answer("Сначала настройте шифрование доставки в .env.", show_alert=True)
        return
    if method == "self_pickup" and current != "1" and not _self_pickup_configured(values):
        await callback.answer("Сначала задайте адрес, график и инструкцию самовывоза.", show_alert=True)
        return
    await db.set_payment_setting(contract["enabled_key"], "0" if current == "1" else "1")
    await admin_delivery_settings(callback)


@router.callback_query(F.data.regexp(r"^delivery_price:(sdek_pickup|russian_post_pickup|self_pickup)$"))
async def delivery_price_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await state.clear()
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    method = callback.data.split(":", 1)[1]
    await state.set_state(DeliverySettingsState.waiting_for_price)
    await state.update_data(delivery_method=method)
    await callback.message.answer(
        f"Введите фиксированную цену «{_DELIVERY_METHOD_SETTINGS[method]['title']}» в рублях (от 0 до 100000).\n\n/cancel для отмены."
    )
    await callback.answer()


@router.message(DeliverySettingsState.waiting_for_price, F.text & ~F.text.startswith("/"))
async def delivery_price_save(message: Message, state: FSMContext):
    if not await _require_admin_state(message, state):
        return
    data = await state.get_data()
    method = data.get("delivery_method")
    if method not in _DELIVERY_METHOD_SETTINGS:
        await state.clear()
        await message.answer("❌ Не удалось определить способ доставки.")
        return
    value = message.text.strip()
    if not value.isdecimal() or int(value) > 100_000:
        await message.answer("❌ Введите целую цену от 0 до 100000 ₽.")
        return
    await db.set_payment_setting(_DELIVERY_METHOD_SETTINGS[method]["price_key"], str(int(value)))
    await state.clear()
    await message.answer("✅ Цена доставки обновлена.")


@router.callback_query(F.data.regexp(r"^delivery_self_pickup:(location|schedule|instructions)$"))
async def delivery_self_pickup_setting_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await state.clear()
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    field = callback.data.split(":", 1)[1]
    _, target_state, label = _SELF_PICKUP_SETTING_STATES[field]
    await state.set_state(target_state)
    await state.update_data(self_pickup_field=field)
    await callback.message.answer(f"Введите {label} (до 500 символов).\n\n/cancel для отмены.")
    await callback.answer()


@router.message(DeliverySettingsState.waiting_for_self_pickup_location, F.text & ~F.text.startswith("/"))
@router.message(DeliverySettingsState.waiting_for_self_pickup_schedule, F.text & ~F.text.startswith("/"))
@router.message(DeliverySettingsState.waiting_for_self_pickup_instructions, F.text & ~F.text.startswith("/"))
async def delivery_self_pickup_setting_save(message: Message, state: FSMContext):
    if not await _require_admin_state(message, state):
        return
    data = await state.get_data()
    field = data.get("self_pickup_field")
    if field not in _SELF_PICKUP_SETTING_STATES:
        await state.clear()
        await message.answer("❌ Не удалось определить настройку.")
        return
    value = " ".join(message.text.split())
    if not value or len(value) > 500:
        await message.answer("❌ Введите непустой текст до 500 символов.")
        return
    setting_key, _, _ = _SELF_PICKUP_SETTING_STATES[field]
    await db.set_payment_setting(setting_key, value)
    await state.clear()
    await message.answer("✅ Настройка самовывоза сохранена.")


# ============================================
# НАСТРОЙКИ TELEGRAM STARS
async def show_stars_settings(callback: CallbackQuery):
    logger.debug(f" admin_stars_menu вызван")
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
    builder.button(text="◀️ К способам оплаты", callback_data="admin_payments")
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
        f"Stars доступны для заказов с доставкой и без неё.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "payment_settings:stars")
async def admin_stars_settings(callback: CallbackQuery):
    await show_stars_settings(callback)


@router.callback_query(F.data == "admin_stars_settings")
async def admin_stars_legacy_settings(callback: CallbackQuery):
    await show_stars_settings(callback)


@router.callback_query(F.data == "stars_toggle")
async def stars_toggle(callback: CallbackQuery):
    logger.debug(f" stars_toggle вызван")
    if not is_admin(callback.from_user.id): return

    current = await db.get_stars_setting('stars_enabled', '0')
    new_value = '0' if current == '1' else '1'
    await db.set_stars_setting('stars_enabled', new_value)

    status = "✅ Включена" if new_value == '1' else "❌ Отключена"
    await callback.answer(f"Оплата Stars {status}", show_alert=True)
    await show_stars_settings(callback)


@router.callback_query(F.data == "stars_set_rate")
async def stars_set_rate_start(callback: CallbackQuery, state: FSMContext):
    logger.debug(f" stars_set_rate_start вызван")
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


@router.message(PaymentSettingsState.waiting_for_stars_rate, F.text & ~F.text.startswith("/"))
async def process_stars_rate(message: Message, state: FSMContext):
    if not await _require_admin_state(message, state):
        return

    logger.debug(f" process_stars_rate: {message.text}")

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


@router.callback_query(F.data == "payment_settings:yookassa")
async def admin_yookassa_settings(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    payment_settings = await db.get_all_payment_settings()
    enabled = payment_settings.get("yookassa_enabled") == "1"
    configured = bool(
        settings.YOOKASSA_SHOP_ID
        and settings.YOOKASSA_SECRET_KEY
        and settings.YOOKASSA_RETURN_URL.startswith("https://")
    )
    return_url = escape(settings.YOOKASSA_RETURN_URL) if settings.YOOKASSA_RETURN_URL else "не задан"
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔴 Отключить ЮKassa" if enabled else "🟢 Включить ЮKassa",
        callback_data="yookassa_toggle",
    )
    builder.button(text="◀️ К способам оплаты", callback_data="admin_payments")
    builder.adjust(1)
    await callback.message.edit_text(
        "🟣 <b>ЮKassa</b>\n\n"
        f"Статус: {'✅ Включена' if enabled else '❌ Отключена'}\n"
        f"Конфигурация: {'✅ shopId, secret key и return URL заданы' if configured else '⚠️ Заполните YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY и YOOKASSA_RETURN_URL в .env'}\n"
        f"Return URL: <code>{return_url}</code>\n\n"
        "Секретный ключ не хранится в боте и не показывается в Telegram.\n"
        "В личном кабинете ЮKassa настройте публичный HTTPS webhook: <code>/webhooks/yookassa</code>.\n"
        "Прокси должен пропускать к этому пути только официальные IP-адреса ЮKassa.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "yookassa_toggle")
async def yookassa_toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    configured = bool(
        settings.YOOKASSA_SHOP_ID
        and settings.YOOKASSA_SECRET_KEY
        and settings.YOOKASSA_RETURN_URL.startswith("https://")
    )
    current = await db.get_payment_setting("yookassa_enabled", "0")
    if current != "1" and not configured:
        await callback.answer("Сначала задайте shopId, secret key и return URL в .env", show_alert=True)
        return
    await db.set_payment_setting("yookassa_enabled", "0" if current == "1" else "1")
    await admin_yookassa_settings(callback)


# ============================================
# ОБРАБОТКА ПЛАТЕЖЕЙ STARS
# ============================================


async def _validate_stars_payment(user_id: int, payload: str, currency: str, amount: int):
    try:
        order_id = int(payload)
    except (TypeError, ValueError):
        return None, "Заказ не найден"
    order = await db.get_order_full(order_id)
    if not order or order['user_id'] != user_id:
        return None, "Заказ не найден"
    if order['status'] != 'awaiting_stars_payment':
        return None, "Заказ уже обработан"
    if currency != 'XTR':
        return None, "Неверная валюта"
    try:
        rate = int(await db.get_stars_setting('rubles_per_star', '0'))
    except (TypeError, ValueError):
        rate = 0
    if rate <= 0:
        return None, "Некорректный курс Stars"
    expected_amount = (order['total'] + rate - 1) // rate
    if amount != expected_amount:
        return None, "Неверная сумма"
    return order, None

@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    order, error = await _validate_stars_payment(
        pre_checkout_query.from_user.id,
        pre_checkout_query.invoice_payload,
        pre_checkout_query.currency,
        pre_checkout_query.total_amount,
    )
    try:
        await bot.answer_pre_checkout_query(
            pre_checkout_query.id,
            ok=error is None,
            error_message=error,
        )
    except Exception as exc:
        logger.warning(f"Ошибка pre_checkout: {exc}")


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, bot: Bot):
    logger.debug(f" process_successful_payment вызван")
    payment = message.successful_payment

    order, error = await _validate_stars_payment(
        message.from_user.id,
        payment.invoice_payload,
        payment.currency,
        payment.total_amount,
    )
    if error:
        await message.answer(f"❌ {error}")
        return
    order_id = order['id']
    if not await db.update_order_status(
        order_id, 'paid', expected_statuses=('awaiting_stars_payment',)
    ):
        await message.answer("ℹ️ Этот платёж уже обработан.")
        return

    receipt = format_order_receipt_html(order)

    await message.answer(
        f"✅ <b>Оплата успешна!</b>\n\n"
        f"📦 Заказ #{order_id}\n"
        f"{receipt}\n\n"
        f"💰 Оплачено: {payment.total_amount} Stars\n\n"
        f"Спасибо за покупку! 🌿",
        parse_mode="HTML"
    )

    for admin_id in recipient_ids_for_event_sync("payment"):
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
async def user_confirm_payment(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Пользователь нажал 'Я оплатил'"""
    logger.debug(f" user_confirm_payment вызван: {callback.data}")

    if not callback.data.startswith("user_paid_"):
        return

    try:
        order_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка: неверный номер заказа", show_alert=True)
        return

    logger.debug(f" order_id: {order_id}")

    order = await db.get_order_full(order_id)
    if not order:
        logger.warning(f"Заказ #{order_id} не найден")
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return

    logger.debug(f" Текущий статус заказа: {order['status']}")

    if order['user_id'] != callback.from_user.id:
        await callback.answer("❌ Этот заказ принадлежит другому пользователю", show_alert=True)
        return

    if order['status'] != 'awaiting_payment':
        await callback.answer("Этот заказ уже обработан ✅", show_alert=True)
        return

    # Обновляем статус на "ожидает подтверждения"
    if not await db.update_order_status(
        order_id, 'payment_pending', expected_statuses=('awaiting_payment',)
    ):
        await callback.answer("Этот заказ уже обработан ✅", show_alert=True)
        return

    await callback.message.edit_text(
        f"✅ <b>Отлично!</b>\n\n"
        f"Мы получили информацию о вашей оплате.\n"
        f"Администратор проверит поступление и подтвердит заказ.\n\n"
        f"Обычно это занимает 5-15 минут 🕐\n\n"
        f"Номер заказа: <b>#{order_id}</b>\n"
        f"{format_order_receipt_html(order)}\n\n"
        f"Если есть вопросы — нажмите 🆘 Поддержка",
        parse_mode="HTML"
    )
    await callback.answer("✅ Информация отправлена администратору!", show_alert=True)

    # Уведомляем админов и сохраняем ID сообщений для последующего удаления
    admin_ids = []
    message_ids = []
    
    for admin_id in recipient_ids_for_event_sync("payment"):
        try:
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Подтвердить оплату", callback_data=f"admin_paid_{order_id}")

            msg = await bot.send_message(
                admin_id,
                f"💰 <b>Пользователь сообщил об оплате!</b>\n\n"
                f"📦 Заказ: #{order_id}\n"
                f"👤 Клиент: {escape(order['user_name'])} (ID: {order['user_id']})\n"
                f"{format_order_receipt_html(order)}\n\n"
                f"Проверьте поступление и подтвердите:",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            logger.info(f"Уведомление отправлено админу {admin_id}, message_id={msg.message_id}")
            admin_ids.append(admin_id)
            message_ids.append(msg.message_id)
        except Exception as e:
            logger.warning(f"Ошибка отправки админу {admin_id}: {e}")
    
    # Сохраняем ID сообщений в БД и отключаем fallback-поллер только после доставки.
    if admin_ids and message_ids:
        await save_admin_notification_ids(order_id, admin_ids, message_ids)
        await db.mark_new_order_notified(order_id)

    # Просим прислать фото чека для ускоренной проверки.
    # Скриншот придёт админу inline-превью с кнопкой подтверждения.
    skip_builder = InlineKeyboardBuilder()
    skip_builder.button(text="⏭ Без фото", callback_data=f"payment_skip_photo_{order_id}")
    await bot.send_message(
        order['user_id'],
        f"📸 Пришлите фото чека или скриншот перевода — "
        f"так администратор подтвердит оплату значительно быстрее.\n\n"
        f"Просто отправьте фото в этот чат. Если не получилось — нажмите «Без фото».",
        reply_markup=skip_builder.as_markup()
    )
    await state.set_state(PaymentReceiptState.waiting_for_photo)
    await state.update_data(order_id=order_id, requested_at=datetime.now().timestamp())
    # Срок ожидания чека — 30 минут; потом состоянием можно пренебречь.


@router.message(PaymentReceiptState.waiting_for_photo, F.photo)
async def payment_receipt_photo(message: Message, bot: Bot, state: FSMContext):
    """Пользователь прислал фото чека — отдаём админам inline-превью."""
    data = await state.get_data()
    order_id = data.get('order_id')

    # Позднее фото (по истечении окна ожидания) не привязываем к заказу
    if order_id and data.get('requested_at'):
        if datetime.now() - datetime.fromtimestamp(data['requested_at']) > RECEIPT_WAIT_TIMEOUT:
            await state.clear()
            await message.answer(
                f"⏳ Время на отправку фото чека истекло, но заказ #{order_id} уже передан "
                f"администратору на проверку. Спасибо! 🌿"
            )
            return

    if not order_id:
        await state.clear()
        await message.answer("❌ Не удалось определить заказ. Создайте заказ заново.")
        return

    order = await db.get_order_full(order_id)
    if not order:
        await state.clear()
        await message.answer("❌ Заказ не найден.")
        return
    if order['user_id'] != message.from_user.id:
        await state.clear()
        await message.answer("❌ Этот заказ принадлежит другому пользователю.")
        return

    # Если заказ уже подтверждён/обработан — фото не нужно
    if order['status'] in ('paid', 'confirmed', 'completed'):
        await state.clear()
        await message.answer(
            f"ℹ️ Заказ #{order_id} уже обработан — чек не требуется. Спасибо! 🌿"
        )
        return

    await state.clear()

    photo = message.photo[-1]
    caption = (
        f"🖼 <b>Чек к заказу #{order_id}</b>\n\n"
        f"👤 Клиент: {escape(order['user_name'])} (ID: {order['user_id']})\n"
        f"{format_order_receipt_html(order)}\n\n"
        f"Проверьте чек и подтвердите оплату:"
    )

    new_pairs = []
    for admin_id in recipient_ids_for_event_sync("payment"):
        try:
            builder = InlineKeyboardBuilder()
            builder.button(text="✅ Подтвердить оплату", callback_data=f"admin_paid_{order_id}")

            msg = await bot.send_photo(
                admin_id,
                photo=photo.file_id,
                caption=caption,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            new_pairs.append((admin_id, msg.message_id))
            logger.info(f"Чек заказа #{order_id} отправлен админу {admin_id} (фото {msg.message_id})")
        except Exception as e:
            logger.warning(f"Ошибка отправки чека админу {admin_id}: {e}")

    # Текстовый квиток у админов заменяем фото-чеком, только если фото доставлено.
    if new_pairs:
        try:
            for notif in order.get('admin_notification_ids') or []:
                admin_id = notif.get('admin_id')
                msg_id = notif.get('message_id')
                if admin_id and msg_id:
                    try:
                        await bot.delete_message(chat_id=admin_id, message_id=msg_id)
                    except Exception:
                        pass
            await replace_admin_notification_ids(order_id, new_pairs)
        except Exception as e:
            logger.warning(f"Ошибка замены текстовых уведомлений заказа #{order_id}: {e}")

    if new_pairs:
        await message.answer(
            f"✅ Фото чека отправлено администратору!\n\n"
            f"Заказ #{order_id} будет подтверждён после проверки. "
            f"Обычно это занимает 5-15 минут 🕐"
        )
    else:
        await message.answer(
            f"ℹ️ Не удалось доставить чек администратору, но заказ #{order_id} "
            f"передан на проверку. Если понадобится — администратор свяжется с вами."
        )


@router.message(PaymentReceiptState.waiting_for_photo, F.text & ~F.text.startswith("/"))
async def payment_receipt_reminder(message: Message, state: FSMContext):
    """Юзер шлёт не-фото, пока ждём чек — мягко напоминаем."""
    data = await state.get_data()
    if data.get('order_id') and data.get('requested_at'):
        if datetime.now() - datetime.fromtimestamp(data['requested_at']) > RECEIPT_WAIT_TIMEOUT:
            await state.clear()
            await message.answer(
                f"⏳ Время на отправку фото чека истекло, но заказ #{data['order_id']} уже передан "
                f"администратору на проверку. Спасибо! 🌿"
            )
            return
    await message.answer(
        "📸 Пришлите, пожалуйста, фото чека (скриншот перевода) "
        "или нажмите кнопку «⏭ Без фото», чтобы перейти дальше."
    )


@router.callback_query(PaymentReceiptState.waiting_for_photo, F.data.startswith("payment_skip_photo_"))
async def payment_skip_photo(callback: CallbackQuery, state: FSMContext):
    """Пользователь отказался от отправки фото чека."""
    data = await state.get_data()
    try:
        order_id = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        order_id = None
    if order_id != data.get('order_id'):
        await state.clear()
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return
    order = await db.get_order_full(order_id)
    if not order or order['user_id'] != callback.from_user.id:
        await state.clear()
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    await state.clear()
    text = (
        f"⏭ Хорошо, без фото.\n\n"
        f"Администратор проверит поступление по заказу #{order_id if order_id else ''} "
        f"и подтвердит оплату вручную. Обычно это занимает 5-15 минут 🕐"
    )
    try:
        await callback.message.edit_text(text)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("admin_paid_"))
async def admin_confirm_payment(callback: CallbackQuery, bot: Bot):
    """Админ подтверждает оплату"""
    logger.debug(f" admin_confirm_payment вызван: {callback.data}")
    logger.debug(f" От: {callback.from_user.id}")

    if not has_permission_sync(callback.from_user.id, "payment.reconcile"):
        logger.warning("Payment confirmation denied")
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    try:
        order_id = int(callback.data.split("_")[-1])
        logger.debug(f" order_id: {order_id}")
    except (ValueError, IndexError) as e:
        logger.warning(f"Ошибка парсинга order_id: {e}")
        await callback.answer("❌ Неверный номер заказа", show_alert=True)
        return

    order = await db.get_order_full(order_id)
    if not order:
        logger.warning(f"Заказ #{order_id} не найден")
        await callback.answer("❌ Заказ не найден", show_alert=True)
        return

    logger.debug(f" Текущий статус заказа: {order['status']}")

    if order['status'] != 'payment_pending':
        await callback.answer("Этот заказ нельзя подтвердить в текущем статусе", show_alert=True)
        return

    # Обновляем статус на "подтверждён"
    try:
        confirmed = await db.update_order_status(
            order_id, 'confirmed', expected_statuses=('payment_pending',)
        )
        if not confirmed:
            await callback.answer("Этот заказ уже обработан ✅", show_alert=True)
            return
        logger.info(f"Статус заказа #{order_id} обновлён на 'confirmed'")
    except Exception as e:
        logger.warning(f"Ошибка обновления статуса: {e}")
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)
        return

    from handlers.admin_orders import notify_confirmed_order

    await notify_confirmed_order(bot, order_id)
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
        logger.info(f"Пользователь {order['user_id']} уведомлён")
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {order['user_id']}: {e}")


from aiogram.filters import Command

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
        logger.warning(f"Ошибка получения транзакций Stars: {e}")
        await message.answer(
            f"❌ Не удалось получить данные.\n\n"
            f"💡 Полный баланс смотрите в @BotFather → Ваш бот → **Balance**"
        )