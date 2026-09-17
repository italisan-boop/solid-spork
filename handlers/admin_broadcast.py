from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
import logging
from authz import has_permission_sync
from controlplane.plan_policy import LIMIT_BROADCAST_RECIPIENTS_PER_DAY
from runtime.features import QuotaExceededError
from runtime.quota import reserve_daily_quota_sync
from db.users import get_all_users
from utils import sanitize_telegram_html
from utils.otp_confirm import (
    issue_otp,
    consume_otp,
    revoke_otp,
    OTP_TTL_SECONDS,
    ACTION_MASS_BROADCAST,
)

logger = logging.getLogger(__name__)
router = Router()


def is_admin(user_id: int) -> bool:
    return has_permission_sync(user_id, "broadcast.send")


class BroadcastState(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()
    waiting_for_code = State()


# ---------------------------------------------------------------------------
# Общая функция входа в рассылку (вызывается из кнопки И из /mass_broadcast)
# ---------------------------------------------------------------------------


async def broadcast_start_ui(target, state: FSMContext):
    """Показать экран ввода текста рассылки.

    target — CallbackQuery (callback) или Message (command).
    """
    await state.clear()

    users = await get_all_users()
    user_count = len(users) if users else 0

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_broadcast_cancel")
    builder.adjust(1)

    text = (
        f"📢 <b>Рассылка сообщений пользователям</b>\n\n"
        f"Всего пользователей: {user_count}\n\n"
        "Отправьте текст сообщения для рассылки.\n\n"
        "Поддерживается Telegram-HTML:\n"
        "<code>&lt;b&gt;</code> <code>&lt;i&gt;</code> <code>&lt;u&gt;</code> "
        "<code>&lt;s&gt;</code> <code>&lt;code&gt;</code> <code>&lt;pre&gt;</code> "
        "<code>&lt;a href=\"...\"&gt;</code>\n"
        "Остальные теги будут автоматически удалены."
    )
    markup = builder.as_markup()

    if isinstance(target, CallbackQuery):
        await target.bot.edit_message_text(
            chat_id=target.from_user.id,
            message_id=target.message.message_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML")

    await state.set_state(BroadcastState.waiting_for_message)


# ---------------------------------------------------------------------------
# Кнопка «Рассылка» в админ-панели
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "admin_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    await broadcast_start_ui(callback, state)


# ---------------------------------------------------------------------------
# Ввод текста рассылки
# ---------------------------------------------------------------------------


@router.message(BroadcastState.waiting_for_message, ~F.text.startswith("/"))
async def process_message(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return

    if not message.text or not message.text.strip():
        await message.answer("❌ Сообщение не может быть пустым. Попробуйте еще раз:")
        return

    raw_text = message.text.strip()
    text = sanitize_telegram_html(raw_text).strip()
    if not text:
        await message.answer("❌ После очистки HTML текст пустой. Попробуйте еще раз:")
        return

    await state.update_data(message_text=text)

    users = await get_all_users()
    user_count = len(users) if users else 0

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить рассылку", callback_data="admin_broadcast_confirm")
    builder.button(text="✏️ Изменить текст", callback_data="admin_broadcast_edit")
    builder.button(text="❌ Отмена", callback_data="admin_broadcast_cancel")
    builder.adjust(1)

    preview_text = text[:500] + "..." if len(text) > 500 else text

    await message.answer(
        f"📢 <b>Предпросмотр рассылки</b>\n\n"
        f"Получателей: {user_count}\n\n"
        f"{preview_text}\n\n"
        f"Подтвердите отправку или измените текст:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await state.set_state(BroadcastState.waiting_for_confirmation)


# ---------------------------------------------------------------------------
# Кнопка «Изменить текст» — вернуться к вводу
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "admin_broadcast_edit")
async def edit_broadcast_message(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    revoke_otp(callback.from_user.id, ACTION_MASS_BROADCAST)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_broadcast_cancel")
    builder.adjust(1)

    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=(
            "📢 <b>Рассылка сообщений пользователям</b>\n\n"
            "Отправьте новый текст сообщения:"
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    await state.set_state(BroadcastState.waiting_for_message)


# ---------------------------------------------------------------------------
# Кнопка «Подтвердить» → показать код (второй шаг)
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "admin_broadcast_confirm")
async def confirm_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    data = await state.get_data()
    text = data.get("message_text")

    if not text:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="❌ Ошибка: текст сообщения не найден.",
        )
        await state.clear()
        return

    users = await get_all_users()
    if not users:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="❌ Нет пользователей для рассылки.",
        )
        await state.clear()
        return

    user_count = len(users)
    code = issue_otp(callback.from_user.id, ACTION_MASS_BROADCAST)

    await state.update_data(
        message_text=text,
        recipient_count=user_count,
        status_message_id=callback.message.message_id,
    )
    await state.set_state(BroadcastState.waiting_for_code)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_broadcast_cancel")
    builder.adjust(1)

    await callback.bot.edit_message_text(
        chat_id=callback.from_user.id,
        message_id=callback.message.message_id,
        text=(
            f"🔐 <b>Второй шаг — код подтверждения</b>\n\n"
            f"Рассылка будет отправлена <b>{user_count}</b> пользователям.\n\n"
            f"Отправьте код следующим сообщением:\n"
            f"<code>{code}</code>\n\n"
            f"Код одноразовый, действует {OTP_TTL_SECONDS} сек.\n"
            f"Если передумали — /cancel."
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Ввод кода подтверждения → запуск рассылки
# ---------------------------------------------------------------------------


@router.message(BroadcastState.waiting_for_code, ~F.text.startswith("/"))
async def process_broadcast_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return

    if not message.text or not message.text.strip():
        await message.answer("❌ Ошибка: пустой код.")
        return

    code = message.text.strip()

    if not consume_otp(message.from_user.id, ACTION_MASS_BROADCAST, code):
        await message.answer(
            "✖ <b>Неверный или истёкший код.</b>\n"
            "Запустите /mass_broadcast заново.",
            parse_mode="HTML",
        )
        await state.clear()
        return

    data = await state.get_data()
    text = data.get("message_text")
    user_count = data.get("recipient_count", 0)

    if not text:
        await message.answer("❌ Ошибка: текст рассылки не найден.")
        await state.clear()
        return

    users = await get_all_users()
    user_count = len(users)
    try:
        reserve_daily_quota_sync(
            LIMIT_BROADCAST_RECIPIENTS_PER_DAY, user_count
        )
    except QuotaExceededError as error:
        await message.answer(
            f"❌ Дневной лимит рассылки исчерпан: {error.limit} получателей."
        )
        await state.clear()
        return
    success_count = 0
    fail_count = 0

    status_msg = await message.answer(
        f"📢 <b>Запуск рассылки...</b>\n\n"
        f"Всего получателей: {user_count}\n"
        f"Отправлено: 0/{user_count}",
        parse_mode="HTML",
    )

    for i, user in enumerate(users):
        try:
            await message.bot.send_message(
                chat_id=user["user_id"], text=text, parse_mode="HTML"
            )
            success_count += 1
        except Exception as e:
            logger.error(
                f"Не удалось отправить сообщение пользователю {user['user_id']}: {e}"
            )
            fail_count += 1

        if (i + 1) % 10 == 0 or i == user_count - 1:
            try:
                await status_msg.edit_text(
                    (
                        f"📢 <b>Рассылка в процессе...</b>\n\n"
                        f"Всего получателей: {user_count}\n"
                        f"Отправлено: {success_count + fail_count}/{user_count}\n"
                        f"Успешно: {success_count}\n"
                        f"Ошибок: {fail_count}"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 В меню админа", callback_data="admin_menu")
    builder.adjust(1)

    await status_msg.edit_text(
        (
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"Всего получателей: {user_count}\n"
            f"Успешно отправлено: {success_count}\n"
            f"Ошибок: {fail_count}\n\n"
            f"Сообщение: {text[:100]}..."
        ),
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )

    logger.info(
        f"Рассылка выполнена админом {message.from_user.id}: "
        f"{success_count} успешно, {fail_count} ошибок"
    )
    await state.clear()


# ---------------------------------------------------------------------------
# Кнопка «Отмена» (из сообщения с кодом)
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "admin_broadcast_cancel")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await state.clear()
    revoke_otp(callback.from_user.id, ACTION_MASS_BROADCAST)

    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 В меню админа", callback_data="admin_menu")
    builder.adjust(1)

    try:
        await callback.bot.edit_message_text(
            chat_id=callback.from_user.id,
            message_id=callback.message.message_id,
            text="❌ <b>Рассылка отменена</b>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer()
