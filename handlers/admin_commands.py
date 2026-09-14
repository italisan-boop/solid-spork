import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from config import settings
from states import CriticalActionState
from utils.otp_confirm import (
    issue_otp,
    consume_otp,
    ACTION_DROP_CACHE,
    ACTION_MASS_BROADCAST,
    OTP_TTL_SECONDS,
)

logger = logging.getLogger(__name__)
router = Router()


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


# ---------------------------------------------------------------------------
# Сброс кэша поддержки
# ---------------------------------------------------------------------------

_CODE_TEXT = (
    "🔐 <b>Подтверждение критичного действия</b>\n\n"
    "Одноразовый код: <code>{code}</code>\n\n"
    "Для подтверждения отправьте этот код следующим сообщением.\n"
    "Код действителен {ttl} сек. Если передумали — нажмите кнопку «Назад» в меню администратора."
)


async def _start_drop_cache_flow(admin_id: int, state: FSMContext) -> str:
    """Выдать OTP и войти в состояние ожидания кода. Возвращает текст с кодом."""
    code = issue_otp(admin_id, ACTION_DROP_CACHE)
    await state.set_state(CriticalActionState.waiting_for_code)
    await state.update_data(action=ACTION_DROP_CACHE)
    return (
        "🗑 <b>Сброс кэша поддержки</b>\n\n"
        "Будут сброшены:\n"
        "• закреплённые тикеты\n"
        "• активные диалоги поддержки\n\n"
        + _CODE_TEXT.format(code=code, ttl=OTP_TTL_SECONDS)
    )


@router.message(Command("drop_cache"))
async def cmd_drop_cache(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return
    await message.answer(
        await _start_drop_cache_flow(message.from_user.id, state),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_drop_cache")
async def cb_drop_cache(callback: CallbackQuery, state: FSMContext):
    """Кнопка «Сброс кэша» в админ-панели."""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    await callback.message.answer(
        await _start_drop_cache_flow(callback.from_user.id, state),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CriticalActionState.waiting_for_code, ~F.text.startswith("/"))
async def submit_drop_cache_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return

    data = await state.get_data()
    expected_action = data.get("action")

    if expected_action != ACTION_DROP_CACHE:
        await state.clear()
        return

    code = (message.text or "").strip()
    if not code:
        return

    if not consume_otp(message.from_user.id, ACTION_DROP_CACHE, code):
        await message.answer(
            "✖ <b>Неверный или истёкший код.</b>\n"
            "Запустите сброс кэша через кнопку в меню администратора.",
            parse_mode="HTML",
        )
        await state.clear()
        return

    # ------- execute -------
    # Чистим живой словарь тикетов (handlers/user.py)
    from handlers.user import support_claims

    support_claims.clear()
    settings.support_pending_users.clear()

    await state.clear()
    await message.answer(
        "✅ <b>Кэш поддержки сброшен</b>\n\n"
        "• закреплённые тикеты отпущены\n"
        "• активные диалоги сброшены",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /mass_broadcast — быстрый вход в рассылку (через command вместо кнопки)
# ---------------------------------------------------------------------------


@router.message(Command("mass_broadcast"))
async def cmd_mass_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return

    from handlers.admin_broadcast import broadcast_start_ui

    await broadcast_start_ui(message, state)
