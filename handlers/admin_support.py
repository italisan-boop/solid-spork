"""Нативный Reply, кнопки и меню поддержки для админов.

Роутер зарегистрирован ДО user.router, поэтому:
  - нативный reply (без команд) перехватывает ответы на уведомления
    до того, как universal_text_handler в user.py их «проглотит»;
  - FSM-состояние SupportReplyState.waiting_for_text также обрабатывается
    здесь первым, и новый текст от админа отправляется пользователю
    как ответ поддержки.
"""
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
from config import settings
from handlers.user import (
    is_admin,
    support_msg_owner,
    support_claims,
    support_last_msg_name,
    support_last_msg_text,
    _send_admin_reply,
    _claim_ticket,
    _release_ticket,
    _build_history_text,
    _ticket_action_markup,
)
from states import SupportReplyState

logger = logging.getLogger(__name__)
router = Router()


# ============================================================
# 1. Нативный reply — ответ «без команд» на уведомление/ответ
# ============================================================

def _is_tracked_admin_reply(message: Message) -> bool:
    """Предикат-фильтр для message-observer.

    Пропускаем сообщение, только если:
      - это текст (не команда),
      - оно ответ (reply_to_message) на сообщение, которое мы
        отслеживали (bot notification / ответ коллеги / предыдущий
        ответ в цепочке),
      - автор — админ.
    В остальных случаях (False) сообщение дойдёт до universal_text_handler
    и будет обработано как обычно (catalog/search/ …).
    """
    if not message.text or message.text.startswith("/"):
        return False
    if not message.reply_to_message:
        return False
    if not is_admin(message.from_user.id):
        return False
    key = (message.chat.id, message.reply_to_message.message_id)
    return key in support_msg_owner


@router.message(_is_tracked_admin_reply)
async def native_admin_reply(message: Message):
    """Админ нажал Telegram-«Ответить» на уведомление/ответ — без команд."""
    key = (message.chat.id, message.reply_to_message.message_id)
    user_id = support_msg_owner.get(key)
    if user_id is None:
        return
    ok, status_text = await _send_admin_reply(message, user_id, message.text)
    await message.answer(status_text, parse_mode="HTML")


# ============================================================
# 2. Кнопка «⚡ Ответить» — FSM: ждём текст, потом отправляем
# ============================================================

@router.callback_query(F.data.startswith("support_reply:"))
async def cb_support_reply(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    await state.clear()
    await state.set_state(SupportReplyState.waiting_for_text)
    await state.update_data(reply_user_id=user_id)
    # Запоминаем связь «это сообщение → тикет», чтобы цепочка reply работала
    support_msg_owner[(callback.message.chat.id, callback.message.message_id)] = user_id
    await callback.message.answer(
        "✍️ Введите текст ответа или шаблон (+greeting, +wait, +resolved, +ask_details, +payment).\n"
        "Чтобы отменить — /cancel",
    )
    await callback.answer()


@router.message(SupportReplyState.waiting_for_text, F.text & ~F.text.startswith("/"))
async def deferred_admin_reply(message: Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("reply_user_id")
    await state.clear()
    if not user_id:
        await message.answer("⚠️ Тикет не определён. Начните заново через кнопку.")
        return
    ok, status_text = await _send_admin_reply(message, user_id, message.text)
    await message.answer(status_text, parse_mode="HTML")


# ============================================================
# 3. Кнопка «📜 История»
# ============================================================

@router.callback_query(F.data.startswith("support_history:"))
async def cb_support_history(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    support_msg_owner[(callback.message.chat.id, callback.message.message_id)] = user_id
    text = _build_history_text(user_id)
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


# ============================================================
# 4. Кнопки «🔒 Взять» / «🔓 Отпустить»
# ============================================================

@router.callback_query(F.data.startswith("support_claim:"))
async def cb_support_claim(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    status = await _claim_ticket(user_id, callback.from_user.id, callback.from_user.full_name, callback.bot)
    await callback.answer(status, show_alert=True)


@router.callback_query(F.data.startswith("support_release:"))
async def cb_support_release(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    status = _release_ticket(user_id)
    await callback.answer(status, show_alert=True)


# ============================================================
# 5. Кнопка «🎧 Поддержка» — список открытых тикетов
# ============================================================

async def _get_active_support_user_ids() -> list[int]:
    """Собрать все user_id в режиме поддержки (in-memory + БД)."""
    ids = set(settings.support_pending_users)
    try:
        rows = await db.get_all_support_active_user_ids()
        ids.update(rows)
    except Exception as e:
        logger.warning(f"Не удалось загрузить активные диалоги из БД: {e}")
    return sorted(ids, reverse=True)


def _build_support_menu_text(user_ids: list[int]) -> str:
    if not user_ids:
        return "🎧 <b>Открытых диалогов поддержки нет.</b>\n\nКогда пользователь напишет — здесь появится список."
    lines = [f"🎧 <b>Открытые диалоги</b> ({len(user_ids)})\n"]
    for uid in user_ids:
        name = support_last_msg_name.get(uid, str(uid))
        last = support_last_msg_text.get(uid, "")
        preview = last if len(last) <= 60 else last[:60] + "…"
        claimed = support_claims.get(uid)
        status = f"🔒 {claimed}" if claimed else ""
        lines.append(f"• <b>{name}</b> (ID <code>{uid}</code>) {status}")
        if preview:
            lines.append(f"  <i>{preview}</i>")
    return "\n".join(lines)


def _build_support_menu_markup(user_ids: list[int]):
    builder = InlineKeyboardBuilder()
    for uid in user_ids:
        name = support_last_msg_name.get(uid, str(uid))
        label = f"👤 {name} ({uid})"
        builder.button(text=label, callback_data=f"support_ticket:{uid}")
    if user_ids:
        builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data == "admin_support_menu")
async def cb_support_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    user_ids = await _get_active_support_user_ids()
    text = _build_support_menu_text(user_ids)
    markup = _build_support_menu_markup(user_ids)
    # Пытаемся отредактировать текущее сообщение, иначе шлём новое
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


# ============================================================
# 6. Карточка тикета из списка (кнопка «👤 name»)
# ============================================================

@router.callback_query(F.data.startswith("support_ticket:"))
async def cb_support_ticket(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    name = support_last_msg_name.get(user_id, str(user_id))
    last = support_last_msg_text.get(user_id, "—")
    preview = last if len(last) <= 120 else last[:120] + "…"
    claimed = support_claims.get(user_id)
    status = f"🔒 Взят в работу: ID <code>{claimed}</code>" if claimed else "📝 Свободен"
    text = (
        f"👤 <b>{name}</b> (ID <code>{user_id}</code>)\n"
        f"📌 {status}\n\n"
        f"💬 Последнее сообщение:\n<code>{preview}</code>\n\n"
        f"История: <code>/history_{user_id}</code>\n"
        f"Ответить: <code>/reply_{user_id} текст</code>\n"
        f"Шаблоны: <code>/reply_{user_id} +greeting</code>"
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=_ticket_action_markup(user_id),
            parse_mode="HTML",
        )
    except Exception:
        await callback.message.answer(
            text,
            reply_markup=_ticket_action_markup(user_id),
            parse_mode="HTML",
        )
    await callback.answer()
