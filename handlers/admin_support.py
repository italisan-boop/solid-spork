"""Нативный Reply, кнопки и меню поддержки для админов.

Роутер зарегистрирован ДО user.router, поэтому:
  - нативный reply (без команд) перехватывает ответы на уведомления
    до того, как universal_text_handler в user.py их «проглотит»;
  - FSM-состояние SupportReplyState.waiting_for_text также обрабатывается
    здесь первым, и новый текст от админа отправляется пользователю
    как ответ поддержки.
"""
import logging
import re
import html
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
from config import settings
from content_defaults import QUICK_TEMPLATE_KEYS, TEMPLATES_BY_KEY
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


def _short_alert(status: str, limit: int = 140) -> str:
    """Алерт кнопки ограничен 200 символами и не поддерживает HTML:
    оставляем первую строку статуса без тегов и укорачиваем."""
    first = status.replace("</code>", "").replace("<code>", "")
    first = re.sub(r"<[^>]+>", "", first).strip()
    if len(first) > limit:
        first = first[: limit - 1] + "…"
    return first


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

def _support_reply_markup(user_id: int):
    builder = InlineKeyboardBuilder()
    for alias, key in QUICK_TEMPLATE_KEYS.items():
        builder.button(
            text=TEMPLATES_BY_KEY[key].title,
            callback_data=f"support_template:{user_id}:{alias}",
        )
    builder.button(text="✍️ Свой ответ", callback_data=f"support_custom_reply:{user_id}")
    builder.button(text="◀️ К тикету", callback_data=f"support_ticket:{user_id}")
    builder.adjust(2, 2, 1, 1, 1)
    return builder.as_markup()


@router.callback_query(F.data.startswith("support_reply:"))
async def cb_support_reply(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    await state.clear()
    support_msg_owner[(callback.message.chat.id, callback.message.message_id)] = user_id
    await callback.message.answer(
        "⚡ <b>Выберите шаблон ответа или напишите свой текст:</b>",
        reply_markup=_support_reply_markup(user_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("support_template:"))
async def cb_support_template(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    _, raw_user_id, alias = callback.data.split(":", 2)
    if alias not in QUICK_TEMPLATE_KEYS:
        await callback.answer("❌ Шаблон не найден", show_alert=True)
        return
    await state.clear()
    ok, status_text = await _send_admin_reply(
        callback.message,
        int(raw_user_id),
        f"+{alias}",
        admin_id=callback.from_user.id,
        admin_name=callback.from_user.full_name,
    )
    await callback.message.answer(status_text, parse_mode="HTML")
    await callback.answer("Шаблон отправлен" if ok else "Не удалось отправить", show_alert=not ok)


@router.callback_query(F.data.startswith("support_custom_reply:"))
async def cb_support_custom_reply(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    await state.clear()
    await state.set_state(SupportReplyState.waiting_for_text)
    await state.update_data(reply_user_id=user_id)
    support_msg_owner[(callback.message.chat.id, callback.message.message_id)] = user_id
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Отмена", callback_data="support_reply_cancel")
    await callback.message.answer("✍️ Введите текст ответа:", reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "support_reply_cancel")
async def cb_support_reply_cancel(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    await state.clear()
    await cb_support_menu(callback)


@router.message(SupportReplyState.waiting_for_text, F.text == "/cancel")
async def support_reply_cancel_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return
    from handlers.user import cancel_action

    await cancel_action(message, state)

@router.message(SupportReplyState.waiting_for_text, F.text & ~F.text.startswith("/"))
async def deferred_admin_reply(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        await message.answer("❌ Нет прав администратора.")
        return

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
    try:
        await callback.message.edit_reply_markup(reply_markup=_ticket_action_markup(user_id))
    except Exception:
        pass
    await callback.answer(_short_alert(status), show_alert=True)


@router.callback_query(F.data.startswith("support_release:"))
async def cb_support_release(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    status = _release_ticket(user_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=_ticket_action_markup(user_id))
    except Exception:
        pass
    await callback.answer(_short_alert(status), show_alert=True)


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
        safe_name = html.escape(name, quote=False)
        safe_preview = html.escape(preview, quote=False)
        lines.append(f"• <b>{safe_name}</b> (ID <code>{uid}</code>) {status}")
        if preview:
            lines.append(f"  <i>{safe_preview}</i>")
    return "\n".join(lines)


def _build_support_menu_markup(user_ids: list[int]):
    builder = InlineKeyboardBuilder()
    for uid in user_ids:
        name = support_last_msg_name.get(uid, str(uid))
        label = f"👤 {name} ({uid})"
        builder.button(text=label, callback_data=f"support_ticket:{uid}")
    builder.button(text="◀️ В админ-панель", callback_data="admin_menu")
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
    safe_name = html.escape(name, quote=False)
    safe_preview = html.escape(preview, quote=False)
    text = (
        f"👤 <b>{safe_name}</b> (ID <code>{user_id}</code>)\n"
        f"📌 {status}\n\n"
        f"💬 Последнее сообщение:\n<code>{safe_preview}</code>\n\n"
        "Используйте кнопки ниже или Telegram Reply для работы с тикетом."
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
